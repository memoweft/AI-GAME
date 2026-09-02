"""Boundary policy for the R6 ActivitySlice supervisor."""

from __future__ import annotations

from datetime import UTC, datetime

from .activity_slice import ActivitySlice, BoundarySnapshot, SliceYieldReason
from .preemption import event_preemption_requested


class ActivityBoundaryPolicy:
    """Choose typed yield reasons without taking over scheduler authority."""

    def evaluate(
        self,
        activity_slice: ActivitySlice,
        boundary: BoundarySnapshot,
        *,
        now: datetime,
    ) -> SliceYieldReason | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("boundary policy requires timezone-aware now")
        now = now.astimezone(UTC)
        deadline = datetime.fromisoformat(
            activity_slice.deadline_at.replace("Z", "+00:00")
        ).astimezone(UTC)

        # Stop/user/control facts are stronger than ordinary scheduling hints.
        if boundary.stop_requested:
            return SliceYieldReason.STOP_REQUESTED
        if boundary.human_active:
            return SliceYieldReason.USER_ACTIVE
        if boundary.control_mode != "AGENT_ACTIVE":
            return SliceYieldReason.CONTROL_MODE_CHANGED
        if boundary.checkpoint_requested:
            return SliceYieldReason.CHECKPOINT_REQUESTED
        if boundary.directive_requires_yield and (
            boundary.directive_revision > activity_slice.observed_directive_revision
        ):
            return SliceYieldReason.DIRECTIVE_CHANGED
        if event_preemption_requested(activity_slice, boundary):
            return SliceYieldReason.EVENT_AVAILABLE
        if now >= deadline:
            return SliceYieldReason.TIME_BUDGET_EXHAUSTED
        if activity_slice.action_count >= activity_slice.budget.action_limit:
            return SliceYieldReason.ACTION_BUDGET_EXHAUSTED
        return None


__all__ = ["ActivityBoundaryPolicy"]
