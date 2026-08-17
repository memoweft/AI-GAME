from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .domain import (
    ActionAttempt,
    ActionDecision,
    IdempotencyConflict,
    InputRevision,
    MobileTaskState,
    Observation,
    PhysicalIntent,
    PlanDraft,
    Reflection,
    ReflectionDecision,
    SkillMemory,
    Subgoal,
    TaskEvent,
    TaskNotFound,
    TaskPlan,
    TaskStateConflict,
    TransportReceipt,
    Verification,
)


_ACTIVE = {"queued", "planning", "running", "stopping"}
_TERMINAL = {"completed", "failed", "stopped", "uncertain"}
_SCHEMA_VERSION = 2
_LEGACY_SCOPE_PREFIX = "legacy:"
_AUTOMATIC_SCOPE_PREFIX = "auto:"


class _SQLiteTaskStore:
    """Namespaced SQLite implementation hidden behind the Runtime Interface."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection(write=True) as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS mobile_agent_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL
                    );
                    INSERT OR IGNORE INTO mobile_agent_schema(singleton, version)
                    VALUES (1, 2);

                    CREATE TABLE IF NOT EXISTS mobile_tasks (
                        task_id TEXT PRIMARY KEY,
                        goal TEXT NOT NULL,
                        target_id TEXT,
                        skill_id TEXT,
                        skill_scope_id TEXT,
                        status TEXT NOT NULL CHECK (status IN (
                            'queued', 'planning', 'running', 'stopping',
                            'completed', 'failed', 'stopped', 'uncertain'
                        )),
                        input_revision INTEGER NOT NULL DEFAULT 0,
                        plan_revision INTEGER NOT NULL DEFAULT 0,
                        active_subgoal_index INTEGER NOT NULL DEFAULT 0,
                        strategy TEXT NOT NULL DEFAULT 'initial',
                        no_progress_count INTEGER NOT NULL DEFAULT 0,
                        reflection_count INTEGER NOT NULL DEFAULT 0,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0
                            CHECK (cancel_requested IN (0, 1)),
                        verification_satisfied INTEGER NOT NULL DEFAULT 0
                            CHECK (verification_satisfied IN (0, 1)),
                        detail TEXT,
                        error_code TEXT,
                        skill_memory_version INTEGER NOT NULL DEFAULT 0,
                        worker_token TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_mobile_tasks_created
                    ON mobile_tasks(created_at DESC, task_id DESC);

                    CREATE TABLE IF NOT EXISTS mobile_task_requests (
                        client_request_id TEXT PRIMARY KEY,
                        operation TEXT NOT NULL,
                        request_digest TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS mobile_task_plans (
                        task_id TEXT NOT NULL REFERENCES mobile_tasks(task_id),
                        revision INTEGER NOT NULL,
                        subgoals_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(task_id, revision)
                    );

                    CREATE TABLE IF NOT EXISTS mobile_task_inputs (
                        task_id TEXT NOT NULL REFERENCES mobile_tasks(task_id),
                        revision INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        lifecycle TEXT NOT NULL CHECK (lifecycle IN ('accepted', 'applied')),
                        client_request_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        applied_at TEXT,
                        PRIMARY KEY(task_id, revision)
                    );

                    CREATE TABLE IF NOT EXISTS mobile_task_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL REFERENCES mobile_tasks(task_id),
                        sequence INTEGER NOT NULL,
                        plan_revision INTEGER NOT NULL,
                        subgoal_index INTEGER NOT NULL,
                        input_revision INTEGER NOT NULL,
                        decision_json TEXT NOT NULL,
                        before_json TEXT NOT NULL,
                        transport_json TEXT,
                        after_json TEXT,
                        verification_json TEXT,
                        phase TEXT NOT NULL CHECK (phase IN ('intent', 'finalized')),
                        created_at TEXT NOT NULL,
                        finalized_at TEXT,
                        UNIQUE(task_id, sequence)
                    );

                    CREATE TABLE IF NOT EXISTS mobile_task_reflections (
                        task_id TEXT NOT NULL REFERENCES mobile_tasks(task_id),
                        sequence INTEGER NOT NULL,
                        previous_strategy TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        consecutive_no_progress INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(task_id, sequence)
                    );

                    CREATE TABLE IF NOT EXISTS mobile_task_events (
                        task_id TEXT NOT NULL REFERENCES mobile_tasks(task_id),
                        sequence INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(task_id, sequence)
                    );

                    CREATE TABLE IF NOT EXISTS mobile_skill_memories (
                        skill_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        source_task_id TEXT NOT NULL UNIQUE REFERENCES mobile_tasks(task_id),
                        memory_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(skill_id, version)
                    );
                    """
                )
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                version = connection.execute(
                    "SELECT version FROM mobile_agent_schema WHERE singleton = 1"
                ).fetchone()
                if version is None:
                    raise RuntimeError("unsupported mobile-agent database schema")
                stored_version = int(version["version"])
                if stored_version == 1:
                    self._migrate_skill_scope_v2(connection)
                    stored_version = 2
                if stored_version != _SCHEMA_VERSION:
                    raise RuntimeError("unsupported mobile-agent database schema")
            self._initialized = True

    @staticmethod
    def _migrate_skill_scope_v2(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(mobile_tasks)").fetchall()
        }
        if "skill_scope_id" not in columns:
            connection.execute(
                "ALTER TABLE mobile_tasks ADD COLUMN skill_scope_id TEXT"
            )
        connection.execute("DROP TABLE IF EXISTS mobile_skill_memories_v2")
        connection.execute(
            """
            CREATE TABLE mobile_skill_memories_v2 (
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                source_task_id TEXT NOT NULL UNIQUE REFERENCES mobile_tasks(task_id),
                memory_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(skill_id, version)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO mobile_skill_memories_v2(
                skill_id, version, source_task_id, memory_json, created_at
            )
            SELECT 'legacy:' || skill_id, version, source_task_id,
                   memory_json, created_at
            FROM mobile_skill_memories
            """
        )
        connection.execute("DROP TABLE mobile_skill_memories")
        connection.execute(
            "ALTER TABLE mobile_skill_memories_v2 RENAME TO mobile_skill_memories"
        )
        connection.execute(
            """
            UPDATE mobile_tasks SET skill_scope_id = 'legacy:' || skill_id
            WHERE skill_id IS NOT NULL AND skill_scope_id IS NULL
            """
        )
        connection.execute(
            "UPDATE mobile_agent_schema SET version = 2 WHERE singleton = 1"
        )

    def recover_active(self) -> list[str]:
        """Resume only work that has no unresolved physical intent."""

        self.initialize()
        resumable: list[str] = []
        with self._connection(write=True) as connection:
            rows = connection.execute(
                "SELECT * FROM mobile_tasks WHERE status IN ('queued','planning','running','stopping')"
            ).fetchall()
            for row in rows:
                task_id = str(row["task_id"])
                open_rows = connection.execute(
                    """
                    SELECT decision_json FROM mobile_task_attempts
                    WHERE task_id = ? AND phase = 'intent'
                    """,
                    (task_id,),
                ).fetchall()
                open_physical = any(
                    json.loads(item["decision_json"])["kind"] == "act"
                    for item in open_rows
                )
                now = _utc_now()
                if open_physical:
                    connection.execute(
                        """
                        UPDATE mobile_tasks
                        SET status = 'uncertain', verification_satisfied = 0,
                            detail = ?, error_code = 'restart_open_intent',
                            worker_token = NULL, finished_at = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        ("重启时存在结果未落账的物理意图；未重放。", now, now, task_id),
                    )
                    self._event(
                        connection,
                        task_id,
                        "restart_open_intent",
                        {"replayed": False},
                        now,
                    )
                elif bool(row["cancel_requested"]) or row["status"] == "stopping":
                    connection.execute(
                        """
                        UPDATE mobile_tasks
                        SET status = 'stopped', worker_token = NULL,
                            detail = ?, finished_at = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        ("任务在重启恢复时按已接收的停止请求终结。", now, now, task_id),
                    )
                    self._event(connection, task_id, "task_stopped", {"recovery": True}, now)
                else:
                    connection.execute(
                        """
                        UPDATE mobile_tasks
                        SET status = 'queued', worker_token = NULL,
                            detail = ?, error_code = NULL, updated_at = ?
                        WHERE task_id = ?
                        """,
                        ("从持久化检查点恢复。", now, task_id),
                    )
                    self._event(connection, task_id, "task_recovered", {}, now)
                    resumable.append(task_id)
        return resumable

    def accept_start(
        self,
        *,
        task_id: str,
        goal: str,
        target_id: str | None,
        skill_id: str | None,
        skill_scope_id: str | None,
        client_request_id: str,
        request_digest: str,
    ) -> tuple[MobileTaskState, bool]:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            existing = self._existing_request(connection, client_request_id, request_digest)
            if existing is not None:
                return self._get_state(connection, existing), False
            connection.execute(
                """
                INSERT INTO mobile_tasks (
                    task_id, goal, target_id, skill_id, skill_scope_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (task_id, goal, target_id, skill_id, skill_scope_id, now, now),
            )
            self._insert_request(
                connection, client_request_id, "start", request_digest, task_id, now
            )
            self._event(
                connection,
                task_id,
                "task_accepted",
                {"target_id": target_id, "skill_id": skill_id},
                now,
            )
            return self._get_state(connection, task_id), True

    def existing_request(
        self, client_request_id: str, request_digest: str
    ) -> MobileTaskState | None:
        self.initialize()
        with self._connection() as connection:
            task_id = self._existing_request(
                connection, client_request_id, request_digest
            )
            return self._get_state(connection, task_id) if task_id is not None else None

    def accept_input(
        self,
        *,
        task_id: str,
        content: str,
        client_request_id: str,
        request_digest: str,
    ) -> MobileTaskState:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            existing = self._existing_request(connection, client_request_id, request_digest)
            if existing is not None:
                return self._get_state(connection, existing)
            row = self._task_row(connection, task_id)
            if row["status"] in _TERMINAL:
                raise TaskStateConflict(f"task {task_id} is already terminal")
            revision = int(row["input_revision"]) + 1
            connection.execute(
                """
                INSERT INTO mobile_task_inputs (
                    task_id, revision, content, lifecycle, client_request_id, created_at
                ) VALUES (?, ?, ?, 'accepted', ?, ?)
                """,
                (task_id, revision, content, client_request_id, now),
            )
            connection.execute(
                "UPDATE mobile_tasks SET input_revision = ?, updated_at = ? WHERE task_id = ?",
                (revision, now, task_id),
            )
            self._insert_request(
                connection, client_request_id, "send", request_digest, task_id, now
            )
            self._event(
                connection,
                task_id,
                "owner_input_accepted",
                {"input_revision": revision},
                now,
            )
            return self._get_state(connection, task_id)

    def accept_stop(
        self,
        *,
        task_id: str,
        client_request_id: str,
        request_digest: str,
    ) -> MobileTaskState:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            existing = self._existing_request(connection, client_request_id, request_digest)
            if existing is not None:
                return self._get_state(connection, existing)
            row = self._task_row(connection, task_id)
            if row["status"] not in _TERMINAL:
                if row["worker_token"] is None:
                    connection.execute(
                        """
                        UPDATE mobile_tasks
                        SET cancel_requested = 1, status = 'stopped', detail = ?,
                            finished_at = ?, updated_at = ? WHERE task_id = ?
                        """,
                        ("任务在执行前已按用户请求停止。", now, now, task_id),
                    )
                    self._event(connection, task_id, "task_stopped", {}, now)
                else:
                    connection.execute(
                        """
                        UPDATE mobile_tasks
                        SET cancel_requested = 1, status = 'stopping', updated_at = ?
                        WHERE task_id = ?
                        """,
                        (now, task_id),
                    )
                self._event(connection, task_id, "stop_requested", {}, now)
            self._insert_request(
                connection, client_request_id, "stop", request_digest, task_id, now
            )
            return self._get_state(connection, task_id)

    def claim(self, task_id: str, worker_token: str) -> bool:
        self.initialize()
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = self._task_row(connection, task_id)
            if row["status"] not in {"queued", "planning", "running"}:
                return False
            if row["worker_token"] is not None:
                return False
            status = "planning" if int(row["plan_revision"]) == 0 else "running"
            connection.execute(
                """
                UPDATE mobile_tasks
                SET worker_token = ?, status = ?, updated_at = ? WHERE task_id = ?
                """,
                (worker_token, status, now, task_id),
            )
            self._event(connection, task_id, "worker_claimed", {}, now)
            return True

    def set_plan_if_current(
        self,
        *,
        task_id: str,
        worker_token: str,
        expected_input_revision: int,
        draft: PlanDraft,
    ) -> Literal["stored", "stale", "closed"]:
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = self._task_row(connection, task_id)
            if not self._worker_can_continue(row, worker_token):
                return "closed"
            if int(row["input_revision"]) != expected_input_revision:
                self._event(
                    connection,
                    task_id,
                    "plan_stale",
                    {
                        "expected_input_revision": expected_input_revision,
                        "current_input_revision": int(row["input_revision"]),
                    },
                    now,
                )
                return "stale"
            revision = int(row["plan_revision"]) + 1
            connection.execute(
                """
                INSERT INTO mobile_task_plans(task_id, revision, subgoals_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, revision, _json(list(draft.subgoals)), now),
            )
            self._apply_inputs(connection, task_id, expected_input_revision, now)
            connection.execute(
                """
                UPDATE mobile_tasks
                SET plan_revision = ?, active_subgoal_index = 0, status = 'running',
                    detail = NULL, updated_at = ? WHERE task_id = ?
                """,
                (revision, now, task_id),
            )
            self._event(
                connection,
                task_id,
                "plan_recorded",
                {"plan_revision": revision, "subgoal_count": len(draft.subgoals)},
                now,
            )
            return "stored"

    def begin_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        worker_token: str,
        expected_input_revision: int,
        decision: ActionDecision,
        before: Observation,
    ) -> Literal["stored", "stale", "closed"]:
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = self._task_row(connection, task_id)
            if not self._worker_can_continue(row, worker_token):
                return "closed"
            if int(row["input_revision"]) != expected_input_revision:
                self._event(
                    connection,
                    task_id,
                    "decision_stale",
                    {
                        "expected_input_revision": expected_input_revision,
                        "current_input_revision": int(row["input_revision"]),
                    },
                    now,
                )
                return "stale"
            sequence = int(row["attempt_count"]) + 1
            self._apply_inputs(connection, task_id, expected_input_revision, now)
            connection.execute(
                """
                INSERT INTO mobile_task_attempts (
                    attempt_id, task_id, sequence, plan_revision, subgoal_index,
                    input_revision, decision_json, before_json, phase, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'intent', ?)
                """,
                (
                    attempt_id,
                    task_id,
                    sequence,
                    int(row["plan_revision"]),
                    int(row["active_subgoal_index"]),
                    expected_input_revision,
                    _json(_decision_dict(decision)),
                    _json(_observation_dict(before)),
                    now,
                ),
            )
            connection.execute(
                "UPDATE mobile_tasks SET attempt_count = ?, updated_at = ? WHERE task_id = ?",
                (sequence, now, task_id),
            )
            self._event(
                connection,
                task_id,
                "physical_intent_recorded" if decision.kind == "act" else "decision_recorded",
                {"attempt_id": attempt_id, "kind": decision.kind},
                now,
            )
            return "stored"

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        worker_token: str,
        transport: TransportReceipt,
        after: Observation | None,
        verification: Verification,
        terminal: tuple[Literal["failed", "uncertain"], str, str] | None = None,
    ) -> MobileTaskState | None:
        now = _utc_now()
        with self._connection(write=True) as connection:
            task = self._task_row(connection, task_id)
            if task["worker_token"] != worker_token or task["status"] in _TERMINAL:
                return None
            attempt = connection.execute(
                "SELECT * FROM mobile_task_attempts WHERE attempt_id = ? AND task_id = ?",
                (attempt_id, task_id),
            ).fetchone()
            if attempt is None or attempt["phase"] != "intent":
                raise TaskStateConflict("attempt is not open")
            expected_input_revision = int(attempt["input_revision"])
            terminal_uncertainty = terminal is not None and terminal[0] == "uncertain"
            verifier_uncertainty = (
                transport.status == "accepted" and verification.uncertain
            )
            cancelled = bool(task["cancel_requested"]) or task["status"] == "stopping"
            revision_stale = int(task["input_revision"]) != expected_input_revision
            state_effect = (
                "uncertain"
                if terminal_uncertainty or verifier_uncertainty
                else "stopped"
                if cancelled
                else "stale"
                if revision_stale
                else "terminal"
                if terminal is not None
                else "applied"
            )
            verification_data = _verification_dict(verification)
            verification_data["state_effect"] = state_effect
            connection.execute(
                """
                UPDATE mobile_task_attempts
                SET transport_json = ?, after_json = ?, verification_json = ?,
                    phase = 'finalized', finalized_at = ?
                WHERE attempt_id = ?
                """,
                (
                    _json(_transport_dict(transport)),
                    _json(_observation_dict(after)) if after is not None else None,
                    _json(verification_data),
                    now,
                    attempt_id,
                ),
            )
            self._event(
                connection,
                task_id,
                "verification_recorded",
                {
                    "attempt_id": attempt_id,
                    "transport": transport.status,
                    "satisfied": verification.satisfied,
                    "progress": verification.progress,
                    "uncertain": verification.uncertain,
                    "state_effect": state_effect,
                },
                now,
            )
            if terminal_uncertainty or verifier_uncertainty:
                if terminal_uncertainty:
                    status, error_code, detail = terminal  # type: ignore[misc]
                else:
                    status = "uncertain"
                    error_code = "verification_uncertain_after_transport"
                    detail = (
                        "物理动作已被传输，但验证证据仍不确定；任务终止且不会重放。"
                    )
                connection.execute(
                    """
                    UPDATE mobile_tasks
                    SET status = ?, verification_satisfied = 0, detail = ?, error_code = ?,
                        worker_token = NULL, finished_at = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (status, detail, error_code, now, now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "task_uncertain",
                    {"error_code": error_code},
                    now,
                )
                return self._get_state(connection, task_id)
            if cancelled:
                connection.execute(
                    """
                    UPDATE mobile_tasks
                    SET status = 'stopped', verification_satisfied = 0,
                        detail = ?, error_code = NULL, worker_token = NULL,
                        finished_at = ?, updated_at = ? WHERE task_id = ?
                    """,
                    ("物理尝试已结算；停止请求阻止了状态推进。", now, now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "task_stopped",
                    {"attempt_id": attempt_id, "after_transport": True},
                    now,
                )
                return self._get_state(connection, task_id)
            if revision_stale:
                connection.execute(
                    """
                    UPDATE mobile_tasks
                    SET status = 'running', verification_satisfied = 0,
                        no_progress_count = 0, detail = ?, error_code = NULL,
                        updated_at = ? WHERE task_id = ?
                    """,
                    ("验证已保存，但输入版本已变化；未推进旧决定。", now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "verification_stale_after_transport",
                    {
                        "attempt_id": attempt_id,
                        "expected_input_revision": expected_input_revision,
                        "current_input_revision": int(task["input_revision"]),
                    },
                    now,
                )
                return self._get_state(connection, task_id)
            if terminal is not None:
                status, error_code, detail = terminal
                connection.execute(
                    """
                    UPDATE mobile_tasks
                    SET status = ?, verification_satisfied = 0, detail = ?, error_code = ?,
                        worker_token = NULL, finished_at = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (status, detail, error_code, now, now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "task_uncertain" if status == "uncertain" else "task_failed",
                    {"error_code": error_code},
                    now,
                )
                return self._get_state(connection, task_id)
            if verification.satisfied:
                return self._advance_verified(connection, task, verification, now)
            no_progress = 0 if verification.progress else int(task["no_progress_count"]) + 1
            connection.execute(
                """
                UPDATE mobile_tasks
                SET no_progress_count = ?, verification_satisfied = 0, updated_at = ?
                WHERE task_id = ?
                """,
                (no_progress, now, task_id),
            )
            return self._get_state(connection, task_id)

    def fence_physical_dispatch(
        self,
        *,
        attempt_id: str,
        task_id: str,
        worker_token: str,
        expected_input_revision: int,
        shutdown_requested: bool = False,
    ) -> Literal["dispatch", "stale", "stopped", "shutdown", "closed"]:
        """Linearization check immediately before crossing the device seam."""

        now = _utc_now()
        with self._connection(write=True) as connection:
            task = self._task_row(connection, task_id)
            attempt = connection.execute(
                "SELECT * FROM mobile_task_attempts WHERE attempt_id = ? AND task_id = ?",
                (attempt_id, task_id),
            ).fetchone()
            if (
                attempt is None
                or attempt["phase"] != "intent"
                or task["worker_token"] != worker_token
                or task["status"] in _TERMINAL
            ):
                return "closed"
            decision = json.loads(attempt["decision_json"])
            if decision["kind"] != "act":
                raise TaskStateConflict("dispatch fence requires an open physical intent")
            if bool(task["cancel_requested"]) or task["status"] == "stopping":
                verification = Verification(
                    False, False, evidence="cancelled before dispatch"
                )
                self._finalize_not_sent(
                    connection,
                    attempt_id=attempt_id,
                    task_id=task_id,
                    verification=verification,
                    state_effect="stopped",
                    now=now,
                )
                connection.execute(
                    """
                    UPDATE mobile_tasks
                    SET status = 'stopped', verification_satisfied = 0,
                        detail = ?, error_code = NULL, worker_token = NULL,
                        finished_at = ?, updated_at = ? WHERE task_id = ?
                    """,
                    ("物理意图在下发前被停止，未发送。", now, now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "task_stopped",
                    {"attempt_id": attempt_id, "before_dispatch": True},
                    now,
                )
                return "stopped"
            if int(task["input_revision"]) != expected_input_revision:
                verification = Verification(False, False, evidence="stale before dispatch")
                self._finalize_not_sent(
                    connection,
                    attempt_id=attempt_id,
                    task_id=task_id,
                    verification=verification,
                    state_effect="stale",
                    now=now,
                )
                self._event(
                    connection,
                    task_id,
                    "decision_stale_after_intent",
                    {
                        "attempt_id": attempt_id,
                        "expected_input_revision": expected_input_revision,
                        "current_input_revision": int(task["input_revision"]),
                    },
                    now,
                )
                return "stale"
            if shutdown_requested:
                verification = Verification(
                    False, False, evidence="runtime shutdown before dispatch"
                )
                self._finalize_not_sent(
                    connection,
                    attempt_id=attempt_id,
                    task_id=task_id,
                    verification=verification,
                    state_effect="shutdown",
                    now=now,
                )
                connection.execute(
                    """
                    UPDATE mobile_tasks
                    SET status = 'queued', verification_satisfied = 0,
                        detail = ?, error_code = NULL, worker_token = NULL,
                        updated_at = ? WHERE task_id = ?
                    """,
                    ("运行时关闭；物理意图未发送，任务保留待恢复。", now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "task_suspended",
                    {"attempt_id": attempt_id, "before_dispatch": True},
                    now,
                )
                return "shutdown"
            return "dispatch"

    def record_reflection_if_current(
        self,
        *,
        task_id: str,
        worker_token: str,
        expected_input_revision: int,
        decision: ReflectionDecision,
    ) -> Literal["stored", "terminated", "stale", "closed", "unchanged"]:
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = self._task_row(connection, task_id)
            if not self._worker_can_continue(row, worker_token):
                return "closed"
            if int(row["input_revision"]) != expected_input_revision:
                self._event(
                    connection,
                    task_id,
                    "reflection_stale",
                    {"expected_input_revision": expected_input_revision},
                    now,
                )
                return "stale"
            previous = str(row["strategy"])
            strategy = decision.strategy.strip()
            if not decision.terminate and (not strategy or strategy == previous):
                return "unchanged"
            sequence = int(row["reflection_count"]) + 1
            connection.execute(
                """
                INSERT INTO mobile_task_reflections (
                    task_id, sequence, previous_strategy, strategy, reason,
                    consecutive_no_progress, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    sequence,
                    previous,
                    strategy or previous,
                    decision.reason,
                    int(row["no_progress_count"]),
                    now,
                ),
            )
            plan_revision = int(row["plan_revision"])
            active_index = int(row["active_subgoal_index"])
            if decision.replacement_subgoals is not None:
                plan_revision += 1
                active_index = 0
                connection.execute(
                    """
                    INSERT INTO mobile_task_plans(task_id, revision, subgoals_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_id, plan_revision, _json(list(decision.replacement_subgoals)), now),
                )
            self._apply_inputs(connection, task_id, expected_input_revision, now)
            self._event(
                connection,
                task_id,
                "reflection_recorded",
                {"reflection": sequence, "terminated": decision.terminate},
                now,
            )
            if decision.terminate:
                connection.execute(
                    """
                    UPDATE mobile_tasks
                    SET status = 'failed', reflection_count = ?, detail = ?,
                        error_code = 'reflection_terminated', worker_token = NULL,
                        finished_at = ?, updated_at = ? WHERE task_id = ?
                    """,
                    (sequence, decision.reason or "反思决定终止任务。", now, now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "task_failed",
                    {"error_code": "reflection_terminated"},
                    now,
                )
                return "terminated"
            connection.execute(
                """
                UPDATE mobile_tasks
                SET strategy = ?, no_progress_count = 0, reflection_count = ?,
                    plan_revision = ?, active_subgoal_index = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (strategy, sequence, plan_revision, active_index, now, task_id),
            )
            return "stored"

    def fail(
        self,
        task_id: str,
        *,
        worker_token: str,
        error_code: str,
        detail: str,
    ) -> None:
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = self._task_row(connection, task_id)
            if row["status"] in _TERMINAL or row["worker_token"] != worker_token:
                return
            connection.execute(
                """
                UPDATE mobile_tasks
                SET status = 'failed', detail = ?, error_code = ?, worker_token = NULL,
                    verification_satisfied = 0, finished_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (detail, error_code, now, now, task_id),
            )
            self._event(connection, task_id, "task_failed", {"error_code": error_code}, now)

    def fail_unclaimed(self, task_id: str, *, error_code: str, detail: str) -> None:
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = self._task_row(connection, task_id)
            if row["status"] in _TERMINAL or row["worker_token"] is not None:
                return
            connection.execute(
                """
                UPDATE mobile_tasks
                SET status = 'failed', detail = ?, error_code = ?,
                    verification_satisfied = 0, finished_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (detail, error_code, now, now, task_id),
            )
            self._event(connection, task_id, "task_failed", {"error_code": error_code}, now)

    def finish_stopped(self, task_id: str, *, worker_token: str) -> None:
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = self._task_row(connection, task_id)
            if row["status"] in _TERMINAL or row["worker_token"] != worker_token:
                return
            connection.execute(
                """
                UPDATE mobile_tasks
                SET status = 'stopped', detail = ?, worker_token = NULL,
                    verification_satisfied = 0, finished_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                ("任务已按用户请求停止。", now, now, task_id),
            )
            self._event(connection, task_id, "task_stopped", {}, now)

    def release_for_shutdown(self, task_id: str, *, worker_token: str) -> bool:
        """Release a worker only at a checkpoint with no open intent."""

        now = _utc_now()
        with self._connection(write=True) as connection:
            row = self._task_row(connection, task_id)
            if row["status"] in _TERMINAL or row["worker_token"] != worker_token:
                return False
            open_attempts = connection.execute(
                """
                SELECT attempt_id, decision_json FROM mobile_task_attempts
                WHERE task_id = ? AND phase = 'intent'
                """,
                (task_id,),
            ).fetchall()
            if any(
                json.loads(item["decision_json"])["kind"] == "act"
                for item in open_attempts
            ):
                return False
            for item in open_attempts:
                self._finalize_not_sent(
                    connection,
                    attempt_id=str(item["attempt_id"]),
                    task_id=task_id,
                    verification=Verification(
                        False, False, evidence="runtime shutdown at safe checkpoint"
                    ),
                    state_effect="shutdown",
                    now=now,
                )
            connection.execute(
                """
                UPDATE mobile_tasks
                SET status = 'queued', worker_token = NULL,
                    verification_satisfied = 0, detail = ?, error_code = NULL,
                    updated_at = ? WHERE task_id = ?
                """,
                ("运行时关闭；任务从安全检查点保留待恢复。", now, task_id),
            )
            self._event(
                connection,
                task_id,
                "task_suspended",
                {"before_dispatch": False},
                now,
            )
            return True

    def inspect(self, task_id: str) -> MobileTaskState:
        self.initialize()
        with self._connection() as connection:
            return self._get_state(connection, task_id)

    def list(self, limit: int) -> list[MobileTaskState]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT task_id FROM mobile_tasks ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._get_state(connection, str(row["task_id"])) for row in rows]

    def skill_memory(self, skill_scope_id: str | None) -> SkillMemory | None:
        if skill_scope_id is None:
            return None
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM mobile_skill_memories
                WHERE skill_id = ? ORDER BY version DESC LIMIT 1
                """,
                (skill_scope_id,),
            ).fetchone()
        return _skill_memory_from_row(row) if row is not None else None

    def _advance_verified(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        verification: Verification,
        now: str,
    ) -> MobileTaskState:
        task_id = str(task["task_id"])
        descriptions = self._plan_descriptions(
            connection, task_id, int(task["plan_revision"])
        )
        next_index = int(task["active_subgoal_index"]) + 1
        self._event(
            connection,
            task_id,
            "subgoal_verified",
            {"subgoal_index": int(task["active_subgoal_index"]), "evidence": verification.evidence},
            now,
        )
        if next_index < len(descriptions):
            connection.execute(
                """
                UPDATE mobile_tasks
                SET active_subgoal_index = ?, no_progress_count = 0,
                    verification_satisfied = 0, updated_at = ? WHERE task_id = ?
                """,
                (next_index, now, task_id),
            )
            return self._get_state(connection, task_id)
        memory_version = 0
        skill_scope_id = task["skill_scope_id"]
        if skill_scope_id is not None:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM mobile_skill_memories WHERE skill_id = ?",
                (skill_scope_id,),
            ).fetchone()
            memory_version = int(version_row["version"]) + 1
            evidence_rows = connection.execute(
                """
                SELECT verification_json FROM mobile_task_attempts
                WHERE task_id = ? AND phase = 'finalized' ORDER BY sequence
                """,
                (task_id,),
            ).fetchall()
            evidence = tuple(
                decoded["evidence"]
                for item in evidence_rows
                if (decoded := json.loads(item["verification_json"]))["satisfied"]
                and decoded.get("state_effect", "applied") == "applied"
                and decoded["evidence"]
            )
            memory = {
                "procedure": descriptions,
                "strategy": str(task["strategy"]),
                "evidence": evidence,
            }
            connection.execute(
                """
                INSERT INTO mobile_skill_memories(
                    skill_id, version, source_task_id, memory_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (skill_scope_id, memory_version, task_id, _json(memory), now),
            )
        connection.execute(
            """
            UPDATE mobile_tasks
            SET status = 'completed', active_subgoal_index = ?, no_progress_count = 0,
                verification_satisfied = 1, detail = ?, error_code = NULL,
                skill_memory_version = ?, worker_token = NULL,
                finished_at = ?, updated_at = ? WHERE task_id = ?
            """,
            (
                len(descriptions),
                "全部 Subgoal 已由新鲜观察验证。",
                memory_version,
                now,
                now,
                task_id,
            ),
        )
        self._event(
            connection,
            task_id,
            "task_completed",
            {"skill_memory_version": memory_version, "verification_satisfied": True},
            now,
        )
        return self._get_state(connection, task_id)

    def _get_state(self, connection: sqlite3.Connection, task_id: str) -> MobileTaskState:
        row = self._task_row(connection, task_id)
        plan: TaskPlan | None = None
        revision = int(row["plan_revision"])
        active_index = int(row["active_subgoal_index"])
        if revision:
            descriptions = self._plan_descriptions(connection, task_id, revision)
            subgoals = tuple(
                Subgoal(
                    index=index,
                    description=description,
                    status=(
                        "completed"
                        if index < active_index or row["status"] == "completed"
                        else "active"
                        if index == active_index
                        else "pending"
                    ),
                )
                for index, description in enumerate(descriptions)
            )
            plan = TaskPlan(revision, subgoals)
        input_rows = connection.execute(
            "SELECT * FROM mobile_task_inputs WHERE task_id = ? ORDER BY revision",
            (task_id,),
        ).fetchall()
        attempt_rows = connection.execute(
            "SELECT * FROM mobile_task_attempts WHERE task_id = ? ORDER BY sequence",
            (task_id,),
        ).fetchall()
        reflection_rows = connection.execute(
            "SELECT * FROM mobile_task_reflections WHERE task_id = ? ORDER BY sequence",
            (task_id,),
        ).fetchall()
        event_rows = connection.execute(
            """
            SELECT * FROM (
                SELECT * FROM mobile_task_events WHERE task_id = ?
                ORDER BY sequence DESC LIMIT 200
            ) ORDER BY sequence
            """,
            (task_id,),
        ).fetchall()
        return MobileTaskState(
            task_id=task_id,
            goal=str(row["goal"]),
            target_id=row["target_id"],
            skill_id=row["skill_id"],
            skill_scope_id=row["skill_scope_id"],
            status=row["status"],
            input_revision=int(row["input_revision"]),
            plan=plan,
            active_subgoal_index=active_index,
            strategy=str(row["strategy"]),
            no_progress_count=int(row["no_progress_count"]),
            reflection_count=int(row["reflection_count"]),
            attempt_count=int(row["attempt_count"]),
            cancel_requested=bool(row["cancel_requested"]),
            verification_satisfied=bool(row["verification_satisfied"]),
            detail=row["detail"],
            error_code=row["error_code"],
            skill_memory_version=int(row["skill_memory_version"]),
            inputs=tuple(
                InputRevision(
                    revision=int(item["revision"]),
                    content=str(item["content"]),
                    lifecycle=item["lifecycle"],
                    client_request_id=str(item["client_request_id"]),
                    created_at=str(item["created_at"]),
                    applied_at=item["applied_at"],
                )
                for item in input_rows
            ),
            attempts=tuple(_attempt_from_row(item) for item in attempt_rows),
            reflections=tuple(
                Reflection(
                    sequence=int(item["sequence"]),
                    previous_strategy=str(item["previous_strategy"]),
                    strategy=str(item["strategy"]),
                    reason=str(item["reason"]),
                    consecutive_no_progress=int(item["consecutive_no_progress"]),
                    created_at=str(item["created_at"]),
                )
                for item in reflection_rows
            ),
            events=tuple(
                TaskEvent(
                    sequence=int(item["sequence"]),
                    event_type=str(item["event_type"]),
                    data=json.loads(item["data_json"]),
                    created_at=str(item["created_at"]),
                )
                for item in event_rows
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            finished_at=row["finished_at"],
        )

    def _existing_request(
        self, connection: sqlite3.Connection, client_request_id: str, request_digest: str
    ) -> str | None:
        row = connection.execute(
            "SELECT * FROM mobile_task_requests WHERE client_request_id = ?",
            (client_request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise IdempotencyConflict(client_request_id)
        return str(row["task_id"])

    @staticmethod
    def _insert_request(
        connection: sqlite3.Connection,
        client_request_id: str,
        operation: str,
        request_digest: str,
        task_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO mobile_task_requests(
                client_request_id, operation, request_digest, task_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (client_request_id, operation, request_digest, task_id, now),
        )

    @staticmethod
    def _worker_can_continue(row: sqlite3.Row, worker_token: str) -> bool:
        return (
            row["worker_token"] == worker_token
            and row["status"] in {"planning", "running"}
            and not bool(row["cancel_requested"])
        )

    @staticmethod
    def _apply_inputs(
        connection: sqlite3.Connection, task_id: str, revision: int, now: str
    ) -> None:
        connection.execute(
            """
            UPDATE mobile_task_inputs
            SET lifecycle = 'applied', applied_at = COALESCE(applied_at, ?)
            WHERE task_id = ? AND revision <= ?
            """,
            (now, task_id, revision),
        )

    @staticmethod
    def _plan_descriptions(
        connection: sqlite3.Connection, task_id: str, revision: int
    ) -> tuple[str, ...]:
        row = connection.execute(
            "SELECT subgoals_json FROM mobile_task_plans WHERE task_id = ? AND revision = ?",
            (task_id, revision),
        ).fetchone()
        if row is None:
            raise TaskStateConflict("TaskPlan revision is missing")
        return tuple(json.loads(row["subgoals_json"]))

    @staticmethod
    def _task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM mobile_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return row

    def _event(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        event_type: str,
        data: dict[str, Any],
        now: str,
    ) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM mobile_task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        sequence = int(row["sequence"]) + 1
        connection.execute(
            """
            INSERT INTO mobile_task_events(task_id, sequence, event_type, data_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, sequence, event_type, _json(data), now),
        )

    def _finalize_not_sent(
        self,
        connection: sqlite3.Connection,
        *,
        attempt_id: str,
        task_id: str,
        verification: Verification,
        state_effect: Literal["stale", "stopped", "shutdown"],
        now: str,
    ) -> None:
        receipt = TransportReceipt("not_sent")
        verification_data = _verification_dict(verification)
        verification_data["state_effect"] = state_effect
        connection.execute(
            """
            UPDATE mobile_task_attempts
            SET transport_json = ?, verification_json = ?, phase = 'finalized',
                finalized_at = ? WHERE attempt_id = ? AND phase = 'intent'
            """,
            (
                _json(_transport_dict(receipt)),
                _json(verification_data),
                now,
                attempt_id,
            ),
        )
        self._event(
            connection,
            task_id,
            "verification_recorded",
            {
                "attempt_id": attempt_id,
                "transport": "not_sent",
                "satisfied": False,
                "progress": False,
                "uncertain": False,
                "state_effect": state_effect,
            },
            now,
        )

    def _connection(self, *, write: bool = False):
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection.execute("BEGIN")
        return _ConnectionContext(connection)


class _ConnectionContext:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.connection.rollback() if exc_type else self.connection.commit()
        finally:
            self.connection.close()


def _attempt_from_row(row: sqlite3.Row) -> ActionAttempt:
    decision_data = json.loads(row["decision_json"])
    intent_data = decision_data.get("intent")
    decision = ActionDecision(
        kind=decision_data["kind"],
        intent=(
            PhysicalIntent(intent_data["name"], intent_data.get("arguments", {}))
            if intent_data is not None
            else None
        ),
        reason=decision_data.get("reason", ""),
    )
    transport_data = json.loads(row["transport_json"]) if row["transport_json"] else None
    verification_data = (
        json.loads(row["verification_json"]) if row["verification_json"] else None
    )
    return ActionAttempt(
        attempt_id=str(row["attempt_id"]),
        sequence=int(row["sequence"]),
        plan_revision=int(row["plan_revision"]),
        subgoal_index=int(row["subgoal_index"]),
        input_revision=int(row["input_revision"]),
        decision=decision,
        before=_observation_from_dict(json.loads(row["before_json"])),
        transport=(TransportReceipt(**transport_data) if transport_data else None),
        after=(
            _observation_from_dict(json.loads(row["after_json"]))
            if row["after_json"]
            else None
        ),
        verification=(
            Verification(
                satisfied=bool(verification_data["satisfied"]),
                progress=bool(verification_data["progress"]),
                uncertain=bool(verification_data.get("uncertain", False)),
                evidence=str(verification_data.get("evidence", "")),
            )
            if verification_data
            else None
        ),
        created_at=str(row["created_at"]),
        finalized_at=row["finalized_at"],
    )


def _skill_memory_from_row(row: sqlite3.Row) -> SkillMemory:
    data = json.loads(row["memory_json"])
    return SkillMemory(
        skill_id=str(row["skill_id"]),
        version=int(row["version"]),
        source_task_id=str(row["source_task_id"]),
        procedure=tuple(data["procedure"]),
        strategy=str(data["strategy"]),
        evidence=tuple(data["evidence"]),
        created_at=str(row["created_at"]),
    )


def _decision_dict(decision: ActionDecision) -> dict[str, Any]:
    return {
        "kind": decision.kind,
        "intent": (
            {"name": decision.intent.name, "arguments": dict(decision.intent.arguments)}
            if decision.intent is not None
            else None
        ),
        "reason": decision.reason,
    }


def _observation_dict(observation: Observation | None) -> dict[str, str] | None:
    if observation is None:
        return None
    return {"evidence_id": observation.evidence_id, "summary": observation.summary}


def _observation_from_dict(data: dict[str, str]) -> Observation:
    return Observation(data["evidence_id"], data["summary"])


def _transport_dict(receipt: TransportReceipt) -> dict[str, Any]:
    return {
        "status": receipt.status,
        "receipt_id": receipt.receipt_id,
        "detail": receipt.detail,
    }


def _verification_dict(verification: Verification) -> dict[str, Any]:
    return {
        "satisfied": verification.satisfied,
        "progress": verification.progress,
        "uncertain": verification.uncertain,
        "evidence": verification.evidence,
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _legacy_skill_scope_id(skill_id: str) -> str:
    return f"{_LEGACY_SCOPE_PREFIX}{skill_id}"


def _automatic_skill_scope_id(scope: str) -> str:
    return f"{_AUTOMATIC_SCOPE_PREFIX}{scope}"
