"""R6 ActivitySlice contracts and the sole multi-step supervisor.

This module deliberately owns neither Goal state nor physical execution.  It
coordinates existing authorities through two injected ports:

* ``BoundarySnapshotPort`` captures the current directive/event/control facts
  plus a fresh before-observation.
* ``ActivityStepExecutorPort`` executes or reconciles exactly one step through
  RuntimeKernel and returns references to its durable ledger facts.

The supervisor persists every boundary before asking the executor to act.  A
restart therefore retries the same step idempotency key instead of inventing a
second action.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid5

if TYPE_CHECKING:
    from .preemption import (
        PersistedPreemptionHandoff,
        VerifiedBoundaryPreemptionRequest,
    )


_ACTIVITY_NAMESPACE = UUID("c154b759-d38e-55f0-87aa-4f97446a36a3")


class ActivitySliceError(RuntimeError):
    """Base error for an invalid or conflicting ActivitySlice operation."""


class ActivitySliceConflict(ActivitySliceError):
    """A durable idempotency key was reused for different facts."""


class ActivitySliceStatus(str, Enum):
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    YIELDED = "YIELDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {
            ActivitySliceStatus.WAITING,
            ActivitySliceStatus.YIELDED,
            ActivitySliceStatus.COMPLETED,
            ActivitySliceStatus.FAILED,
        }


class ActivityStepStatus(str, Enum):
    PREPARED = "PREPARED"
    EXECUTING = "EXECUTING"
    SETTLED = "SETTLED"


class ActivityStepPhase(str, Enum):
    OBSERVATION_CAPTURED = "OBSERVATION_CAPTURED"
    DECISION_COMMITTED = "DECISION_COMMITTED"
    ACTION_PERSISTED = "ACTION_PERSISTED"
    DISPATCHING = "DISPATCHING"
    RECEIPT_RECORDED = "RECEIPT_RECORDED"
    AFTER_OBSERVATION_CAPTURED = "AFTER_OBSERVATION_CAPTURED"
    VERIFICATION_RECORDED = "VERIFICATION_RECORDED"
    SETTLED = "SETTLED"


_PHASE_ORDER = {phase: index for index, phase in enumerate(ActivityStepPhase)}


class StepOutcome(str, Enum):
    ACT = "ACT"
    WAIT_EVENT = "WAIT_EVENT"
    WAIT_TIME = "WAIT_TIME"
    NEED_USER_FACT = "NEED_USER_FACT"
    GOAL_PROGRESS = "GOAL_PROGRESS"
    GOAL_COMPLETE = "GOAL_COMPLETE"
    YIELD_TO_SCHEDULER = "YIELD_TO_SCHEDULER"


# The longer name is useful at composition boundaries while ``StepOutcome``
# keeps model adapters concise.
ActivityStepOutcome = StepOutcome


class WakePlanKind(str, Enum):
    EVENT = "EVENT"
    TIME = "TIME"
    USER_FACT = "USER_FACT"


class WakePlanStatus(str, Enum):
    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    CANCELLED = "CANCELLED"


class SliceYieldReason(str, Enum):
    WAIT_EVENT = "WAIT_EVENT"
    WAIT_TIME = "WAIT_TIME"
    NEED_USER_FACT = "NEED_USER_FACT"
    GOAL_COMPLETE_PROPOSAL = "GOAL_COMPLETE_PROPOSAL"
    YIELD_TO_SCHEDULER = "YIELD_TO_SCHEDULER"
    DIRECTIVE_CHANGED = "DIRECTIVE_CHANGED"
    EVENT_AVAILABLE = "EVENT_AVAILABLE"
    USER_ACTIVE = "USER_ACTIVE"
    CONTROL_MODE_CHANGED = "CONTROL_MODE_CHANGED"
    STOP_REQUESTED = "STOP_REQUESTED"
    CHECKPOINT_REQUESTED = "CHECKPOINT_REQUESTED"
    TIME_BUDGET_EXHAUSTED = "TIME_BUDGET_EXHAUSTED"
    ACTION_BUDGET_EXHAUSTED = "ACTION_BUDGET_EXHAUSTED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    PROCESS_RESTART = "PROCESS_RESTART"


@dataclass(frozen=True, slots=True)
class SliceBudget:
    time_budget_seconds: float
    action_limit: int

    def __post_init__(self) -> None:
        if self.time_budget_seconds <= 0:
            raise ValueError("time_budget_seconds must be positive")
        if self.action_limit < 0:
            raise ValueError("action_limit must not be negative")


@dataclass(frozen=True, slots=True)
class BoundarySnapshot:
    """Facts that must be checked between every pair of ActivitySteps."""

    captured_at: str
    fresh_observation_ref: str
    directive_revision: int
    event_cursor: int
    human_active: bool = False
    control_mode: str = "AGENT_ACTIVE"
    stop_requested: bool = False
    checkpoint_requested: bool = False
    directive_requires_yield: bool = False
    event_requires_yield: bool = False
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_utc(self.captured_at, "captured_at")
        _require_text(self.fresh_observation_ref, "fresh_observation_ref")
        if self.directive_revision < 1:
            raise ValueError("directive_revision must be positive")
        if self.event_cursor < 0:
            raise ValueError("event_cursor must not be negative")
        _require_text(self.control_mode, "control_mode")


@dataclass(frozen=True, slots=True)
class ActivitySlice:
    id: str
    idempotency_key: str
    session_id: str
    goal_id: str
    attention_decision_id: str
    attention_decision_revision: int
    objective: str
    status: ActivitySliceStatus
    budget: SliceBudget
    started_at: str
    deadline_at: str
    action_count: int
    next_step_ordinal: int
    last_step_id: str | None
    observed_directive_revision: int
    observed_event_cursor: int
    yield_reason: SliceYieldReason | None
    continuation_key: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.id, "slice id"),
            (self.idempotency_key, "slice idempotency_key"),
            (self.session_id, "session id"),
            (self.goal_id, "goal id"),
            (self.attention_decision_id, "attention decision id"),
            (self.objective, "objective"),
        ):
            _require_text(value, label)
        if self.attention_decision_revision < 1:
            raise ValueError("attention_decision_revision must be positive")
        if self.action_count < 0:
            raise ValueError("action_count must not be negative")
        if self.next_step_ordinal < 1:
            raise ValueError("next_step_ordinal must be positive")
        if self.observed_directive_revision < 1:
            raise ValueError("observed_directive_revision must be positive")
        if self.observed_event_cursor < 0:
            raise ValueError("observed_event_cursor must not be negative")
        for value, label in (
            (self.started_at, "started_at"),
            (self.deadline_at, "deadline_at"),
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
        ):
            _require_utc(value, label)

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @property
    def remaining_actions(self) -> int:
        return max(0, self.budget.action_limit - self.action_count)


@dataclass(frozen=True, slots=True)
class ActivityStep:
    id: str
    idempotency_key: str
    slice_id: str
    ordinal: int
    status: ActivityStepStatus
    phase: ActivityStepPhase
    boundary: BoundarySnapshot
    outcome: StepOutcome | None
    physical_action_ref: str | None
    action_receipt_ref: str | None
    after_observation_ref: str | None
    verification_ref: str | None
    verified: bool | None
    decision: Mapping[str, Any] | None
    result_facts: Mapping[str, Any]
    created_at: str
    updated_at: str
    settled_at: str | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.id, "step id"),
            (self.idempotency_key, "step idempotency_key"),
            (self.slice_id, "slice id"),
        ):
            _require_text(value, label)
        if self.ordinal < 1:
            raise ValueError("step ordinal must be positive")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.settled_at is not None:
            _require_utc(self.settled_at, "settled_at")

    @property
    def before_observation_ref(self) -> str:
        return self.boundary.fresh_observation_ref


@dataclass(frozen=True, slots=True)
class WakePlan:
    id: str
    idempotency_key: str
    slice_id: str
    step_id: str
    kind: WakePlanKind
    status: WakePlanStatus
    matcher: Mapping[str, Any] | None
    due_at: str | None
    user_fact_key: str | None
    created_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.id, "wake plan id"),
            (self.idempotency_key, "wake plan idempotency_key"),
            (self.slice_id, "slice id"),
            (self.step_id, "step id"),
        ):
            _require_text(value, label)
        _require_utc(self.created_at, "created_at")
        if self.kind is WakePlanKind.EVENT and not self.matcher:
            raise ValueError("EVENT WakePlan requires matcher")
        if self.kind is WakePlanKind.TIME:
            if self.due_at is None:
                raise ValueError("TIME WakePlan requires due_at")
            _require_utc(self.due_at, "due_at")
        if self.kind is WakePlanKind.USER_FACT:
            _require_text(self.user_fact_key or "", "user_fact_key")


@dataclass(frozen=True, slots=True)
class ContinuationDirective:
    idempotency_key: str
    slice_id: str
    session_id: str
    goal_id: str
    reason: SliceYieldReason
    last_settled_step_id: str | None
    resume_step_ordinal: int
    observed_directive_revision: int
    observed_event_cursor: int
    remaining_time_seconds: float
    remaining_actions: int
    checkpoint_ref: str
    wake_plan_id: str | None
    created_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.idempotency_key, "continuation idempotency_key"),
            (self.slice_id, "slice id"),
            (self.session_id, "session id"),
            (self.goal_id, "goal id"),
            (self.checkpoint_ref, "checkpoint_ref"),
        ):
            _require_text(value, label)
        if self.resume_step_ordinal < 1:
            raise ValueError("resume_step_ordinal must be positive")
        if self.remaining_time_seconds < 0:
            raise ValueError("remaining_time_seconds must not be negative")
        if self.remaining_actions < 0:
            raise ValueError("remaining_actions must not be negative")
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    outcome: StepOutcome
    decision: Mapping[str, Any] = field(default_factory=dict)
    physical_action_ref: str | None = None
    action_receipt_ref: str | None = None
    after_observation_ref: str | None = None
    verification_ref: str | None = None
    verified: bool | None = None
    wake_matcher: Mapping[str, Any] | None = None
    due_at: str | None = None
    user_fact_key: str | None = None
    progress_fact: Mapping[str, Any] | None = None
    completion_proposal: Mapping[str, Any] | None = None
    completion_verified: bool = False
    facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.physical_action_ref is not None:
            _require_text(self.physical_action_ref, "physical_action_ref")
            _require_text(self.action_receipt_ref or "", "action_receipt_ref")
            _require_text(self.after_observation_ref or "", "after_observation_ref")
            _require_text(self.verification_ref or "", "verification_ref")
            if self.verified is None:
                raise ValueError("a physical action requires a verification verdict")
        if self.outcome is StepOutcome.ACT and self.physical_action_ref is None:
            raise ValueError("ACT requires one physical_action_ref")
        if self.outcome is StepOutcome.WAIT_EVENT and not self.wake_matcher:
            raise ValueError("WAIT_EVENT requires wake_matcher")
        if self.outcome is StepOutcome.WAIT_TIME:
            if self.due_at is None:
                raise ValueError("WAIT_TIME requires due_at")
            _require_utc(self.due_at, "due_at")
        if self.outcome is StepOutcome.NEED_USER_FACT:
            _require_text(self.user_fact_key or "", "user_fact_key")
        if self.outcome is StepOutcome.GOAL_PROGRESS and self.progress_fact is None:
            raise ValueError("GOAL_PROGRESS requires progress_fact")
        if self.outcome is StepOutcome.GOAL_COMPLETE and self.completion_proposal is None:
            raise ValueError("GOAL_COMPLETE requires completion_proposal")
        if self.completion_verified:
            if self.outcome is not StepOutcome.GOAL_COMPLETE:
                raise ValueError("only GOAL_COMPLETE can be runtime verified")
            if not self.verified or not self.after_observation_ref or not self.verification_ref:
                raise ValueError(
                    "verified completion requires fresh after-observation and verification"
                )


@dataclass(frozen=True, slots=True)
class StepProgress:
    phase: ActivityStepPhase
    physical_action_ref: str | None = None
    action_receipt_ref: str | None = None
    after_observation_ref: str | None = None
    verification_ref: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActivityStepContext:
    slice: ActivitySlice
    step: ActivityStep
    boundary: BoundarySnapshot
    remaining_time_seconds: float
    remaining_actions: int
    resume_phase: ActivityStepPhase
    resume_facts: Mapping[str, Any]
    _progress_sink: Callable[[StepProgress], None] = field(repr=False, compare=False)

    def record_progress(self, progress: StepProgress) -> None:
        """Persist executor progress before the corresponding side effect continues."""

        self._progress_sink(progress)


@dataclass(frozen=True, slots=True)
class SliceBoundaryRequest:
    slice: ActivitySlice
    after_step_id: str | None


@dataclass(frozen=True, slots=True)
class ActivitySliceRunResult:
    slice: ActivitySlice
    steps: tuple[ActivityStep, ...]
    wake_plan: WakePlan | None
    continuation: ContinuationDirective | None
    preemption_handoff: PersistedPreemptionHandoff | None = None


class BoundarySnapshotPort(Protocol):
    def capture_boundary(self, request: SliceBoundaryRequest) -> BoundarySnapshot: ...


class ActivityStepExecutorPort(Protocol):
    def execute_activity_step(self, context: ActivityStepContext) -> StepExecutionResult: ...


class BoundaryPolicyPort(Protocol):
    def evaluate(
        self,
        activity_slice: ActivitySlice,
        boundary: BoundarySnapshot,
        *,
        now: datetime,
    ) -> SliceYieldReason | None: ...


class ActivitySliceStorePort(Protocol):
    def create_slice(
        self,
        *,
        session_id: str,
        goal_id: str,
        attention_decision_id: str,
        attention_decision_revision: int,
        objective: str,
        budget: SliceBudget,
        directive_revision: int,
        event_cursor: int,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[ActivitySlice, bool]: ...

    def get_slice(self, slice_id: str) -> ActivitySlice: ...

    def list_steps(self, slice_id: str) -> tuple[ActivityStep, ...]: ...

    def pending_step(self, slice_id: str) -> ActivityStep | None: ...

    def begin_step(
        self, slice_id: str, boundary: BoundarySnapshot, *, now: datetime
    ) -> ActivityStep: ...

    def record_step_progress(
        self, step_id: str, progress: StepProgress, *, now: datetime
    ) -> ActivityStep: ...

    def settle_step(
        self, step_id: str, result: StepExecutionResult, *, now: datetime
    ) -> ActivityStep: ...

    def create_wake_plan(
        self,
        step_id: str,
        *,
        kind: WakePlanKind,
        matcher: Mapping[str, Any] | None,
        due_at: str | None,
        user_fact_key: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[WakePlan, bool]: ...

    def get_wake_plan_for_slice(self, slice_id: str) -> WakePlan | None: ...

    def yield_slice(
        self,
        slice_id: str,
        *,
        status: ActivitySliceStatus,
        reason: SliceYieldReason,
        continuation: ContinuationDirective,
        boundary: BoundarySnapshot | None,
        now: datetime,
    ) -> ActivitySlice: ...

    def complete_slice(self, slice_id: str, *, now: datetime) -> ActivitySlice: ...

    def continuation(self, slice_id: str) -> ContinuationDirective | None: ...


CrashHook = Callable[[str, Mapping[str, Any]], None]


class ActivitySliceSupervisor:
    """The only R6 component allowed to iterate across ActivitySteps."""

    def __init__(
        self,
        store: ActivitySliceStorePort,
        boundary_port: BoundarySnapshotPort,
        step_executor: ActivityStepExecutorPort,
        *,
        policy: BoundaryPolicyPort | None = None,
        clock: Callable[[], datetime] | None = None,
        crash_hook: CrashHook | None = None,
    ) -> None:
        if policy is None:
            from .activity_policy import ActivityBoundaryPolicy

            policy = ActivityBoundaryPolicy()
        self.store = store
        self.boundary_port = boundary_port
        self.step_executor = step_executor
        self.policy = policy
        self.clock = clock or (lambda: datetime.now(UTC))
        self.crash_hook = crash_hook

    def start_slice(
        self,
        *,
        session_id: str,
        goal_id: str,
        attention_decision_id: str,
        attention_decision_revision: int,
        objective: str,
        budget: SliceBudget,
        directive_revision: int,
        event_cursor: int,
        idempotency_key: str | None = None,
    ) -> tuple[ActivitySlice, bool]:
        key = idempotency_key or deterministic_slice_key(
            session_id,
            goal_id,
            attention_decision_id,
            attention_decision_revision,
        )
        return self.store.create_slice(
            session_id=session_id,
            goal_id=goal_id,
            attention_decision_id=attention_decision_id,
            attention_decision_revision=attention_decision_revision,
            objective=objective,
            budget=budget,
            directive_revision=directive_revision,
            event_cursor=event_cursor,
            idempotency_key=key,
            now=self._now(),
        )

    def run_slice(self, slice_id: str) -> ActivitySliceRunResult:
        """Run immediate steps until a typed boundary, wait, or completion."""

        while True:
            activity_slice = self.store.get_slice(slice_id)
            if activity_slice.terminal:
                return self._result(activity_slice)

            step = self.store.pending_step(slice_id)
            if step is None:
                self._crash("before_observation", {"slice_id": slice_id})
                boundary = self.boundary_port.capture_boundary(
                    SliceBoundaryRequest(
                        slice=activity_slice,
                        after_step_id=activity_slice.last_step_id,
                    )
                )
                reason = self.policy.evaluate(
                    activity_slice, boundary, now=self._now()
                )
                if reason is not None:
                    preemption = None
                    if reason is SliceYieldReason.EVENT_AVAILABLE:
                        from .preemption import build_verified_boundary_preemption

                        steps = self.store.list_steps(slice_id)
                        last_step = steps[-1] if steps else None
                        preemption = build_verified_boundary_preemption(
                            activity_slice, boundary, last_step
                        )
                        if preemption is None:
                            raise ActivitySliceConflict(
                                "EVENT_AVAILABLE lacks a verified preemption request"
                            )
                    activity_slice = self._yield(
                        activity_slice,
                        reason,
                        boundary=boundary,
                        preemption=preemption,
                    )
                    return self._result(activity_slice)
                step = self.store.begin_step(
                    slice_id, boundary, now=self._now()
                )
                self._crash(
                    "after_observation_before_decision",
                    {"slice_id": slice_id, "step_id": step.id},
                )

            activity_slice = self.store.get_slice(slice_id)
            context = ActivityStepContext(
                slice=activity_slice,
                step=step,
                boundary=step.boundary,
                remaining_time_seconds=_remaining_time(
                    activity_slice.deadline_at, self._now()
                ),
                remaining_actions=activity_slice.remaining_actions,
                resume_phase=step.phase,
                resume_facts=step.result_facts,
                _progress_sink=lambda progress, step_id=step.id: self._record_progress(
                    step_id, progress
                ),
            )
            result = self.step_executor.execute_activity_step(context)
            self._validate_freshness(step, result)
            settled = self.store.settle_step(step.id, result, now=self._now())
            self._crash(
                "after_step_settle_before_next_step",
                {"slice_id": slice_id, "step_id": settled.id},
            )
            activity_slice = self.store.get_slice(slice_id)

            terminal = self._project_step_outcome(activity_slice, settled, result)
            if terminal is not None:
                return self._result(terminal)

    def recover(self, slice_id: str) -> ActivitySliceRunResult:
        """Converge an interrupted Slice using the pending Step's stable key."""

        return self.run_slice(slice_id)

    def _record_progress(self, step_id: str, progress: StepProgress) -> None:
        persisted = self.store.record_step_progress(
            step_id, progress, now=self._now()
        )
        crash_point = {
            ActivityStepPhase.DECISION_COMMITTED: "after_decision_before_action_persist",
            ActivityStepPhase.ACTION_PERSISTED: "after_action_persist_before_dispatch",
            ActivityStepPhase.DISPATCHING: "during_dispatch",
            ActivityStepPhase.RECEIPT_RECORDED: "after_receipt_before_after_observation",
            ActivityStepPhase.AFTER_OBSERVATION_CAPTURED: "after_after_observation_before_verify",
            ActivityStepPhase.VERIFICATION_RECORDED: "after_verify_before_step_settle",
        }.get(progress.phase)
        if crash_point is not None:
            self._crash(
                crash_point,
                {
                    "slice_id": persisted.slice_id,
                    "step_id": persisted.id,
                    "physical_action_ref": persisted.physical_action_ref,
                },
            )

    def _project_step_outcome(
        self,
        activity_slice: ActivitySlice,
        step: ActivityStep,
        result: StepExecutionResult,
    ) -> ActivitySlice | None:
        if result.outcome is StepOutcome.ACT:
            if result.verified:
                return None
            return self._yield(
                activity_slice,
                SliceYieldReason.VERIFICATION_FAILED,
                boundary=step.boundary,
            )
        if result.outcome is StepOutcome.GOAL_PROGRESS:
            return None
        if result.outcome is StepOutcome.WAIT_EVENT:
            wake, _ = self.store.create_wake_plan(
                step.id,
                kind=WakePlanKind.EVENT,
                matcher=result.wake_matcher,
                due_at=None,
                user_fact_key=None,
                idempotency_key=f"{step.idempotency_key}:wake:event",
                now=self._now(),
            )
            return self._yield(
                activity_slice,
                SliceYieldReason.WAIT_EVENT,
                boundary=step.boundary,
                wake_plan=wake,
                waiting=True,
            )
        if result.outcome is StepOutcome.WAIT_TIME:
            wake, _ = self.store.create_wake_plan(
                step.id,
                kind=WakePlanKind.TIME,
                matcher=None,
                due_at=result.due_at,
                user_fact_key=None,
                idempotency_key=f"{step.idempotency_key}:wake:time",
                now=self._now(),
            )
            return self._yield(
                activity_slice,
                SliceYieldReason.WAIT_TIME,
                boundary=step.boundary,
                wake_plan=wake,
                waiting=True,
            )
        if result.outcome is StepOutcome.NEED_USER_FACT:
            wake, _ = self.store.create_wake_plan(
                step.id,
                kind=WakePlanKind.USER_FACT,
                matcher=None,
                due_at=None,
                user_fact_key=result.user_fact_key,
                idempotency_key=f"{step.idempotency_key}:wake:user-fact",
                now=self._now(),
            )
            return self._yield(
                activity_slice,
                SliceYieldReason.NEED_USER_FACT,
                boundary=step.boundary,
                wake_plan=wake,
                waiting=True,
            )
        if result.outcome is StepOutcome.GOAL_COMPLETE:
            if result.completion_verified:
                # This completes only the Slice.  The AgentSession service still
                # owns any GoalNode completion transaction.
                return self.store.complete_slice(activity_slice.id, now=self._now())
            return self._yield(
                activity_slice,
                SliceYieldReason.GOAL_COMPLETE_PROPOSAL,
                boundary=step.boundary,
            )
        if result.outcome is StepOutcome.YIELD_TO_SCHEDULER:
            return self._yield(
                activity_slice,
                SliceYieldReason.YIELD_TO_SCHEDULER,
                boundary=step.boundary,
            )
        raise AssertionError(f"unhandled ActivityStep outcome {result.outcome}")

    def _yield(
        self,
        activity_slice: ActivitySlice,
        reason: SliceYieldReason,
        *,
        boundary: BoundarySnapshot | None,
        wake_plan: WakePlan | None = None,
        waiting: bool = False,
        preemption: VerifiedBoundaryPreemptionRequest | None = None,
    ) -> ActivitySlice:
        now = self._now()
        ordinal = activity_slice.next_step_ordinal
        if preemption is not None and reason is not SliceYieldReason.EVENT_AVAILABLE:
            raise ActivitySliceConflict(
                "verified preemption request requires EVENT_AVAILABLE"
            )
        key = (
            preemption.continuation_key
            if preemption is not None
            else f"{activity_slice.idempotency_key}:yield:{reason.value}:{ordinal}"
        )
        checkpoint_ref = (
            preemption.checkpoint_ref
            if preemption is not None
            else f"slice:{activity_slice.id}:step:{ordinal - 1}:settled"
        )
        continuation = ContinuationDirective(
            idempotency_key=key,
            slice_id=activity_slice.id,
            session_id=activity_slice.session_id,
            goal_id=activity_slice.goal_id,
            reason=reason,
            last_settled_step_id=activity_slice.last_step_id,
            resume_step_ordinal=ordinal,
            observed_directive_revision=(
                boundary.directive_revision
                if boundary is not None
                else activity_slice.observed_directive_revision
            ),
            observed_event_cursor=(
                boundary.event_cursor
                if boundary is not None
                else activity_slice.observed_event_cursor
            ),
            remaining_time_seconds=_remaining_time(activity_slice.deadline_at, now),
            remaining_actions=activity_slice.remaining_actions,
            checkpoint_ref=checkpoint_ref,
            wake_plan_id=wake_plan.id if wake_plan is not None else None,
            created_at=_iso(now),
        )
        yielded = self.store.yield_slice(
            activity_slice.id,
            status=(
                ActivitySliceStatus.WAITING if waiting else ActivitySliceStatus.YIELDED
            ),
            reason=reason,
            continuation=continuation,
            boundary=boundary,
            now=now,
        )
        self._crash(
            "after_continuation_before_yield",
            {"slice_id": activity_slice.id, "continuation_key": key},
        )
        return yielded

    def _validate_freshness(
        self, step: ActivityStep, result: StepExecutionResult
    ) -> None:
        if result.physical_action_ref is None:
            return
        if result.after_observation_ref == step.before_observation_ref:
            raise ActivitySliceConflict(
                "physical action after-observation must be fresh"
            )

    def _result(self, activity_slice: ActivitySlice) -> ActivitySliceRunResult:
        continuation = self.store.continuation(activity_slice.id)
        preemption_handoff = None
        if activity_slice.yield_reason is SliceYieldReason.EVENT_AVAILABLE:
            from .preemption import restore_persisted_preemption_handoff

            # This object is deliberately constructed only after the
            # Continuation has been read back from the durable Slice ledger.
            preemption_handoff = restore_persisted_preemption_handoff(
                activity_slice, continuation
            )
        return ActivitySliceRunResult(
            slice=activity_slice,
            steps=self.store.list_steps(activity_slice.id),
            wake_plan=self.store.get_wake_plan_for_slice(activity_slice.id),
            continuation=continuation,
            preemption_handoff=preemption_handoff,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ActivitySlice clock must return timezone-aware datetime")
        return value.astimezone(UTC)

    def _crash(self, point: str, facts: Mapping[str, Any]) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point, facts)


def deterministic_slice_key(
    session_id: str,
    goal_id: str,
    attention_decision_id: str,
    attention_decision_revision: int,
) -> str:
    """Return the stable logical key for one scheduler-selected Slice."""

    for value, label in (
        (session_id, "session_id"),
        (goal_id, "goal_id"),
        (attention_decision_id, "attention_decision_id"),
    ):
        _require_text(value, label)
    if attention_decision_revision < 1:
        raise ValueError("attention_decision_revision must be positive")
    return (
        f"slice:{session_id}:goal:{goal_id}:decision:{attention_decision_id}:"
        f"revision:{attention_decision_revision}"
    )


def deterministic_id(kind: str, idempotency_key: str) -> str:
    _require_text(kind, "id kind")
    _require_text(idempotency_key, "idempotency_key")
    return str(uuid5(_ACTIVITY_NAMESPACE, f"{kind}:{idempotency_key}"))


def phase_precedes(left: ActivityStepPhase, right: ActivityStepPhase) -> bool:
    return _PHASE_ORDER[left] < _PHASE_ORDER[right]


def _remaining_time(deadline_at: str, now: datetime) -> float:
    deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    return max(0.0, (deadline - now.astimezone(UTC)).total_seconds())


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _deadline(started_at: datetime, seconds: float) -> str:
    return _iso(started_at + timedelta(seconds=seconds))


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be blank")


def _require_utc(value: str, label: str) -> None:
    _require_text(value, label)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include timezone")


__all__ = [
    "ActivitySlice",
    "ActivitySliceConflict",
    "ActivitySliceError",
    "ActivitySliceRunResult",
    "ActivitySliceStatus",
    "ActivitySliceSupervisor",
    "ActivitySliceStorePort",
    "ActivityStep",
    "ActivityStepContext",
    "ActivityStepExecutorPort",
    "ActivityStepOutcome",
    "ActivityStepPhase",
    "ActivityStepStatus",
    "BoundaryPolicyPort",
    "BoundarySnapshot",
    "BoundarySnapshotPort",
    "ContinuationDirective",
    "SliceBoundaryRequest",
    "SliceBudget",
    "SliceYieldReason",
    "StepExecutionResult",
    "StepOutcome",
    "StepProgress",
    "WakePlan",
    "WakePlanKind",
    "WakePlanStatus",
    "deterministic_id",
    "deterministic_slice_key",
    "phase_precedes",
]
