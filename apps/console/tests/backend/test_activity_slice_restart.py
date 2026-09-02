from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_game_console.agent_runtime.activity_slice import (
    ActivitySliceStatus,
    ActivitySliceSupervisor,
    ActivityStepPhase,
    BoundarySnapshot,
    SliceBudget,
    SliceYieldReason,
    StepExecutionResult,
    StepOutcome,
    StepProgress,
)
from ai_game_console.agent_runtime.activity_store import ActivitySliceStore


CRASH_POINTS = (
    "before_observation",
    "after_observation_before_decision",
    "after_decision_before_action_persist",
    "after_action_persist_before_dispatch",
    "during_dispatch",
    "after_receipt_before_after_observation",
    "after_after_observation_before_verify",
    "after_verify_before_step_settle",
    "after_step_settle_before_next_step",
    "after_continuation_before_yield",
)


class InjectedCrash(RuntimeError):
    pass


class FailOnce:
    def __init__(self, target: str) -> None:
        self.target = target
        self.failed = False

    def __call__(self, point, facts) -> None:
        if point == self.target and not self.failed:
            self.failed = True
            raise InjectedCrash(point)


class BoundaryPort:
    def __init__(self) -> None:
        self.count = 0

    def capture_boundary(self, request):
        self.count += 1
        return BoundarySnapshot(
            captured_at="2026-08-26T01:00:00Z",
            fresh_observation_ref=f"restart-before-{self.count}",
            directive_revision=1,
            event_cursor=0,
        )


class IdempotentKernelStepExecutor:
    def __init__(self) -> None:
        self.physical_execution_counts: dict[str, int] = {}
        self.action_by_key: dict[str, str] = {}

    def execute_activity_step(self, context):
        key = context.step.idempotency_key
        action_ref = self.action_by_key.setdefault(key, f"kernel:{key}")
        context.record_progress(
            StepProgress(ActivityStepPhase.DECISION_COMMITTED)
        )
        context.record_progress(
            StepProgress(
                ActivityStepPhase.ACTION_PERSISTED,
                physical_action_ref=action_ref,
            )
        )
        context.record_progress(
            StepProgress(
                ActivityStepPhase.DISPATCHING,
                physical_action_ref=action_ref,
            )
        )
        if key not in self.physical_execution_counts:
            self.physical_execution_counts[key] = 1
        receipt_ref = f"receipt:{key}"
        after_ref = f"after:{key}"
        verify_ref = f"verify:{key}"
        context.record_progress(
            StepProgress(
                ActivityStepPhase.RECEIPT_RECORDED,
                physical_action_ref=action_ref,
                action_receipt_ref=receipt_ref,
            )
        )
        context.record_progress(
            StepProgress(
                ActivityStepPhase.AFTER_OBSERVATION_CAPTURED,
                physical_action_ref=action_ref,
                action_receipt_ref=receipt_ref,
                after_observation_ref=after_ref,
            )
        )
        context.record_progress(
            StepProgress(
                ActivityStepPhase.VERIFICATION_RECORDED,
                physical_action_ref=action_ref,
                action_receipt_ref=receipt_ref,
                after_observation_ref=after_ref,
                verification_ref=verify_ref,
            )
        )
        return StepExecutionResult(
            StepOutcome.ACT,
            physical_action_ref=action_ref,
            action_receipt_ref=receipt_ref,
            after_observation_ref=after_ref,
            verification_ref=verify_ref,
            verified=True,
        )


def make_supervisor(database: Path, boundary_port, executor, hook=None):
    return ActivitySliceSupervisor(
        ActivitySliceStore(database),
        boundary_port,
        executor,
        clock=lambda: datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
        crash_hook=hook,
    )


@pytest.mark.parametrize("crash_point", CRASH_POINTS)
def test_restart_matrix_converges_without_duplicate_action(
    tmp_path: Path, crash_point: str
) -> None:
    database = tmp_path / "restart.db"
    boundary_port = BoundaryPort()
    executor = IdempotentKernelStepExecutor()
    fail_once = FailOnce(crash_point)
    first = make_supervisor(database, boundary_port, executor, fail_once)
    activity_slice, _ = first.start_slice(
        session_id="restart-session",
        goal_id="restart-goal",
        attention_decision_id="restart-decision",
        attention_decision_revision=1,
        objective="重启后继续普通设置导航",
        budget=SliceBudget(60, 1),
        directive_revision=1,
        event_cursor=0,
    )

    with pytest.raises(InjectedCrash, match=crash_point):
        first.run_slice(activity_slice.id)

    restarted = make_supervisor(database, boundary_port, executor, fail_once)
    result = restarted.recover(activity_slice.id)

    assert result.slice.status is ActivitySliceStatus.YIELDED
    assert result.slice.yield_reason is SliceYieldReason.ACTION_BUDGET_EXHAUSTED
    assert len(result.steps) == 1
    assert result.steps[0].status.value == "SETTLED"
    assert result.steps[0].physical_action_ref is not None
    assert list(executor.physical_execution_counts.values()) == [1]
    assert result.continuation is not None
    assert result.continuation.idempotency_key == result.slice.continuation_key


def test_repeated_recovery_returns_same_continuation_and_yield(tmp_path: Path) -> None:
    database = tmp_path / "repeat.db"
    boundary_port = BoundaryPort()
    executor = IdempotentKernelStepExecutor()
    supervisor = make_supervisor(database, boundary_port, executor)
    activity_slice, _ = supervisor.start_slice(
        session_id="repeat-session",
        goal_id="repeat-goal",
        attention_decision_id="repeat-decision",
        attention_decision_revision=1,
        objective="验证幂等 continuation",
        budget=SliceBudget(60, 1),
        directive_revision=1,
        event_cursor=0,
    )

    first = supervisor.run_slice(activity_slice.id)
    second = make_supervisor(database, boundary_port, executor).recover(activity_slice.id)

    assert second.slice == first.slice
    assert second.continuation == first.continuation
    assert len(second.steps) == 1
    assert list(executor.physical_execution_counts.values()) == [1]
