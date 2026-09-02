from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import (
    CapabilityBindingPlan,
    GoalCompletionAssessment,
    GoalIdempotencyConflict,
    GoalNotFound,
    GoalRecord,
    GoalNotification,
    GoalSpecificationDraft,
    StoredEvent,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO goal_schema(singleton, version) VALUES (1, 7);

CREATE TABLE IF NOT EXISTS goal_runs (
    goal_id TEXT PRIMARY KEY,
    original_goal TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    control_state TEXT NOT NULL,
    resume_execution_status TEXT,
    active_stage TEXT,
    binding_kind TEXT NOT NULL,
    binding_state TEXT NOT NULL,
    bound_task_id TEXT UNIQUE,
    target_id TEXT,
    waiting_reason_json TEXT,
    error_json TEXT,
    result_summary TEXT,
    environment_json TEXT NOT NULL DEFAULT '{"state":"NOT_STARTED","facts":[],"target_options":[]}',
    source_event_cursor INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_goal_runs_created
ON goal_runs(created_at DESC, goal_id DESC);

CREATE TABLE IF NOT EXISTS goal_specifications (
    goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
    revision INTEGER NOT NULL,
    original_goal TEXT NOT NULL,
    normalized_intent_json TEXT NOT NULL,
    success_criteria_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(goal_id, revision)
);

CREATE TABLE IF NOT EXISTS goal_requests (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS goal_messages (
    goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(goal_id, revision),
    UNIQUE(goal_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS goal_events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
    event_type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goal_events_goal_cursor
ON goal_events(goal_id, cursor);

CREATE TABLE IF NOT EXISTS goal_repair_attempts (
    goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
    repair_key TEXT NOT NULL,
    component TEXT NOT NULL,
    state TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT,
    detail TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(goal_id, repair_key)
);
CREATE INDEX IF NOT EXISTS idx_goal_repair_attempts_goal_created
ON goal_repair_attempts(goal_id, created_at);

CREATE TABLE IF NOT EXISTS goal_completion_assessments (
    goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
    revision INTEGER NOT NULL,
    specification_revision INTEGER NOT NULL,
    source_task_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('verified','partial','uncertain')),
    criteria_json TEXT NOT NULL,
    verified_facts_json TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(goal_id, revision),
    UNIQUE(goal_id, revision)
);

CREATE TABLE IF NOT EXISTS goal_binding_plans (
    goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
    revision INTEGER NOT NULL,
    route_kind TEXT NOT NULL,
    binding_kind TEXT NOT NULL,
    capability_ids_json TEXT NOT NULL,
    owner_kind TEXT,
    owner_binding_ref TEXT,
    profile_id TEXT,
    classification TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(goal_id, revision)
);

CREATE TABLE IF NOT EXISTS goal_notifications (
    notification_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
    source_event_sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(goal_id, source_event_sequence, kind)
);
CREATE INDEX IF NOT EXISTS idx_goal_notifications_goal_created
ON goal_notifications(goal_id, created_at);
"""


class SQLiteGoalStore:
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
                version = connection.execute(
                    "SELECT version FROM goal_schema WHERE singleton = 1"
                ).fetchone()
                if version is None:
                    raise RuntimeError("unsupported goal database schema")
                stored_version = int(version["version"])
                if stored_version == 1:
                    columns = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(goal_runs)").fetchall()
                    }
                    if "environment_json" not in columns:
                        connection.execute(
                            "ALTER TABLE goal_runs ADD COLUMN environment_json TEXT NOT NULL "
                            "DEFAULT '{\"state\":\"NOT_STARTED\",\"facts\":[],\"target_options\":[]}'"
                        )
                    connection.execute(
                        "UPDATE goal_schema SET version = 2 WHERE singleton = 1"
                    )
                    stored_version = 2
                if stored_version == 2:
                    connection.executescript("""
                        CREATE TABLE IF NOT EXISTS goal_repair_attempts (
                            goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
                            repair_key TEXT NOT NULL,
                            component TEXT NOT NULL,
                            state TEXT NOT NULL,
                            identity_json TEXT NOT NULL,
                            before_json TEXT NOT NULL,
                            after_json TEXT,
                            detail TEXT,
                            applied INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY(goal_id, repair_key)
                        );
                        CREATE INDEX IF NOT EXISTS idx_goal_repair_attempts_goal_created
                        ON goal_repair_attempts(goal_id, created_at);
                        UPDATE goal_schema SET version = 3 WHERE singleton = 1;
                    """)
                    stored_version = 3
                if stored_version == 3:
                    connection.executescript("""
                        CREATE TABLE IF NOT EXISTS goal_completion_assessments (
                            goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
                            revision INTEGER NOT NULL,
                            specification_revision INTEGER NOT NULL,
                            source_task_id TEXT NOT NULL,
                            verdict TEXT NOT NULL CHECK (verdict IN ('verified','partial','uncertain')),
                            criteria_json TEXT NOT NULL,
                            verified_facts_json TEXT NOT NULL,
                            result_summary TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY(goal_id, revision),
                            UNIQUE(goal_id, revision)
                        );
                        UPDATE goal_schema SET version = 4 WHERE singleton = 1;
                    """)
                    stored_version = 4
                if stored_version == 4:
                    connection.executescript("""
                        ALTER TABLE goal_completion_assessments
                        RENAME TO goal_completion_assessments_v4;
                        CREATE TABLE goal_completion_assessments (
                            goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
                            revision INTEGER NOT NULL,
                            specification_revision INTEGER NOT NULL,
                            source_task_id TEXT NOT NULL,
                            verdict TEXT NOT NULL CHECK (verdict IN ('verified','partial','uncertain')),
                            criteria_json TEXT NOT NULL,
                            verified_facts_json TEXT NOT NULL,
                            result_summary TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY(goal_id, revision),
                            UNIQUE(goal_id, revision)
                        );
                        INSERT INTO goal_completion_assessments(
                            goal_id, revision, specification_revision, source_task_id,
                            verdict, criteria_json, verified_facts_json, result_summary,
                            created_at
                        )
                        SELECT goal_id, revision, specification_revision, source_task_id,
                               verdict, criteria_json, verified_facts_json, result_summary,
                               created_at
                        FROM goal_completion_assessments_v4;
                        DROP TABLE goal_completion_assessments_v4;
                        UPDATE goal_schema SET version = 5 WHERE singleton = 1;
                    """)
                    stored_version = 5
                if stored_version == 5:
                    connection.executescript("""
                        CREATE TABLE IF NOT EXISTS goal_binding_plans (
                            goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
                            revision INTEGER NOT NULL,
                            route_kind TEXT NOT NULL,
                            binding_kind TEXT NOT NULL,
                            capability_ids_json TEXT NOT NULL,
                            owner_kind TEXT,
                            profile_id TEXT,
                            classification TEXT NOT NULL,
                            rationale TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY(goal_id, revision)
                        );
                        CREATE TABLE IF NOT EXISTS goal_notifications (
                            notification_id TEXT PRIMARY KEY,
                            goal_id TEXT NOT NULL REFERENCES goal_runs(goal_id),
                            source_event_sequence INTEGER NOT NULL,
                            kind TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            evidence_refs_json TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            UNIQUE(goal_id, source_event_sequence, kind)
                        );
                        CREATE INDEX IF NOT EXISTS idx_goal_notifications_goal_created
                        ON goal_notifications(goal_id, created_at);
                        UPDATE goal_schema SET version = 6 WHERE singleton = 1;
                    """)
                    stored_version = 6
                if stored_version == 6:
                    columns = {
                        str(row["name"])
                        for row in connection.execute(
                            "PRAGMA table_info(goal_binding_plans)"
                        ).fetchall()
                    }
                    if "owner_binding_ref" not in columns:
                        connection.execute(
                            "ALTER TABLE goal_binding_plans "
                            "ADD COLUMN owner_binding_ref TEXT"
                        )
                    rows = connection.execute(
                        "SELECT goal_id FROM goal_binding_plans "
                        "WHERE owner_kind='external_owner' "
                        "AND owner_binding_ref IS NULL"
                    ).fetchall()
                    for row in rows:
                        goal_id = str(row["goal_id"])
                        connection.execute(
                            "UPDATE goal_binding_plans SET owner_binding_ref=? "
                            "WHERE goal_id=? AND owner_binding_ref IS NULL",
                            (_owner_binding_ref(goal_id), goal_id),
                        )
                    connection.execute(
                        "UPDATE goal_schema SET version = 7 WHERE singleton = 1"
                    )
                    stored_version = 7
                if stored_version != 7:
                    raise RuntimeError("unsupported goal database schema")
            self._initialized = True

    def create(self, *, goal: str, idempotency_key: str) -> tuple[GoalRecord, bool]:
        self.initialize()
        digest = _digest({"goal": goal})
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT request_digest, goal_id FROM goal_requests "
                "WHERE scope = 'create' AND idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise GoalIdempotencyConflict(
                        "同一 idempotency_key 已用于不同的目标创建请求。"
                    )
                return self._get(connection, str(existing["goal_id"])), False
            goal_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO goal_runs(goal_id, original_goal, execution_status, "
                "control_state, binding_kind, binding_state, created_at, updated_at) "
                "VALUES (?, ?, 'ACCEPTED', 'AUTOMATED', 'unclassified', "
                "'PLANNED', ?, ?)",
                (goal_id, goal, now, now),
            )
            connection.execute(
                "INSERT INTO goal_specifications(goal_id, revision, original_goal, "
                "normalized_intent_json, success_criteria_json, created_at) "
                "VALUES (?, 1, ?, ?, '[]', ?)",
                (goal_id, goal, _json({"classification": "pending_u3"}), now),
            )
            connection.execute(
                "INSERT INTO goal_requests(scope, idempotency_key, request_digest, "
                "goal_id, created_at) VALUES ('create', ?, ?, ?, ?)",
                (idempotency_key, digest, goal_id, now),
            )
            self._event(
                connection,
                goal_id,
                "goal_accepted",
                {"binding_kind": "unclassified", "specification_revision": 1},
                now,
            )
            return self._get(connection, goal_id), True

    def record_binding_plan(
        self,
        goal_id: str,
        *,
        route_kind: str,
        binding_kind: str,
        capability_ids: tuple[str, ...],
        owner_kind: str | None,
        owner_binding_ref: str | None = None,
        profile_id: str | None,
        classification: str,
        rationale: str,
    ) -> CapabilityBindingPlan:
        """Freeze the selected capability route before executor side effects."""

        self.initialize()
        if not route_kind or not binding_kind or not capability_ids or not classification:
            raise ValueError("binding plan requires a route, binding and capabilities")
        if owner_kind == "external_owner" and owner_binding_ref is None:
            owner_binding_ref = _owner_binding_ref(goal_id)
        if owner_binding_ref is not None and not owner_binding_ref.strip():
            raise ValueError("owner binding reference must not be blank")
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            record = self._get(connection, goal_id)
            existing = connection.execute(
                "SELECT * FROM goal_binding_plans WHERE goal_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
            if existing is not None:
                plan = _binding_plan(existing)
                proposed = (
                    route_kind,
                    binding_kind,
                    capability_ids,
                    owner_kind,
                    owner_binding_ref,
                    profile_id,
                    classification,
                )
                current = (
                    plan.route_kind,
                    plan.binding_kind,
                    plan.capability_ids,
                    plan.owner_kind,
                    plan.owner_binding_ref,
                    plan.profile_id,
                    plan.classification,
                )
                if proposed != current:
                    raise GoalIdempotencyConflict(
                        "GoalRun 已冻结另一条能力绑定计划。"
                    )
                return plan
            if record.bound_task_id is not None:
                raise GoalIdempotencyConflict(
                    "GoalRun 已产生执行绑定，不能补写另一条能力计划。"
                )
            connection.execute(
                "INSERT INTO goal_binding_plans(goal_id, revision, route_kind, "
                "binding_kind, capability_ids_json, owner_kind, owner_binding_ref, "
                "profile_id, classification, rationale, created_at) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    goal_id,
                    route_kind,
                    binding_kind,
                    _json(capability_ids),
                    owner_kind,
                    owner_binding_ref,
                    profile_id,
                    classification,
                    rationale,
                    now,
                ),
            )
            connection.execute(
                "UPDATE goal_runs SET binding_kind = ?, binding_state = 'PLANNED', "
                "updated_at = ? WHERE goal_id = ?",
                (binding_kind, now, goal_id),
            )
            self._event(
                connection,
                goal_id,
                "goal_binding_plan_frozen",
                {
                    "revision": 1,
                    "route_kind": route_kind,
                    "binding_kind": binding_kind,
                    "capability_ids": list(capability_ids),
                    "owner_kind": owner_kind,
                    "owner_binding_ref": owner_binding_ref,
                    "profile_id": profile_id,
                    "classification": classification,
                },
                now,
            )
            row = connection.execute(
                "SELECT * FROM goal_binding_plans WHERE goal_id = ? AND revision = 1",
                (goal_id,),
            ).fetchone()
            return _binding_plan(row)

    def binding_plan(self, goal_id: str) -> CapabilityBindingPlan | None:
        self.initialize()
        with self._connection() as connection:
            self._get(connection, goal_id)
            row = connection.execute(
                "SELECT * FROM goal_binding_plans WHERE goal_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
            return _binding_plan(row) if row is not None else None

    def inspect(self, goal_id: str) -> GoalRecord:
        self.initialize()
        with self._connection() as connection:
            return self._get(connection, goal_id)

    def list(self, limit: int) -> list[GoalRecord]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT goal_id FROM goal_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._get(connection, str(row["goal_id"])) for row in rows]

    def recoverable_records(self, *, bound_only: bool = True) -> list[GoalRecord]:
        """Return the complete nonterminal recovery set without UI truncation."""

        self.initialize()
        with self._connection() as connection:
            sql = "SELECT goal_id FROM goal_runs WHERE terminal_at IS NULL"
            if bound_only:
                sql += " AND bound_task_id IS NOT NULL"
            rows = connection.execute(
                sql + " ORDER BY created_at, rowid"
            ).fetchall()
            return [self._get(connection, str(row["goal_id"])) for row in rows]

    def mark_waiting_configuration(self, goal_id: str, *, code: str, message: str) -> None:
        self._update_projection(
            goal_id,
            execution_status="WAITING_CONFIGURATION",
            binding_state="WAITING_CONFIGURATION",
            waiting_reason={"code": code, "message": message},
            event_type="goal_waiting_configuration",
        )

    def mark_waiting_external(self, goal_id: str, *, code: str, message: str) -> None:
        self._update_projection(
            goal_id,
            execution_status="WAITING_EXTERNAL",
            binding_state="WAITING_EXTERNAL",
            waiting_reason={"code": code, "message": message},
            event_type="goal_waiting_external",
        )

    def begin_preflight(self, goal_id: str) -> None:
        self.initialize()
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            record = self._get(connection, goal_id)
            if record.bound_task_id is not None:
                return
            current = json.loads(connection.execute(
                "SELECT environment_json FROM goal_runs WHERE goal_id = ?", (goal_id,)
            ).fetchone()["environment_json"])
            current["state"] = "DISCOVER"
            connection.execute(
                "UPDATE goal_runs SET execution_status = 'PREFLIGHT', binding_state = 'PREFLIGHT', "
                "waiting_reason_json = NULL, error_json = NULL, environment_json = ?, updated_at = ? "
                "WHERE goal_id = ?",
                (_json(current), now, goal_id),
            )
            self._event(connection, goal_id, "goal_preflight_started", {}, now)

    def record_preflight(
        self,
        goal_id: str,
        *,
        state: str,
        projection: dict[str, Any],
        waiting_reason: dict[str, Any] | None,
    ) -> None:
        self.initialize()
        now = _now()
        execution = "PREFLIGHT" if state == "READY" else state
        binding_state = "PREFLIGHT_READY" if state == "READY" else state
        with self._lock, self._connection(write=True) as connection:
            self._get(connection, goal_id)
            connection.execute(
                "UPDATE goal_runs SET execution_status = ?, binding_state = ?, "
                "environment_json = ?, waiting_reason_json = ?, error_json = NULL, "
                "updated_at = ? WHERE goal_id = ?",
                (
                    execution,
                    binding_state,
                    _json(projection),
                    _json(waiting_reason) if waiting_reason else None,
                    now,
                    goal_id,
                ),
            )
            self._event(
                connection,
                goal_id,
                "goal_preflight_assessed",
                {"state": state, "waiting_code": (waiting_reason or {}).get("code")},
                now,
            )

    def mark_binding_failed(self, goal_id: str, *, code: str, message: str) -> None:
        self._update_projection(
            goal_id,
            execution_status="FAILED",
            binding_state="FAILED",
            error={"code": code, "message": message},
            terminal=True,
            event_type="goal_binding_failed",
        )

    def record_experience_unavailable(
        self, goal_id: str, *, error_type: str
    ) -> None:
        """Keep an additive learning failure visible without rewriting execution."""

        self.initialize()
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            self._get(connection, goal_id)
            self._event(
                connection,
                goal_id,
                "goal_experience_unavailable",
                {"error_type": error_type},
                now,
            )

    def bind_task(
        self,
        goal_id: str,
        task_id: str,
        target_id: str,
        *,
        binding_kind: str = "mobile_task_compat",
    ) -> None:
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            record = self._get(connection, goal_id)
            if record.bound_task_id is not None and record.bound_task_id != task_id:
                raise GoalIdempotencyConflict("GoalRun 已绑定到另一兼容任务。")
            connection.execute(
                "UPDATE goal_runs SET bound_task_id = ?, target_id = ?, binding_kind = ?, "
                "binding_state = 'BOUND', execution_status = 'ACCEPTED', "
                "waiting_reason_json = NULL, error_json = NULL, updated_at = ? "
                "WHERE goal_id = ?",
                (task_id, target_id, binding_kind, now, goal_id),
            )
            if record.bound_task_id is None:
                event_type = (
                    "kernel_task_bound"
                    if binding_kind in {"runtime_kernel", "runtime_kernel_canary"}
                    else "application_instance_bound"
                    if binding_kind in {
                        "application_runtime",
                        "long_lived_mobile_composition",
                    }
                    else "language_task_bound"
                    if binding_kind == "local_language"
                    else "compatibility_task_bound"
                )
                self._event(
                    connection,
                    goal_id,
                    event_type,
                    {"binding_kind": binding_kind, "task_id": task_id,
                     "target_id": target_id, "completion_gate": "pending_u3"},
                    now,
                )

    def complete_language_result(self, goal_id: str, *, result_summary: str) -> GoalRecord:
        self.initialize()
        summary = result_summary.strip()
        if not summary:
            raise ValueError("language result must not be blank")
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            record = self._get(connection, goal_id)
            if record.binding_kind != "local_language" or record.bound_task_id is None:
                raise GoalIdempotencyConflict("GoalRun 尚未绑定本地语言能力。")
            if record.execution_status == "COMPLETED":
                return record
            connection.execute(
                "UPDATE goal_runs SET execution_status = 'COMPLETED', "
                "binding_state = 'BOUND', result_summary = ?, waiting_reason_json = NULL, "
                "error_json = NULL, updated_at = ?, terminal_at = ? WHERE goal_id = ?",
                (summary, now, now, goal_id),
            )
            self._event(
                connection,
                goal_id,
                "goal_language_result_completed",
                {"binding_kind": "local_language"},
                now,
            )
            return self._get(connection, goal_id)

    def add_message(self, goal_id: str, *, content: str, idempotency_key: str) -> tuple[int, bool]:
        self.initialize()
        digest = _digest({"goal_id": goal_id, "content": content})
        scope = f"message:{goal_id}"
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            self._get(connection, goal_id)
            existing = connection.execute(
                "SELECT request_digest FROM goal_requests WHERE scope = ? AND idempotency_key = ?",
                (scope, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise GoalIdempotencyConflict(
                        "同一 idempotency_key 已用于不同的目标消息。"
                    )
                row = connection.execute(
                    "SELECT revision FROM goal_messages WHERE goal_id = ? AND idempotency_key = ?",
                    (goal_id, idempotency_key),
                ).fetchone()
                return int(row["revision"]), False
            revision = int(connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                "FROM goal_messages WHERE goal_id = ?", (goal_id,)
            ).fetchone()["revision"])
            connection.execute(
                "INSERT INTO goal_messages(goal_id, revision, content, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (goal_id, revision, content, idempotency_key, now),
            )
            connection.execute(
                "INSERT INTO goal_requests(scope, idempotency_key, request_digest, goal_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, idempotency_key, digest, goal_id, now),
            )
            self._event(connection, goal_id, "goal_message_accepted", {"revision": revision}, now)
            return revision, True

    def record_control(self, goal_id: str, *, action: str, idempotency_key: str) -> bool:
        self.initialize()
        digest = _digest({"goal_id": goal_id, "action": action})
        scope = f"control:{goal_id}"
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            self._get(connection, goal_id)
            existing = connection.execute(
                "SELECT request_digest FROM goal_requests WHERE scope = ? AND idempotency_key = ?",
                (scope, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise GoalIdempotencyConflict(
                        "同一 idempotency_key 已用于不同的目标控制。"
                    )
                return False
            connection.execute(
                "INSERT INTO goal_requests(scope, idempotency_key, request_digest, goal_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, idempotency_key, digest, goal_id, now),
            )
            if action == "stop":
                # Persist the supervisor-facing stop intent in the same
                # transaction as idempotency admission.  Launcher recovery can
                # therefore fence the ApplicationRuntime before any worker is
                # allowed to resume, even if the process died before the
                # downstream Stop command was delivered.
                connection.execute(
                    "UPDATE goal_runs SET control_state = 'STOP_REQUESTED', "
                    "updated_at = ? WHERE goal_id = ?",
                    (now, goal_id),
                )
            self._event(
                connection,
                goal_id,
                {
                    "pause": "goal_pause_requested",
                    "resume": "goal_resume_requested",
                    "stop": "goal_stop_requested",
                    "takeover": "goal_takeover_requested",
                }.get(action, "goal_control_requested"),
                {"action": action},
                now,
            )
            return True

    def settle_takeover(self, goal_id: str) -> None:
        self.initialize()
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            self._get(connection, goal_id)
            connection.execute(
                "UPDATE goal_runs SET control_state = 'TAKEOVER', updated_at = ? "
                "WHERE goal_id = ?",
                (now, goal_id),
            )
            self._event(connection, goal_id, "goal_takeover_settled", {}, now)

    def cancel_unbound(self, goal_id: str) -> GoalRecord:
        """Settle user stop when no executor/owner was ever bound."""

        self.initialize()
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            record = self._get(connection, goal_id)
            if record.bound_task_id is not None:
                raise GoalIdempotencyConflict(
                    "GoalRun 已绑定执行 owner，必须由 owner 确认停止。"
                )
            if record.execution_status == "CANCELLED":
                return record
            if record.execution_status in {
                "COMPLETED", "PARTIAL", "FAILED", "UNCERTAIN"
            }:
                raise GoalIdempotencyConflict("终态 GoalRun 不能改写为取消。")
            connection.execute(
                "UPDATE goal_runs SET execution_status = 'CANCELLED', "
                "control_state = 'AUTOMATED', binding_state = 'CANCELLED', "
                "waiting_reason_json = NULL, updated_at = ?, terminal_at = ? "
                "WHERE goal_id = ?",
                (now, now, goal_id),
            )
            self._event(
                connection,
                goal_id,
                "goal_cancelled_without_binding",
                {"physical_binding": False},
                now,
            )
            return self._get(connection, goal_id)

    def sync_mobile_projection(self, goal_id: str, state: Any) -> GoalRecord:
        status = str(_value(state, "status", "failed"))
        execution, control, terminal = {
            "queued": ("ACCEPTED", "AUTOMATED", False),
            "planning": ("PLANNING", "AUTOMATED", False),
            "running": ("RUNNING", "AUTOMATED", False),
            "paused": ("RUNNING", "PAUSED", False),
            "stopping": ("RUNNING", "STOP_REQUESTED", False),
            "completed": ("CANDIDATE_COMPLETE", "AUTOMATED", False),
            "failed": ("FAILED", "AUTOMATED", True),
            "stopped": ("CANCELLED", "AUTOMATED", True),
            "uncertain": ("UNCERTAIN", "AUTOMATED", True),
        }.get(status, ("FAILED", "AUTOMATED", True))
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            record = self._get(connection, goal_id)
            if record.execution_status == "COMPLETED":
                return record
            source_cursor = int(connection.execute(
                "SELECT source_event_cursor FROM goal_runs WHERE goal_id = ?", (goal_id,)
            ).fetchone()["source_event_cursor"])
            for source_event in _value(state, "events", ()):
                sequence = int(_value(source_event, "sequence", 0))
                if sequence <= source_cursor:
                    continue
                self._event(
                    connection,
                    goal_id,
                    "compatibility_event",
                    {"source": record.binding_kind, "source_sequence": sequence,
                     "source_event_type": str(_value(source_event, "event_type", "unknown"))},
                    str(_value(source_event, "created_at", now)),
                )
                source_cursor = sequence
            changed = record.execution_status != execution or record.control_state != control
            detail = _value(state, "detail")
            error_code = _value(state, "error_code")
            result_summary = (
                "兼容执行器已完成其生成的计划；原始目标尚待独立完成验证。"
                if execution == "CANDIDATE_COMPLETE" else detail
            )
            error = (
                {"code": str(error_code or f"mobile_task_{status}"), "message": str(detail or "兼容任务失败。")}
                if terminal and execution in {"FAILED", "UNCERTAIN"} else None
            )
            connection.execute(
                "UPDATE goal_runs SET execution_status = ?, control_state = ?, "
                "result_summary = ?, error_json = ?, source_event_cursor = ?, "
                "updated_at = ?, terminal_at = ? WHERE goal_id = ?",
                (execution, control, result_summary, _json(error) if error else None,
                 source_cursor, now, now if terminal else None, goal_id),
            )
            if changed:
                self._event(
                    connection, goal_id, "goal_projection_changed",
                    {"execution_status": execution, "control_state": control,
                     "source_status": status}, now,
                )
            return self._get(connection, goal_id)

    def source_event_cursor(self, goal_id: str) -> int:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT source_event_cursor FROM goal_runs WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            if row is None:
                raise GoalNotFound("未找到该 GoalRun。")
            return int(row["source_event_cursor"])

    def sync_application_projection(self, goal_id: str, state: Any) -> GoalRecord:
        """Project one long-lived ApplicationRuntime instance into GoalRun truth."""

        status = str(_value(state, "status", "failed"))
        execution, control, terminal = {
            "queued": ("ACCEPTED", "AUTOMATED", False),
            "running": ("RUNNING", "AUTOMATED", False),
            "waiting": ("WAITING_EXTERNAL", "AUTOMATED", False),
            "paused": ("RUNNING", "PAUSED", False),
            "stopping": ("RUNNING", "STOP_REQUESTED", False),
            "stopped": ("CANCELLED", "AUTOMATED", True),
            # A continuous long-lived binding may not silently turn an owner
            # cycle completion into the user's terminal external outcome.
            "completed": ("FAILED", "AUTOMATED", True),
            "failed": ("FAILED", "AUTOMATED", True),
        }.get(status, ("FAILED", "AUTOMATED", True))
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            record = self._get(connection, goal_id)
            if record.binding_kind not in {
                "application_runtime",
                "long_lived_mobile_composition",
            }:
                raise GoalIdempotencyConflict("GoalRun 不是长期运行绑定。")
            source_cursor = int(connection.execute(
                "SELECT source_event_cursor FROM goal_runs WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()["source_event_cursor"])
            for source_event in _value(state, "events", ()):
                sequence = int(_value(source_event, "sequence", 0))
                if sequence <= source_cursor:
                    continue
                source_type = str(_value(source_event, "event_type", "unknown"))
                source_data = _value(source_event, "data", {})
                self._event(
                    connection,
                    goal_id,
                    "application_event",
                    {
                        "source": "application_runtime",
                        "source_sequence": sequence,
                        "source_event_type": source_type,
                    },
                    str(_value(source_event, "created_at", now)),
                )
                if source_type == "candidate_notified":
                    data = source_data if isinstance(source_data, dict) else {}
                    summary = str(data.get("summary") or "已发现一个候选里程碑；长期目标继续运行。")[:500]
                    evidence_refs = tuple(
                        str(item)[:256]
                        for item in data.get("evidence_refs", ())
                        if isinstance(item, str) and item.strip()
                    )[:16]
                    notification_id = str(
                        data.get("notification_id")
                        or uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"ai-game:{goal_id}:candidate:{sequence}",
                        )
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO goal_notifications(notification_id, goal_id, "
                        "source_event_sequence, kind, summary, evidence_refs_json, created_at) "
                        "VALUES (?, ?, ?, 'candidate', ?, ?, ?)",
                        (
                            notification_id,
                            goal_id,
                            sequence,
                            summary,
                            _json(evidence_refs),
                            str(_value(source_event, "created_at", now)),
                        ),
                    )
                    self._event(
                        connection,
                        goal_id,
                        "goal_candidate_notified",
                        {
                            "notification_id": notification_id,
                            "source_event_sequence": sequence,
                            "continues": True,
                        },
                        str(_value(source_event, "created_at", now)),
                    )
                source_cursor = sequence
            if status == "paused" and record.control_state == "TAKEOVER":
                control = "TAKEOVER"
            waiting_reason = (
                {
                    "code": "TIME_OR_INBOUND_EVENT",
                    "message": "长期目标正在等待下一次定时唤醒或授权入站事件。",
                }
                if status == "waiting"
                else None
            )
            detail = _value(state, "detail")
            error_code = _value(state, "error_code")
            if status == "completed":
                error_code = "long_lived_runtime_completed_unexpectedly"
                detail = "长期运行能力提前结束，未把外部候选或一次循环伪装成用户目标完成。"
            error = (
                {
                    "code": str(error_code or "application_runtime_failed"),
                    "message": str(detail or "长期运行能力失败。"),
                }
                if execution == "FAILED"
                else None
            )
            changed = (
                record.execution_status != execution
                or record.control_state != control
            )
            connection.execute(
                "UPDATE goal_runs SET execution_status = ?, control_state = ?, "
                "waiting_reason_json = ?, error_json = ?, result_summary = ?, "
                "source_event_cursor = ?, updated_at = ?, terminal_at = ? "
                "WHERE goal_id = ?",
                (
                    execution,
                    control,
                    _json(waiting_reason) if waiting_reason else None,
                    _json(error) if error else None,
                    str(detail) if detail else None,
                    source_cursor,
                    now,
                    (record.terminal_at or now) if terminal else record.terminal_at,
                    goal_id,
                ),
            )
            if changed:
                self._event(
                    connection,
                    goal_id,
                    "goal_projection_changed",
                    {
                        "execution_status": execution,
                        "control_state": control,
                        "source_status": status,
                    },
                    now,
                )
            return self._get(connection, goal_id)

    def notifications(self, goal_id: str) -> list[GoalNotification]:
        self.initialize()
        with self._connection() as connection:
            self._get(connection, goal_id)
            rows = connection.execute(
                "SELECT * FROM goal_notifications WHERE goal_id = ? "
                "ORDER BY created_at, source_event_sequence",
                (goal_id,),
            ).fetchall()
            return [_notification(row) for row in rows]

    def events(self, goal_id: str, *, after: int, limit: int) -> list[StoredEvent]:
        self.initialize()
        with self._connection() as connection:
            self._get(connection, goal_id)
            rows = connection.execute(
                "SELECT * FROM goal_events WHERE goal_id = ? AND cursor > ? "
                "ORDER BY cursor LIMIT ?", (goal_id, after, limit)
            ).fetchall()
            return [StoredEvent(int(row["cursor"]), goal_id, str(row["event_type"]),
                                json.loads(row["data_json"]), str(row["created_at"]))
                    for row in rows]

    def specification(self, goal_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM goal_specifications WHERE goal_id = ? ORDER BY revision DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
            if row is None:
                raise GoalNotFound("未找到该 GoalRun。")
            return {"revision": int(row["revision"]), "original_goal": str(row["original_goal"]),
                    "normalized_intent": json.loads(row["normalized_intent_json"]),
                    "success_criteria": json.loads(row["success_criteria_json"])}

    def record_specification(
        self, goal_id: str, specification: GoalSpecificationDraft
    ) -> dict[str, Any]:
        """Freeze one structured interpretation before compatibility execution."""
        self.initialize()
        now = _now()
        criteria = [
            {
                "id": item.criterion_id,
                "description": item.description,
                "evidence_requirement": item.evidence_requirement,
                "source_quote": item.source_quote,
            }
            for item in specification.success_criteria
        ]
        with self._lock, self._connection(write=True) as connection:
            record = self._get(connection, goal_id)
            latest = connection.execute(
                "SELECT * FROM goal_specifications WHERE goal_id = ? "
                "ORDER BY revision DESC LIMIT 1", (goal_id,),
            ).fetchone()
            if latest is not None and json.loads(latest["success_criteria_json"]):
                return {
                    "revision": int(latest["revision"]),
                    "original_goal": str(latest["original_goal"]),
                    "normalized_intent": json.loads(latest["normalized_intent_json"]),
                    "success_criteria": json.loads(latest["success_criteria_json"]),
                }
            revision = int(latest["revision"]) + 1 if latest is not None else 1
            connection.execute(
                "INSERT INTO goal_specifications(goal_id, revision, original_goal, "
                "normalized_intent_json, success_criteria_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (goal_id, revision, record.original_goal,
                 _json(specification.normalized_intent), _json(criteria), now),
            )
            self._event(connection, goal_id, "goal_specification_frozen", {
                "specification_revision": revision,
                "criterion_ids": [item["id"] for item in criteria],
            }, now)
        return self.specification(goal_id)

    def completion(self, goal_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connection() as connection:
            self._get(connection, goal_id)
            row = connection.execute(
                "SELECT * FROM goal_completion_assessments WHERE goal_id = ? "
                "ORDER BY revision DESC LIMIT 1", (goal_id,),
            ).fetchone()
            return _completion_payload(row) if row is not None else None

    def completions(self, goal_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            self._get(connection, goal_id)
            rows = connection.execute(
                "SELECT * FROM goal_completion_assessments WHERE goal_id = ? "
                "ORDER BY revision", (goal_id,),
            ).fetchall()
            return [_completion_payload(row) for row in rows]

    def record_completion(
        self,
        goal_id: str,
        *,
        specification_revision: int,
        source_task_id: str,
        assessment: GoalCompletionAssessment,
    ) -> dict[str, Any]:
        """Persist one independent verdict and terminalize only a verified one."""
        self.initialize()
        now = _now()
        criteria = [
            {
                "criterion_id": item.criterion_id,
                "satisfied": item.satisfied,
                "subgoal_indices": list(item.subgoal_indices),
                "attempt_sequences": list(item.attempt_sequences),
                "evidence": item.evidence,
            }
            for item in assessment.criteria
        ]
        with self._lock, self._connection(write=True) as connection:
            self._get(connection, goal_id)
            revision = int(connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                "FROM goal_completion_assessments WHERE goal_id = ?", (goal_id,),
            ).fetchone()["revision"])
            connection.execute(
                "INSERT INTO goal_completion_assessments(goal_id, revision, "
                "specification_revision, source_task_id, verdict, criteria_json, "
                "verified_facts_json, result_summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (goal_id, revision, specification_revision, source_task_id,
                 assessment.verdict, _json(criteria), _json(assessment.verified_facts),
                 assessment.result_summary, now),
            )
            if assessment.verdict == "verified":
                connection.execute(
                    "UPDATE goal_runs SET execution_status = 'COMPLETED', "
                    "result_summary = ?, updated_at = ?, terminal_at = ? WHERE goal_id = ?",
                    (assessment.result_summary, now, now, goal_id),
                )
            self._event(connection, goal_id, "goal_completion_assessed", {
                "assessment_revision": revision,
                "specification_revision": specification_revision,
                "verdict": assessment.verdict,
            }, now)
            row = connection.execute(
                "SELECT * FROM goal_completion_assessments WHERE goal_id = ? AND revision = ?",
                (goal_id, revision),
            ).fetchone()
            return _completion_payload(row)

    def environment(self, goal_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT environment_json FROM goal_runs WHERE goal_id = ?", (goal_id,)
            ).fetchone()
            if row is None:
                raise GoalNotFound("未找到该 GoalRun。")
            return json.loads(row["environment_json"])

    def begin_repair(
        self, goal_id: str, *, repair_key: str, component: str,
        identity: dict[str, Any], before: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Durably claim one repair key before any external mutation.

        Replays return the existing attempt and never claim the mutation again.
        An attempt left APPLYING after a crash is deliberately not replayed.
        """
        self.initialize()
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            self._get(connection, goal_id)
            existing = connection.execute(
                "SELECT * FROM goal_repair_attempts WHERE goal_id = ? AND repair_key = ?",
                (goal_id, repair_key),
            ).fetchone()
            if existing is not None:
                return _repair_payload(existing), False
            connection.execute(
                "INSERT INTO goal_repair_attempts(goal_id, repair_key, component, state, "
                "identity_json, before_json, created_at, updated_at) "
                "VALUES (?, ?, ?, 'APPLYING', ?, ?, ?, ?)",
                (goal_id, repair_key, component, _json(identity), _json(before), now, now),
            )
            self._event(connection, goal_id, "goal_repair_started", {
                "repair_key": repair_key, "component": component,
            }, now)
            row = connection.execute(
                "SELECT * FROM goal_repair_attempts WHERE goal_id = ? AND repair_key = ?",
                (goal_id, repair_key),
            ).fetchone()
            return _repair_payload(row), True

    def finish_repair(
        self, goal_id: str, *, repair_key: str, state: str,
        after: dict[str, Any], detail: str, applied: bool,
    ) -> dict[str, Any]:
        if state not in {"VERIFIED", "FAILED", "SKIPPED_IDENTITY"}:
            raise ValueError(f"invalid terminal repair state: {state}")
        self.initialize()
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE goal_repair_attempts SET state = ?, after_json = ?, detail = ?, "
                "applied = ?, updated_at = ? WHERE goal_id = ? AND repair_key = ? "
                "AND state = 'APPLYING'",
                (state, _json(after), detail, int(applied), now, goal_id, repair_key),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT * FROM goal_repair_attempts WHERE goal_id = ? AND repair_key = ?",
                    (goal_id, repair_key),
                ).fetchone()
                if row is None:
                    raise GoalNotFound(f"repair attempt not found: {goal_id}/{repair_key}")
                return _repair_payload(row)
            self._event(connection, goal_id, "goal_repair_finished", {
                "repair_key": repair_key, "state": state, "applied": applied,
            }, now)
            row = connection.execute(
                "SELECT * FROM goal_repair_attempts WHERE goal_id = ? AND repair_key = ?",
                (goal_id, repair_key),
            ).fetchone()
            return _repair_payload(row)

    def repairs(self, goal_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            self._get(connection, goal_id)
            rows = connection.execute(
                "SELECT * FROM goal_repair_attempts WHERE goal_id = ? "
                "ORDER BY created_at, repair_key",
                (goal_id,),
            ).fetchall()
            return [_repair_payload(row) for row in rows]

    def _update_projection(self, goal_id: str, *, execution_status: str,
                           binding_state: str, event_type: str,
                           waiting_reason: dict[str, Any] | None = None,
                           error: dict[str, Any] | None = None,
                           terminal: bool = False) -> None:
        self.initialize()
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            self._get(connection, goal_id)
            connection.execute(
                "UPDATE goal_runs SET execution_status = ?, binding_state = ?, "
                "waiting_reason_json = ?, error_json = ?, updated_at = ?, terminal_at = ? "
                "WHERE goal_id = ?",
                (execution_status, binding_state,
                 _json(waiting_reason) if waiting_reason else None,
                 _json(error) if error else None, now, now if terminal else None, goal_id),
            )
            self._event(connection, goal_id, event_type, waiting_reason or error or {}, now)

    def _get(self, connection: sqlite3.Connection, goal_id: str) -> GoalRecord:
        row = connection.execute("SELECT * FROM goal_runs WHERE goal_id = ?", (goal_id,)).fetchone()
        if row is None:
            raise GoalNotFound("未找到该 GoalRun。")
        return GoalRecord(
            id=str(row["goal_id"]), original_goal=str(row["original_goal"]),
            execution_status=str(row["execution_status"]), control_state=str(row["control_state"]),
            resume_execution_status=row["resume_execution_status"], active_stage=row["active_stage"],
            binding_kind=str(row["binding_kind"]), binding_state=str(row["binding_state"]),
            bound_task_id=row["bound_task_id"], target_id=row["target_id"],
            waiting_reason=json.loads(row["waiting_reason_json"]) if row["waiting_reason_json"] else None,
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            result_summary=row["result_summary"], created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]), terminal_at=row["terminal_at"],
        )

    @staticmethod
    def _event(connection: sqlite3.Connection, goal_id: str, event_type: str,
               data: dict[str, Any], created_at: str) -> None:
        connection.execute(
            "INSERT INTO goal_events(goal_id, event_type, data_json, created_at) VALUES (?, ?, ?, ?)",
            (goal_id, event_type, _json(data), created_at),
        )

    def _connection(self, *, write: bool = False):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        return _ConnectionContext(connection)


class _ConnectionContext:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
    def __enter__(self) -> sqlite3.Connection:
        return self.connection
    def __exit__(self, kind, value, traceback) -> None:
        if kind is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _owner_binding_ref(goal_id: str) -> str:
    """Stable opaque owner-binding handle for a frozen external-owner plan."""

    return "owner-binding:v1:" + hashlib.sha256(
        f"ai-game:goal-owner-binding:{goal_id}".encode("utf-8")
    ).hexdigest()


def _repair_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "repair_key": str(row["repair_key"]),
        "component": str(row["component"]),
        "state": str(row["state"]),
        "identity": json.loads(row["identity_json"]),
        "before": json.loads(row["before_json"]),
        "after": json.loads(row["after_json"]) if row["after_json"] else None,
        "detail": str(row["detail"]) if row["detail"] is not None else None,
        "applied": bool(row["applied"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _completion_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "revision": int(row["revision"]),
        "specification_revision": int(row["specification_revision"]),
        "source_task_id": str(row["source_task_id"]),
        "verdict": str(row["verdict"]),
        "criteria": json.loads(row["criteria_json"]),
        "verified_facts": json.loads(row["verified_facts_json"]),
        "result_summary": str(row["result_summary"]),
        "created_at": str(row["created_at"]),
    }


def _binding_plan(row: sqlite3.Row) -> CapabilityBindingPlan:
    return CapabilityBindingPlan(
        goal_id=str(row["goal_id"]),
        revision=int(row["revision"]),
        route_kind=str(row["route_kind"]),
        binding_kind=str(row["binding_kind"]),
        capability_ids=tuple(json.loads(row["capability_ids_json"])),
        owner_kind=str(row["owner_kind"]) if row["owner_kind"] is not None else None,
        owner_binding_ref=(
            str(row["owner_binding_ref"])
            if row["owner_binding_ref"] is not None
            else None
        ),
        profile_id=str(row["profile_id"]) if row["profile_id"] is not None else None,
        classification=str(row["classification"]),
        rationale=str(row["rationale"]),
        created_at=str(row["created_at"]),
    )


def _notification(row: sqlite3.Row) -> GoalNotification:
    return GoalNotification(
        notification_id=str(row["notification_id"]),
        goal_id=str(row["goal_id"]),
        source_event_sequence=int(row["source_event_sequence"]),
        kind=str(row["kind"]),
        summary=str(row["summary"]),
        evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
        created_at=str(row["created_at"]),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
