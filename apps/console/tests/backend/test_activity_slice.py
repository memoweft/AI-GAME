from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_game_console.agent_runtime.activity_slice import (
    ActivitySliceConflict,
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


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class Boundaries:
    def __init__(self, *items: BoundarySnapshot) -> None:
        self.items = deque(items)
        self.calls = 0

    def capture_boundary(self, request):
        self.calls += 1
        if self.items:
            return self.items.popleft()
        return boundary(self.calls)


class Executor:
    def __init__(self, *outcomes: StepExecutionResult) -> None:
        self.outcomes = deque(outcomes)
        self.contexts = []
        self.kernel_attempts: dict[str, int] = {}

    def execute_activity_step(self, context):
        self.contexts.append(context)
        result = self.outcomes.popleft() if self.outcomes else act(context.step.ordinal)
        context.record_progress(
            StepProgress(
                ActivityStepPhase.DECISION_COMMITTED,
                facts={"objective": context.slice.objective},
            )
        )
        if result.physical_action_ref:
            key = context.step.idempotency_key
            if key not in self.kernel_attempts:
                self.kernel_attempts[key] = 1
            context.record_progress(
                StepProgress(
                    ActivityStepPhase.ACTION_PERSISTED,
                    physical_action_ref=result.physical_action_ref,
                )
            )
            context.record_progress(
                StepProgress(
                    ActivityStepPhase.DISPATCHING,
                    physical_action_ref=result.physical_action_ref,
                )
            )
            context.record_progress(
                StepProgress(
                    ActivityStepPhase.RECEIPT_RECORDED,
                    physical_action_ref=result.physical_action_ref,
                    action_receipt_ref=result.action_receipt_ref,
                )
            )
            context.record_progress(
                StepProgress(
                    ActivityStepPhase.AFTER_OBSERVATION_CAPTURED,
                    physical_action_ref=result.physical_action_ref,
                    action_receipt_ref=result.action_receipt_ref,
                    after_observation_ref=result.after_observation_ref,
                )
            )
            context.record_progress(
                StepProgress(
                    ActivityStepPhase.VERIFICATION_RECORDED,
                    physical_action_ref=result.physical_action_ref,
                    action_receipt_ref=result.action_receipt_ref,
                    after_observation_ref=result.after_observation_ref,
                    verification_ref=result.verification_ref,
                )
            )
        return result


def boundary(
    index: int,
    *,
    directive_revision: int = 1,
    event_cursor: int = 0,
    **changes,
) -> BoundarySnapshot:
    return BoundarySnapshot(
        captured_at="2026-08-26T01:00:00Z",
        fresh_observation_ref=f"before-{index}",
        directive_revision=directive_revision,
        event_cursor=event_cursor,
        **changes,
    )


def act(index: int, *, verified: bool = True) -> StepExecutionResult:
    return StepExecutionResult(
        StepOutcome.ACT,
        decision={"action": f"tap-{index}"},
        physical_action_ref=f"kernel-action-{index}",
        action_receipt_ref=f"receipt-{index}",
        after_observation_ref=f"after-{index}",
        verification_ref=f"verification-{index}",
        verified=verified,
    )


def make_supervisor(
    tmp_path: Path,
    executor: Executor,
    boundaries: Boundaries | None = None,
    *,
    clock: Clock | None = None,
    crash_hook=None,
):
    clock = clock or Clock()
    store = ActivitySliceStore(tmp_path / "activity.db")
    supervisor = ActivitySliceSupervisor(
        store,
        boundaries or Boundaries(),
        executor,
        clock=clock,
        crash_hook=crash_hook,
    )
    activity_slice, created = supervisor.start_slice(
        session_id="session-1",
        goal_id="goal-1",
        attention_decision_id="decision-1",
        attention_decision_revision=1,
        objective="在系统设置中依次打开三个普通页面",
        budget=SliceBudget(time_budget_seconds=180, action_limit=3),
        directive_revision=1,
        event_cursor=0,
    )
    assert created
    return supervisor, store, activity_slice, clock


def test_slice_runs_multiple_verified_steps_without_timer_gap(tmp_path: Path) -> None:
    executor = Executor(act(1), act(2), act(3))
    supervisor, _, activity_slice, clock = make_supervisor(tmp_path, executor)

    result = supervisor.run_slice(activity_slice.id)

    assert [step.outcome for step in result.steps] == [StepOutcome.ACT] * 3
    assert result.slice.action_count == 3
    assert result.slice.yield_reason is SliceYieldReason.ACTION_BUDGET_EXHAUSTED
    assert result.wake_plan is None
    assert clock.value == datetime(2026, 8, 26, 1, 0, tzinfo=UTC)


def test_activity_slice_is_the_only_multi_iteration_supervisor(tmp_path: Path) -> None:
    executor = Executor(act(1), act(2))
    supervisor, _, activity_slice, _ = make_supervisor(tmp_path, executor)
    # The executor has a one-step port; only supervisor.run_slice performs iteration.
    assert not hasattr(executor, "run")

    result = supervisor.run_slice(activity_slice.id)

    assert len(executor.contexts) == 3  # third default ACT consumes final budget
    assert len(result.steps) == 3


def test_each_step_allows_at_most_one_kernel_physical_action() -> None:
    result = act(1)
    assert result.physical_action_ref == "kernel-action-1"
    assert not hasattr(result, "physical_action_refs")


def test_each_step_reads_fresh_observation(tmp_path: Path) -> None:
    boundaries = Boundaries(boundary(1), boundary(2), boundary(3))
    executor = Executor(act(1), act(2), act(3))
    supervisor, _, activity_slice, _ = make_supervisor(
        tmp_path, executor, boundaries
    )

    result = supervisor.run_slice(activity_slice.id)

    refs = [step.before_observation_ref for step in result.steps]
    assert refs == ["before-1", "before-2", "before-3"]
    assert len(set(refs + [step.after_observation_ref for step in result.steps])) == 6


def test_prior_goal_resume_uses_fresh_observation(tmp_path: Path) -> None:
    first_boundaries = Boundaries(
        boundary(1, event_cursor=40),
        boundary(2, event_cursor=41, event_requires_yield=True),
    )
    supervisor, store, first_slice, _ = make_supervisor(
        tmp_path,
        Executor(act(1)),
        first_boundaries,
    )

    preempted = supervisor.run_slice(first_slice.id)
    assert preempted.slice.yield_reason is SliceYieldReason.EVENT_AVAILABLE
    assert preempted.continuation is not None
    assert preempted.steps[0].before_observation_ref == "before-1"
    assert preempted.steps[0].after_observation_ref == "after-1"

    supervisor.boundary_port = Boundaries(boundary(3, event_cursor=41))
    supervisor.step_executor = Executor(act(2))
    resumed_slice, created = supervisor.start_slice(
        session_id=first_slice.session_id,
        goal_id=first_slice.goal_id,
        attention_decision_id="decision-2-reselected-prior-goal",
        attention_decision_revision=2,
        objective=first_slice.objective,
        budget=SliceBudget(time_budget_seconds=180, action_limit=1),
        directive_revision=1,
        event_cursor=41,
        idempotency_key="prior-goal:fresh-resume:decision-2",
    )
    assert created is True

    resumed = supervisor.run_slice(resumed_slice.id)

    assert resumed.steps[0].before_observation_ref == "before-3"
    assert resumed.steps[0].before_observation_ref not in {
        preempted.steps[0].before_observation_ref,
        preempted.steps[0].after_observation_ref,
        "before-2",
    }
    assert store.get_slice(resumed_slice.id).attention_decision_id == (
        "decision-2-reselected-prior-goal"
    )


def test_reusing_previous_after_observation_is_rejected(tmp_path: Path) -> None:
    boundaries = Boundaries(
        boundary(1),
        BoundarySnapshot(
            captured_at="2026-08-26T01:00:00Z",
            fresh_observation_ref="after-1",
            directive_revision=1,
            event_cursor=0,
        ),
    )
    supervisor, _, activity_slice, _ = make_supervisor(
        tmp_path, Executor(act(1), act(2)), boundaries
    )

    with pytest.raises(ActivitySliceConflict, match="reuse"):
        supervisor.run_slice(activity_slice.id)


def test_each_physical_action_has_own_kernel_ledger(tmp_path: Path) -> None:
    supervisor, _, activity_slice, _ = make_supervisor(
        tmp_path, Executor(act(1), act(2), act(3))
    )

    result = supervisor.run_slice(activity_slice.id)

    assert len({step.physical_action_ref for step in result.steps}) == 3
    assert len({step.action_receipt_ref for step in result.steps}) == 3
    assert len({step.verification_ref for step in result.steps}) == 3
    assert all(step.verified for step in result.steps)


def test_wait_event_creates_goal_wake_plan(tmp_path: Path) -> None:
    executor = Executor(
        StepExecutionResult(
            StepOutcome.WAIT_EVENT,
            wake_matcher={"event_type": "NotificationPostedEvent", "goal_id": "goal-1"},
        )
    )
    supervisor, _, activity_slice, _ = make_supervisor(tmp_path, executor)

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.status is ActivitySliceStatus.WAITING
    assert result.slice.yield_reason is SliceYieldReason.WAIT_EVENT
    assert result.wake_plan is not None
    assert result.wake_plan.matcher == {
        "event_type": "NotificationPostedEvent",
        "goal_id": "goal-1",
    }
    assert result.continuation.wake_plan_id == result.wake_plan.id


def test_wait_time_uses_persisted_due_time(tmp_path: Path) -> None:
    due_at = "2026-08-26T02:30:00Z"
    executor = Executor(StepExecutionResult(StepOutcome.WAIT_TIME, due_at=due_at))
    supervisor, store, activity_slice, _ = make_supervisor(tmp_path, executor)

    first = supervisor.run_slice(activity_slice.id)
    reopened = ActivitySliceStore(store.database_path)

    assert first.wake_plan.due_at == due_at
    assert reopened.get_wake_plan_for_slice(activity_slice.id).due_at == due_at


def test_goal_complete_remains_proposal_until_runtime_verifies(tmp_path: Path) -> None:
    proposal = StepExecutionResult(
        StepOutcome.GOAL_COMPLETE,
        completion_proposal={"summary": "model says done"},
    )
    supervisor, _, activity_slice, _ = make_supervisor(tmp_path, Executor(proposal))

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.status is ActivitySliceStatus.YIELDED
    assert result.slice.yield_reason is SliceYieldReason.GOAL_COMPLETE_PROPOSAL
    assert result.steps[0].result_facts["completion_verified"] is False


def test_runtime_verified_goal_complete_only_completes_slice(tmp_path: Path) -> None:
    verified = StepExecutionResult(
        StepOutcome.GOAL_COMPLETE,
        physical_action_ref="kernel-action-complete",
        action_receipt_ref="receipt-complete",
        after_observation_ref="after-complete",
        verification_ref="verification-complete",
        verified=True,
        completion_proposal={"summary": "candidate"},
        completion_verified=True,
    )
    supervisor, _, activity_slice, _ = make_supervisor(tmp_path, Executor(verified))

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.status is ActivitySliceStatus.COMPLETED
    assert result.steps[0].result_facts["completion_verified"] is True


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_reason"),
    [
        (
            StepExecutionResult(StepOutcome.NEED_USER_FACT, user_fact_key="account.city"),
            ActivitySliceStatus.WAITING,
            SliceYieldReason.NEED_USER_FACT,
        ),
        (
            StepExecutionResult(StepOutcome.YIELD_TO_SCHEDULER),
            ActivitySliceStatus.YIELDED,
            SliceYieldReason.YIELD_TO_SCHEDULER,
        ),
    ],
)
def test_activity_step_typed_outcomes_project_without_crossing_goal_authority(
    tmp_path: Path,
    outcome: StepExecutionResult,
    expected_status: ActivitySliceStatus,
    expected_reason: SliceYieldReason,
) -> None:
    supervisor, _, activity_slice, _ = make_supervisor(tmp_path, Executor(outcome))

    result = supervisor.run_slice(activity_slice.id)

    assert result.slice.status is expected_status
    assert result.slice.yield_reason is expected_reason


def test_activity_objective_has_no_fixed_soul_semantics(tmp_path: Path) -> None:
    executor = Executor(StepExecutionResult(StepOutcome.YIELD_TO_SCHEDULER))
    supervisor, _, activity_slice, _ = make_supervisor(tmp_path, executor)

    supervisor.run_slice(activity_slice.id)

    assert executor.contexts[0].slice.objective == "在系统设置中依次打开三个普通页面"
    assert "Soul" not in executor.contexts[0].slice.objective
