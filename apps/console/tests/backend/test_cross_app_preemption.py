from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_game_console.agent_runtime.activity_slice import (
    ActivitySliceStatus,
    ActivitySliceSupervisor,
    ActivityStepPhase,
    ActivityStepStatus,
    BoundarySnapshot,
    SliceBudget,
    SliceYieldReason,
    StepExecutionResult,
    StepOutcome,
    StepProgress,
)
from ai_game_console.agent_runtime.activity_store import ActivitySliceStore
from ai_game_console.agent_runtime.preemption import (
    PersistedPreemptionHandoff,
    VerifiedBoundaryPreemptionError,
    build_verified_boundary_preemption,
)


class NotificationBoundaryPort:
    def __init__(self) -> None:
        self.notification_waiting = False
        self.captures = 0

    def capture_boundary(self, request):
        self.captures += 1
        event_cursor = 41 if self.notification_waiting else 40
        return BoundarySnapshot(
            captured_at="2026-08-26T02:00:00Z",
            fresh_observation_ref=f"notification-boundary-{self.captures}",
            directive_revision=1,
            event_cursor=event_cursor,
            event_requires_yield=self.notification_waiting,
            facts={
                "event_id": "wechat-notification-41",
                "event_type": "notification.posted",
            },
        )


class AtomicActionExecutor:
    def __init__(self, boundaries: NotificationBoundaryPort) -> None:
        self.boundaries = boundaries
        self.calls = 0
        self.timeline: list[str] = []

    def execute_activity_step(self, context):
        self.calls += 1
        self.timeline.append("decision")
        context.record_progress(StepProgress(ActivityStepPhase.DECISION_COMMITTED))
        self.timeline.append("action-persisted")
        context.record_progress(
            StepProgress(
                ActivityStepPhase.ACTION_PERSISTED,
                physical_action_ref="kernel-action-1",
            )
        )
        self.timeline.append("dispatch")
        context.record_progress(
            StepProgress(
                ActivityStepPhase.DISPATCHING,
                physical_action_ref="kernel-action-1",
            )
        )
        # The notification arrives while RuntimeKernel owns the atomic action.
        # It becomes visible only on the next ActivitySlice boundary capture.
        self.boundaries.notification_waiting = True
        self.timeline.append("receipt")
        context.record_progress(
            StepProgress(
                ActivityStepPhase.RECEIPT_RECORDED,
                physical_action_ref="kernel-action-1",
                action_receipt_ref="receipt-1",
            )
        )
        self.timeline.append("after-observation")
        context.record_progress(
            StepProgress(
                ActivityStepPhase.AFTER_OBSERVATION_CAPTURED,
                physical_action_ref="kernel-action-1",
                action_receipt_ref="receipt-1",
                after_observation_ref="after-1",
            )
        )
        self.timeline.append("verification")
        context.record_progress(
            StepProgress(
                ActivityStepPhase.VERIFICATION_RECORDED,
                physical_action_ref="kernel-action-1",
                action_receipt_ref="receipt-1",
                after_observation_ref="after-1",
                verification_ref="verification-1",
            )
        )
        return StepExecutionResult(
            StepOutcome.ACT,
            physical_action_ref="kernel-action-1",
            action_receipt_ref="receipt-1",
            after_observation_ref="after-1",
            verification_ref="verification-1",
            verified=True,
        )


class InjectedCrash(RuntimeError):
    pass


def make_supervisor(
    database: Path,
    boundaries: NotificationBoundaryPort,
    executor: AtomicActionExecutor,
    *,
    crash_hook=None,
):
    store = ActivitySliceStore(database)
    supervisor = ActivitySliceSupervisor(
        store,
        boundaries,
        executor,
        clock=lambda: datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        crash_hook=crash_hook,
    )
    return store, supervisor


def start(supervisor: ActivitySliceSupervisor):
    activity_slice, created = supervisor.start_slice(
        session_id="cross-app-session",
        goal_id="goal-app-a",
        attention_decision_id="decision-app-a",
        attention_decision_revision=1,
        objective="在 App A 完成当前动作后处理微信通知",
        budget=SliceBudget(time_budget_seconds=180, action_limit=3),
        directive_revision=1,
        event_cursor=40,
    )
    assert created
    return activity_slice


def test_notification_preempts_at_verified_step_boundary(tmp_path: Path) -> None:
    boundaries = NotificationBoundaryPort()
    executor = AtomicActionExecutor(boundaries)
    store, supervisor = make_supervisor(
        tmp_path / "notification-boundary.db", boundaries, executor
    )
    activity_slice = start(supervisor)

    result = supervisor.run_slice(activity_slice.id)

    assert executor.timeline == [
        "decision",
        "action-persisted",
        "dispatch",
        "receipt",
        "after-observation",
        "verification",
    ]
    assert executor.calls == 1
    assert boundaries.captures == 2
    assert result.slice.status is ActivitySliceStatus.YIELDED
    assert result.slice.yield_reason is SliceYieldReason.EVENT_AVAILABLE
    assert len(result.steps) == 1
    settled = result.steps[0]
    assert settled.status is ActivityStepStatus.SETTLED
    assert settled.phase is ActivityStepPhase.SETTLED
    assert settled.verified is True
    assert settled.action_receipt_ref == "receipt-1"
    assert settled.after_observation_ref == "after-1"
    assert settled.verification_ref == "verification-1"
    assert store.pending_step(activity_slice.id) is None


def test_current_goal_continuation_is_saved_before_switch(tmp_path: Path) -> None:
    boundaries = NotificationBoundaryPort()
    executor = AtomicActionExecutor(boundaries)
    store, supervisor = make_supervisor(
        tmp_path / "continuation-before-switch.db", boundaries, executor
    )
    activity_slice = start(supervisor)

    result = supervisor.run_slice(activity_slice.id)
    handoff = result.preemption_handoff

    assert isinstance(handoff, PersistedPreemptionHandoff)
    persisted = store.continuation(activity_slice.id)
    assert persisted is not None
    assert persisted == result.continuation
    assert persisted.idempotency_key == handoff.continuation_idempotency_key
    assert persisted.goal_id == "goal-app-a"
    assert persisted.last_settled_step_id == result.steps[0].id
    assert persisted.checkpoint_ref.endswith("step:1:settled")
    assert persisted.observed_event_cursor == 41
    # A scheduler/service can receive only this post-persistence handoff Port
    # value; raw BoundarySnapshot is intentionally not a switch authority.
    assert handoff.request.current_goal_id == persisted.goal_id
    assert handoff.request.event_cursor == persisted.observed_event_cursor


def test_unsettled_atomic_action_cannot_form_preemption_request(tmp_path: Path) -> None:
    boundaries = NotificationBoundaryPort()
    executor = AtomicActionExecutor(boundaries)
    _, supervisor = make_supervisor(
        tmp_path / "unsettled-action.db", boundaries, executor
    )
    activity_slice = start(supervisor)
    first_boundary = boundaries.capture_boundary(None)
    step = supervisor.store.begin_step(
        activity_slice.id,
        first_boundary,
        now=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
    )
    notification = replace(
        first_boundary,
        fresh_observation_ref="notification-before-settlement",
        event_cursor=41,
        event_requires_yield=True,
    )

    with pytest.raises(
        VerifiedBoundaryPreemptionError,
        match="unsettled atomic action",
    ):
        build_verified_boundary_preemption(
            activity_slice,
            notification,
            replace(
                step,
                status=ActivityStepStatus.EXECUTING,
                phase=ActivityStepPhase.DISPATCHING,
            ),
        )


def test_restart_during_preemption_restores_one_current_goal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart-preemption.db"
    boundaries = NotificationBoundaryPort()
    executor = AtomicActionExecutor(boundaries)

    def crash_after_continuation(point, facts):
        if point == "after_continuation_before_yield":
            raise InjectedCrash(str(facts["continuation_key"]))

    store, first = make_supervisor(
        database, boundaries, executor, crash_hook=crash_after_continuation
    )
    activity_slice = start(first)

    with pytest.raises(InjectedCrash):
        first.run_slice(activity_slice.id)

    persisted_before_restart = store.continuation(activity_slice.id)
    assert persisted_before_restart is not None
    restarted_store, restarted = make_supervisor(database, boundaries, executor)
    first_recovery = restarted.recover(activity_slice.id)
    second_recovery = restarted.recover(activity_slice.id)

    assert executor.calls == 1
    assert first_recovery.continuation == persisted_before_restart
    assert second_recovery.continuation == persisted_before_restart
    assert first_recovery.preemption_handoff == second_recovery.preemption_handoff
    assert (
        first_recovery.preemption_handoff.request.id
        == second_recovery.preemption_handoff.request.id
    )
    assert (
        first_recovery.preemption_handoff.request.idempotency_key
        == second_recovery.preemption_handoff.request.idempotency_key
    )
    assert restarted_store.pending_step(activity_slice.id) is None
