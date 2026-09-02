from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from ai_game_console.agent_runtime.activity_integration import NormalActivitySliceRunner
from ai_game_console.agent_runtime.fact_questions import FactQuestionCoordinator
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.activity_slice import (
    ActivitySliceStatus,
    BoundarySnapshot,
    ContinuationDirective,
    SliceBudget,
    SliceYieldReason,
)
from ai_game_console.agent_runtime.activity_store import ActivitySliceStore
from ai_game_console.agent_runtime.domain import (
    AttentionDecisionDraft,
    AttentionDecisionOutcome,
    AttentionSelectorKind,
    GoalEligibilityDraft,
    GoalEligibilityStatus,
    GoalGraphRevisionDraft,
    GoalNodeDraft,
    GoalNodeStatus,
    SliceBudgetDraft,
)
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.device_body.store import SQLiteDeviceBodyStore
from ai_game_console.kernel_canary import KernelApplicationCycleResult
from ai_game_console.long_lived_mobile_application_composition import (
    ActivitySliceDispatchRequest,
    compose_long_lived_mobile_application_runtime,
)
from ai_game_console.user_fact_runtime import SQLiteUserFactStore, UserFactService

from test_agent_session_store import FakeGoalService


class _RecoveredAgentRuntime:
    def __init__(self, slice_id: str) -> None:
        self.active_slice_id = slice_id

    def get_session(self, session_id: str):
        assert session_id == "session-recovery"
        return SimpleNamespace(active_slice_id=self.active_slice_id)

    def sessions_for_recovery(self):
        return [SimpleNamespace(active_slice_id=self.active_slice_id)]


class _RecoveredPreemptionCoordinator:
    def __init__(self, runtime: _RecoveredAgentRuntime) -> None:
        self.runtime = runtime
        self.handoffs = []

    def settle_preemption_checkpoint(self, handoff) -> None:
        self.handoffs.append(handoff)
        self.runtime.active_slice_id = None


class _Observation:
    def __init__(self) -> None:
        self.calls = 0

    def read_device_state(self, target_id: str):
        self.calls += 1
        assert target_id == "adb:test-device"
        return SimpleNamespace(
            captured_at=f"2026-08-26T01:00:{self.calls:02d}Z",
            foreground_app="com.android.settings",
        )


def test_normal_runner_maps_selected_decision_budget_and_persists_three_steps(tmp_path):
    runtime = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = runtime.create_unplanned_session(
        instruction="连续打开三个设置页面", client_request_id="session-1"
    )
    directive = runtime.directives(session.id)[0]
    goal_node_id = str(uuid4())
    runtime.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(
            authority_revision=1,
            source_directive_id=directive.id,
            base_graph_revision=0,
        ),
        nodes=[
            GoalNodeDraft(
                id=goal_node_id,
                title="设置三步",
                source_directive_id=directive.id,
                original_fragment="连续打开三个设置页面",
            )
        ],
    )
    intent = runtime.pending_activation_intents()[0]
    binding = runtime.settle_goal_run(intent.id, goal_run_id="goal-run-1")
    current = runtime.get_session(session.id)
    decision, _, _ = runtime.commit_attention_decision(
        session.id,
        draft=AttentionDecisionDraft(
            trigger_key="test:selected",
            authority_revision=1,
            graph_revision=1,
            event_cursor=current.event_cursor,
            outcome=AttentionDecisionOutcome.SELECTED,
            selected_goal_id=goal_node_id,
            selector_kind=AttentionSelectorKind.DETERMINISTIC,
            reason="selected",
            slice_budget=SliceBudgetDraft(time_budget_ms=180_000, action_budget=3),
        ),
        candidates=[
            GoalEligibilityDraft(
                goal_id=goal_node_id,
                eligibility=GoalEligibilityStatus.ELIGIBLE,
                reason="ready",
                goal_status=GoalNodeStatus.READY,
                rank=1,
            )
        ],
    )
    devices = SQLiteDeviceBodyStore(tmp_path / "agent-runtime.db")
    # Normal composition creates the Session DeviceBody binding only when the
    # first Kernel Step dispatches.  Slice startup must not require that later
    # side effect as an earlier precondition.
    assert devices.binding_for_session(session.id) is None
    observation = _Observation()
    runner = NormalActivitySliceRunner(
        ActivitySliceStore(tmp_path / "activity-slices.db"),
        agent_runtime_store=runtime,
        device_body_store=devices,
        observation_provider=observation,
        clock=lambda: datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
    )

    calls = []

    def execute(step):
        calls.append(step)
        index = step.ordinal
        return KernelApplicationCycleResult(
            task_id=f"task-{index}",
            cycle_key=f"cycle-{index}",
            target_id="adb:test-device",
            status="confirmed_success",
            application_id="com.android.settings",
            application_ready=True,
            before_observation_id=f"before-{index}",
            before_evidence_id=f"before-evidence-{index}",
            action_id=f"action-{index}",
            execution_id=f"execution-{index}",
            after_observation_id=f"after-{index}",
            after_evidence_id=f"after-evidence-{index}",
            verification_id=f"verification-{index}",
            verdict="SUCCESS",
            evidence=f"verified-{index}",
            physical_action_sent=True,
        )

    request = ActivitySliceDispatchRequest(
        idempotency_key="outer-cycle-1",
        application_instance_id="application-1",
        application_cycle=1,
        goal_id=binding.goal_run_id,
        target_id="adb:test-device",
        objective="连续打开三个设置页面",
        trigger_id="trigger-1",
        trigger_kind="goal_input",
        initial_application_context="com.android.settings",
    )
    no_action_result = runner.dispatch_activity_slice(
        replace(request, idempotency_key="outer-cycle-no-action"),
        lambda step: KernelApplicationCycleResult(
            task_id=f"no-action-task-{step.ordinal}",
            cycle_key=f"no-action-cycle-{step.ordinal}",
            target_id="adb:test-device",
            status="confirmed_failure",
            application_id="com.android.settings",
            application_ready=True,
            before_observation_id="no-action-before",
            before_evidence_id="no-action-before-evidence",
            action_id=None,
            execution_id=None,
            after_observation_id=None,
            after_evidence_id=None,
            verification_id=None,
            verdict=None,
            evidence="Kernel produced no physical action",
            physical_action_sent=False,
        ),
    )
    assert no_action_result.status == "incomplete"
    assert no_action_result.accepted is False
    assert no_action_result.physical_action_sent is False

    fact_service = UserFactService(SQLiteUserFactStore(tmp_path / "agent-runtime.db"))
    question_coordinator = FactQuestionCoordinator(
        fact_service, AgentSessionService(runtime, FakeGoalService())
    )
    runner.bind_fact_question_coordinator(question_coordinator)
    fact_wait_result = runner.dispatch_activity_slice(
        replace(request, idempotency_key="outer-cycle-fact-wait"),
        lambda step: KernelApplicationCycleResult(
            task_id=f"fact-task-{step.ordinal}",
            cycle_key=f"fact-cycle-{step.ordinal}",
            target_id="adb:test-device",
            status="needs_user_fact",
            application_id="com.example.hr",
            application_ready=True,
            before_observation_id="fact-before",
            before_evidence_id="fact-before-evidence",
            action_id=None,
            execution_id=None,
            after_observation_id=None,
            after_evidence_id=None,
            verification_id=None,
            verdict=None,
            evidence="HR asks expected salary",
            physical_action_sent=False,
            fact_need={
                "status": "UNKNOWN",
                "fact_key": "employment.expected_salary",
                "question": "HR 问期望薪资，我不知道，你期望多少？",
                "why_needed": "需要回答当前招聘问题",
                "answer_schema": {"type": "string", "minLength": 1},
                "applicability": {"domain": "employment"},
                "resume_stage": "reply-to-hr",
            },
        ),
    )
    assert fact_wait_result.status == "incomplete"
    assert fact_wait_result.physical_action_sent is False
    assert runtime.goal_nodes(session.id)[0].status is GoalNodeStatus.WAITING_USER_FACT
    assert fact_service.store.list_needs(session_id=session.id)[0].fact_key == "employment.expected_salary"

    # The remaining happy-path actions need the selected Goal eligible again.
    runtime.set_goal_ready(session.id, goal_node_id)

    result = runner.dispatch_activity_slice(request, execute)

    assert result.status == "confirmed_success"
    assert result.physical_action_sent is True
    assert len(result.step_ids) == len(calls) == 3
    saved = runner.store.get_slice(result.slice_id)
    assert saved.session_id == session.id
    assert saved.goal_id == goal_node_id
    assert saved.attention_decision_id == decision.id
    assert saved.budget.action_limit == 3
    assert saved.budget.time_budget_seconds == 180
    assert runner.dispatch_activity_slice(request, execute) == result
    assert len(calls) == 3

    composition = compose_long_lived_mobile_application_runtime(
        tmp_path,
        kernel=SimpleNamespace(),
        observation_provider=observation,
        activity_slice_runner=runner,
    )
    assert composition.runtime._activity_slice_runner is runner


def test_reconcile_settles_persisted_event_preemption_after_outer_timeout(tmp_path):
    now = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)
    store = ActivitySliceStore(tmp_path / "activity-slices.db")
    activity_slice, _ = store.create_slice(
        session_id="session-recovery",
        goal_id="goal-recovery",
        attention_decision_id="decision-recovery",
        attention_decision_revision=1,
        objective="continue settings",
        budget=SliceBudget(time_budget_seconds=300, action_limit=2),
        directive_revision=1,
        event_cursor=10,
        idempotency_key="outer-cycle-recovery",
        now=now,
    )
    key = (
        f"{activity_slice.idempotency_key}:preempt:event:11:checkpoint:0"
    )
    continuation = ContinuationDirective(
        idempotency_key=f"{key}:continuation",
        slice_id=activity_slice.id,
        session_id=activity_slice.session_id,
        goal_id=activity_slice.goal_id,
        reason=SliceYieldReason.EVENT_AVAILABLE,
        last_settled_step_id=None,
        resume_step_ordinal=1,
        observed_directive_revision=1,
        observed_event_cursor=11,
        remaining_time_seconds=299,
        remaining_actions=2,
        checkpoint_ref=f"slice:{activity_slice.id}:step:0:settled",
        wake_plan_id=None,
        created_at=now.isoformat().replace("+00:00", "Z"),
    )
    store.yield_slice(
        activity_slice.id,
        status=ActivitySliceStatus.YIELDED,
        reason=SliceYieldReason.EVENT_AVAILABLE,
        continuation=continuation,
        boundary=BoundarySnapshot(
            captured_at=now.isoformat().replace("+00:00", "Z"),
            fresh_observation_ref="device-state:recovery",
            directive_revision=1,
            event_cursor=11,
            event_requires_yield=True,
        ),
        now=now,
    )
    runtime = _RecoveredAgentRuntime(activity_slice.id)
    coordinator = _RecoveredPreemptionCoordinator(runtime)
    runner = NormalActivitySliceRunner(
        store,
        agent_runtime_store=runtime,
        device_body_store=SimpleNamespace(),
        observation_provider=SimpleNamespace(),
        preemption_coordinator=coordinator,
        clock=lambda: now,
    )

    assert runner.recover_persisted_preemptions() == 1
    first = runner.reconcile_activity_slice(activity_slice.idempotency_key)
    replay = runner.reconcile_activity_slice(activity_slice.idempotency_key)

    assert first is not None and first.status == "incomplete"
    assert replay == first
    assert len(coordinator.handoffs) == 1
    assert coordinator.handoffs[0].request.event_cursor == 11
