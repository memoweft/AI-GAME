"""R7 verified-boundary preemption contracts.

External events are scheduler input, not an interrupt primitive.  This module
therefore creates a preemption request only from a persisted ActivitySlice
boundary with no in-flight Step.  A handoff becomes visible to the scheduler
only after the matching Continuation has been read back from durable storage.

The ActivitySlice ledger is the persistence port for this contract.  The
AgentSession service may consume :class:`PersistedPreemptionHandoff`, but must
not switch the current Goal from a raw notification callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol
from uuid import UUID, uuid5

if TYPE_CHECKING:
    from .activity_slice import (
        ActivitySlice,
        ActivityStep,
        BoundarySnapshot,
        ContinuationDirective,
    )


_PREEMPTION_NAMESPACE = UUID("1a13f61c-cb42-5bd0-9ce4-a0f9ca2f5975")


class VerifiedBoundaryPreemptionError(RuntimeError):
    """A caller attempted to preempt outside a verified Step boundary."""


@dataclass(frozen=True, slots=True)
class VerifiedBoundaryPreemptionRequest:
    """Stable request derived from one new durable Session event cursor."""

    id: str
    idempotency_key: str
    continuation_key: str
    session_id: str
    current_goal_id: str
    activity_slice_id: str
    attention_decision_id: str
    event_cursor: int
    last_settled_step_id: str | None
    resume_step_ordinal: int
    checkpoint_ref: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.id, "preemption id"),
            (self.idempotency_key, "preemption idempotency_key"),
            (self.continuation_key, "preemption continuation_key"),
            (self.session_id, "preemption session_id"),
            (self.current_goal_id, "preemption current_goal_id"),
            (self.activity_slice_id, "preemption activity_slice_id"),
            (self.attention_decision_id, "preemption attention_decision_id"),
            (self.checkpoint_ref, "preemption checkpoint_ref"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must not be blank")
        if self.event_cursor < 1:
            raise ValueError("preemption event_cursor must be positive")
        if self.resume_step_ordinal < 1:
            raise ValueError("preemption resume_step_ordinal must be positive")


@dataclass(frozen=True, slots=True)
class PersistedPreemptionHandoff:
    """Scheduler-safe handoff proving the current Continuation is durable."""

    request: VerifiedBoundaryPreemptionRequest
    continuation_idempotency_key: str
    persisted_at: str

    def __post_init__(self) -> None:
        if self.continuation_idempotency_key != self.request.continuation_key:
            raise ValueError("preemption handoff does not match its Continuation")
        if not isinstance(self.persisted_at, str) or not self.persisted_at.strip():
            raise ValueError("preemption persisted_at must not be blank")


class ActivitySlicePreemptionCoordinatorPort(Protocol):
    """Port for the cross-ledger R7 saga owned by AgentRuntime service/store.

    Both methods must be idempotent on their stable identifiers.  Settling a
    checkpoint authorizes a new AttentionDecision, never a direct Goal/App
    switch.  The caller may invoke it only with the post-persistence handoff
    returned by ``ActivitySliceRunResult``.
    """

    def claim_active_slice(
        self,
        *,
        session_id: str,
        goal_id: str,
        attention_decision_id: str,
        activity_slice_id: str,
        slice_idempotency_key: str,
    ) -> None: ...

    def settle_preemption_checkpoint(
        self, handoff: PersistedPreemptionHandoff
    ) -> None: ...


def event_preemption_requested(
    activity_slice: ActivitySlice,
    boundary: BoundarySnapshot,
) -> bool:
    """Return whether a newer external event asks this Slice to yield."""

    return bool(
        boundary.event_requires_yield
        and boundary.event_cursor > activity_slice.observed_event_cursor
    )


def build_verified_boundary_preemption(
    activity_slice: ActivitySlice,
    boundary: BoundarySnapshot,
    last_step: ActivityStep | None,
) -> VerifiedBoundaryPreemptionRequest | None:
    """Build a stable request only when there is no unsettled atomic action.

    ``ActivitySliceSupervisor`` calls this after ``pending_step`` returned
    ``None``.  The additional checks make that ordering an explicit R7
    contract and reject a corrupted/replayed boundary instead of silently
    switching Goals.
    """

    if not event_preemption_requested(activity_slice, boundary):
        return None
    _require_verified_checkpoint(activity_slice, last_step)
    return _request(
        activity_slice=activity_slice,
        event_cursor=boundary.event_cursor,
        last_settled_step_id=(last_step.id if last_step is not None else None),
        resume_step_ordinal=activity_slice.next_step_ordinal,
    )


def restore_persisted_preemption_handoff(
    activity_slice: ActivitySlice,
    continuation: ContinuationDirective | None,
) -> PersistedPreemptionHandoff:
    """Rebuild the same handoff after process restart from durable facts."""

    if _enum_value(activity_slice.status) != "YIELDED":
        raise VerifiedBoundaryPreemptionError(
            "preemption handoff requires a durably YIELDED ActivitySlice"
        )
    if _enum_value(activity_slice.yield_reason) != "EVENT_AVAILABLE":
        raise VerifiedBoundaryPreemptionError(
            "preemption handoff requires EVENT_AVAILABLE"
        )
    if continuation is None:
        raise VerifiedBoundaryPreemptionError(
            "preemption handoff requires a persisted Continuation"
        )
    if _enum_value(continuation.reason) != "EVENT_AVAILABLE":
        raise VerifiedBoundaryPreemptionError(
            "persisted Continuation does not describe event preemption"
        )
    for actual, expected, label in (
        (continuation.slice_id, activity_slice.id, "slice_id"),
        (continuation.session_id, activity_slice.session_id, "session_id"),
        (continuation.goal_id, activity_slice.goal_id, "goal_id"),
        (
            continuation.last_settled_step_id,
            activity_slice.last_step_id,
            "last_settled_step_id",
        ),
        (
            continuation.resume_step_ordinal,
            activity_slice.next_step_ordinal,
            "resume_step_ordinal",
        ),
        (
            continuation.observed_event_cursor,
            activity_slice.observed_event_cursor,
            "event_cursor",
        ),
    ):
        if actual != expected:
            raise VerifiedBoundaryPreemptionError(
                f"persisted preemption {label} does not match ActivitySlice"
            )
    request = _request(
        activity_slice=activity_slice,
        event_cursor=continuation.observed_event_cursor,
        last_settled_step_id=continuation.last_settled_step_id,
        resume_step_ordinal=continuation.resume_step_ordinal,
    )
    if continuation.checkpoint_ref != request.checkpoint_ref:
        raise VerifiedBoundaryPreemptionError(
            "persisted preemption checkpoint does not match the settled Step"
        )
    if continuation.idempotency_key != request.continuation_key:
        raise VerifiedBoundaryPreemptionError(
            "persisted preemption Continuation key is not restart-stable"
        )
    return PersistedPreemptionHandoff(
        request=request,
        continuation_idempotency_key=continuation.idempotency_key,
        persisted_at=continuation.created_at,
    )


def _require_verified_checkpoint(
    activity_slice: ActivitySlice,
    last_step: ActivityStep | None,
) -> None:
    if activity_slice.last_step_id is None:
        if last_step is not None:
            if _enum_value(last_step.status) != "SETTLED":
                raise VerifiedBoundaryPreemptionError(
                    "event cannot interrupt an unsettled atomic action"
                )
            raise VerifiedBoundaryPreemptionError(
                "empty Slice preemption boundary has an uncommitted settled Step"
            )
        if activity_slice.next_step_ordinal != 1:
            raise VerifiedBoundaryPreemptionError(
                "empty Slice preemption boundary has inconsistent Step state"
            )
        return
    if last_step is None or last_step.id != activity_slice.last_step_id:
        raise VerifiedBoundaryPreemptionError(
            "event preemption requires the current last settled Step"
        )
    if (
        _enum_value(last_step.status) != "SETTLED"
        or _enum_value(last_step.phase) != "SETTLED"
    ):
        raise VerifiedBoundaryPreemptionError(
            "event cannot interrupt an unsettled atomic action"
        )
    if last_step.ordinal + 1 != activity_slice.next_step_ordinal:
        raise VerifiedBoundaryPreemptionError(
            "event preemption resume ordinal does not follow the settled Step"
        )
    if last_step.physical_action_ref is None:
        return
    if (
        last_step.verified is not True
        or not last_step.action_receipt_ref
        or not last_step.after_observation_ref
        or not last_step.verification_ref
    ):
        raise VerifiedBoundaryPreemptionError(
            "event cannot preempt before Receipt, fresh observation, and Verification"
        )


def _request(
    *,
    activity_slice: ActivitySlice,
    event_cursor: int,
    last_settled_step_id: str | None,
    resume_step_ordinal: int,
) -> VerifiedBoundaryPreemptionRequest:
    checkpoint_ref = (
        f"slice:{activity_slice.id}:step:{resume_step_ordinal - 1}:settled"
    )
    key = (
        f"{activity_slice.idempotency_key}:preempt:event:{event_cursor}:"
        f"checkpoint:{resume_step_ordinal - 1}"
    )
    return VerifiedBoundaryPreemptionRequest(
        id=str(uuid5(_PREEMPTION_NAMESPACE, key)),
        idempotency_key=key,
        continuation_key=f"{key}:continuation",
        session_id=activity_slice.session_id,
        current_goal_id=activity_slice.goal_id,
        activity_slice_id=activity_slice.id,
        attention_decision_id=activity_slice.attention_decision_id,
        event_cursor=event_cursor,
        last_settled_step_id=last_settled_step_id,
        resume_step_ordinal=resume_step_ordinal,
        checkpoint_ref=checkpoint_ref,
    )


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return str(raw)


class TaskSafeBoundaryPreemption:
    """C-side bridge from a verified action boundary to Package B controls.

    It is intentionally unable to preempt an in-flight action.  The caller
    must present the already persisted ``PersistedPreemptionHandoff`` created
    by ActivitySlice.  At that point pending revisions are applied through
    Package B's canonical service before the new attention/replan callback is
    requested; neither this adapter nor an event callback chooses a Goal.
    """

    def __init__(
        self,
        task_service: Any,
        *,
        replan: Callable[[str, PersistedPreemptionHandoff], None],
    ) -> None:
        self.task_service = task_service
        self.replan = replan

    def preempt(
        self,
        handoff: PersistedPreemptionHandoff,
        *,
        reason_code: str = "event_preemption",
    ) -> dict[str, Any]:
        task_id = handoff.request.session_id
        task = self.task_service.inspect_task(task_id)
        if task.get("terminal") or task.get("status") in {"paused", "user_takeover"}:
            return task
        self.task_service.apply_revision_boundary(task_id)
        task = self.task_service.transition_task(
            task_id,
            status="replanning",
            reason_code=reason_code,
            summary="External event reached a verified action checkpoint; replanning.",
            recoverable=True,
            idempotency_key=f"preemption:{handoff.request.idempotency_key}:replanning",
        )
        self.replan(task_id, handoff)
        return task

    def reprioritize_at_checkpoint(
        self,
        handoff: PersistedPreemptionHandoff,
        *,
        priority: int,
        expected_revision: int,
        idempotency_key: str,
        requested_by: Mapping[str, Any],
    ) -> dict[str, Any]:
        """A priority change is a control revision, never a mid-action switch."""

        task_id = handoff.request.session_id
        self.task_service.apply_revision_boundary(task_id)
        return self.task_service.control_task(
            task_id,
            action="reprioritize",
            priority=priority,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )


__all__ = [
    "PersistedPreemptionHandoff",
    "ActivitySlicePreemptionCoordinatorPort",
    "VerifiedBoundaryPreemptionError",
    "VerifiedBoundaryPreemptionRequest",
    "build_verified_boundary_preemption",
    "event_preemption_requested",
    "restore_persisted_preemption_handoff",
    "TaskSafeBoundaryPreemption",
]
