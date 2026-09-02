from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .domain import (
    FactResolutionStatus,
    NeedUserFact,
    NeedUserFactNotFound,
    NeedUserFactStateConflict,
    NeedUserFactStatus,
    UserFact,
    UserFactIdempotencyConflict,
    UserFactRevision,
    UserFactSourceKind,
    utc_now,
)


_SCHEMA_REVISION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_fact_runtime_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO user_fact_runtime_schema(singleton, revision) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS user_facts (
    fact_id TEXT PRIMARY KEY,
    user_scope TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_scope, fact_key)
);

CREATE TABLE IF NOT EXISTS user_fact_revisions (
    revision_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL REFERENCES user_facts(fact_id),
    revision INTEGER NOT NULL,
    value_json TEXT NOT NULL,
    applicability_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(fact_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_user_fact_revisions_lookup
ON user_fact_revisions(fact_id, revision DESC);

CREATE TABLE IF NOT EXISTS need_user_facts (
    need_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    user_scope TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    question TEXT NOT NULL,
    why_needed TEXT NOT NULL,
    answer_schema_json TEXT NOT NULL,
    applicability_json TEXT NOT NULL,
    applicability_digest TEXT NOT NULL,
    resolution_status TEXT NOT NULL,
    status TEXT NOT NULL,
    resume_stage TEXT NOT NULL,
    conversation_hint TEXT,
    answer_fact_id TEXT,
    answer_revision_id TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    applied_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_need_user_facts_one_open
ON need_user_facts(session_id, goal_id, user_scope, fact_key, applicability_digest)
WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS idx_need_user_facts_session
ON need_user_facts(session_id, status, created_at, need_id);

CREATE TABLE IF NOT EXISTS user_fact_answer_requests (
    need_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(need_id, idempotency_key)
);
"""


class SQLiteUserFactStore:
    """Append-only UserFact storage safe to share with ``agent-runtime.db``."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection(write=True) as connection:
                connection.executescript(_SCHEMA)
                row = connection.execute(
                    "SELECT revision FROM user_fact_runtime_schema WHERE singleton=1"
                ).fetchone()
                if row is None or int(row["revision"]) != _SCHEMA_REVISION:
                    raise RuntimeError("unsupported user-fact runtime schema")
            self._initialized = True

    def get_fact(self, *, user_scope: str, fact_key: str) -> UserFact | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM user_facts WHERE user_scope=? AND fact_key=?",
                (_required(user_scope, "user_scope"), _required(fact_key, "fact_key")),
            ).fetchone()
        return _fact(row) if row is not None else None

    def list_facts(self, *, user_scope: str | None = None) -> list[UserFact]:
        self.initialize()
        query = "SELECT * FROM user_facts"
        parameters: tuple[object, ...] = ()
        if user_scope is not None:
            query += " WHERE user_scope=?"
            parameters = (_required(user_scope, "user_scope"),)
        query += " ORDER BY fact_key, fact_id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_fact(row) for row in rows]

    def revisions(self, *, user_scope: str, fact_key: str) -> list[UserFactRevision]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT f.user_scope, f.fact_key, r.* FROM user_fact_revisions r "
                "JOIN user_facts f ON f.fact_id=r.fact_id "
                "WHERE f.user_scope=? AND f.fact_key=? ORDER BY r.revision",
                (_required(user_scope, "user_scope"), _required(fact_key, "fact_key")),
            ).fetchall()
        return [_revision(row) for row in rows]

    def append_revision(
        self,
        *,
        user_scope: str,
        fact_key: str,
        value: Any,
        applicability: dict[str, Any] | None,
        source_kind: UserFactSourceKind,
        source_ref: str,
        provenance: dict[str, Any],
        confidence: float = 1.0,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> UserFactRevision:
        if source_kind not in {UserFactSourceKind.IMPORTED, UserFactSourceKind.OBSERVED}:
            raise ValueError("user answers must be appended through answer_need")
        if not provenance:
            raise ValueError("imported and observed revisions require provenance")
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            return self._append_revision(
                connection,
                user_scope=_required(user_scope, "user_scope"),
                fact_key=_required(fact_key, "fact_key"),
                value=value,
                applicability=applicability or {},
                source_kind=source_kind,
                source_ref=_required(source_ref, "source_ref"),
                provenance=provenance,
                confidence=confidence,
                valid_from=valid_from or now,
                valid_until=valid_until,
                now=now,
            )

    def create_need(
        self,
        *,
        session_id: str,
        goal_id: str,
        user_scope: str,
        fact_key: str,
        question: str,
        why_needed: str,
        answer_schema: dict[str, Any],
        applicability: dict[str, Any] | None,
        resolution_status: FactResolutionStatus,
        resume_stage: str,
        conversation_hint: str | None = None,
    ) -> tuple[NeedUserFact, bool]:
        if resolution_status is FactResolutionStatus.KNOWN:
            raise ValueError("a KNOWN fact does not require a question")
        self.initialize()
        applicability = applicability or {}
        applicability_json = _json(applicability)
        applicability_digest = _sha256(applicability_json)
        values = (
            str(uuid.uuid4()),
            _required(session_id, "session_id"),
            _required(goal_id, "goal_id"),
            _required(user_scope, "user_scope"),
            _required(fact_key, "fact_key"),
            _required(question, "question"),
            _required(why_needed, "why_needed"),
            _json(answer_schema),
            applicability_json,
            applicability_digest,
            resolution_status.value,
            NeedUserFactStatus.OPEN.value,
            _required(resume_stage, "resume_stage"),
            conversation_hint,
            _iso(utc_now()),
        )
        with self._lock, self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM need_user_facts WHERE session_id=? AND goal_id=? "
                "AND user_scope=? AND fact_key=? AND applicability_digest=? AND status='OPEN'",
                (values[1], values[2], values[3], values[4], applicability_digest),
            ).fetchone()
            if existing is not None:
                return _need(existing), False
            try:
                connection.execute(
                    "INSERT INTO need_user_facts(need_id,session_id,goal_id,user_scope,fact_key,"
                    "question,why_needed,answer_schema_json,applicability_json,applicability_digest,"
                    "resolution_status,status,resume_stage,conversation_hint,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT * FROM need_user_facts WHERE session_id=? AND goal_id=? "
                    "AND user_scope=? AND fact_key=? AND applicability_digest=? AND status='OPEN'",
                    (values[1], values[2], values[3], values[4], applicability_digest),
                ).fetchone()
                if existing is None:
                    raise
                return _need(existing), False
            row = connection.execute(
                "SELECT * FROM need_user_facts WHERE need_id=?", (values[0],)
            ).fetchone()
        return _need(row), True

    def get_need(self, need_id: str) -> NeedUserFact:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM need_user_facts WHERE need_id=?", (need_id,)
            ).fetchone()
        if row is None:
            raise NeedUserFactNotFound(need_id)
        return _need(row)

    def list_needs(
        self, *, session_id: str | None = None, status: NeedUserFactStatus | None = None
    ) -> list[NeedUserFact]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[object] = []
        if session_id is not None:
            clauses.append("session_id=?")
            parameters.append(session_id)
        if status is not None:
            clauses.append("status=?")
            parameters.append(status.value)
        query = "SELECT * FROM need_user_facts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, need_id"
        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [_need(row) for row in rows]

    def answer_need(
        self,
        *,
        need_id: str,
        value: Any,
        idempotency_key: str,
        provenance: dict[str, Any] | None = None,
        confidence: float = 1.0,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> tuple[NeedUserFact, UserFactRevision, bool]:
        self.initialize()
        key = _required(idempotency_key, "idempotency_key")
        digest = _sha256(_json({"value": value, "valid_from": _maybe_iso(valid_from), "valid_until": _maybe_iso(valid_until)}))
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            prior = connection.execute(
                "SELECT request_digest,revision_id FROM user_fact_answer_requests "
                "WHERE need_id=? AND idempotency_key=?",
                (need_id, key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_digest"]) != digest:
                    raise UserFactIdempotencyConflict(
                        "the same answer idempotency key was used with different content"
                    )
                need_row = connection.execute(
                    "SELECT * FROM need_user_facts WHERE need_id=?", (need_id,)
                ).fetchone()
                revision_row = self._revision_row(connection, str(prior["revision_id"]))
                if need_row is None or revision_row is None:
                    raise RuntimeError("incomplete persisted answer request")
                return _need(need_row), _revision(revision_row), False

            need_row = connection.execute(
                "SELECT * FROM need_user_facts WHERE need_id=?", (need_id,)
            ).fetchone()
            if need_row is None:
                raise NeedUserFactNotFound(need_id)
            need = _need(need_row)
            if need.status is not NeedUserFactStatus.OPEN:
                raise NeedUserFactStateConflict(
                    f"need {need_id} is already {need.status.value}"
                )
            revision = self._append_revision(
                connection,
                user_scope=need.user_scope,
                fact_key=need.fact_key,
                value=value,
                applicability=need.applicability,
                source_kind=UserFactSourceKind.USER_ANSWER,
                source_ref=f"need:{need.id}",
                provenance={"need_id": need.id, **(provenance or {})},
                confidence=confidence,
                valid_from=valid_from or now,
                valid_until=valid_until,
                now=now,
            )
            connection.execute(
                "UPDATE need_user_facts SET status=?,answer_fact_id=?,answer_revision_id=?,answered_at=? "
                "WHERE need_id=? AND status='OPEN'",
                (NeedUserFactStatus.ANSWERED.value, revision.fact_id, revision.id, _iso(now), need_id),
            )
            connection.execute(
                "INSERT INTO user_fact_answer_requests(need_id,idempotency_key,request_digest,revision_id,created_at) "
                "VALUES (?,?,?,?,?)",
                (need_id, key, digest, revision.id, _iso(now)),
            )
            updated = connection.execute(
                "SELECT * FROM need_user_facts WHERE need_id=?", (need_id,)
            ).fetchone()
        return _need(updated), revision, True

    def mark_need_applied(self, need_id: str) -> NeedUserFact:
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM need_user_facts WHERE need_id=?", (need_id,)
            ).fetchone()
            if row is None:
                raise NeedUserFactNotFound(need_id)
            need = _need(row)
            if need.status is NeedUserFactStatus.APPLIED:
                return need
            if need.status is not NeedUserFactStatus.ANSWERED:
                raise NeedUserFactStateConflict("only an ANSWERED need can be applied")
            connection.execute(
                "UPDATE need_user_facts SET status='APPLIED', applied_at=? WHERE need_id=?",
                (_iso(utc_now()), need_id),
            )
            updated = connection.execute(
                "SELECT * FROM need_user_facts WHERE need_id=?", (need_id,)
            ).fetchone()
        return _need(updated)

    def _append_revision(
        self,
        connection: sqlite3.Connection,
        *,
        user_scope: str,
        fact_key: str,
        value: Any,
        applicability: dict[str, Any],
        source_kind: UserFactSourceKind,
        source_ref: str,
        provenance: dict[str, Any],
        confidence: float,
        valid_from: datetime,
        valid_until: datetime | None,
        now: datetime,
    ) -> UserFactRevision:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("valid_until must be later than valid_from")
        fact_row = connection.execute(
            "SELECT * FROM user_facts WHERE user_scope=? AND fact_key=?",
            (user_scope, fact_key),
        ).fetchone()
        if fact_row is None:
            fact_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO user_facts(fact_id,user_scope,fact_key,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (fact_id, user_scope, fact_key, _iso(now), _iso(now)),
            )
            revision_number = 1
        else:
            fact_id = str(fact_row["fact_id"])
            revision_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 AS next_revision "
                    "FROM user_fact_revisions WHERE fact_id=?",
                    (fact_id,),
                ).fetchone()["next_revision"]
            )
            connection.execute(
                "UPDATE user_facts SET updated_at=? WHERE fact_id=?", (_iso(now), fact_id)
            )
        revision_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO user_fact_revisions(revision_id,fact_id,revision,value_json,applicability_json,"
            "source_kind,source_ref,provenance_json,confidence,valid_from,valid_until,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id, fact_id, revision_number, _json(value), _json(applicability),
                source_kind.value, source_ref, _json(provenance), confidence,
                _iso(valid_from), _maybe_iso(valid_until), _iso(now),
            ),
        )
        row = self._revision_row(connection, revision_id)
        if row is None:
            raise RuntimeError("revision insert did not persist")
        return _revision(row)

    @staticmethod
    def _revision_row(connection: sqlite3.Connection, revision_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT f.user_scope,f.fact_key,r.* FROM user_fact_revisions r "
            "JOIN user_facts f ON f.fact_id=r.fact_id WHERE r.revision_id=?",
            (revision_id,),
        ).fetchone()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()


def _fact(row: sqlite3.Row) -> UserFact:
    return UserFact(
        id=str(row["fact_id"]), user_scope=str(row["user_scope"]),
        fact_key=str(row["fact_key"]), created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _revision(row: sqlite3.Row) -> UserFactRevision:
    return UserFactRevision(
        id=str(row["revision_id"]), fact_id=str(row["fact_id"]),
        user_scope=str(row["user_scope"]), fact_key=str(row["fact_key"]),
        value=json.loads(str(row["value_json"])),
        applicability=json.loads(str(row["applicability_json"])),
        source_kind=UserFactSourceKind(str(row["source_kind"])), source_ref=str(row["source_ref"]),
        provenance=json.loads(str(row["provenance_json"])), confidence=float(row["confidence"]),
        valid_from=_datetime(row["valid_from"]), valid_until=_maybe_datetime(row["valid_until"]),
        revision=int(row["revision"]), created_at=_datetime(row["created_at"]),
    )


def _need(row: sqlite3.Row) -> NeedUserFact:
    return NeedUserFact(
        id=str(row["need_id"]), session_id=str(row["session_id"]), goal_id=str(row["goal_id"]),
        user_scope=str(row["user_scope"]), fact_key=str(row["fact_key"]),
        question=str(row["question"]), why_needed=str(row["why_needed"]),
        answer_schema=json.loads(str(row["answer_schema_json"])),
        applicability=json.loads(str(row["applicability_json"])),
        resolution_status=FactResolutionStatus(str(row["resolution_status"])),
        status=NeedUserFactStatus(str(row["status"])), resume_stage=str(row["resume_stage"]),
        conversation_hint=str(row["conversation_hint"]) if row["conversation_hint"] is not None else None,
        answer_fact_id=str(row["answer_fact_id"]) if row["answer_fact_id"] is not None else None,
        answer_revision_id=str(row["answer_revision_id"]) if row["answer_revision_id"] is not None else None,
        created_at=_datetime(row["created_at"]), answered_at=_maybe_datetime(row["answered_at"]),
        applied_at=_maybe_datetime(row["applied_at"]),
    )


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _maybe_iso(value: datetime | None) -> str | None:
    return _iso(value) if value is not None else None


def _datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def _maybe_datetime(value: object) -> datetime | None:
    return _datetime(value) if value is not None else None
