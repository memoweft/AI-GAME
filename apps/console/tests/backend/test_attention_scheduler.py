from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_game_console.agent_runtime.agenda import (
    AgendaContext,
    AgendaGoalSnapshot,
    AttentionHardTier,
    EventUrgency,
    GoalEligibilityStatus,
    GoalSchedulingClass,
    evaluate_agenda,
)
from ai_game_console.agent_runtime.domain import (
    AttentionDecisionOutcome,
    AttentionSelectorKind,
    GoalNodeStatus,
)
from ai_game_console.agent_runtime.scheduler import (
    AttentionScheduler,
    AttentionSelectionDraft,
    DeterministicAttentionSelector,
    TransportFallbackAttentionSelector,
)


NOW = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)


def _goal(
    goal_id: str,
    *,
    priority: int = 0,
    status: GoalNodeStatus = GoalNodeStatus.READY,
    created_at: datetime | None = None,
    last_service_at: datetime | None = None,
    scheduling_class: GoalSchedulingClass = GoalSchedulingClass.NORMAL,
    **changes,
) -> AgendaGoalSnapshot:
    return AgendaGoalSnapshot(
        goal_id=goal_id,
        status=status,
        created_at=created_at or NOW - timedelta(minutes=5),
        explicit_priority=priority,
        scheduling_class=scheduling_class,
        last_service_at=last_service_at,
        **changes,
    )


def _request(
    *goals: AgendaGoalSnapshot,
    context: AgendaContext | None = None,
    now: datetime = NOW,
    scheduler: AttentionScheduler | None = None,
):
    return (scheduler or AttentionScheduler()).request_attention(
        session_id="session-1",
        trigger_key="test:attention",
        original_instruction="处理今天的多个目标",
        authority_revision=1,
        goals=goals,
        context=context or AgendaContext(),
        now=now,
    )


def test_scheduler_selects_highest_current_value_goal():
    plan = _request(_goal("A", priority=300), _goal("B"))

    assert plan.selected_goal_id == "A"
    assert [item.goal_id for item in plan.evaluation.candidates] == ["A", "B"]
    assert plan.evaluation.for_goal("A").user_priority_component == 300
    assert plan.evaluation.for_goal("A").total_score == 305
    assert plan.shortlist_goal_ids == ("A",)
    decision = plan.to_decision_draft(
        authority_revision=1,
        graph_revision=1,
        event_cursor=5,
    )
    assert decision.selected_goal_id == "A"
    assert decision.reason == "deterministic rank 1: tier 1, score 305"


def test_first_uninterrupted_active_goal_receives_continuity_without_continuation():
    active = _goal(
        "active",
        status=GoalNodeStatus.ACTIVE,
        continuation_resumable=False,
    )
    ready = _goal("ready")
    plan = _request(
        active,
        ready,
        context=AgendaContext(active_goal_id="active"),
    )

    assert plan.selected_goal_id == "active"
    assert plan.evaluation.for_goal("active").continuity_component == 250
    assert plan.evaluation.for_goal("ready").continuity_component == 0


def test_latest_user_directive_changes_next_attention_decision():
    goals = (_goal("A", priority=1_000), _goal("B"))
    baseline = _request(*goals)
    directive = _request(
        *goals,
        context=AgendaContext(latest_directive_goal_ids=frozenset({"B"})),
    )

    assert baseline.selected_goal_id == "A"
    assert directive.selected_goal_id == "B"
    assert directive.selected.hard_tier == AttentionHardTier.LATEST_USER_DIRECTIVE
    assert directive.shortlist_goal_ids == ("B",)


def test_waiting_goal_does_not_hold_execution_slot():
    waiting = _goal(
        "A",
        priority=1_000,
        status=GoalNodeStatus.WAITING_TIME,
        next_eligible_at=NOW + timedelta(minutes=5),
    )
    plan = _request(waiting, _goal("B"), context=AgendaContext(active_goal_id="A"))

    assert plan.selected_goal_id == "B"
    assert plan.evaluation.for_goal("A").eligibility is GoalEligibilityStatus.WAITING
    assert "WAITING_TIME" in plan.evaluation.for_goal("A").reason


def test_unsatisfied_goal_graph_dependency_blocks_high_priority_successor():
    blocked = _goal(
        "successor",
        priority=1_000,
        dependencies_satisfied=False,
    )
    plan = _request(blocked, _goal("predecessor"))

    assert plan.selected_goal_id == "predecessor"
    assert (
        plan.evaluation.for_goal("successor").eligibility
        is GoalEligibilityStatus.DEPENDENCY_BLOCKED
    )


def test_woken_goal_carries_wait_age_into_first_eligible_decision():
    woken = _goal(
        "woken",
        status=GoalNodeStatus.READY,
        wait_started_at=NOW - timedelta(minutes=17),
    )
    plan = _request(woken)

    assert plan.selected_goal_id == "woken"
    assert plan.selected.waiting_age_component == 17


def test_event_wakes_only_matching_goal():
    a = _goal("wechat-A", status=GoalNodeStatus.WAITING_EVENT, wake_satisfied=True)
    b = _goal("wechat-B", status=GoalNodeStatus.WAITING_EVENT)
    plan = _request(
        a,
        b,
        _goal("other", priority=1_000),
        context=AgendaContext(
            event_goal_ids=frozenset({"wechat-A"}),
            event_urgency=EventUrgency.CRITICAL,
        ),
    )

    assert plan.selected_goal_id == "wechat-A"
    assert plan.selected.hard_tier == AttentionHardTier.CRITICAL_EVENT
    assert plan.evaluation.for_goal("wechat-B").eligibility is GoalEligibilityStatus.WAITING
    assert plan.evaluation.for_goal("other").matched_trigger_event is False


def test_idle_only_goal_runs_when_normal_ready_set_is_empty():
    normal = _goal("work")
    idle = _goal(
        "game",
        priority=1_000,
        scheduling_class=GoalSchedulingClass.IDLE_ONLY,
    )
    busy = _request(normal, idle)
    free = _request(
        _goal("work", status=GoalNodeStatus.WAITING_TIME),
        idle,
    )

    assert busy.selected_goal_id == "work"
    assert busy.evaluation.for_goal("game").eligibility is GoalEligibilityStatus.IDLE_DEFERRED
    assert free.selected_goal_id == "game"
    assert free.selected.hard_tier == AttentionHardTier.IDLE_ONLY


def test_latest_user_directive_can_explicitly_promote_idle_goal():
    plan = _request(
        _goal("work", priority=1_000),
        _goal("game", scheduling_class=GoalSchedulingClass.IDLE_ONLY),
        context=AgendaContext(latest_directive_goal_ids=frozenset({"game"})),
    )
    assert plan.selected_goal_id == "game"
    assert plan.selected.hard_tier == AttentionHardTier.LATEST_USER_DIRECTIVE


def test_starvation_age_eventually_serves_ready_goal():
    t0 = NOW - timedelta(minutes=30)
    low = _goal("low", priority=0, created_at=t0, last_service_at=t0)
    high = _goal(
        "high",
        priority=1_000,
        created_at=t0,
        last_service_at=NOW - timedelta(seconds=1),
    )

    before = _request(low, high, now=NOW - timedelta(seconds=1))
    at_deadline = _request(low, high, now=NOW)

    assert before.selected_goal_id == "high"
    assert before.evaluation.for_goal("low").hard_tier == AttentionHardTier.NORMAL
    assert at_deadline.selected_goal_id == "low"
    assert at_deadline.selected.hard_tier == AttentionHardTier.STARVATION_DEADLINE
    assert at_deadline.shortlist_goal_ids == ("low",)


def test_backoff_prevents_immediate_failure_loop():
    failed = _goal(
        "A",
        priority=500,
        consecutive_failure_count=1,
        backoff_until=NOW + timedelta(seconds=60),
    )
    sibling = _goal("B")

    at_failure = _request(failed, sibling)
    just_before = _request(failed, sibling, now=NOW + timedelta(seconds=59, milliseconds=999))
    eligible = _request(failed, sibling, now=NOW + timedelta(seconds=60))

    assert at_failure.selected_goal_id == "B"
    assert just_before.evaluation.for_goal("A").eligibility is GoalEligibilityStatus.BACKOFF
    assert eligible.selected_goal_id == "A"
    assert eligible.selected.recent_failure_component == 200
    assert eligible.selected.total_score > eligible.evaluation.for_goal("B").total_score


class _FixedSelector:
    def __init__(self, goal_id: str):
        self.goal_id = goal_id

    def select(self, context):
        return AttentionSelectionDraft(
            selected_goal_id=self.goal_id,
            reason="bounded model choice",
            selector_kind=AttentionSelectorKind.QWEN,
        )


def test_selector_is_bounded_to_maximum_hard_tier_and_top_score_band():
    scheduler = AttentionScheduler(_FixedSelector("B"))
    with pytest.raises(ValueError, match="outside bounded shortlist"):
        _request(
            _goal("A", priority=300),
            _goal("B"),
            scheduler=scheduler,
        )


def test_model_may_choose_inside_score_band_and_adjustment_is_persistable():
    plan = _request(
        _goal("A", priority=100),
        _goal("B", priority=50),
        scheduler=AttentionScheduler(_FixedSelector("B")),
    )
    decision = plan.to_decision_draft(
        authority_revision=1,
        graph_revision=1,
        event_cursor=3,
    )
    assert plan.selected_goal_id == "B"
    assert plan.shortlist_goal_ids == ("A", "B")
    assert decision.selector_kind is AttentionSelectorKind.QWEN
    assert decision.model_adjustment == -50


def test_no_eligible_goal_forms_explicit_no_eligible_decision():
    plan = _request(_goal("A", status=GoalNodeStatus.WAITING_EVENT))
    decision = plan.to_decision_draft(
        authority_revision=1,
        graph_revision=1,
        event_cursor=2,
    )
    assert plan.selected_goal_id is None
    assert decision.outcome is AttentionDecisionOutcome.NO_ELIGIBLE
    assert decision.selected_goal_id is None
    assert "no eligible Goal" in decision.reason


def test_only_declared_transport_error_uses_deterministic_fallback():
    class ModelTransportError(RuntimeError):
        pass

    class BrokenTransport:
        def select(self, context):
            raise ModelTransportError("offline")

    fallback = TransportFallbackAttentionSelector(
        BrokenTransport(), transport_errors=(ModelTransportError,)
    )
    plan = _request(_goal("A"), scheduler=AttentionScheduler(fallback))
    assert plan.selected_goal_id == "A"
    assert plan.selector_kind is AttentionSelectorKind.DETERMINISTIC

    class BrokenSchema:
        def select(self, context):
            raise ValueError("bad model schema")

    no_schema_fallback = TransportFallbackAttentionSelector(
        BrokenSchema(), transport_errors=(ModelTransportError,)
    )
    with pytest.raises(ValueError, match="bad model schema"):
        _request(_goal("A"), scheduler=AttentionScheduler(no_schema_fallback))


def test_tie_break_is_stable_by_service_then_creation_then_goal_id():
    oldest_service = NOW - timedelta(minutes=10)
    created = NOW - timedelta(minutes=5)
    evaluation = evaluate_agenda(
        (
            _goal("B", created_at=created, last_service_at=oldest_service),
            _goal("A", created_at=created, last_service_at=oldest_service),
        ),
        context=AgendaContext(),
        now=NOW,
    )
    assert [item.goal_id for item in evaluation.candidates] == ["A", "B"]
