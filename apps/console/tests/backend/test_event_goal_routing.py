from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from ai_game_console.agent_runtime.agenda import (
    AgendaContext,
    AgendaGoalSnapshot,
    EventUrgency,
)
from ai_game_console.agent_runtime.domain import (
    GoalGraphRevisionDraft,
    GoalNodeDraft,
    GoalNodeStatus,
    SessionEventType,
    WakeConditionDraft,
    WakeConditionKind,
)
from ai_game_console.agent_runtime.event_router import (
    EventRouteClass,
    SessionEventRouter,
)
from ai_game_console.agent_runtime.scheduler import (
    AttentionEvaluationScope,
    AttentionScheduler,
)
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore


NOW = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)


def _session_with_goals(
    store: SQLiteAgentRuntimeStore,
    *goal_ids: str,
):
    session, _ = store.create_unplanned_session(
        instruction="处理跨 App 事件",
        client_request_id=str(uuid.uuid4()),
    )
    directive = store.directives(session.id)[0]
    store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(
            authority_revision=1,
            source_directive_id=directive.id,
            base_graph_revision=0,
        ),
        nodes=[
            GoalNodeDraft(
                id=goal_id,
                title=goal_id,
                source_directive_id=directive.id,
                original_fragment=goal_id,
            )
            for goal_id in goal_ids
        ],
    )
    return session


def _goal(
    goal_id: str,
    *,
    status: GoalNodeStatus = GoalNodeStatus.READY,
    priority: int = 0,
    continuation_resumable: bool = False,
) -> AgendaGoalSnapshot:
    return AgendaGoalSnapshot(
        goal_id=goal_id,
        status=status,
        created_at=NOW - timedelta(minutes=5),
        explicit_priority=priority,
        continuation_resumable=continuation_resumable,
    )


def test_notification_routes_to_matching_goal(tmp_path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session = _session_with_goals(store, "wechat-a", "wechat-b")
    matched = store.create_wake_condition(
        session.id,
        "wechat-a",
        WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={
                "application_package": "com.tencent.mm",
                "conversation_hint": "A",
            },
        ),
    )
    store.create_wake_condition(
        session.id,
        "wechat-b",
        WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={
                "application_package": "com.tencent.mm",
                "conversation_hint": "B",
            },
        ),
    )

    result = SessionEventRouter(store).ingest(
        session.id,
        source_namespace="android-companion-v1",
        source_event_id="notification-1",
        event_type=SessionEventType.NOTIFICATION_POSTED,
        occurred_at="2026-08-26T01:00:00Z",
        payload={
            "package_name": "com.tencent.mm",
            "conversation_hint": "A",
            # Client-supplied routing claims are not authoritative.
            "affected_goal_ids": ["wechat-b"],
        },
    )

    assert result.affected_goal_ids == ("wechat-a",)
    assert result.classification.event_class is EventRouteClass.NOTIFICATION
    assert result.classification.urgency is EventUrgency.HIGH
    assert result.routing == {
        "event_class": "notification",
        "urgency": "HIGH",
        "reason": "exact notification match affected 1 Goal(s) through 1 WakeCondition(s)",
        "routes": [
            {
                "goal_id": "wechat-a",
                "wake_condition_id": matched.id,
                "matcher_kind": "EVENT",
                "reason": (
                    "matched exact notification fields: "
                    "application_package, conversation_hint"
                ),
            }
        ],
    }
    assert result.event.data["application_package"] == "com.tencent.mm"
    assert result.event.data["urgency"] == "HIGH"
    assert result.event.data["routing"] == result.routing
    persisted = next(
        event
        for event in store.events(session.id, after=0, limit=100)
        if event.id == result.event.id
    )
    assert persisted.data["routing"] == result.routing
    assert result.requires_attention_decision is True


def test_external_event_persists_to_event_inbox_before_routing(tmp_path):
    timeline: list[str] = []

    class OrderingStore(SQLiteAgentRuntimeStore):
        def ingest_event(self, *args, **kwargs):
            event, created = super().ingest_event(*args, **kwargs)
            assert self.event_by_id(event.id) == event
            timeline.append("event_inbox_persisted")
            return event, created

        def route_inbox_event(self, event_id):
            assert self.event_by_id(event_id) is not None
            timeline.append("wake_routed")
            return super().route_inbox_event(event_id)

        def record_event_routing(self, event_id, routing):
            assert self.event_by_id(event_id) is not None
            timeline.append("routing_explanation_persisted")
            return super().record_event_routing(event_id, routing)

    store = OrderingStore(tmp_path / "persist-before-route.db")
    session = _session_with_goals(store, "wechat")
    store.create_wake_condition(
        session.id,
        "wechat",
        WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={"application_package": "com.tencent.mm"},
        ),
    )

    result = SessionEventRouter(store).ingest(
        session.id,
        source_namespace="android-companion-v1",
        source_event_id="persist-before-route-1",
        event_type=SessionEventType.NOTIFICATION_POSTED,
        occurred_at="2026-08-26T01:00:00Z",
        payload={"application_package": "com.tencent.mm"},
    )

    assert timeline == [
        "event_inbox_persisted",
        "wake_routed",
        "routing_explanation_persisted",
    ]
    persisted = store.event_by_id(result.event.id)
    assert persisted is not None
    assert persisted.data["routing"] == result.routing
    assert result.requires_attention_decision is True


def test_foreground_event_uses_exact_application_match(tmp_path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session = _session_with_goals(store, "wechat", "settings")
    store.create_wake_condition(
        session.id,
        "wechat",
        WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={
                "event_type": SessionEventType.FOREGROUND_APPLICATION_CHANGED.value,
                "application_package": "com.tencent.mm",
            },
        ),
    )
    store.create_wake_condition(
        session.id,
        "settings",
        WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={
                "event_type": SessionEventType.FOREGROUND_APPLICATION_CHANGED.value,
                "application_package": "com.android.settings",
            },
        ),
    )

    result = SessionEventRouter(store).ingest(
        session.id,
        source_namespace="android-companion-v1",
        source_event_id="foreground-1",
        event_type=SessionEventType.FOREGROUND_APPLICATION_CHANGED,
        occurred_at="2026-08-26T01:00:00Z",
        payload={"foreground_package": "com.tencent.mm"},
    )

    assert result.affected_goal_ids == ("wechat",)
    assert result.classification.event_class is EventRouteClass.FOREGROUND
    assert result.classification.urgency is EventUrgency.NORMAL
    assert result.event.data["application_package"] == "com.tencent.mm"


def test_timer_event_wakes_only_its_exact_wake_condition(tmp_path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session = _session_with_goals(store, "timer-a", "timer-b")
    due_at = "2026-08-26T01:00:00Z"
    wake_a = store.create_wake_condition(
        session.id,
        "timer-a",
        WakeConditionDraft(kind=WakeConditionKind.TIME, due_at=due_at),
    )
    store.create_wake_condition(
        session.id,
        "timer-b",
        WakeConditionDraft(kind=WakeConditionKind.TIME, due_at=due_at),
    )

    result = SessionEventRouter(store).ingest(
        session.id,
        source_namespace="agent-runtime-timer",
        source_event_id=f"{wake_a.id}:{due_at}",
        event_type=SessionEventType.TIMER_DUE,
        occurred_at=due_at,
        payload={"wake_condition_id": wake_a.id, "due_at": due_at},
    )

    assert result.affected_goal_ids == ("timer-a",)
    assert result.classification.event_class is EventRouteClass.TIMER
    assert result.classification.urgency is EventUrgency.NORMAL
    assert result.classification.routes[0].reason == (
        "matched exact timer wake_condition_id"
    )


def test_duplicate_notification_causes_one_wake(tmp_path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session = _session_with_goals(store, "wechat")
    store.create_wake_condition(
        session.id,
        "wechat",
        WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={"application_package": "com.tencent.mm"},
        ),
    )
    router = SessionEventRouter(store)
    request = {
        "source_namespace": "android-companion-v1",
        "source_event_id": "notification-duplicate",
        "event_type": SessionEventType.NOTIFICATION_POSTED,
        "occurred_at": "2026-08-26T01:00:00Z",
        "payload": {"application_package": "com.tencent.mm"},
    }

    first = router.ingest(session.id, **request)
    replay = router.ingest(session.id, **request)

    assert first.created is True
    assert first.duplicate is False
    assert len(first.new_route_effects) == 1
    assert first.requires_attention_decision is True
    assert replay.event.id == first.event.id
    assert replay.routing == first.routing
    assert replay.duplicate is True
    assert replay.new_route_effects == ()
    assert replay.requires_attention_decision is False


def test_preemption_requires_new_attention_decision():
    scheduler = AttentionScheduler()
    plan = scheduler.request_event_attention(
        session_id="session-1",
        trigger_event_id="notification-event-1",
        original_instruction="处理微信消息并继续原工作",
        authority_revision=1,
        goals=(
            _goal("prior", status=GoalNodeStatus.ACTIVE),
            _goal("wechat"),
        ),
        context=AgendaContext(
            active_goal_id="prior",
            event_goal_ids=frozenset({"wechat"}),
            event_urgency=EventUrgency.HIGH,
        ),
        now=NOW,
        prior_goal_id="prior",
    )

    assert plan.selected_goal_id == "wechat"
    assert plan.requires_new_decision is True
    assert plan.is_preemption_candidate is True
    assert plan.evaluation_scope is AttentionEvaluationScope.ALL_ELIGIBLE_GOALS
    decision = plan.to_decision_draft(
        authority_revision=1,
        graph_revision=1,
        event_cursor=7,
    )
    assert decision.trigger_event_id == "notification-event-1"
    assert decision.selected_goal_id == "wechat"


def test_prior_goal_return_is_not_forced_lifo():
    scheduler = AttentionScheduler()
    plan = scheduler.request_attention(
        session_id="session-1",
        trigger_key="event:event-goal-waited",
        original_instruction="处理多个目标",
        authority_revision=1,
        goals=(
            _goal("prior", continuation_resumable=True),
            _goal("event-goal", status=GoalNodeStatus.WAITING_EVENT),
            _goal("higher-current-value", priority=600),
        ),
        context=AgendaContext(active_goal_id="event-goal"),
        now=NOW,
        trigger_event_id="event-goal-waited",
        prior_goal_id="prior",
    )

    assert plan.evaluated_goal_ids == (
        "prior",
        "event-goal",
        "higher-current-value",
    )
    assert plan.selected_goal_id == "higher-current-value"
    assert plan.forces_prior_goal_return is False
    assert plan.evaluation.for_goal("prior").eligibility.value == "ELIGIBLE"
