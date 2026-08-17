from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from .domain import require_hex64, require_safe_ref
from .errors import SoulApplicationError


ReplyLength = Literal["short", "medium"]
QuestionUsage = Literal["none", "one"]
ReplyTone = Literal["natural", "warm", "playful"]
DelayedOutcome = Literal[
    "positive_engagement", "negative_engagement", "no_response"
]


@dataclass(frozen=True, slots=True)
class TrialDraft:
    trial_id: str
    application_intent_id: str
    instance_id: str
    before_evidence_id: str
    conversation_ref: str
    pending_generation_ref: str
    transcript_revision: str
    scope_commitment_sha256: str
    draft_sha256: str
    strategy: Mapping[str, str]
    prompt_version: str
    persona_version: str
    memory_version: int
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class DelayedOutcomeEvidence:
    status: DelayedOutcome
    evidence_ref: str
    elapsed_seconds: int | None = None
    no_new_inbound_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class StrategyRecommendation:
    strategy: Mapping[str, str]
    strategy_key: str
    alpha: float
    beta: float
    explicit_outcomes: int
    revision: int


class ReplyLearning(Protocol):
    def begin_trial(self, draft: TrialDraft) -> Mapping[str, Any]: ...

    def bind_owner(
        self, trial_id: str, owner_ref: str, status: str
    ) -> Mapping[str, Any]: ...

    def record_send_proof(
        self, trial_id: str, *, owner_ref: str, status: str, proof_ref: str
    ) -> Mapping[str, Any]: ...

    def record_delayed_outcome(
        self, trial_id: str, evidence: DelayedOutcomeEvidence
    ) -> StrategyRecommendation: ...

    def observe_pending_inbound(
        self,
        *,
        conversation_ref: str,
        pending_generation_ref: str,
        transcript_revision: str,
        evidence_ref: str,
    ) -> StrategyRecommendation | None: ...

    def recommend_strategy(self) -> StrategyRecommendation: ...


_DEFAULT_STRATEGY = {
    "reply_length": "short",
    "question_usage": "one",
    "tone": "natural",
}
_STRATEGY_ARMS = (
    _DEFAULT_STRATEGY,
    {"reply_length": "short", "question_usage": "none", "tone": "warm"},
    {"reply_length": "medium", "question_usage": "one", "tone": "warm"},
    {"reply_length": "short", "question_usage": "one", "tone": "playful"},
)
_SEND_STATUSES = {
    "drafted",
    "reserved",
    "confirmed",
    "uncertain",
    "terminal_no_replay",
}
_MODEL_METADATA = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,191}")


class ReplyLearningStore:
    """Soul-owned delayed-outcome learner.

    It stores commitments and versioned strategy metadata, never message or
    draft bodies. A confirmed send establishes lineage only. Posterior updates
    require a later, explicit owner-derived outcome.
    """

    def __init__(
        self, database_path: Path | str, *, no_response_seconds: int = 24 * 60 * 60
    ) -> None:
        if no_response_seconds < 60:
            raise ValueError("no_response_seconds must be at least 60")
        self.database_path = Path(database_path)
        self.no_response_seconds = no_response_seconds
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_lock = threading.Lock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate(self) -> None:
        with self._migration_lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS soul_reply_trials (
                        trial_id TEXT PRIMARY KEY,
                        application_intent_id TEXT NOT NULL UNIQUE,
                        instance_id TEXT NOT NULL,
                        before_evidence_id TEXT NOT NULL,
                        conversation_ref TEXT NOT NULL,
                        pending_generation_ref TEXT NOT NULL,
                        transcript_revision TEXT NOT NULL,
                        scope_commitment_sha256 TEXT NOT NULL,
                        draft_sha256 TEXT NOT NULL,
                        strategy_key TEXT NOT NULL,
                        strategy_json TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        persona_version TEXT NOT NULL,
                        memory_version INTEGER NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        owner_ref TEXT,
                        send_status TEXT NOT NULL,
                        send_proof_ref TEXT,
                        delayed_outcome TEXT NOT NULL DEFAULT 'pending',
                        delayed_evidence_ref TEXT,
                        created_at TEXT NOT NULL,
                        reserved_at TEXT,
                        confirmed_at TEXT,
                        outcome_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_soul_reply_trials_delayed
                    ON soul_reply_trials(conversation_ref, send_status, delayed_outcome, confirmed_at);

                    CREATE TABLE IF NOT EXISTS soul_reply_strategy_posteriors (
                        strategy_key TEXT PRIMARY KEY,
                        strategy_json TEXT NOT NULL,
                        alpha REAL NOT NULL,
                        beta REAL NOT NULL,
                        explicit_outcomes INTEGER NOT NULL,
                        revision INTEGER NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS soul_reply_inbound_evidence (
                        conversation_ref TEXT NOT NULL,
                        pending_generation_ref TEXT NOT NULL,
                        transcript_revision TEXT NOT NULL,
                        evidence_ref TEXT NOT NULL,
                        attributed_trial_id TEXT,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY (conversation_ref, pending_generation_ref),
                        FOREIGN KEY (attributed_trial_id)
                            REFERENCES soul_reply_trials(trial_id)
                    );
                    """
                )
                now = _now()
                for arm in _STRATEGY_ARMS:
                    strategy_json, strategy_key = _strategy(arm)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO soul_reply_strategy_posteriors
                        (strategy_key,strategy_json,alpha,beta,explicit_outcomes,revision,updated_at)
                        VALUES (?,?,1.0,1.0,0,0,?)
                        """,
                        (strategy_key, strategy_json, now),
                    )
            finally:
                conn.close()

    def begin_trial(self, draft: TrialDraft) -> Mapping[str, Any]:
        values = _validate_trial(draft)
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM soul_reply_trials WHERE trial_id=? OR application_intent_id=?",
                (draft.trial_id, draft.application_intent_id),
            ).fetchone()
            if existing is not None:
                if _trial_commitment(existing) != values["commitment"]:
                    raise SoulApplicationError("reply_trial_id_conflict")
                conn.commit()
                return _public_trial(existing)
            conn.execute(
                """
                INSERT INTO soul_reply_trials (
                    trial_id,application_intent_id,instance_id,before_evidence_id,
                    conversation_ref,pending_generation_ref,transcript_revision,
                    scope_commitment_sha256,draft_sha256,strategy_key,strategy_json,
                    prompt_version,persona_version,memory_version,provider,model,
                    send_status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    draft.trial_id,
                    draft.application_intent_id,
                    draft.instance_id,
                    draft.before_evidence_id,
                    draft.conversation_ref,
                    draft.pending_generation_ref,
                    draft.transcript_revision,
                    draft.scope_commitment_sha256,
                    draft.draft_sha256,
                    values["strategy_key"],
                    values["strategy_json"],
                    draft.prompt_version,
                    draft.persona_version,
                    draft.memory_version,
                    draft.provider,
                    draft.model,
                    "drafted",
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO soul_reply_strategy_posteriors
                (strategy_key,strategy_json,alpha,beta,explicit_outcomes,revision,updated_at)
                VALUES (?,?,1.0,1.0,0,0,?)
                """,
                (values["strategy_key"], values["strategy_json"], now),
            )
            row = conn.execute(
                "SELECT * FROM soul_reply_trials WHERE trial_id=?", (draft.trial_id,)
            ).fetchone()
            conn.commit()
            assert row is not None
            return _public_trial(row)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def bind_owner(
        self, trial_id: str, owner_ref: str, status: str
    ) -> Mapping[str, Any]:
        trial_id = require_safe_ref(trial_id, "reply_trial_id_invalid")
        owner_ref = require_safe_ref(owner_ref, "owner_ref_invalid", maximum=96)
        if status not in {"reserved", "reserve_replayed"}:
            raise SoulApplicationError("reply_trial_owner_status_invalid")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM soul_reply_trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if row is None:
                raise SoulApplicationError("reply_trial_not_found")
            if row["owner_ref"] and row["owner_ref"] != owner_ref:
                raise SoulApplicationError("reply_trial_owner_conflict")
            if row["send_status"] not in {"drafted", "reserved"}:
                if row["owner_ref"] == owner_ref:
                    conn.commit()
                    return _public_trial(row)
                raise SoulApplicationError("reply_trial_owner_conflict")
            conn.execute(
                """UPDATE soul_reply_trials
                   SET owner_ref=?,send_status='reserved',reserved_at=COALESCE(reserved_at,?)
                   WHERE trial_id=?""",
                (owner_ref, _now(), trial_id),
            )
            result = conn.execute(
                "SELECT * FROM soul_reply_trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            conn.commit()
            assert result is not None
            return _public_trial(result)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def record_send_proof(
        self, trial_id: str, *, owner_ref: str, status: str, proof_ref: str
    ) -> Mapping[str, Any]:
        trial_id = require_safe_ref(trial_id, "reply_trial_id_invalid")
        owner_ref = require_safe_ref(owner_ref, "owner_ref_invalid", maximum=96)
        proof_ref = require_safe_ref(proof_ref, "send_proof_ref_invalid")
        normalized = {
            "confirmed": "confirmed",
            "uncertain_needs_reconciliation": "uncertain",
            "terminal_no_replay": "terminal_no_replay",
            "stale_preflight": "terminal_no_replay",
            "preclick_rejected": "terminal_no_replay",
        }.get(status)
        if normalized is None:
            raise SoulApplicationError("send_proof_status_invalid")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM soul_reply_trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if row is None:
                raise SoulApplicationError("reply_trial_not_found")
            if row["owner_ref"] != owner_ref:
                raise SoulApplicationError("reply_trial_owner_conflict")
            if row["send_status"] == normalized and row["send_proof_ref"] == proof_ref:
                conn.commit()
                return _public_trial(row)
            if row["send_status"] == "confirmed" and normalized != "confirmed":
                raise SoulApplicationError("reply_trial_send_proof_conflict")
            if row["send_status"] == "terminal_no_replay" and normalized != "terminal_no_replay":
                raise SoulApplicationError("reply_trial_send_proof_conflict")
            confirmed_at = _now() if normalized == "confirmed" else None
            conn.execute(
                """UPDATE soul_reply_trials
                   SET send_status=?,send_proof_ref=?,confirmed_at=COALESCE(confirmed_at,?)
                   WHERE trial_id=?""",
                (normalized, proof_ref, confirmed_at, trial_id),
            )
            result = conn.execute(
                "SELECT * FROM soul_reply_trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            conn.commit()
            assert result is not None
            return _public_trial(result)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def record_delayed_outcome(
        self, trial_id: str, evidence: DelayedOutcomeEvidence
    ) -> StrategyRecommendation:
        trial_id = require_safe_ref(trial_id, "reply_trial_id_invalid")
        if evidence.status not in {
            "positive_engagement",
            "negative_engagement",
            "no_response",
        }:
            raise SoulApplicationError("delayed_outcome_invalid")
        if evidence.status == "no_response" and (
            isinstance(evidence.elapsed_seconds, bool)
            or not isinstance(evidence.elapsed_seconds, int)
            or evidence.elapsed_seconds < self.no_response_seconds
            or evidence.no_new_inbound_confirmed is not True
        ):
            raise SoulApplicationError("no_response_evidence_incomplete")
        evidence_ref = require_safe_ref(
            evidence.evidence_ref, "delayed_evidence_ref_invalid"
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = self._record_outcome_locked(
                conn, trial_id, evidence.status, evidence_ref
            )
            conn.commit()
            return result
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def observe_pending_inbound(
        self,
        *,
        conversation_ref: str,
        pending_generation_ref: str,
        transcript_revision: str,
        evidence_ref: str,
    ) -> StrategyRecommendation | None:
        conversation_ref = require_hex64(conversation_ref, "conversation_ref_invalid")
        pending_generation_ref = require_hex64(
            pending_generation_ref, "pending_generation_ref_invalid"
        )
        require_hex64(transcript_revision, "transcript_revision_invalid")
        evidence_ref = require_safe_ref(evidence_ref, "delayed_evidence_ref_invalid")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            seen = conn.execute(
                """SELECT 1 FROM soul_reply_inbound_evidence
                   WHERE conversation_ref=? AND pending_generation_ref=?""",
                (conversation_ref, pending_generation_ref),
            ).fetchone()
            if seen is not None:
                conn.commit()
                return None
            row = conn.execute(
                """
                SELECT trial_id,transcript_revision FROM soul_reply_trials
                WHERE conversation_ref=? AND send_status='confirmed'
                  AND delayed_outcome='pending' AND pending_generation_ref<>?
                ORDER BY confirmed_at DESC, created_at DESC LIMIT 1
                """,
                (conversation_ref, pending_generation_ref),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO soul_reply_inbound_evidence VALUES (?,?,?,?,NULL,?)""",
                    (
                        conversation_ref,
                        pending_generation_ref,
                        transcript_revision,
                        evidence_ref,
                        _now(),
                    ),
                )
                conn.commit()
                return None
            if row["transcript_revision"] == transcript_revision:
                raise SoulApplicationError("pending_lineage_inconsistent")
            result = self._record_outcome_locked(
                conn, row["trial_id"], "positive_engagement", evidence_ref
            )
            conn.execute(
                """INSERT INTO soul_reply_inbound_evidence VALUES (?,?,?,?,?,?)""",
                (
                    conversation_ref,
                    pending_generation_ref,
                    transcript_revision,
                    evidence_ref,
                    row["trial_id"],
                    _now(),
                ),
            )
            conn.commit()
            return result
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _record_outcome_locked(
        self,
        conn: sqlite3.Connection,
        trial_id: str,
        outcome: DelayedOutcome,
        evidence_ref: str,
    ) -> StrategyRecommendation:
        row = conn.execute(
            "SELECT * FROM soul_reply_trials WHERE trial_id=?", (trial_id,)
        ).fetchone()
        if row is None:
            raise SoulApplicationError("reply_trial_not_found")
        if row["send_status"] != "confirmed":
            raise SoulApplicationError("delayed_outcome_requires_confirmed_send")
        if outcome == "no_response":
            newer_inbound = conn.execute(
                """
                SELECT 1 FROM soul_reply_inbound_evidence
                WHERE conversation_ref=? AND pending_generation_ref<>?
                  AND observed_at>?
                LIMIT 1
                """,
                (
                    row["conversation_ref"],
                    row["pending_generation_ref"],
                    row["confirmed_at"] or "",
                ),
            ).fetchone()
            if newer_inbound is not None:
                raise SoulApplicationError("no_response_contradicted_by_new_inbound")
        if row["delayed_outcome"] != "pending":
            if (
                row["delayed_outcome"] == outcome
                and row["delayed_evidence_ref"] == evidence_ref
            ):
                posterior = conn.execute(
                    "SELECT * FROM soul_reply_strategy_posteriors WHERE strategy_key=?",
                    (row["strategy_key"],),
                ).fetchone()
                assert posterior is not None
                return _recommendation(posterior, _global_revision(conn))
            raise SoulApplicationError("delayed_outcome_conflict")
        alpha_increment = 1.0 if outcome == "positive_engagement" else 0.0
        beta_increment = 0.0 if outcome == "positive_engagement" else 1.0
        now = _now()
        conn.execute(
            """UPDATE soul_reply_trials
               SET delayed_outcome=?,delayed_evidence_ref=?,outcome_at=?
               WHERE trial_id=?""",
            (outcome, evidence_ref, now, trial_id),
        )
        conn.execute(
            """UPDATE soul_reply_strategy_posteriors
               SET alpha=alpha+?,beta=beta+?,explicit_outcomes=explicit_outcomes+1,
                   revision=revision+1,updated_at=? WHERE strategy_key=?""",
            (alpha_increment, beta_increment, now, row["strategy_key"]),
        )
        posterior = conn.execute(
            "SELECT * FROM soul_reply_strategy_posteriors WHERE strategy_key=?",
            (row["strategy_key"],),
        ).fetchone()
        assert posterior is not None
        return _recommendation(posterior, _global_revision(conn))

    def recommend_strategy(self) -> StrategyRecommendation:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM soul_reply_strategy_posteriors"
            ).fetchall()
            if rows:
                order = {
                    _strategy(arm)[1]: index
                    for index, arm in enumerate(_STRATEGY_ARMS)
                }
                row = min(
                    rows,
                    key=lambda item: (
                        int(item["explicit_outcomes"]),
                        -(float(item["alpha"]) / (float(item["alpha"]) + float(item["beta"]))),
                        order.get(item["strategy_key"], len(order)),
                        item["strategy_key"],
                    ),
                )
                return _recommendation(row, _global_revision(conn))
        finally:
            conn.close()
        strategy_json, strategy_key = _strategy(_DEFAULT_STRATEGY)
        return StrategyRecommendation(
            json.loads(strategy_json), strategy_key, 1.0, 1.0, 0, 0
        )

    def get_trial(self, trial_id: str) -> Mapping[str, Any] | None:
        trial_id = require_safe_ref(trial_id, "reply_trial_id_invalid")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM soul_reply_trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            return _public_trial(row) if row is not None else None
        finally:
            conn.close()


def _validate_trial(draft: TrialDraft) -> dict[str, str]:
    require_safe_ref(draft.trial_id, "reply_trial_id_invalid")
    require_safe_ref(draft.application_intent_id, "application_intent_id_invalid", maximum=160)
    require_safe_ref(draft.instance_id, "reply_instance_id_invalid")
    require_safe_ref(draft.before_evidence_id, "before_evidence_id_invalid")
    require_hex64(draft.conversation_ref, "conversation_ref_invalid")
    require_hex64(draft.pending_generation_ref, "pending_generation_ref_invalid")
    require_hex64(draft.transcript_revision, "transcript_revision_invalid")
    require_hex64(draft.scope_commitment_sha256, "scope_commitment_invalid")
    require_hex64(draft.draft_sha256, "draft_commitment_invalid")
    for value, code in (
        (draft.prompt_version, "prompt_version_invalid"),
        (draft.persona_version, "persona_version_invalid"),
    ):
        require_safe_ref(value, code)
    for value, code in (
        (draft.provider, "reply_provider_invalid"),
        (draft.model, "reply_model_invalid"),
    ):
        if not isinstance(value, str) or not _MODEL_METADATA.fullmatch(value):
            raise SoulApplicationError(code)
    if isinstance(draft.memory_version, bool) or not isinstance(draft.memory_version, int) or draft.memory_version < 0:
        raise SoulApplicationError("memory_version_invalid")
    strategy_json, strategy_key = _strategy(draft.strategy)
    commitment = hashlib.sha256(
        json.dumps(
            {
                "trial_id": draft.trial_id,
                "application_intent_id": draft.application_intent_id,
                "instance_id": draft.instance_id,
                "before_evidence_id": draft.before_evidence_id,
                "conversation_ref": draft.conversation_ref,
                "pending_generation_ref": draft.pending_generation_ref,
                "transcript_revision": draft.transcript_revision,
                "scope_commitment_sha256": draft.scope_commitment_sha256,
                "draft_sha256": draft.draft_sha256,
                "strategy_json": strategy_json,
                "prompt_version": draft.prompt_version,
                "persona_version": draft.persona_version,
                "memory_version": draft.memory_version,
                "provider": draft.provider,
                "model": draft.model,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "strategy_json": strategy_json,
        "strategy_key": strategy_key,
        "commitment": commitment,
    }


def _strategy(value: Mapping[str, str]) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "reply_length",
        "question_usage",
        "tone",
    }:
        raise SoulApplicationError("reply_strategy_invalid")
    normalized = {
        "reply_length": value["reply_length"],
        "question_usage": value["question_usage"],
        "tone": value["tone"],
    }
    if normalized["reply_length"] not in {"short", "medium"}:
        raise SoulApplicationError("reply_strategy_invalid")
    if normalized["question_usage"] not in {"none", "one"}:
        raise SoulApplicationError("reply_strategy_invalid")
    if normalized["tone"] not in {"natural", "warm", "playful"}:
        raise SoulApplicationError("reply_strategy_invalid")
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _trial_commitment(row: sqlite3.Row) -> str:
    encoded = json.dumps(
        {
            "trial_id": row["trial_id"],
            "application_intent_id": row["application_intent_id"],
            "instance_id": row["instance_id"],
            "before_evidence_id": row["before_evidence_id"],
            "conversation_ref": row["conversation_ref"],
            "pending_generation_ref": row["pending_generation_ref"],
            "transcript_revision": row["transcript_revision"],
            "scope_commitment_sha256": row["scope_commitment_sha256"],
            "draft_sha256": row["draft_sha256"],
            "strategy_json": row["strategy_json"],
            "prompt_version": row["prompt_version"],
            "persona_version": row["persona_version"],
            "memory_version": row["memory_version"],
            "provider": row["provider"],
            "model": row["model"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _public_trial(row: sqlite3.Row) -> dict[str, Any]:
    # Body-free by construction; no title/name columns exist in this schema.
    return {
        "trial_id": row["trial_id"],
        "application_intent_id": row["application_intent_id"],
        "instance_id": row["instance_id"],
        "before_evidence_id": row["before_evidence_id"],
        "conversation_ref": row["conversation_ref"],
        "pending_generation_ref": row["pending_generation_ref"],
        "transcript_revision": row["transcript_revision"],
        "scope_commitment_sha256": row["scope_commitment_sha256"],
        "draft_sha256": row["draft_sha256"],
        "strategy": json.loads(row["strategy_json"]),
        "prompt_version": row["prompt_version"],
        "persona_version": row["persona_version"],
        "memory_version": row["memory_version"],
        "provider": row["provider"],
        "model": row["model"],
        "owner_ref": row["owner_ref"],
        "send_status": row["send_status"],
        "send_proof_ref": row["send_proof_ref"],
        "delayed_outcome": row["delayed_outcome"],
        "delayed_evidence_ref": row["delayed_evidence_ref"],
        "created_at": row["created_at"],
        "reserved_at": row["reserved_at"],
        "confirmed_at": row["confirmed_at"],
        "outcome_at": row["outcome_at"],
    }


def _recommendation(
    row: sqlite3.Row, global_revision: int
) -> StrategyRecommendation:
    return StrategyRecommendation(
        strategy=json.loads(row["strategy_json"]),
        strategy_key=row["strategy_key"],
        alpha=float(row["alpha"]),
        beta=float(row["beta"]),
        explicit_outcomes=int(row["explicit_outcomes"]),
        revision=global_revision,
    )


def _global_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(revision),0) AS value FROM soul_reply_strategy_posteriors"
    ).fetchone()
    return int(row["value"] if row is not None else 0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
