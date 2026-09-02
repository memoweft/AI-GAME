"""SQLite persistence for the AgentSession and versioned GoalGraph.

The store deliberately owns only AgentSession facts.  A GoalRun remains owned
by ``goal_runtime``; the durable outbox bridges the two independent SQLite
transactions without attempting an unsafe distributed transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

from .domain import (
    AttentionDecision,
    AttentionDecisionDraft,
    AttentionDecisionOutcome,
    AttentionDispatch,
    AttentionDispatchStatus,
    AttentionSelectorKind,
    AgentSession,
    Continuation,
    ContinuationCheckpointKind,
    ContinuationDraft,
    ContinuationYieldReason,
    DirectiveKind,
    DurableOutboxIntent,
    EventHandlingStatus,
    GoalBindingStatus,
    GoalCoverage,
    GoalCoverageDraft,
    GoalCoverageKind,
    GoalCriterion,
    GoalCriterionDraft,
    GoalCriterionStatus,
    GoalEdge,
    GoalEdgeDraft,
    GoalEdgeKind,
    GoalGraphRevision,
    GoalGraphRevisionDraft,
    GoalNode,
    GoalNodeDraft,
    GoalNodeStatus,
    GoalEligibility,
    GoalEligibilityDraft,
    GoalEligibilityStatus,
    GoalSchedulingClass,
    OutboxIntentStatus,
    OutboxIntentType,
    SCHEMA_REVISION,
    SessionControlMode,
    SessionEvent,
    SessionEventType,
    SessionGoalBinding,
    SessionIdempotencyConflict,
    SessionKind,
    SessionNotFound,
    SessionStateConflict,
    SessionStatus,
    Task,
    TaskControl,
    TaskControlAction,
    TaskControlConflict,
    TaskDispatchConflict,
    TaskIntegrityState,
    TaskReason,
    TaskRecordStatus,
    TaskRevision,
    TaskRevisionConflict,
    TaskRevisionKind,
    TaskStatus,
    TaskSubtask,
    RunnerDispatchCommit,
    RunnerDispatchRequest,
    SliceBudgetDraft,
    UserDirective,
    WakeCondition,
    WakeConditionDraft,
    WakeConditionKind,
    WakeConditionStatus,
    PreemptionPolicy,
    PreemptionRequest,
    PreemptionRequestStatus,
    RawObservation,
    SessionControlTransition,
    can_transition_session,
    can_transition_task,
    utc_now,
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runtime_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO agent_runtime_schema(singleton, revision) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    client_request_id TEXT NOT NULL UNIQUE,
    original_instruction TEXT NOT NULL,
    authority_revision INTEGER NOT NULL,
    session_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    control_mode TEXT NOT NULL,
    device_binding_id TEXT,
    active_goal_id TEXT,
    active_slice_id TEXT,
    event_cursor INTEGER NOT NULL DEFAULT 0,
    agenda_revision INTEGER NOT NULL DEFAULT 0,
    calendar_started_at TEXT NOT NULL,
    calendar_window_end TEXT,
    terminal_condition TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stopped_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_created
ON agent_sessions(created_at DESC, session_id DESC);

CREATE TABLE IF NOT EXISTS session_requests (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, request_key)
);

CREATE TABLE IF NOT EXISTS user_directives (
    directive_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    directive_kind TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, revision),
    UNIQUE(session_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS idx_user_directives_session_revision
ON user_directives(session_id, revision);

CREATE TABLE IF NOT EXISTS goal_nodes (
    goal_node_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    title TEXT NOT NULL,
    source_directive_id TEXT NOT NULL REFERENCES user_directives(directive_id),
    status TEXT NOT NULL,
    bound_goal_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goal_nodes_session_created
ON goal_nodes(session_id, created_at, goal_node_id);

CREATE TABLE IF NOT EXISTS session_goal_bindings (
    binding_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    goal_node_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id),
    goal_run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(goal_node_id, goal_run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_binding_per_goal_node
ON session_goal_bindings(goal_node_id)
WHERE status IN ('REQUESTED', 'BOUND');
CREATE INDEX IF NOT EXISTS idx_session_goal_bindings_session
ON session_goal_bindings(session_id, goal_node_id);

CREATE TABLE IF NOT EXISTS session_events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    event_type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    handling_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    handled_at TEXT,
    UNIQUE(session_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_session_events_session_cursor
ON session_events(session_id, cursor);

CREATE TABLE IF NOT EXISTS agent_outbox (
    intent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    goal_node_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id),
    intent_type TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    goal_text TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    last_error TEXT,
    UNIQUE(goal_node_id, intent_type)
);
CREATE INDEX IF NOT EXISTS idx_agent_outbox_pending
ON agent_outbox(status, created_at, intent_id);
"""


class TaskExpectedStatusConflict(TaskControlConflict):
    """A compare-and-swap Task mutation observed a newer canonical status."""


class SQLiteAgentRuntimeStore:
    """Schema-v3 AgentSession store with GoalGraph and attention facts."""

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
                self._migrate_schema(connection)
                row = connection.execute(
                    "SELECT revision FROM agent_runtime_schema WHERE singleton = 1"
                ).fetchone()
                if row is None or int(row["revision"]) != SCHEMA_REVISION:
                    raise RuntimeError("unsupported agent-runtime database schema")
            self._initialized = True

    def create_session(
        self, *, instruction: str, client_request_id: str, prepare_compatibility_goal: bool = True,
        owner_principal_id: str = "local-default", controller_id: str = "local-installation",
        origin: Mapping[str, Any] | None = None,
    ) -> tuple[AgentSession, bool]:
        self.initialize()
        instruction = instruction.strip()
        origin = _normalize_task_origin(origin or {})
        if not owner_principal_id.strip() or not controller_id.strip():
            raise ValueError("task owner_principal_id and controller_id are required")
        digest = _digest({"instruction": instruction, "owner_principal_id": owner_principal_id,
                          "controller_id": controller_id, "origin": origin})
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT request_digest, session_id FROM session_requests "
                "WHERE scope='create' AND request_key=?",
                (client_request_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise SessionIdempotencyConflict(
                        "同一 client_request_id 已用于不同的 Session 创建请求。"
                    )
                return self._session(connection, str(existing["session_id"])), False

            session_id = str(uuid.uuid4())
            directive_id = str(uuid.uuid4())
            goal_node_id = str(uuid.uuid4()) if prepare_compatibility_goal else None
            outbox_id = str(uuid.uuid4()) if prepare_compatibility_goal else None
            goal_key = _goal_create_key(session_id, goal_node_id) if goal_node_id else None
            connection.execute(
                "INSERT INTO agent_sessions("
                "session_id, client_request_id, original_instruction, authority_revision, "
                "session_kind, status, control_mode, calendar_started_at, created_at, updated_at, "
                "task_owner_principal_id, task_controller_id, task_origin_json"
                ") VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, client_request_id, instruction, SessionKind.OPEN_ENDED.value,
                    SessionStatus.PLANNING.value, SessionControlMode.AGENT_ACTIVE.value,
                    now, now, now, owner_principal_id, controller_id, _json(origin),
                ),
            )
            connection.execute(
                "INSERT INTO session_requests(scope, request_key, request_digest, session_id, created_at) "
                "VALUES ('create', ?, ?, ?, ?)",
                (client_request_id, digest, session_id, now),
            )
            connection.execute(
                "INSERT INTO user_directives("
                "directive_id, session_id, revision, content, directive_kind, source_message_id, created_at"
                ") VALUES (?, ?, 1, ?, ?, ?, ?)",
                (directive_id, session_id, instruction, DirectiveKind.ORIGINAL.value,
                 f"create:{client_request_id}", now),
            )
            if prepare_compatibility_goal:
                connection.execute(
                    "INSERT INTO goal_graph_revisions("
                    "graph_revision_id, session_id, revision, authority_revision, source_directive_id, reason, created_at"
                    ") VALUES (?, ?, 1, 1, ?, ?, ?)",
                    (str(uuid.uuid4()), session_id, directive_id, "R1 compatibility graph", now),
                )
                connection.execute(
                    "INSERT INTO goal_nodes("
                    "goal_node_id, session_id, title, source_directive_id, graph_revision, original_fragment, status, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (goal_node_id, session_id, instruction, directive_id,
                     instruction, GoalNodeStatus.PLANNED.value, now, now),
                )
                connection.execute(
                    "INSERT INTO agent_outbox("
                    "intent_id, session_id, goal_node_id, intent_type, status, idempotency_key, "
                    "goal_text, graph_revision, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (outbox_id, session_id, goal_node_id, OutboxIntentType.CREATE_GOAL_RUN.value,
                     OutboxIntentStatus.PENDING.value, goal_key, instruction, now, now),
                )
            self._append_event(connection, session_id, SessionEventType.SESSION_CREATED,
                               {"original_instruction": instruction}, f"create:{client_request_id}:session", now)
            self._append_task_event(
                connection, session_id, SessionEventType.TASK_CREATED,
                {"status": TaskStatus.SCHEDULED.value, "reason_code": "accepted", "revision": 1},
                f"create:{client_request_id}:task", now,
            )
            self._append_event(connection, session_id, SessionEventType.DIRECTIVE_RECORDED,
                               {"directive_id": directive_id, "revision": 1, "kind": "original"},
                               f"create:{client_request_id}:directive", now)
            if prepare_compatibility_goal:
                self._append_event(connection, session_id, SessionEventType.GOAL_NODE_CREATED,
                                   {"goal_node_id": goal_node_id}, f"create:{client_request_id}:goal-node", now)
                self._append_event(connection, session_id, SessionEventType.GOAL_ACTIVATION_REQUESTED,
                                   {"goal_node_id": goal_node_id, "outbox_intent_id": outbox_id},
                                   f"create:{client_request_id}:goal-activation", now)
            return self._session(connection, session_id), True

    def create_unplanned_session(
        self, *, instruction: str, client_request_id: str,
        owner_principal_id: str = "local-default", controller_id: str = "local-installation",
        origin: Mapping[str, Any] | None = None,
    ) -> tuple[AgentSession, bool]:
        """R2 prepare boundary: persist user authority without any GoalRun outbox."""
        return self.create_session(
            instruction=instruction, client_request_id=client_request_id,
            prepare_compatibility_goal=False, owner_principal_id=owner_principal_id,
            controller_id=controller_id, origin=origin,
        )

    # -- Canonical long-task aggregate ---------------------------------
    # A Task has no table and no generated identity of its own.  Its snapshot
    # lives on the AgentSession row, its timeline is session_events, and these
    # supporting tables retain only audit facts that cannot fit in one row.

    def get_task(self, task_id: str) -> Task:
        self.initialize()
        with self._connection() as connection:
            return self._task(connection, task_id)

    def get_task_for_owner(
        self, task_id: str, *, owner_principal_id: str, controller_id: str,
    ) -> Task:
        """Return a task only when its complete capability owner pair owns it.

        This is deliberately a not-found result rather than an authorization
        detail, so another local principal or controller installation cannot
        enumerate task identities.
        """
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_sessions WHERE session_id=? "
                "AND task_owner_principal_id=? AND task_controller_id=?",
                (task_id, owner_principal_id, controller_id),
            ).fetchone()
            if row is None:
                raise SessionNotFound("未找到该 AgentSession Task。")
            return _task(row)

    def list_tasks(self, *, limit: int = 100, include_archived: bool = False) -> list[Task]:
        self.initialize()
        with self._connection() as connection:
            where = "" if include_archived else "WHERE task_archived_at IS NULL"
            rows = connection.execute(
                f"SELECT * FROM agent_sessions {where} ORDER BY updated_at DESC, session_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_task(row) for row in rows]

    def list_tasks_for_scheduler(self) -> list[Task]:
        """Exhaustively discover non-archived Tasks for the resident scheduler.

        This is the scheduler's sole unscoped operation.  It returns persisted
        owner pairs so every subsequent inspect or mutation can use an explicit
        owner-scoped ``CanonicalTaskService``.  There is deliberately no silent
        100/1000 row ceiling.
        """

        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_sessions WHERE task_archived_at IS NULL "
                "ORDER BY updated_at DESC, session_id DESC"
            ).fetchall()
            return [_task(row) for row in rows]

    def list_tasks_for_owner(
        self, owner_principal_id: str, controller_id: str, *, limit: int = 100,
        include_archived: bool = False,
    ) -> list[Task]:
        self.initialize()
        with self._connection() as connection:
            archived_clause = "" if include_archived else "AND task_archived_at IS NULL"
            rows = connection.execute(
                "SELECT * FROM agent_sessions WHERE task_owner_principal_id=? AND task_controller_id=? " + archived_clause +
                " ORDER BY updated_at DESC, session_id DESC LIMIT ?",
                (owner_principal_id, controller_id, limit),
            ).fetchall()
            return [_task(row) for row in rows]

    def commit_runner_dispatch(
        self,
        task_id: str,
        *,
        owner_principal_id: str,
        controller_id: str,
        request: RunnerDispatchRequest,
    ) -> RunnerDispatchCommit:
        """Linearize one effect-bearing runner step with canonical authority.

        Task controls and revision acceptance share this ``BEGIN IMMEDIATE``
        writer boundary.  A control/revision committed first therefore blocks
        the physical boundary; a committed dispatch is the one action boundary
        that may subsequently be reconciled once.
        """

        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            if (
                task.owner_principal_id != owner_principal_id
                or task.controller_id != controller_id
            ):
                raise SessionNotFound("未找到该 AgentSession Task。")
            existing = connection.execute(
                "SELECT * FROM task_runner_dispatches WHERE session_id=? AND step_id=?",
                (task_id, request.step_id),
            ).fetchone()
            if existing is not None:
                committed = _runner_dispatch(existing)
                if (
                    committed.owner_principal_id != owner_principal_id
                    or committed.controller_id != controller_id
                    or committed.request != request
                ):
                    raise TaskDispatchConflict(
                        "runner dispatch step identity was reused with a different payload"
                    )
                return committed
            # This is the canonical half of the cross-database saga.  A
            # dispatch may exist before the generic-claims database is
            # reached (or after a hard crash), but no new step may roll past
            # that unresolved physical boundary.
            active_effect = connection.execute(
                "SELECT dispatch_id FROM task_runner_effects WHERE session_id=? AND state='ACTIVE'",
                (task_id,),
            ).fetchone()
            if active_effect is not None:
                raise TaskDispatchConflict("Task has an unresolved committed runner effect")
            if task.status is not TaskStatus.RUNNING:
                raise TaskDispatchConflict(
                    f"runner dispatch requires running Task, found {task.status.value}"
                )
            if task.current_revision != request.expected_revision:
                raise TaskDispatchConflict("runner dispatch revision is stale")
            # Accepting a revision advances canonical authority immediately,
            # but it may only take effect after the already committed effect
            # settles.  A *new* runner step during that gap could otherwise
            # execute under an instruction set that has never been applied.
            pending_revision = connection.execute(
                "SELECT 1 FROM task_revisions WHERE session_id=? AND status=? LIMIT 1",
                (task_id, TaskRecordStatus.ACCEPTED.value),
            ).fetchone()
            if pending_revision is not None:
                raise TaskDispatchConflict(
                    "runner dispatch requires accepted revisions to reach an applied boundary"
                )
            origin = dict(task.origin)
            if (
                origin.get("runner_kind") != request.runner_kind
                or str(origin.get("runner_version")) != request.runner_version
            ):
                raise TaskDispatchConflict("runner marker/version does not match Task origin")
            bindings = connection.execute(
                "SELECT * FROM task_subtasks WHERE session_id=? AND kind=?",
                (task_id, request.runner_kind),
            ).fetchall()
            if len(bindings) != 1:
                raise TaskDispatchConflict("runner dispatch requires exactly one binding subtask")
            binding = _task_subtask(bindings[0])
            if (
                binding.id != request.subtask_id
                or task.current_subtask_id != request.subtask_id
                or binding.object_ref != request.profile_id
                or binding.conversation_ref is not None
                or binding.status not in {"scheduled", "running", "recovering", "replanning"}
            ):
                raise TaskDispatchConflict("runner subtask/profile binding no longer matches")
            collision = connection.execute(
                "SELECT dispatch_id FROM task_runner_dispatches WHERE session_id=? "
                "AND (action_id=? OR command_id=?)",
                (task_id, request.action_id, request.command_id),
            ).fetchone()
            if collision is not None:
                raise TaskDispatchConflict("runner action or command identity is already bound")
            now = utc_now()
            dispatch_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO task_runner_dispatches("
                "dispatch_id,session_id,owner_principal_id,controller_id,authority_revision,"
                "step_id,action_id,command_id,runner_kind,runner_version,subtask_id,profile_id,"
                "profile_generation,device_boot_id,canonical_device_id,command_type,payload_digest,committed_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dispatch_id, task_id, owner_principal_id, controller_id,
                    request.expected_revision, request.step_id, request.action_id,
                    request.command_id, request.runner_kind, request.runner_version,
                    request.subtask_id, request.profile_id, request.profile_generation,
                    request.device_boot_id, request.canonical_device_id,
                    request.command_type, request.payload_digest, now,
                ),
            )
            connection.execute(
                "INSERT INTO task_runner_effects(dispatch_id,session_id,command_id,state,outcome,updated_at) "
                "VALUES (?,?,?,'ACTIVE',NULL,?)",
                (dispatch_id, task_id, request.command_id, now),
            )
            self._append_task_event(
                connection, task_id, SessionEventType.TASK_DISPATCH_COMMITTED,
                {
                    "dispatch_id": dispatch_id,
                    "step_id": request.step_id,
                    "action_id": request.action_id,
                    "command_type": request.command_type,
                    "runner_kind": request.runner_kind,
                    "revision": request.expected_revision,
                },
                f"task-runner-dispatch:{dispatch_id}", now,
            )
            row = connection.execute(
                "SELECT * FROM task_runner_dispatches WHERE dispatch_id=?", (dispatch_id,)
            ).fetchone()
            return _runner_dispatch(row)

    def settle_runner_effect(
        self, task_id: str, *, owner_principal_id: str, controller_id: str,
        command_id: str, outcome: str,
    ) -> None:
        """Close one exact canonical effect after evidence-bound reconciliation.

        This does not mutate Task lifecycle/control.  It only releases the
        unique active-effect fence so a subsequent fresh plan can be
        linearized.  Cross-store callers cannot close another owner/task.
        """

        if outcome not in {"observed", "not_observed", "preflight_rejected"}:
            raise ValueError("unsupported runner effect settlement outcome")
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            if task.owner_principal_id != owner_principal_id or task.controller_id != controller_id:
                raise SessionNotFound("未找到该 AgentSession Task。")
            row = connection.execute(
                "SELECT effect.dispatch_id,effect.state,effect.outcome,dispatch.command_id "
                "FROM task_runner_effects effect "
                "JOIN task_runner_dispatches dispatch ON dispatch.dispatch_id=effect.dispatch_id "
                "WHERE effect.session_id=? AND dispatch.command_id=?",
                (task_id, command_id),
            ).fetchone()
            if row is None:
                raise TaskDispatchConflict("runner effect was not found")
            if str(row["state"]) == "SETTLED":
                if str(row["outcome"]) != outcome:
                    raise TaskDispatchConflict(
                        "settled runner effect cannot change reconciliation outcome"
                    )
                return
            connection.execute(
                "UPDATE task_runner_effects SET state='SETTLED',outcome=?,updated_at=? WHERE dispatch_id=? AND state='ACTIVE'",
                (outcome, utc_now(), str(row["dispatch_id"])),
            )

    def require_active_runner_dispatch(
        self,
        task_id: str,
        *,
        owner_principal_id: str,
        controller_id: str,
        dispatch_id: str,
        command_id: str,
    ) -> RunnerDispatchCommit:
        """Read the exact canonical dispatch that still owns one ACTIVE effect.

        This is the final physical-authority query.  It deliberately does not
        require the Task to remain RUNNING or its current revision to remain
        unchanged: a dispatch that linearized before pause/takeover/revision
        may honestly finish its one already-committed effect.  It does require
        the same owner, immutable runner binding and exact ACTIVE effect.
        """

        self.initialize()
        with self._connection() as connection:
            task = self._task(connection, task_id)
            if (
                task.owner_principal_id != owner_principal_id
                or task.controller_id != controller_id
            ):
                raise SessionNotFound("未找到该 AgentSession Task。")
            row = connection.execute(
                "SELECT dispatch.* FROM task_runner_dispatches dispatch "
                "JOIN task_runner_effects effect ON effect.dispatch_id=dispatch.dispatch_id "
                "WHERE dispatch.dispatch_id=? AND dispatch.session_id=? "
                "AND dispatch.command_id=? AND effect.session_id=? "
                "AND effect.command_id=? AND effect.state='ACTIVE' "
                "AND effect.outcome IS NULL",
                (dispatch_id, task_id, command_id, task_id, command_id),
            ).fetchone()
            if row is None:
                raise TaskDispatchConflict(
                    "exact ACTIVE canonical runner effect was not found"
                )
            committed = _runner_dispatch(row)
            if (
                committed.owner_principal_id != owner_principal_id
                or committed.controller_id != controller_id
                or task.current_subtask_id != committed.request.subtask_id
            ):
                raise TaskDispatchConflict(
                    "canonical runner owner or current binding changed"
                )
            origin = dict(task.origin)
            if (
                origin.get("runner_kind") != committed.request.runner_kind
                or str(origin.get("runner_version")) != committed.request.runner_version
            ):
                raise TaskDispatchConflict("canonical runner marker changed")
            bindings = connection.execute(
                "SELECT * FROM task_subtasks WHERE session_id=? AND kind=?",
                (task_id, committed.request.runner_kind),
            ).fetchall()
            if len(bindings) != 1:
                raise TaskDispatchConflict(
                    "canonical runner requires exactly one current binding subtask"
                )
            binding = _task_subtask(bindings[0])
            if (
                binding.id != committed.request.subtask_id
                or binding.object_ref != committed.request.profile_id
                or binding.conversation_ref is not None
                or binding.status not in {"scheduled", "running", "recovering", "replanning"}
            ):
                raise TaskDispatchConflict("canonical runner binding changed")
            return committed

    def runner_dispatches(self, task_id: str) -> list[RunnerDispatchCommit]:
        """Internal recovery lookup; callers must perform owner fencing first."""

        self.initialize()
        with self._connection() as connection:
            self._task(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM task_runner_dispatches WHERE session_id=? "
                "ORDER BY committed_at, dispatch_id", (task_id,)
            ).fetchall()
            return [_runner_dispatch(row) for row in rows]

    def task_revisions(self, task_id: str) -> list[TaskRevision]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM task_revisions WHERE session_id=? ORDER BY revision", (task_id,)
            ).fetchall()
            return [_task_revision(row) for row in rows]

    def task_controls(self, task_id: str) -> list[TaskControl]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM task_controls WHERE session_id=? ORDER BY requested_at, control_id", (task_id,)
            ).fetchall()
            return [_task_control(row) for row in rows]

    def task_subtasks(self, task_id: str) -> list[TaskSubtask]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM task_subtasks WHERE session_id=? ORDER BY created_at, subtask_id", (task_id,)
            ).fetchall()
            return [_task_subtask(row) for row in rows]

    def upsert_task_subtask(
        self, task_id: str, *, kind: str, object_ref: str | None, conversation_ref: str | None,
        status: str, priority: int = 50, current_stage: str | None = None,
    ) -> TaskSubtask:
        self.initialize()
        if not kind.strip() or not status.strip():
            raise ValueError("subtask kind and status are required")
        if not 0 <= priority <= 100:
            raise ValueError("subtask priority must be between 0 and 100")
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            row = connection.execute(
                "SELECT * FROM task_subtasks WHERE session_id=? AND kind=? "
                "AND object_ref IS ? AND conversation_ref IS ?",
                (task_id, kind, object_ref, conversation_ref),
            ).fetchone()
            if row is None:
                subtask_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO task_subtasks(subtask_id, session_id, kind, object_ref, conversation_ref, status, priority, current_stage, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (subtask_id, task_id, kind, object_ref, conversation_ref, status, priority, current_stage, now, now),
                )
            else:
                subtask_id = str(row["subtask_id"])
                connection.execute(
                    "UPDATE task_subtasks SET status=?, priority=?, current_stage=?, updated_at=? WHERE subtask_id=?",
                    (status, priority, current_stage, now, subtask_id),
                )
            connection.execute(
                "UPDATE agent_sessions SET task_current_subtask_id=?, updated_at=? WHERE session_id=?",
                (subtask_id, now, task_id),
            )
            self._append_task_event(
                connection, task_id, SessionEventType.TASK_SUBTASK_UPSERTED,
                {"subtask_id": subtask_id, "kind": kind, "object_ref": object_ref,
                 "conversation_ref": conversation_ref, "status": status, "revision": task.current_revision},
                f"subtask:{subtask_id}:{status}:{current_stage or ''}", now,
            )
            return _task_subtask(connection.execute(
                "SELECT * FROM task_subtasks WHERE subtask_id=?", (subtask_id,)
            ).fetchone())

    def project_task_subtask_transition(
        self,
        task_id: str,
        *,
        owner_principal_id: str,
        controller_id: str,
        expected_revision: int,
        expected_current_status: TaskStatus | str,
        kind: str,
        object_ref: str | None,
        conversation_ref: str | None,
        subtask_status: str,
        current_stage: str,
        target_status: TaskStatus | str,
        reason: TaskReason,
        idempotency_key: str,
        next_wake_at: str | None = None,
        ordinary_failure: bool = False,
    ) -> tuple[TaskSubtask, Task]:
        """Atomically CAS one runner result into its canonical projections.

        A model/handler result is stale as soon as a control or revision wins.
        The subtask/current-stage projection must therefore share the same
        ``BEGIN IMMEDIATE`` and authority check as its Task lifecycle change;
        otherwise a late result can overwrite the visible subtask after pause,
        takeover, cancel, or revision acceptance has already committed.

        This is deliberately an update-only runner seam.  Runner bindings are
        immutable admission facts and must exist before execution starts.
        """

        self.initialize()
        if expected_revision < 1:
            raise ValueError("expected task revision must be positive")
        if not kind.strip() or not subtask_status.strip() or not current_stage.strip():
            raise ValueError("runner subtask kind, status, and current_stage are required")
        if not idempotency_key.strip():
            raise ValueError("runner projection idempotency_key is required")
        try:
            expected = TaskStatus(expected_current_status)
            target = TaskStatus(target_status)
        except ValueError as error:
            raise TaskControlConflict("unsupported runner projection task status") from error
        if target is TaskStatus.WAITING_TIME and next_wake_at is None:
            raise ValueError("waiting_time requires next_wake_at")
        if target is not TaskStatus.WAITING_TIME:
            next_wake_at = None
        if ordinary_failure:
            if reason.code in _INTEGRITY_REASON_CODES:
                raise ValueError("integrity failures must use fence_task_integrity")
            if not reason.recoverable or target not in {
                TaskStatus.RECOVERING,
                TaskStatus.REPLANNING,
                TaskStatus.WAITING_TIME,
                TaskStatus.WAITING_EVENT,
            }:
                raise ValueError("ordinary runner failure requires a recoverable lifecycle target")

        now = utc_now()
        projection_key = f"runner-projection:{idempotency_key}:subtask"
        state_key = (
            f"ordinary-failure:{idempotency_key}:state"
            if ordinary_failure
            else f"task-state:{idempotency_key}"
        )
        request_key = f"ordinary-failure:{idempotency_key}" if ordinary_failure else None
        request_type = (
            SessionEventType.TASK_REPLAN_REQUESTED
            if target is TaskStatus.REPLANNING
            else SessionEventType.TASK_RECOVERY_REQUESTED
        )

        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            if (
                task.owner_principal_id != owner_principal_id
                or task.controller_id != controller_id
            ):
                raise SessionNotFound("未找到该 AgentSession Task。")

            projection_row = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? AND idempotency_key=?",
                (task_id, projection_key),
            ).fetchone()
            state_row = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? AND idempotency_key=?",
                (task_id, state_key),
            ).fetchone()
            request_row = (
                connection.execute(
                    "SELECT * FROM session_events WHERE session_id=? AND idempotency_key=?",
                    (task_id, request_key),
                ).fetchone()
                if request_key is not None
                else None
            )
            if projection_row is not None or state_row is not None or request_row is not None:
                required_rows = [projection_row, state_row]
                if ordinary_failure:
                    required_rows.append(request_row)
                if any(row is None for row in required_rows):
                    raise SessionIdempotencyConflict(
                        "runner projection replay is missing part of its atomic event set"
                    )
                projection_event = _event(projection_row)
                state_event = _event(state_row)
                projection_data = dict(projection_event.data)
                state_data = dict(state_event.data)
                replay_matches = (
                    projection_event.event_type is SessionEventType.TASK_SUBTASK_UPSERTED
                    and projection_data.get("kind") == kind
                    and projection_data.get("object_ref") == object_ref
                    and projection_data.get("conversation_ref") == conversation_ref
                    and projection_data.get("status") == subtask_status
                    and projection_data.get("current_stage") == current_stage
                    and projection_data.get("revision") == expected_revision
                    and state_event.event_type is SessionEventType.TASK_STATE_CHANGED
                    and state_data.get("from_status") == expected.value
                    and state_data.get("status") == target.value
                    and state_data.get("reason_code") == reason.code
                    and state_data.get("recoverable") is reason.recoverable
                    and state_data.get("next_wake_at") == next_wake_at
                    and state_data.get("revision") == expected_revision
                )
                if ordinary_failure:
                    assert request_row is not None
                    request_event = _event(request_row)
                    request_data = dict(request_event.data)
                    replay_matches = replay_matches and (
                        request_event.event_type is request_type
                        and request_data.get("reason_code") == reason.code
                        and request_data.get("summary") == reason.summary[:500]
                        and request_data.get("revision") == expected_revision
                    )
                if not replay_matches:
                    raise SessionIdempotencyConflict(
                        "runner projection idempotency key was reused with a different payload"
                    )
                stored_subtask = connection.execute(
                    "SELECT * FROM task_subtasks WHERE subtask_id=?",
                    (projection_data.get("subtask_id"),),
                ).fetchone()
                if stored_subtask is None:
                    raise SessionIdempotencyConflict(
                        "runner projection replay references a missing subtask"
                    )
                return _task_subtask(stored_subtask), task

            if task.current_revision != expected_revision:
                raise TaskExpectedStatusConflict(
                    f"task expected revision {expected_revision}, found {task.current_revision}"
                )
            if task.status is not expected:
                raise TaskExpectedStatusConflict(
                    f"task expected status {expected.value}, found {task.status.value}"
                )
            if not can_transition_task(task.status, target):
                raise TaskControlConflict(
                    f"task state {task.status.value} cannot transition to {target.value}"
                )

            subtask_row = connection.execute(
                "SELECT * FROM task_subtasks WHERE session_id=? AND kind=? "
                "AND object_ref IS ? AND conversation_ref IS ?",
                (task_id, kind, object_ref, conversation_ref),
            ).fetchone()
            if subtask_row is None:
                raise TaskControlConflict("runner projection requires an existing immutable subtask binding")
            subtask_id = str(subtask_row["subtask_id"])
            connection.execute(
                "UPDATE task_subtasks SET status=?, priority=?, current_stage=?, updated_at=? "
                "WHERE subtask_id=?",
                (subtask_status, task.priority, current_stage, now, subtask_id),
            )
            self._append_task_event(
                connection,
                task_id,
                SessionEventType.TASK_SUBTASK_UPSERTED,
                {
                    "subtask_id": subtask_id,
                    "kind": kind,
                    "object_ref": object_ref,
                    "conversation_ref": conversation_ref,
                    "status": subtask_status,
                    "priority": task.priority,
                    "current_stage": current_stage,
                    "revision": expected_revision,
                },
                projection_key,
                now,
            )
            if ordinary_failure:
                assert request_key is not None
                self._append_task_event(
                    connection,
                    task_id,
                    request_type,
                    {
                        "reason_code": reason.code,
                        "summary": reason.summary[:500],
                        "revision": expected_revision,
                    },
                    request_key,
                    now,
                )
            self._append_task_event(
                connection,
                task_id,
                SessionEventType.TASK_STATE_CHANGED,
                {
                    "from_status": task.status.value,
                    "status": target.value,
                    "reason_code": reason.code,
                    "recoverable": reason.recoverable,
                    "next_wake_at": next_wake_at,
                    "revision": expected_revision,
                },
                state_key,
                now,
            )
            terminal_at = now if target.terminal else None
            connection.execute(
                "UPDATE agent_sessions SET task_current_subtask_id=?, task_status=?, "
                "task_reason_code=?, task_reason_summary=?, task_reason_recoverable=?, "
                "task_next_wake_at=?, task_terminal_at=COALESCE(?, task_terminal_at), "
                "updated_at=? WHERE session_id=?",
                (
                    subtask_id,
                    target.value,
                    reason.code,
                    reason.summary[:500],
                    int(reason.recoverable),
                    next_wake_at,
                    terminal_at,
                    now,
                    task_id,
                ),
            )
            projected_subtask = _task_subtask(connection.execute(
                "SELECT * FROM task_subtasks WHERE subtask_id=?", (subtask_id,)
            ).fetchone())
            return projected_subtask, self._task(connection, task_id)

    def record_task_revision(
        self, task_id: str, *, base_revision: int, kind: TaskRevisionKind | str,
        instruction: str, patch: Mapping[str, Any], idempotency_key: str,
        requested_by: Mapping[str, Any], effective_boundary: str = "after_current_action",
    ) -> TaskRevision:
        self.initialize()
        try:
            parsed_kind = TaskRevisionKind(kind)
        except ValueError as error:
            raise TaskRevisionConflict(f"unsupported task revision kind: {kind}") from error
        instruction = instruction.strip()
        if not instruction or not idempotency_key.strip():
            raise ValueError("task revision instruction and idempotency_key are required")
        if effective_boundary not in {"after_current_action", "current_checkpoint"}:
            raise ValueError("unsupported task revision effective_boundary")
        digest = _digest({"base_revision": base_revision, "kind": parsed_kind.value, "instruction": instruction,
                          "patch": dict(patch), "requested_by": dict(requested_by), "effective_boundary": effective_boundary})
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            existing = connection.execute(
                "SELECT * FROM task_revisions WHERE session_id=? AND idempotency_key=?",
                (task_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                stored = _task_revision(existing)
                if _digest({"base_revision": stored.base_revision, "kind": stored.kind.value,
                            "instruction": stored.instruction, "patch": dict(stored.patch),
                            "requested_by": dict(stored.requested_by), "effective_boundary": stored.effective_boundary}) != digest:
                    raise TaskRevisionConflict("task revision idempotency key was reused with a different payload")
                return stored
            if task.terminal:
                raise TaskRevisionConflict("terminal tasks cannot accept a revision")
            if base_revision != task.current_revision:
                raise TaskRevisionConflict(
                    f"task revision conflict: expected {base_revision}, current {task.current_revision}"
                )
            revision = base_revision + 1
            revision_id = str(uuid.uuid4())
            # An accepted revision advances authority immediately.  Application
            # is separately recorded at a verified action boundary, so a
            # dispatched command can never be replayed to apply a new intent.
            connection.execute(
                "INSERT INTO task_revisions(task_revision_id, session_id, revision, base_revision, kind, instruction, patch_json, effective_boundary, requested_by_json, status, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (revision_id, task_id, revision, base_revision, parsed_kind.value, instruction, _json(dict(patch)),
                 effective_boundary, _json(dict(requested_by)), TaskRecordStatus.ACCEPTED.value, idempotency_key, now),
            )
            connection.execute(
                "UPDATE agent_sessions SET authority_revision=?, updated_at=? WHERE session_id=?",
                (revision, now, task_id),
            )
            self._append_task_event(
                connection, task_id, SessionEventType.TASK_REVISION_ACCEPTED,
                {"revision_id": revision_id, "revision": revision, "base_revision": base_revision,
                 "kind": parsed_kind.value, "effective_boundary": effective_boundary},
                f"task-revision:{idempotency_key}:accepted", now,
            )
            if effective_boundary == "current_checkpoint" and self._session(connection, task_id).active_slice_id is None:
                self._apply_task_revision(connection, revision_id, now)
            return _task_revision(connection.execute(
                "SELECT * FROM task_revisions WHERE task_revision_id=?", (revision_id,)
            ).fetchone())

    def apply_task_revisions_at_action_boundary(self, task_id: str) -> tuple[TaskRevision, ...]:
        """Apply accepted revisions only after the caller has verified a safe boundary."""
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            if task.terminal:
                return ()
            rows = connection.execute(
                "SELECT * FROM task_revisions WHERE session_id=? AND status=? ORDER BY revision",
                (task_id, TaskRecordStatus.ACCEPTED.value),
            ).fetchall()
            applied = [self._apply_task_revision(connection, str(row["task_revision_id"]), now) for row in rows]
            return tuple(applied)

    def transition_task(
        self, task_id: str, *, status: TaskStatus | str, reason: TaskReason,
        idempotency_key: str, next_wake_at: str | None = None,
        expected_current_status: TaskStatus | str | None = None,
    ) -> Task:
        self.initialize()
        try:
            target = TaskStatus(status)
        except ValueError as error:
            raise TaskControlConflict(f"unsupported task status: {status}") from error
        try:
            expected = (
                TaskStatus(expected_current_status)
                if expected_current_status is not None
                else None
            )
        except ValueError as error:
            raise TaskControlConflict(
                f"unsupported expected current task status: {expected_current_status}"
            ) from error
        if target is TaskStatus.WAITING_TIME and next_wake_at is None:
            raise ValueError("waiting_time requires next_wake_at")
        if target is not TaskStatus.WAITING_TIME:
            next_wake_at = None
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            event_key = f"task-state:{idempotency_key}"
            existing_row = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? AND idempotency_key=?",
                (task_id, event_key),
            ).fetchone()
            if existing_row is not None:
                existing = _event(existing_row)
                data = dict(existing.data)
                replay_matches = (
                    existing.event_type is SessionEventType.TASK_STATE_CHANGED
                    and data.get("status") == target.value
                    and data.get("reason_code") == reason.code
                    and data.get("recoverable") is reason.recoverable
                    and data.get("next_wake_at") == next_wake_at
                    and (expected is None or data.get("from_status") == expected.value)
                )
                if not replay_matches:
                    raise SessionIdempotencyConflict(
                        "task transition idempotency key was reused with a different payload"
                    )
                return task
            if expected is not None and task.status is not expected:
                raise TaskExpectedStatusConflict(
                    f"task expected status {expected.value}, found {task.status.value}"
                )
            if not can_transition_task(task.status, target):
                raise TaskControlConflict(
                    f"task state {task.status.value} cannot transition to {target.value}"
                )
            event = self._append_task_event(
                connection, task_id, SessionEventType.TASK_STATE_CHANGED,
                {"from_status": task.status.value, "status": target.value, "reason_code": reason.code,
                 "recoverable": reason.recoverable, "next_wake_at": next_wake_at,
                 "revision": task.current_revision},
                event_key, now,
            )
            terminal_at = now if target.terminal else None
            connection.execute(
                "UPDATE agent_sessions SET task_status=?, task_reason_code=?, task_reason_summary=?, task_reason_recoverable=?, task_next_wake_at=?, task_terminal_at=COALESCE(?, task_terminal_at), updated_at=? WHERE session_id=?",
                (target.value, reason.code, reason.summary[:500], int(reason.recoverable), next_wake_at, terminal_at, now, task_id),
            )
            # The task event and its durable projection outbox were inserted
            # before this update in the same SQLite transaction; rollback rolls
            # both back, keeping snapshot/event/outbox atomic.
            _ = event
            return self._task(connection, task_id)

    def record_ordinary_failure(
        self, task_id: str, *, reason_code: str, summary: str, idempotency_key: str,
        replan: bool = False, next_wake_at: str | None = None,
        expected_current_status: TaskStatus | str | None = None,
    ) -> Task:
        """Keep recoverable page/network/model failures out of terminal state."""
        if reason_code in _INTEGRITY_REASON_CODES:
            raise ValueError("integrity failures must use fence_task_integrity")
        try:
            expected = (
                TaskStatus(expected_current_status)
                if expected_current_status is not None
                else None
            )
        except ValueError as error:
            raise TaskControlConflict(
                f"unsupported expected current task status: {expected_current_status}"
            ) from error
        target = TaskStatus.REPLANNING if replan else (TaskStatus.WAITING_TIME if next_wake_at else TaskStatus.RECOVERING)
        event_type = SessionEventType.TASK_REPLAN_REQUESTED if replan else SessionEventType.TASK_RECOVERY_REQUESTED
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            request_key = f"ordinary-failure:{idempotency_key}"
            state_key = f"ordinary-failure:{idempotency_key}:state"
            request_row = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? AND idempotency_key=?",
                (task_id, request_key),
            ).fetchone()
            state_row = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? AND idempotency_key=?",
                (task_id, state_key),
            ).fetchone()
            if request_row is not None or state_row is not None:
                if request_row is None or state_row is None:
                    raise SessionIdempotencyConflict(
                        "ordinary failure replay is missing its atomic state event"
                    )
                request_event = _event(request_row)
                state_event = _event(state_row)
                request_data = dict(request_event.data)
                state_data = dict(state_event.data)
                replay_matches = (
                    request_event.event_type is event_type
                    and request_data.get("reason_code") == reason_code
                    and request_data.get("summary") == summary[:500]
                    and state_event.event_type is SessionEventType.TASK_STATE_CHANGED
                    and state_data.get("status") == target.value
                    and state_data.get("reason_code") == reason_code
                    and state_data.get("recoverable") is True
                    and state_data.get("next_wake_at") == next_wake_at
                    and (expected is None or state_data.get("from_status") == expected.value)
                )
                if not replay_matches:
                    raise SessionIdempotencyConflict(
                        "ordinary failure idempotency key was reused with a different payload"
                    )
                return task
            if expected is not None and task.status is not expected:
                raise TaskExpectedStatusConflict(
                    f"task expected status {expected.value}, found {task.status.value}"
                )
            if task.terminal:
                return task
            if not can_transition_task(task.status, target):
                raise TaskControlConflict(
                    f"task state {task.status.value} cannot enter ordinary recovery {target.value}"
                )
            self._append_task_event(
                connection, task_id, event_type,
                {"reason_code": reason_code, "summary": summary[:500], "revision": task.current_revision},
                request_key, now,
            )
            self._append_task_event(
                connection, task_id, SessionEventType.TASK_STATE_CHANGED,
                {"from_status": task.status.value, "status": target.value, "reason_code": reason_code,
                 "recoverable": True, "next_wake_at": next_wake_at, "revision": task.current_revision},
                state_key, now,
            )
            connection.execute(
                "UPDATE agent_sessions SET task_status=?, task_reason_code=?, task_reason_summary=?, task_reason_recoverable=1, task_next_wake_at=?, updated_at=? WHERE session_id=?",
                (target.value, reason_code, summary[:500], next_wake_at if target is TaskStatus.WAITING_TIME else None, now, task_id),
            )
            return self._task(connection, task_id)

    def fence_task_integrity(
        self, task_id: str, *, reason_code: str, summary: str, idempotency_key: str,
    ) -> Task:
        """Persist one of the four immediate-stop integrity fences."""
        if reason_code not in _INTEGRITY_REASON_CODES:
            raise ValueError("unsupported integrity reason_code")
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            self._append_task_event(
                connection, task_id, SessionEventType.TASK_INTEGRITY_BLOCKED,
                {"reason_code": reason_code, "summary": summary[:500], "revision": task.current_revision},
                f"integrity:{idempotency_key}", now,
            )
            connection.execute(
                "UPDATE agent_sessions SET task_integrity_state=?, task_integrity_reason_code=?, updated_at=? WHERE session_id=?",
                (TaskIntegrityState.BLOCKED.value, reason_code, now, task_id),
            )
            if not task.terminal:
                connection.execute(
                    "UPDATE agent_sessions SET task_status=?, task_reason_code=?, task_reason_summary=?, task_reason_recoverable=0, task_terminal_at=?, updated_at=? WHERE session_id=?",
                    (TaskStatus.FAILED.value, reason_code, summary[:500], now, now, task_id),
                )
            return self._task(connection, task_id)

    def control_task(
        self, task_id: str, *, action: TaskControlAction | str, idempotency_key: str,
        expected_revision: int, requested_by: Mapping[str, Any], priority: int | None = None,
    ) -> TaskControl:
        self.initialize()
        try:
            parsed_action = TaskControlAction(action)
        except ValueError as error:
            raise TaskControlConflict(f"unsupported task control action: {action}") from error
        if priority is not None and not 0 <= priority <= 100:
            raise ValueError("task priority must be between 0 and 100")
        now = utc_now()
        digest = _digest({"action": parsed_action.value, "expected_revision": expected_revision,
                          "requested_by": dict(requested_by), "priority": priority})
        with self._lock, self._connection(write=True) as connection:
            task = self._task(connection, task_id)
            existing = connection.execute(
                "SELECT * FROM task_controls WHERE session_id=? AND idempotency_key=?", (task_id, idempotency_key)
            ).fetchone()
            if existing is not None:
                control = _task_control(existing)
                stored_digest = _digest({"action": control.action.value, "expected_revision": control.expected_revision,
                                         "requested_by": dict(control.requested_by),
                                         "priority": existing["requested_priority"]})
                if stored_digest != digest:
                    raise TaskControlConflict("task control idempotency key was reused with a different payload")
                return control
            if expected_revision != task.current_revision:
                raise TaskControlConflict(
                    f"task control revision conflict: expected {expected_revision}, current {task.current_revision}"
                )
            if parsed_action is not TaskControlAction.ARCHIVE and task.terminal:
                raise TaskControlConflict("terminal task controls are immutable; create a new task to rerun")
            if parsed_action is TaskControlAction.ARCHIVE and not task.terminal:
                raise TaskControlConflict("only terminal tasks may be archived")
            control_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO task_controls(control_id, session_id, action, idempotency_key, expected_revision, requested_priority, requested_by_json, status, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (control_id, task_id, parsed_action.value, idempotency_key, expected_revision,
                 priority, _json(dict(requested_by)), TaskRecordStatus.ACCEPTED.value, now),
            )
            self._append_task_event(
                connection, task_id, SessionEventType.TASK_CONTROL_ACCEPTED,
                {"control_id": control_id, "action": parsed_action.value, "revision": expected_revision},
                f"task-control:{idempotency_key}:accepted", now,
            )
            self._apply_task_control(connection, control_id, priority, now)
            return _task_control(connection.execute("SELECT * FROM task_controls WHERE control_id=?", (control_id,)).fetchone())

    def list_sessions(self, limit: int | None = None) -> list[AgentSession]:
        self.initialize()
        with self._connection() as connection:
            sql = "SELECT * FROM agent_sessions ORDER BY created_at DESC, session_id DESC"
            rows = (
                connection.execute(sql + " LIMIT ?", (limit,)).fetchall()
                if limit is not None
                else connection.execute(sql).fetchall()
            )
            return [_agent_session(row) for row in rows]

    def sessions_for_recovery(self) -> list[AgentSession]:
        """Return every durable Session required for correctness recovery."""

        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_sessions ORDER BY created_at, session_id"
            ).fetchall()
            return [_agent_session(row) for row in rows]

    def selected_bindings_for_recovery(self) -> list[SessionGoalBinding]:
        """Return every live scheduler selection without a UI list ceiling.

        Launcher recovery must fence physical owners before any downstream
        worker starts.  Reusing ``list_sessions(limit)`` here would silently
        fail open once the durable history grew past that presentation limit.
        """

        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT b.* FROM session_goal_bindings b "
                "JOIN agent_sessions s ON s.session_id=b.session_id "
                "WHERE s.active_goal_id=b.goal_node_id "
                "AND s.control_mode='AGENT_ACTIVE' "
                "AND s.status NOT IN ('STOPPED', 'COMPLETED', 'PARTIAL', 'FAILED') "
                "AND b.status='BOUND' "
                "ORDER BY s.created_at, s.session_id, b.binding_id"
            ).fetchall()
            return [_binding(row) for row in rows]

    def get_session(self, session_id: str) -> AgentSession:
        self.initialize()
        with self._connection() as connection:
            return self._session(connection, session_id)

    def directives(self, session_id: str) -> list[UserDirective]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM user_directives WHERE session_id=? ORDER BY revision", (session_id,)
            ).fetchall()
            return [_directive(row) for row in rows]

    def goal_nodes(self, session_id: str) -> list[GoalNode]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM goal_nodes WHERE session_id=? ORDER BY created_at, goal_node_id", (session_id,)
            ).fetchall()
            return [_goal_node(row) for row in rows]

    def binding(self, session_id: str) -> SessionGoalBinding | None:
        """Return the R2 compatibility binding for ``active_goal_id``.

        Before R3 there is no AttentionDecision yet, so the first activated
        Goal remains the stable compatibility focus.  Never let SQLite row
        order silently select a different GoalRun in a multi-Goal Session.
        """
        self.initialize()
        with self._connection() as connection:
            session = self._session(connection, session_id)
            if session.active_goal_id is not None:
                row = connection.execute(
                    "SELECT * FROM session_goal_bindings WHERE session_id=? AND goal_node_id=? "
                    "AND status IN ('REQUESTED', 'BOUND') LIMIT 1",
                    (session_id, session.active_goal_id),
                ).fetchone()
                if row is not None:
                    return _binding(row)
            row = connection.execute(
                "SELECT * FROM session_goal_bindings WHERE session_id=? "
                "AND status IN ('REQUESTED', 'BOUND') ORDER BY created_at, binding_id LIMIT 1",
                (session_id,),
            ).fetchone()
            return _binding(row) if row is not None else None

    def events(self, session_id: str, *, after: int, limit: int) -> list[SessionEvent]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? AND cursor>? "
                "ORDER BY cursor LIMIT ?", (session_id, after, limit),
            ).fetchall()
            return [_event(row) for row in rows]

    def all_events(self, session_id: str) -> list[SessionEvent]:
        """Return a complete Session timeline for restart recovery only."""

        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? ORDER BY cursor",
                (session_id,),
            ).fetchall()
            return [_event(row) for row in rows]

    def event_by_id(self, event_id: str) -> SessionEvent | None:
        """Look up a globally unique durable EventInbox record directly."""

        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM session_events WHERE event_id=?", (event_id,)
            ).fetchone()
            return _event(row) if row is not None else None

    def ingest_event(
        self,
        session_id: str,
        *,
        source_namespace: str,
        source_event_id: str,
        event_type: SessionEventType | str,
        payload: dict[str, Any],
        occurred_at: str,
        device_id: str | None = None,
        device_boot_id: str | None = None,
        source_cursor: str | None = None,
        source_stream_id: str | None = None,
    ) -> tuple[SessionEvent, bool]:
        """Persist one inbox event before classification or scheduler effects.

        Device identities are canonicalized to the exact
        ``device_id + device_boot_id + source_event_id`` source key.  A replay
        with the same identity and canonical payload returns the original row;
        a changed payload is an idempotency conflict.
        """

        self.initialize()
        source_namespace = source_namespace.strip()
        source_event_id = source_event_id.strip()
        if not source_namespace or not source_event_id:
            raise ValueError("event source namespace and source event id must not be blank")
        if (device_id is None) != (device_boot_id is None):
            raise ValueError("device_id and device_boot_id must be supplied together")
        if source_stream_id is not None:
            source_stream_id = source_stream_id.strip()
            if not source_stream_id or device_id is None:
                raise ValueError(
                    "source_stream_id requires a non-blank device event stream"
                )
        kind = event_type if isinstance(event_type, SessionEventType) else SessionEventType(event_type)
        canonical_namespace = (
            (
                f"device:{device_id}:{device_boot_id}:stream:{source_stream_id}"
                if source_stream_id is not None
                else f"device:{device_id}:{device_boot_id}"
            )
            if device_id is not None
            else source_namespace
        )
        idempotency_key = f"{canonical_namespace}:{source_event_id}"
        digest = _digest({
            "source_namespace": canonical_namespace,
            "source_event_id": source_event_id,
            "event_type": kind.value,
            "occurred_at": occurred_at,
            "payload": payload,
            "device_id": device_id,
            "device_boot_id": device_boot_id,
            "source_cursor": source_cursor,
        })
        received_at = utc_now()
        with self._lock, self._connection(write=True) as connection:
            self._session(connection, session_id)
            existing = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? AND source_namespace=? AND source_event_id=?",
                (session_id, canonical_namespace, source_event_id),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_digest"]) != digest:
                    raise SessionIdempotencyConflict(
                        "同一事件来源标识已用于不同的事件内容。"
                    )
                return _event(existing), False
            if device_id is not None and source_cursor is not None:
                cursor_existing = connection.execute(
                    "SELECT * FROM session_events WHERE session_id=? AND source_namespace=? "
                    "AND source_cursor=?",
                    (session_id, canonical_namespace, source_cursor),
                ).fetchone()
                if cursor_existing is not None:
                    raise SessionIdempotencyConflict(
                        "同一设备启动游标已用于不同的事件标识。"
                    )
            event_id = str(uuid.uuid4())
            cursor = connection.execute(
                "INSERT INTO session_events(event_id, session_id, event_type, data_json, idempotency_key, "
                "handling_status, created_at, handled_at, source_namespace, source_event_id, device_id, "
                "device_boot_id, source_cursor, occurred_at, received_at, affected_goal_ids_json, payload_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, '[]', ?)",
                (event_id, session_id, kind.value, _json(payload), idempotency_key,
                 EventHandlingStatus.RECEIVED.value, received_at, canonical_namespace,
                 source_event_id, device_id, device_boot_id, source_cursor, occurred_at,
                 received_at, digest),
            ).lastrowid
            connection.execute(
                "UPDATE agent_sessions SET event_cursor=?, updated_at=? WHERE session_id=?",
                (cursor, received_at, session_id),
            )
            row = connection.execute("SELECT * FROM session_events WHERE cursor=?", (cursor,)).fetchone()
            return _event(row), True

    def pending_inbox_events(
        self, session_id: str | None = None, *, limit: int | None = None
    ) -> list[SessionEvent]:
        self.initialize()
        with self._connection() as connection:
            sql = "SELECT * FROM session_events WHERE handling_status IN ('RECEIVED', 'CLASSIFIED')"
            args: tuple[Any, ...] = ()
            if session_id is not None:
                self._session(connection, session_id)
                sql += " AND session_id=?"
                args = (session_id,)
            sql += " ORDER BY cursor"
            if limit is not None:
                sql += " LIMIT ?"
                args = (*args, limit)
            rows = connection.execute(sql, args).fetchall()
            return [_event(row) for row in rows]

    def route_inbox_event(self, event_id: str) -> tuple[SessionEvent, tuple[WakeCondition, ...]]:
        """Classify an event and satisfy only exact matching wake conditions."""

        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM session_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown session event: {event_id}")
            event = _event(row)
            if event.handling_status is not EventHandlingStatus.RECEIVED:
                affected_rows = connection.execute(
                    "SELECT * FROM wake_conditions WHERE session_id=? AND satisfied_by_event_id=? "
                    "ORDER BY created_at, wake_condition_id", (event.session_id, event.id),
                ).fetchall()
                return event, tuple(_wake_condition(item) for item in affected_rows)
            matched: list[WakeCondition] = []
            wake_rows = connection.execute(
                "SELECT * FROM wake_conditions WHERE session_id=? AND status='PENDING' ORDER BY created_at, wake_condition_id",
                (event.session_id,),
            ).fetchall()
            for wake_row in wake_rows:
                wake = _wake_condition(wake_row)
                if not _event_matches_wake(event, wake):
                    continue
                connection.execute(
                    "UPDATE wake_conditions SET status=?, satisfied_by_event_id=?, satisfied_at=? "
                    "WHERE wake_condition_id=? AND status='PENDING'",
                    (WakeConditionStatus.SATISFIED.value, event.id, now, wake.id),
                )
                connection.execute(
                    "UPDATE goal_nodes SET status=?, waiting_kind=NULL, waiting_ref=NULL, "
                    "next_eligible_at=NULL, backoff_until=NULL, updated_at=? "
                    "WHERE goal_node_id=? AND status LIKE 'WAITING_%'",
                    (GoalNodeStatus.READY.value, now, wake.goal_id),
                )
                self._advance_agenda_revision(connection, event.session_id, now)
                matched.append(_wake_condition(connection.execute(
                    "SELECT * FROM wake_conditions WHERE wake_condition_id=?", (wake.id,)
                ).fetchone()))
            affected_goal_ids = tuple(item.goal_id for item in matched)
            status = (
                EventHandlingStatus.IGNORED
                if event.event_type is SessionEventType.NOTIFICATION_POSTED and not affected_goal_ids
                else EventHandlingStatus.CLASSIFIED
            )
            handled_at = now if status is EventHandlingStatus.IGNORED else None
            connection.execute(
                "UPDATE session_events SET handling_status=?, classified_at=?, affected_goal_ids_json=?, handled_at=? "
                "WHERE event_id=?",
                (status.value, now, _json(affected_goal_ids), handled_at, event.id),
            )
            row = connection.execute("SELECT * FROM session_events WHERE event_id=?", (event.id,)).fetchone()
            return _event(row), tuple(matched)

    def mark_event_handled(self, event_id: str, *, decision_id: str | None) -> SessionEvent:
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM session_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown session event: {event_id}")
            event = _event(row)
            if event.handling_status is EventHandlingStatus.HANDLED:
                if event.decision_id != decision_id:
                    raise SessionStateConflict("event is already handled by a different decision")
                return event
            if event.handling_status not in {EventHandlingStatus.CLASSIFIED, EventHandlingStatus.RECEIVED}:
                raise SessionStateConflict("event cannot transition to HANDLED")
            connection.execute(
                "UPDATE session_events SET handling_status=?, decision_id=?, handled_at=? WHERE event_id=?",
                (EventHandlingStatus.HANDLED.value, decision_id, now, event_id),
            )
            return _event(connection.execute(
                "SELECT * FROM session_events WHERE event_id=?", (event_id,)
            ).fetchone())

    def record_event_routing(
        self, event_id: str, routing: Mapping[str, Any]
    ) -> SessionEvent:
        """Persist the deterministic R7 route explanation on its inbox fact."""

        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM session_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown session event: {event_id}")
            event = _event(row)
            payload = dict(event.data)
            existing = payload.get("routing")
            candidate = dict(routing)
            if existing is not None and existing != candidate:
                raise SessionStateConflict(
                    "event routing classification cannot be replaced"
                )
            payload["routing"] = candidate
            if candidate.get("urgency") is not None:
                payload["urgency"] = str(candidate["urgency"])
            connection.execute(
                "UPDATE session_events SET data_json=?, classified_at=COALESCE(classified_at, ?), "
                "handling_status=CASE WHEN handling_status='RECEIVED' THEN 'CLASSIFIED' ELSE handling_status END "
                "WHERE event_id=?",
                (_json(payload), now, event_id),
            )
            return _event(
                connection.execute(
                    "SELECT * FROM session_events WHERE event_id=?", (event_id,)
                ).fetchone()
            )

    def raw_observations(self, session_id: str) -> list[RawObservation]:
        """Project R7 raw device facts without creating learning objects."""

        self.initialize()
        event_types = tuple(item.value for item in _RAW_OBSERVATION_EVENT_TYPES)
        placeholders = ",".join("?" for _ in event_types)
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                f"SELECT * FROM session_events WHERE session_id=? AND event_type IN ({placeholders}) "
                "ORDER BY cursor",
                (session_id, *event_types),
            ).fetchall()
            return [_raw_observation(_event(row)) for row in rows]

    def claim_active_slice(
        self,
        session_id: str,
        *,
        goal_id: str,
        attention_decision_id: str,
        slice_id: str,
    ) -> AgentSession:
        """Fence the one R7 running Slice selected by current authority."""

        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            session = self._session(connection, session_id)
            if session.control_mode is not SessionControlMode.AGENT_ACTIVE:
                raise SessionStateConflict("Session control mode blocks ActivitySlice start")
            if session.active_goal_id != goal_id:
                raise SessionStateConflict("ActivitySlice goal is not the selected Goal")
            if session.current_attention_decision_id != attention_decision_id:
                raise SessionStateConflict(
                    "ActivitySlice decision is not the current AttentionDecision"
                )
            if session.active_slice_id not in {None, slice_id}:
                raise SessionStateConflict("Session already owns another running ActivitySlice")
            connection.execute(
                "UPDATE agent_sessions SET active_slice_id=?, updated_at=? WHERE session_id=?",
                (slice_id, now, session_id),
            )
            return self._session(connection, session_id)

    def release_active_slice(self, session_id: str, *, slice_id: str) -> AgentSession:
        """Idempotently release only the Slice currently owned by the Session."""

        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            session = self._session(connection, session_id)
            if session.active_slice_id is None:
                return session
            if session.active_slice_id != slice_id:
                raise SessionStateConflict("cannot release a different active ActivitySlice")
            connection.execute(
                "UPDATE agent_sessions SET active_slice_id=NULL, updated_at=? WHERE session_id=?",
                (now, session_id),
            )
            return self._session(connection, session_id)

    def create_preemption_request(
        self, event_id: str, *, reason: str
    ) -> tuple[PreemptionRequest, bool]:
        """Record an EventInbox-triggered request without changing attention."""

        self.initialize()
        reason = reason.strip()
        if not reason:
            raise ValueError("preemption reason must not be blank")
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            event_row = connection.execute(
                "SELECT * FROM session_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if event_row is None:
                raise KeyError(f"unknown session event: {event_id}")
            event = _event(event_row)
            existing = connection.execute(
                "SELECT * FROM preemption_requests WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                request = _preemption_request(existing)
                if request.reason != reason:
                    raise SessionStateConflict(
                        "preemption request reason cannot be replaced"
                    )
                return request, False
            session = self._session(connection, event.session_id)
            if session.active_goal_id is None or session.active_slice_id is None:
                raise SessionStateConflict(
                    "preemption request requires a selected Goal and running Slice"
                )
            request_id = str(uuid.uuid4())
            key = f"event:{event.id}:preempt:{session.active_slice_id}"
            connection.execute(
                "INSERT INTO preemption_requests("
                "preemption_request_id,session_id,event_id,prior_goal_id,prior_slice_id,"
                "status,reason,idempotency_key,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    session.id,
                    event.id,
                    session.active_goal_id,
                    session.active_slice_id,
                    PreemptionRequestStatus.PENDING_CHECKPOINT.value,
                    reason,
                    key,
                    now,
                    now,
                ),
            )
            return (
                _preemption_request(
                    connection.execute(
                        "SELECT * FROM preemption_requests WHERE preemption_request_id=?",
                        (request_id,),
                    ).fetchone()
                ),
                True,
            )

    def pending_preemption_requests(
        self, session_id: str | None = None
    ) -> list[PreemptionRequest]:
        self.initialize()
        with self._connection() as connection:
            sql = (
                "SELECT * FROM preemption_requests WHERE status IN "
                "('PENDING_CHECKPOINT','CHECKPOINTED')"
            )
            args: tuple[Any, ...] = ()
            if session_id is not None:
                self._session(connection, session_id)
                sql += " AND session_id=?"
                args = (session_id,)
            rows = connection.execute(
                sql + " ORDER BY created_at,preemption_request_id", args
            ).fetchall()
            return [_preemption_request(row) for row in rows]

    def preemption_request_for_event(
        self, event_id: str
    ) -> PreemptionRequest | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM preemption_requests WHERE event_id=?", (event_id,)
            ).fetchone()
            return _preemption_request(row) if row is not None else None

    def checkpoint_preemption_requests(
        self,
        session_id: str,
        *,
        slice_id: str,
        observed_event_cursor: int,
        checkpoint_ref: str,
        continuation_id: str | None = None,
    ) -> tuple[PreemptionRequest, ...]:
        """Close the ActivitySlice DB -> AgentRuntime DB checkpoint saga."""

        self.initialize()
        checkpoint_ref = checkpoint_ref.strip()
        if not checkpoint_ref:
            raise ValueError("preemption checkpoint_ref must not be blank")
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            session = self._session(connection, session_id)
            if session.active_slice_id != slice_id:
                raise SessionStateConflict(
                    "preemption checkpoint does not own the active ActivitySlice"
                )
            rows = connection.execute(
                "SELECT p.* FROM preemption_requests p "
                "JOIN session_events e ON e.event_id=p.event_id "
                "WHERE p.session_id=? AND p.prior_slice_id=? "
                "AND p.status='PENDING_CHECKPOINT' AND e.cursor<=? "
                "ORDER BY e.cursor,p.preemption_request_id",
                (session_id, slice_id, observed_event_cursor),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE preemption_requests SET status=?,checkpoint_ref=?,"
                    "continuation_id=?,updated_at=? WHERE preemption_request_id=?",
                    (
                        PreemptionRequestStatus.CHECKPOINTED.value,
                        checkpoint_ref,
                        continuation_id,
                        now,
                        row["preemption_request_id"],
                    ),
                )
            return tuple(
                _preemption_request(
                    connection.execute(
                        "SELECT * FROM preemption_requests WHERE preemption_request_id=?",
                        (row["preemption_request_id"],),
                    ).fetchone()
                )
                for row in rows
            )

    def settle_preemption_request(
        self, event_id: str, *, attention_decision_id: str
    ) -> PreemptionRequest | None:
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM preemption_requests WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            request = _preemption_request(row)
            if request.status is PreemptionRequestStatus.DECIDED:
                if request.attention_decision_id != attention_decision_id:
                    raise SessionStateConflict(
                        "preemption request is already settled by another decision"
                    )
                return request
            if request.status is not PreemptionRequestStatus.CHECKPOINTED:
                raise SessionStateConflict(
                    "preemption requires a verified checkpoint before decision"
                )
            connection.execute(
                "UPDATE preemption_requests SET status=?,attention_decision_id=?,updated_at=? "
                "WHERE preemption_request_id=?",
                (
                    PreemptionRequestStatus.DECIDED.value,
                    attention_decision_id,
                    now,
                    request.id,
                ),
            )
            return _preemption_request(
                connection.execute(
                    "SELECT * FROM preemption_requests WHERE preemption_request_id=?",
                    (request.id,),
                ).fetchone()
            )

    def transition_control_mode(
        self,
        session_id: str,
        *,
        event_id: str,
        to_mode: SessionControlMode,
        reason: str,
        idempotency_key: str,
        expected_from: frozenset[SessionControlMode] | None = None,
        fresh_observation_ref: str | None = None,
    ) -> tuple[SessionControlTransition, bool]:
        """Persist one R7 human-control transition and Session projection."""

        self.initialize()
        reason = reason.strip()
        idempotency_key = idempotency_key.strip()
        if not reason or not idempotency_key:
            raise ValueError("control transition reason/key must not be blank")
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM session_control_transitions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                transition = _control_transition(existing)
                if (
                    transition.session_id != session_id
                    or transition.event_id != event_id
                    or transition.to_mode is not to_mode
                    or transition.fresh_observation_ref != fresh_observation_ref
                ):
                    raise SessionStateConflict(
                        "control transition idempotency key cannot be reused"
                    )
                return transition, False
            event = connection.execute(
                "SELECT session_id FROM session_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if event is None or str(event["session_id"]) != session_id:
                raise SessionStateConflict(
                    "control transition event does not belong to Session"
                )
            session = self._session(connection, session_id)
            if session.terminal:
                raise SessionStateConflict("terminal Session cannot change control mode")
            if expected_from is not None and session.control_mode not in expected_from:
                raise SessionStateConflict(
                    f"control mode {session.control_mode.value} cannot transition to {to_mode.value}"
                )
            if (
                session.control_mode is SessionControlMode.RECOVERING_CONTEXT
                and to_mode is SessionControlMode.AGENT_ACTIVE
                and not fresh_observation_ref
            ):
                raise SessionStateConflict(
                    "RECOVERING_CONTEXT requires a fresh observation before AGENT_ACTIVE"
                )
            transition_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO session_control_transitions("
                "transition_id,session_id,event_id,from_mode,to_mode,reason,"
                "idempotency_key,fresh_observation_ref,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    transition_id,
                    session_id,
                    event_id,
                    session.control_mode.value,
                    to_mode.value,
                    reason,
                    idempotency_key,
                    fresh_observation_ref,
                    now,
                ),
            )
            next_status = (
                SessionStatus.USER_ACTIVE.value
                if to_mode is SessionControlMode.USER_ACTIVE
                else (
                    SessionStatus.ACTIVE.value
                    if to_mode
                    in {
                        SessionControlMode.AGENT_ACTIVE,
                        SessionControlMode.RECOVERING_CONTEXT,
                    }
                    else session.status.value
                )
            )
            connection.execute(
                "UPDATE agent_sessions SET control_mode=?,status=?,updated_at=? WHERE session_id=?",
                (to_mode.value, next_status, now, session_id),
            )
            return (
                _control_transition(
                    connection.execute(
                        "SELECT * FROM session_control_transitions WHERE transition_id=?",
                        (transition_id,),
                    ).fetchone()
                ),
                True,
            )

    def control_transitions(self, session_id: str) -> list[SessionControlTransition]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM session_control_transitions WHERE session_id=? "
                "ORDER BY created_at,transition_id",
                (session_id,),
            ).fetchall()
            return [_control_transition(row) for row in rows]

    def create_wake_condition(
        self, session_id: str, goal_id: str, draft: WakeConditionDraft
    ) -> WakeCondition:
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            self._owned_goal(connection, session_id, goal_id)
            return self._create_wake_condition(connection, session_id, goal_id, draft)

    def enter_goal_waiting(
        self,
        session_id: str,
        goal_id: str,
        continuation_draft: ContinuationDraft,
        wake_draft: WakeConditionDraft,
    ) -> tuple[Continuation, WakeCondition]:
        """Atomically persist checkpoint, wake condition and Goal wait state."""

        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            goal = self._owned_goal(connection, session_id, goal_id)
            if GoalNodeStatus(str(goal["status"])).terminal:
                raise SessionStateConflict("terminal Goal cannot enter waiting")
            continuation, _ = self._append_continuation(
                connection, session_id, goal_id, continuation_draft
            )
            wake = self._create_wake_condition(
                connection,
                session_id,
                goal_id,
                wake_draft,
                waiting_kind=continuation_draft.waiting_kind,
            )
            return continuation, wake

    def wake_conditions(self, session_id: str, *, pending_only: bool = False) -> list[WakeCondition]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            sql = "SELECT * FROM wake_conditions WHERE session_id=?"
            if pending_only:
                sql += " AND status='PENDING'"
            rows = connection.execute(sql + " ORDER BY created_at, wake_condition_id", (session_id,)).fetchall()
            return [_wake_condition(row) for row in rows]

    def due_time_wake_conditions(self, *, now: datetime) -> tuple[WakeCondition, ...]:
        """Return due TIME wakes without changing Goal or EventInbox state."""

        self.initialize()
        clock = now.astimezone(UTC)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM wake_conditions WHERE status='PENDING' AND kind='TIME' "
                "AND due_at IS NOT NULL ORDER BY due_at,wake_condition_id"
            ).fetchall()
        due: list[WakeCondition] = []
        for row in rows:
            wake = _wake_condition(row)
            parsed = datetime.fromisoformat(
                str(wake.due_at).replace("Z", "+00:00")
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed.astimezone(UTC) <= clock:
                due.append(wake)
        return tuple(due)

    def wake_due_time_conditions(self, *, now: datetime) -> tuple[str, ...]:
        """Materialize and classify due TIME wakes as EventInbox facts.

        The stable source identity makes launcher retries converge on the same
        ``TimerDueEvent``.  Exact wake matching remains owned by
        :meth:`route_inbox_event`; time passing never edits Goal state around
        the inbox authority.  Normal runtime composition calls
        ``AgentSessionService.poll_due_time_events`` so the same durable event
        also reaches AttentionDecision handling without requiring a restart.
        """

        clock = now.astimezone(UTC)
        changed_sessions: set[str] = set()
        occurred_at = clock.isoformat()
        for wake in self.due_time_wake_conditions(now=clock):
            event, _created = self.ingest_event(
                wake.session_id,
                source_namespace="agent-runtime-timer",
                source_event_id=f"wake:{wake.id}:due:{wake.due_at}",
                event_type=SessionEventType.TIMER_DUE,
                occurred_at=occurred_at,
                payload={
                    "wake_condition_id": wake.id,
                    "due_at": wake.due_at,
                    "goal_id": wake.goal_id,
                    "urgency": "HIGH",
                },
            )
            routed, matched = self.route_inbox_event(event.id)
            from .event_router import classify_event_routing

            classification = classify_event_routing(routed, matched)
            self.record_event_routing(event.id, classification.to_payload())
            if matched:
                changed_sessions.add(wake.session_id)
        return tuple(sorted(changed_sessions))

    def set_goal_ready(self, session_id: str, goal_id: str) -> GoalNode:
        """Make one owned nonterminal Goal READY and retire its old wait."""

        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            row = self._owned_goal(connection, session_id, goal_id)
            if GoalNodeStatus(str(row["status"])).terminal:
                raise SessionStateConflict("terminal Goal cannot become READY")
            connection.execute(
                "UPDATE wake_conditions SET status=?, superseded_at=? "
                "WHERE goal_node_id=? AND status='PENDING'",
                (WakeConditionStatus.SUPERSEDED.value, now, goal_id),
            )
            connection.execute(
                "UPDATE goal_nodes SET status=?, waiting_kind=NULL, waiting_ref=NULL, "
                "next_eligible_at=NULL, backoff_until=NULL, updated_at=? WHERE goal_node_id=?",
                (GoalNodeStatus.READY.value, now, goal_id),
            )
            self._advance_agenda_revision(connection, session_id, now)
            return _goal_node(connection.execute(
                "SELECT * FROM goal_nodes WHERE goal_node_id=?", (goal_id,)
            ).fetchone())

    def append_continuation(
        self, session_id: str, goal_id: str, draft: ContinuationDraft
    ) -> tuple[Continuation, bool]:
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            self._owned_goal(connection, session_id, goal_id)
            return self._append_continuation(connection, session_id, goal_id, draft)

    def continuations(self, goal_id: str) -> list[Continuation]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM goal_continuations WHERE goal_node_id=? ORDER BY revision", (goal_id,)
            ).fetchall()
            return [_continuation(row) for row in rows]

    def latest_continuation(self, goal_id: str) -> Continuation | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM goal_continuations WHERE goal_node_id=? ORDER BY revision DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
            return _continuation(row) if row is not None else None

    def commit_attention_decision(
        self,
        session_id: str,
        *,
        draft: AttentionDecisionDraft,
        candidates: Sequence[GoalEligibilityDraft],
        previous_continuation: tuple[str, ContinuationDraft] | None = None,
    ) -> tuple[AttentionDecision, bool, AttentionDispatch | None]:
        """Atomically persist decision, candidates, preemption checkpoint and dispatch."""

        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            session = self._session(connection, session_id)
            existing = connection.execute(
                "SELECT * FROM attention_decisions WHERE session_id=? AND trigger_key=?",
                (session_id, draft.trigger_key),
            ).fetchone()
            if existing is not None:
                decision = _attention_decision(existing)
                dispatch_row = connection.execute(
                    "SELECT * FROM attention_dispatches WHERE decision_id=?", (decision.id,)
                ).fetchone()
                return decision, False, _attention_dispatch(dispatch_row) if dispatch_row is not None else None
            graph_row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS value FROM goal_graph_revisions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if (
                session.authority_revision != draft.authority_revision
                or int(graph_row["value"]) != draft.graph_revision
                or session.event_cursor != draft.event_cursor
                or (
                    draft.agenda_revision is not None
                    and session.agenda_revision != draft.agenda_revision
                )
            ):
                raise SessionStateConflict("attention snapshot is stale and must be recomputed")
            if draft.trigger_event_id is not None:
                trigger = connection.execute(
                    "SELECT * FROM session_events WHERE event_id=? AND session_id=?",
                    (draft.trigger_event_id, session_id),
                ).fetchone()
                if trigger is None:
                    raise ValueError("attention trigger event must belong to the Session")
            candidate_ids = [item.goal_id for item in candidates]
            if len(candidate_ids) != len(set(candidate_ids)):
                raise ValueError("attention candidates must contain unique Goal ids")
            for goal_id in candidate_ids:
                self._owned_goal(connection, session_id, goal_id)
            selected = draft.selected_goal_id
            if draft.outcome is AttentionDecisionOutcome.SELECTED:
                if selected is None:
                    raise ValueError("SELECTED attention outcome requires selected_goal_id")
                selected_candidate = next((item for item in candidates if item.goal_id == selected), None)
                if selected_candidate is None or selected_candidate.eligibility is not GoalEligibilityStatus.ELIGIBLE:
                    raise ValueError("selected Goal must be an eligible persisted candidate")
            elif selected is not None:
                raise ValueError("NO_ELIGIBLE attention outcome cannot select a Goal")

            decision_id = str(uuid.uuid4())
            revision = int(connection.execute(
                "SELECT COALESCE(MAX(decision_revision), 0) + 1 AS value FROM attention_decisions WHERE session_id=?",
                (session_id,),
            ).fetchone()["value"])
            connection.execute(
                "INSERT INTO attention_decisions(decision_id, session_id, decision_revision, trigger_event_id, trigger_key, "
                "authority_revision, graph_revision, event_cursor, agenda_revision, outcome, candidate_goal_ids_json, selected_goal_id, "
                "selector_kind, selected_hard_tier, reason, user_priority_component, event_urgency_component, "
                "waiting_age_component, starvation_component, continuity_component, app_switch_cost, backoff_component, "
                "recent_failure_component, base_score, model_adjustment, slice_time_budget_ms, slice_action_budget, "
                "checkpoint_policy, preemption_policy, preemption_checkpoint_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_id, session_id, revision, draft.trigger_event_id, draft.trigger_key,
                 draft.authority_revision, draft.graph_revision, draft.event_cursor, session.agenda_revision + 1, draft.outcome.value,
                 _json(candidate_ids), selected, draft.selector_kind.value, draft.selected_hard_tier,
                 draft.reason, draft.user_priority_component, draft.event_urgency_component,
                 draft.waiting_age_component, draft.starvation_component, draft.continuity_component,
                 draft.app_switch_cost, draft.backoff_component, draft.recent_failure_component,
                 draft.base_score, draft.model_adjustment, draft.slice_budget.time_budget_ms,
                 draft.slice_budget.action_budget, draft.slice_budget.checkpoint_policy,
                 draft.preemption_policy.value, draft.preemption_checkpoint_ref, now),
            )
            for item in candidates:
                connection.execute(
                    "INSERT INTO attention_candidates(decision_id, goal_node_id, eligibility, reason, goal_status, "
                    "scheduling_class, binding_status, wake_condition_id, next_eligible_at, continuation_revision, "
                    "latest_directive_target, matched_trigger_event, hard_tier, user_priority_component, "
                    "event_urgency_component, waiting_age_component, starvation_component, continuity_component, "
                    "app_switch_cost, backoff_component, recent_failure_component, total_score, rank) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (decision_id, item.goal_id, item.eligibility.value, item.reason, item.goal_status.value,
                     item.scheduling_class.value, item.binding_status, item.wake_condition_id,
                     item.next_eligible_at, item.continuation_revision, int(item.latest_directive_target),
                     int(item.matched_trigger_event), item.hard_tier, item.user_priority_component,
                     item.event_urgency_component, item.waiting_age_component, item.starvation_component,
                     item.continuity_component, item.app_switch_cost, item.backoff_component,
                     item.recent_failure_component, item.total_score, item.rank),
                )
            if previous_continuation is not None:
                previous_goal_id, continuation_draft = previous_continuation
                self._owned_goal(connection, session_id, previous_goal_id)
                continuation_draft = ContinuationDraft(
                    authority_revision=continuation_draft.authority_revision,
                    graph_revision=continuation_draft.graph_revision,
                    checkpoint_kind=continuation_draft.checkpoint_kind,
                    yield_reason=continuation_draft.yield_reason,
                    idempotency_key=continuation_draft.idempotency_key,
                    attention_decision_id=decision_id,
                    checkpoint_ref=continuation_draft.checkpoint_ref,
                    stage_id=continuation_draft.stage_id,
                    application_package=continuation_draft.application_package,
                    scene_ref=continuation_draft.scene_ref,
                    person_id=continuation_draft.person_id,
                    conversation_id=continuation_draft.conversation_id,
                    verified_fact_refs=continuation_draft.verified_fact_refs,
                    pending_intent=continuation_draft.pending_intent,
                    waiting_kind=continuation_draft.waiting_kind,
                    waiting_ref=continuation_draft.waiting_ref,
                    resume_preconditions=continuation_draft.resume_preconditions,
                    next_eligible_at=continuation_draft.next_eligible_at,
                )
                self._append_continuation(connection, session_id, previous_goal_id, continuation_draft)
                connection.execute(
                    "UPDATE goal_nodes SET status=CASE WHEN status='ACTIVE' THEN 'READY' ELSE status END, updated_at=? "
                    "WHERE goal_node_id=?", (now, previous_goal_id),
                )
            dispatch: AttentionDispatch | None = None
            if selected is not None:
                connection.execute(
                    "UPDATE goal_nodes SET status=?, last_selected_at=?, last_service_at=?, wait_started_at=NULL, "
                    "next_eligible_at=NULL, backoff_until=NULL, updated_at=? WHERE goal_node_id=?",
                    (GoalNodeStatus.ACTIVE.value, now, now, now, selected),
                )
                dispatch_id = str(uuid.uuid4())
                dispatch_key = f"attention:{decision_id}:activate:{selected}"
                connection.execute(
                    "INSERT INTO attention_dispatches(dispatch_id, session_id, decision_id, goal_node_id, status, "
                    "idempotency_key, attempt_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (dispatch_id, session_id, decision_id, selected,
                     AttentionDispatchStatus.PENDING.value, dispatch_key, now, now),
                )
                next_status = SessionStatus.ACTIVE
                dispatch = _attention_dispatch(connection.execute(
                    "SELECT * FROM attention_dispatches WHERE dispatch_id=?", (dispatch_id,)
                ).fetchone())
            else:
                next_status = SessionStatus.WAITING_ALL
            connection.execute(
                "UPDATE agent_sessions SET active_goal_id=?, current_attention_decision_id=?, status=?, updated_at=? "
                "WHERE session_id=?",
                (selected, decision_id, next_status.value, now, session_id),
            )
            self._advance_agenda_revision(connection, session_id, now)
            self._append_event(
                connection, session_id, SessionEventType.ATTENTION_DECISION_COMMITTED,
                {"decision_id": decision_id, "decision_revision": revision, "selected_goal_id": selected,
                 "trigger_key": draft.trigger_key},
                f"attention:{decision_id}:committed", now,
            )
            if draft.trigger_event_id is not None:
                connection.execute(
                    "UPDATE session_events SET handling_status=?, decision_id=?, handled_at=? WHERE event_id=?",
                    (EventHandlingStatus.HANDLED.value, decision_id, now, draft.trigger_event_id),
                )
            connection.execute(
                "UPDATE attention_decisions SET agenda_revision=("
                "SELECT agenda_revision FROM agent_sessions WHERE session_id=?"
                ") WHERE decision_id=?",
                (session_id, decision_id),
            )
            decision = _attention_decision(connection.execute(
                "SELECT * FROM attention_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone())
            return decision, True, dispatch

    def latest_attention_decision(self, session_id: str) -> AttentionDecision | None:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            row = connection.execute(
                "SELECT * FROM attention_decisions WHERE session_id=? ORDER BY decision_revision DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return _attention_decision(row) if row is not None else None

    def attention_decision_for_trigger(
        self, session_id: str, trigger_key: str
    ) -> AttentionDecision | None:
        """Return a committed trigger result before any new planning work."""

        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            row = connection.execute(
                "SELECT * FROM attention_decisions WHERE session_id=? AND trigger_key=?",
                (session_id, trigger_key),
            ).fetchone()
            return _attention_decision(row) if row is not None else None

    def attention_decision(self, decision_id: str) -> AttentionDecision | None:
        """Read one immutable AttentionDecision by its durable identity."""

        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM attention_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            return _attention_decision(row) if row is not None else None

    def attention_candidates(self, decision_id: str) -> list[GoalEligibility]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM attention_candidates WHERE decision_id=? "
                "ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank, goal_node_id", (decision_id,),
            ).fetchall()
            return [_goal_eligibility(row) for row in rows]

    def pending_attention_dispatches(
        self, session_id: str | None = None, *, limit: int | None = None
    ) -> list[AttentionDispatch]:
        self.initialize()
        with self._connection() as connection:
            # RETRYABLE remains visible to recovery as an unsettled outcome,
            # but the service must fence it against the persisted Goal
            # eligibility before calling the owner again.
            sql = "SELECT * FROM attention_dispatches WHERE status IN ('PENDING', 'PROCESSING', 'RETRYABLE')"
            args: tuple[Any, ...] = ()
            if session_id is not None:
                self._session(connection, session_id)
                sql += " AND session_id=?"
                args = (session_id,)
            sql += " ORDER BY created_at, dispatch_id"
            if limit is not None:
                sql += " LIMIT ?"
                args = (*args, limit)
            rows = connection.execute(sql, args).fetchall()
            return [_attention_dispatch(row) for row in rows]

    def mark_attention_dispatch_processing(self, dispatch_id: str) -> AttentionDispatch:
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            row = self._attention_dispatch_row(connection, dispatch_id)
            if row["status"] == AttentionDispatchStatus.DELIVERED.value:
                return _attention_dispatch(row)
            connection.execute(
                "UPDATE attention_dispatches SET status=?, attempt_count=attempt_count+1, updated_at=?, last_error=NULL "
                "WHERE dispatch_id=?",
                (AttentionDispatchStatus.PROCESSING.value, now, dispatch_id),
            )
            return _attention_dispatch(self._attention_dispatch_row(connection, dispatch_id))

    def settle_attention_dispatch(self, dispatch_id: str) -> AttentionDispatch:
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            dispatch = _attention_dispatch(self._attention_dispatch_row(connection, dispatch_id))
            connection.execute(
                "UPDATE attention_dispatches SET status=?, delivered_at=COALESCE(delivered_at, ?), "
                "updated_at=?, last_error=NULL WHERE dispatch_id=?",
                (AttentionDispatchStatus.DELIVERED.value, now, now, dispatch_id),
            )
            connection.execute(
                "UPDATE goal_nodes SET consecutive_failure_count=0, backoff_until=NULL, "
                "next_eligible_at=NULL, wait_started_at=NULL, updated_at=? WHERE goal_node_id=?",
                (now, dispatch.goal_id),
            )
            return _attention_dispatch(self._attention_dispatch_row(connection, dispatch_id))

    def fail_attention_dispatch(
        self, dispatch_id: str, error: str, *, retryable: bool = False
    ) -> AttentionDispatch:
        self.initialize()
        now = utc_now()
        status = AttentionDispatchStatus.RETRYABLE if retryable else AttentionDispatchStatus.FAILED
        with self._lock, self._connection(write=True) as connection:
            self._attention_dispatch_row(connection, dispatch_id)
            connection.execute(
                "UPDATE attention_dispatches SET status=?, updated_at=?, last_error=? WHERE dispatch_id=?",
                (status.value, now, error, dispatch_id),
            )
            return _attention_dispatch(self._attention_dispatch_row(connection, dispatch_id))

    def yield_attention_dispatch_device_busy(
        self,
        dispatch_id: str,
        continuation_draft: ContinuationDraft,
        wake_draft: WakeConditionDraft,
        *,
        occurred_at: str,
        retry_at: str,
        error: str = "selected Goal yielded retryable device busy",
    ) -> tuple[AttentionDispatch, SessionEvent, Continuation, WakeCondition]:
        """Atomically yield a busy dispatch back to persistent scheduling.

        The RETRYABLE dispatch is an outcome/audit fact, not permission to
        bypass the scheduler.  A stable pending DeviceBusyEvent drives the
        replacement decision after this transaction, including after a crash.
        """

        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            dispatch_row = self._attention_dispatch_row(connection, dispatch_id)
            dispatch = _attention_dispatch(dispatch_row)
            goal = self._owned_goal(connection, dispatch.session_id, dispatch.goal_id)
            if GoalNodeStatus(str(goal["status"])).terminal:
                raise SessionStateConflict("terminal Goal cannot yield device busy")
            if wake_draft.kind is not WakeConditionKind.DEVICE:
                raise ValueError("device-busy yield requires a DEVICE WakeCondition")

            source_namespace = "attention-dispatch"
            source_event_id = f"dispatch:{dispatch.id}:device-busy"
            event_id = source_event_id
            payload = {
                "dispatch_id": dispatch.id,
                "decision_id": dispatch.decision_id,
                "goal_id": dispatch.goal_id,
                "status": GoalNodeStatus.WAITING_DEVICE.value,
                "retry_at": retry_at,
                "error": error,
            }
            digest = _digest({
                "source_namespace": source_namespace,
                "source_event_id": source_event_id,
                "event_type": SessionEventType.DEVICE_BUSY.value,
                "occurred_at": occurred_at,
                "payload": payload,
                "device_id": None,
                "device_boot_id": None,
                "source_cursor": None,
            })
            event_row = connection.execute(
                "SELECT * FROM session_events WHERE session_id=? AND source_namespace=? AND source_event_id=?",
                (dispatch.session_id, source_namespace, source_event_id),
            ).fetchone()
            if event_row is not None and str(event_row["payload_digest"]) != digest:
                raise SessionIdempotencyConflict(
                    "同一 device-busy dispatch 事件已用于不同内容。"
                )
            event_created = event_row is None
            if event_created:
                cursor = connection.execute(
                    "INSERT INTO session_events(event_id, session_id, event_type, data_json, idempotency_key, "
                    "handling_status, created_at, handled_at, source_namespace, source_event_id, occurred_at, "
                    "received_at, affected_goal_ids_json, payload_digest) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, '[]', ?)",
                    (event_id, dispatch.session_id, SessionEventType.DEVICE_BUSY.value,
                     _json(payload), source_event_id, EventHandlingStatus.RECEIVED.value,
                     now, source_namespace, source_event_id, occurred_at, now, digest),
                ).lastrowid
                connection.execute(
                    "UPDATE agent_sessions SET event_cursor=?, updated_at=? WHERE session_id=?",
                    (cursor, now, dispatch.session_id),
                )
                event_row = connection.execute(
                    "SELECT * FROM session_events WHERE cursor=?", (cursor,)
                ).fetchone()

            continuation_draft = replace(
                continuation_draft,
                attention_decision_id=dispatch.decision_id,
                waiting_kind=WakeConditionKind.DEVICE.value,
                waiting_ref=event_id,
                next_eligible_at=retry_at,
            )
            wake_draft = replace(
                wake_draft,
                kind=WakeConditionKind.DEVICE,
                due_at=retry_at,
                created_by_event_id=event_id,
                created_by_decision_id=dispatch.decision_id,
            )
            continuation, _ = self._append_continuation(
                connection, dispatch.session_id, dispatch.goal_id, continuation_draft
            )
            wake = self._create_wake_condition(
                connection, dispatch.session_id, dispatch.goal_id, wake_draft
            )
            connection.execute(
                "UPDATE goal_nodes SET backoff_until=?, "
                "consecutive_failure_count=consecutive_failure_count+? WHERE goal_node_id=?",
                (retry_at, int(event_created), dispatch.goal_id),
            )
            self._advance_agenda_revision(connection, dispatch.session_id, now)
            connection.execute(
                "UPDATE attention_dispatches SET status=?, updated_at=?, last_error=? WHERE dispatch_id=?",
                (AttentionDispatchStatus.RETRYABLE.value, now, error, dispatch.id),
            )
            connection.execute(
                "UPDATE agent_sessions SET active_goal_id=CASE WHEN active_goal_id=? THEN NULL ELSE active_goal_id END, "
                "updated_at=? WHERE session_id=?",
                (dispatch.goal_id, now, dispatch.session_id),
            )
            return (
                _attention_dispatch(self._attention_dispatch_row(connection, dispatch.id)),
                _event(event_row),
                continuation,
                wake,
            )

    def record_message(
        self, session_id: str, *, content: str, client_request_id: str
    ) -> tuple[UserDirective, bool]:
        return self._record_directive(
            session_id, content=content, client_request_id=client_request_id, kind=DirectiveKind.ADD
        )

    def record_directive(
        self, session_id: str, *, content: str, client_request_id: str, kind: DirectiveKind
    ) -> tuple[UserDirective, bool]:
        if kind is DirectiveKind.ORIGINAL:
            raise ValueError("an original directive is created only with its AgentSession")
        return self._record_directive(
            session_id, content=content, client_request_id=client_request_id, kind=kind
        )

    def record_stop_directive(
        self, session_id: str, *, client_request_id: str
    ) -> tuple[UserDirective, bool]:
        return self._record_directive(
            session_id, content="用户请求停止此 Session。", client_request_id=client_request_id,
            kind=DirectiveKind.STOP,
        )

    def record_control(
        self, session_id: str, *, action: str, client_request_id: str
    ) -> bool:
        self.initialize()
        digest = _digest({"action": action})
        now = utc_now()
        scope = f"control:{session_id}"
        with self._lock, self._connection(write=True) as connection:
            session = self._session(connection, session_id)
            existing = connection.execute(
                "SELECT request_digest FROM session_requests WHERE scope=? AND request_key=?",
                (scope, client_request_id),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise SessionIdempotencyConflict("同一 client_request_id 已用于不同的 Session 控制请求。")
                return False
            connection.execute(
                "INSERT INTO session_requests(scope, request_key, request_digest, session_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (scope, client_request_id, digest, session_id, now),
            )
            status, mode = _control_state(session, action)
            connection.execute(
                "UPDATE agent_sessions SET status=?, control_mode=?, updated_at=? "
                "WHERE session_id=?", (status.value, mode.value, now, session_id),
            )
            self._append_event(connection, session_id, SessionEventType.CONTROL_REQUESTED,
                               {"action": action}, f"control:{client_request_id}:requested", now)
            return True

    def project_goal_run(self, session_id: str, goal_run: Any) -> AgentSession:
        """Persist a changed GoalRun compatibility projection exactly once."""

        self.initialize()
        now = utc_now()
        goal_run_id = _goal_run_id(goal_run)
        execution_status = _value(goal_run, "execution_status")
        with self._lock, self._connection(write=True) as connection:
            session = self._session(connection, session_id)
            binding = connection.execute(
                "SELECT * FROM session_goal_bindings WHERE session_id=? AND goal_run_id=? "
                "AND status='BOUND'", (session_id, goal_run_id),
            ).fetchone()
            if binding is None:
                return session
            node = connection.execute(
                "SELECT * FROM goal_nodes WHERE goal_node_id=?", (binding["goal_node_id"],)
            ).fetchone()
            # A terminal/verified GoalNode is an immutable Session fact.  A
            # stale compatibility GoalRun projection cannot reopen or erase it.
            if GoalNodeStatus(str(node["status"])).terminal or node["verified_result_ref"]:
                return session
            target_node = _goal_node_status(execution_status)
            sibling_statuses = [
                GoalNodeStatus(str(row["status"]))
                for row in connection.execute(
                    "SELECT status FROM goal_nodes WHERE session_id=? AND goal_node_id<>?", (session_id, node["goal_node_id"])
                ).fetchall()
            ]
            target_session = _session_status_for_goal(session.status, target_node, sibling_statuses)
            if node["status"] == target_node.value and target_session is session.status:
                return session
            connection.execute(
                "UPDATE goal_nodes SET status=?, updated_at=? WHERE goal_node_id=?",
                (target_node.value, now, node["goal_node_id"]),
            )
            connection.execute(
                "UPDATE agent_sessions SET status=?, updated_at=? WHERE session_id=?",
                (target_session.value, now, session_id),
            )
            self._append_event(
                connection, session_id, SessionEventType.GOAL_PROJECTION_CHANGED,
                {"goal_node_id": node["goal_node_id"], "goal_run_id": goal_run_id,
                 "execution_status": execution_status, "goal_node_status": target_node.value,
                 "session_status": target_session.value},
                f"goal-projection:{goal_run_id}:{execution_status}", now,
            )
            return self._session(connection, session_id)

    def stopping_sessions(self, *, limit: int | None = None) -> list[tuple[AgentSession, str]]:
        """Return durable stop requests that have not reached STOPPED yet."""

        self.initialize()
        with self._connection() as connection:
            sql = (
                "SELECT s.*, e.idempotency_key FROM agent_sessions s "
                "JOIN session_events e ON e.session_id=s.session_id "
                "WHERE s.status='STOPPING' AND e.event_type=? ORDER BY e.cursor DESC"
            )
            args: tuple[Any, ...] = (SessionEventType.CONTROL_REQUESTED.value,)
            if limit is not None:
                sql += " LIMIT ?"
                args = (*args, limit)
            rows = connection.execute(sql, args).fetchall()
            result: list[tuple[AgentSession, str]] = []
            seen: set[str] = set()
            for row in rows:
                session_id = str(row["session_id"])
                if session_id in seen:
                    continue
                key = str(row["idempotency_key"])
                if not key.startswith("control:") or not key.endswith(":requested"):
                    continue
                # ``control:<client_request_id>:requested`` is inert and its
                # client key has the same character contract as the API.
                seen.add(session_id)
                result.append((_agent_session(row), key[len("control:"):-len(":requested")]))
            return result

    def mark_outbox_processing(self, intent_id: str) -> DurableOutboxIntent:
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            row = self._outbox(connection, intent_id)
            if row["status"] == OutboxIntentStatus.DELIVERED.value:
                return _outbox(row)
            connection.execute(
                "UPDATE agent_outbox SET status=?, attempt_count=attempt_count+1, updated_at=?, last_error=NULL "
                "WHERE intent_id=?", (OutboxIntentStatus.PROCESSING.value, now, intent_id),
            )
            return _outbox(self._outbox(connection, intent_id))

    def pending_outbox(self, *, limit: int | None = None) -> list[DurableOutboxIntent]:
        self.initialize()
        with self._connection() as connection:
            sql = (
                "SELECT * FROM agent_outbox WHERE status IN ('PENDING', 'PROCESSING', 'FAILED') "
                "ORDER BY created_at, intent_id"
            )
            rows = (
                connection.execute(sql + " LIMIT ?", (limit,)).fetchall()
                if limit is not None
                else connection.execute(sql).fetchall()
            )
            return [_outbox(row) for row in rows]

    def settle_goal_run(self, intent_id: str, *, goal_run_id: str) -> SessionGoalBinding:
        """Atomically bind one GoalRun and settle the originating outbox intent."""

        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            intent = self._outbox(connection, intent_id)
            existing = connection.execute(
                "SELECT * FROM session_goal_bindings WHERE goal_node_id=? "
                "AND status IN ('REQUESTED', 'BOUND')", (intent["goal_node_id"],),
            ).fetchone()
            if existing is not None:
                binding = _binding(existing)
                if binding.goal_run_id != goal_run_id:
                    raise RuntimeError("goal node is already bound to a different GoalRun")
            else:
                binding_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO session_goal_bindings("
                    "binding_id, session_id, goal_node_id, goal_run_id, status, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (binding_id, intent["session_id"], intent["goal_node_id"], goal_run_id,
                     GoalBindingStatus.BOUND.value, now, now),
                )
                connection.execute(
                    "UPDATE goal_nodes SET bound_goal_run_id=?, "
                    "status=CASE WHEN status=? THEN ? ELSE status END, updated_at=? "
                    "WHERE goal_node_id=?",
                    (
                        goal_run_id,
                        GoalNodeStatus.PLANNED.value,
                        GoalNodeStatus.READY.value,
                        now,
                        intent["goal_node_id"],
                    ),
                )
                self._advance_agenda_revision(connection, str(intent["session_id"]), now)
                session = self._session(connection, str(intent["session_id"]))
                next_status = (
                    SessionStatus.ACTIVE if session.status is SessionStatus.PLANNING else session.status
                )
                connection.execute(
                    "UPDATE agent_sessions SET status=?, updated_at=? WHERE session_id=?",
                    (next_status.value, now, intent["session_id"]),
                )
                self._append_event(connection, str(intent["session_id"]), SessionEventType.GOAL_RUN_BOUND,
                                   {"goal_node_id": intent["goal_node_id"], "goal_run_id": goal_run_id},
                                   f"outbox:{intent_id}:bound", now)
                binding = _binding(connection.execute(
                    "SELECT * FROM session_goal_bindings WHERE binding_id=?", (binding_id,)
                ).fetchone())
            connection.execute(
                "UPDATE agent_outbox SET status=?, delivered_at=COALESCE(delivered_at, ?), updated_at=?, last_error=NULL "
                "WHERE intent_id=?", (OutboxIntentStatus.DELIVERED.value, now, now, intent_id),
            )
            return binding

    def fail_outbox(self, intent_id: str, error: str) -> None:
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            self._outbox(connection, intent_id)
            connection.execute(
                "UPDATE agent_outbox SET status=?, last_error=?, updated_at=? WHERE intent_id=?",
                (OutboxIntentStatus.FAILED.value, error[:1000], now, intent_id),
            )

    def settle_control(self, session_id: str, *, action: str, client_request_id: str) -> AgentSession:
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            session = self._session(connection, session_id)
            if action == "stop":
                status, mode, stopped_at = SessionStatus.STOPPED, SessionControlMode.STOPPED, now
                event_type = SessionEventType.SESSION_STOPPED
            else:
                status, mode, stopped_at = session.status, session.control_mode, None
                event_type = SessionEventType.CONTROL_SETTLED
            connection.execute(
                "UPDATE agent_sessions SET status=?, control_mode=?, stopped_at=COALESCE(?, stopped_at), updated_at=? "
                "WHERE session_id=?", (status.value, mode.value, stopped_at, now, session_id),
            )
            self._append_event(connection, session_id, event_type, {"action": action},
                               f"control:{client_request_id}:settled", now)
            return self._session(connection, session_id)

    def graph_revisions(self, session_id: str) -> list[GoalGraphRevision]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM goal_graph_revisions WHERE session_id=? ORDER BY revision", (session_id,)
            ).fetchall()
            return [_graph_revision(row) for row in rows]

    def graph_revision(self, session_id: str, revision: int | None = None) -> GoalGraphRevision | None:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            if revision is None:
                row = connection.execute(
                    "SELECT * FROM goal_graph_revisions WHERE session_id=? ORDER BY revision DESC LIMIT 1", (session_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM goal_graph_revisions WHERE session_id=? AND revision=?", (session_id, revision)
                ).fetchone()
            return _graph_revision(row) if row is not None else None

    def goal_edges(self, session_id: str, *, revision: int | None = None) -> list[GoalEdge]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            sql = "SELECT * FROM goal_edges WHERE session_id=?"
            args: tuple[Any, ...] = (session_id,)
            if revision is not None:
                sql += " AND revision=?"
                args += (revision,)
            rows = connection.execute(sql + " ORDER BY created_at, edge_id", args).fetchall()
            return [_edge(row) for row in rows]

    def criteria(self, goal_node_id: str, *, revision: int | None = None) -> list[GoalCriterion]:
        self.initialize()
        with self._connection() as connection:
            sql = "SELECT * FROM goal_criteria WHERE goal_node_id=?"
            args: tuple[Any, ...] = (goal_node_id,)
            if revision is not None:
                sql += " AND revision=?"
                args += (revision,)
            rows = connection.execute(sql + " ORDER BY created_at, criterion_id", args).fetchall()
            return [_criterion(row) for row in rows]

    def coverage(self, session_id: str, *, revision: int | None = None) -> list[GoalCoverage]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            sql = "SELECT * FROM goal_coverage WHERE session_id=?"
            args: tuple[Any, ...] = (session_id,)
            if revision is not None:
                sql += " AND graph_revision=?"
                args += (revision,)
            rows = connection.execute(sql + " ORDER BY created_at, coverage_id", args).fetchall()
            return [_coverage(row) for row in rows]

    def bindings(self, session_id: str) -> list[SessionGoalBinding]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM session_goal_bindings WHERE session_id=? ORDER BY created_at, binding_id", (session_id,)
            ).fetchall()
            return [_binding(row) for row in rows]

    def pending_activation_intents(self, *, limit: int = 100) -> list[DurableOutboxIntent]:
        return self.pending_outbox(limit=limit)

    def activation_intents(self, session_id: str) -> list[DurableOutboxIntent]:
        self.initialize()
        with self._connection() as connection:
            self._session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM agent_outbox WHERE session_id=? ORDER BY created_at, intent_id", (session_id,)
            ).fetchall()
            return [_outbox(row) for row in rows]

    def apply_graph_revision(
        self,
        session_id: str,
        *,
        revision: GoalGraphRevisionDraft,
        nodes: Sequence[GoalNodeDraft],
        edges: Sequence[GoalEdgeDraft] = (),
        criteria: Sequence[GoalCriterionDraft] = (),
        coverage: Sequence[GoalCoverageDraft] = (),
    ) -> GoalGraphRevision:
        """Commit one complete graph snapshot and all new activation intents.

        This method has no GoalRuntime dependency.  The outbox records become
        visible only after the same ``BEGIN IMMEDIATE`` transaction commits.
        Prior terminal nodes and their criterion/fact projections are append-
        only and cannot be overwritten by a later graph revision.
        """
        self.initialize()
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            session = self._session(connection, session_id)
            if revision.authority_revision != session.authority_revision:
                raise ValueError("graph authority_revision must equal persisted Session authority")
            already_applied = connection.execute(
                "SELECT * FROM goal_graph_revisions WHERE session_id=? AND authority_revision=?",
                (session_id, revision.authority_revision),
            ).fetchone()
            if already_applied is not None:
                return _graph_revision(already_applied)
            latest_row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS value FROM goal_graph_revisions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            latest_revision = int(latest_row["value"])
            if (
                revision.base_graph_revision is not None
                and revision.base_graph_revision != latest_revision
            ):
                raise ValueError("graph base revision is stale")
            next_revision = latest_revision + 1
            directive = connection.execute(
                "SELECT directive_id FROM user_directives WHERE directive_id=? AND session_id=?",
                (revision.source_directive_id, session_id),
            ).fetchone()
            if directive is None:
                raise ValueError("graph revision must reference a directive in its Session")
            graph_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO goal_graph_revisions(graph_revision_id, session_id, revision, authority_revision, source_directive_id, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (graph_id, session_id, next_revision, revision.authority_revision,
                 revision.source_directive_id, revision.reason, now),
            )
            existing_ids = {str(row["goal_node_id"]): row for row in connection.execute(
                "SELECT * FROM goal_nodes WHERE session_id=?", (session_id,)
            ).fetchall()}
            node_ids: set[str] = set()
            activation_events: list[tuple[str, str]] = []
            for draft in nodes:
                node_id = draft.existing_goal_id if draft.operation != "CREATE" else draft.id
                assert node_id is not None
                node_ids.add(node_id)
                old = existing_ids.get(node_id)
                if draft.operation == "CREATE":
                    if old is not None:
                        raise ValueError("CREATE graph draft reuses an existing GoalNode id")
                    connection.execute(
                        "INSERT INTO goal_nodes(goal_node_id, session_id, title, source_directive_id, graph_revision, original_fragment, "
                        "goal_family, application_hint, account_hint, explicit_priority, scheduling_class, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (node_id, session_id, draft.title, draft.source_directive_id, next_revision,
                         draft.original_fragment, draft.goal_family, draft.application_hint, draft.account_hint,
                         draft.explicit_priority, draft.scheduling_class.value, draft.status.value, now, now),
                    )
                    intent_id = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO agent_outbox(intent_id, session_id, goal_node_id, intent_type, status, idempotency_key, goal_text, graph_revision, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (intent_id, session_id, node_id, OutboxIntentType.CREATE_GOAL_RUN.value,
                         OutboxIntentStatus.PENDING.value, _goal_create_key(session_id, node_id),
                         draft.execution_goal or draft.title, next_revision, now, now),
                    )
                    self._append_event(connection, session_id, SessionEventType.GOAL_NODE_CREATED,
                                       {"goal_node_id": node_id, "graph_revision": next_revision},
                                       f"graph:{next_revision}:goal-node:{node_id}", now)
                    activation_events.append((node_id, intent_id))
                    if draft.initial_wake is not None:
                        self._create_wake_condition(
                            connection,
                            session_id,
                            node_id,
                            draft.initial_wake,
                        )
                else:
                    if old is None:
                        raise ValueError("KEEP/UPDATE graph draft references an unknown GoalNode")
                    # Verified/terminal ownership is never revised in place.
                    if GoalNodeStatus(str(old["status"])).terminal or old["verified_result_ref"]:
                        continue
                    if draft.operation == "UPDATE":
                        connection.execute(
                            "UPDATE goal_nodes SET title=?, source_directive_id=?, graph_revision=?, original_fragment=?, goal_family=?, "
                            "application_hint=?, account_hint=?, explicit_priority=?, scheduling_class=?, updated_at=? WHERE goal_node_id=?",
                            (draft.title, draft.source_directive_id, next_revision, draft.original_fragment,
                             draft.goal_family, draft.application_hint, draft.account_hint, draft.explicit_priority,
                             draft.scheduling_class.value, now, node_id),
                        )
            known_nodes = set(existing_ids) | node_ids
            for edge in edges:
                if edge.from_goal_id not in known_nodes or edge.to_goal_id not in known_nodes:
                    raise ValueError("goal edge must reference a GoalNode in this Session")
                connection.execute(
                    "INSERT INTO goal_edges(edge_id, session_id, from_goal_id, to_goal_id, edge_kind, evidence_ref, revision, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), session_id, edge.from_goal_id, edge.to_goal_id,
                     edge.edge_kind.value, edge.evidence_ref, next_revision, now),
                )
            for criterion in criteria:
                if criterion.goal_id not in known_nodes:
                    raise ValueError("criterion must reference a GoalNode in this Session")
                connection.execute(
                    "INSERT INTO goal_criteria(criterion_id, goal_node_id, revision, description, evidence_requirement, required, status, evidence_refs_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), criterion.goal_id, next_revision, criterion.description,
                     criterion.evidence_requirement, int(criterion.required), GoalCriterionStatus.PENDING.value,
                     "[]", now),
                )
            for item in coverage:
                if item.coverage_kind is GoalCoverageKind.GOAL and item.target_goal_id not in known_nodes:
                    raise ValueError("Goal coverage must reference a GoalNode in this Session")
                connection.execute(
                    "INSERT INTO goal_coverage(coverage_id, session_id, graph_revision, source_directive_id, original_fragment, coverage_kind, target_goal_id, session_policy, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), session_id, next_revision, item.source_directive_id,
                     item.original_fragment, item.coverage_kind.value, item.target_goal_id,
                     item.session_policy, now),
                )
            self._append_event(connection, session_id, SessionEventType.GOAL_GRAPH_REVISION_APPLIED,
                               {"graph_revision": next_revision, "authority_revision": revision.authority_revision,
                                "goal_node_count": len(nodes), "coverage_count": len(coverage)},
                               f"graph:{next_revision}:applied", now)
            for node_id, intent_id in activation_events:
                self._append_event(connection, session_id, SessionEventType.GOAL_ACTIVATION_REQUESTED,
                                   {"goal_node_id": node_id, "outbox_intent_id": intent_id, "graph_revision": next_revision},
                                   f"graph:{next_revision}:activation:{node_id}", now)
            return _graph_revision(connection.execute(
                "SELECT * FROM goal_graph_revisions WHERE graph_revision_id=?", (graph_id,)
            ).fetchone())

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        current = connection.execute("SELECT revision FROM agent_runtime_schema WHERE singleton=1").fetchone()
        if current is None:
            raise RuntimeError("agent-runtime schema ledger is missing")
        value = int(current["revision"])
        if value > SCHEMA_REVISION:
            raise RuntimeError("agent-runtime database schema is newer than this runtime")
        if value == 1:
            SQLiteAgentRuntimeStore._migrate_schema_v2(connection)
            value = 2
        if value == 2:
            SQLiteAgentRuntimeStore._migrate_schema_v3(connection)
            value = 3
        if value == 3:
            SQLiteAgentRuntimeStore._migrate_schema_v4(connection)
            value = 4
        if value == 4:
            SQLiteAgentRuntimeStore._migrate_schema_v5(connection)
            value = 5
        if value == 5:
            SQLiteAgentRuntimeStore._migrate_schema_v6(connection)
            value = 6
        if value == 6:
            # Package B is additive to the current durable schema marker.  A
            # later integration-owned migration may assign a public revision;
            # retaining 6 here avoids breaking the already-persisted R7
            # compatibility contract while the new tables are idempotent.
            SQLiteAgentRuntimeStore._migrate_schema_v7(connection)
            SQLiteAgentRuntimeStore._migrate_schema_v8(connection)
        if value != SCHEMA_REVISION:
            raise RuntimeError("unsupported agent-runtime database schema")

    @staticmethod
    def _migrate_schema_v2(connection: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(goal_nodes)")}
        additions = {
            "graph_revision": "INTEGER NOT NULL DEFAULT 1", "original_fragment": "TEXT NOT NULL DEFAULT ''",
            "goal_family": "TEXT", "application_hint": "TEXT", "account_hint": "TEXT", "explicit_priority": "INTEGER",
            "waiting_kind": "TEXT", "waiting_ref": "TEXT", "current_stage_id": "TEXT", "continuation_id": "TEXT",
            "saturation": "REAL NOT NULL DEFAULT 0", "progress_summary": "TEXT", "verified_result_ref": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE goal_nodes ADD COLUMN {name} {definition}")
        outbox_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(agent_outbox)")}
        if "graph_revision" not in outbox_columns:
            connection.execute("ALTER TABLE agent_outbox ADD COLUMN graph_revision INTEGER NOT NULL DEFAULT 1")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS goal_graph_revisions (
                graph_revision_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                revision INTEGER NOT NULL, authority_revision INTEGER NOT NULL,
                source_directive_id TEXT NOT NULL REFERENCES user_directives(directive_id), reason TEXT, created_at TEXT NOT NULL,
                UNIQUE(session_id, revision)
            );
            CREATE TABLE IF NOT EXISTS goal_edges (
                edge_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                from_goal_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id), to_goal_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id),
                edge_kind TEXT NOT NULL, evidence_ref TEXT, revision INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_goal_edges_session_revision ON goal_edges(session_id, revision, edge_id);
            CREATE TABLE IF NOT EXISTS goal_criteria (
                criterion_id TEXT PRIMARY KEY, goal_node_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id), revision INTEGER NOT NULL,
                description TEXT NOT NULL, evidence_requirement TEXT, required INTEGER NOT NULL, status TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_goal_criteria_goal_revision ON goal_criteria(goal_node_id, revision, criterion_id);
            CREATE TABLE IF NOT EXISTS goal_coverage (
                coverage_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(session_id), graph_revision INTEGER NOT NULL,
                source_directive_id TEXT NOT NULL REFERENCES user_directives(directive_id), original_fragment TEXT NOT NULL,
                coverage_kind TEXT NOT NULL, target_goal_id TEXT REFERENCES goal_nodes(goal_node_id), session_policy TEXT, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_goal_coverage_session_revision ON goal_coverage(session_id, graph_revision, coverage_id);
        """)
        # Establish an auditable revision-one snapshot for every R1 session.
        rows = connection.execute("SELECT * FROM agent_sessions").fetchall()
        for session in rows:
            directive = connection.execute(
                "SELECT directive_id FROM user_directives WHERE session_id=? AND revision=1", (session["session_id"],)
            ).fetchone()
            if directive is None:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO goal_graph_revisions(graph_revision_id, session_id, revision, authority_revision, source_directive_id, reason, created_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?)",
                (str(uuid.uuid4()), session["session_id"], 1, directive["directive_id"],
                  "migrated R1 compatibility graph", session["created_at"]),
            )
        # Repair databases initialized by an early R2 candidate that claimed
        # the latest Session authority while migrating only the revision-one
        # R1 Goal. Startup recovery must still plan any later directives.
        connection.execute(
            "UPDATE goal_graph_revisions SET authority_revision=1 "
            "WHERE revision=1 AND reason='migrated R1 compatibility graph' AND authority_revision<>1"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_graph_one_per_authority "
            "ON goal_graph_revisions(session_id, authority_revision)"
        )
        connection.execute("UPDATE goal_nodes SET original_fragment=title WHERE original_fragment='' ")
        connection.execute("UPDATE agent_runtime_schema SET revision=2 WHERE singleton=1")

    @staticmethod
    def _migrate_schema_v3(connection: sqlite3.Connection) -> None:
        session_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(agent_sessions)")}
        if "current_attention_decision_id" not in session_columns:
            connection.execute("ALTER TABLE agent_sessions ADD COLUMN current_attention_decision_id TEXT")

        goal_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(goal_nodes)")}
        goal_additions = {
            "scheduling_class": "TEXT NOT NULL DEFAULT 'NORMAL'",
            "next_eligible_at": "TEXT",
            "last_selected_at": "TEXT",
            "last_service_at": "TEXT",
            "wait_started_at": "TEXT",
            "consecutive_failure_count": "INTEGER NOT NULL DEFAULT 0",
            "backoff_until": "TEXT",
        }
        for name, definition in goal_additions.items():
            if name not in goal_columns:
                connection.execute(f"ALTER TABLE goal_nodes ADD COLUMN {name} {definition}")

        # R2 prepared and activated every GoalRun before an AttentionDecision
        # existed, so historical multi-Goal Sessions can contain several
        # bound ACTIVE nodes while ``active_goal_id`` names only one.  In R3
        # ACTIVE is scheduler authority.  Normalize only the non-selected,
        # bound historical siblings; terminal and genuine WAITING states are
        # deliberately untouched.
        connection.execute("""
            UPDATE goal_nodes
            SET status='READY'
            WHERE status='ACTIVE'
              AND bound_goal_run_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM agent_sessions AS session
                  WHERE session.session_id=goal_nodes.session_id
                    AND session.status NOT IN ('STOPPED', 'COMPLETED', 'PARTIAL', 'FAILED')
                    AND COALESCE(session.active_goal_id, '')<>goal_nodes.goal_node_id
              )
        """)

        event_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(session_events)")}
        event_additions = {
            "source_namespace": "TEXT",
            "source_event_id": "TEXT",
            "device_id": "TEXT",
            "device_boot_id": "TEXT",
            "source_cursor": "TEXT",
            "occurred_at": "TEXT",
            "received_at": "TEXT",
            "classified_at": "TEXT",
            "affected_goal_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "decision_id": "TEXT",
            "error": "TEXT",
            "payload_digest": "TEXT",
        }
        for name, definition in event_additions.items():
            if name not in event_columns:
                connection.execute(f"ALTER TABLE session_events ADD COLUMN {name} {definition}")
        legacy_events = connection.execute(
            "SELECT event_id, event_type, data_json, created_at FROM session_events "
            "WHERE source_namespace IS NULL OR source_event_id IS NULL OR payload_digest IS NULL"
        ).fetchall()
        for row in legacy_events:
            digest = _digest({
                "event_type": str(row["event_type"]),
                "occurred_at": str(row["created_at"]),
                "payload": json.loads(str(row["data_json"])),
            })
            connection.execute(
                "UPDATE session_events SET source_namespace=COALESCE(source_namespace, 'audit'), "
                "source_event_id=COALESCE(source_event_id, event_id), occurred_at=COALESCE(occurred_at, created_at), "
                "received_at=COALESCE(received_at, created_at), payload_digest=COALESCE(payload_digest, ?) "
                "WHERE event_id=?",
                (digest, row["event_id"]),
            )
        connection.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_session_event_source_identity
            ON session_events(session_id, source_namespace, source_event_id)
            WHERE source_namespace IS NOT NULL AND source_event_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_session_event_inbox
            ON session_events(handling_status, received_at, cursor);

            CREATE TABLE IF NOT EXISTS attention_decisions (
                decision_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                decision_revision INTEGER NOT NULL,
                trigger_event_id TEXT REFERENCES session_events(event_id),
                trigger_key TEXT NOT NULL,
                authority_revision INTEGER NOT NULL,
                graph_revision INTEGER NOT NULL,
                event_cursor INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                candidate_goal_ids_json TEXT NOT NULL,
                selected_goal_id TEXT REFERENCES goal_nodes(goal_node_id),
                selector_kind TEXT NOT NULL,
                selected_hard_tier INTEGER,
                reason TEXT NOT NULL,
                user_priority_component INTEGER NOT NULL,
                event_urgency_component INTEGER NOT NULL,
                waiting_age_component INTEGER NOT NULL,
                starvation_component INTEGER NOT NULL,
                continuity_component INTEGER NOT NULL,
                app_switch_cost INTEGER NOT NULL,
                backoff_component INTEGER NOT NULL,
                recent_failure_component INTEGER NOT NULL,
                base_score INTEGER NOT NULL,
                model_adjustment INTEGER NOT NULL,
                slice_time_budget_ms INTEGER NOT NULL,
                slice_action_budget INTEGER NOT NULL,
                checkpoint_policy TEXT NOT NULL,
                preemption_policy TEXT NOT NULL,
                preemption_checkpoint_ref TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, decision_revision),
                UNIQUE(session_id, trigger_key),
                UNIQUE(session_id, trigger_event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_attention_decisions_session_revision
            ON attention_decisions(session_id, decision_revision DESC);

            CREATE TABLE IF NOT EXISTS attention_candidates (
                decision_id TEXT NOT NULL REFERENCES attention_decisions(decision_id),
                goal_node_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id),
                eligibility TEXT NOT NULL,
                reason TEXT NOT NULL,
                goal_status TEXT NOT NULL,
                scheduling_class TEXT NOT NULL,
                binding_status TEXT,
                wake_condition_id TEXT,
                next_eligible_at TEXT,
                continuation_revision INTEGER,
                latest_directive_target INTEGER NOT NULL,
                matched_trigger_event INTEGER NOT NULL,
                hard_tier INTEGER NOT NULL,
                user_priority_component INTEGER NOT NULL,
                event_urgency_component INTEGER NOT NULL,
                waiting_age_component INTEGER NOT NULL,
                starvation_component INTEGER NOT NULL,
                continuity_component INTEGER NOT NULL,
                app_switch_cost INTEGER NOT NULL,
                backoff_component INTEGER NOT NULL,
                recent_failure_component INTEGER NOT NULL,
                total_score INTEGER NOT NULL,
                rank INTEGER,
                PRIMARY KEY(decision_id, goal_node_id)
            );

            CREATE TABLE IF NOT EXISTS goal_continuations (
                continuation_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                goal_node_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id),
                revision INTEGER NOT NULL,
                authority_revision INTEGER NOT NULL,
                graph_revision INTEGER NOT NULL,
                attention_decision_id TEXT REFERENCES attention_decisions(decision_id),
                checkpoint_kind TEXT NOT NULL,
                checkpoint_ref TEXT,
                stage_id TEXT,
                application_package TEXT,
                scene_ref TEXT,
                person_id TEXT,
                conversation_id TEXT,
                verified_fact_refs_json TEXT NOT NULL,
                pending_intent_json TEXT,
                waiting_kind TEXT,
                waiting_ref TEXT,
                resume_preconditions_json TEXT NOT NULL,
                next_eligible_at TEXT,
                yield_reason TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(goal_node_id, revision),
                UNIQUE(session_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_goal_continuations_latest
            ON goal_continuations(goal_node_id, revision DESC);

            CREATE TABLE IF NOT EXISTS wake_conditions (
                wake_condition_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                goal_node_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id),
                revision INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                matcher_json TEXT,
                due_at TEXT,
                created_by_event_id TEXT REFERENCES session_events(event_id),
                created_by_decision_id TEXT REFERENCES attention_decisions(decision_id),
                satisfied_by_event_id TEXT REFERENCES session_events(event_id),
                created_at TEXT NOT NULL,
                satisfied_at TEXT,
                superseded_at TEXT,
                UNIQUE(goal_node_id, revision)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_wake_per_goal
            ON wake_conditions(goal_node_id) WHERE status='PENDING';
            CREATE INDEX IF NOT EXISTS idx_wake_conditions_session_status
            ON wake_conditions(session_id, status, due_at);

            CREATE TABLE IF NOT EXISTS attention_dispatches (
                dispatch_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                decision_id TEXT NOT NULL UNIQUE REFERENCES attention_decisions(decision_id),
                goal_node_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id),
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_attention_dispatch_pending
            ON attention_dispatches(status, created_at, dispatch_id);
        """)
        connection.execute("UPDATE agent_runtime_schema SET revision=3 WHERE singleton=1")

    @staticmethod
    def _migrate_schema_v4(connection: sqlite3.Connection) -> None:
        """Add the independent durable scheduler snapshot revision."""

        session_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }
        if "agenda_revision" not in session_columns:
            connection.execute(
                "ALTER TABLE agent_sessions ADD COLUMN agenda_revision INTEGER NOT NULL DEFAULT 0"
            )
        decision_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(attention_decisions)")
        }
        if "agenda_revision" not in decision_columns:
            connection.execute(
                "ALTER TABLE attention_decisions ADD COLUMN agenda_revision INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute("UPDATE agent_runtime_schema SET revision=4 WHERE singleton=1")

    @staticmethod
    def _migrate_schema_v5(connection: sqlite3.Connection) -> None:
        """Add R4 DeviceBody transport facts without a second event ledger."""

        connection.executescript("""
            CREATE TABLE IF NOT EXISTS device_body_bindings (
                binding_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE REFERENCES agent_sessions(session_id),
                device_id TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                device_boot_id TEXT NOT NULL,
                connection_state TEXT NOT NULL,
                capability_revision INTEGER NOT NULL DEFAULT 0,
                event_cursor INTEGER NOT NULL DEFAULT 0,
                action_cursor INTEGER NOT NULL DEFAULT 0,
                bound_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_heartbeat_at TEXT,
                protocol_revision INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_device_body_bindings_device
            ON device_body_bindings(device_id, device_boot_id, session_id);

            CREATE TABLE IF NOT EXISTS device_body_capabilities (
                capability_id TEXT PRIMARY KEY,
                binding_id TEXT NOT NULL REFERENCES device_body_bindings(binding_id),
                device_id TEXT NOT NULL,
                device_boot_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                capability_kind TEXT NOT NULL,
                capability_state TEXT NOT NULL,
                reason_code TEXT,
                evidence_ref TEXT,
                observed_at TEXT NOT NULL,
                protocol_revision INTEGER NOT NULL,
                payload_digest TEXT NOT NULL,
                UNIQUE(binding_id, revision, capability_kind),
                UNIQUE(binding_id, revision, payload_digest)
            );
            CREATE INDEX IF NOT EXISTS idx_device_body_capabilities_revision
            ON device_body_capabilities(binding_id, revision, capability_kind);

            CREATE TABLE IF NOT EXISTS device_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                binding_id TEXT NOT NULL REFERENCES device_body_bindings(binding_id),
                device_id TEXT NOT NULL,
                device_boot_id TEXT NOT NULL,
                capture_request_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                foreground_package TEXT,
                foreground_activity TEXT,
                screen_on INTEGER NOT NULL,
                locked INTEGER NOT NULL,
                network_state TEXT NOT NULL,
                orientation TEXT NOT NULL,
                human_presence TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                capture_started_at TEXT NOT NULL,
                capture_completed_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                caused_by_command_id TEXT,
                screenshot_ref TEXT,
                accessibility_tree_ref TEXT,
                input_read_back_sha256 TEXT,
                input_read_back_length INTEGER,
                input_method TEXT,
                protocol_revision INTEGER NOT NULL,
                payload_digest TEXT NOT NULL,
                UNIQUE(binding_id, capture_request_id),
                UNIQUE(binding_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_device_snapshots_latest
            ON device_snapshots(binding_id, sequence DESC);

            CREATE TABLE IF NOT EXISTS body_action_commands (
                command_id TEXT PRIMARY KEY,
                binding_id TEXT NOT NULL REFERENCES device_body_bindings(binding_id),
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                device_id TEXT NOT NULL,
                device_boot_id TEXT NOT NULL,
                kernel_action_id TEXT NOT NULL UNIQUE,
                action_cursor INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                expected_state_json TEXT NOT NULL,
                required_capabilities_json TEXT NOT NULL,
                status TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                target_companion_install_id TEXT,
                slice_id TEXT,
                dispatched_at TEXT,
                acknowledged_at TEXT,
                settled_at TEXT,
                protocol_revision INTEGER NOT NULL,
                payload_digest TEXT NOT NULL UNIQUE,
                UNIQUE(binding_id, action_cursor)
            );
            CREATE INDEX IF NOT EXISTS idx_body_action_commands_reconciliation
            ON body_action_commands(status, action_cursor, command_id);
            CREATE INDEX IF NOT EXISTS idx_body_action_commands_binding_cursor
            ON body_action_commands(binding_id, action_cursor);

            CREATE TABLE IF NOT EXISTS body_execution_receipts (
                receipt_id TEXT PRIMARY KEY,
                source_receipt_id TEXT NOT NULL,
                command_id TEXT NOT NULL UNIQUE REFERENCES body_action_commands(command_id),
                kernel_action_id TEXT NOT NULL UNIQUE,
                binding_id TEXT NOT NULL REFERENCES device_body_bindings(binding_id),
                device_id TEXT NOT NULL,
                device_boot_id TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                acknowledgement TEXT NOT NULL,
                transport TEXT NOT NULL,
                execution TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                retryable INTEGER NOT NULL,
                adapter_code TEXT,
                reason_code TEXT,
                evidence_ref TEXT,
                protocol_revision INTEGER NOT NULL,
                payload_digest TEXT NOT NULL UNIQUE,
                UNIQUE(adapter_id, source_receipt_id)
            );
            CREATE INDEX IF NOT EXISTS idx_body_execution_receipts_command
            ON body_execution_receipts(command_id);
        """)

        legacy_rows = connection.execute(
            "SELECT session_id, device_binding_id, created_at, updated_at "
            "FROM agent_sessions "
            "WHERE device_binding_id IS NOT NULL AND trim(device_binding_id)<>''"
        ).fetchall()
        for row in legacy_rows:
            binding_id = str(row["device_binding_id"])
            existing = connection.execute(
                "SELECT session_id FROM device_body_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["session_id"]) != str(row["session_id"]):
                    raise RuntimeError("legacy device binding id is shared by different sessions")
                continue
            connection.execute(
                "INSERT INTO device_body_bindings("
                "binding_id, session_id, device_id, adapter_id, device_boot_id, connection_state, "
                "capability_revision, event_cursor, action_cursor, bound_at, updated_at, protocol_revision"
                ") VALUES (?, ?, ?, 'legacy-unresolved', ?, 'DEGRADED', 0, 0, 0, ?, ?, 1)",
                (
                    binding_id, str(row["session_id"]), f"legacy-binding:{binding_id}",
                    f"legacy-boot:{binding_id}", str(row["created_at"]), str(row["updated_at"]),
                ),
            )
        connection.execute("UPDATE agent_runtime_schema SET revision=5 WHERE singleton=1")

    @staticmethod
    def _migrate_schema_v6(connection: sqlite3.Connection) -> None:
        """Add the R7 cross-database preemption saga and control audit."""

        connection.executescript("""
            CREATE TABLE IF NOT EXISTS preemption_requests (
                preemption_request_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                event_id TEXT NOT NULL UNIQUE REFERENCES session_events(event_id),
                prior_goal_id TEXT NOT NULL REFERENCES goal_nodes(goal_node_id),
                prior_slice_id TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                checkpoint_ref TEXT,
                continuation_id TEXT REFERENCES goal_continuations(continuation_id),
                attention_decision_id TEXT REFERENCES attention_decisions(decision_id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_preemption_recovery
            ON preemption_requests(status, created_at, preemption_request_id);

            CREATE TABLE IF NOT EXISTS session_control_transitions (
                transition_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                event_id TEXT NOT NULL REFERENCES session_events(event_id),
                from_mode TEXT NOT NULL,
                to_mode TEXT NOT NULL,
                reason TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                fresh_observation_ref TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_control_transitions_session
            ON session_control_transitions(session_id, created_at, transition_id);
        """)
        connection.execute(
            "DROP INDEX IF EXISTS idx_one_pending_preemption_per_session"
        )
        connection.execute("UPDATE agent_runtime_schema SET revision=6 WHERE singleton=1")

    @staticmethod
    def _migrate_schema_v7(connection: sqlite3.Connection) -> None:
        """Make the existing AgentSession row the sole canonical Task snapshot.

        No ``tasks`` table is created: the invariant is enforced by deriving
        every Task ID from ``agent_sessions.session_id``.  Supporting tables
        only retain revision/control/subtask audit data and a same-transaction
        projection outbox.
        """

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }
        migrating_legacy_task_snapshot = "task_status" not in columns
        additions = {
            "task_status": "TEXT NOT NULL DEFAULT 'scheduled'",
            "task_owner_principal_id": "TEXT NOT NULL DEFAULT 'legacy-local-principal'",
            "task_controller_id": "TEXT NOT NULL DEFAULT 'legacy-installation'",
            "task_origin_json": "TEXT NOT NULL DEFAULT '{}'",
            "task_reason_code": "TEXT NOT NULL DEFAULT 'accepted'",
            "task_reason_summary": "TEXT NOT NULL DEFAULT 'Task accepted.'",
            "task_reason_recoverable": "INTEGER NOT NULL DEFAULT 1",
            "task_priority": "INTEGER NOT NULL DEFAULT 50",
            "task_current_subtask_id": "TEXT",
            "task_next_wake_at": "TEXT",
            "task_integrity_state": "TEXT NOT NULL DEFAULT 'clear'",
            "task_integrity_reason_code": "TEXT",
            "task_terminal_at": "TEXT",
            "task_archived_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE agent_sessions ADD COLUMN {name} {definition}")

        # Existing R1/R7 Session rows have to remain inspectable after the
        # migration.  Map their old lifecycle conservatively; no legacy row is
        # presented as successful without its prior completed state.
        if migrating_legacy_task_snapshot:
            connection.execute("""
                UPDATE agent_sessions
                SET task_status = CASE status
                    WHEN 'STOPPED' THEN 'cancelled'
                    WHEN 'COMPLETED' THEN 'succeeded'
                    WHEN 'PARTIAL' THEN 'failed'
                    WHEN 'FAILED' THEN 'failed'
                    WHEN 'WAITING_ALL' THEN 'waiting_event'
                    WHEN 'USER_ACTIVE' THEN 'user_takeover'
                    ELSE 'running'
                END,
                task_reason_code = CASE status
                    WHEN 'COMPLETED' THEN 'legacy_completed'
                    WHEN 'STOPPED' THEN 'legacy_stopped'
                    WHEN 'PARTIAL' THEN 'legacy_partial'
                    WHEN 'FAILED' THEN 'legacy_failed'
                    WHEN 'WAITING_ALL' THEN 'legacy_waiting'
                    ELSE 'legacy_session'
                END,
                task_reason_summary = 'Migrated from AgentSession lifecycle.',
                task_reason_recoverable = CASE WHEN status IN ('STOPPED', 'COMPLETED', 'PARTIAL', 'FAILED') THEN 0 ELSE 1 END,
                task_terminal_at = CASE WHEN status IN ('STOPPED', 'COMPLETED', 'PARTIAL', 'FAILED') THEN COALESCE(stopped_at, updated_at) ELSE NULL END
            """)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS task_revisions (
                task_revision_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                revision INTEGER NOT NULL,
                base_revision INTEGER NOT NULL,
                kind TEXT NOT NULL,
                instruction TEXT NOT NULL,
                patch_json TEXT NOT NULL,
                effective_boundary TEXT NOT NULL,
                requested_by_json TEXT NOT NULL,
                status TEXT NOT NULL,
                reason_code TEXT,
                idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                applied_at TEXT,
                UNIQUE(session_id, revision),
                UNIQUE(session_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_task_revisions_task
            ON task_revisions(session_id, revision);

            CREATE TABLE IF NOT EXISTS task_controls (
                control_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                action TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                expected_revision INTEGER NOT NULL,
                requested_priority INTEGER,
                requested_by_json TEXT NOT NULL,
                status TEXT NOT NULL,
                reason_code TEXT,
                requested_at TEXT NOT NULL,
                applied_at TEXT,
                UNIQUE(session_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_task_controls_task
            ON task_controls(session_id, requested_at, control_id);

            CREATE TABLE IF NOT EXISTS task_subtasks (
                subtask_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                kind TEXT NOT NULL,
                object_ref TEXT,
                conversation_ref TEXT,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL,
                current_stage TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, kind, object_ref, conversation_ref)
            );
            CREATE INDEX IF NOT EXISTS idx_task_subtasks_task
            ON task_subtasks(session_id, updated_at, subtask_id);

            CREATE TABLE IF NOT EXISTS task_projection_outbox (
                intent_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                event_id TEXT NOT NULL UNIQUE REFERENCES session_events(event_id),
                intent_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                UNIQUE(session_id, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_task_projection_outbox_pending
            ON task_projection_outbox(status, created_at, intent_id);
        """)
        connection.execute("UPDATE agent_runtime_schema SET revision=6 WHERE singleton=1")

    @staticmethod
    def _migrate_schema_v8(connection: sqlite3.Connection) -> None:
        """Add the canonical runner dispatch linearization ledger.

        This remains additive to the current schema marker for the same reason
        as v7: existing runtime databases continue to report revision 6 while
        Package K work is composed behind explicit runner admission.
        """

        connection.executescript("""
            CREATE TABLE IF NOT EXISTS task_runner_dispatches (
                dispatch_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                owner_principal_id TEXT NOT NULL,
                controller_id TEXT NOT NULL,
                authority_revision INTEGER NOT NULL,
                step_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                runner_kind TEXT NOT NULL,
                runner_version TEXT NOT NULL,
                subtask_id TEXT NOT NULL REFERENCES task_subtasks(subtask_id),
                profile_id TEXT NOT NULL,
                profile_generation INTEGER NOT NULL,
                device_boot_id TEXT NOT NULL,
                canonical_device_id TEXT NOT NULL,
                command_type TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                UNIQUE(session_id, step_id),
                UNIQUE(session_id, action_id),
                UNIQUE(session_id, command_id)
            );
            CREATE INDEX IF NOT EXISTS idx_task_runner_dispatch_task
            ON task_runner_dispatches(session_id, committed_at, dispatch_id);
            CREATE INDEX IF NOT EXISTS idx_task_runner_dispatch_device
            ON task_runner_dispatches(canonical_device_id, committed_at, dispatch_id);
            CREATE TABLE IF NOT EXISTS task_runner_effects (
                dispatch_id TEXT PRIMARY KEY REFERENCES task_runner_dispatches(dispatch_id),
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                command_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('ACTIVE','SETTLED')),
                outcome TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(dispatch_id, session_id, command_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runner_one_active_effect
            ON task_runner_effects(session_id) WHERE state='ACTIVE';
        """)

    def _record_directive(
        self, session_id: str, *, content: str, client_request_id: str, kind: DirectiveKind
    ) -> tuple[UserDirective, bool]:
        self.initialize()
        content = content.strip()
        scope = f"directive:{session_id}"
        digest = _digest({"content": content, "kind": kind.value})
        now = utc_now()
        with self._lock, self._connection(write=True) as connection:
            self._session(connection, session_id)
            existing = connection.execute(
                "SELECT request_digest FROM session_requests WHERE scope=? AND request_key=?",
                (scope, client_request_id),
            ).fetchone()
            source_id = f"{kind.value}:{client_request_id}"
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise SessionIdempotencyConflict("同一 client_request_id 已用于不同的 Session 指令。")
                row = connection.execute(
                    "SELECT * FROM user_directives WHERE session_id=? AND source_message_id=?",
                    (session_id, source_id),
                ).fetchone()
                return _directive(row), False
            revision = int(connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM user_directives WHERE session_id=?",
                (session_id,),
            ).fetchone()["revision"])
            directive_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO session_requests(scope, request_key, request_digest, session_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (scope, client_request_id, digest, session_id, now),
            )
            connection.execute(
                "INSERT INTO user_directives("
                "directive_id, session_id, revision, content, directive_kind, source_message_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (directive_id, session_id, revision, content, kind.value, source_id, now),
            )
            connection.execute(
                "UPDATE agent_sessions SET authority_revision=?, updated_at=? WHERE session_id=?",
                (revision, now, session_id),
            )
            self._append_event(connection, session_id, SessionEventType.DIRECTIVE_RECORDED,
                               {"directive_id": directive_id, "revision": revision, "kind": kind.value},
                               f"directive:{client_request_id}:recorded", now)
            return _directive(connection.execute(
                "SELECT * FROM user_directives WHERE directive_id=?", (directive_id,)
            ).fetchone()), True

    def _session(self, connection: sqlite3.Connection, session_id: str) -> AgentSession:
        row = connection.execute("SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFound("未找到该 AgentSession。")
        return _agent_session(row)

    @staticmethod
    def _advance_agenda_revision(
        connection: sqlite3.Connection, session_id: str, now: str
    ) -> None:
        connection.execute(
            "UPDATE agent_sessions SET agenda_revision=agenda_revision+1, updated_at=? "
            "WHERE session_id=?",
            (now, session_id),
        )

    @staticmethod
    def _owned_goal(connection: sqlite3.Connection, session_id: str, goal_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM goal_nodes WHERE goal_node_id=? AND session_id=?", (goal_id, session_id)
        ).fetchone()
        if row is None:
            raise ValueError("Goal must belong to the Session")
        return row

    @staticmethod
    def _attention_dispatch_row(connection: sqlite3.Connection, dispatch_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM attention_dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown attention dispatch: {dispatch_id}")
        return row

    def _append_continuation(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        goal_id: str,
        draft: ContinuationDraft,
    ) -> tuple[Continuation, bool]:
        payload = {
            "authority_revision": draft.authority_revision,
            "graph_revision": draft.graph_revision,
            "attention_decision_id": draft.attention_decision_id,
            "checkpoint_kind": draft.checkpoint_kind.value,
            "checkpoint_ref": draft.checkpoint_ref,
            "stage_id": draft.stage_id,
            "application_package": draft.application_package,
            "scene_ref": draft.scene_ref,
            "person_id": draft.person_id,
            "conversation_id": draft.conversation_id,
            "verified_fact_refs": draft.verified_fact_refs,
            "pending_intent": draft.pending_intent,
            "waiting_kind": draft.waiting_kind,
            "waiting_ref": draft.waiting_ref,
            "resume_preconditions": draft.resume_preconditions,
            "next_eligible_at": draft.next_eligible_at,
            "yield_reason": draft.yield_reason.value,
        }
        digest = _digest(payload)
        existing = connection.execute(
            "SELECT * FROM goal_continuations WHERE session_id=? AND idempotency_key=?",
            (session_id, draft.idempotency_key),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_digest"]) != digest:
                raise SessionIdempotencyConflict(
                    "同一 continuation idempotency_key 已用于不同内容。"
                )
            return _continuation(existing), False
        revision = int(connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM goal_continuations WHERE goal_node_id=?",
            (goal_id,),
        ).fetchone()["value"])
        continuation_id = str(uuid.uuid4())
        created_at = utc_now()
        connection.execute(
            "INSERT INTO goal_continuations(continuation_id, session_id, goal_node_id, revision, authority_revision, "
            "graph_revision, attention_decision_id, checkpoint_kind, checkpoint_ref, stage_id, application_package, "
            "scene_ref, person_id, conversation_id, verified_fact_refs_json, pending_intent_json, waiting_kind, waiting_ref, "
            "resume_preconditions_json, next_eligible_at, yield_reason, idempotency_key, payload_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (continuation_id, session_id, goal_id, revision, draft.authority_revision,
             draft.graph_revision, draft.attention_decision_id, draft.checkpoint_kind.value,
             draft.checkpoint_ref, draft.stage_id, draft.application_package, draft.scene_ref,
             draft.person_id, draft.conversation_id, _json(draft.verified_fact_refs),
             _json(draft.pending_intent) if draft.pending_intent is not None else None,
             draft.waiting_kind, draft.waiting_ref, _json(draft.resume_preconditions),
             draft.next_eligible_at, draft.yield_reason.value, draft.idempotency_key, digest, created_at),
        )
        connection.execute(
            "UPDATE goal_nodes SET continuation_id=?, next_eligible_at=COALESCE(?, next_eligible_at), updated_at=? "
            "WHERE goal_node_id=?",
            (continuation_id, draft.next_eligible_at, created_at, goal_id),
        )
        self._advance_agenda_revision(connection, session_id, created_at)
        row = connection.execute(
            "SELECT * FROM goal_continuations WHERE continuation_id=?", (continuation_id,)
        ).fetchone()
        return _continuation(row), True

    def _create_wake_condition(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        goal_id: str,
        draft: WakeConditionDraft,
        *,
        waiting_kind: str | None = None,
    ) -> WakeCondition:
        now = utc_now()
        prior = connection.execute(
            "SELECT * FROM wake_conditions WHERE goal_node_id=? AND status='PENDING'", (goal_id,)
        ).fetchone()
        if prior is not None:
            existing = _wake_condition(prior)
            if (
                existing.kind == draft.kind
                and existing.matcher == draft.matcher
                and existing.due_at == draft.due_at
            ):
                if waiting_kind is not None:
                    connection.execute(
                        "UPDATE goal_nodes SET waiting_kind=?, updated_at=? "
                        "WHERE goal_node_id=?",
                        (waiting_kind, now, goal_id),
                    )
                return existing
            connection.execute(
                "UPDATE wake_conditions SET status=?, superseded_at=? WHERE wake_condition_id=?",
                (WakeConditionStatus.SUPERSEDED.value, now, existing.id),
            )
        revision = int(connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM wake_conditions WHERE goal_node_id=?",
            (goal_id,),
        ).fetchone()["value"])
        wake_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO wake_conditions(wake_condition_id, session_id, goal_node_id, revision, kind, status, "
            "matcher_json, due_at, created_by_event_id, created_by_decision_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (wake_id, session_id, goal_id, revision, draft.kind.value,
             WakeConditionStatus.PENDING.value, _json(draft.matcher) if draft.matcher is not None else None,
             draft.due_at, draft.created_by_event_id, draft.created_by_decision_id, now),
        )
        waiting_status = {
            WakeConditionKind.EVENT: GoalNodeStatus.WAITING_EVENT,
            WakeConditionKind.TIME: GoalNodeStatus.WAITING_TIME,
            WakeConditionKind.USER_FACT: GoalNodeStatus.WAITING_USER_FACT,
            WakeConditionKind.DEVICE: GoalNodeStatus.WAITING_DEVICE,
            WakeConditionKind.ACCOUNT: GoalNodeStatus.WAITING_ACCOUNT,
            WakeConditionKind.IDENTITY: GoalNodeStatus.WAITING_IDENTITY,
        }[draft.kind]
        connection.execute(
            "UPDATE goal_nodes SET status=?, waiting_kind=?, waiting_ref=?, wait_started_at=?, "
            "next_eligible_at=?, updated_at=? WHERE goal_node_id=?",
            (
                waiting_status.value,
                waiting_kind or draft.kind.value,
                wake_id,
                now,
                draft.due_at,
                now,
                goal_id,
            ),
        )
        self._advance_agenda_revision(connection, session_id, now)
        return _wake_condition(connection.execute(
            "SELECT * FROM wake_conditions WHERE wake_condition_id=?", (wake_id,)
        ).fetchone())

    @staticmethod
    def _outbox(connection: sqlite3.Connection, intent_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM agent_outbox WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown outbox intent: {intent_id}")
        return row

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection, session_id: str, event_type: SessionEventType,
        data: dict[str, Any], idempotency_key: str, created_at: str,
    ) -> SessionEvent:
        existing = connection.execute(
            "SELECT * FROM session_events WHERE session_id=? AND idempotency_key=?",
            (session_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return _event(existing)
        event_id = str(uuid.uuid4())
        cursor = connection.execute(
            "INSERT INTO session_events(event_id, session_id, event_type, data_json, idempotency_key, "
            "handling_status, created_at, handled_at, source_namespace, source_event_id, occurred_at, "
            "received_at, classified_at, affected_goal_ids_json, payload_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'audit', ?, ?, ?, ?, '[]', ?)",
            (event_id, session_id, event_type.value, _json(data), idempotency_key,
             EventHandlingStatus.HANDLED.value, created_at, created_at, idempotency_key,
             created_at, created_at, created_at,
             _digest({"event_type": event_type.value, "occurred_at": created_at, "payload": data})),
        ).lastrowid
        connection.execute(
            "UPDATE agent_sessions SET event_cursor=?, updated_at=? WHERE session_id=?",
            (cursor, created_at, session_id),
        )
        row = connection.execute("SELECT * FROM session_events WHERE cursor=?", (cursor,)).fetchone()
        return _event(row)

    @staticmethod
    def _append_task_event(
        connection: sqlite3.Connection, session_id: str, event_type: SessionEventType,
        data: dict[str, Any], idempotency_key: str, created_at: str,
    ) -> SessionEvent:
        """Append one canonical Task event and its projection outbox atomically."""

        existing = connection.execute(
            "SELECT * FROM session_events WHERE session_id=? AND idempotency_key=?",
            (session_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            event = _event(existing)
            if event.event_type is not event_type or dict(event.data) != data:
                raise SessionIdempotencyConflict("task event idempotency key was reused with a different payload")
            return event
        event = SQLiteAgentRuntimeStore._append_event(
            connection, session_id, event_type, data, idempotency_key, created_at
        )
        connection.execute(
            "INSERT INTO task_projection_outbox(intent_id, session_id, event_id, intent_type, payload_json, status, created_at) "
            "VALUES (?, ?, ?, 'task_projection', ?, 'PENDING', ?)",
            (str(uuid.uuid4()), session_id, event.id, _json({"cursor": event.cursor, "type": event_type.value}), created_at),
        )
        return event

    def _task(self, connection: sqlite3.Connection, task_id: str) -> Task:
        row = connection.execute("SELECT * FROM agent_sessions WHERE session_id=?", (task_id,)).fetchone()
        if row is None:
            raise SessionNotFound("未找到该 AgentSession Task。")
        return _task(row)

    def _apply_task_revision(
        self, connection: sqlite3.Connection, revision_id: str, now: str,
    ) -> TaskRevision:
        row = connection.execute(
            "SELECT * FROM task_revisions WHERE task_revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown task revision: {revision_id}")
        revision = _task_revision(row)
        if revision.status is TaskRecordStatus.APPLIED:
            return revision
        task = self._task(connection, revision.task_id)
        if task.terminal:
            raise TaskRevisionConflict("terminal task revision cannot be applied")
        priority = task.priority
        if revision.kind is TaskRevisionKind.REPRIORITIZE and "priority" in revision.patch:
            candidate = revision.patch["priority"]
            if not isinstance(candidate, int) or not 0 <= candidate <= 100:
                raise TaskRevisionConflict("reprioritize revision requires priority between 0 and 100")
            priority = candidate
        connection.execute(
            "UPDATE task_revisions SET status=?, applied_at=? WHERE task_revision_id=?",
            (TaskRecordStatus.APPLIED.value, now, revision_id),
        )
        connection.execute(
            "UPDATE agent_sessions SET task_priority=?, updated_at=? WHERE session_id=?",
            (priority, now, revision.task_id),
        )
        self._append_task_event(
            connection, revision.task_id, SessionEventType.TASK_REVISION_APPLIED,
            {"revision_id": revision.id, "revision": revision.revision, "kind": revision.kind.value,
             "effective_boundary": revision.effective_boundary},
            f"task-revision:{revision.id}:applied", now,
        )
        return _task_revision(connection.execute(
            "SELECT * FROM task_revisions WHERE task_revision_id=?", (revision_id,)
        ).fetchone())

    def _apply_task_control(
        self, connection: sqlite3.Connection, control_id: str, priority: int | None, now: str,
    ) -> TaskControl:
        row = connection.execute("SELECT * FROM task_controls WHERE control_id=?", (control_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task control: {control_id}")
        control = _task_control(row)
        if control.status is TaskRecordStatus.APPLIED:
            return control
        task = self._task(connection, control.task_id)
        target: TaskStatus | None = None
        if control.action is TaskControlAction.PAUSE:
            target = TaskStatus.PAUSED
        elif control.action is TaskControlAction.RESUME:
            if task.status not in {TaskStatus.PAUSED, TaskStatus.WAITING_TIME, TaskStatus.WAITING_EVENT}:
                raise TaskControlConflict("resume requires paused or recoverable waiting task")
            target = TaskStatus.RUNNING
        elif control.action is TaskControlAction.CANCEL:
            target = TaskStatus.CANCELLED
        elif control.action is TaskControlAction.TAKEOVER:
            target = TaskStatus.USER_TAKEOVER
        elif control.action is TaskControlAction.RELEASE_TAKEOVER:
            if task.status is not TaskStatus.USER_TAKEOVER:
                raise TaskControlConflict("release_takeover requires user_takeover")
            target = TaskStatus.REPLANNING
        elif control.action is TaskControlAction.REPRIORITIZE:
            if priority is None:
                raise TaskControlConflict("reprioritize requires priority")
            connection.execute(
                "UPDATE agent_sessions SET task_priority=?, updated_at=? WHERE session_id=?",
                (priority, now, control.task_id),
            )
        elif control.action is TaskControlAction.ARCHIVE:
            connection.execute(
                "UPDATE agent_sessions SET task_archived_at=COALESCE(task_archived_at, ?), updated_at=? WHERE session_id=?",
                (now, now, control.task_id),
            )
        if target is not None:
            if not can_transition_task(task.status, target):
                raise TaskControlConflict(f"task state {task.status.value} cannot execute {control.action.value}")
            connection.execute(
                "UPDATE agent_sessions SET task_status=?, task_reason_code=?, task_reason_summary=?, task_reason_recoverable=?, task_next_wake_at=NULL, task_terminal_at=COALESCE(?, task_terminal_at), updated_at=? WHERE session_id=?",
                (target.value, control.action.value, f"Task control {control.action.value} applied.",
                 int(not target.terminal), now if target.terminal else None, now, control.task_id),
            )
        connection.execute(
            "UPDATE task_controls SET status=?, applied_at=? WHERE control_id=?",
            (TaskRecordStatus.APPLIED.value, now, control_id),
        )
        event_type = SessionEventType.TASK_ARCHIVED if control.action is TaskControlAction.ARCHIVE else SessionEventType.TASK_CONTROL_APPLIED
        self._append_task_event(
            connection, control.task_id, event_type,
            {"control_id": control.id, "action": control.action.value, "revision": control.expected_revision,
             "priority": priority, "status": target.value if target is not None else task.status.value},
            f"task-control:{control.id}:applied", now,
        )
        return _task_control(connection.execute("SELECT * FROM task_controls WHERE control_id=?", (control_id,)).fetchone())

    def _connection(self, *, write: bool = False) -> _ConnectionContext:
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

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        if kind is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()


def _agent_session(row: sqlite3.Row) -> AgentSession:
    return AgentSession(
        id=str(row["session_id"]), client_request_id=str(row["client_request_id"]),
        original_instruction=str(row["original_instruction"]), authority_revision=int(row["authority_revision"]),
        session_kind=SessionKind(str(row["session_kind"])), status=SessionStatus(str(row["status"])),
        control_mode=SessionControlMode(str(row["control_mode"])), device_binding_id=row["device_binding_id"],
        active_goal_id=row["active_goal_id"], active_slice_id=row["active_slice_id"],
        event_cursor=int(row["event_cursor"]), calendar_started_at=str(row["calendar_started_at"]),
        calendar_window_end=row["calendar_window_end"], terminal_condition=row["terminal_condition"],
        summary=row["summary"], created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        stopped_at=row["stopped_at"],
        current_attention_decision_id=row["current_attention_decision_id"] if "current_attention_decision_id" in row.keys() else None,
        agenda_revision=int(row["agenda_revision"]) if "agenda_revision" in row.keys() else 0,
    )


_INTEGRITY_REASON_CODES = frozenset({
    "duplicate_real_side_effect",
    "identity_crosswire",
    "credential_exposure",
    "fabricated_success",
})


def _task(row: sqlite3.Row) -> Task:
    """Build the v2 Task snapshot from its owning AgentSession row."""
    return Task(
        id=str(row["session_id"]),
        owner_principal_id=str(row["task_owner_principal_id"]),
        controller_id=str(row["task_controller_id"]),
        origin=json.loads(str(row["task_origin_json"])),
        current_revision=int(row["authority_revision"]),
        status=TaskStatus(str(row["task_status"])),
        reason=TaskReason(
            code=str(row["task_reason_code"]),
            summary=str(row["task_reason_summary"]),
            recoverable=bool(row["task_reason_recoverable"]),
        ),
        priority=int(row["task_priority"]),
        current_subtask_id=row["task_current_subtask_id"],
        next_wake_at=row["task_next_wake_at"],
        integrity_state=TaskIntegrityState(str(row["task_integrity_state"])),
        integrity_reason_code=row["task_integrity_reason_code"],
        archived_at=row["task_archived_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        terminal_at=row["task_terminal_at"],
    )


def _runner_dispatch(row: sqlite3.Row) -> RunnerDispatchCommit:
    request = RunnerDispatchRequest(
        step_id=str(row["step_id"]),
        action_id=str(row["action_id"]),
        command_id=str(row["command_id"]),
        runner_kind=str(row["runner_kind"]),
        runner_version=str(row["runner_version"]),
        subtask_id=str(row["subtask_id"]),
        profile_id=str(row["profile_id"]),
        profile_generation=int(row["profile_generation"]),
        device_boot_id=str(row["device_boot_id"]),
        canonical_device_id=str(row["canonical_device_id"]),
        command_type=str(row["command_type"]),
        payload_digest=str(row["payload_digest"]),
        expected_revision=int(row["authority_revision"]),
    )
    return RunnerDispatchCommit(
        dispatch_id=str(row["dispatch_id"]),
        task_id=str(row["session_id"]),
        owner_principal_id=str(row["owner_principal_id"]),
        controller_id=str(row["controller_id"]),
        request=request,
        committed_at=str(row["committed_at"]),
    )


def _task_revision(row: sqlite3.Row) -> TaskRevision:
    return TaskRevision(
        id=str(row["task_revision_id"]), task_id=str(row["session_id"]),
        revision=int(row["revision"]), base_revision=int(row["base_revision"]),
        kind=TaskRevisionKind(str(row["kind"])), instruction=str(row["instruction"]),
        patch=json.loads(str(row["patch_json"])), effective_boundary=str(row["effective_boundary"]),
        requested_by=json.loads(str(row["requested_by_json"])),
        status=TaskRecordStatus(str(row["status"])), reason_code=row["reason_code"],
        created_at=str(row["created_at"]), applied_at=row["applied_at"],
    )


def _task_control(row: sqlite3.Row) -> TaskControl:
    return TaskControl(
        id=str(row["control_id"]), task_id=str(row["session_id"]),
        action=TaskControlAction(str(row["action"])), idempotency_key=str(row["idempotency_key"]),
        expected_revision=int(row["expected_revision"]), requested_by=json.loads(str(row["requested_by_json"])),
        status=TaskRecordStatus(str(row["status"])), reason_code=row["reason_code"],
        requested_at=str(row["requested_at"]), applied_at=row["applied_at"],
    )


def _task_subtask(row: sqlite3.Row) -> TaskSubtask:
    return TaskSubtask(
        id=str(row["subtask_id"]), task_id=str(row["session_id"]), kind=str(row["kind"]),
        object_ref=row["object_ref"], conversation_ref=row["conversation_ref"], status=str(row["status"]),
        priority=int(row["priority"]), current_stage=row["current_stage"],
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
    )


def _directive(row: sqlite3.Row) -> UserDirective:
    return UserDirective(
        id=str(row["directive_id"]), session_id=str(row["session_id"]), revision=int(row["revision"]),
        content=str(row["content"]), directive_kind=DirectiveKind(str(row["directive_kind"])),
        source_message_id=str(row["source_message_id"]), created_at=str(row["created_at"]),
    )


def _goal_node(row: sqlite3.Row) -> GoalNode:
    return GoalNode(
        id=str(row["goal_node_id"]), session_id=str(row["session_id"]), title=str(row["title"]),
        source_directive_id=str(row["source_directive_id"]), status=GoalNodeStatus(str(row["status"])),
        bound_goal_run_id=row["bound_goal_run_id"], created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        graph_revision=int(row["graph_revision"]), original_fragment=row["original_fragment"],
        goal_family=row["goal_family"], application_hint=row["application_hint"], account_hint=row["account_hint"],
        explicit_priority=row["explicit_priority"], waiting_kind=row["waiting_kind"], waiting_ref=row["waiting_ref"],
        current_stage_id=row["current_stage_id"], continuation_id=row["continuation_id"],
        saturation=float(row["saturation"]), progress_summary=row["progress_summary"],
        verified_result_ref=row["verified_result_ref"],
        scheduling_class=GoalSchedulingClass(str(row["scheduling_class"])),
        next_eligible_at=row["next_eligible_at"], last_selected_at=row["last_selected_at"],
        last_service_at=row["last_service_at"], wait_started_at=row["wait_started_at"],
        consecutive_failure_count=int(row["consecutive_failure_count"]), backoff_until=row["backoff_until"],
    )


def _binding(row: sqlite3.Row) -> SessionGoalBinding:
    return SessionGoalBinding(
        id=str(row["binding_id"]), session_id=str(row["session_id"]),
        goal_node_id=str(row["goal_node_id"]), goal_run_id=str(row["goal_run_id"]),
        status=GoalBindingStatus(str(row["status"])), created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _event(row: sqlite3.Row) -> SessionEvent:
    return SessionEvent(
        id=str(row["event_id"]), cursor=int(row["cursor"]), session_id=str(row["session_id"]),
        event_type=SessionEventType(str(row["event_type"])), data=json.loads(str(row["data_json"])),
        idempotency_key=str(row["idempotency_key"]),
        handling_status=EventHandlingStatus(str(row["handling_status"])),
        created_at=str(row["created_at"]), handled_at=row["handled_at"],
        source_namespace=row["source_namespace"] if "source_namespace" in row.keys() else None,
        source_event_id=row["source_event_id"] if "source_event_id" in row.keys() else None,
        device_id=row["device_id"] if "device_id" in row.keys() else None,
        device_boot_id=row["device_boot_id"] if "device_boot_id" in row.keys() else None,
        source_cursor=row["source_cursor"] if "source_cursor" in row.keys() else None,
        occurred_at=row["occurred_at"] if "occurred_at" in row.keys() else None,
        received_at=row["received_at"] if "received_at" in row.keys() else None,
        classified_at=row["classified_at"] if "classified_at" in row.keys() else None,
        affected_goal_ids=tuple(json.loads(str(row["affected_goal_ids_json"]))) if "affected_goal_ids_json" in row.keys() else (),
        decision_id=row["decision_id"] if "decision_id" in row.keys() else None,
        error=row["error"] if "error" in row.keys() else None,
        payload_digest=row["payload_digest"] if "payload_digest" in row.keys() else None,
    )


def _preemption_request(row: sqlite3.Row) -> PreemptionRequest:
    return PreemptionRequest(
        id=str(row["preemption_request_id"]),
        session_id=str(row["session_id"]),
        event_id=str(row["event_id"]),
        prior_goal_id=str(row["prior_goal_id"]),
        prior_slice_id=str(row["prior_slice_id"]),
        status=PreemptionRequestStatus(str(row["status"])),
        reason=str(row["reason"]),
        idempotency_key=str(row["idempotency_key"]),
        checkpoint_ref=row["checkpoint_ref"],
        continuation_id=row["continuation_id"],
        attention_decision_id=row["attention_decision_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _control_transition(row: sqlite3.Row) -> SessionControlTransition:
    return SessionControlTransition(
        id=str(row["transition_id"]),
        session_id=str(row["session_id"]),
        event_id=str(row["event_id"]),
        from_mode=SessionControlMode(str(row["from_mode"])),
        to_mode=SessionControlMode(str(row["to_mode"])),
        reason=str(row["reason"]),
        idempotency_key=str(row["idempotency_key"]),
        fresh_observation_ref=row["fresh_observation_ref"],
        created_at=str(row["created_at"]),
    )


_RAW_OBSERVATION_EVENT_TYPES = frozenset(
    {
        SessionEventType.NOTIFICATION_POSTED,
        SessionEventType.NOTIFICATION_REMOVED,
        SessionEventType.FOREGROUND_APPLICATION_CHANGED,
        SessionEventType.HUMAN_TOUCH_STARTED,
        SessionEventType.HUMAN_TOUCH_ENDED,
        SessionEventType.HUMAN_IDLE,
        SessionEventType.SCREEN_STATE_CHANGED,
        SessionEventType.LOCK_STATE_CHANGED,
        SessionEventType.NETWORK_STATE_CHANGED,
        SessionEventType.ORIENTATION_CHANGED,
    }
)


def _raw_observation(event: SessionEvent) -> RawObservation:
    payload = dict(event.data)
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        facts = {}
    provenance = facts.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {
            "source_namespace": event.source_namespace,
            "source_event_id": event.source_event_id,
            "source_cursor": event.source_cursor,
            "device_id": event.device_id,
            "device_boot_id": event.device_boot_id,
        }
    raw = facts.get("raw_observation")
    if not isinstance(raw, dict):
        raw = payload
    foreground_package = (
        payload.get("foreground_package")
        or facts.get("foreground_package")
        or raw.get("foreground_package")
    )
    foreground_activity = (
        payload.get("foreground_activity")
        or facts.get("foreground_activity")
        or raw.get("foreground_activity")
    )
    return RawObservation(
        event_id=event.id,
        session_id=event.session_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at or event.created_at,
        received_at=event.received_at or event.created_at,
        source_namespace=event.source_namespace or "audit",
        source_event_id=event.source_event_id or event.id,
        foreground_package=(
            str(foreground_package) if foreground_package else None
        ),
        foreground_activity=(
            str(foreground_activity) if foreground_activity else None
        ),
        provenance=provenance,
        payload=raw,
    )


def _attention_decision(row: sqlite3.Row) -> AttentionDecision:
    return AttentionDecision(
        id=str(row["decision_id"]), session_id=str(row["session_id"]),
        decision_revision=int(row["decision_revision"]), trigger_event_id=row["trigger_event_id"],
        trigger_key=str(row["trigger_key"]), authority_revision=int(row["authority_revision"]),
        graph_revision=int(row["graph_revision"]), event_cursor=int(row["event_cursor"]),
        agenda_revision=int(row["agenda_revision"]) if "agenda_revision" in row.keys() else 0,
        outcome=AttentionDecisionOutcome(str(row["outcome"])),
        candidate_goal_ids=tuple(json.loads(str(row["candidate_goal_ids_json"]))),
        selected_goal_id=row["selected_goal_id"], selector_kind=AttentionSelectorKind(str(row["selector_kind"])),
        selected_hard_tier=row["selected_hard_tier"], reason=str(row["reason"]),
        user_priority_component=int(row["user_priority_component"]),
        event_urgency_component=int(row["event_urgency_component"]),
        waiting_age_component=int(row["waiting_age_component"]),
        starvation_component=int(row["starvation_component"]),
        continuity_component=int(row["continuity_component"]), app_switch_cost=int(row["app_switch_cost"]),
        backoff_component=int(row["backoff_component"]),
        recent_failure_component=int(row["recent_failure_component"]), base_score=int(row["base_score"]),
        model_adjustment=int(row["model_adjustment"]),
        slice_budget=SliceBudgetDraft(
            time_budget_ms=int(row["slice_time_budget_ms"]), action_budget=int(row["slice_action_budget"]),
            checkpoint_policy=str(row["checkpoint_policy"]),
        ),
        preemption_policy=PreemptionPolicy(str(row["preemption_policy"])),
        preemption_checkpoint_ref=row["preemption_checkpoint_ref"], created_at=str(row["created_at"]),
    )


def _goal_eligibility(row: sqlite3.Row) -> GoalEligibility:
    return GoalEligibility(
        decision_id=str(row["decision_id"]), goal_id=str(row["goal_node_id"]),
        eligibility=GoalEligibilityStatus(str(row["eligibility"])), reason=str(row["reason"]),
        goal_status=GoalNodeStatus(str(row["goal_status"])),
        scheduling_class=GoalSchedulingClass(str(row["scheduling_class"])), binding_status=row["binding_status"],
        wake_condition_id=row["wake_condition_id"], next_eligible_at=row["next_eligible_at"],
        continuation_revision=row["continuation_revision"], latest_directive_target=bool(row["latest_directive_target"]),
        matched_trigger_event=bool(row["matched_trigger_event"]), hard_tier=int(row["hard_tier"]),
        user_priority_component=int(row["user_priority_component"]), event_urgency_component=int(row["event_urgency_component"]),
        waiting_age_component=int(row["waiting_age_component"]), starvation_component=int(row["starvation_component"]),
        continuity_component=int(row["continuity_component"]), app_switch_cost=int(row["app_switch_cost"]),
        backoff_component=int(row["backoff_component"]), recent_failure_component=int(row["recent_failure_component"]),
        total_score=int(row["total_score"]), rank=row["rank"],
    )


def _attention_dispatch(row: sqlite3.Row) -> AttentionDispatch:
    return AttentionDispatch(
        id=str(row["dispatch_id"]), session_id=str(row["session_id"]), decision_id=str(row["decision_id"]),
        goal_id=str(row["goal_node_id"]), status=AttentionDispatchStatus(str(row["status"])),
        idempotency_key=str(row["idempotency_key"]), attempt_count=int(row["attempt_count"]),
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]), delivered_at=row["delivered_at"],
        last_error=row["last_error"],
    )


def _continuation(row: sqlite3.Row) -> Continuation:
    return Continuation(
        id=str(row["continuation_id"]), session_id=str(row["session_id"]), goal_id=str(row["goal_node_id"]),
        revision=int(row["revision"]), authority_revision=int(row["authority_revision"]),
        graph_revision=int(row["graph_revision"]), attention_decision_id=row["attention_decision_id"],
        checkpoint_kind=ContinuationCheckpointKind(str(row["checkpoint_kind"])), checkpoint_ref=row["checkpoint_ref"],
        stage_id=row["stage_id"], application_package=row["application_package"], scene_ref=row["scene_ref"],
        person_id=row["person_id"], conversation_id=row["conversation_id"],
        verified_fact_refs=tuple(json.loads(str(row["verified_fact_refs_json"]))),
        pending_intent=json.loads(str(row["pending_intent_json"])) if row["pending_intent_json"] is not None else None,
        waiting_kind=row["waiting_kind"], waiting_ref=row["waiting_ref"],
        resume_preconditions=json.loads(str(row["resume_preconditions_json"])), next_eligible_at=row["next_eligible_at"],
        yield_reason=ContinuationYieldReason(str(row["yield_reason"])), idempotency_key=str(row["idempotency_key"]),
        created_at=str(row["created_at"]),
    )


def _wake_condition(row: sqlite3.Row) -> WakeCondition:
    return WakeCondition(
        id=str(row["wake_condition_id"]), session_id=str(row["session_id"]), goal_id=str(row["goal_node_id"]),
        revision=int(row["revision"]), kind=WakeConditionKind(str(row["kind"])),
        status=WakeConditionStatus(str(row["status"])),
        matcher=json.loads(str(row["matcher_json"])) if row["matcher_json"] is not None else None,
        due_at=row["due_at"], created_by_event_id=row["created_by_event_id"],
        created_by_decision_id=row["created_by_decision_id"], satisfied_by_event_id=row["satisfied_by_event_id"],
        created_at=str(row["created_at"]), satisfied_at=row["satisfied_at"], superseded_at=row["superseded_at"],
    )


def _outbox(row: sqlite3.Row) -> DurableOutboxIntent:
    return DurableOutboxIntent(
        id=str(row["intent_id"]), session_id=str(row["session_id"]), goal_node_id=str(row["goal_node_id"]),
        intent_type=OutboxIntentType(str(row["intent_type"])), status=OutboxIntentStatus(str(row["status"])),
        idempotency_key=str(row["idempotency_key"]), goal_text=str(row["goal_text"]),
        attempt_count=int(row["attempt_count"]), created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]), delivered_at=row["delivered_at"], last_error=row["last_error"],
        graph_revision=int(row["graph_revision"]),
    )


def _graph_revision(row: sqlite3.Row) -> GoalGraphRevision:
    return GoalGraphRevision(
        id=str(row["graph_revision_id"]), session_id=str(row["session_id"]), revision=int(row["revision"]),
        authority_revision=int(row["authority_revision"]), source_directive_id=str(row["source_directive_id"]),
        reason=row["reason"], created_at=str(row["created_at"]),
    )


def _edge(row: sqlite3.Row) -> GoalEdge:
    return GoalEdge(
        id=str(row["edge_id"]), session_id=str(row["session_id"]), from_goal_id=str(row["from_goal_id"]),
        to_goal_id=str(row["to_goal_id"]), edge_kind=GoalEdgeKind(str(row["edge_kind"])),
        evidence_ref=row["evidence_ref"], revision=int(row["revision"]), created_at=str(row["created_at"]),
    )


def _criterion(row: sqlite3.Row) -> GoalCriterion:
    return GoalCriterion(
        id=str(row["criterion_id"]), goal_id=str(row["goal_node_id"]), revision=int(row["revision"]),
        description=str(row["description"]), evidence_requirement=row["evidence_requirement"],
        required=bool(row["required"]), status=GoalCriterionStatus(str(row["status"])),
        evidence_refs=tuple(json.loads(str(row["evidence_refs_json"]))), created_at=str(row["created_at"]),
    )


def _coverage(row: sqlite3.Row) -> GoalCoverage:
    return GoalCoverage(
        id=str(row["coverage_id"]), session_id=str(row["session_id"]), graph_revision=int(row["graph_revision"]),
        source_directive_id=str(row["source_directive_id"]), original_fragment=str(row["original_fragment"]),
        coverage_kind=GoalCoverageKind(str(row["coverage_kind"])), target_goal_id=row["target_goal_id"],
        session_policy=row["session_policy"], created_at=str(row["created_at"]),
    )


def _event_matches_wake(event: SessionEvent, wake: WakeCondition) -> bool:
    """Deterministic R3 matcher; model inference is never used to wake Goals."""

    payload = event.data
    matcher = wake.matcher or {}
    if wake.kind is WakeConditionKind.TIME:
        return (
            event.event_type is SessionEventType.TIMER_DUE
            and payload.get("wake_condition_id") == wake.id
        )
    if wake.kind is WakeConditionKind.USER_FACT:
        return (
            event.event_type is SessionEventType.USER_FACT_ANSWERED
            and bool(matcher.get("need_id"))
            and payload.get("need_id") == matcher.get("need_id")
            and (
                matcher.get("fact_key") is None
                or payload.get("fact_key") == matcher.get("fact_key")
            )
        )
    if wake.kind is WakeConditionKind.DEVICE:
        if event.event_type is SessionEventType.TIMER_DUE:
            return payload.get("wake_condition_id") == wake.id
        if event.event_type not in {
            SessionEventType.DEVICE_AVAILABLE,
            SessionEventType.COMPANION_CONNECTED,
        }:
            return False
    elif wake.kind is WakeConditionKind.EVENT:
        expected_type = matcher.get("event_type", SessionEventType.NOTIFICATION_POSTED.value)
        if event.event_type.value != expected_type:
            return False
    else:
        expected_type = matcher.get("event_type")
        if expected_type is None or event.event_type.value != expected_type:
            return False
    for key in ("application_package", "person_hint", "conversation_hint"):
        expected = matcher.get(key)
        if expected is not None and payload.get(key) != expected:
            return False
    expected_device = matcher.get("device_id")
    if expected_device is not None and event.device_id != expected_device:
        return False
    return True


def _control_state(session: AgentSession, action: str) -> tuple[SessionStatus, SessionControlMode]:
    target: tuple[SessionStatus, SessionControlMode]
    if action == "pause":
        target = (SessionStatus.USER_ACTIVE, SessionControlMode.USER_ACTIVE)
    elif action == "resume":
        target = (SessionStatus.ACTIVE, SessionControlMode.AGENT_ACTIVE)
    elif action == "takeover":
        target = (SessionStatus.USER_ACTIVE, SessionControlMode.TAKEOVER)
    elif action == "stop":
        target = (SessionStatus.STOPPING, SessionControlMode.STOPPING)
    else:
        raise ValueError(f"unsupported session control: {action}")
    if not can_transition_session(session.status, target[0]):
        from .domain import SessionStateConflict
        raise SessionStateConflict(
            f"Session 状态 {session.status.value} 不允许执行 {action}。"
        )
    return target


def _goal_node_status(execution_status: Any) -> GoalNodeStatus:
    status = str(execution_status or "")
    if status == "COMPLETED":
        return GoalNodeStatus.COMPLETED
    if status in {"PARTIAL"}:
        return GoalNodeStatus.PARTIAL
    if status in {"FAILED", "UNCERTAIN"}:
        return GoalNodeStatus.FAILED
    if status in {"CANCELLED", "STOPPED"}:
        return GoalNodeStatus.CANCELLED
    if status == "CANDIDATE_COMPLETE":
        return GoalNodeStatus.CANDIDATE_COMPLETE
    if status.startswith("WAITING"):
        return GoalNodeStatus.WAITING_DEVICE
    if status in {"ACCEPTED", "PLANNED"}:
        return GoalNodeStatus.PLANNED
    return GoalNodeStatus.ACTIVE


def _session_status_for_goal(
    current: SessionStatus, node_status: GoalNodeStatus, sibling_statuses: Sequence[GoalNodeStatus] = ()
) -> SessionStatus:
    """Aggregate every GoalNode; one wait/failure cannot settle its Session."""
    if current in {SessionStatus.STOPPING, SessionStatus.STOPPED}:
        return current
    statuses = [*sibling_statuses, node_status]
    nonterminal = [status for status in statuses if not status.terminal]
    if nonterminal:
        target = (
            SessionStatus.WAITING_ALL
            if all(status.value.startswith("WAITING_") for status in nonterminal)
            else SessionStatus.ACTIVE
        )
        return target if can_transition_session(current, target) else current
    if statuses and all(status is GoalNodeStatus.COMPLETED for status in statuses):
        target = SessionStatus.COMPLETED
    elif statuses and all(status is GoalNodeStatus.FAILED for status in statuses):
        target = SessionStatus.FAILED
    else:
        target = SessionStatus.PARTIAL
    return target if can_transition_session(current, target) else current


def _goal_run_id(goal_run: Any) -> str:
    value = _value(goal_run, "id")
    if not isinstance(value, str) or not value:
        raise TypeError("GoalRun projection requires a non-blank id")
    return value


def _value(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _goal_create_key(session_id: str, goal_node_id: str) -> str:
    return "r1-goal:" + hashlib.sha256(
        f"ai-game:r1:{session_id}:{goal_node_id}".encode("utf-8")
    ).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _normalize_task_origin(origin: Mapping[str, Any]) -> dict[str, str]:
    """Keep provenance separate from ownership and reject accidental secrets."""
    allowed = {
        "dsh_session_id", "created_execution_id", "created_turn_id",
        "created_tool_call_id", "root_call_id", "runner_kind", "runner_version",
    }
    unknown = set(origin) - allowed
    if unknown:
        raise ValueError("task origin contains unsupported fields")
    normalized: dict[str, str] = {}
    for key, value in origin.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"task origin {key} must be non-empty text")
        normalized[key] = value
    return normalized


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
