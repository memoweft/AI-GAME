from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_game_console.agent_runtime.continuation import FreshReplanGate
from ai_game_console.agent_runtime.event_router import (
    ExternalEventDeliveryGuard,
    ExternalEventOrder,
    SessionEventRouter,
)
from ai_game_console.agent_runtime.domain import SessionEventType, SessionNotFound
from ai_game_console.agent_runtime.scheduler import (
    LongTaskScheduler,
    OverlapPolicy,
    SQLiteSchedulerCoordination,
    WakeDispatch,
)
from ai_game_console.agent_runtime.service import CanonicalTaskService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore


FROZEN_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _service(path: Path, *, principal: str = "principal-a") -> CanonicalTaskService:
    return CanonicalTaskService(SQLiteAgentRuntimeStore(path), principal_id=principal)


def _due_task(service: CanonicalTaskService, *, due_at: str = "2026-08-30T11:59:00Z") -> str:
    task = service.create_task("later check", "create:task")
    task_id = task["task_id"]
    service.transition_task(
        task_id, status="running", reason_code="started", summary="Started.",
        recoverable=True, idempotency_key="start:task",
    )
    service.transition_task(
        task_id, status="waiting_time", reason_code="timer_not_due", summary="Waiting.",
        recoverable=True, next_wake_at=due_at, idempotency_key="wait:task",
    )
    return task_id


def _scheduler(
    service: CanonicalTaskService, coordination: SQLiteSchedulerCoordination,
    dispatched: list[WakeDispatch], *, owner: str = "scheduler-a",
    gate=lambda _task_id, _revision: True,
) -> LongTaskScheduler:
    return LongTaskScheduler(
        service, coordination, owner_id=owner,
        dispatch=lambda _task, wake: dispatched.append(wake),
        fresh_replan_gate=gate, clock=lambda: FROZEN_NOW,
    )


def test_due_time_wake_has_one_canonical_projection_across_restart(tmp_path: Path) -> None:
    database = tmp_path / "agent-runtime.db"
    service = _service(database)
    task_id = _due_task(service)
    delivered: list[WakeDispatch] = []

    first = _scheduler(service, SQLiteSchedulerCoordination(database), delivered)
    assert first.poll_once() == 1
    assert [wake.task_id for wake in delivered] == [task_id]

    # A new process sees the same SQLite coordination row and the canonical
    # Task state already moved to replanning; it cannot project the wake again.
    restarted = _scheduler(service, SQLiteSchedulerCoordination(database), delivered)
    assert restarted.poll_once() == 0
    timer_states = [
        event for event in service.events(task_id)
        if event["type"] == "task.state_changed" and event["data"].get("reason_code") == "timer_due"
    ]
    assert len(timer_states) == 1


def test_overdue_wake_is_reconciled_only_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "agent-runtime.db"
    service = _service(database)
    _due_task(service, due_at="2026-08-30T10:00:00Z")
    delivered: list[WakeDispatch] = []

    # No background thread is required: a post-restart poll reconciles the
    # persisted overdue time and writes exactly the normal canonical wake.
    restarted = _scheduler(service, SQLiteSchedulerCoordination(database), delivered)
    assert restarted.poll_once() == 1
    assert len(delivered) == 1


def test_restart_reclaims_a_wake_crash_before_its_canonical_projection(tmp_path: Path) -> None:
    database = tmp_path / "agent-runtime.db"
    service = _service(database)
    task_id = _due_task(service)
    coordination = SQLiteSchedulerCoordination(database)
    pre_crash = WakeDispatch(
        f"task:{task_id}:revision:1:due:2026-08-30T11:59:00Z",
        task_id, 1, "2026-08-30T11:59:00Z", "active",
    )
    assert coordination.claim_wake(pre_crash, overlap_policy=OverlapPolicy.SKIP) == "dispatch"
    delivered: list[WakeDispatch] = []
    restarted = _scheduler(service, SQLiteSchedulerCoordination(database), delivered)
    assert restarted.poll_once() == 1
    assert [wake.wake_id for wake in delivered] == [pre_crash.wake_id]


def test_scheduler_dispatch_error_enters_replanning_not_a_terminal_state(tmp_path: Path) -> None:
    database = tmp_path / "agent-runtime.db"
    service = _service(database)
    task_id = _due_task(service)
    scheduler = LongTaskScheduler(
        service, SQLiteSchedulerCoordination(database), owner_id="one",
        dispatch=lambda _task, _wake: (_ for _ in ()).throw(RuntimeError("fake runner failure")),
        clock=lambda: FROZEN_NOW,
    )
    assert scheduler.poll_once() == 0
    task = service.inspect_task(task_id)
    assert task["status"] == "replanning"
    assert task["terminal"] is False


def test_scheduler_owner_lease_allows_one_live_owner(tmp_path: Path) -> None:
    database = tmp_path / "scheduler.db"
    first = SQLiteSchedulerCoordination(database)
    second = SQLiteSchedulerCoordination(database)
    assert first.acquire_lease(owner_id="one", now=FROZEN_NOW, ttl=timedelta(seconds=30))
    assert not second.acquire_lease(owner_id="two", now=FROZEN_NOW, ttl=timedelta(seconds=30))
    assert second.acquire_lease(
        owner_id="two", now=FROZEN_NOW + timedelta(seconds=31), ttl=timedelta(seconds=30)
    )


@pytest.mark.parametrize("status", ["paused", "user_takeover"])
def test_pause_or_takeover_never_dispatches_stale_due_projection(tmp_path: Path, status: str) -> None:
    class StaleTaskService:
        def __init__(self) -> None:
            self.transition_calls = 0

        def list_tasks(self, **_kwargs):
            return [{"task_id": "task-1", "id": "task-1", "current_revision": 1,
                     "status": status, "terminal": False, "next_wake_at": "2026-08-30T11:00:00Z"}]

        def transition_task(self, *_args, **_kwargs):
            self.transition_calls += 1

    service = StaleTaskService()
    dispatched: list[WakeDispatch] = []
    scheduler = LongTaskScheduler(
        service, SQLiteSchedulerCoordination(tmp_path / "scheduler.db"), owner_id="one",
        dispatch=lambda _task, wake: dispatched.append(wake), clock=lambda: FROZEN_NOW,
    )
    assert scheduler.poll_once() == 0
    assert service.transition_calls == 0
    assert dispatched == []


def test_release_takeover_requires_fresh_observation_before_replan_dispatch(tmp_path: Path) -> None:
    database = tmp_path / "agent-runtime.db"
    service = _service(database)
    task_id = _due_task(service)
    task = service.inspect_task(task_id)
    service.control_task(
        task_id, action="takeover", idempotency_key="takeover", expected_revision=task["current_revision"], requested_by={"source": "test"},
    )
    gate = FreshReplanGate(database)
    task = service.inspect_task(task_id)
    release = gate.require_after_release(
        service, task_id, expected_revision=task["current_revision"],
        idempotency_key="release", requested_by={"source": "test"},
    )
    revision = release["task"]["current_revision"]
    assert release["task"]["status"] == "replanning"
    assert not gate.allows_dispatch(task_id, revision)
    gate.satisfy(task_id, revision=revision, observation_ref="observation:after-user", checkpoint_ref="checkpoint:replan")
    assert gate.allows_dispatch(task_id, revision)


def test_skip_and_buffer_one_never_create_duplicate_active_run(tmp_path: Path) -> None:
    coordination = SQLiteSchedulerCoordination(tmp_path / "scheduler.db")
    first = WakeDispatch("wake-1", "task-1", 1, "2026-08-30T12:00:00Z", "active")
    second = WakeDispatch("wake-2", "task-1", 1, "2026-08-30T12:01:00Z", "active")
    third = WakeDispatch("wake-3", "task-1", 1, "2026-08-30T12:02:00Z", "active")
    assert coordination.claim_wake(first, overlap_policy=OverlapPolicy.SKIP) == "dispatch"
    assert coordination.claim_wake(second, overlap_policy=OverlapPolicy.SKIP) == "skipped"

    separate = SQLiteSchedulerCoordination(tmp_path / "buffer.db")
    assert separate.claim_wake(first, overlap_policy=OverlapPolicy.BUFFER_ONE) == "dispatch"
    assert separate.claim_wake(second, overlap_policy=OverlapPolicy.BUFFER_ONE) == "buffered"
    assert separate.claim_wake(third, overlap_policy=OverlapPolicy.BUFFER_ONE) == "skipped"
    promoted = separate.complete_wake(wake_id=first.wake_id)
    assert promoted is not None and promoted.wake_id == second.wake_id and promoted.state == "active"


def test_duplicate_and_out_of_order_external_events_remain_one_safe_intake(tmp_path: Path) -> None:
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = store.create_unplanned_session(
        instruction="observe notices", client_request_id="session:create",
        owner_principal_id="principal-a", controller_id="controller-a",
    )
    guard = ExternalEventDeliveryGuard()
    first = guard.receive(source_namespace="fake", source_event_id="evt-2", source_stream_id="stream", source_cursor=2)
    duplicate = guard.receive(source_namespace="fake", source_event_id="evt-2", source_stream_id="stream", source_cursor=2)
    old = guard.receive(source_namespace="fake", source_event_id="evt-1", source_stream_id="stream", source_cursor=1)
    assert (first.ordering, duplicate.ordering, old.ordering) == (
        ExternalEventOrder.NEW, ExternalEventOrder.DUPLICATE, ExternalEventOrder.OUT_OF_ORDER,
    )

    router = SessionEventRouter(store)
    created = router.ingest(
        session.id, source_namespace="fake", source_event_id="evt-2",
        event_type=SessionEventType.NOTIFICATION_POSTED, occurred_at="2026-08-30T12:00:00Z",
        payload={"application_package": "example.app"}, source_stream_id="stream", source_cursor="2",
        device_id="emulator:fake", device_boot_id="boot-1",
    )
    replay = router.ingest(
        session.id, source_namespace="fake", source_event_id="evt-2",
        event_type=SessionEventType.NOTIFICATION_POSTED, occurred_at="2026-08-30T12:00:00Z",
        payload={"application_package": "example.app"}, source_stream_id="stream", source_cursor="2",
        device_id="emulator:fake", device_boot_id="boot-1",
    )
    assert created.created is True
    assert replay.created is False
    assert replay.new_route_effects == ()


def test_same_principal_can_use_new_dsh_session_but_other_principal_cannot(tmp_path: Path) -> None:
    database = tmp_path / "agent-runtime.db"
    owner_a = _service(database, principal="principal-a")
    task = owner_a.create_task("global task", "task:create")
    # A later official DSH session maps to the same capability principal.
    assert _service(database, principal="principal-a").inspect_task(task["task_id"])["task_id"] == task["task_id"]
    with pytest.raises(SessionNotFound):
        _service(database, principal="principal-b").inspect_task(task["task_id"])
