"""Durable identity, operation, event, and evidence indexes for execution v1."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS harness_executions (
    execution_id TEXT PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    dsh_session_id TEXT NOT NULL,
    dsh_turn_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    root_call_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    internal_client_request_id TEXT NOT NULL UNIQUE,
    internal_session_id TEXT,
    execution_mode TEXT,
    kernel_task_id TEXT UNIQUE,
    phone_confirmed_at TEXT,
    phone_confirmation_operation_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_operations (
    execution_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    PRIMARY KEY (execution_id, operation_key),
    FOREIGN KEY (execution_id) REFERENCES harness_executions(execution_id)
);
CREATE TABLE IF NOT EXISTS harness_events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (execution_id, dedupe_key),
    FOREIGN KEY (execution_id) REFERENCES harness_executions(execution_id)
);
CREATE TABLE IF NOT EXISTS harness_evidence (
    evidence_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    reference TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    canonical_device_id TEXT,
    companion_install_id TEXT,
    boot_id TEXT,
    connection_epoch INTEGER,
    kernel_action_id TEXT,
    command_id TEXT,
    caused_by_command_id TEXT,
    adapter_id TEXT,
    physical_execution_count INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (execution_id, reference),
    FOREIGN KEY (execution_id) REFERENCES harness_executions(execution_id)
);
-- v2 keeps only adapter correlation, authorization ownership, operation
-- idempotency, and a projection cursor.  It intentionally contains no task
-- status, revision, event, or lifecycle state: AgentRuntime owns those facts.
CREATE TABLE IF NOT EXISTS execution_v2_aliases (
    execution_id TEXT PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    task_id TEXT NOT NULL,
    submission_key TEXT,
    principal_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    dsh_session_id TEXT NOT NULL,
    dsh_turn_id TEXT,
    tool_call_id TEXT,
    root_call_id TEXT,
    envelope_hash TEXT,
    runner_kind TEXT,
    runner_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_v2_aliases_task_id
ON execution_v2_aliases(task_id);
CREATE TABLE IF NOT EXISTS execution_v2_submission_scopes (
    principal_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    submission_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    task_id TEXT,
    dsh_session_id TEXT NOT NULL,
    dsh_turn_id TEXT NOT NULL,
    canonical_envelope_json TEXT,
    envelope_hash TEXT,
    runner_kind TEXT,
    runner_version TEXT,
    device_profile_id TEXT,
    authorization_mode TEXT,
    canonical_client_request_id TEXT,
    canonical_idempotency_key TEXT,
    canonical_origin_json TEXT,
    canonical_origin_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, controller_id, submission_key)
);
CREATE INDEX IF NOT EXISTS idx_execution_v2_submission_scopes_task_id
ON execution_v2_submission_scopes(task_id);
CREATE TABLE IF NOT EXISTS execution_v2_operations (
    task_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    PRIMARY KEY (task_id, operation_key)
);
CREATE TABLE IF NOT EXISTS execution_v2_projection_checkpoints (
    task_id TEXT PRIMARY KEY,
    source_cursor INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


class SQLiteExecutionContractStore:
    """Small SQLite authority for the external-to-internal execution mapping."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(harness_operations)")
            }
            if "applied_at" not in columns:
                connection.execute(
                    "ALTER TABLE harness_operations ADD COLUMN applied_at TEXT"
                )
            execution_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(harness_executions)")
            }
            if "execution_mode" not in execution_columns:
                connection.execute(
                    "ALTER TABLE harness_executions ADD COLUMN execution_mode TEXT"
                )
            if "kernel_task_id" not in execution_columns:
                connection.execute(
                    "ALTER TABLE harness_executions ADD COLUMN kernel_task_id TEXT"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_harness_executions_kernel_task "
                    "ON harness_executions(kernel_task_id) WHERE kernel_task_id IS NOT NULL"
                )
            if "phone_confirmed_at" not in execution_columns:
                connection.execute(
                    "ALTER TABLE harness_executions ADD COLUMN phone_confirmed_at TEXT"
                )
            if "phone_confirmation_operation_key" not in execution_columns:
                connection.execute(
                    "ALTER TABLE harness_executions "
                    "ADD COLUMN phone_confirmation_operation_key TEXT"
                )
            evidence_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(harness_evidence)")
            }
            for name, sql_type in (
                ("canonical_device_id", "TEXT"),
                ("companion_install_id", "TEXT"),
                ("boot_id", "TEXT"),
                ("connection_epoch", "INTEGER"),
                ("kernel_action_id", "TEXT"),
                ("command_id", "TEXT"),
                ("caused_by_command_id", "TEXT"),
                ("adapter_id", "TEXT"),
                ("physical_execution_count", "INTEGER"),
            ):
                if name not in evidence_columns:
                    connection.execute(
                        f"ALTER TABLE harness_evidence ADD COLUMN {name} {sql_type}"
                    )
            self._migrate_v2_owner_columns(connection)
            self._migrate_v2_attempt_columns(connection)
            self._migrate_v2_admission_columns(connection)

    @staticmethod
    def _migrate_v2_owner_columns(connection: sqlite3.Connection) -> None:
        """Split the preview DSH-session owner into explicit capability context."""
        for table in ("execution_v2_aliases", "execution_v2_operations"):
            columns = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "principal_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN principal_id TEXT")
            if "controller_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN controller_id TEXT")
            # v2 has not been composed.  Preserve any local preview record
            # under a legacy capability instead of treating a DSH session as
            # a continuing authorization principal.
            if "principal_key" in columns:
                connection.execute(
                    f"UPDATE {table} SET principal_id = COALESCE(principal_id, principal_key), "
                    "controller_id = COALESCE(controller_id, 'legacy-v2-preview')"
                )

    @staticmethod
    def _migrate_v2_attempt_columns(connection: sqlite3.Connection) -> None:
        """Add retry-attempt provenance without rewriting historical v2 aliases."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(execution_v2_aliases)")
        }
        for name in (
            "submission_key", "dsh_turn_id", "tool_call_id", "root_call_id",
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE execution_v2_aliases ADD COLUMN {name} TEXT"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_v2_aliases_submission "
            "ON execution_v2_aliases(submission_key)"
        )

    @staticmethod
    def _migrate_v2_admission_columns(connection: sqlite3.Connection) -> None:
        """Add K0 create-envelope facts without rewriting historical submissions.

        Existing rows deliberately remain without an envelope.  They can retain
        their public aliases, but cannot be admitted to a new runner because an
        exact replayable create request was never persisted for them.
        """
        additions = {
            "execution_v2_aliases": (
                ("envelope_hash", "TEXT"), ("runner_kind", "TEXT"),
                ("runner_version", "TEXT"),
            ),
            "execution_v2_submission_scopes": (
                ("canonical_envelope_json", "TEXT"), ("envelope_hash", "TEXT"),
                ("runner_kind", "TEXT"), ("runner_version", "TEXT"),
                ("device_profile_id", "TEXT"), ("authorization_mode", "TEXT"),
                ("canonical_client_request_id", "TEXT"),
                ("canonical_idempotency_key", "TEXT"),
                ("canonical_origin_json", "TEXT"), ("canonical_origin_hash", "TEXT"),
            ),
        }
        for table, columns in additions.items():
            existing = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for name, sql_type in columns:
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def reserve(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM harness_executions WHERE identity_key = ?",
                (record["identity_key"],),
            ).fetchone()
            if existing is not None:
                return _row(existing), False
            connection.execute(
                """INSERT INTO harness_executions (
                    execution_id, identity_key, payload_hash, payload_json,
                    dsh_session_id, dsh_turn_id, tool_call_id, root_call_id,
                    client_request_id, idempotency_key, internal_client_request_id,
                    internal_session_id, execution_mode, kernel_task_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)""",
                tuple(record[name] for name in (
                    "execution_id", "identity_key", "payload_hash", "payload_json",
                    "dsh_session_id", "dsh_turn_id", "tool_call_id", "root_call_id",
                    "client_request_id", "idempotency_key", "internal_client_request_id",
                    "execution_mode", "created_at", "updated_at",
                )),
            )
            return dict(record, internal_session_id=None), True

    def bind_internal(self, execution_id: str, session_id: str, updated_at: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE harness_executions SET internal_session_id = ?, updated_at = ? "
                "WHERE execution_id = ? AND (internal_session_id IS NULL OR internal_session_id = ?)",
                (session_id, updated_at, execution_id, session_id),
            )
            row = connection.execute(
                "SELECT internal_session_id FROM harness_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None or row["internal_session_id"] != session_id:
                raise RuntimeError("execution internal identity conflict")

    def bind_kernel_task(
        self, execution_id: str, kernel_task_id: str, updated_at: str
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE harness_executions SET kernel_task_id = ?, updated_at = ? "
                "WHERE execution_id = ? AND (kernel_task_id IS NULL OR kernel_task_id = ?)",
                (kernel_task_id, updated_at, execution_id, kernel_task_id),
            )
            row = connection.execute(
                "SELECT kernel_task_id FROM harness_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None or row["kernel_task_id"] != kernel_task_id:
                raise RuntimeError("execution Kernel task identity conflict")

    def confirm_phone_execution(
        self, execution_id: str, operation_key: str, confirmed_at: str
    ) -> None:
        """Persist the pre-action authorization without changing task identity.

        Legacy executions use a user answer; DSH-authorized executions use a
        stable host-issued operation key.  Both intentionally share the
        existing durable marker so restart recovery has one narrow rule.
        """
        with self.transaction() as connection:
            connection.execute(
                "UPDATE harness_executions SET "
                "phone_confirmed_at = COALESCE(phone_confirmed_at, ?), "
                "phone_confirmation_operation_key = "
                "COALESCE(phone_confirmation_operation_key, ?), updated_at = ? "
                "WHERE execution_id = ? AND execution_mode = ?",
                (
                    confirmed_at,
                    operation_key,
                    confirmed_at,
                    execution_id,
                    "physical_android_companion",
                ),
            )
            row = connection.execute(
                "SELECT phone_confirmed_at, phone_confirmation_operation_key "
                "FROM harness_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if (
                row is None
                or row["phone_confirmed_at"] is None
                or row["phone_confirmation_operation_key"] != operation_key
            ):
                raise RuntimeError("phone execution confirmation identity conflict")

    def get(self, execution_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM harness_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        return _row(row) if row is not None else None

    def by_identity(self, identity_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM harness_executions WHERE identity_key = ?", (identity_key,)
            ).fetchone()
        return _row(row) if row is not None else None

    def records_for_mode(self, execution_mode: str) -> list[dict[str, Any]]:
        """Return durable execution owners in creation order for startup recovery."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM harness_executions WHERE execution_mode = ? "
                "ORDER BY created_at, execution_id",
                (execution_mode,),
            ).fetchall()
        return [_row(row) for row in rows]

    def record_operation(
        self, execution_id: str, operation_key: str, kind: str,
        payload_hash: str, created_at: str,
    ) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT operation_kind, payload_hash, applied_at FROM harness_operations "
                "WHERE execution_id = ? AND operation_key = ?",
                (execution_id, operation_key),
            ).fetchone()
            if row is not None:
                if row["operation_kind"] != kind or row["payload_hash"] != payload_hash:
                    raise ValueError("operation idempotency conflict")
                return row["applied_at"] is not None
            connection.execute(
                "INSERT INTO harness_operations "
                "(execution_id, operation_key, operation_kind, payload_hash, created_at, applied_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (execution_id, operation_key, kind, payload_hash, created_at),
            )
            return False

    def mark_operation_applied(
        self, execution_id: str, operation_key: str, applied_at: str
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE harness_operations SET applied_at = COALESCE(applied_at, ?) "
                "WHERE execution_id = ? AND operation_key = ?",
                (applied_at, execution_id, operation_key),
            )

    def append_event(
        self, execution_id: str, dedupe_key: str, event_type: str,
        payload: dict[str, Any], created_at: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO harness_events "
                "(execution_id, dedupe_key, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (execution_id, dedupe_key, event_type, _json(payload), created_at),
            )

    def events(self, execution_id: str, after: int, limit: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT cursor, event_type, payload_json, created_at FROM harness_events "
                "WHERE execution_id = ? AND cursor > ? ORDER BY cursor LIMIT ?",
                (execution_id, after, limit),
            ).fetchall()
        return [
            {
                "cursor": int(row["cursor"]),
                "type": str(row["event_type"]),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def latest_cursor(self, execution_id: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(cursor), 0) AS cursor FROM harness_events "
                "WHERE execution_id = ?", (execution_id,),
            ).fetchone()
        return int(row["cursor"]) if row is not None else 0

    # v2 adapter-only persistence -------------------------------------------------

    def reserve_v2_submission(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Atomically fence one owner-scoped DSH submit intent before Task creation."""
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM execution_v2_submission_scopes "
                "WHERE principal_id = ? AND controller_id = ? AND submission_key = ?",
                (
                    record["principal_id"], record["controller_id"],
                    record["submission_key"],
                ),
            ).fetchone()
            if existing is not None:
                return _row(existing), False
            connection.execute(
                """INSERT INTO execution_v2_submission_scopes
                   (principal_id, controller_id, submission_key, request_hash, task_id,
                    dsh_session_id, dsh_turn_id, canonical_envelope_json, envelope_hash,
                    runner_kind, runner_version, device_profile_id, authorization_mode,
                    canonical_client_request_id, canonical_idempotency_key,
                    canonical_origin_json, canonical_origin_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(
                    record.get(name) if name in {
                        "canonical_envelope_json", "envelope_hash", "runner_kind", "runner_version",
                        "device_profile_id", "authorization_mode", "canonical_client_request_id",
                        "canonical_idempotency_key", "canonical_origin_json", "canonical_origin_hash",
                    } else record[name]
                    for name in (
                        "principal_id", "controller_id", "submission_key", "request_hash",
                        "dsh_session_id", "dsh_turn_id", "canonical_envelope_json", "envelope_hash",
                        "runner_kind", "runner_version", "device_profile_id", "authorization_mode",
                        "canonical_client_request_id", "canonical_idempotency_key",
                        "canonical_origin_json", "canonical_origin_hash", "created_at", "updated_at",
                    )
                ),
            )
            return dict(record, task_id=None), True

    def bind_v2_submission(
        self, *, principal_id: str, controller_id: str, submission_key: str,
        task_id: str, updated_at: str,
    ) -> dict[str, Any]:
        """Bind the reserved submit intent exactly once to its canonical Task."""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE execution_v2_submission_scopes SET task_id = ?, updated_at = ? "
                "WHERE principal_id = ? AND controller_id = ? AND submission_key = ? "
                "AND (task_id IS NULL OR task_id = ?)",
                (
                    task_id, updated_at, principal_id, controller_id,
                    submission_key, task_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM execution_v2_submission_scopes "
                "WHERE principal_id = ? AND controller_id = ? AND submission_key = ?",
                (principal_id, controller_id, submission_key),
            ).fetchone()
            if row is None or str(row["task_id"] or "") != task_id:
                raise ValueError("submission task binding conflict")
            return _row(row)

    def v2_submission(
        self, principal_id: str, controller_id: str, submission_key: str,
    ) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM execution_v2_submission_scopes "
                "WHERE principal_id = ? AND controller_id = ? AND submission_key = ?",
                (principal_id, controller_id, submission_key),
            ).fetchone()
        return _row(row) if row is not None else None

    def reserve_v2_alias(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Reserve an execution alias without creating a parallel Task record."""
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM execution_v2_aliases WHERE identity_key = ?",
                (record["identity_key"],),
            ).fetchone()
            if existing is not None:
                return _row(existing), False
            connection.execute(
                """INSERT INTO execution_v2_aliases
                   (execution_id, identity_key, payload_hash, task_id, submission_key,
                    principal_id, controller_id, dsh_session_id, dsh_turn_id,
                    tool_call_id, root_call_id, envelope_hash, runner_kind, runner_version,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(record[name] for name in (
                    "execution_id", "identity_key", "payload_hash", "task_id",
                    "submission_key", "principal_id", "controller_id", "dsh_session_id",
                    "dsh_turn_id", "tool_call_id", "root_call_id", "envelope_hash",
                    "runner_kind", "runner_version", "created_at", "updated_at",
                )),
            )
            return dict(record), True

    def v2_alias_by_identity(self, identity_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM execution_v2_aliases WHERE identity_key = ?", (identity_key,)
            ).fetchone()
        return _row(row) if row is not None else None

    def v2_alias_for_task(self, task_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM execution_v2_aliases WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row(row) if row is not None else None

    def v2_tasks_for_owner(self, principal_id: str, controller_id: str) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT task_id, MIN(created_at) AS first_created_at FROM execution_v2_aliases "
                "WHERE principal_id = ? AND controller_id = ? "
                "GROUP BY task_id ORDER BY first_created_at, task_id",
                (principal_id, controller_id),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def v2_attempts_for_submission(self, submission_key: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM execution_v2_aliases WHERE submission_key = ? "
                "ORDER BY created_at, execution_id",
                (submission_key,),
            ).fetchall()
        return [_row(row) for row in rows]

    def v2_runner_admission(
        self, *, task_id: str, principal_id: str, controller_id: str,
    ) -> dict[str, Any] | None:
        """Return only a fully joined, exact runner-admission fact.

        This is deliberately a query, not a scheduler decision.  A future
        scheduler must still compare this immutable fact with the canonical
        Task marker/binding before doing Profile I/O or claiming a wake.
        """
        with closing(self._connect()) as connection:
            scope_row = connection.execute(
                """SELECT * FROM execution_v2_submission_scopes
                   WHERE task_id = ? AND principal_id = ? AND controller_id = ?""",
                (task_id, principal_id, controller_id),
            ).fetchone()
            if scope_row is None:
                return None
            # Do not JOIN away bad attempts.  Inspect every alias bearing this
            # opaque key so a corrupted same-scope attempt cannot disappear
            # behind the happy-path join predicates.
            aliases = connection.execute(
                """SELECT * FROM execution_v2_aliases WHERE submission_key = ?
                   ORDER BY created_at, execution_id""",
                (scope_row["submission_key"],),
            ).fetchall()
        if not aliases:
            return None
        first = _row(scope_row)
        required = (
            first.get("task_id"), first.get("submission_key"), first.get("canonical_envelope_json"),
            first.get("envelope_hash"),
            first.get("request_hash"), first.get("principal_id"), first.get("controller_id"),
            first.get("runner_kind"), first.get("runner_version"),
            first.get("canonical_origin_json"), first.get("canonical_origin_hash"),
        )
        if any(not isinstance(value, str) or not value for value in required):
            return None
        try:
            envelope = json.loads(str(first["canonical_envelope_json"]))
            canonical_origin = json.loads(str(first["canonical_origin_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(envelope, dict) or not isinstance(canonical_origin, dict)
            or _sha_json(envelope) != first["envelope_hash"]
            or _sha_json(canonical_origin) != first["canonical_origin_hash"]
            or envelope.get("origin") != canonical_origin
        ):
            return None
        goal = envelope.get("goal") if isinstance(envelope.get("goal"), dict) else {}
        stable_goal = {key: value for key, value in goal.items() if key != "summary"}
        expected_request_hash = _sha_json({
            "client_request_id": envelope.get("client_request_id"),
            "idempotency_key": envelope.get("idempotency_key"),
            "goal": stable_goal,
            "priority": envelope.get("priority", 50),
            "device_profile_id": envelope.get("device_profile_id"),
            "runner_kind": envelope.get("runner_kind"),
            "runner_version": envelope.get("runner_version"),
            "authorization_mode": envelope.get("authorization_mode"),
        })
        if not (
            first["request_hash"] == expected_request_hash
            and envelope.get("principal_id") == principal_id
            and envelope.get("controller_id") == controller_id
            and envelope.get("runner_kind") == first["runner_kind"]
            and envelope.get("runner_version") == first["runner_version"]
            and envelope.get("device_profile_id") == first.get("device_profile_id")
            and envelope.get("authorization_mode") == first.get("authorization_mode")
            and envelope.get("client_request_id") == first.get("canonical_client_request_id")
            and envelope.get("idempotency_key") == first.get("canonical_idempotency_key")
        ):
            return None
        if not (
            first["task_id"] == task_id
            and first["principal_id"] == principal_id
            and first["controller_id"] == controller_id
        ):
            return None
        if first["runner_kind"] != "android_ui_agent" or first["runner_version"] != "1":
            return None
        valid_attempts = 0
        # More than one response-drop attempt is normal, but every attempt in
        # this owner scope must bind to exactly the same immutable facts.  A
        # different owner may reuse the opaque key only for a different Task;
        # that is a separate authorization scope and must not leak or poison it.
        for row in aliases:
            item = _row(row)
            same_owner = (
                item.get("principal_id") == principal_id
                and item.get("controller_id") == controller_id
            )
            if not same_owner:
                if item.get("task_id") == task_id:
                    return None
                continue
            if not (
                item.get("task_id") == task_id
                and item.get("submission_key") == first["submission_key"]
                and item.get("principal_id") == principal_id
                and item.get("controller_id") == controller_id
                and item.get("envelope_hash") == first["envelope_hash"]
                and item.get("runner_kind") == first["runner_kind"]
                and item.get("runner_version") == first["runner_version"]
            ):
                return None
            valid_attempts += 1
        if valid_attempts == 0:
            return None
        return {
            "task_id": task_id,
            "principal_id": principal_id,
            "controller_id": controller_id,
            "submission_key": first["submission_key"],
            "request_hash": first["request_hash"],
            "envelope_hash": first["envelope_hash"],
            "runner_kind": first["runner_kind"],
            "runner_version": first["runner_version"],
            "device_profile_id": first.get("device_profile_id"),
        }

    def reserve_v2_operation(
        self, *, task_id: str, operation_key: str, operation_kind: str,
        payload_hash: str, principal_id: str, controller_id: str, created_at: str,
    ) -> tuple[dict[str, Any], bool]:
        """Reserve a mutation idempotency key; retries retain the original result."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v2_operations WHERE task_id = ? AND operation_key = ?",
                (task_id, operation_key),
            ).fetchone()
            if row is not None:
                existing = _row(row)
                if (
                    existing["operation_kind"] != operation_kind
                    or existing["payload_hash"] != payload_hash
                    or existing["principal_id"] != principal_id
                    or existing["controller_id"] != controller_id
                ):
                    raise ValueError("operation idempotency conflict")
                return existing, False
            connection.execute(
                """INSERT INTO execution_v2_operations
                   (task_id, operation_key, operation_kind, payload_hash, principal_id,
                    controller_id, result_json, created_at, applied_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL)""",
                (task_id, operation_key, operation_kind, payload_hash, principal_id, controller_id, created_at),
            )
            return {
                "task_id": task_id, "operation_key": operation_key,
                "operation_kind": operation_kind, "payload_hash": payload_hash,
                "principal_id": principal_id, "controller_id": controller_id, "result_json": None,
                "created_at": created_at, "applied_at": None,
            }, True

    def complete_v2_operation(
        self, *, task_id: str, operation_key: str, result: dict[str, Any], applied_at: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE execution_v2_operations SET result_json = COALESCE(result_json, ?), "
                "applied_at = COALESCE(applied_at, ?) WHERE task_id = ? AND operation_key = ?",
                (_json(result), applied_at, task_id, operation_key),
            )

    def v2_operation_result(self, record: dict[str, Any]) -> dict[str, Any] | None:
        raw = record.get("result_json")
        return json.loads(str(raw)) if raw is not None else None

    def checkpoint_v2_projection(self, task_id: str, source_cursor: int, updated_at: str) -> None:
        """Store a replay checkpoint only; it must never contain Task status."""
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO execution_v2_projection_checkpoints
                   (task_id, source_cursor, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     source_cursor = MAX(source_cursor, excluded.source_cursor),
                     updated_at = excluded.updated_at""",
                (task_id, source_cursor, updated_at),
            )

    def v2_projection_checkpoint(self, task_id: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT source_cursor FROM execution_v2_projection_checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["source_cursor"]) if row is not None else 0

    def put_evidence(self, record: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO harness_evidence
                   (evidence_id, execution_id, reference, content_type, size_bytes, sha256,
                    canonical_device_id, companion_install_id, boot_id, connection_epoch,
                    kernel_action_id, command_id, caused_by_command_id, adapter_id,
                    physical_execution_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(evidence_id) DO NOTHING""",
                tuple(
                    record.get(name)
                    if name in {
                        "canonical_device_id", "companion_install_id", "boot_id",
                        "connection_epoch", "kernel_action_id", "command_id",
                        "caused_by_command_id", "adapter_id", "physical_execution_count",
                    }
                    else record[name]
                    for name in (
                    "evidence_id", "execution_id", "reference", "content_type",
                    "size_bytes", "sha256", "canonical_device_id",
                    "companion_install_id", "boot_id", "connection_epoch",
                    "kernel_action_id", "command_id", "caused_by_command_id",
                    "adapter_id", "physical_execution_count", "created_at",
                )),
            )
            persisted = connection.execute(
                "SELECT * FROM harness_evidence WHERE evidence_id = ?",
                (record["evidence_id"],),
            ).fetchone()
            if persisted is None or any(
                persisted[name] != record.get(name)
                for name in (
                    "execution_id", "reference", "content_type", "size_bytes", "sha256",
                    "canonical_device_id", "companion_install_id", "boot_id",
                    "connection_epoch", "kernel_action_id", "command_id",
                    "caused_by_command_id", "adapter_id", "physical_execution_count",
                )
            ):
                raise RuntimeError("execution evidence provenance conflict")

    def evidence(self, execution_id: str, evidence_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM harness_evidence WHERE execution_id = ? AND evidence_id = ?",
                (execution_id, evidence_id),
            ).fetchone()
        return _row(row) if row is not None else None

    def evidence_for(self, execution_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM harness_evidence WHERE execution_id = ? ORDER BY created_at, evidence_id",
                (execution_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


__all__ = ["SQLiteExecutionContractStore"]
