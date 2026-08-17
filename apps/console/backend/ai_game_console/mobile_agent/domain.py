from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol


TaskStatus = Literal[
    "queued",
    "planning",
    "running",
    "stopping",
    "completed",
    "failed",
    "stopped",
    "uncertain",
]
SubgoalStatus = Literal["pending", "active", "completed"]
InputLifecycle = Literal["accepted", "applied"]
DecisionKind = Literal["act", "finish", "terminate"]
TransportStatus = Literal["not_sent", "accepted", "rejected", "uncertain"]


class MobileTaskError(RuntimeError):
    code = "mobile_task_error"


class TaskNotFound(MobileTaskError):
    code = "mobile_task_not_found"


class IdempotencyConflict(MobileTaskError):
    code = "mobile_task_request_id_conflict"


class TaskStateConflict(MobileTaskError):
    code = "mobile_task_state_conflict"


class TaskQueueFull(MobileTaskError):
    code = "mobile_task_queue_full"


class TaskRuntimeClosed(MobileTaskError):
    code = "mobile_task_runtime_closed"


@dataclass(frozen=True, slots=True)
class Observation:
    """A persisted reference and bounded summary, never raw screen bytes."""

    evidence_id: str
    summary: str

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("observation evidence_id must not be blank")
        if len(self.summary) > 8_000:
            raise ValueError("observation summary is too long")


@dataclass(frozen=True, slots=True)
class PhysicalIntent:
    """One transportable GUI intent; persistence must precede execution."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("physical intent name must not be blank")
        try:
            json.dumps(dict(self.arguments), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("physical intent arguments must be JSON serializable") from exc


@dataclass(frozen=True, slots=True)
class TransportReceipt:
    """A transport fact, deliberately not an application success claim."""

    status: TransportStatus
    receipt_id: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Verification:
    """Fresh-observation comparison for the current Subgoal."""

    satisfied: bool
    progress: bool
    uncertain: bool = False
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.satisfied and self.uncertain:
            raise ValueError("verification cannot be both satisfied and uncertain")
        if self.satisfied and not self.progress:
            raise ValueError("satisfied verification must also establish progress")
        if len(self.evidence) > 8_000:
            raise ValueError("verification evidence is too long")


@dataclass(frozen=True, slots=True)
class PlanDraft:
    subgoals: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized = tuple(item.strip() for item in self.subgoals)
        if not normalized or any(not item for item in normalized):
            raise ValueError("a TaskPlan requires at least one non-blank Subgoal")
        if len(normalized) > 64:
            raise ValueError("a TaskPlan may contain at most 64 Subgoals")
        object.__setattr__(self, "subgoals", normalized)


@dataclass(frozen=True, slots=True)
class ActionDecision:
    kind: DecisionKind
    intent: PhysicalIntent | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind == "act" and self.intent is None:
            raise ValueError("act decision requires a PhysicalIntent")
        if self.kind != "act" and self.intent is not None:
            raise ValueError("only act decisions may carry a PhysicalIntent")


@dataclass(frozen=True, slots=True)
class ReflectionDecision:
    strategy: str
    terminate: bool = False
    reason: str = ""
    replacement_subgoals: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.replacement_subgoals is not None:
            PlanDraft(self.replacement_subgoals)


@dataclass(frozen=True, slots=True)
class Subgoal:
    index: int
    description: str
    status: SubgoalStatus


@dataclass(frozen=True, slots=True)
class TaskPlan:
    revision: int
    subgoals: tuple[Subgoal, ...]


@dataclass(frozen=True, slots=True)
class InputRevision:
    revision: int
    content: str
    lifecycle: InputLifecycle
    client_request_id: str
    created_at: str
    applied_at: str | None


@dataclass(frozen=True, slots=True)
class TaskEvent:
    sequence: int
    event_type: str
    data: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class Reflection:
    sequence: int
    previous_strategy: str
    strategy: str
    reason: str
    consecutive_no_progress: int
    created_at: str


@dataclass(frozen=True, slots=True)
class ActionAttempt:
    attempt_id: str
    sequence: int
    plan_revision: int
    subgoal_index: int
    input_revision: int
    decision: ActionDecision
    before: Observation
    transport: TransportReceipt | None
    after: Observation | None
    verification: Verification | None
    created_at: str
    finalized_at: str | None


@dataclass(frozen=True, slots=True)
class SkillMemory:
    skill_id: str
    version: int
    source_task_id: str
    procedure: tuple[str, ...]
    strategy: str
    evidence: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class MobileTaskState:
    task_id: str
    goal: str
    target_id: str | None
    skill_id: str | None
    skill_scope_id: str | None
    status: TaskStatus
    input_revision: int
    plan: TaskPlan | None
    active_subgoal_index: int
    strategy: str
    no_progress_count: int
    reflection_count: int
    attempt_count: int
    cancel_requested: bool
    verification_satisfied: bool
    detail: str | None
    error_code: str | None
    skill_memory_version: int
    inputs: tuple[InputRevision, ...]
    attempts: tuple[ActionAttempt, ...]
    reflections: tuple[Reflection, ...]
    events: tuple[TaskEvent, ...]
    created_at: str
    updated_at: str
    finished_at: str | None

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "stopped", "uncertain"}


@dataclass(frozen=True, slots=True)
class PlanContext:
    task_id: str
    goal: str
    target_id: str | None
    input_revision: int
    owner_inputs: tuple[InputRevision, ...]
    observation: Observation
    skill_memory: SkillMemory | None


@dataclass(frozen=True, slots=True)
class DecisionContext:
    task_id: str
    goal: str
    target_id: str | None
    plan_revision: int
    subgoal: Subgoal
    input_revision: int
    owner_inputs: tuple[InputRevision, ...]
    observation: Observation
    strategy: str
    consecutive_no_progress: int
    recent_attempts: tuple[ActionAttempt, ...]
    skill_memory: SkillMemory | None


@dataclass(frozen=True, slots=True)
class VerificationContext:
    task_id: str
    goal: str
    subgoal: Subgoal
    decision: ActionDecision
    before: Observation
    transport: TransportReceipt
    after: Observation
    input_revision: int = 0
    owner_inputs: tuple[InputRevision, ...] = ()


@dataclass(frozen=True, slots=True)
class ReflectionContext:
    task_id: str
    goal: str
    subgoal: Subgoal
    input_revision: int
    owner_inputs: tuple[InputRevision, ...]
    strategy: str
    consecutive_no_progress: int
    recent_attempts: tuple[ActionAttempt, ...]
    skill_memory: SkillMemory | None


class TaskSession(Protocol):
    """One task-scoped device lease held across observe/decide/execute/verify."""

    def observe(self) -> Observation: ...

    def execute(self, intent: PhysicalIntent) -> TransportReceipt: ...

    def close(self) -> None: ...


class TaskDriver(Protocol):
    """Internal seam implemented by a task-scoped local device adapter."""

    def open(self, task_id: str, target_id: str | None) -> TaskSession: ...


class SkillScopeResolver(Protocol):
    """Derive one stable application/task scope for an implicit skill."""

    def __call__(self, goal: str, target_id: str | None) -> str | None: ...


class RoleModel(Protocol):
    """Sequential planning, action, verification, and reflection roles."""

    def plan(self, context: PlanContext) -> PlanDraft: ...

    def decide(self, context: DecisionContext) -> ActionDecision: ...

    def verify(self, context: VerificationContext) -> Verification: ...

    def reflect(self, context: ReflectionContext) -> ReflectionDecision: ...
