from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from .domain import (
    ApplicationInstance,
    ExecutionReceipt,
    IdempotencyConflict,
    Intent,
    MemoryCandidate,
    Outcome,
    RuntimeEvent,
    RuntimeIntent,
    RuntimeNotFound,
    RuntimeOutcome,
)


_ACTIVE = {"queued", "running", "waiting", "paused", "stopping"}
_SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def request_digest(operation: str, data: Mapping[str, Any]) -> str:
    body = json.dumps(
        {"operation": operation, **data}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class _SQLiteApplicationStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._lock = threading.Lock()
        self._initialized = False
        self.initialize()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as db:
                db.executescript(
                    f"""
                    CREATE TABLE IF NOT EXISTS application_runtime_schema (
                      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                      version INTEGER NOT NULL);
                    INSERT OR IGNORE INTO application_runtime_schema
                      VALUES(1, {_SCHEMA_VERSION});
                    CREATE TABLE IF NOT EXISTS application_instances (
                      instance_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                      target_id TEXT, initial_input TEXT, status TEXT NOT NULL,
                      revision INTEGER NOT NULL DEFAULT 0,
                      degraded INTEGER NOT NULL DEFAULT 0,
                      hard_risk INTEGER NOT NULL DEFAULT 0,
                      detail TEXT, error_code TEXT,
                      memory_version INTEGER NOT NULL DEFAULT 0,
                      worker_token TEXT, wake_at TEXT,
                      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                      finished_at TEXT);
                    CREATE TABLE IF NOT EXISTS application_requests (
                      client_request_id TEXT PRIMARY KEY,
                      operation TEXT NOT NULL, digest TEXT NOT NULL,
                      instance_id TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS application_commands (
                      instance_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                      tag TEXT NOT NULL, content TEXT, revision INTEGER NOT NULL,
                      client_request_id TEXT NOT NULL UNIQUE,
                      created_at TEXT NOT NULL,
                      PRIMARY KEY(instance_id, sequence));
                    CREATE TABLE IF NOT EXISTS application_cycles (
                      instance_id TEXT NOT NULL, cycle INTEGER NOT NULL,
                      revision INTEGER NOT NULL, state TEXT NOT NULL,
                      created_at TEXT NOT NULL, finished_at TEXT,
                      PRIMARY KEY(instance_id, cycle));
                    CREATE TABLE IF NOT EXISTS application_observations (
                      observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                      instance_id TEXT NOT NULL, cycle INTEGER NOT NULL,
                      phase TEXT NOT NULL, evidence_id TEXT NOT NULL,
                      summary TEXT NOT NULL, fresh INTEGER NOT NULL,
                      data_json TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS application_intents (
                      intent_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
                      cycle INTEGER NOT NULL, revision INTEGER NOT NULL,
                      name TEXT NOT NULL, arguments_json TEXT NOT NULL,
                      hard_risk INTEGER NOT NULL, phase TEXT NOT NULL,
                      reservation_id TEXT, receipt_json TEXT,
                      created_at TEXT NOT NULL, finalized_at TEXT);
                    CREATE TABLE IF NOT EXISTS application_outcomes (
                      instance_id TEXT NOT NULL, cycle INTEGER NOT NULL,
                      status TEXT NOT NULL, evidence TEXT NOT NULL,
                      hard_risk INTEGER NOT NULL,
                      terminal INTEGER NOT NULL DEFAULT 1,
                      after_evidence_id TEXT, created_at TEXT NOT NULL,
                      PRIMARY KEY(instance_id, cycle));
                    CREATE TABLE IF NOT EXISTS application_reflections (
                      instance_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                      detail TEXT NOT NULL, created_at TEXT NOT NULL,
                      PRIMARY KEY(instance_id, sequence));
                    CREATE TABLE IF NOT EXISTS application_memory_candidates (
                      candidate_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
                      scope TEXT NOT NULL, content_json TEXT NOT NULL,
                      evidence_json TEXT NOT NULL,
                      reward_required INTEGER NOT NULL DEFAULT 0,
                      eligible INTEGER NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS application_memory_versions (
                      scope TEXT NOT NULL, version INTEGER NOT NULL,
                      source_instance_id TEXT NOT NULL,
                      content_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      PRIMARY KEY(scope, version));
                    CREATE TABLE IF NOT EXISTS application_memory_heads (
                      scope TEXT PRIMARY KEY, version INTEGER NOT NULL,
                      updated_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS application_events (
                      instance_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                      event_type TEXT NOT NULL, data_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      PRIMARY KEY(instance_id, sequence));
                    """
                )
                row = db.execute(
                    "SELECT version FROM application_runtime_schema WHERE singleton=1"
                ).fetchone()
                if row is None:
                    raise RuntimeError("missing application runtime database schema")
                version = int(row["version"])
                if version == 1:
                    self._migrate_v1_to_v2(db)
                    version = 2
                if version != _SCHEMA_VERSION:
                    raise RuntimeError("unsupported application runtime database schema")
            self._initialized = True

    @staticmethod
    def _migrate_v1_to_v2(db: sqlite3.Connection) -> None:
        def add_column(table: str, declaration: str) -> None:
            name = declaration.split()[0]
            columns = {
                str(row["name"])
                for row in db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if name not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")

        add_column("application_instances", "wake_at TEXT")
        add_column("application_outcomes", "terminal INTEGER NOT NULL DEFAULT 1")
        add_column(
            "application_memory_candidates",
            "reward_required INTEGER NOT NULL DEFAULT 0",
        )
        db.execute(
            "UPDATE application_runtime_schema SET version=2 WHERE singleton=1"
        )

    def accept_start(
        self,
        instance_id: str,
        profile_id: str,
        target_id: str | None,
        initial_input: str | None,
        request_id: str,
        digest: str,
    ) -> tuple[ApplicationInstance, bool]:
        with self._connection() as db:
            old = self._request(db, request_id, digest)
            if old:
                return self.inspect(old), False
            now = _now()
            db.execute(
                """
                INSERT INTO application_instances(
                  instance_id,profile_id,target_id,initial_input,status,
                  created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    instance_id,
                    profile_id,
                    target_id,
                    initial_input,
                    "queued",
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO application_requests VALUES(?,?,?,?,?)",
                (request_id, "start", digest, instance_id, now),
            )
            self._event(db, instance_id, "started", {"profile_id": profile_id})
        return self.inspect(instance_id), True

    def existing_request(
        self, request_id: str, digest: str
    ) -> ApplicationInstance | None:
        with self._connection() as db:
            instance_id = self._request(db, request_id, digest)
        return self.inspect(instance_id) if instance_id is not None else None

    def accept_command(
        self,
        instance_id: str,
        tag: str,
        content: str | None,
        request_id: str,
        digest: str,
    ) -> tuple[ApplicationInstance, bool]:
        with self._connection() as db:
            old = self._request(db, request_id, digest)
            if old:
                return self.inspect(old), False
            row = self._instance(db, instance_id)
            if row["status"] in {"stopped", "completed", "failed"}:
                raise RuntimeError("terminal application instance cannot accept commands")
            revision = int(row["revision"]) + (1 if tag == "Input" else 0)
            sequence = int(
                db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM application_commands "
                    "WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()[0]
            )
            now = _now()
            db.execute(
                "INSERT INTO application_commands VALUES(?,?,?,?,?,?,?)",
                (instance_id, sequence, tag, content, revision, request_id, now),
            )
            old_status = str(row["status"])
            has_unfinished_intent = db.execute(
                """
                SELECT 1 FROM application_intents i
                JOIN application_cycles c
                  ON c.instance_id=i.instance_id AND c.cycle=i.cycle
                WHERE i.instance_id=? AND c.finished_at IS NULL
                  AND i.phase IN (
                    'open','reserved','dispatching','dispatched','reconciled'
                  )
                LIMIT 1
                """,
                (instance_id,),
            ).fetchone() is not None
            if tag == "Pause":
                status = "paused"
            elif tag == "Resume" and old_status in {"paused", "waiting"}:
                status = "queued"
            elif tag == "Stop":
                status = "stopping"
            elif tag == "Input" and old_status == "waiting":
                status = "queued"
            else:
                status = old_status
            wake_at = (
                row["wake_at"]
                if has_unfinished_intent
                and status in {"waiting", "paused", "stopping"}
                else None
            )
            db.execute(
                """
                UPDATE application_instances
                SET revision=?,status=?,wake_at=?,updated_at=?
                WHERE instance_id=?
                """,
                (revision, status, wake_at, now, instance_id),
            )
            db.execute(
                "INSERT INTO application_requests VALUES(?,?,?,?,?)",
                (request_id, "command", digest, instance_id, now),
            )
            self._event(
                db,
                instance_id,
                "command_accepted",
                {"tag": tag, "revision": revision},
            )
        return self.inspect(instance_id), True

    def claim(self, instance_id: str, worker_token: str) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                """
                UPDATE application_instances
                SET status=CASE WHEN status='stopping' THEN 'stopping'
                                WHEN status='paused' THEN 'paused'
                                ELSE 'running' END,
                    worker_token=?,wake_at=NULL,updated_at=?
                WHERE instance_id=?
                  AND status IN ('queued','running','paused','stopping')
                  AND worker_token IS NULL
                """,
                (worker_token, _now(), instance_id),
            )
            return cursor.rowcount == 1

    def release(self, instance_id: str, worker_token: str) -> None:
        with self._connection() as db:
            db.execute(
                """
                UPDATE application_instances SET worker_token=NULL,updated_at=?
                WHERE instance_id=? AND worker_token=?
                """,
                (_now(), instance_id, worker_token),
            )

    def begin_cycle(
        self, instance_id: str, worker_token: str
    ) -> tuple[int, int] | None:
        with self._connection() as db:
            row = self._instance(db, instance_id)
            if row["worker_token"] != worker_token or row["status"] != "running":
                return None
            cycle = int(
                db.execute(
                    "SELECT COALESCE(MAX(cycle),0)+1 FROM application_cycles "
                    "WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()[0]
            )
            db.execute(
                "INSERT INTO application_cycles VALUES(?,?,?,?,?,NULL)",
                (instance_id, cycle, row["revision"], "open", _now()),
            )
            return cycle, int(row["revision"])

    def record_observation(
        self, instance_id: str, cycle: int, phase: str, observation: Any
    ) -> None:
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO application_observations(
                  instance_id,cycle,phase,evidence_id,summary,fresh,data_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    instance_id,
                    cycle,
                    phase,
                    observation.evidence_id,
                    observation.summary,
                    int(observation.fresh),
                    json.dumps(dict(observation.data), sort_keys=True),
                    _now(),
                ),
            )

    def persist_intent(
        self,
        intent_id: str,
        instance_id: str,
        cycle: int,
        revision: int,
        intent: Intent,
        persisted_intent: Intent | None = None,
    ) -> bool:
        durable = persisted_intent or intent
        with self._connection() as db:
            row = self._instance(db, instance_id)
            if row["status"] != "running" or row["revision"] != revision:
                return False
            db.execute(
                """
                INSERT INTO application_intents
                VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    intent_id,
                    instance_id,
                    cycle,
                    revision,
                    durable.name,
                    json.dumps(dict(durable.arguments), sort_keys=True),
                    int(durable.hard_risk),
                    "open",
                    None,
                    None,
                    _now(),
                ),
            )
            self._event(
                db,
                instance_id,
                "intent_open",
                {"intent_id": intent_id, "revision": revision},
            )
            return True

    def pre_dispatch(self, instance_id: str, intent_id: str, revision: int) -> bool:
        with self._connection() as db:
            row = self._instance(db, instance_id)
            if row["status"] != "running" or row["revision"] != revision:
                self._settle(db, intent_id, "abandoned")
                self._event(
                    db, instance_id, "intent_fenced", {"intent_id": intent_id}
                )
                return False
            return True

    def mark_reserved(
        self, instance_id: str, intent_id: str, reservation_id: str
    ) -> bool:
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise ValueError("execution owner reservation_id must not be blank")
        with self._connection() as db:
            changed = (
                db.execute(
                    """
                    UPDATE application_intents
                    SET phase='reserved',reservation_id=?
                    WHERE intent_id=? AND phase='open'
                    """,
                    (reservation_id, intent_id),
                ).rowcount
                == 1
            )
            if changed:
                self._event(
                    db, instance_id, "intent_reserved", {"intent_id": intent_id}
                )
            return changed

    def mark_dispatching(
        self, instance_id: str, intent_id: str, revision: int
    ) -> bool:
        with self._connection() as db:
            row = self._instance(db, instance_id)
            if row["status"] != "running" or row["revision"] != revision:
                self._settle(db, intent_id, "abandoned")
                return False
            return (
                db.execute(
                    """
                    UPDATE application_intents SET phase='dispatching'
                    WHERE intent_id=? AND phase='reserved'
                    """,
                    (intent_id,),
                ).rowcount
                == 1
            )

    def mark_dispatched(
        self,
        instance_id: str,
        intent_id: str,
        receipt: ExecutionReceipt,
        persisted_receipt: ExecutionReceipt | None = None,
    ) -> bool:
        durable = persisted_receipt or receipt
        with self._connection() as db:
            changed = (
                db.execute(
                    """
                    UPDATE application_intents
                    SET phase='dispatched',receipt_json=?
                    WHERE intent_id=? AND phase='dispatching'
                    """,
                    (
                        json.dumps(
                            {
                                "receipt_id": durable.receipt_id,
                                "accepted": durable.accepted,
                                "detail": durable.detail,
                            }
                        ),
                        intent_id,
                    ),
                ).rowcount
                == 1
            )
            if changed:
                self._event(
                    db, instance_id, "intent_dispatched", {"intent_id": intent_id}
                )
            return changed

    def settle_fence(self, instance_id: str, cycle: int) -> str:
        """Close a cycle whose revision/status fence rejected further work."""
        with self._connection() as db:
            row = self._instance(db, instance_id)
            now = _now()
            status = str(row["status"])
            if status == "stopping":
                status = "stopped"
            cycle_state = {
                "paused": "paused",
                "stopped": "stopped",
            }.get(status, "fenced")
            db.execute(
                """
                UPDATE application_cycles SET state=?,finished_at=?
                WHERE instance_id=? AND cycle=? AND finished_at IS NULL
                """,
                (cycle_state, now, instance_id, cycle),
            )
            terminal = status == "stopped"
            db.execute(
                """
                UPDATE application_instances
                SET status=?,worker_token=NULL,updated_at=?,finished_at=?
                WHERE instance_id=?
                """,
                (status, now, now if terminal else None, instance_id),
            )
            return status

    def schedule_wait(
        self, instance_id: str, cycle: int, revision: int, wait_seconds: float
    ) -> str | None:
        with self._connection() as db:
            row = self._instance(db, instance_id)
            if row["status"] != "running" or int(row["revision"]) != revision:
                return None
            now = datetime.now(UTC)
            wake_at = (now + timedelta(seconds=float(wait_seconds))).isoformat()
            db.execute(
                """
                UPDATE application_cycles SET state='waiting',finished_at=?
                WHERE instance_id=? AND cycle=? AND finished_at IS NULL
                """,
                (now.isoformat(), instance_id, cycle),
            )
            db.execute(
                """
                UPDATE application_instances
                SET status='waiting',worker_token=NULL,wake_at=?,detail='waiting',
                    error_code=NULL,updated_at=?,finished_at=NULL
                WHERE instance_id=?
                """,
                (wake_at, now.isoformat(), instance_id),
            )
            self._event(
                db,
                instance_id,
                "wait_scheduled",
                {"cycle": cycle, "wait_seconds": float(wait_seconds)},
            )
            return wake_at

    def schedule_reconciliation_wait(
        self,
        instance_id: str,
        intent_id: str,
        retry_after_seconds: float,
    ) -> str | None:
        """Persist an inspect-only retry without closing the intent or cycle."""
        with self._connection() as db:
            row = self._instance(db, instance_id)
            unfinished = db.execute(
                """
                SELECT 1 FROM application_intents i
                JOIN application_cycles c
                  ON c.instance_id=i.instance_id AND c.cycle=i.cycle
                WHERE i.intent_id=? AND i.instance_id=? AND c.finished_at IS NULL
                  AND i.phase IN (
                    'open','reserved','dispatching','dispatched','reconciled'
                  )
                """,
                (intent_id, instance_id),
            ).fetchone()
            if unfinished is None or row["status"] in {
                "stopped",
                "completed",
                "failed",
            }:
                return None
            now = datetime.now(UTC)
            wake_at = (
                now + timedelta(seconds=float(retry_after_seconds))
            ).isoformat()
            current_status = str(row["status"])
            status = (
                current_status
                if current_status in {"paused", "stopping"}
                else "waiting"
            )
            db.execute(
                """
                UPDATE application_instances
                SET status=?,worker_token=NULL,wake_at=?,degraded=1,
                    detail='owner execution still in flight',error_code=NULL,
                    updated_at=?,finished_at=NULL
                WHERE instance_id=?
                """,
                (status, wake_at, now.isoformat(), instance_id),
            )
            self._event(
                db,
                instance_id,
                "reconciliation_wait_scheduled",
                {
                    "intent_id": intent_id,
                    "retry_after_seconds": float(retry_after_seconds),
                },
            )
            return wake_at

    def schedule_retry(
        self,
        instance_id: str,
        cycle: int,
        retry_after_seconds: float,
        *,
        error_code: str,
        stage: str,
    ) -> str | None:
        """Persist a degraded, interruptible retry before any intent exists."""

        with self._connection() as db:
            row = self._instance(db, instance_id)
            cycle_row = db.execute(
                """
                SELECT revision FROM application_cycles
                WHERE instance_id=? AND cycle=? AND finished_at IS NULL
                """,
                (instance_id, cycle),
            ).fetchone()
            if (
                cycle_row is None
                or row["status"] != "running"
                or int(row["revision"]) != int(cycle_row["revision"])
            ):
                return None
            now = datetime.now(UTC)
            wake_at = (
                now + timedelta(seconds=float(retry_after_seconds))
            ).isoformat()
            db.execute(
                """
                UPDATE application_cycles SET state='retry_waiting',finished_at=?
                WHERE instance_id=? AND cycle=? AND finished_at IS NULL
                """,
                (now.isoformat(), instance_id, cycle),
            )
            db.execute(
                """
                UPDATE application_instances
                SET status='waiting',worker_token=NULL,wake_at=?,degraded=1,
                    detail='application dependency retry scheduled',error_code=?,
                    updated_at=?,finished_at=NULL
                WHERE instance_id=?
                """,
                (wake_at, error_code, now.isoformat(), instance_id),
            )
            self._event(
                db,
                instance_id,
                "runtime_retry_scheduled",
                {
                    "stage": stage,
                    "error_code": error_code,
                    "retry_after_seconds": float(retry_after_seconds),
                },
            )
            return wake_at

    def wake_waiting(self, instance_id: str, wake_at: str | None = None) -> bool:
        return self.wake_scheduled(instance_id, wake_at)

    def wake_scheduled(self, instance_id: str, wake_at: str | None = None) -> bool:
        with self._connection() as db:
            where = (
                "instance_id=? AND status IN ('waiting','paused','stopping') "
                "AND wake_at IS NOT NULL"
            )
            args: list[Any] = [instance_id]
            if wake_at is not None:
                where += " AND wake_at=?"
                args.append(wake_at)
            changed = (
                db.execute(
                    f"""
                    UPDATE application_instances
                    SET status=CASE WHEN status='waiting' THEN 'queued'
                                    ELSE status END,
                        wake_at=NULL,updated_at=?
                    WHERE {where}
                    """,
                    [_now(), *args],
                ).rowcount
                == 1
            )
            if changed:
                self._event(db, instance_id, "wait_elapsed", {})
            return changed

    def finish_cycle(
        self,
        instance_id: str,
        cycle: int,
        outcome: Outcome,
        after_evidence_id: str | None,
        *,
        degraded: bool,
        detail: str | None,
        status: str | None = None,
        error_code: str | None = None,
        expected_revision: int | None = None,
        replan_on_revision_change: bool = False,
        owner_settlement_priority: bool = False,
    ) -> str:
        with self._connection() as db:
            now = _now()
            db.execute(
                """
                INSERT OR REPLACE INTO application_outcomes(
                  instance_id,cycle,status,evidence,hard_risk,terminal,
                  after_evidence_id,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    instance_id,
                    cycle,
                    outcome.status,
                    outcome.evidence,
                    int(outcome.hard_risk),
                    int(outcome.terminal),
                    after_evidence_id,
                    now,
                ),
            )
            db.execute(
                """
                UPDATE application_cycles SET state='finished',finished_at=?
                WHERE instance_id=? AND cycle=?
                """,
                (now, instance_id, cycle),
            )
            db.execute(
                """
                UPDATE application_intents SET phase='finalized',finalized_at=?
                WHERE instance_id=? AND cycle=?
                  AND phase IN ('open','reserved','dispatching','dispatched','reconciled')
                """,
                (now, instance_id, cycle),
            )
            row = self._instance(db, instance_id)
            current_status = str(row["status"])
            requested_status = status or "running"
            if owner_settlement_priority and requested_status == "failed":
                resolved_status = "failed"
            elif current_status == "stopping":
                resolved_status = "stopped"
                detail = detail or "stop requested after dispatch"
            elif current_status == "paused":
                resolved_status = "paused"
                detail = detail or "paused after dispatched action"
            elif (
                expected_revision is not None
                and int(row["revision"]) != expected_revision
                and replan_on_revision_change
            ):
                resolved_status = "running"
                detail = detail or "new input accepted after dispatch"
            else:
                resolved_status = requested_status
            terminal = resolved_status in {"stopped", "completed", "failed"}
            db.execute(
                """
                UPDATE application_instances
                SET status=?,degraded=?,hard_risk=?,detail=?,error_code=?,
                    worker_token=NULL,wake_at=NULL,updated_at=?,finished_at=?
                WHERE instance_id=?
                """,
                (
                    resolved_status,
                    int(degraded),
                    int(outcome.hard_risk),
                    detail,
                    error_code,
                    now,
                    now if terminal else None,
                    instance_id,
                ),
            )
            self._event(
                db,
                instance_id,
                "cycle_finished",
                {"cycle": cycle, "outcome": outcome.status},
            )
            return resolved_status

    def complete_without_action(
        self, instance_id: str, cycle: int, detail: str
    ) -> None:
        with self._connection() as db:
            now = _now()
            self._event(db, instance_id, "completion_rejected", {"cycle": cycle})
            db.execute(
                """
                UPDATE application_cycles SET state='finished',finished_at=?
                WHERE instance_id=? AND cycle=?
                """,
                (now, instance_id, cycle),
            )
            db.execute(
                """
                UPDATE application_instances
                SET status='failed',degraded=1,
                    error_code='completion_without_confirmed_success',detail=?,
                    worker_token=NULL,wake_at=NULL,updated_at=?,finished_at=?
                WHERE instance_id=?
                """,
                (detail, now, now, instance_id),
            )

    def defer_reconciliation(
        self, instance_id: str, error_code: str, stage: str
    ) -> None:
        with self._connection() as db:
            now = _now()
            row = self._instance(db, instance_id)
            status = str(row["status"])
            if status not in {"paused", "stopping"}:
                status = "queued"
            db.execute(
                """
                UPDATE application_instances
                SET status=?,degraded=1,detail='owner reconciliation required',
                    error_code=?,worker_token=NULL,wake_at=NULL,updated_at=?
                WHERE instance_id=?
                """,
                (status, error_code, now, instance_id),
            )
            self._event(
                db,
                instance_id,
                "runtime_error",
                {"stage": stage, "error_code": error_code, "reconcile": True},
            )

    def fail_cycle(
        self,
        instance_id: str,
        cycle: int | None,
        error_code: str,
        stage: str,
        *,
        hard_risk: bool = False,
    ) -> None:
        with self._connection() as db:
            now = _now()
            if cycle is not None:
                db.execute(
                    """
                    UPDATE application_cycles SET state='failed',finished_at=?
                    WHERE instance_id=? AND cycle=? AND finished_at IS NULL
                    """,
                    (now, instance_id, cycle),
                )
            db.execute(
                """
                UPDATE application_instances
                SET status='failed',degraded=1,hard_risk=?,
                    detail='application runtime stage failed',error_code=?,
                    worker_token=NULL,wake_at=NULL,updated_at=?,finished_at=?
                WHERE instance_id=?
                """,
                (int(hard_risk), error_code, now, now, instance_id),
            )
            self._event(
                db,
                instance_id,
                "runtime_error",
                {"stage": stage, "error_code": error_code, "reconcile": False},
            )

    def mark_reconciled(
        self,
        instance_id: str,
        intent_id: str,
        receipt: ExecutionReceipt | None,
    ) -> None:
        receipt_json = None
        if receipt is not None:
            receipt_json = json.dumps(
                {
                    "receipt_id": receipt.receipt_id,
                    "accepted": receipt.accepted,
                    "detail": receipt.detail,
                }
            )
        with self._connection() as db:
            db.execute(
                """
                UPDATE application_intents
                SET phase='reconciled',receipt_json=COALESCE(?,receipt_json),
                    finalized_at=?
                WHERE intent_id=?
                """,
                (receipt_json, _now(), intent_id),
            )
            self._event(
                db, instance_id, "intent_reconciled", {"intent_id": intent_id}
            )

    def settle_stop_if_idle(self, instance_id: str) -> bool:
        """A paused/waiting instance has no coordinator pass to observe Stop."""
        with self._connection() as db:
            row = self._instance(db, instance_id)
            if row["status"] != "stopping" or row["worker_token"] is not None:
                return False
            unfinished = db.execute(
                """
                SELECT 1 FROM application_intents i
                JOIN application_cycles c
                  ON c.instance_id=i.instance_id AND c.cycle=i.cycle
                WHERE i.instance_id=? AND c.finished_at IS NULL
                  AND i.phase IN (
                    'open','reserved','dispatching','dispatched','reconciled'
                  )
                LIMIT 1
                """,
                (instance_id,),
            ).fetchone()
            if unfinished is not None:
                return False
            now = _now()
            db.execute(
                """
                UPDATE application_instances
                SET status='stopped',wake_at=NULL,updated_at=?,finished_at=?
                WHERE instance_id=?
                """,
                (now, now, instance_id),
            )
            self._event(db, instance_id, "stopped_idle", {})
            return True

    def recover(self) -> list[tuple[str, float]]:
        """Make active work claimable and return its immediate/delayed schedule.

        Unfinished intents are never dispatched here.  The coordinator will
        route them to ``ExecutionOwner.reconcile`` before beginning a new
        policy cycle.
        """
        schedule: list[tuple[str, float]] = []
        with self._connection() as db:
            now = datetime.now(UTC)
            rows = db.execute(
                "SELECT * FROM application_instances WHERE status IN "
                "('queued','running','waiting','paused','stopping')"
            ).fetchall()
            for row in rows:
                instance_id = str(row["instance_id"])
                status = str(row["status"])
                unfinished = db.execute(
                    """
                    SELECT 1 FROM application_intents
                    WHERE instance_id=?
                      AND phase IN (
                        'open','reserved','dispatching','dispatched','reconciled'
                      )
                    LIMIT 1
                    """,
                    (instance_id,),
                ).fetchone()
                if status == "stopping" and not unfinished:
                    db.execute(
                        """
                        UPDATE application_instances
                        SET status='stopped',worker_token=NULL,wake_at=NULL,
                            updated_at=?,finished_at=? WHERE instance_id=?
                        """,
                        (now.isoformat(), now.isoformat(), instance_id),
                    )
                    continue
                if unfinished:
                    self._event(
                        db, instance_id, "recovery_no_replay", {"inspect": True}
                    )
                wake = _parse_time(row["wake_at"])
                if unfinished and wake and status in {"waiting", "paused", "stopping"}:
                    delay = max(0.0, (wake - now).total_seconds())
                    db.execute(
                        "UPDATE application_instances SET worker_token=NULL "
                        "WHERE instance_id=?",
                        (instance_id,),
                    )
                    schedule.append((instance_id, delay))
                    continue
                elif status == "paused":
                    db.execute(
                        "UPDATE application_instances SET worker_token=NULL "
                        "WHERE instance_id=?",
                        (instance_id,),
                    )
                    if unfinished:
                        # Pause stops future planning, not convergence of a
                        # physical action that may already have started.
                        schedule.append((instance_id, 0.0))
                    continue
                if status == "waiting":
                    delay = max(0.0, (wake - now).total_seconds()) if wake else 0.0
                    db.execute(
                        "UPDATE application_instances SET worker_token=NULL "
                        "WHERE instance_id=?",
                        (instance_id,),
                    )
                    schedule.append((instance_id, delay))
                elif status == "stopping":
                    db.execute(
                        "UPDATE application_instances SET worker_token=NULL "
                        "WHERE instance_id=?",
                        (instance_id,),
                    )
                    schedule.append((instance_id, 0.0))
                else:
                    db.execute(
                        """
                        UPDATE application_instances
                        SET status='queued',worker_token=NULL,wake_at=NULL,updated_at=?
                        WHERE instance_id=?
                        """,
                        (now.isoformat(), instance_id),
                    )
                    schedule.append((instance_id, 0.0))
                if not unfinished:
                    db.execute(
                        """
                        UPDATE application_cycles
                        SET state='recovered_abandoned',finished_at=?
                        WHERE instance_id=? AND finished_at IS NULL
                        """,
                        (now.isoformat(), instance_id),
                    )
        return schedule

    def unfinished_intent(self, instance_id: str) -> RuntimeIntent | None:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT i.* FROM application_intents i
                JOIN application_cycles c
                  ON c.instance_id=i.instance_id AND c.cycle=i.cycle
                WHERE i.instance_id=? AND c.finished_at IS NULL
                  AND i.phase IN (
                    'open','reserved','dispatching','dispatched','reconciled'
                  )
                ORDER BY i.created_at DESC LIMIT 1
                """,
                (instance_id,),
            ).fetchone()
            return self._intent(row) if row else None

    def promote_memory(
        self,
        instance_id: str,
        candidate_id: str,
        candidate: MemoryCandidate,
        outcome: Outcome,
        promoted_content: Mapping[str, Any] | None,
        persisted_candidate: MemoryCandidate | None = None,
    ) -> int:
        durable = persisted_candidate or candidate
        with self._connection() as db:
            now = _now()
            eligible = int(
                outcome.confirmed_success
                and not outcome.hard_risk
                and not candidate.reward_required
                and promoted_content is not None
            )
            db.execute(
                """
                INSERT INTO application_memory_candidates(
                  candidate_id,instance_id,scope,content_json,evidence_json,
                  reward_required,eligible,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    candidate_id,
                    instance_id,
                    durable.scope,
                    json.dumps(dict(durable.content), sort_keys=True),
                    json.dumps(durable.evidence),
                    int(durable.reward_required),
                    eligible,
                    now,
                ),
            )
            if not eligible:
                event_type = (
                    "memory_trial_recorded"
                    if candidate.reward_required
                    else "memory_candidate_rejected"
                )
                self._event(
                    db,
                    instance_id,
                    event_type,
                    {"scope": durable.scope, "candidate_id": candidate_id},
                )
                return 0
            previous = db.execute(
                "SELECT version FROM application_memory_heads WHERE scope=?",
                (candidate.scope,),
            ).fetchone()
            version = int(previous[0]) + 1 if previous else 1
            db.execute(
                "INSERT INTO application_memory_versions VALUES(?,?,?,?,?,?)",
                (
                    candidate.scope,
                    version,
                    instance_id,
                    json.dumps(dict(promoted_content), sort_keys=True),
                    json.dumps(durable.evidence),
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO application_memory_heads VALUES(?,?,?)
                ON CONFLICT(scope) DO UPDATE SET
                  version=excluded.version,updated_at=excluded.updated_at
                """,
                (candidate.scope, version, now),
            )
            db.execute(
                "UPDATE application_instances SET memory_version=? WHERE instance_id=?",
                (version, instance_id),
            )
            self._event(
                db,
                instance_id,
                "memory_promoted",
                {"scope": candidate.scope, "version": version},
            )
            return version

    def active_memory(self, scope: str) -> Mapping[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT content_json FROM application_memory_versions
                WHERE scope=? ORDER BY version DESC LIMIT 1
                """,
                (scope,),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def record_runtime_event(
        self, instance_id: str, event_type: str, data: Mapping[str, Any]
    ) -> None:
        with self._connection() as db:
            self._event(db, instance_id, event_type, data)

    def inspect(self, instance_id: str) -> ApplicationInstance:
        with self._connection() as db:
            row = self._instance(db, instance_id)
            commands = db.execute(
                """
                SELECT content FROM application_commands
                WHERE instance_id=? AND tag='Input' ORDER BY sequence
                """,
                (instance_id,),
            ).fetchall()
            intents = tuple(
                self._intent(item)
                for item in db.execute(
                    "SELECT * FROM application_intents WHERE instance_id=? "
                    "ORDER BY created_at",
                    (instance_id,),
                )
            )
            outcomes = tuple(
                RuntimeOutcome(
                    item["cycle"],
                    item["status"],
                    item["evidence"],
                    bool(item["hard_risk"]),
                    bool(item["terminal"]),
                    item["after_evidence_id"],
                    item["created_at"],
                )
                for item in db.execute(
                    "SELECT * FROM application_outcomes WHERE instance_id=? "
                    "ORDER BY cycle",
                    (instance_id,),
                )
            )
            events = tuple(
                RuntimeEvent(
                    item["sequence"],
                    item["event_type"],
                    json.loads(item["data_json"]),
                    item["created_at"],
                )
                for item in db.execute(
                    "SELECT * FROM application_events WHERE instance_id=? "
                    "ORDER BY sequence",
                    (instance_id,),
                )
            )
            return ApplicationInstance(
                row["instance_id"],
                row["profile_id"],
                row["target_id"],
                row["initial_input"],
                row["status"],
                row["revision"],
                bool(row["degraded"]),
                bool(row["hard_risk"]),
                row["detail"],
                row["error_code"],
                row["memory_version"],
                tuple(item[0] for item in commands),
                intents,
                outcomes,
                events,
                row["created_at"],
                row["updated_at"],
                row["finished_at"],
                row["wake_at"],
            )

    def list(self, limit: int) -> list[ApplicationInstance]:
        with self._connection() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT instance_id FROM application_instances "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            ]
        return [self.inspect(instance_id) for instance_id in ids]

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database_path, timeout=5.0)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _instance(db: sqlite3.Connection, instance_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM application_instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFound(instance_id)
        return row

    @staticmethod
    def _request(
        db: sqlite3.Connection, request_id: str, digest: str
    ) -> str | None:
        row = db.execute(
            """
            SELECT digest,instance_id FROM application_requests
            WHERE client_request_id=?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["digest"] != digest:
            raise IdempotencyConflict(request_id)
        return str(row["instance_id"])

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        instance_id: str,
        event_type: str,
        data: Mapping[str, Any],
    ) -> None:
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM application_events "
            "WHERE instance_id=?",
            (instance_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO application_events VALUES(?,?,?,?,?)",
            (
                instance_id,
                sequence,
                event_type,
                json.dumps(dict(data), sort_keys=True),
                _now(),
            ),
        )

    @staticmethod
    def _settle(db: sqlite3.Connection, intent_id: str, phase: str) -> None:
        db.execute(
            "UPDATE application_intents SET phase=?,finalized_at=? WHERE intent_id=?",
            (phase, _now(), intent_id),
        )

    @staticmethod
    def _intent(row: sqlite3.Row) -> RuntimeIntent:
        receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
        return RuntimeIntent(
            row["intent_id"],
            row["cycle"],
            row["revision"],
            Intent(
                row["name"],
                json.loads(row["arguments_json"]),
                bool(row["hard_risk"]),
            ),
            row["phase"],
            row["reservation_id"],
            ExecutionReceipt(**receipt) if receipt else None,
            row["created_at"],
            row["finalized_at"],
        )
