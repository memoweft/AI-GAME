from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ai_game_console.agent_runtime.domain import (
    AttentionSelectorKind,
    GoalNodeStatus,
    SessionEventType,
    WakeConditionDraft,
    WakeConditionKind,
)
from ai_game_console.agent_runtime.scheduler import (
    AttentionScheduler,
    AttentionSelectionDraft,
)
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore


class _PreparedGoals:
    def __init__(self) -> None:
        self._next = 0

    def prepare_goal(self, goal: str, key: str) -> dict[str, str]:
        del goal
        self._next += 1
        return {"id": f"prepared-{self._next}", "key": key}

    def activate_goal(self, goal_id: str) -> dict[str, str]:
        return {"id": goal_id, "execution_status": "ACCEPTED"}

    def inspect(self, goal_id: str) -> dict[str, str]:
        return {"id": goal_id, "execution_status": "ACCEPTED"}


def _service(tmp_path, *, scheduler: AttentionScheduler | None = None) -> AgentSessionService:
    return AgentSessionService(
        SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"),
        _PreparedGoals(),
        attention_scheduler=scheduler or AttentionScheduler(),
        recover_on_start=False,
    )


def test_attention_decision_rejects_stale_scheduler_revision(tmp_path) -> None:
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    goals = _PreparedGoals()
    service = AgentSessionService(
        store, goals, attention_scheduler=AttentionScheduler(), recover_on_start=False
    )
    created = service.create("先处理 A；再处理 B", "stale-agenda-create")
    first, second = created["goal_nodes"]
    store.create_wake_condition(
        created["id"],
        second["id"],
        WakeConditionDraft(
            kind=WakeConditionKind.TIME,
            due_at="2030-01-01T00:00:00+00:00",
        ),
    )
    # The first snapshot can only choose the active A.  Making B ready from
    # inside final selection simulates a concurrent agenda write between read
    # and decision commit; B's priority wins after the required re-read.
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE goal_nodes SET explicit_priority=1000 WHERE goal_node_id=?",
            (second["id"],),
        )

    class _AgendaMutatingSelector:
        def __init__(self) -> None:
            self.calls = 0

        def select(self, context):
            self.calls += 1
            if self.calls == 1:
                store.set_goal_ready(created["id"], second["id"])
            return AttentionSelectionDraft(
                selected_goal_id=context.candidates[0].goal_id,
                reason=f"selector call {self.calls}",
                selector_kind=AttentionSelectorKind.QWEN,
            )

    selector = _AgendaMutatingSelector()
    service.attention_scheduler = AttentionScheduler(selector)
    before = store.latest_attention_decision(created["id"])
    decision = service.reconcile_attention(
        created["id"], trigger_key="r4:stale-agenda"
    )

    assert selector.calls == 2
    assert decision.selected_goal_id == second["id"]
    assert decision.agenda_revision == store.get_session(created["id"]).agenda_revision
    assert decision.decision_revision == before.decision_revision + 1
    assert store.attention_decision_for_trigger(
        created["id"], "r4:stale-agenda"
    ).id == decision.id


def test_same_trigger_retry_has_no_transient_plan_side_effect(tmp_path) -> None:
    class _CountingSelector:
        def __init__(self) -> None:
            self.calls = 0

        def select(self, context):
            self.calls += 1
            return AttentionSelectionDraft(
                selected_goal_id=context.candidates[0].goal_id,
                reason="counted final selection",
                selector_kind=AttentionSelectorKind.QWEN,
            )

    selector = _CountingSelector()
    service = _service(tmp_path, scheduler=AttentionScheduler(selector))
    created = service.create("处理一件事", "same-trigger-create")
    directive_id = created["directives"][-1]["id"]
    trigger = f"directive:{directive_id}"
    original = service.store.attention_decision_for_trigger(created["id"], trigger)

    replay = service.reconcile_attention(created["id"], trigger_key=trigger)

    assert replay.id == original.id
    assert selector.calls == 1
    assert len(service.store.attention_candidates(replay.id)) == len(
        service.store.goal_nodes(created["id"])
    )
    assert len(service.store.pending_attention_dispatches(created["id"])) == 0


def test_no_eligible_session_wakes_when_eligibility_changes(tmp_path) -> None:
    service = _service(tmp_path)
    created = service.create("等待一个计时任务", "timer-wake-create")
    goal_id = created["goal_nodes"][0]["id"]
    due_at = datetime(2030, 1, 1, tzinfo=UTC)
    service.store.create_wake_condition(
        created["id"], goal_id, WakeConditionDraft(
            kind=WakeConditionKind.TIME, due_at=due_at.isoformat()
        )
    )
    empty = service.reconcile_attention(
        created["id"], trigger_key="r4:no-eligible"
    )
    assert empty.selected_goal_id is None
    assert empty.outcome.value == "NO_ELIGIBLE"

    recovered = service.recover_attention(
        dispatch=False, now=due_at + timedelta(seconds=1)
    )
    current = service.store.latest_attention_decision(created["id"])

    assert recovered == 1
    assert current.selected_goal_id == goal_id
    assert current.id != empty.id
    assert service.store.goal_nodes(created["id"])[0].status is GoalNodeStatus.ACTIVE


def test_recovery_scan_is_complete_beyond_five_hundred_sessions(tmp_path) -> None:
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    store.initialize()
    timestamp = "2026-08-24T00:00:00+00:00"
    with store._connection(write=True) as connection:
        for index in range(501):
            session_id = f"session-{index:03d}"
            directive_id = f"directive-{index:03d}"
            goal_id = f"goal-{index:03d}"
            connection.execute(
                "INSERT INTO agent_sessions(session_id, client_request_id, original_instruction, "
                "authority_revision, session_kind, status, control_mode, active_goal_id, "
                "event_cursor, calendar_started_at, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, 'today', 'ACTIVE', 'AGENT_ACTIVE', NULL, 0, ?, ?, ?)",
                (session_id, f"request-{index:03d}", f"goal {index}", timestamp, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO user_directives(directive_id, session_id, revision, content, directive_kind, "
                "source_message_id, created_at) VALUES (?, ?, 1, ?, 'original', ?, ?)",
                (directive_id, session_id, f"goal {index}", f"source-{index:03d}", timestamp),
            )
            connection.execute(
                "INSERT INTO goal_graph_revisions(graph_revision_id, session_id, revision, authority_revision, "
                "source_directive_id, reason, created_at) VALUES (?, ?, 1, 1, ?, 'test', ?)",
                (f"graph-{index:03d}", session_id, directive_id, timestamp),
            )
            connection.execute(
                "INSERT INTO goal_nodes(goal_node_id, session_id, title, source_directive_id, graph_revision, "
                "original_fragment, status, bound_goal_run_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, 'READY', ?, ?, ?)",
                (goal_id, session_id, f"goal {index}", directive_id, f"goal {index}", f"run-{index:03d}", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO session_goal_bindings(binding_id, session_id, goal_node_id, goal_run_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'BOUND', ?, ?)",
                (f"binding-{index:03d}", session_id, goal_id, f"run-{index:03d}", timestamp, timestamp),
            )

    event, created = store.ingest_event(
        "session-000",
        source_namespace="r4-preflight",
        source_event_id="oldest-visible-event",
        event_type=SessionEventType.NOTIFICATION_POSTED,
        payload={"summary": "recover all sessions"},
        occurred_at=timestamp,
    )
    assert created
    service = AgentSessionService(
        store, _PreparedGoals(), attention_scheduler=AttentionScheduler(), recover_on_start=False
    )

    assert service._event_by_id(event.id).id == event.id
    assert service.recover_attention(dispatch=False) == 501
    assert store.latest_attention_decision("session-000") is not None
    assert store.latest_attention_decision("session-500") is not None
