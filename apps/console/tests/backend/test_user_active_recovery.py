from __future__ import annotations

import sqlite3

import pytest

from ai_game_console.agent_runtime.activity_integration import (
    AgentRuntimeActivitySlicePreemptionCoordinator,
)
from ai_game_console.agent_runtime.domain import (
    PreemptionRequestStatus,
    SessionControlMode,
    SessionEventType,
    SessionStateConflict,
)
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.agent_runtime.preemption import (
    PersistedPreemptionHandoff,
    VerifiedBoundaryPreemptionRequest,
)
from r7_fixtures import (
    build_service,
    create_user_active_runtime,
    enter_user_active_at_verified_checkpoint,
    ingest_event,
)


def _touch_started(runtime, source_event_id: str = "touch-started-1"):
    return ingest_event(
        runtime,
        SessionEventType.HUMAN_TOUCH_STARTED,
        source_event_id,
        occurred_at="2026-08-26T01:00:01Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "foreground_activity": ".ui.LauncherUI",
            "touch_source": "accessibility_touch_interaction_controller",
            "accessibility_ref": "accessibility:event:101",
            "provenance": {
                "source": "android_companion",
                "device_boot_id": "boot-r7",
            },
        },
    )


def _enter_user_active(runtime):
    touch = _touch_started(runtime)
    before_dispatch = runtime.goals.dispatch_count
    before_decision = runtime.store.latest_attention_decision(runtime.session_id)

    runtime.service.handle_inbox_event(touch.event.id)

    requests = runtime.store.pending_preemption_requests(runtime.session_id)
    assert runtime.goals.dispatch_count == before_dispatch
    assert len(requests) == 1
    assert requests[0].event_id == touch.event.id
    assert runtime.session.control_mode is SessionControlMode.AGENT_ACTIVE
    assert runtime.store.latest_attention_decision(runtime.session_id) == before_decision
    settled, transitioned, continuation = enter_user_active_at_verified_checkpoint(
        runtime,
        request=requests[0],
    )
    assert settled.checkpoint_ref == "verified-step:1"
    assert settled.continuation_id == continuation.id
    assert transitioned.to_mode is SessionControlMode.USER_ACTIVE
    return touch, settled, continuation


def test_user_touch_enters_user_active_after_current_action_settles(tmp_path) -> None:
    runtime = create_user_active_runtime(tmp_path)
    touch = _touch_started(runtime)
    before_dispatch = runtime.goals.dispatch_count

    runtime.service.handle_inbox_event(touch.event.id)

    requests = runtime.store.pending_preemption_requests(runtime.session_id)
    assert len(requests) == 1
    request = requests[0]
    assert request.event_id == touch.event.id
    assert request.status is PreemptionRequestStatus.PENDING_CHECKPOINT
    assert request.checkpoint_ref is None
    assert runtime.session.control_mode is SessionControlMode.AGENT_ACTIVE
    assert runtime.goals.dispatch_count == before_dispatch

    settled, transitioned, continuation = enter_user_active_at_verified_checkpoint(
        runtime,
        request=request,
    )

    assert settled.continuation_id == continuation.id
    assert settled.checkpoint_ref == continuation.checkpoint_ref
    assert transitioned.to_mode is SessionControlMode.USER_ACTIVE


def test_recovered_handoff_reuses_existing_touch_preemption_request(tmp_path) -> None:
    runtime = create_user_active_runtime(tmp_path)
    touch = _touch_started(runtime)
    runtime.service.handle_inbox_event(touch.event.id)
    original = runtime.store.pending_preemption_requests(runtime.session_id)[0]
    decision = runtime.store.latest_attention_decision(runtime.session_id)
    assert decision is not None
    key = "activity-slice:r7-user-active:preempt:event:1:checkpoint:1"
    handoff = PersistedPreemptionHandoff(
        request=VerifiedBoundaryPreemptionRequest(
            id="handoff-1",
            idempotency_key=key,
            continuation_key=f"{key}:continuation",
            session_id=runtime.session_id,
            current_goal_id=runtime.goal_id,
            activity_slice_id=runtime.slice_id,
            attention_decision_id=decision.id,
            event_cursor=runtime.store.get_session(runtime.session_id).event_cursor,
            last_settled_step_id="step-1",
            resume_step_ordinal=2,
            checkpoint_ref="verified-step:1",
        ),
        continuation_idempotency_key=f"{key}:continuation",
        persisted_at="2026-08-26T01:00:02Z",
    )
    coordinator = AgentRuntimeActivitySlicePreemptionCoordinator(
        runtime.store, event_handler=runtime.service.handle_inbox_event
    )

    coordinator.settle_preemption_checkpoint(handoff)

    settled = runtime.store.pending_preemption_requests(runtime.session_id)[0]
    assert settled.id == original.id
    assert settled.reason == original.reason
    assert settled.status is PreemptionRequestStatus.CHECKPOINTED
    assert runtime.store.get_session(runtime.session_id).control_mode is SessionControlMode.USER_ACTIVE


def test_duplicate_touch_and_observation_have_one_durable_effect(tmp_path) -> None:
    runtime = create_user_active_runtime(tmp_path)
    first = _touch_started(runtime, "touch-duplicate")
    replay = _touch_started(runtime, "touch-duplicate")
    runtime.service.handle_inbox_event(first.event.id)
    runtime.service.handle_inbox_event(replay.event.id)

    requests = runtime.store.pending_preemption_requests(runtime.session_id)
    assert first.created is True
    assert replay.created is False
    assert replay.event.id == first.event.id
    assert len(requests) == 1
    enter_user_active_at_verified_checkpoint(runtime, request=requests[0])

    observation = ingest_event(
        runtime,
        SessionEventType.FOREGROUND_APPLICATION_CHANGED,
        "foreground-duplicate",
        occurred_at="2026-08-26T01:00:02Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "foreground_activity": ".ui.LauncherUI",
            "touch_source": "accessibility",
            "accessibility_ref": "accessibility:event:102",
            "provenance": {"source": "android_companion"},
        },
    )
    runtime.service.handle_inbox_event(observation.event.id)
    replay_observation = ingest_event(
        runtime,
        SessionEventType.FOREGROUND_APPLICATION_CHANGED,
        "foreground-duplicate",
        occurred_at="2026-08-26T01:00:02Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "foreground_activity": ".ui.LauncherUI",
            "touch_source": "accessibility",
            "accessibility_ref": "accessibility:event:102",
            "provenance": {"source": "android_companion"},
        },
    )
    runtime.service.handle_inbox_event(replay_observation.event.id)

    observations = runtime.store.raw_observations(runtime.session_id)
    matching = [item for item in observations if item.event_id == observation.event.id]
    assert len(matching) == 1


def test_user_active_records_passive_capability_event_without_dispatch(tmp_path) -> None:
    runtime = create_user_active_runtime(tmp_path)
    _enter_user_active(runtime)
    before_decision = runtime.store.latest_attention_decision(runtime.session_id)
    capability = ingest_event(
        runtime,
        SessionEventType.CAPABILITIES_CHANGED,
        "capabilities-user-active",
        occurred_at="2026-08-26T01:00:03Z",
        payload={
            "capabilities": {"gesture_dispatch": "READY"},
            "provenance": {"source": "android_companion"},
        },
    )

    handled = runtime.service.handle_inbox_event(capability.event.id)

    assert handled.handling_status.value == "HANDLED"
    assert runtime.store.latest_attention_decision(runtime.session_id) == before_decision
    assert runtime.store.get_session(runtime.session_id).control_mode is SessionControlMode.USER_ACTIVE


def test_user_active_records_observations_without_new_agent_action(tmp_path) -> None:
    runtime = create_user_active_runtime(tmp_path)
    _enter_user_active(runtime)
    before_dispatch = runtime.goals.dispatch_count
    before_decision = runtime.store.latest_attention_decision(runtime.session_id)
    before_raw_count = len(runtime.store.raw_observations(runtime.session_id))

    foreground = ingest_event(
        runtime,
        SessionEventType.FOREGROUND_APPLICATION_CHANGED,
        "foreground-human-1",
        occurred_at="2026-08-26T01:00:02Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "foreground_activity": ".ui.LauncherUI",
            "touch_source": "accessibility",
            "accessibility_ref": "accessibility:event:102",
            "provenance": {
                "source": "android_companion",
                "source_cursor": "102",
            },
        },
    )
    touch_ended = ingest_event(
        runtime,
        SessionEventType.HUMAN_TOUCH_ENDED,
        "touch-ended-human-1",
        occurred_at="2026-08-26T01:00:03Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "foreground_activity": ".ui.LauncherUI",
            "touch_source": "accessibility_touch_interaction_controller",
            "accessibility_ref": "accessibility:event:103",
            "provenance": {
                "source": "android_companion",
                "source_cursor": "103",
            },
        },
    )
    runtime.service.handle_inbox_event(foreground.event.id)
    runtime.service.handle_inbox_event(touch_ended.event.id)

    observations = runtime.store.raw_observations(runtime.session_id)
    assert len(observations) == before_raw_count + 2
    projected = {item.event_id: item for item in observations}
    assert projected[foreground.event.id].occurred_at == "2026-08-26T01:00:02Z"
    assert projected[foreground.event.id].foreground_package == "com.tencent.mm"
    assert projected[touch_ended.event.id].payload["touch_source"] == (
        "accessibility_touch_interaction_controller"
    )
    assert projected[touch_ended.event.id].payload["accessibility_ref"] == (
        "accessibility:event:103"
    )
    assert projected[touch_ended.event.id].provenance["source_namespace"] == (
        "device:android:test-device:boot-r7"
    )
    assert runtime.goals.dispatch_count == before_dispatch
    assert runtime.store.latest_attention_decision(runtime.session_id) == before_decision
    assert runtime.session.control_mode is SessionControlMode.USER_ACTIVE


def test_user_active_does_not_create_human_demonstration_in_r7(tmp_path) -> None:
    runtime = create_user_active_runtime(tmp_path)
    _enter_user_active(runtime)
    observation = ingest_event(
        runtime,
        SessionEventType.FOREGROUND_APPLICATION_CHANGED,
        "raw-only-1",
        occurred_at="2026-08-26T01:00:04Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "foreground_activity": ".ui.ChattingUI",
            "accessibility_ref": "accessibility:event:104",
            "provenance": {"source": "android_companion"},
        },
    )
    runtime.service.handle_inbox_event(observation.event.id)

    raw = runtime.store.raw_observations(runtime.session_id)
    assert any(item.event_id == observation.event.id for item in raw)
    with sqlite3.connect(runtime.database) as connection:
        tables = {
            str(row[0]).casefold()
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert not any("human_demonstration" in name for name in tables)
    assert not any(name == "experiences" or name.startswith("experience_") for name in tables)


def test_human_idle_enters_recovering_context_then_resumes(
    tmp_path,
) -> None:
    runtime = create_user_active_runtime(tmp_path)
    _enter_user_active(runtime)
    previous_decision = runtime.store.latest_attention_decision(runtime.session_id)
    before_dispatch = runtime.goals.dispatch_count
    trace_start = len(runtime.trace)
    idle = ingest_event(
        runtime,
        SessionEventType.HUMAN_IDLE,
        "human-idle-1",
        occurred_at="2026-08-26T01:00:10Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "foreground_activity": ".ui.ChattingUI",
            "touch_source": "idle_detector",
            "provenance": {"source": "android_companion", "idle_ms": 3000},
        },
    )

    runtime.service.handle_inbox_event(idle.event.id)

    resumed = runtime.session
    latest_decision = runtime.store.latest_attention_decision(runtime.session_id)
    idle_raw = [
        item
        for item in runtime.store.raw_observations(runtime.session_id)
        if item.event_id == idle.event.id
    ]
    assert len(runtime.recovery.calls) == 1
    assert len(idle_raw) == 1
    assert latest_decision is not None
    assert previous_decision is not None
    assert latest_decision.id != previous_decision.id
    assert latest_decision.trigger_event_id == idle.event.id
    assert resumed.control_mode is SessionControlMode.AGENT_ACTIVE
    assert resumed.active_goal_id == latest_decision.selected_goal_id
    assert runtime.goals.dispatch_count == before_dispatch + 1
    recovery_trace = runtime.trace[trace_start:]
    assert recovery_trace.index("context:fresh_snapshot") < (
        recovery_trace.index("goal:activate_selected")
    )


def test_recovering_context_cannot_become_agent_active_without_fresh_snapshot(
    tmp_path,
) -> None:
    runtime = create_user_active_runtime(tmp_path)
    _enter_user_active(runtime)
    idle = ingest_event(
        runtime,
        SessionEventType.HUMAN_IDLE,
        "human-idle-missing-fresh",
        occurred_at="2026-08-26T01:00:10Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "touch_source": "idle_detector",
            "provenance": {"source": "android_companion", "idle_ms": 3000},
        },
    )
    recovering, created = runtime.store.transition_control_mode(
        runtime.session_id,
        event_id=idle.event.id,
        to_mode=SessionControlMode.RECOVERING_CONTEXT,
        reason="human idle starts context recovery",
        idempotency_key=f"event:{idle.event.id}:recovering-context",
        expected_from=frozenset({SessionControlMode.USER_ACTIVE}),
    )
    assert created is True
    assert recovering.to_mode is SessionControlMode.RECOVERING_CONTEXT

    with pytest.raises(SessionStateConflict, match="fresh"):
        runtime.store.transition_control_mode(
            runtime.session_id,
            event_id=idle.event.id,
            to_mode=SessionControlMode.AGENT_ACTIVE,
            reason="resume after context recovery",
            idempotency_key=f"event:{idle.event.id}:agent-active-without-fresh",
            expected_from=frozenset({SessionControlMode.RECOVERING_CONTEXT}),
        )


def test_explicit_takeover_requires_explicit_resume(tmp_path) -> None:
    runtime = create_user_active_runtime(tmp_path)
    # This case isolates the durable TAKEOVER control fence after its current
    # atomic boundary; ActivitySlice settlement is covered separately.
    runtime.store.release_active_slice(runtime.session_id, slice_id=runtime.slice_id)
    taken = runtime.service.control(runtime.session_id, "takeover", "takeover-r7")
    previous_decision = runtime.store.latest_attention_decision(runtime.session_id)
    before_dispatch = runtime.goals.dispatch_count
    idle = ingest_event(
        runtime,
        SessionEventType.HUMAN_IDLE,
        "human-idle-takeover",
        occurred_at="2026-08-26T01:00:10Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "touch_source": "idle_detector",
            "provenance": {"source": "android_companion", "idle_ms": 3000},
        },
    )
    runtime.service.handle_inbox_event(idle.event.id)

    assert taken["control_mode"] == SessionControlMode.TAKEOVER.value
    assert runtime.session.control_mode is SessionControlMode.TAKEOVER
    assert runtime.store.latest_attention_decision(runtime.session_id) == previous_decision
    assert runtime.goals.dispatch_count == before_dispatch
    assert runtime.recovery.calls == []
    assert any(
        item.event_id == idle.event.id
        for item in runtime.store.raw_observations(runtime.session_id)
    )

    resumed = runtime.service.control(runtime.session_id, "resume", "resume-r7")
    assert resumed["control_mode"] == SessionControlMode.AGENT_ACTIVE.value
    assert runtime.session.control_mode is SessionControlMode.AGENT_ACTIVE


def test_restart_during_user_active_preserves_mode(
    tmp_path,
) -> None:
    runtime = create_user_active_runtime(tmp_path)
    _enter_user_active(runtime)
    before_dispatch = runtime.goals.dispatch_count
    before_decision = runtime.store.latest_attention_decision(runtime.session_id)
    before_raw = list(runtime.store.raw_observations(runtime.session_id))

    restarted_store = SQLiteAgentRuntimeStore(runtime.database)
    restarted = build_service(
        restarted_store,
        runtime.goals,
        runtime.recovery,
        recover_on_start=True,
    )

    restored = restarted_store.get_session(runtime.session_id)
    assert restored.control_mode is SessionControlMode.USER_ACTIVE
    assert restarted_store.latest_attention_decision(runtime.session_id) == before_decision
    assert restarted_store.raw_observations(runtime.session_id) == before_raw
    assert runtime.goals.dispatch_count == before_dispatch
    assert runtime.recovery.calls == []
    assert restarted.inspect(runtime.session_id)["control_mode"] == (
        SessionControlMode.USER_ACTIVE.value
    )


def test_restart_during_recovering_context_resumes_once_from_fresh_snapshot(
    tmp_path,
) -> None:
    runtime = create_user_active_runtime(tmp_path)
    _enter_user_active(runtime)
    idle, created = runtime.store.ingest_event(
        runtime.session_id,
        source_namespace="android-companion-v1",
        source_event_id="idle-crash-after-recovering-transition",
        event_type=SessionEventType.HUMAN_IDLE,
        occurred_at="2026-08-26T01:00:10Z",
        payload={
            "foreground_package": "com.tencent.mm",
            "touch_source": "idle_detector",
            "provenance": {"source": "android_companion", "idle_ms": 3000},
        },
        device_id="android:test-device",
        device_boot_id="boot-r7",
        source_cursor="idle-restart-1",
    )
    assert created is True
    transition, transitioned = runtime.store.transition_control_mode(
        runtime.session_id,
        event_id=idle.id,
        to_mode=SessionControlMode.RECOVERING_CONTEXT,
        reason="human idle persisted before process restart",
        idempotency_key=f"event:{idle.id}:control:recovering",
        expected_from=frozenset({SessionControlMode.USER_ACTIVE}),
    )
    assert transitioned is True
    assert transition.to_mode is SessionControlMode.RECOVERING_CONTEXT
    previous_decision = runtime.store.latest_attention_decision(runtime.session_id)
    before_dispatch = runtime.goals.dispatch_count

    restarted_store = SQLiteAgentRuntimeStore(runtime.database)
    restarted = build_service(
        restarted_store,
        runtime.goals,
        runtime.recovery,
        recover_on_start=True,
    )

    recovered = restarted_store.get_session(runtime.session_id)
    recovered_decision = restarted_store.latest_attention_decision(runtime.session_id)
    assert recovered.control_mode is SessionControlMode.AGENT_ACTIVE
    assert recovered_decision is not None and previous_decision is not None
    assert recovered_decision.id != previous_decision.id
    assert recovered_decision.trigger_event_id == idle.id
    assert runtime.recovery.calls != [] and len(runtime.recovery.calls) == 1
    assert runtime.goals.dispatch_count == before_dispatch + 1
    assert any(
        item.event_id == idle.id
        for item in restarted_store.raw_observations(runtime.session_id)
    )

    # A second launcher recovery sees an already handled idle event and the
    # stable decision/transition keys, so it must not observe or dispatch twice.
    build_service(
        SQLiteAgentRuntimeStore(runtime.database),
        runtime.goals,
        runtime.recovery,
        recover_on_start=True,
    )
    assert len(runtime.recovery.calls) == 1
    assert runtime.goals.dispatch_count == before_dispatch + 1
