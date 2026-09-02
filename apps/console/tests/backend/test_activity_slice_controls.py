from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_game_console.agent_runtime.activity_slice import (
    ActivitySliceStatus,
    ActivitySliceSupervisor,
    BoundarySnapshot,
    SliceBudget,
    SliceYieldReason,
    StepExecutionResult,
    StepOutcome,
)
from ai_game_console.agent_runtime.activity_store import ActivitySliceStore


class Port:
    def __init__(self, boundaries, outcomes, *, on_capture=None):
        self.boundaries = deque(boundaries)
        self.outcomes = deque(outcomes)
        self.contexts = []
        self.capture_count = 0
        self.on_capture = on_capture

    def capture_boundary(self, request):
        self.capture_count += 1
        if self.on_capture:
            self.on_capture(self.capture_count)
        return self.boundaries.popleft()

    def execute_activity_step(self, context):
        self.contexts.append(context)
        return self.outcomes.popleft()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def snapshot(index: int, **changes):
    values = {
        "captured_at": "2026-08-26T01:00:00Z",
        "fresh_observation_ref": f"before-control-{index}",
        "directive_revision": 1,
        "event_cursor": 0,
    }
    values.update(changes)
    return BoundarySnapshot(**values)


def action(index: int):
    return StepExecutionResult(
        StepOutcome.ACT,
        physical_action_ref=f"control-action-{index}",
        action_receipt_ref=f"control-receipt-{index}",
        after_observation_ref=f"control-after-{index}",
        verification_ref=f"control-verification-{index}",
        verified=True,
    )


def start(tmp_path: Path, port: Port, *, clock=None, action_limit=3):
    store = ActivitySliceStore(tmp_path / "controls.db")
    supervisor = ActivitySliceSupervisor(
        store, port, port, clock=clock or Clock()
    )
    activity_slice, _ = supervisor.start_slice(
        session_id="control-session",
        goal_id="control-goal",
        attention_decision_id="control-decision",
        attention_decision_revision=1,
        objective="通用设置导航",
        budget=SliceBudget(60, action_limit),
        directive_revision=1,
        event_cursor=0,
    )
    return supervisor, activity_slice


def test_directive_between_steps_changes_next_decision(tmp_path: Path) -> None:
    port = Port(
        [snapshot(1), snapshot(2, directive_revision=2)],
        [action(1), StepExecutionResult(StepOutcome.YIELD_TO_SCHEDULER)],
    )
    supervisor, activity_slice = start(tmp_path, port)

    result = supervisor.run_slice(activity_slice.id)

    assert [context.boundary.directive_revision for context in port.contexts] == [1, 2]
    assert result.continuation.observed_directive_revision == 2


def test_notification_between_steps_can_request_yield(tmp_path: Path) -> None:
    port = Port(
        [
            snapshot(1),
            snapshot(2, event_cursor=8, event_requires_yield=True),
        ],
        [action(1)],
    )
    supervisor, activity_slice = start(tmp_path, port)

    result = supervisor.run_slice(activity_slice.id)

    assert len(result.steps) == 1
    assert result.slice.yield_reason is SliceYieldReason.EVENT_AVAILABLE
    assert result.continuation.observed_event_cursor == 8


def test_user_touch_between_steps_enters_user_active(tmp_path: Path) -> None:
    port = Port([snapshot(1), snapshot(2, human_active=True)], [action(1)])
    supervisor, activity_slice = start(tmp_path, port)

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.status is ActivitySliceStatus.YIELDED
    assert result.slice.yield_reason is SliceYieldReason.USER_ACTIVE
    assert len(result.steps) == 1


def test_stop_request_between_steps_yields_with_checkpoint(tmp_path: Path) -> None:
    port = Port([snapshot(1), snapshot(2, stop_requested=True)], [action(1)])
    supervisor, activity_slice = start(tmp_path, port)

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.yield_reason is SliceYieldReason.STOP_REQUESTED
    assert result.continuation.last_settled_step_id == result.steps[0].id
    assert result.continuation.checkpoint_ref.endswith("step:1:settled")


def test_run_to_completion_loop_cannot_bypass_step_boundaries(tmp_path: Path) -> None:
    port = Port(
        [snapshot(1), snapshot(2), snapshot(3), snapshot(4)],
        [action(1), action(2), action(3)],
    )
    supervisor, activity_slice = start(tmp_path, port)

    result = supervisor.run_slice(activity_slice.id)

    assert port.capture_count == 4  # final capture performs budget checkpoint
    assert len(port.contexts) == 3
    assert len(result.steps) == 3


def test_time_budget_yields_with_continuation(tmp_path: Path) -> None:
    clock = Clock()

    def advance_on_second_capture(count):
        if count == 2:
            clock.now = datetime(2026, 8, 26, 1, 2, tzinfo=UTC)

    port = Port(
        [snapshot(1), snapshot(2)],
        [action(1)],
        on_capture=advance_on_second_capture,
    )
    supervisor, activity_slice = start(tmp_path, port, clock=clock)

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.yield_reason is SliceYieldReason.TIME_BUDGET_EXHAUSTED
    assert result.continuation.remaining_time_seconds == 0
    assert result.continuation.last_settled_step_id == result.steps[0].id


def test_action_budget_yields_with_continuation(tmp_path: Path) -> None:
    port = Port([snapshot(1), snapshot(2)], [action(1)])
    supervisor, activity_slice = start(tmp_path, port, action_limit=1)

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.yield_reason is SliceYieldReason.ACTION_BUDGET_EXHAUSTED
    assert result.continuation.remaining_actions == 0
    assert result.continuation.resume_step_ordinal == 2


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"control_mode": "TAKEOVER"}, SliceYieldReason.CONTROL_MODE_CHANGED),
        ({"checkpoint_requested": True}, SliceYieldReason.CHECKPOINT_REQUESTED),
        (
            {"directive_revision": 2, "directive_requires_yield": True},
            SliceYieldReason.DIRECTIVE_CHANGED,
        ),
    ],
)
def test_boundary_controls_are_typed(tmp_path: Path, changes, reason) -> None:
    port = Port([snapshot(1, **changes)], [])
    supervisor, activity_slice = start(tmp_path, port)

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.yield_reason is reason
    assert result.steps == ()
