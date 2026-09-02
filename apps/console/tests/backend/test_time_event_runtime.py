from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from ai_game_console.agent_runtime.domain import utc_now
from ai_game_console.agent_runtime.event_pump import AgentRuntimeEventPump
from ai_game_console.agent_runtime.event_router import SessionEventRouter
from ai_game_console.agent_runtime.scheduler import AttentionScheduler
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore

from test_session_planner import PreparedGoalService


def test_time_event_poll_persists_before_wake_and_attention_without_restart(
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
    created = service.create("投简历；维护微信", "r7-time-event-create")
    first = created["goal_nodes"][0]
    due = datetime.now(UTC) + timedelta(seconds=5)
    due_at = due.isoformat().replace("+00:00", "Z")
    waiting = router.ingest(
        created["id"],
        source_namespace="test:r7-time",
        source_event_id="goal-enters-time-wait",
        event_type="GoalStateChangedEvent",
        occurred_at=utc_now(),
        payload={
            "goal_id": first["id"],
            "goal_run_id": first["binding"]["goal_run_id"],
            "status": "WAITING_TIME",
            "waiting": {"kind": "timer", "due_at": due_at},
        },
    )
    service.handle_inbox_event(waiting.event.id)
    assert next(
        item
        for item in service.inspect(created["id"])["goal_nodes"]
        if item["id"] == first["id"]
    )["status"] == "WAITING_TIME"

    handled = service.poll_due_time_events(
        dispatch=True,
        now=due + timedelta(seconds=1),
    )

    assert handled == 1
    timer_events = [
        event
        for event in store.events(created["id"], after=0, limit=500)
        if event.event_type.value == "TimerDueEvent"
    ]
    assert len(timer_events) == 1
    timer = timer_events[0]
    assert timer.source_namespace == "agent-runtime-timer"
    assert timer.affected_goal_ids == (first["id"],)
    assert timer.data["routing"]["event_class"] == "timer"
    assert timer.data["routing"]["routes"][0]["goal_id"] == first["id"]
    assert timer.decision_id is not None
    after = service.inspect(created["id"])
    assert after["attention_decision"]["trigger_event_id"] == timer.id
    assert after["current_goal"]["id"] == first["id"]

    assert service.poll_due_time_events(
        dispatch=True,
        now=due + timedelta(seconds=2),
    ) == 0
    assert len(
        [
            event
            for event in store.events(created["id"], after=0, limit=500)
            if event.event_type.value == "TimerDueEvent"
        ]
    ) == 1


def test_agent_runtime_event_pump_polls_until_shutdown() -> None:
    observed = threading.Event()

    class Service:
        def __init__(self) -> None:
            self.calls = 0

        def poll_due_time_events(self, *, dispatch, now):
            assert dispatch is True
            assert now.tzinfo is not None
            self.calls += 1
            observed.set()
            return 0

    service = Service()
    pump = AgentRuntimeEventPump(
        service,
        interval_seconds=0.01,
        clock=lambda: datetime(2026, 8, 26, 4, 0, tzinfo=UTC),
    )

    pump.start()
    assert observed.wait(timeout=1)
    assert pump.is_running is True
    pump.shutdown()

    assert service.calls >= 1
    assert pump.is_running is False


def test_event_pump_releases_scheduler_only_after_the_single_thread_joins() -> None:
    entered = threading.Event()
    release = threading.Event()
    shutdowns: list[str] = []

    class Service:
        def poll_due_time_events(self, *, dispatch, now):
            entered.set()
            release.wait(timeout=5)
            return 0

    class Scheduler:
        def poll_once(self):
            return 0

        def shutdown(self):
            shutdowns.append("released")

    pump = AgentRuntimeEventPump(Service(), long_task_scheduler=Scheduler(), interval_seconds=0.01)
    pump.start()
    assert entered.wait(timeout=1)
    assert [thread.name for thread in threading.enumerate()].count("agent-runtime-event-pump") == 1

    pump.shutdown()
    assert pump.is_running is True
    assert shutdowns == [], "join timeout must leave the process lease to TTL expiry"

    release.set()
    pump.shutdown()
    assert pump.is_running is False
    assert shutdowns == ["released"]
