from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from ..execution import GuiAction
from .domain import (
    ActionProposal,
    ArtifactRef,
    DistilledTransition,
    LearningJob,
    OutcomeVerification,
    PolicyMemory,
    Transition,
    TransportReceipt,
)


class LearningStoreError(RuntimeError):
    pass


class LearningRecordNotFound(LearningStoreError):
    pass


class LearningIdempotencyConflict(LearningStoreError):
    pass


class LearningStore(Protocol):
    def initialize(self) -> None: ...

    def recover_active_jobs(self) -> int: ...

    def get_job_by_request(self, client_request_id: str) -> LearningJob | None: ...

    def accept_job(
        self,
        *,
        job_id: str,
        profile_id: str,
        profile_revision: int,
        target_id: str | None,
        instruction: str,
        client_request_id: str,
        request_digest: str,
    ) -> tuple[LearningJob, bool]: ...

    def claim_job(self, job_id: str) -> LearningJob | None: ...

    def get_job(self, job_id: str) -> LearningJob | None: ...

    def list_jobs(self, limit: int = 100) -> list[LearningJob]: ...

    def request_stop(self, job_id: str) -> LearningJob: ...

    def append_intent(
        self,
        *,
        transition_id: str,
        job_id: str,
        sequence: int,
        proposal: ActionProposal,
        before: ArtifactRef | None,
    ) -> Transition: ...

    def finalize_transition(
        self,
        *,
        transition_id: str,
        after: ArtifactRef | None,
        transport: TransportReceipt,
        outcome: OutcomeVerification,
    ) -> Transition: ...

    def list_transitions(self, job_id: str) -> list[Transition]: ...

    def get_policy(self, profile_id: str) -> PolicyMemory: ...

    def finish(
        self,
        job_id: str,
        *,
        status: Literal["not_learned", "failed", "stopped", "stopped_uncertain"],
        detail: str,
        error_code: str | None,
    ) -> LearningJob: ...

    def complete_learned(
        self,
        job_id: str,
        *,
        distilled: Sequence[DistilledTransition],
        detail: str,
    ) -> LearningJob: ...


class SQLiteLearningStore:
    """Isolated append-only learning journal; never points at console.db."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        if self.database_path.name.casefold() == "console.db":
            raise ValueError("game learning must not use the live console database")
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > 1:
                    raise LearningStoreError(
                        f"learning database schema version {version} is newer than supported v1"
                    )
                has_unversioned_schema = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'learning_jobs'
                    """
                ).fetchone()
                if version == 0 and has_unversioned_schema is not None:
                    raise LearningStoreError(
                        "unversioned learning database cannot be migrated implicitly"
                    )
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS learning_jobs (
                        job_id TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL,
                        profile_revision INTEGER NOT NULL,
                        target_id TEXT,
                        instruction TEXT NOT NULL,
                        client_request_id TEXT NOT NULL UNIQUE,
                        request_digest TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN (
                            'queued', 'running', 'stopping', 'learned',
                            'not_learned', 'failed', 'stopped', 'stopped_uncertain'
                        )),
                        cancel_requested INTEGER NOT NULL DEFAULT 0
                            CHECK (cancel_requested IN (0, 1)),
                        transition_count INTEGER NOT NULL DEFAULT 0,
                        policy_version INTEGER NOT NULL DEFAULT 0,
                        policy_memory_count INTEGER,
                        outcome TEXT NOT NULL DEFAULT 'unknown' CHECK (outcome IN (
                            'unknown', 'confirmed_success', 'confirmed_failure', 'unconfirmed'
                        )),
                        policy_state TEXT NOT NULL DEFAULT 'unchanged' CHECK (policy_state IN (
                            'unchanged', 'candidate', 'promoted', 'rejected'
                        )),
                        total_reward REAL,
                        verified_successes INTEGER,
                        detail TEXT,
                        error_code TEXT,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_learning_jobs_created
                    ON learning_jobs(created_at DESC, job_id DESC);

                    CREATE TABLE IF NOT EXISTS learning_transition_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transition_id TEXT NOT NULL,
                        job_id TEXT NOT NULL REFERENCES learning_jobs(job_id),
                        sequence INTEGER NOT NULL,
                        phase TEXT NOT NULL CHECK (phase IN ('intent', 'finalized')),
                        data_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(job_id, sequence, phase),
                        UNIQUE(transition_id, phase)
                    );

                    CREATE INDEX IF NOT EXISTS idx_learning_transition_events_job
                    ON learning_transition_events(job_id, sequence, event_id);

                    CREATE TABLE IF NOT EXISTS learning_policy_memory (
                        profile_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        trajectory_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(profile_id, version)
                    );
                    """
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            self._initialized = True

    def recover_active_jobs(self) -> int:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            active = connection.execute(
                """
                SELECT job_id FROM learning_jobs
                WHERE status IN ('queued', 'running', 'stopping')
                """
            ).fetchall()
            for row in active:
                job_id = str(row["job_id"])
                open_intent = connection.execute(
                    """
                    SELECT 1
                    FROM learning_transition_events intent
                    WHERE intent.job_id = ? AND intent.phase = 'intent'
                      AND NOT EXISTS (
                        SELECT 1 FROM learning_transition_events final
                        WHERE final.transition_id = intent.transition_id
                          AND final.phase = 'finalized'
                      )
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                status = "stopped_uncertain" if open_intent is not None else "failed"
                detail = (
                    "学习协调器重启；存在传输结果不明的动作，未重放。"
                    if open_intent is not None
                    else "学习协调器重启；未完成任务已终结，未自动恢复。"
                )
                connection.execute(
                    """
                    UPDATE learning_jobs
                    SET status = ?, cancel_requested = 1, detail = ?,
                        error_code = 'coordinator_restarted', finished_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, detail, now, now, job_id),
                )
        return len(active)

    def get_job_by_request(self, client_request_id: str) -> LearningJob | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def accept_job(
        self,
        *,
        job_id: str,
        profile_id: str,
        profile_revision: int,
        target_id: str | None,
        instruction: str,
        client_request_id: str,
        request_digest: str,
    ) -> tuple[LearningJob, bool]:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM learning_jobs WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise LearningIdempotencyConflict(client_request_id)
                return _job_from_row(existing), False
            version_row = connection.execute(
                """
                SELECT version, trajectory_json FROM learning_policy_memory
                WHERE profile_id = ? ORDER BY version DESC LIMIT 1
                """,
                (profile_id,),
            ).fetchone()
            version = int(version_row["version"]) if version_row is not None else 0
            memory_count = (
                len(json.loads(version_row["trajectory_json"]))
                if version_row is not None
                else 0
            )
            connection.execute(
                """
                INSERT INTO learning_jobs (
                    job_id, profile_id, profile_revision, target_id, instruction, client_request_id,
                    request_digest, status, cancel_requested, transition_count,
                    policy_version, policy_memory_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    profile_id,
                    profile_revision,
                    target_id,
                    instruction,
                    client_request_id,
                    request_digest,
                    version,
                    memory_count,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(row), True

    def claim_job(self, job_id: str) -> LearningJob | None:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                UPDATE learning_jobs
                SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE job_id = ? AND status = 'queued' AND cancel_requested = 0
                """,
                (now, now, job_id),
            )
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise LearningRecordNotFound(job_id)
        job = _job_from_row(row)
        return job if job.status == "running" else None

    def get_job(self, job_id: str) -> LearningJob | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def list_jobs(self, limit: int = 100) -> list[LearningJob]:
        self.initialize()
        bounded = max(1, min(int(limit), 500))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_jobs
                ORDER BY created_at DESC, job_id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def request_stop(self, job_id: str) -> LearningJob:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise LearningRecordNotFound(job_id)
            current = _job_from_row(row)
            if current.status in _TERMINAL_STATUSES:
                return current
            if current.status == "queued":
                status = "stopped"
                finished_at = now
            else:
                status = "stopping"
                finished_at = None
            connection.execute(
                """
                UPDATE learning_jobs
                SET status = ?, cancel_requested = 1,
                    detail = '已请求停止学习任务。', finished_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, finished_at, now, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(updated)

    def append_intent(
        self,
        *,
        transition_id: str,
        job_id: str,
        sequence: int,
        proposal: ActionProposal,
        before: ArtifactRef | None,
    ) -> Transition:
        self.initialize()
        now = _utc_now()
        data = {
            "proposal": _proposal_to_data(proposal),
            "before": _artifact_to_data(before),
        }
        with self._connection(write=True) as connection:
            job = connection.execute(
                "SELECT status, cancel_requested FROM learning_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise LearningRecordNotFound(job_id)
            if job["status"] != "running" or bool(job["cancel_requested"]):
                raise LearningStoreError("job is not available for a new intent")
            connection.execute(
                """
                INSERT INTO learning_transition_events (
                    transition_id, job_id, sequence, phase, data_json, created_at
                ) VALUES (?, ?, ?, 'intent', ?, ?)
                """,
                (transition_id, job_id, sequence, _json(data), now),
            )
        return Transition(
            transition_id=transition_id,
            job_id=job_id,
            sequence=sequence,
            proposal=proposal,
            before=before,
            after=None,
            transport=None,
            outcome=None,
            created_at=now,
        )

    def finalize_transition(
        self,
        *,
        transition_id: str,
        after: ArtifactRef | None,
        transport: TransportReceipt,
        outcome: OutcomeVerification,
    ) -> Transition:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            intent = connection.execute(
                """
                SELECT * FROM learning_transition_events
                WHERE transition_id = ? AND phase = 'intent'
                """,
                (transition_id,),
            ).fetchone()
            if intent is None:
                raise LearningRecordNotFound(transition_id)
            data = {
                "after": _artifact_to_data(after),
                "transport": asdict(transport),
                "outcome": asdict(outcome),
            }
            connection.execute(
                """
                INSERT INTO learning_transition_events (
                    transition_id, job_id, sequence, phase, data_json, created_at
                ) VALUES (?, ?, ?, 'finalized', ?, ?)
                """,
                (
                    transition_id,
                    intent["job_id"],
                    intent["sequence"],
                    _json(data),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE learning_jobs
                SET transition_count = transition_count + 1,
                    total_reward = COALESCE(total_reward, 0) + ?,
                    verified_successes = COALESCE(verified_successes, 0) + ?,
                    outcome = CASE
                        WHEN ? THEN 'confirmed_success'
                        WHEN outcome = 'confirmed_success' THEN outcome
                        WHEN ? AND ? < 0 THEN 'confirmed_failure'
                        WHEN NOT ? AND outcome = 'unknown' THEN 'unconfirmed'
                        ELSE outcome END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    outcome.reward,
                    int(outcome.task_succeeded),
                    int(outcome.task_succeeded),
                    int(outcome.confirmed),
                    outcome.reward,
                    int(outcome.confirmed),
                    now,
                    intent["job_id"],
                ),
            )
        transition = self._get_transition(transition_id)
        if transition is None:  # pragma: no cover - transaction invariant
            raise LearningStoreError("finalized transition could not be read")
        return transition

    def list_transitions(self, job_id: str) -> list[Transition]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_transition_events
                WHERE job_id = ? ORDER BY sequence, event_id
                """,
                (job_id,),
            ).fetchall()
        return _transitions_from_rows(rows)

    def get_policy(self, profile_id: str) -> PolicyMemory:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM learning_policy_memory
                WHERE profile_id = ? ORDER BY version DESC LIMIT 1
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return PolicyMemory(profile_id=profile_id, version=0)
        raw = json.loads(row["trajectory_json"])
        return PolicyMemory(
            profile_id=profile_id,
            version=int(row["version"]),
            trajectory=tuple(_distilled_from_data(item) for item in raw),
        )

    def finish(
        self,
        job_id: str,
        *,
        status: Literal["not_learned", "failed", "stopped", "stopped_uncertain"],
        detail: str,
        error_code: str | None,
    ) -> LearningJob:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise LearningRecordNotFound(job_id)
            current = _job_from_row(row)
            if current.status in _TERMINAL_STATUSES:
                return current
            connection.execute(
                """
                UPDATE learning_jobs
                SET status = ?, detail = ?, error_code = ?,
                    outcome = CASE
                        WHEN outcome = 'unknown' AND ? = 'not_learned' THEN 'unconfirmed'
                        ELSE outcome END,
                    cancel_requested = CASE WHEN ? IN ('stopped', 'stopped_uncertain')
                        THEN 1 ELSE cancel_requested END,
                    finished_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, detail[:1_000], error_code, status, status, now, now, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(updated)

    def complete_learned(
        self,
        job_id: str,
        *,
        distilled: Sequence[DistilledTransition],
        detail: str,
    ) -> LearningJob:
        self.initialize()
        if not distilled or any(item.reward <= 0 for item in distilled):
            raise LearningStoreError("only a non-empty positive trajectory may be promoted")
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise LearningRecordNotFound(job_id)
            current = _job_from_row(row)
            if current.status in _TERMINAL_STATUSES:
                return current
            event_rows = connection.execute(
                """
                SELECT * FROM learning_transition_events
                WHERE job_id = ? ORDER BY sequence, event_id
                """,
                (job_id,),
            ).fetchall()
            transitions = _transitions_from_rows(event_rows)
            if not any(
                item.finalized
                and item.outcome is not None
                and item.outcome.confirmed
                and item.outcome.task_succeeded
                for item in transitions
            ):
                raise LearningStoreError("task success was not confirmed")

            eligible = [
                DistilledTransition(
                    action=item.proposal.action,
                    reward=item.outcome.reward,
                    before_sha256=item.before.sha256 if item.before else None,
                    after_sha256=item.after.sha256 if item.after else None,
                    verifier_detail=item.outcome.detail,
                )
                for item in transitions
                if item.finalized
                and item.proposal.action is not None
                and item.transport is not None
                and item.transport.status == "accepted"
                and item.outcome is not None
                and item.outcome.confirmed
                and item.outcome.reward > 0
            ]
            unmatched = list(eligible)
            for candidate in distilled:
                try:
                    matched_index = unmatched.index(candidate)
                except ValueError as exc:
                    raise LearningStoreError(
                        "distilled trajectory lacks accepted positive Transition provenance"
                    ) from exc
                unmatched.pop(matched_index)

            memory = connection.execute(
                """
                SELECT * FROM learning_policy_memory
                WHERE profile_id = ? ORDER BY version DESC LIMIT 1
                """,
                (current.profile_id,),
            ).fetchone()
            previous_version = int(memory["version"]) if memory is not None else 0
            previous = json.loads(memory["trajectory_json"]) if memory is not None else []
            promoted = [*previous, *(_distilled_to_data(item) for item in distilled)]
            next_version = previous_version + 1
            connection.execute(
                """
                INSERT INTO learning_policy_memory (
                    profile_id, version, trajectory_json, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (current.profile_id, next_version, _json(promoted), now),
            )
            connection.execute(
                """
                UPDATE learning_jobs
                SET status = 'learned', policy_version = ?, policy_memory_count = ?,
                    outcome = 'confirmed_success', policy_state = 'promoted',
                    detail = ?, error_code = NULL,
                    finished_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (next_version, len(promoted), detail[:1_000], now, now, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM learning_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(updated)

    def _get_transition(self, transition_id: str) -> Transition | None:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_transition_events
                WHERE transition_id = ? ORDER BY event_id
                """,
                (transition_id,),
            ).fetchall()
        items = _transitions_from_rows(rows)
        return items[0] if items else None

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()


_TERMINAL_STATUSES = {
    "learned",
    "not_learned",
    "failed",
    "stopped",
    "stopped_uncertain",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _job_from_row(row: sqlite3.Row) -> LearningJob:
    return LearningJob(
        job_id=row["job_id"],
        profile_id=row["profile_id"],
        profile_revision=int(row["profile_revision"]),
        target_id=row["target_id"],
        instruction=row["instruction"],
        client_request_id=row["client_request_id"],
        request_digest=row["request_digest"],
        status=row["status"],
        cancel_requested=bool(row["cancel_requested"]),
        transition_count=int(row["transition_count"]),
        policy_version=int(row["policy_version"]),
        policy_memory_count=(
            int(row["policy_memory_count"])
            if row["policy_memory_count"] is not None
            else None
        ),
        outcome=row["outcome"],
        policy_state=row["policy_state"],
        total_reward=(float(row["total_reward"]) if row["total_reward"] is not None else None),
        verified_successes=(
            int(row["verified_successes"])
            if row["verified_successes"] is not None
            else None
        ),
        detail=row["detail"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
    )


def _proposal_to_data(proposal: ActionProposal) -> dict[str, Any]:
    return {
        "kind": proposal.kind,
        "action": asdict(proposal.action) if proposal.action is not None else None,
        "wait_seconds": proposal.wait_seconds,
    }


def _proposal_from_data(data: dict[str, Any]) -> ActionProposal:
    action = GuiAction(**data["action"]) if data.get("action") is not None else None
    return ActionProposal(
        kind=data["kind"],
        action=action,
        wait_seconds=data.get("wait_seconds"),
    )


def _artifact_to_data(artifact: ArtifactRef | None) -> dict[str, Any] | None:
    return asdict(artifact) if artifact is not None else None


def _artifact_from_data(data: dict[str, Any] | None) -> ArtifactRef | None:
    return ArtifactRef(**data) if data is not None else None


def _transitions_from_rows(rows: Sequence[sqlite3.Row]) -> list[Transition]:
    grouped: dict[str, dict[str, sqlite3.Row]] = {}
    order: list[str] = []
    for row in rows:
        transition_id = str(row["transition_id"])
        if transition_id not in grouped:
            grouped[transition_id] = {}
            order.append(transition_id)
        grouped[transition_id][str(row["phase"])] = row

    result: list[Transition] = []
    for transition_id in order:
        phases = grouped[transition_id]
        intent = phases.get("intent")
        if intent is None:
            continue
        intent_data = json.loads(intent["data_json"])
        finalized = phases.get("finalized")
        final_data = json.loads(finalized["data_json"]) if finalized is not None else None
        result.append(
            Transition(
                transition_id=transition_id,
                job_id=intent["job_id"],
                sequence=int(intent["sequence"]),
                proposal=_proposal_from_data(intent_data["proposal"]),
                before=_artifact_from_data(intent_data.get("before")),
                after=(
                    _artifact_from_data(final_data.get("after"))
                    if final_data is not None
                    else None
                ),
                transport=(
                    TransportReceipt(**final_data["transport"])
                    if final_data is not None
                    else None
                ),
                outcome=(
                    OutcomeVerification(**final_data["outcome"])
                    if final_data is not None
                    else None
                ),
                created_at=intent["created_at"],
                finalized_at=finalized["created_at"] if finalized is not None else None,
            )
        )
    return result


def _distilled_to_data(item: DistilledTransition) -> dict[str, Any]:
    return {
        "action": asdict(item.action),
        "reward": item.reward,
        "before_sha256": item.before_sha256,
        "after_sha256": item.after_sha256,
        "verifier_detail": item.verifier_detail,
    }


def _distilled_from_data(data: dict[str, Any]) -> DistilledTransition:
    return DistilledTransition(
        action=GuiAction(**data["action"]),
        reward=float(data["reward"]),
        before_sha256=data.get("before_sha256"),
        after_sha256=data.get("after_sha256"),
        verifier_detail=data.get("verifier_detail", ""),
    )
