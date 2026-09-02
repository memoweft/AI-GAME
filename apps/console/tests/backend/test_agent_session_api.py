from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_game_console.api import _resolve_kernel_task_session_owner, create_app
from ai_game_console.agent_runtime.api import (
    agent_runtime_error_handler,
    create_agent_session_router,
)
from ai_game_console.agent_runtime.domain import (
    AgentRuntimeError,
    AttentionDecisionDraft,
    AttentionDecisionOutcome,
    AttentionSelectorKind,
    GoalEligibilityDraft,
    GoalEligibilityStatus,
    GoalNodeStatus,
)
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.device_body.domain import (
    CapabilityKind,
    CapabilityState,
    ConnectionState,
    DeviceBodyBinding,
    DeviceBodyCapability,
)
from ai_game_console.device_body.store import SQLiteDeviceBodyStore

from conftest import WRITE_HEADERS, build_settings
from test_agent_session_store import FakeGoalService
from test_goal_api import FakeMobileRuntime


def _client(tmp_path):
    service = AgentSessionService(
        SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"), FakeGoalService()
    )
    app = FastAPI()
    app.add_exception_handler(AgentRuntimeError, agent_runtime_error_handler)
    app.include_router(create_agent_session_router(service))
    return TestClient(app)


def test_v3_session_api_create_list_detail_events_and_validation(tmp_path):
    with _client(tmp_path) as client:
        created = client.post(
            "/api/v3/sessions",
            json={"instruction": "打开设置查看电池", "client_request_id": "create-1"},
        )
        assert created.status_code == 202
        session_id = created.json()["id"]
        assert client.get("/api/v3/sessions").json()["count"] == 1
        assert client.get(f"/api/v3/sessions/{session_id}").json()["id"] == session_id
        events = client.get(f"/api/v3/sessions/{session_id}/events?after=0").json()
        assert events["next_cursor"] >= 4
        assert client.post(
            "/api/v3/sessions",
            json={"instruction": "x", "client_request_id": "contains whitespace"},
        ).status_code == 422


def test_v3_session_api_message_and_control_are_idempotent(tmp_path):
    with _client(tmp_path) as client:
        created = client.post(
            "/api/v3/sessions",
            json={"instruction": "打开设置查看电池", "client_request_id": "create-1"},
        ).json()
        session_id = created["id"]
        message = {"content": "完成后回到桌面", "client_request_id": "message-1"}
        assert client.post(f"/api/v3/sessions/{session_id}/messages", json=message).status_code == 202
        assert client.post(f"/api/v3/sessions/{session_id}/messages", json=message).status_code == 202
        control = {"action": "stop", "client_request_id": "stop-1"}
        assert client.post(f"/api/v3/sessions/{session_id}/controls", json=control).status_code == 202
        assert client.post(f"/api/v3/sessions/{session_id}/controls", json=control).json()["status"] == "STOPPED"


def test_v3_session_event_intake_is_server_routed_idempotent_and_conflict_safe(tmp_path):
    with _client(tmp_path) as client:
        session_id = client.post(
            "/api/v3/sessions",
            json={"instruction": "等待微信消息", "client_request_id": "create-events"},
        ).json()["id"]
        request = {
            "source_namespace": "acceptance:r3",
            "source_event_id": "notification-1",
            "event_type": "NotificationPostedEvent",
            "occurred_at": "2026-08-24T01:00:00Z",
            "payload": {
                "application_package": "com.tencent.mm",
                "conversation_hint": "contact-A",
                # Caller input is data, never the authoritative wake result.
                "affected_goal_ids": ["forged-goal-id"],
            },
        }
        first = client.post(f"/api/v3/sessions/{session_id}/events", json=request)
        replay = client.post(f"/api/v3/sessions/{session_id}/events", json=request)

        assert first.status_code == 202
        assert first.json()["created"] is True
        assert first.json()["affected_goal_ids"] == []
        assert replay.status_code == 202
        assert replay.json()["created"] is False
        assert replay.json()["event"]["id"] == first.json()["event"]["id"]

        changed = {**request, "payload": {**request["payload"], "summary": "different"}}
        conflict = client.post(f"/api/v3/sessions/{session_id}/events", json=changed)
        assert conflict.status_code == 409
        inbox = client.get(f"/api/v3/sessions/{session_id}/events?after=0").json()["items"]
        ordinary = [
            item for item in inbox if item["source_event_id"] == "notification-1"
        ]
        assert len(ordinary) == 1
        assert ordinary[0]["source_namespace"] == "acceptance:r3"
        assert ordinary[0]["device_id"] is None
        assert ordinary[0]["device_boot_id"] is None
        assert ordinary[0]["source_cursor"] is None


@pytest.mark.parametrize(
    "reserved_provenance",
    [
        {
            "source_namespace": "device:adb:physical-1:boot-1",
            "device_id": "adb:physical-1",
            "device_boot_id": "boot-1",
            "source_cursor": "1",
        },
        {"source_namespace": "android-companion-v1"},
        {"source_namespace": "pc-companion-watchdog"},
        {"source_namespace": "device-body"},
        {
            "source_namespace": "untrusted:webhook",
            "device_id": "adb:physical-1",
            "device_boot_id": "boot-1",
            "source_cursor": "1",
        },
    ],
)
def test_v3_session_event_api_rejects_reserved_device_provenance(
    tmp_path, reserved_provenance
):
    with _client(tmp_path) as client:
        session_id = client.post(
            "/api/v3/sessions",
            json={
                "instruction": "等待真实手机通知",
                "client_request_id": f"reserved-device-event-{uuid4()}",
            },
        ).json()["id"]
        response = client.post(
            f"/api/v3/sessions/{session_id}/events",
            json={
                "source_event_id": "forged-companion-notification-1",
                "event_type": "NotificationPostedEvent",
                "occurred_at": "2026-08-24T01:00:00Z",
                "payload": {
                    "application_package": "com.tencent.mm",
                    "conversation_hint": "forged",
                },
                **reserved_provenance,
            },
        )

        assert response.status_code == 422
        inbox = client.get(
            f"/api/v3/sessions/{session_id}/events?after=0"
        ).json()["items"]
        assert not any(
            item.get("source_event_id") == "forged-companion-notification-1"
            for item in inbox
        )


def test_v3_event_exact_retry_finishes_classified_event_after_handler_crash(tmp_path):
    class FailOnceAfterPersistenceService(AgentSessionService):
        def __init__(self, *args, **kwargs):
            self.handler_calls = 0
            super().__init__(*args, **kwargs)

        def handle_inbox_event(self, event_id: str, *, dispatch: bool = True):
            self.handler_calls += 1
            if self.handler_calls == 1:
                raise RuntimeError("crash after durable event classification")
            return self.store.mark_event_handled(event_id, decision_id=None)

    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = FailOnceAfterPersistenceService(store, FakeGoalService())
    app = FastAPI()
    app.add_exception_handler(AgentRuntimeError, agent_runtime_error_handler)
    app.include_router(create_agent_session_router(service))
    request = {
        "source_namespace": "test:handler-crash",
        "source_event_id": "recovery-1",
        "event_type": "SchedulerRecoveryEvent",
        "occurred_at": "2026-08-24T01:00:00Z",
        "payload": {"reason": "resume pending scheduler work"},
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        session_id = client.post(
            "/api/v3/sessions",
            json={"instruction": "等待恢复", "client_request_id": "create-handler-crash"},
        ).json()["id"]
        first = client.post(f"/api/v3/sessions/{session_id}/events", json=request)
        assert first.status_code == 500
        persisted = [
            item
            for item in store.events(session_id, after=0, limit=100)
            if item.source_event_id == "recovery-1"
        ]
        assert len(persisted) == 1
        assert persisted[0].handling_status.value == "CLASSIFIED"

        retry = client.post(f"/api/v3/sessions/{session_id}/events", json=request)
        terminal_replay = client.post(f"/api/v3/sessions/{session_id}/events", json=request)
        assert retry.status_code == 202
        assert retry.json()["created"] is False
        assert retry.json()["event"]["handling_status"] == "HANDLED"
        assert terminal_replay.status_code == 202
        assert terminal_replay.json()["event"]["id"] == retry.json()["event"]["id"]
        assert service.handler_calls == 2
        assert store.latest_attention_decision(session_id) is None
        assert len([
            item
            for item in store.events(session_id, after=0, limit=100)
            if item.source_event_id == "recovery-1"
        ]) == 1

        changed = {**request, "payload": {"reason": "changed payload"}}
        conflict = client.post(f"/api/v3/sessions/{session_id}/events", json=changed)
        assert conflict.status_code == 409


def test_v3_event_rejects_naive_or_non_utc_occurred_at(tmp_path):
    with _client(tmp_path) as client:
        session_id = client.post(
            "/api/v3/sessions",
            json={"instruction": "等待事件", "client_request_id": "create-event-time"},
        ).json()["id"]
        baseline = {
            "source_namespace": "test:event-time",
            "source_event_id": "event-time",
            "event_type": "SchedulerRecoveryEvent",
            "payload": {},
        }
        for occurred_at in (
            "2026-08-24T01:00:00",
            "2026-08-24T09:00:00+08:00",
            "not-a-timestamp",
        ):
            response = client.post(
                f"/api/v3/sessions/{session_id}/events",
                json={**baseline, "occurred_at": occurred_at},
            )
            assert response.status_code == 422
        accepted = client.post(
            f"/api/v3/sessions/{session_id}/events",
            json={**baseline, "occurred_at": "2026-08-24T01:00:00+00:00"},
        )
        assert accepted.status_code == 202
        assert accepted.json()["event"]["occurred_at"] == "2026-08-24T01:00:00Z"


def test_v3_event_response_includes_linked_durable_attention_decision(tmp_path):
    class DecisionLinkService(AgentSessionService):
        decision_id: str | None = None
        handler_calls = 0

        def handle_inbox_event(self, event_id: str, *, dispatch: bool = True):
            self.handler_calls += 1
            assert self.decision_id is not None
            return self.store.mark_event_handled(
                event_id, decision_id=self.decision_id
            )

    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = DecisionLinkService(store, FakeGoalService())
    app = FastAPI()
    app.add_exception_handler(AgentRuntimeError, agent_runtime_error_handler)
    app.include_router(create_agent_session_router(service))
    with TestClient(app) as client:
        session_id = client.post(
            "/api/v3/sessions",
            json={"instruction": "选择一个目标", "client_request_id": "create-decision-link"},
        ).json()["id"]
        goal_id = store.goal_nodes(session_id)[0].id
        store.set_goal_ready(session_id, goal_id)
        session = store.get_session(session_id)
        graph = store.graph_revision(session_id)
        assert graph is not None
        decision, _, _ = store.commit_attention_decision(
            session_id,
            draft=AttentionDecisionDraft(
                trigger_key="test:linked-decision",
                authority_revision=session.authority_revision,
                graph_revision=graph.revision,
                event_cursor=session.event_cursor,
                outcome=AttentionDecisionOutcome.SELECTED,
                selected_goal_id=goal_id,
                selector_kind=AttentionSelectorKind.DETERMINISTIC,
                reason="persisted before event response",
            ),
            candidates=[
                GoalEligibilityDraft(
                    goal_id=goal_id,
                    eligibility=GoalEligibilityStatus.ELIGIBLE,
                    reason="ready",
                    goal_status=GoalNodeStatus.READY,
                    rank=1,
                )
            ],
        )
        service.decision_id = decision.id
        request = {
            "source_namespace": "test:decision-link",
            "source_event_id": "event-1",
            "event_type": "SchedulerRecoveryEvent",
            "occurred_at": "2026-08-24T01:00:00Z",
            "payload": {},
        }
        first = client.post(f"/api/v3/sessions/{session_id}/events", json=request)
        replay = client.post(f"/api/v3/sessions/{session_id}/events", json=request)

        assert first.status_code == 202
        assert first.json()["event"]["decision_id"] == decision.id
        assert first.json()["attention_decision"]["id"] == decision.id
        assert first.json()["attention_decision"]["reason"] == decision.reason
        assert replay.json()["created"] is False
        assert replay.json()["attention_decision"]["id"] == decision.id
        assert service.handler_calls == 1


def test_v2_goal_api_remains_compatible(tmp_path):
    """Normal composition serves both the protected v3 and legacy v2 APIs."""

    settings = build_settings(tmp_path)
    runtime = FakeMobileRuntime()
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=runtime,
    )
    with TestClient(app) as client:
        denied = client.post(
            "/api/v3/sessions",
            json={"instruction": "查看电池", "client_request_id": "session-1"},
        )
        assert denied.status_code == 403
        created = client.post(
            "/api/v3/sessions", headers=WRITE_HEADERS,
            json={"instruction": "查看电池", "client_request_id": "session-1"},
        )
        assert created.status_code == 202
        session_id = created.json()["id"]
        assert client.get("/api/v3/sessions").json()["count"] == 1
        assert client.get(f"/api/v3/sessions/{session_id}").status_code == 200
        assert client.get(f"/api/v3/sessions/{session_id}/events?after=0").status_code == 200
        goal = {"goal": "查看电池", "idempotency_key": "v2-goal-1"}
        v2_created = client.post("/api/v2/goals", headers=WRITE_HEADERS, json=goal)
        v2_replay = client.post("/api/v2/goals", headers=WRITE_HEADERS, json=goal)
        assert v2_created.status_code == 202
        assert v2_replay.status_code == 202
        goal_id = v2_created.json()["id"]
        assert v2_replay.json()["id"] == goal_id
        assert client.get(f"/api/v2/goals/{goal_id}").status_code == 200
        assert client.get(f"/api/v2/goals/{goal_id}/events?after=0").status_code == 200


def test_normal_create_app_composes_kernel_device_body_seam(tmp_path):
    """Kernel-active composition wires one shared Body store and EventInbox.

    The test deliberately does not open a TestClient or dispatch an action:
    the fake ADB path proves construction itself performs no device I/O.
    """

    settings = replace(
        build_settings(tmp_path),
        runtime_mode="kernel_active",
        gui_executor_enabled=True,
        adb_path=str(tmp_path / "fake-adb"),
        local_chat_endpoint="http://127.0.0.1:18080/v1/chat/completions",
        local_chat_model="fake-role-model",
    )
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=FakeMobileRuntime(),
    )

    bridge = app.state.device_body_bridge
    coordinator = app.state.kernel_canary
    assert bridge is not None
    assert coordinator is not None
    assert coordinator.kernel._body_dispatcher is bridge
    assert bridge._store is app.state.device_body_store
    assert bridge._event_inbox._target is app.state.device_body_event_inbox
    assert app.state.device_body_adapter is not None


@pytest.mark.parametrize("binding_kind", ["runtime_kernel", "runtime_kernel_canary"])
def test_kernel_device_body_owner_requires_exact_bound_task_id(binding_kind: str) -> None:
    """A matching ``goal:`` source cannot borrow another Kernel Task's owner."""

    session = SimpleNamespace(id="session-1")
    binding = SimpleNamespace(goal_run_id="goal-run-1")
    goal_store = SimpleNamespace(
        inspect=lambda goal_run_id: SimpleNamespace(
            id=goal_run_id,
            bound_task_id="kernel-task-bound",
            binding_kind=binding_kind,
        )
    )
    agent_store = SimpleNamespace(
        sessions_for_recovery=lambda: [session],
        bindings=lambda session_id: [binding] if session_id == session.id else [],
    )
    legal_task = SimpleNamespace(
        id="kernel-task-bound",
        source=SimpleNamespace(conversation_id="goal:goal-run-1"),
    )

    assert _resolve_kernel_task_session_owner(
        task=legal_task,
        goal_store=goal_store,
        agent_runtime_store=agent_store,
    ) is session

    forged_second_task = SimpleNamespace(
        id="kernel-task-forged",
        source=SimpleNamespace(conversation_id="goal:goal-run-1"),
    )
    with pytest.raises(RuntimeError, match="not the GoalRun's bound task"):
        _resolve_kernel_task_session_owner(
            task=forged_second_task,
            goal_store=goal_store,
            agent_runtime_store=agent_store,
        )


def _long_lived_cycle_owner_facts(*, cycle_key: str = "cycle-key-1", cycle: int = 7):
    """Return isolated durable facts for the long-lived child authorization seam."""

    session = SimpleNamespace(id="session-1")
    binding = SimpleNamespace(goal_run_id="goal-run-1")
    goal_run = SimpleNamespace(
        id="goal-run-1",
        bound_task_id="application-instance-1",
        binding_kind="long_lived_mobile_composition",
    )
    goal_store = SimpleNamespace(inspect=lambda _goal_run_id: goal_run)
    agent_store = SimpleNamespace(
        sessions_for_recovery=lambda: [session],
        bindings=lambda session_id: [binding] if session_id == session.id else [],
    )
    task = SimpleNamespace(
        id="kernel-cycle-task-1",
        device_id="adb:device-1",
        source=SimpleNamespace(
            client_id="goal-v2-long-lived-mobile",
            conversation_id="goal:goal-run-1",
            initial_message_id=cycle_key,
        ),
    )
    authorization = SimpleNamespace(
        instance_id="application-instance-1",
        goal_id="goal-run-1",
        target_id="adb:device-1",
        activated_at="2026-08-25T00:00:00Z",
        application_cycle=cycle,
        intent_phase="dispatching",
        reservation_id=cycle_key,
    )
    event = SimpleNamespace(
        type="KernelApplicationCycleAccepted",
        payload={
            "goal_id": "goal-run-1",
            "application_instance_id": "application-instance-1",
            "application_cycle": cycle,
            "cycle_key": cycle_key,
            "target_id": "adb:device-1",
        },
    )
    runtime = SimpleNamespace(
        durable_child_authorization=lambda instance_id: (
            authorization if instance_id == "application-instance-1" else None
        )
    )
    kernel = SimpleNamespace(events=lambda task_id: [event] if task_id == task.id else [])
    return SimpleNamespace(
        session=session,
        goal_run=goal_run,
        goal_store=goal_store,
        agent_store=agent_store,
        task=task,
        authorization=authorization,
        event=event,
        runtime=runtime,
        kernel=kernel,
    )


def test_kernel_device_body_owner_accepts_only_active_long_lived_cycle_child() -> None:
    facts = _long_lived_cycle_owner_facts()

    assert _resolve_kernel_task_session_owner(
        task=facts.task,
        goal_store=facts.goal_store,
        agent_runtime_store=facts.agent_store,
        long_lived_mobile_runtime=facts.runtime,
        runtime_kernel=facts.kernel,
    ) is facts.session


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("instance", "dispatching long-lived child"),
        ("reservation", "dispatching long-lived child"),
        ("cycle", "accepted-cycle event does not match"),
        ("target", "dispatching long-lived child"),
        ("client", "not a durable long-lived mobile cycle"),
        ("event", "exactly one accepted long-lived cycle event"),
    ],
)
def test_kernel_device_body_owner_rejects_forged_long_lived_cycle_facts(
    tamper: str, error: str
) -> None:
    facts = _long_lived_cycle_owner_facts()
    if tamper == "instance":
        facts.authorization.instance_id = "application-instance-forged"
    elif tamper == "reservation":
        facts.authorization.reservation_id = "cycle-key-forged"
    elif tamper == "cycle":
        facts.event.payload["application_cycle"] = 99
    elif tamper == "target":
        facts.authorization.target_id = "adb:device-forged"
    elif tamper == "client":
        facts.task.source.client_id = "goal-v2-forged"
    elif tamper == "event":
        facts.kernel.events = lambda _task_id: []
    else:  # pragma: no cover - parameter table is exhaustive
        raise AssertionError(tamper)

    with pytest.raises(RuntimeError, match=error):
        _resolve_kernel_task_session_owner(
            task=facts.task,
            goal_store=facts.goal_store,
            agent_runtime_store=facts.agent_store,
            long_lived_mobile_runtime=facts.runtime,
            runtime_kernel=facts.kernel,
        )


def test_kernel_device_body_owner_accepts_successive_durable_long_lived_cycles() -> None:
    """A GoalRun may dispatch another cycle, but only after the authorization moves."""

    for cycle_key, cycle in (("cycle-key-1", 7), ("cycle-key-2", 8)):
        facts = _long_lived_cycle_owner_facts(cycle_key=cycle_key, cycle=cycle)
        assert _resolve_kernel_task_session_owner(
            task=facts.task,
            goal_store=facts.goal_store,
            agent_runtime_store=facts.agent_store,
            long_lived_mobile_runtime=facts.runtime,
            runtime_kernel=facts.kernel,
        ) is facts.session


def test_session_projection_exposes_current_device_capability_revision(tmp_path):
    """Unicode transport/read-back readiness is a visible Session fact."""

    database = tmp_path / "agent-runtime.db"
    store = SQLiteAgentRuntimeStore(database)
    body_store = SQLiteDeviceBodyStore(database)
    service = AgentSessionService(
        store, FakeGoalService(), device_body_store=body_store
    )
    created = service.create("等待可验证的中文输入", "capability-session-1")
    now = "2026-08-24T12:00:00Z"
    binding = body_store.create_binding(
        DeviceBodyBinding(
            id=str(uuid4()),
            session_id=created["id"],
            device_id="adb:127.0.0.1:16384",
            adapter_id="adb-r4-compat",
            device_boot_id="boot-capability-1",
            connection_state=ConnectionState.CONNECTED,
            capability_revision=0,
            event_cursor=0,
            action_cursor=0,
            bound_at=now,
            updated_at=now,
        )
    )
    capabilities = tuple(
        DeviceBodyCapability(
            id=f"capability-{kind.value}",
            binding_id=binding.id,
            device_id=binding.device_id,
            device_boot_id=binding.device_boot_id,
            revision=1,
            kind=kind,
            state=(
                CapabilityState.UNSUPPORTED
                if kind is CapabilityKind.TEXT_READ_BACK
                else CapabilityState.READY
            ),
            reason_code=(
                "adb_text_read_back_unsupported"
                if kind is CapabilityKind.TEXT_READ_BACK
                else None
            ),
            evidence_ref=(
                None
                if kind is CapabilityKind.TEXT_READ_BACK
                else "test:capability"
            ),
            observed_at=now,
        )
        for kind in CapabilityKind
    )
    body_store.replace_capability_revision(binding.id, capabilities)

    projection = service.inspect(created["id"])

    assert projection["device_body_binding"]["id"] == binding.id
    projected = {
        item["kind"]: item for item in projection["device_capabilities"]
    }
    assert projected["TEXT_READ_BACK"]["state"] == "UNSUPPORTED"
    assert (
        projected["TEXT_READ_BACK"]["reason_code"]
        == "adb_text_read_back_unsupported"
    )
