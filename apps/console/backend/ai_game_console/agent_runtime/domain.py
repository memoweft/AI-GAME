from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any


SCHEMA_REVISION = 6
"""The durable AgentSession contract, including the canonical Task aggregate."""


class SessionKind(str, Enum):
    TODAY = "today"
    OPEN_ENDED = "open_ended"
    BOUNDED = "bounded"


class SessionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    USER_ACTIVE = "USER_ACTIVE"
    WAITING_ALL = "WAITING_ALL"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {
            SessionStatus.STOPPED,
            SessionStatus.COMPLETED,
            SessionStatus.PARTIAL,
            SessionStatus.FAILED,
        }


class SessionControlMode(str, Enum):
    AGENT_ACTIVE = "AGENT_ACTIVE"
    USER_ACTIVE = "USER_ACTIVE"
    RECOVERING_CONTEXT = "RECOVERING_CONTEXT"
    TAKEOVER = "TAKEOVER"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class SessionControlAction(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    TAKEOVER = "takeover"
    STOP = "stop"


class DirectiveKind(str, Enum):
    ORIGINAL = "original"
    ADD = "add"
    REVISE = "revise"
    REPRIORITIZE = "reprioritize"
    STOP = "stop"


class GoalNodeStatus(str, Enum):
    PLANNED = "PLANNED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    WAITING_EVENT = "WAITING_EVENT"
    WAITING_TIME = "WAITING_TIME"
    WAITING_USER_FACT = "WAITING_USER_FACT"
    WAITING_DEVICE = "WAITING_DEVICE"
    WAITING_ACCOUNT = "WAITING_ACCOUNT"
    WAITING_IDENTITY = "WAITING_IDENTITY"
    SATURATED = "SATURATED"
    CANDIDATE_COMPLETE = "CANDIDATE_COMPLETE"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            GoalNodeStatus.COMPLETED,
            GoalNodeStatus.PARTIAL,
            GoalNodeStatus.FAILED,
            GoalNodeStatus.CANCELLED,
        }


class GoalBindingStatus(str, Enum):
    REQUESTED = "REQUESTED"
    BOUND = "BOUND"
    RELEASED = "RELEASED"
    FAILED = "FAILED"

    @property
    def active(self) -> bool:
        return self in {GoalBindingStatus.REQUESTED, GoalBindingStatus.BOUND}


ACTIVE_BINDING_STATUSES = frozenset(
    {GoalBindingStatus.REQUESTED, GoalBindingStatus.BOUND}
)
"""Statuses covered by the store's one-active-binding-per-GoalNode fence."""


class SessionEventType(str, Enum):
    SESSION_CREATED = "session_created"
    DIRECTIVE_RECORDED = "directive_recorded"
    GOAL_NODE_CREATED = "goal_node_created"
    GOAL_ACTIVATION_REQUESTED = "goal_activation_requested"
    GOAL_GRAPH_REVISION_APPLIED = "goal_graph_revision_applied"
    GOAL_RUN_BOUND = "goal_run_bound"
    GOAL_PROJECTION_CHANGED = "goal_projection_changed"
    CONTROL_REQUESTED = "control_requested"
    CONTROL_SETTLED = "control_settled"
    SESSION_STOPPED = "session_stopped"
    ATTENTION_DECISION_COMMITTED = "attention_decision_committed"
    USER_DIRECTIVE_EVENT = "UserDirectiveEvent"
    GOAL_STATE_CHANGED = "GoalStateChangedEvent"
    NOTIFICATION_POSTED = "NotificationPostedEvent"
    NOTIFICATION_REMOVED = "NotificationRemovedEvent"
    FOREGROUND_APPLICATION_CHANGED = "ForegroundApplicationChangedEvent"
    HUMAN_TOUCH_STARTED = "HumanTouchStartedEvent"
    HUMAN_TOUCH_ENDED = "HumanTouchEndedEvent"
    HUMAN_IDLE = "HumanIdleEvent"
    SCREEN_STATE_CHANGED = "ScreenStateChangedEvent"
    LOCK_STATE_CHANGED = "LockStateChangedEvent"
    NETWORK_STATE_CHANGED = "NetworkStateChangedEvent"
    ORIENTATION_CHANGED = "OrientationChangedEvent"
    CAPABILITIES_CHANGED = "CapabilitiesChangedEvent"
    COMPANION_CONNECTED = "CompanionConnectedEvent"
    COMPANION_DISCONNECTED = "CompanionDisconnectedEvent"
    COMPANION_HEARTBEAT = "CompanionHeartbeatEvent"
    TIMER_DUE = "TimerDueEvent"
    DEVICE_BUSY = "DeviceBusyEvent"
    DEVICE_AVAILABLE = "DeviceAvailableEvent"
    USER_FACT_ANSWERED = "UserFactAnsweredEvent"
    BODY_EVENT = "BodyEvent"
    SCHEDULER_RECOVERY = "SchedulerRecoveryEvent"
    # The following events are the canonical external Task history.  They use
    # the existing Session event ledger: ``task_id`` is always ``session_id``.
    # Keeping one append-only ledger prevents a second task truth from
    # diverging from AgentSession during restart or partial integration.
    TASK_CREATED = "task.created"
    TASK_STATE_CHANGED = "task.state_changed"
    TASK_REVISION_ACCEPTED = "task.revision_accepted"
    TASK_REVISION_APPLIED = "task.revision_applied"
    TASK_CONTROL_ACCEPTED = "task.control_accepted"
    TASK_CONTROL_APPLIED = "task.control_applied"
    TASK_SUBTASK_UPSERTED = "task.subtask_upserted"
    TASK_RECOVERY_REQUESTED = "task.recovery_requested"
    TASK_REPLAN_REQUESTED = "task.replan_requested"
    TASK_INTEGRITY_BLOCKED = "integrity.blocked"
    TASK_ARCHIVED = "task.archived"
    TASK_DISPATCH_COMMITTED = "task.dispatch_committed"


class TaskStatus(str, Enum):
    """Stable v2 task lifecycle; these values deliberately differ from R1 statuses."""

    SCHEDULED = "scheduled"
    RUNNING = "running"
    WAITING_TIME = "waiting_time"
    WAITING_EVENT = "waiting_event"
    RECOVERING = "recovering"
    REPLANNING = "replanning"
    PAUSED = "paused"
    USER_TAKEOVER = "user_takeover"
    NEEDS_USER_INPUT = "needs_user_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class TaskControlAction(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    TAKEOVER = "takeover"
    RELEASE_TAKEOVER = "release_takeover"
    REPRIORITIZE = "reprioritize"
    ARCHIVE = "archive"


class TaskRevisionKind(str, Enum):
    ADD = "add"
    REVISE = "revise"
    REPRIORITIZE = "reprioritize"
    RESCHEDULE = "reschedule"
    CHANGE_STOP_CONDITION = "change_stop_condition"


class TaskRecordStatus(str, Enum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"


class TaskIntegrityState(str, Enum):
    CLEAR = "clear"
    BLOCKED = "blocked"


TASK_STATUS_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.SCHEDULED: frozenset({TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.WAITING_TIME, TaskStatus.WAITING_EVENT, TaskStatus.RECOVERING,
        TaskStatus.REPLANNING, TaskStatus.PAUSED, TaskStatus.USER_TAKEOVER,
        TaskStatus.NEEDS_USER_INPUT, TaskStatus.SUCCEEDED, TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.WAITING_TIME: frozenset({TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.USER_TAKEOVER, TaskStatus.CANCELLED}),
    TaskStatus.WAITING_EVENT: frozenset({TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.USER_TAKEOVER, TaskStatus.CANCELLED}),
    TaskStatus.RECOVERING: frozenset({
        TaskStatus.RUNNING, TaskStatus.REPLANNING, TaskStatus.WAITING_TIME,
        TaskStatus.WAITING_EVENT, TaskStatus.NEEDS_USER_INPUT, TaskStatus.PAUSED, TaskStatus.USER_TAKEOVER, TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.REPLANNING: frozenset({
        TaskStatus.RUNNING, TaskStatus.WAITING_TIME, TaskStatus.WAITING_EVENT,
        TaskStatus.NEEDS_USER_INPUT, TaskStatus.PAUSED, TaskStatus.USER_TAKEOVER, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.PAUSED: frozenset({
        TaskStatus.SCHEDULED, TaskStatus.RUNNING, TaskStatus.WAITING_TIME,
        TaskStatus.WAITING_EVENT, TaskStatus.CANCELLED,
    }),
    TaskStatus.USER_TAKEOVER: frozenset({TaskStatus.REPLANNING, TaskStatus.PAUSED, TaskStatus.CANCELLED}),
    TaskStatus.NEEDS_USER_INPUT: frozenset({TaskStatus.REPLANNING, TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.USER_TAKEOVER, TaskStatus.CANCELLED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def can_transition_task(current: TaskStatus, target: TaskStatus) -> bool:
    """Return whether a canonical Task state change is legal and non-reviving."""

    return target == current or target in TASK_STATUS_TRANSITIONS[current]


class EventHandlingStatus(str, Enum):
    RECEIVED = "RECEIVED"
    CLASSIFIED = "CLASSIFIED"
    HANDLED = "HANDLED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


class AttentionDecisionOutcome(str, Enum):
    SELECTED = "SELECTED"
    NO_ELIGIBLE = "NO_ELIGIBLE"


class AttentionSelectorKind(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    QWEN = "QWEN"


class GoalEligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    TERMINAL = "TERMINAL"
    NOT_BOUND = "NOT_BOUND"
    WAITING = "WAITING"
    BACKOFF = "BACKOFF"
    CONTROL_BLOCKED = "CONTROL_BLOCKED"
    DEPENDENCY_BLOCKED = "DEPENDENCY_BLOCKED"
    IDLE_DEFERRED = "IDLE_DEFERRED"
    UNCHECKPOINTED = "UNCHECKPOINTED"


class GoalSchedulingClass(str, Enum):
    NORMAL = "NORMAL"
    IDLE_ONLY = "IDLE_ONLY"


class WakeConditionKind(str, Enum):
    EVENT = "EVENT"
    TIME = "TIME"
    USER_FACT = "USER_FACT"
    DEVICE = "DEVICE"
    ACCOUNT = "ACCOUNT"
    IDENTITY = "IDENTITY"


class WakeConditionStatus(str, Enum):
    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class ContinuationCheckpointKind(str, Enum):
    NO_INFLIGHT_ACTION = "NO_INFLIGHT_ACTION"
    SCHEDULER_BOUNDARY = "SCHEDULER_BOUNDARY"
    VERIFIED_ACTION = "VERIFIED_ACTION"
    WAIT_ENTRY = "WAIT_ENTRY"


class ContinuationYieldReason(str, Enum):
    PREEMPTED = "PREEMPTED"
    WAITING = "WAITING"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    USER_ACTIVITY = "USER_ACTIVITY"
    DEVICE_BUSY = "DEVICE_BUSY"
    PROCESS_RESTART = "PROCESS_RESTART"


class AttentionDispatchStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DELIVERED = "DELIVERED"
    RETRYABLE = "RETRYABLE"
    FAILED = "FAILED"


class PreemptionPolicy(str, Enum):
    VERIFIED_CHECKPOINT_ONLY = "VERIFIED_CHECKPOINT_ONLY"


class PreemptionRequestStatus(str, Enum):
    PENDING_CHECKPOINT = "PENDING_CHECKPOINT"
    CHECKPOINTED = "CHECKPOINTED"
    DECIDED = "DECIDED"
    CANCELLED = "CANCELLED"


class OutboxIntentType(str, Enum):
    CREATE_GOAL_RUN = "create_goal_run"


class GoalEdgeKind(str, Enum):
    BLOCKS = "BLOCKS"
    UNBLOCKS = "UNBLOCKS"
    CONTRIBUTES_TO = "CONTRIBUTES_TO"
    REPLACES = "REPLACES"
    SPLITS_FROM = "SPLITS_FROM"
    SHARES_CONTEXT_WITH = "SHARES_CONTEXT_WITH"


class GoalCriterionStatus(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    NOT_MET = "NOT_MET"
    WAIVED = "WAIVED"


class GoalCoverageKind(str, Enum):
    GOAL = "GOAL"
    SESSION_POLICY = "SESSION_POLICY"


class OutboxIntentStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


SESSION_STATUS_TRANSITIONS: Mapping[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.ACCEPTED: frozenset(
        {SessionStatus.PLANNING, SessionStatus.ACTIVE, SessionStatus.STOPPING, SessionStatus.FAILED}
    ),
    SessionStatus.PLANNING: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.WAITING_ALL, SessionStatus.STOPPING, SessionStatus.FAILED}
    ),
    SessionStatus.ACTIVE: frozenset(
        {
            SessionStatus.USER_ACTIVE,
            SessionStatus.WAITING_ALL,
            SessionStatus.STOPPING,
            SessionStatus.COMPLETED,
            SessionStatus.PARTIAL,
            SessionStatus.FAILED,
        }
    ),
    SessionStatus.USER_ACTIVE: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.WAITING_ALL, SessionStatus.STOPPING, SessionStatus.FAILED}
    ),
    SessionStatus.WAITING_ALL: frozenset(
        {
            SessionStatus.ACTIVE,
            SessionStatus.USER_ACTIVE,
            SessionStatus.STOPPING,
            SessionStatus.COMPLETED,
            SessionStatus.PARTIAL,
            SessionStatus.FAILED,
        }
    ),
    SessionStatus.STOPPING: frozenset({SessionStatus.STOPPED, SessionStatus.FAILED}),
    SessionStatus.STOPPED: frozenset(),
    SessionStatus.COMPLETED: frozenset(),
    SessionStatus.PARTIAL: frozenset(),
    SessionStatus.FAILED: frozenset(),
}


def can_transition_session(current: SessionStatus, target: SessionStatus) -> bool:
    """Return whether a persisted Session lifecycle transition is legal."""

    return target == current or target in SESSION_STATUS_TRANSITIONS[current]


class AgentRuntimeError(RuntimeError):
    code = "agent_runtime_error"
    status_code = 409

    def as_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": str(self)}}


class SessionNotFound(AgentRuntimeError):
    code = "session_not_found"
    status_code = 404


class SessionIdempotencyConflict(AgentRuntimeError):
    code = "session_idempotency_conflict"


class SessionStateConflict(AgentRuntimeError):
    code = "session_state_conflict"


class SessionControlUnsupported(AgentRuntimeError):
    code = "session_control_unsupported"


class TaskRevisionConflict(AgentRuntimeError):
    code = "task_revision_conflict"


class TaskControlConflict(AgentRuntimeError):
    code = "task_control_conflict"


class TaskDispatchConflict(TaskControlConflict):
    """A runner dispatch cannot be linearized with Task authority safely."""

    code = "task_dispatch_conflict"


@dataclass(frozen=True, slots=True)
class TaskReason:
    code: str
    summary: str
    recoverable: bool

    def __post_init__(self) -> None:
        _require_text(self.code, "task reason code")
        _require_text(self.summary, "task reason summary")


@dataclass(frozen=True, slots=True)
class Task:
    """Safe long-task view rooted in exactly one :class:`AgentSession`.

    ``id`` is intentionally not generated here.  Store code constructs it
    from the existing AgentSession primary key so an external caller cannot
    accidentally create an independent top-level task identity.
    """

    id: str
    owner_principal_id: str
    controller_id: str
    origin: Mapping[str, Any]
    current_revision: int
    status: TaskStatus
    reason: TaskReason
    priority: int
    current_subtask_id: str | None
    next_wake_at: str | None
    integrity_state: TaskIntegrityState
    integrity_reason_code: str | None
    archived_at: str | None
    created_at: str
    updated_at: str
    terminal_at: str | None

    def __post_init__(self) -> None:
        _require_text(self.id, "task id")
        _require_text(self.owner_principal_id, "task owner_principal_id")
        _require_text(self.controller_id, "task controller_id")
        _require_json(self.origin, "task origin")
        if self.current_revision < 1:
            raise ValueError("task current_revision must be positive")
        if not 0 <= self.priority <= 100:
            raise ValueError("task priority must be between 0 and 100")
        _require_optional_text(self.current_subtask_id, "task current_subtask_id")
        _require_optional_utc(self.next_wake_at, "task next_wake_at")
        _require_optional_text(self.integrity_reason_code, "task integrity_reason_code")
        _require_optional_utc(self.archived_at, "task archived_at")
        _require_utc(self.created_at, "task created_at")
        _require_utc(self.updated_at, "task updated_at")
        _require_optional_utc(self.terminal_at, "task terminal_at")
        if self.status.terminal and self.terminal_at is None:
            raise ValueError("a terminal task requires terminal_at")
        if self.integrity_state is TaskIntegrityState.BLOCKED and not self.integrity_reason_code:
            raise ValueError("a blocked task requires integrity_reason_code")

    @property
    def task_id(self) -> str:
        return self.id

    @property
    def terminal(self) -> bool:
        return self.status.terminal


@dataclass(frozen=True, slots=True)
class TaskRevision:
    id: str
    task_id: str
    revision: int
    base_revision: int
    kind: TaskRevisionKind
    instruction: str
    patch: Mapping[str, Any]
    effective_boundary: str
    requested_by: Mapping[str, Any]
    status: TaskRecordStatus
    reason_code: str | None
    created_at: str
    applied_at: str | None

    def __post_init__(self) -> None:
        for value, label in ((self.id, "task revision id"), (self.task_id, "task revision task_id"),
                             (self.instruction, "task revision instruction"),
                             (self.effective_boundary, "task revision effective_boundary")):
            _require_text(value, label)
        if self.revision < 1 or self.base_revision < 0:
            raise ValueError("task revision numbers are invalid")
        _require_json(self.patch, "task revision patch")
        _require_json(self.requested_by, "task revision requested_by")
        _require_optional_text(self.reason_code, "task revision reason_code")
        _require_utc(self.created_at, "task revision created_at")
        _require_optional_utc(self.applied_at, "task revision applied_at")


@dataclass(frozen=True, slots=True)
class TaskControl:
    id: str
    task_id: str
    action: TaskControlAction
    idempotency_key: str
    expected_revision: int
    requested_by: Mapping[str, Any]
    status: TaskRecordStatus
    reason_code: str | None
    requested_at: str
    applied_at: str | None

    def __post_init__(self) -> None:
        for value, label in ((self.id, "task control id"), (self.task_id, "task control task_id"),
                             (self.idempotency_key, "task control idempotency_key")):
            _require_text(value, label)
        if self.expected_revision < 1:
            raise ValueError("task control expected_revision must be positive")
        _require_json(self.requested_by, "task control requested_by")
        _require_optional_text(self.reason_code, "task control reason_code")
        _require_utc(self.requested_at, "task control requested_at")
        _require_optional_utc(self.applied_at, "task control applied_at")


@dataclass(frozen=True, slots=True)
class TaskSubtask:
    id: str
    task_id: str
    kind: str
    object_ref: str | None
    conversation_ref: str | None
    status: str
    priority: int
    current_stage: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        for value, label in ((self.id, "subtask id"), (self.task_id, "subtask task_id"),
                             (self.kind, "subtask kind"), (self.status, "subtask status")):
            _require_text(value, label)
        _require_optional_text(self.object_ref, "subtask object_ref")
        _require_optional_text(self.conversation_ref, "subtask conversation_ref")
        _require_optional_text(self.current_stage, "subtask current_stage")
        if not 0 <= self.priority <= 100:
            raise ValueError("subtask priority must be between 0 and 100")
        _require_utc(self.created_at, "subtask created_at")
        _require_utc(self.updated_at, "subtask updated_at")


@dataclass(frozen=True, slots=True)
class RunnerDispatchRequest:
    """Bounded, secret-free identity for one runner physical boundary.

    The action payload itself is never stored in the canonical Task ledger.
    ``payload_digest`` must come from the generic command store after typed
    text has been redacted.
    """

    step_id: str
    action_id: str
    command_id: str
    runner_kind: str
    runner_version: str
    subtask_id: str
    profile_id: str
    profile_generation: int
    device_boot_id: str
    canonical_device_id: str
    command_type: str
    payload_digest: str
    expected_revision: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.step_id, "runner dispatch step_id"),
            (self.action_id, "runner dispatch action_id"),
            (self.command_id, "runner dispatch command_id"),
            (self.runner_kind, "runner dispatch runner_kind"),
            (self.runner_version, "runner dispatch runner_version"),
            (self.subtask_id, "runner dispatch subtask_id"),
            (self.profile_id, "runner dispatch profile_id"),
            (self.device_boot_id, "runner dispatch device_boot_id"),
            (self.canonical_device_id, "runner dispatch canonical_device_id"),
            (self.command_type, "runner dispatch command_type"),
            (self.payload_digest, "runner dispatch payload_digest"),
        ):
            _require_text(value, label)
            if len(value) > 512:
                raise ValueError(f"{label} is too long")
        if self.profile_generation < 1:
            raise ValueError("runner dispatch profile_generation must be positive")
        if self.expected_revision < 1:
            raise ValueError("runner dispatch expected_revision must be positive")
        if len(self.payload_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.payload_digest
        ):
            raise ValueError("runner dispatch payload_digest must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class RunnerDispatchCommit:
    """Durable fact returned by the canonical dispatch linearization point."""

    dispatch_id: str
    task_id: str
    owner_principal_id: str
    controller_id: str
    request: RunnerDispatchRequest
    committed_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.dispatch_id, "runner dispatch dispatch_id"),
            (self.task_id, "runner dispatch task_id"),
            (self.owner_principal_id, "runner dispatch owner_principal_id"),
            (self.controller_id, "runner dispatch controller_id"),
        ):
            _require_text(value, label)
        _require_utc(self.committed_at, "runner dispatch committed_at")


@dataclass(frozen=True, slots=True)
class AgentSession:
    id: str
    client_request_id: str
    original_instruction: str
    authority_revision: int
    session_kind: SessionKind
    status: SessionStatus
    control_mode: SessionControlMode
    device_binding_id: str | None
    active_goal_id: str | None
    active_slice_id: str | None
    event_cursor: int
    calendar_started_at: str
    calendar_window_end: str | None
    terminal_condition: str | None
    summary: str | None
    created_at: str
    updated_at: str
    stopped_at: str | None
    current_attention_decision_id: str | None = None
    agenda_revision: int = 0

    def __post_init__(self) -> None:
        _require_text(self.id, "session id")
        _require_text(self.client_request_id, "client_request_id")
        _require_text(self.original_instruction, "original instruction")
        if self.authority_revision < 1:
            raise ValueError("authority_revision must be positive")
        if self.event_cursor < 0:
            raise ValueError("event_cursor must not be negative")
        if self.agenda_revision < 0:
            raise ValueError("agenda_revision must not be negative")
        _require_utc(self.calendar_started_at, "calendar_started_at")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_optional_utc(self.calendar_window_end, "calendar_window_end")
        _require_optional_utc(self.stopped_at, "stopped_at")
        if self.status.terminal and self.status is not SessionStatus.COMPLETED:
            if self.status is SessionStatus.STOPPED and self.stopped_at is None:
                raise ValueError("a stopped session requires stopped_at")

    @property
    def terminal(self) -> bool:
        return self.status.terminal


@dataclass(frozen=True, slots=True)
class UserDirective:
    id: str
    session_id: str
    revision: int
    content: str
    directive_kind: DirectiveKind
    source_message_id: str
    created_at: str

    def __post_init__(self) -> None:
        _require_text(self.id, "directive id")
        _require_text(self.session_id, "session id")
        _require_text(self.content, "directive content")
        _require_text(self.source_message_id, "source_message_id")
        if self.revision < 1:
            raise ValueError("directive revision must be positive")
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class GoalNode:
    id: str
    session_id: str
    title: str
    source_directive_id: str
    status: GoalNodeStatus
    bound_goal_run_id: str | None
    created_at: str
    updated_at: str
    graph_revision: int = 1
    original_fragment: str | None = None
    goal_family: str | None = None
    application_hint: str | None = None
    account_hint: str | None = None
    explicit_priority: int | None = None
    waiting_kind: str | None = None
    waiting_ref: str | None = None
    current_stage_id: str | None = None
    continuation_id: str | None = None
    saturation: float = 0.0
    progress_summary: str | None = None
    verified_result_ref: str | None = None
    scheduling_class: GoalSchedulingClass = GoalSchedulingClass.NORMAL
    next_eligible_at: str | None = None
    last_selected_at: str | None = None
    last_service_at: str | None = None
    wait_started_at: str | None = None
    consecutive_failure_count: int = 0
    backoff_until: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.id, "goal node id")
        _require_text(self.session_id, "session id")
        _require_text(self.title, "goal title")
        _require_text(self.source_directive_id, "source directive id")
        if self.graph_revision < 1:
            raise ValueError("goal graph_revision must be positive")
        if not 0.0 <= self.saturation <= 1.0:
            raise ValueError("goal saturation must be between 0 and 1")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_optional_utc(self.next_eligible_at, "next_eligible_at")
        _require_optional_utc(self.last_selected_at, "last_selected_at")
        _require_optional_utc(self.last_service_at, "last_service_at")
        _require_optional_utc(self.wait_started_at, "wait_started_at")
        _require_optional_utc(self.backoff_until, "backoff_until")
        if self.consecutive_failure_count < 0:
            raise ValueError("consecutive_failure_count must not be negative")

    @property
    def terminal(self) -> bool:
        return self.status.terminal


@dataclass(frozen=True, slots=True)
class SessionGoalBinding:
    id: str
    session_id: str
    goal_node_id: str
    goal_run_id: str
    status: GoalBindingStatus
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _require_text(self.id, "binding id")
        _require_text(self.session_id, "session id")
        _require_text(self.goal_node_id, "goal node id")
        _require_text(self.goal_run_id, "goal run id")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")

    @property
    def active(self) -> bool:
        return self.status.active


@dataclass(frozen=True, slots=True)
class SessionEvent:
    id: str
    cursor: int
    session_id: str
    event_type: SessionEventType
    data: Mapping[str, Any]
    idempotency_key: str
    handling_status: EventHandlingStatus
    created_at: str
    handled_at: str | None
    source_namespace: str | None = None
    source_event_id: str | None = None
    device_id: str | None = None
    device_boot_id: str | None = None
    source_cursor: str | None = None
    occurred_at: str | None = None
    received_at: str | None = None
    classified_at: str | None = None
    affected_goal_ids: tuple[str, ...] = ()
    decision_id: str | None = None
    error: str | None = None
    payload_digest: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.id, "event id")
        _require_text(self.session_id, "session id")
        _require_text(self.idempotency_key, "event idempotency_key")
        if self.cursor < 1:
            raise ValueError("event cursor must be positive")
        _require_json(self.data, "event data")
        _require_utc(self.created_at, "created_at")
        _require_optional_utc(self.handled_at, "handled_at")
        _require_optional_utc(self.occurred_at, "occurred_at")
        _require_optional_utc(self.received_at, "received_at")
        _require_optional_utc(self.classified_at, "classified_at")
        if not isinstance(self.affected_goal_ids, tuple):
            raise ValueError("affected_goal_ids must be a tuple")


@dataclass(frozen=True, slots=True)
class PreemptionRequest:
    id: str
    session_id: str
    event_id: str
    prior_goal_id: str
    prior_slice_id: str
    status: PreemptionRequestStatus
    reason: str
    idempotency_key: str
    checkpoint_ref: str | None
    continuation_id: str | None
    attention_decision_id: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.id, "preemption request id"),
            (self.session_id, "preemption request session id"),
            (self.event_id, "preemption request event id"),
            (self.prior_goal_id, "preemption request prior goal id"),
            (self.prior_slice_id, "preemption request prior slice id"),
            (self.reason, "preemption request reason"),
            (self.idempotency_key, "preemption request idempotency key"),
        ):
            _require_text(value, label)
        _require_optional_text(self.checkpoint_ref, "preemption checkpoint ref")
        _require_optional_text(self.continuation_id, "preemption continuation id")
        _require_optional_text(
            self.attention_decision_id, "preemption attention decision id"
        )
        _require_utc(self.created_at, "preemption request created_at")
        _require_utc(self.updated_at, "preemption request updated_at")


@dataclass(frozen=True, slots=True)
class SessionControlTransition:
    id: str
    session_id: str
    event_id: str
    from_mode: SessionControlMode
    to_mode: SessionControlMode
    reason: str
    idempotency_key: str
    fresh_observation_ref: str | None
    created_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.id, "control transition id"),
            (self.session_id, "control transition session id"),
            (self.event_id, "control transition event id"),
            (self.reason, "control transition reason"),
            (self.idempotency_key, "control transition idempotency key"),
        ):
            _require_text(value, label)
        _require_optional_text(
            self.fresh_observation_ref, "control transition fresh observation ref"
        )
        _require_utc(self.created_at, "control transition created_at")


@dataclass(frozen=True, slots=True)
class RawObservation:
    event_id: str
    session_id: str
    event_type: SessionEventType
    occurred_at: str
    received_at: str
    source_namespace: str
    source_event_id: str
    foreground_package: str | None
    foreground_activity: str | None
    provenance: Mapping[str, Any]
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for value, label in (
            (self.event_id, "raw observation event id"),
            (self.session_id, "raw observation session id"),
            (self.source_namespace, "raw observation source namespace"),
            (self.source_event_id, "raw observation source event id"),
        ):
            _require_text(value, label)
        _require_utc(self.occurred_at, "raw observation occurred_at")
        _require_utc(self.received_at, "raw observation received_at")
        _require_optional_text(
            self.foreground_package, "raw observation foreground package"
        )
        _require_optional_text(
            self.foreground_activity, "raw observation foreground activity"
        )
        _require_json(self.provenance, "raw observation provenance")
        _require_json(self.payload, "raw observation payload")


@dataclass(frozen=True, slots=True)
class SliceBudgetDraft:
    time_budget_ms: int = 300_000
    action_budget: int = 12
    checkpoint_policy: str = "VERIFIED_CHECKPOINT_ONLY"

    def __post_init__(self) -> None:
        if not 180_000 <= self.time_budget_ms <= 600_000:
            raise ValueError("time_budget_ms must be between 180000 and 600000")
        if not 1 <= self.action_budget <= 50:
            raise ValueError("action_budget must be between 1 and 50")
        _require_text(self.checkpoint_policy, "checkpoint_policy")


@dataclass(frozen=True, slots=True)
class GoalEligibility:
    decision_id: str
    goal_id: str
    eligibility: GoalEligibilityStatus
    reason: str
    goal_status: GoalNodeStatus
    scheduling_class: GoalSchedulingClass = GoalSchedulingClass.NORMAL
    binding_status: str | None = None
    wake_condition_id: str | None = None
    next_eligible_at: str | None = None
    continuation_revision: int | None = None
    latest_directive_target: bool = False
    matched_trigger_event: bool = False
    hard_tier: int = 0
    user_priority_component: int = 0
    event_urgency_component: int = 0
    waiting_age_component: int = 0
    starvation_component: int = 0
    continuity_component: int = 0
    app_switch_cost: int = 0
    backoff_component: int = 0
    recent_failure_component: int = 0
    total_score: int = 0
    rank: int | None = None


@dataclass(frozen=True, slots=True)
class GoalEligibilityDraft:
    goal_id: str
    eligibility: GoalEligibilityStatus
    reason: str
    goal_status: GoalNodeStatus
    scheduling_class: GoalSchedulingClass = GoalSchedulingClass.NORMAL
    binding_status: str | None = None
    wake_condition_id: str | None = None
    next_eligible_at: str | None = None
    continuation_revision: int | None = None
    latest_directive_target: bool = False
    matched_trigger_event: bool = False
    hard_tier: int = 0
    user_priority_component: int = 0
    event_urgency_component: int = 0
    waiting_age_component: int = 0
    starvation_component: int = 0
    continuity_component: int = 0
    app_switch_cost: int = 0
    backoff_component: int = 0
    recent_failure_component: int = 0
    total_score: int = 0
    rank: int | None = None


@dataclass(frozen=True, slots=True)
class AttentionDecision:
    id: str
    session_id: str
    decision_revision: int
    trigger_event_id: str | None
    trigger_key: str
    authority_revision: int
    graph_revision: int
    event_cursor: int
    agenda_revision: int
    outcome: AttentionDecisionOutcome
    candidate_goal_ids: tuple[str, ...]
    selected_goal_id: str | None
    selector_kind: AttentionSelectorKind
    selected_hard_tier: int | None
    reason: str
    user_priority_component: int
    event_urgency_component: int
    waiting_age_component: int
    starvation_component: int
    continuity_component: int
    app_switch_cost: int
    backoff_component: int
    recent_failure_component: int
    base_score: int
    model_adjustment: int
    slice_budget: SliceBudgetDraft
    preemption_policy: PreemptionPolicy
    preemption_checkpoint_ref: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class AttentionDecisionDraft:
    trigger_key: str
    authority_revision: int
    graph_revision: int
    event_cursor: int
    outcome: AttentionDecisionOutcome
    selected_goal_id: str | None
    selector_kind: AttentionSelectorKind
    reason: str
    trigger_event_id: str | None = None
    selected_hard_tier: int | None = None
    user_priority_component: int = 0
    event_urgency_component: int = 0
    waiting_age_component: int = 0
    starvation_component: int = 0
    continuity_component: int = 0
    app_switch_cost: int = 0
    backoff_component: int = 0
    recent_failure_component: int = 0
    base_score: int = 0
    model_adjustment: int = 0
    slice_budget: SliceBudgetDraft = field(default_factory=SliceBudgetDraft)
    preemption_policy: PreemptionPolicy = PreemptionPolicy.VERIFIED_CHECKPOINT_ONLY
    preemption_checkpoint_ref: str | None = None
    agenda_revision: int | None = None


@dataclass(frozen=True, slots=True)
class AttentionDispatch:
    id: str
    session_id: str
    decision_id: str
    goal_id: str
    status: AttentionDispatchStatus
    idempotency_key: str
    attempt_count: int
    created_at: str
    updated_at: str
    delivered_at: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class Continuation:
    id: str
    session_id: str
    goal_id: str
    revision: int
    authority_revision: int
    graph_revision: int
    attention_decision_id: str | None
    checkpoint_kind: ContinuationCheckpointKind
    checkpoint_ref: str | None
    stage_id: str | None
    application_package: str | None
    scene_ref: str | None
    person_id: str | None
    conversation_id: str | None
    verified_fact_refs: tuple[str, ...]
    pending_intent: Mapping[str, Any] | None
    waiting_kind: str | None
    waiting_ref: str | None
    resume_preconditions: Mapping[str, Any]
    next_eligible_at: str | None
    yield_reason: ContinuationYieldReason
    idempotency_key: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ContinuationDraft:
    authority_revision: int
    graph_revision: int
    checkpoint_kind: ContinuationCheckpointKind
    yield_reason: ContinuationYieldReason
    idempotency_key: str
    attention_decision_id: str | None = None
    checkpoint_ref: str | None = None
    stage_id: str | None = None
    application_package: str | None = None
    scene_ref: str | None = None
    person_id: str | None = None
    conversation_id: str | None = None
    verified_fact_refs: tuple[str, ...] = ()
    pending_intent: Mapping[str, Any] | None = None
    waiting_kind: str | None = None
    waiting_ref: str | None = None
    resume_preconditions: Mapping[str, Any] = field(default_factory=dict)
    next_eligible_at: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.idempotency_key, "continuation idempotency_key")


@dataclass(frozen=True, slots=True)
class WakeCondition:
    id: str
    session_id: str
    goal_id: str
    revision: int
    kind: WakeConditionKind
    status: WakeConditionStatus
    matcher: Mapping[str, Any] | None
    due_at: str | None
    created_by_event_id: str | None
    created_by_decision_id: str | None
    satisfied_by_event_id: str | None
    created_at: str
    satisfied_at: str | None
    superseded_at: str | None


@dataclass(frozen=True, slots=True)
class WakeConditionDraft:
    kind: WakeConditionKind
    matcher: Mapping[str, Any] | None = None
    due_at: str | None = None
    created_by_event_id: str | None = None
    created_by_decision_id: str | None = None


@dataclass(frozen=True, slots=True)
class DurableOutboxIntent:
    """One crash-safe request to create the R1 GoalRun compatibility binding.

    ``goal_text`` and ``idempotency_key`` are the entire owner payload.  The
    remaining fields are delivery-envelope facts; no execution result or
    device action is stored in this outbox.
    """

    id: str
    session_id: str
    goal_node_id: str
    intent_type: OutboxIntentType
    status: OutboxIntentStatus
    idempotency_key: str
    goal_text: str
    attempt_count: int
    created_at: str
    updated_at: str
    delivered_at: str | None
    last_error: str | None
    graph_revision: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "outbox intent id")
        _require_text(self.session_id, "session id")
        _require_text(self.goal_node_id, "goal node id")
        _require_text(self.idempotency_key, "outbox idempotency_key")
        _require_text(self.goal_text, "outbox goal text")
        if self.attempt_count < 0:
            raise ValueError("outbox attempt_count must not be negative")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_optional_utc(self.delivered_at, "delivered_at")
        if self.status is OutboxIntentStatus.DELIVERED and self.delivered_at is None:
            raise ValueError("a delivered outbox intent requires delivered_at")


# ``DurableOutboxIntent`` is retained as the R1 name consumed by the existing
# Session service.  R2 calls the exact same durable record an activation
# intent: it is written with the complete graph snapshot and can only be
# consumed after that transaction commits.
GoalActivationIntent = DurableOutboxIntent


@dataclass(frozen=True, slots=True)
class GoalGraphRevision:
    id: str
    session_id: str
    revision: int
    authority_revision: int
    source_directive_id: str
    reason: str | None
    created_at: str

    def __post_init__(self) -> None:
        _require_text(self.id, "goal graph revision id")
        _require_text(self.session_id, "session id")
        _require_text(self.source_directive_id, "source directive id")
        if self.revision < 1 or self.authority_revision < 1:
            raise ValueError("goal graph and authority revisions must be positive")
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class GoalEdge:
    id: str
    session_id: str
    from_goal_id: str
    to_goal_id: str
    edge_kind: GoalEdgeKind
    evidence_ref: str | None
    revision: int
    created_at: str

    def __post_init__(self) -> None:
        for value, label in ((self.id, "goal edge id"), (self.session_id, "session id"),
                             (self.from_goal_id, "from goal id"), (self.to_goal_id, "to goal id")):
            _require_text(value, label)
        if self.from_goal_id == self.to_goal_id:
            raise ValueError("goal edge must join distinct GoalNodes")
        if self.revision < 1:
            raise ValueError("goal edge revision must be positive")
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class GoalCriterion:
    id: str
    goal_id: str
    revision: int
    description: str
    evidence_requirement: str | None
    required: bool
    status: GoalCriterionStatus
    evidence_refs: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        _require_text(self.id, "goal criterion id")
        _require_text(self.goal_id, "goal id")
        _require_text(self.description, "criterion description")
        if self.revision < 1:
            raise ValueError("criterion revision must be positive")
        if not isinstance(self.evidence_refs, tuple) or any(not isinstance(item, str) for item in self.evidence_refs):
            raise ValueError("criterion evidence_refs must be a tuple of strings")
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class GoalCoverage:
    id: str
    session_id: str
    graph_revision: int
    source_directive_id: str
    original_fragment: str
    coverage_kind: GoalCoverageKind
    target_goal_id: str | None
    session_policy: str | None
    created_at: str

    def __post_init__(self) -> None:
        for value, label in ((self.id, "coverage id"), (self.session_id, "session id"),
                             (self.source_directive_id, "source directive id"),
                             (self.original_fragment, "original fragment")):
            _require_text(value, label)
        if self.graph_revision < 1:
            raise ValueError("coverage graph_revision must be positive")
        if self.coverage_kind is GoalCoverageKind.GOAL and not self.target_goal_id:
            raise ValueError("Goal coverage requires target_goal_id")
        if self.coverage_kind is GoalCoverageKind.SESSION_POLICY and not self.session_policy:
            raise ValueError("Session-policy coverage requires session_policy")
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class GoalNodeDraft:
    id: str
    title: str
    source_directive_id: str
    original_fragment: str
    status: GoalNodeStatus = GoalNodeStatus.PLANNED
    goal_family: str | None = None
    application_hint: str | None = None
    account_hint: str | None = None
    explicit_priority: int | None = None
    scheduling_class: GoalSchedulingClass = GoalSchedulingClass.NORMAL
    # Stable planner-facing identity and snapshot operation.  ``id`` remains
    # the durable GoalNode id for a CREATE, while ``existing_goal_id`` must be
    # used to carry a prior node through a later revision.
    goal_ref: str | None = None
    existing_goal_id: str | None = None
    operation: str = "CREATE"
    execution_goal: str | None = None
    initial_wake: WakeConditionDraft | None = None

    def __post_init__(self) -> None:
        for value, label in ((self.id, "goal draft id"), (self.title, "goal draft title"),
                             (self.source_directive_id, "source directive id"),
                             (self.original_fragment, "original fragment")):
            _require_text(value, label)
        if self.operation not in {"CREATE", "KEEP", "UPDATE"}:
            raise ValueError("goal draft operation must be CREATE, KEEP, or UPDATE")
        if self.operation != "CREATE" and not self.existing_goal_id:
            raise ValueError("KEEP/UPDATE goal draft requires existing_goal_id")
        if self.operation != "CREATE" and self.initial_wake is not None:
            raise ValueError("only CREATE goal drafts may declare an initial wake")


@dataclass(frozen=True, slots=True)
class GoalEdgeDraft:
    from_goal_id: str
    to_goal_id: str
    edge_kind: GoalEdgeKind
    evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class GoalCriterionDraft:
    goal_id: str
    description: str
    evidence_requirement: str | None = None
    required: bool = True


@dataclass(frozen=True, slots=True)
class GoalCoverageDraft:
    source_directive_id: str
    original_fragment: str
    coverage_kind: GoalCoverageKind
    target_goal_id: str | None = None
    session_policy: str | None = None


@dataclass(frozen=True, slots=True)
class GoalGraphRevisionDraft:
    authority_revision: int
    source_directive_id: str
    reason: str | None = None
    # Optimistic-concurrency fence captured before the planner runs. ``None``
    # remains available only to direct R1-compatibility store callers.
    base_graph_revision: int | None = None


def utc_now() -> str:
    """Return the canonical persisted timestamp form used by schema v1."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def domain_dict(value: Any) -> dict[str, Any]:
    """Create a JSON-safe public/store projection of a domain dataclass."""

    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError("domain_dict requires a dataclass instance")
    return _json_value(asdict(value))


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be blank")


def _require_optional_text(value: str | None, label: str) -> None:
    if value is not None:
        _require_text(value, label)


def _require_utc(value: str, label: str) -> None:
    _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be in UTC")


def _require_optional_utc(value: str | None, label: str) -> None:
    if value is not None:
        _require_utc(value, label)


def _require_json(value: Mapping[str, Any], label: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON serializable") from error
