from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.agent_runtime.domain import (
    GoalCoverageDraft,
    GoalCoverageKind,
    GoalCriterionDraft,
    GoalEdgeDraft,
    GoalEdgeKind,
    GoalGraphRevisionDraft,
    GoalNodeDraft,
)
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.scheduler import AttentionScheduler
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.agent_runtime.event_router import SessionEventRouter
from ai_game_console.agent_runtime.domain import utc_now
from ai_game_console.application_runtime.store import _SQLiteApplicationStore
from ai_game_console.application_runtime_catalog import ApplicationRuntimeCatalog
from ai_game_console.goal_runtime.service import GoalService
from ai_game_console.goal_runtime.domain import GoalOwnerUnavailable
from ai_game_console.goal_runtime.store import SQLiteGoalStore
from ai_game_console.local_managed_application_composition import (
    LocalManagedApplicationRuntimeGateway,
    PROFILE_ID as LOCAL_PROFILE_ID,
)
from ai_game_console.long_lived_mobile_application_composition import (
    LongLivedMobileApplicationRuntimeGateway,
)

from test_session_planner import PreparedGoalService
from conftest import build_settings


def _future_due_at() -> str:
    """Keep waiting-state tests independent from the wall-clock test date."""
    return (datetime.now(UTC) + timedelta(days=1)).isoformat()


def test_graph_outbox_prepares_all_goals_but_selected_dispatch_activates_one(
    tmp_path,
) -> None:
    goals = PreparedGoalService()
    service = AgentSessionService(
        SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"),
        goals,
        attention_scheduler=AttentionScheduler(),
    )

    created = service.create("投简历；维护微信", "selected-create")

    assert [kind for kind, _ in goals.calls] == ["prepare", "prepare", "activate"]
    assert len(created["bindings"]) == 2
    selected = created["current_goal"]
    assert selected is not None
    selected_node = next(
        item for item in created["goal_nodes"] if item["id"] == selected["id"]
    )
    assert [call for call in goals.calls if call[0] == "activate"] == [
        ("activate", selected_node["binding"]["goal_run_id"])
    ]
    restarted = AgentSessionService(
        SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"),
        goals,
        attention_scheduler=AttentionScheduler(),
    )
    assert restarted.inspect(created["id"])["current_goal"]["id"] == selected["id"]
    assert len([call for call in goals.calls if call[0] == "activate"]) == 1


def test_nonselected_mobile_owner_is_paused_before_runtime_recovery(tmp_path) -> None:
    """Assert the downstream archive fence, not merely Session projection."""

    database = tmp_path / "long-lived.db"
    archive = _SQLiteApplicationStore(database)
    instance, created = archive.accept_start(
        "instance-old-nonselected",
        "private-long-lived-mobile-v1",
        "adb:device-1",
        "历史 Goal",
        "prepare-old-nonselected",
        "prepare-digest",
    )
    assert created and instance.status == "queued"

    gateway = LongLivedMobileApplicationRuntimeGateway(
        database,
        kernel=SimpleNamespace(),
        observation_provider=SimpleNamespace(),
    )
    first = gateway.fence_pause_before_start(
        instance.instance_id,
        "goal:old:attention-nonselected-startup-fence",
    )
    replay = gateway.fence_pause_before_start(
        instance.instance_id,
        "goal:old:attention-nonselected-startup-fence",
    )

    assert first.status == replay.status == "paused"
    # ApplicationRuntime startup calls this same archive recovery method.  A
    # paused instance with no in-flight action is intentionally not scheduled.
    assert archive.recover() == []


def test_nonselected_catalog_owner_is_paused_before_worker_start(tmp_path) -> None:
    database = tmp_path / "local-managed.db"
    archive = _SQLiteApplicationStore(database)
    instance, created = archive.accept_start(
        "instance-old-catalog",
        LOCAL_PROFILE_ID,
        None,
        "历史本地 Goal",
        "prepare-old-catalog",
        "prepare-digest",
    )
    assert created and instance.status == "queued"
    gateway = LocalManagedApplicationRuntimeGateway(database)
    catalog = ApplicationRuntimeCatalog(
        {LOCAL_PROFILE_ID: gateway},
        scheduler_profile_id=LOCAL_PROFILE_ID,
    )

    fenced = catalog.fence_pause_before_start(
        instance.instance_id,
        "goal:old:attention-nonselected-application-startup-fence",
    )
    assert fenced.status == "paused"
    catalog.startup()
    try:
        assert catalog.inspect(instance.instance_id).status == "paused"
        assert archive.recover() == []
    finally:
        catalog.shutdown()


def test_wait_event_yields_slot_and_notification_wakes_only_matching_goal(
    tmp_path,
) -> None:
    goals = PreparedGoalService()
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = AgentSessionService(
        store,
        goals,
        attention_scheduler=AttentionScheduler(),
    )
    router = SessionEventRouter(store)
    created = service.create("投简历；维护微信", "events-create")
    first, second = created["goal_nodes"]

    waiting_time = router.ingest(
        created["id"],
        source_namespace="test:r3",
        source_event_id="job-wait",
        event_type="GoalStateChangedEvent",
        occurred_at=utc_now(),
        payload={
            "goal_id": first["id"],
            "goal_run_id": first["binding"]["goal_run_id"],
            "status": "WAITING_TIME",
            "waiting": {
                "kind": "timer",
                "due_at": _future_due_at(),
            },
        },
    )
    service.handle_inbox_event(waiting_time.event.id)
    after_wait = service.inspect(created["id"])
    assert after_wait["current_goal"]["id"] == second["id"]
    first_after_wait = next(
        item for item in after_wait["goal_nodes"] if item["id"] == first["id"]
    )
    assert first_after_wait["status"] == "WAITING_TIME"
    assert first_after_wait["continuation"]["yield_reason"] == "WAITING"
    assert first_after_wait["wake_condition"]["kind"] == "TIME"

    waiting_notification = router.ingest(
        created["id"],
        source_namespace="test:r3",
        source_event_id="wechat-wait",
        event_type="GoalStateChangedEvent",
        occurred_at=utc_now(),
        payload={
            "goal_id": second["id"],
            "goal_run_id": second["binding"]["goal_run_id"],
            "status": "WAITING_EVENT",
            "waiting": {
                "kind": "notification",
                "matcher": {
                    "application_package": "com.tencent.mm",
                    "conversation_hint": "r3-contact",
                },
            },
        },
    )
    service.handle_inbox_event(waiting_notification.event.id)
    assert service.inspect(created["id"])["current_goal"] is None

    notification = router.ingest(
        created["id"],
        source_namespace="test:r3",
        source_event_id="wechat-notification-1",
        event_type="NotificationPostedEvent",
        occurred_at=utc_now(),
        payload={
            "application_package": "com.tencent.mm",
            "conversation_hint": "r3-contact",
            "urgency": "HIGH",
            "summary": "联系人发来一条消息",
        },
    )
    assert notification.affected_goal_ids == (second["id"],)
    service.handle_inbox_event(notification.event.id)
    awakened = service.inspect(created["id"])
    assert awakened["current_goal"]["id"] == second["id"]
    assert awakened["attention_decision"]["trigger_event_id"] == notification.event.id
    assert next(
        item
        for item in awakened["attention_candidates"]
        if item["goal_id"] == second["id"]
    )["event_urgency_component"] == 2_000


def test_restart_restores_selected_goal_and_continuations(
    tmp_path,
) -> None:
    goals = PreparedGoalService()
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = AgentSessionService(
        store,
        goals,
        attention_scheduler=AttentionScheduler(),
    )
    created = service.create("投简历；维护微信", "event-crash-create")
    goal = created["goal_nodes"][0]
    routed = SessionEventRouter(store).ingest(
        created["id"],
        source_namespace="test:r3-crash",
        source_event_id="wait-before-handler",
        event_type="GoalStateChangedEvent",
        occurred_at=utc_now(),
        payload={
            "goal_id": goal["id"],
            "goal_run_id": goal["binding"]["goal_run_id"],
            "status": "WAITING_TIME",
            "waiting": {
                "kind": "timer",
                "due_at": _future_due_at(),
            },
        },
    )
    assert routed.event.handling_status.value in {"CLASSIFIED", "IGNORED"}

    restarted = AgentSessionService(
        SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"),
        goals,
        attention_scheduler=AttentionScheduler(),
    )
    restored = restarted.inspect(created["id"])
    restored_goal = next(
        item for item in restored["goal_nodes"] if item["id"] == goal["id"]
    )
    assert restored_goal["status"] == "WAITING_TIME"
    assert restored_goal["continuation"]["yield_reason"] == "WAITING"
    recovered_event = next(
        event
        for event in restarted.events(created["id"], after=0, limit=500)
        if event.id == routed.event.id
    )
    assert recovered_event.handling_status.value == "HANDLED"
    assert recovered_event.decision_id is not None


def test_restart_redecides_when_latest_selected_goal_is_ready_not_active(
    tmp_path,
) -> None:
    goals = PreparedGoalService()
    database = tmp_path / "agent-runtime.db"
    store = SQLiteAgentRuntimeStore(database)
    service = AgentSessionService(
        store,
        goals,
        attention_scheduler=AttentionScheduler(),
    )
    created = service.create("投简历", "ready-recovery-create")
    goal_id = created["current_goal"]["id"]
    first_revision = created["attention_decision"]["decision_revision"]
    store.set_goal_ready(created["id"], goal_id)
    assert store.get_session(created["id"]).active_goal_id == goal_id
    assert store.goal_nodes(created["id"])[0].status.value == "READY"

    restarted = AgentSessionService(
        SQLiteAgentRuntimeStore(database),
        goals,
        attention_scheduler=AttentionScheduler(),
    )
    restored = restarted.inspect(created["id"])
    assert restored["current_goal"]["id"] == goal_id
    assert restored["current_goal"]["status"] == "ACTIVE"
    assert (
        restored["attention_decision"]["decision_revision"]
        == first_revision + 1
    )


def test_goal_service_fences_every_historical_owner_except_selected() -> None:
    records = [
        SimpleNamespace(
            id="goal-selected",
            binding_kind="long_lived_mobile_composition",
            bound_task_id="instance-selected",
            terminal_at=None,
            control_state="AUTOMATED",
        ),
        SimpleNamespace(
            id="goal-old",
            binding_kind="long_lived_mobile_composition",
            bound_task_id="instance-old",
            terminal_at=None,
            control_state="AUTOMATED",
        ),
    ]

    class Store:
        def list(self, limit):
            assert limit == 500
            return records

    class Runtime:
        def __init__(self):
            self.calls = []

        def fence_pause_before_start(self, instance_id, request_id):
            self.calls.append((instance_id, request_id))

    runtime = Runtime()
    service = GoalService(
        Store(),
        mobile_runtime=None,
        mobile_archive=None,
        configured_serial=None,
        long_lived_mobile_runtime=runtime,
        long_lived_mobile_archive=runtime,
    )

    service.fence_nonselected_long_lived_bindings_before_start({"goal-selected"})

    assert runtime.calls == [
        (
            "instance-old",
            "goal:goal-old:attention-nonselected-startup-fence",
        )
    ]


def test_goal_service_fences_nonselected_catalog_binding_only() -> None:
    records = [
        SimpleNamespace(
            id="goal-selected",
            binding_kind="application_runtime",
            bound_task_id="instance-selected",
            terminal_at=None,
            control_state="AUTOMATED",
        ),
        SimpleNamespace(
            id="goal-old",
            binding_kind="application_runtime",
            bound_task_id="instance-old",
            terminal_at=None,
            control_state="AUTOMATED",
        ),
    ]

    class Store:
        def list(self, limit):
            assert limit == 500
            return records

    class Runtime:
        def __init__(self):
            self.calls = []

        def fence_pause_before_start(self, instance_id, request_id):
            self.calls.append((instance_id, request_id))

    runtime = Runtime()
    service = GoalService(
        Store(),
        mobile_runtime=None,
        mobile_archive=None,
        configured_serial=None,
        application_runtime=runtime,
        application_archive=runtime,
    )
    service.fence_nonselected_application_bindings_before_start(
        {"goal-selected"}
    )
    assert runtime.calls == [
        (
            "instance-old",
            "goal:goal-old:attention-nonselected-application-startup-fence",
        )
    ]


def test_device_busy_yields_retryable_goal_state(tmp_path) -> None:
    class BusyGoalService(PreparedGoalService):
        def activate_goal(self, goal_id: str) -> dict[str, object]:
            self.calls.append(("activate", goal_id))
            value = dict(self.inspect(goal_id))
            value["waiting_reason"] = {
                "code": "target_busy",
                "message": "device lease is temporarily busy",
            }
            return value

    goals = BusyGoalService()
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = AgentSessionService(
        store,
        goals,
        attention_scheduler=AttentionScheduler(),
    )

    created = service.create("查看手机网络状态", "busy-create")

    assert store.pending_attention_dispatches(created["id"]) == []
    node = store.goal_nodes(created["id"])[0]
    assert node.status.value == "WAITING_DEVICE"
    assert store.latest_continuation(node.id).yield_reason.value == "DEVICE_BUSY"
    wake = store.wake_conditions(created["id"], pending_only=True)
    assert len(wake) == 1 and wake[0].kind.value == "DEVICE"
    assert service.inspect(created["id"])["current_goal"] is None


def test_restart_handles_atomic_device_busy_yield_after_handler_crash(
    tmp_path,
) -> None:
    class BusyGoalService(PreparedGoalService):
        def activate_goal(self, goal_id: str) -> dict[str, object]:
            self.calls.append(("activate", goal_id))
            value = dict(self.inspect(goal_id))
            value["waiting_reason"] = {"code": "target_busy"}
            return value

    class CrashAfterAtomicYieldService(AgentSessionService):
        def handle_inbox_event(self, event_id: str, *, dispatch: bool = True):
            event = self._event_by_id(event_id)
            if event is not None and event.event_type.value == "DeviceBusyEvent":
                raise RuntimeError("crash after atomic busy yield")
            return super().handle_inbox_event(event_id, dispatch=dispatch)

    goals = BusyGoalService()
    database = tmp_path / "agent-runtime.db"
    store = SQLiteAgentRuntimeStore(database)
    interrupted = CrashAfterAtomicYieldService(
        store,
        goals,
        attention_scheduler=AttentionScheduler(),
    )
    created = interrupted.create("查看手机网络状态", "busy-crash-create")
    node = store.goal_nodes(created["id"])[0]
    pending = store.pending_attention_dispatches(created["id"])
    assert len(pending) == 1 and pending[0].status.value == "RETRYABLE"
    assert node.status.value == "WAITING_DEVICE"
    assert store.latest_continuation(node.id).yield_reason.value == "DEVICE_BUSY"
    busy_event = next(
        event
        for event in store.events(created["id"], after=0, limit=500)
        if event.event_type.value == "DeviceBusyEvent"
    )
    assert busy_event.decision_id is None

    restarted = AgentSessionService(
        SQLiteAgentRuntimeStore(database),
        goals,
        attention_scheduler=AttentionScheduler(),
    )
    restored = restarted.inspect(created["id"])
    assert restored["current_goal"] is None
    assert restored["goal_nodes"][0]["status"] == "WAITING_DEVICE"
    recovered_event = next(
        event
        for event in restarted.events(created["id"], after=0, limit=500)
        if event.id == busy_event.id
    )
    assert recovered_event.handling_status.value == "HANDLED"
    assert recovered_event.decision_id is not None
    assert restarted.store.pending_attention_dispatches(created["id"]) == []
    assert len([call for call in goals.calls if call[0] == "activate"]) == 1


def test_sibling_pause_failure_keeps_dispatch_retryable_and_never_activates_selected(
    tmp_path,
) -> None:
    class FenceFailureGoalService(PreparedGoalService):
        def __init__(self) -> None:
            super().__init__()
            self.selected_activations: list[tuple[str, str]] = []

        def pause_goal_at_checkpoint(self, goal_id: str, key: str) -> None:
            del goal_id, key
            raise GoalOwnerUnavailable("sibling owner inspect unavailable")

        def activate_selected_goal(self, goal_id: str, key: str):
            self.selected_activations.append((goal_id, key))
            return self.inspect(goal_id)

    goals = FenceFailureGoalService()
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = AgentSessionService(
        store,
        goals,
        attention_scheduler=AttentionScheduler(),
    )

    created = service.create("投简历；维护微信", "pause-fail-closed")

    assert goals.selected_activations == []
    pending = store.pending_attention_dispatches(created["id"])
    assert len(pending) == 1
    assert pending[0].status.value == "RETRYABLE"
    assert "GoalOwnerUnavailable" in str(pending[0].last_error)


def test_selected_owner_inspect_failure_does_not_deliver_dispatch(tmp_path) -> None:
    class SelectedFailureGoalService(PreparedGoalService):
        def pause_goal_at_checkpoint(self, goal_id: str, key: str) -> None:
            del goal_id, key

        def activate_selected_goal(self, goal_id: str, key: str):
            del goal_id, key
            raise GoalOwnerUnavailable("selected owner inspect unavailable")

    goals = SelectedFailureGoalService()
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = AgentSessionService(
        store,
        goals,
        attention_scheduler=AttentionScheduler(),
    )

    created = service.create("查看手机状态", "selected-fail-closed")

    pending = store.pending_attention_dispatches(created["id"])
    assert len(pending) == 1
    assert pending[0].status.value == "RETRYABLE"
    assert "GoalOwnerUnavailable" in str(pending[0].last_error)


def test_goal_service_owner_inspect_and_kernel_startup_fence_fail_closed() -> None:
    record = SimpleNamespace(
        id="goal-kernel",
        binding_kind="runtime_kernel",
        bound_task_id="kernel-task",
        terminal_at=None,
        control_state="AUTOMATED",
    )

    class Store:
        def inspect(self, goal_id):
            assert goal_id == record.id
            return record

        def list(self, limit):
            assert limit == 500
            return [record]

    class UnavailableKernel:
        def inspect(self, task_id):
            assert task_id == record.bound_task_id
            raise RuntimeError("kernel archive unavailable")

    service = GoalService(
        Store(),
        mobile_runtime=None,
        mobile_archive=None,
        configured_serial=None,
        kernel_runtime=UnavailableKernel(),
        kernel_binding_kind="runtime_kernel",
    )

    with pytest.raises(GoalOwnerUnavailable):
        service.activate_selected_goal(record.id, "selected-inspect")
    with pytest.raises(GoalOwnerUnavailable):
        service.pause_goal_at_checkpoint(record.id, "pause-inspect")
    with pytest.raises(GoalOwnerUnavailable):
        service.fence_nonselected_kernel_bindings_before_recover(set())


@pytest.mark.parametrize("edge_kind", [GoalEdgeKind.BLOCKS, GoalEdgeKind.UNBLOCKS])
def test_goal_graph_success_dependency_blocks_then_unlocks_successor(
    tmp_path, edge_kind,
) -> None:
    goals = PreparedGoalService()
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = store.create_unplanned_session(
        instruction="先完成 A，再执行 B",
        client_request_id="dependency-create",
    )
    directive = store.directives(session.id)[0]
    predecessor_id = str(uuid.uuid4())
    successor_id = str(uuid.uuid4())
    store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(1, directive.id, "directed prerequisite"),
        nodes=(
            GoalNodeDraft(
                predecessor_id,
                "完成 A",
                directive.id,
                "完成 A",
                explicit_priority=1,
                execution_goal="完成 A",
            ),
            GoalNodeDraft(
                successor_id,
                "执行 B",
                directive.id,
                "执行 B",
                explicit_priority=100,
                execution_goal="执行 B",
            ),
        ),
        edges=(
            GoalEdgeDraft(
                predecessor_id,
                successor_id,
                edge_kind,
                "user-order",
            ),
        ),
        criteria=(
            GoalCriterionDraft(predecessor_id, "A 已验证"),
            GoalCriterionDraft(successor_id, "B 已验证"),
        ),
        coverage=(
            GoalCoverageDraft(
                directive.id,
                "完成 A",
                GoalCoverageKind.GOAL,
                predecessor_id,
            ),
            GoalCoverageDraft(
                directive.id,
                "执行 B",
                GoalCoverageKind.GOAL,
                successor_id,
            ),
        ),
    )
    service = AgentSessionService(
        store,
        goals,
        attention_scheduler=AttentionScheduler(),
        recover_on_start=False,
    )
    service.recover_pending(session_id=session.id)

    first = service.reconcile_attention(
        session.id,
        trigger_key="dependency:predecessor-required",
    )
    assert first.selected_goal_id == predecessor_id
    successor_candidate = next(
        item
        for item in store.attention_candidates(first.id)
        if item.goal_id == successor_id
    )
    assert successor_candidate.eligibility.value == "DEPENDENCY_BLOCKED"

    # A failed terminal projection is not successful prerequisite evidence.
    # It stays blocked until a later graph/runtime fact proves success.
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE goal_nodes SET status='FAILED' WHERE goal_node_id=?",
            (predecessor_id,),
        )
    after_failure = service.reconcile_attention(
        session.id,
        trigger_key="dependency:predecessor-failed",
    )
    assert after_failure.selected_goal_id is None
    failed_successor = next(
        item
        for item in store.attention_candidates(after_failure.id)
        if item.goal_id == successor_id
    )
    assert failed_successor.eligibility.value == "DEPENDENCY_BLOCKED"

    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE goal_nodes SET status='COMPLETED' WHERE goal_node_id=?",
            (predecessor_id,),
        )
    second = service.reconcile_attention(
        session.id,
        trigger_key="dependency:predecessor-completed",
    )
    assert second.selected_goal_id == successor_id


def test_recovery_queries_do_not_drop_oldest_records_after_500(tmp_path) -> None:
    timestamp = "2026-08-24T00:00:00Z"
    agent_store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    agent_store.initialize()
    with agent_store._connection(write=True) as connection:
        for index in range(501):
            session_id = f"session-{index:03d}"
            directive_id = f"directive-{index:03d}"
            node_id = f"node-{index:03d}"
            run_id = f"run-{index:03d}"
            connection.execute(
                "INSERT INTO agent_sessions("
                "session_id, client_request_id, original_instruction, "
                "authority_revision, session_kind, status, control_mode, "
                "active_goal_id, event_cursor, calendar_started_at, created_at, updated_at"
                ") VALUES (?, ?, ?, 1, 'today', 'ACTIVE', 'AGENT_ACTIVE', ?, 0, ?, ?, ?)",
                (
                    session_id,
                    f"request-{index:03d}",
                    f"goal {index}",
                    node_id,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO user_directives("
                "directive_id, session_id, revision, content, directive_kind, "
                "source_message_id, created_at"
                ") VALUES (?, ?, 1, ?, 'original', ?, ?)",
                (
                    directive_id,
                    session_id,
                    f"goal {index}",
                    f"source-{index:03d}",
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO goal_nodes("
                "goal_node_id, session_id, title, source_directive_id, status, "
                "bound_goal_run_id, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
                (
                    node_id,
                    session_id,
                    f"goal {index}",
                    directive_id,
                    run_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO session_goal_bindings("
                "binding_id, session_id, goal_node_id, goal_run_id, status, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'BOUND', ?, ?)",
                (
                    f"binding-{index:03d}",
                    session_id,
                    node_id,
                    run_id,
                    timestamp,
                    timestamp,
                ),
            )

    session_service = AgentSessionService(
        agent_store,
        PreparedGoalService(),
        attention_scheduler=AttentionScheduler(),
        recover_on_start=False,
    )
    selected = session_service.selected_goal_run_ids()
    assert len(selected) == 501
    assert "run-000" in selected

    goal_store = SQLiteGoalStore(tmp_path / "goals.db")
    goal_store.initialize()
    with goal_store._connection(write=True) as connection:
        for index in range(501):
            connection.execute(
                "INSERT INTO goal_runs("
                "goal_id, original_goal, execution_status, control_state, "
                "binding_kind, binding_state, bound_task_id, target_id, "
                "created_at, updated_at"
                ") VALUES (?, ?, 'ACCEPTED', 'AUTOMATED', "
                "'application_runtime', 'BOUND', ?, 'local', ?, ?)",
                (
                    f"goal-{index:03d}",
                    f"goal {index}",
                    f"task-{index:03d}",
                    timestamp,
                    timestamp,
                ),
            )

    class FenceRuntime:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def fence_pause_before_start(self, instance_id: str, key: str) -> None:
            self.calls.append((instance_id, key))

    runtime = FenceRuntime()
    goal_service = GoalService(
        goal_store,
        mobile_runtime=None,
        mobile_archive=None,
        configured_serial=None,
        application_runtime=runtime,
        application_archive=runtime,
    )
    goal_service.fence_nonselected_application_bindings_before_start(
        {"goal-500"}
    )

    fenced_instances = {instance_id for instance_id, _ in runtime.calls}
    assert len(fenced_instances) == 500
    assert "task-000" in fenced_instances
    assert "task-500" not in fenced_instances


def test_bound_goal_with_missing_runtime_is_not_selected_or_delivered(
    tmp_path,
) -> None:
    goal_store = SQLiteGoalStore(tmp_path / "goals.db")
    historical, _ = goal_store.create(
        goal="历史 application owner",
        idempotency_key="historical-missing-runtime",
    )
    goal_store.bind_task(
        historical.id,
        "historical-instance",
        "local",
        binding_kind="application_runtime",
    )
    goal_service = GoalService(
        goal_store,
        mobile_runtime=None,
        mobile_archive=None,
        configured_serial=None,
        application_runtime=None,
        application_archive=None,
    )

    agent_store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = agent_store.create_unplanned_session(
        instruction="恢复历史 owner",
        client_request_id="missing-runtime-session",
    )
    directive = agent_store.directives(session.id)[0]
    node_id = str(uuid.uuid4())
    agent_store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(1, directive.id),
        nodes=(
            GoalNodeDraft(
                node_id,
                "恢复历史 owner",
                directive.id,
                "恢复历史 owner",
                execution_goal="恢复历史 owner",
            ),
        ),
        criteria=(GoalCriterionDraft(node_id, "owner 已恢复"),),
        coverage=(
            GoalCoverageDraft(
                directive.id,
                "恢复历史 owner",
                GoalCoverageKind.GOAL,
                node_id,
            ),
        ),
    )
    intent = next(
        item
        for item in agent_store.pending_activation_intents()
        if item.goal_node_id == node_id
    )
    agent_store.settle_goal_run(intent.id, goal_run_id=historical.id)
    service = AgentSessionService(
        agent_store,
        goal_service,
        attention_scheduler=AttentionScheduler(),
        recover_on_start=False,
    )

    decision = service.reconcile_attention(
        session.id,
        trigger_key="startup:missing-bound-runtime",
    )

    assert decision.selected_goal_id is None
    candidate = agent_store.attention_candidates(decision.id)[0]
    assert candidate.eligibility.value == "NOT_BOUND"
    assert agent_store.pending_attention_dispatches(session.id) == []
    assert agent_store.get_session(session.id).active_goal_id is None
    assert agent_store.goal_nodes(session.id)[0].status.value != "ACTIVE"
    waiting = goal_store.inspect(historical.id)
    assert waiting.execution_status == "WAITING_CONFIGURATION"
    assert waiting.waiting_reason["code"] == (
        "bound_goal_owner_runtime_not_configured"
    )
    with pytest.raises(GoalOwnerUnavailable):
        goal_service.activate_selected_goal(
            historical.id,
            "race-after-availability-probe",
        )


def test_lifespan_fences_application_before_start_and_resumes_before_dispatch(
    tmp_path,
) -> None:
    order: list[str] = []

    class AgentService(AgentSessionService):
        def recover_planning(self):
            order.append("agenda:planning")

        def recover_pending(self):
            order.append("agenda:pending")

        def recover_inbox_events(self, *, dispatch=True):
            assert dispatch is False
            order.append("agenda:inbox")

        def recover_attention(self, *, dispatch=True):
            assert dispatch is False
            order.append("agenda:attention")

        def selected_goal_run_ids(self):
            order.append("agenda:selected")
            return {"goal-selected"}

        def recover_stopping(self):
            order.append("session:stopping")

        def recover_attention_dispatches(self):
            order.append("attention:dispatch")

    class GoalServiceSpy:
        def fence_nonselected_application_bindings_before_start(self, selected):
            assert selected == {"goal-selected"}
            order.append("application:fence")

        def recover_selected_application_bindings(self, selected):
            assert selected == {"goal-selected"}
            order.append("application:selected-resume")

    class ApplicationRuntimeSpy:
        def startup(self):
            order.append("application:startup")

        def shutdown(self):
            order.append("application:shutdown")

        def list(self, limit=100):
            del limit
            return []

    runtime = ApplicationRuntimeSpy()
    agent_service = AgentService(
        SQLiteAgentRuntimeStore(tmp_path / "lifespan-agent-runtime.db"),
        PreparedGoalService(),
        attention_scheduler=AttentionScheduler(),
        recover_on_start=False,
    )
    app = create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        application_runtime=runtime,
        application_runtime_archive=runtime,
        goal_service=GoalServiceSpy(),
        agent_session_service=agent_service,
    )

    with TestClient(app):
        pass

    assert order.index("agenda:attention") < order.index("application:fence")
    assert order.index("application:fence") < order.index("application:startup")
    assert order.index("application:startup") < order.index(
        "application:selected-resume"
    )
    assert order.index("application:selected-resume") < order.index(
        "attention:dispatch"
    )
