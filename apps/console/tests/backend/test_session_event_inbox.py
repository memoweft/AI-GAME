from __future__ import annotations

import uuid

import pytest

from ai_game_console.agent_runtime.domain import (
    GoalGraphRevisionDraft,
    GoalNodeDraft,
    GoalNodeStatus,
    SessionEventType,
    SessionIdempotencyConflict,
    WakeConditionDraft,
    WakeConditionKind,
    WakeConditionStatus,
)
from ai_game_console.agent_runtime.event_router import (
    DeviceBodyEventInbox,
    SessionEventRouter,
)
from ai_game_console.agent_runtime.scheduler import AttentionScheduler
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.device_body.domain import (
    BodyEvent,
    BodyEventType,
    ConnectionState,
    DeviceBodyBinding,
    DeviceSnapshot,
    HumanPresenceState,
    NetworkState,
    Orientation,
)
from ai_game_console.device_body.store import SQLiteDeviceBodyStore
from test_session_planner import PreparedGoalService


def _session_with_goals(store: SQLiteAgentRuntimeStore, count: int = 2):
    session, _ = store.create_unplanned_session(
        instruction="等待两个不同会话", client_request_id=str(uuid.uuid4())
    )
    directive = store.directives(session.id)[0]
    goal_ids = [str(uuid.uuid4()) for _ in range(count)]
    store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(
            authority_revision=1, source_directive_id=directive.id, base_graph_revision=0
        ),
        nodes=[
            GoalNodeDraft(
                id=goal_id,
                title=f"Goal {index}",
                source_directive_id=directive.id,
                original_fragment=f"Goal {index}",
            )
            for index, goal_id in enumerate(goal_ids)
        ],
    )
    return session, goal_ids


def test_event_wakes_only_matching_goal(tmp_path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, (first, second) = _session_with_goals(store)
    first_wake = store.create_wake_condition(
        session.id,
        first,
        WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={"application_package": "com.tencent.mm", "conversation_hint": "A"},
        ),
    )
    second_wake = store.create_wake_condition(
        session.id,
        second,
        WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={"application_package": "com.tencent.mm", "conversation_hint": "B"},
        ),
    )

    result = SessionEventRouter(store).ingest(
        session.id,
        source_namespace="test",
        source_event_id="notification-1",
        event_type=SessionEventType.NOTIFICATION_POSTED,
        occurred_at="2026-08-24T01:00:00Z",
        payload={
            "application_package": "com.tencent.mm",
            "conversation_hint": "A",
            "affected_goal_ids": [second],
        },
    )

    assert result.created is True
    assert result.affected_goal_ids == (first,)
    status_by_id = {item.id: item.status.value for item in store.goal_nodes(session.id)}
    assert status_by_id[first] == "READY"
    assert status_by_id[second] == "WAITING_EVENT"
    wake_by_id = {item.id: item for item in store.wake_conditions(session.id)}
    assert wake_by_id[first_wake.id].status is WakeConditionStatus.SATISFIED
    assert wake_by_id[second_wake.id].status is WakeConditionStatus.PENDING


def test_duplicate_event_produces_one_decision_trigger(tmp_path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = AgentSessionService(
        store,
        PreparedGoalService(),
        attention_scheduler=AttentionScheduler(),
    )
    session = service.create("等待微信消息", "duplicate-event-session")
    goal_id = session["goal_nodes"][0]["id"]
    store.create_wake_condition(
        session["id"], goal_id, WakeConditionDraft(
            kind=WakeConditionKind.EVENT,
            matcher={"application_package": "com.tencent.mm", "conversation_hint": "A"},
        )
    )
    router = SessionEventRouter(store)
    request = dict(
        source_namespace="device:provided-by-client-but-not-authority",
        source_event_id="source-1",
        event_type=SessionEventType.NOTIFICATION_POSTED,
        occurred_at="2026-08-24T01:00:00Z",
        payload={"application_package": "com.tencent.mm", "conversation_hint": "A"},
        device_id="device-1",
        device_boot_id="boot-1",
    )

    first = router.ingest(session["id"], **request)
    assert first.created is True
    first_decision = service.handle_inbox_event(first.event.id)
    replay = router.ingest(session["id"], **request)

    assert replay.created is False
    assert replay.event.id == first.event.id
    assert replay.affected_goal_ids == (goal_id,)
    assert store.latest_attention_decision(session["id"]).id == first_decision.id
    inbox = [event for event in store.events(session["id"], after=0, limit=100) if event.source_event_id == "source-1"]
    assert len(inbox) == 1
    assert inbox[0].source_namespace == "device:device-1:boot-1"

    changed = {**request, "payload": {**request["payload"], "summary": "changed"}}
    with pytest.raises(SessionIdempotencyConflict):
        router.ingest(session["id"], **changed)


def test_waiting_capability_reuses_waiting_device_with_precise_reason(tmp_path):
    """R4 does not manufacture a parallel Goal status for capability gaps."""

    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = AgentSessionService(
        store,
        PreparedGoalService(),
        attention_scheduler=AttentionScheduler(),
    )
    session = service.create("输入中文", "waiting-capability-session")
    goal_id = session["goal_nodes"][0]["id"]
    event = SessionEventRouter(store).ingest(
        session["id"],
        source_namespace="kernel:capability",
        source_event_id="unicode-unavailable-1",
        event_type=SessionEventType.GOAL_STATE_CHANGED,
        occurred_at="2026-08-24T01:00:00Z",
        payload={
            "goal_id": goal_id,
            "status": "WAITING_DEVICE",
            "waiting": {
                "waiting_kind": "WAITING_CAPABILITY",
                "matcher": {
                    "event_type": "BodyEvent",
                    "capability": "action.input_text_unicode",
                    "status": "READY",
                },
            },
        },
    )

    service.handle_inbox_event(event.event.id, dispatch=False)

    node = next(item for item in store.goal_nodes(session["id"]) if item.id == goal_id)
    continuation = store.latest_continuation(goal_id)
    wake = next(item for item in store.wake_conditions(session["id"]) if item.goal_id == goal_id)
    assert node.status is GoalNodeStatus.WAITING_DEVICE
    assert node.waiting_kind == "WAITING_CAPABILITY"
    assert continuation is not None
    assert continuation.waiting_kind == "WAITING_CAPABILITY"
    assert wake.kind is WakeConditionKind.DEVICE


def test_body_event_projects_through_event_inbox(tmp_path):
    """R4 BodyEvent has no parallel scheduler/event journal."""

    database = tmp_path / "agent-runtime.db"
    store = SQLiteAgentRuntimeStore(database)
    session, _ = _session_with_goals(store, count=1)
    body_store = SQLiteDeviceBodyStore(database)
    binding = body_store.create_binding(
        DeviceBodyBinding(
            id="binding-1",
            session_id=session.id,
            device_id="adb:device-1",
            adapter_id="adb-r4-compat",
            device_boot_id="boot-1",
            connection_state=ConnectionState.CONNECTED,
            capability_revision=0,
            event_cursor=0,
            action_cursor=0,
            bound_at="2026-08-24T01:00:00Z",
            updated_at="2026-08-24T01:00:00Z",
        )
    )
    body_event = BodyEvent(
        id="body-event-1",
        binding_id=binding.id,
        device_id=binding.device_id,
        device_boot_id=binding.device_boot_id,
        source_event_id="foreground:boot-1:2",
        source_cursor=2,
        event_type=BodyEventType.FOREGROUND_CHANGED,
        occurred_at="2026-08-24T01:00:01Z",
        received_at="2026-08-24T01:00:01Z",
        foreground_package="com.android.settings",
    )

    inbox = DeviceBodyEventInbox(store, session_id=session.id)
    first = inbox.ingest(body_event)
    replay = inbox.ingest(body_event)

    assert first.created is True
    assert replay.created is False
    assert replay.event.id == first.event.id
    assert first.event.event_type is SessionEventType.BODY_EVENT
    expected_body_facts = {
        "body_event_id": "body-event-1",
        "body_event_type": "FOREGROUND_CHANGED",
        "binding_id": binding.id,
        "source_cursor": 2,
        "caused_by_command_id": None,
        "foreground_package": "com.android.settings",
        "foreground_activity": None,
        "capability_revision": None,
        "connection_state": None,
        "human_presence": None,
        "evidence_ref": None,
    }
    assert {
        key: first.event.data[key] for key in expected_body_facts
    } == expected_body_facts
    assert first.event.data["routing"] == {
        "event_class": "device_system",
        "urgency": "NONE",
        "reason": "no pending WakeCondition matched the device_system event",
        "routes": [],
    }
    persisted = store.events(session.id, after=0, limit=100)
    assert [item.id for item in persisted if item.id == first.event.id] == [first.event.id]


def test_attention_current_application_projects_latest_device_snapshot(tmp_path):
    """A selected Goal's wish is not evidence that the body reached that app."""

    database = tmp_path / "agent-runtime.db"
    store = SQLiteAgentRuntimeStore(database)
    body_store = SQLiteDeviceBodyStore(database)
    service = AgentSessionService(
        store,
        PreparedGoalService(),
        attention_scheduler=AttentionScheduler(),
        device_body_store=body_store,
    )
    session = service.create("打开微信", "snapshot-projection-session")
    binding = body_store.create_binding(
        DeviceBodyBinding(
            id="binding-snapshot",
            session_id=session["id"],
            device_id="adb:device-1",
            adapter_id="adb-r4-compat",
            device_boot_id="boot-1",
            connection_state=ConnectionState.CONNECTED,
            capability_revision=0,
            event_cursor=0,
            action_cursor=0,
            bound_at="2026-08-24T01:00:00Z",
            updated_at="2026-08-24T01:00:00Z",
        )
    )
    snapshot = body_store.persist_snapshot(
        DeviceSnapshot(
            id="snapshot-settings",
            binding_id=binding.id,
            device_id=binding.device_id,
            device_boot_id=binding.device_boot_id,
            capture_request_id="capture-settings",
            sequence=1,
            foreground_package="com.android.settings",
            foreground_activity=".Settings",
            screen_on=True,
            locked=False,
            network_state=NetworkState.UNKNOWN,
            orientation=Orientation.PORTRAIT,
            human_presence=HumanPresenceState.ABSENT,
            requested_at="2026-08-24T01:00:01Z",
            capture_started_at="2026-08-24T01:00:01Z",
            capture_completed_at="2026-08-24T01:00:02Z",
            received_at="2026-08-24T01:00:02Z",
            observed_at="2026-08-24T01:00:02Z",
        )
    )

    projected = service.inspect(session["id"])

    assert projected["current_application"] == "com.android.settings"
    assert projected["latest_device_snapshot"]["id"] == snapshot.id
    assert projected["current_application"] != "com.tencent.mm"
