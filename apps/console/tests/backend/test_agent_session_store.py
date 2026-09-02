from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_game_console.agent_runtime import (
    SessionIdempotencyConflict,
    SessionStateConflict,
    SessionStatus,
)
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore


class FakeGoalService:
    def __init__(self) -> None:
        self.by_key: dict[str, dict[str, object]] = {}
        self.create_calls: list[tuple[str, str]] = []
        self.message_calls: list[tuple[str, str, str]] = []
        self.control_calls: list[tuple[str, str, str]] = []

    def create(self, goal: str, key: str) -> dict[str, object]:
        self.create_calls.append((goal, key))
        if key not in self.by_key:
            self.by_key[key] = {
                "id": f"goal-{len(self.by_key) + 1}", "original_goal": goal,
                "execution_status": "ACCEPTED", "control_state": "AUTOMATED",
            }
        return self.by_key[key]

    def inspect(self, goal_id: str) -> dict[str, object]:
        for value in self.by_key.values():
            if value["id"] == goal_id:
                return value
        raise KeyError(goal_id)

    def send_message(self, goal_id: str, content: str, key: str) -> dict[str, object]:
        self.message_calls.append((goal_id, content, key))
        return self.inspect(goal_id)

    def control(self, goal_id: str, action: str, key: str) -> dict[str, object]:
        self.control_calls.append((goal_id, action, key))
        value = self.inspect(goal_id)
        if action == "stop":
            value["execution_status"] = "CANCELLED"
        return value


def _service(tmp_path: Path, goal_service: FakeGoalService, **kwargs: object) -> AgentSessionService:
    return AgentSessionService(
        SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"), goal_service, **kwargs
    )


def test_create_session_is_idempotent(tmp_path: Path):
    goals = FakeGoalService()
    service = _service(tmp_path, goals)

    first = service.create("打开设置查看电池", "create-1")
    replay = service.create("打开设置查看电池", "create-1")

    assert replay["id"] == first["id"]
    assert len(goals.by_key) == 1
    with pytest.raises(SessionIdempotencyConflict):
        service.create("打开相机", "create-1")


def test_session_persists_original_directive_before_goal_activation(tmp_path: Path):
    goals = FakeGoalService()
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, created = store.create_session(
        instruction="打开设置查看电池", client_request_id="create-1"
    )

    assert created is True
    assert session.status is SessionStatus.PLANNING
    assert [item.content for item in store.directives(session.id)] == ["打开设置查看电池"]
    assert len(store.goal_nodes(session.id)) == 1
    pending = store.pending_outbox()
    assert len(pending) == 1 and pending[0].session_id == session.id
    assert goals.create_calls == []


def test_r1_creates_one_goal_node_and_one_goal_run_binding(tmp_path: Path):
    service = _service(tmp_path, FakeGoalService())
    created = service.create("打开设置查看电池", "create-1")

    assert len(created["goal_nodes"]) == 1
    assert created["binding"] is not None
    assert created["goal_nodes"][0]["bound_goal_run_id"] == created["binding"]["goal_run_id"]
    # R3 binding is a prepare fact; AttentionScheduler alone selects current.
    assert created["goal_nodes"][0]["status"] != "ACTIVE"
    assert created["current_goal"] is None


def test_restart_recovers_same_session_goal_and_event_cursor(tmp_path: Path):
    goals = FakeGoalService()
    service = _service(tmp_path, goals)
    created = service.create("打开设置查看电池", "create-1")
    cursor = created["event_cursor"]
    session_id = created["id"]
    goal_id = created["binding"]["goal_run_id"]

    restarted = _service(tmp_path, goals)
    restored = restarted.inspect(session_id)

    assert restored["original_instruction"] == "打开设置查看电池"
    assert restored["binding"]["goal_run_id"] == goal_id
    assert restored["event_cursor"] == cursor


def test_duplicate_recovery_does_not_create_second_goal_run(tmp_path: Path):
    goals = FakeGoalService()

    def crash_after_owner(_: str, __: dict[str, object]) -> None:
        raise RuntimeError("simulated process death")

    interrupted = _service(
        tmp_path, goals, crash_hook=crash_after_owner, recover_on_start=False
    )
    with pytest.raises(RuntimeError, match="simulated process death"):
        interrupted.create("打开设置查看电池", "create-1")
    # R2's first crash fence is after the whole GoalGraph commit and before
    # any GoalRuntime prepare/activate call.  The durable activation intent is
    # therefore present while the downstream GoalRun is still absent.
    assert len(goals.by_key) == 0

    restarted = _service(tmp_path, goals)
    recovered = restarted.list()[0]
    assert recovered["binding"]["goal_run_id"] == "goal-1"
    assert len(goals.by_key) == 1


def test_session_message_creates_new_directive_revision(tmp_path: Path):
    goals = FakeGoalService()
    service = _service(tmp_path, goals)
    session_id = service.create("打开设置查看电池", "create-1")["id"]

    first = service.send_message(session_id, "完成后回到桌面", "message-1")
    replay = service.send_message(session_id, "完成后回到桌面", "message-1")

    assert [item["revision"] for item in first["directives"]] == [1, 2]
    assert [item["revision"] for item in replay["directives"]] == [1, 2]
    # R2 treats an ADD directive as a GoalGraph revision.  It does not
    # broadcast the new user intent into the first GoalRun.
    assert goals.message_calls == []
    assert first["goal_graph"]["revision"] == 2
    assert len(first["goal_nodes"]) == 2
    assert len(goals.by_key) == 2


def test_goal_run_waiting_projection_sets_goal_waiting_and_session_waiting_all(tmp_path: Path):
    goals = FakeGoalService()
    service = _service(tmp_path, goals)
    created = service.create("打开设置查看电池", "create-1")
    goals.inspect(created["binding"]["goal_run_id"])["execution_status"] = "WAITING_CONFIGURATION"

    projected = service.inspect(created["id"])

    assert projected["goal_nodes"][0]["status"] == "WAITING_DEVICE"
    assert projected["status"] == "WAITING_ALL"


def test_session_stop_projects_to_bound_goal_and_settles_session(tmp_path: Path):
    goals = FakeGoalService()
    service = _service(tmp_path, goals)
    session_id = service.create("打开设置查看电池", "create-1")["id"]

    stopped = service.control(session_id, "stop", "stop-1")
    replay = service.control(session_id, "stop", "stop-1")

    assert stopped["status"] == "STOPPED"
    assert replay["status"] == "STOPPED"
    assert len(goals.control_calls) == 1
    assert goals.control_calls[0][1] == "stop"


def test_session_stop_settles_planner_only_session_without_activation_owner(tmp_path: Path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, created = store.create_unplanned_session(
        instruction="无法形成完整目标图", client_request_id="planner-only-stop"
    )
    assert created is True
    service = AgentSessionService(store, FakeGoalService(), recover_on_start=False)

    stopped = service.control(session.id, "stop", "stop-planner-only")
    replay = service.control(session.id, "stop", "stop-planner-only")

    assert stopped["status"] == "STOPPED"
    assert replay["status"] == "STOPPED"


def test_recover_stopping_settles_planner_only_session_without_activation_owner(tmp_path: Path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = store.create_unplanned_session(
        instruction="无法形成完整目标图", client_request_id="planner-only-recovery"
    )
    store.record_control(
        session.id, action="stop", client_request_id="recover-planner-only"
    )
    store.record_stop_directive(
        session.id, client_request_id="recover-planner-only"
    )
    service = AgentSessionService(store, FakeGoalService(), recover_on_start=False)

    assert service.recover_stopping() == 1
    assert service.inspect(session.id)["status"] == "STOPPED"


def test_session_stop_keeps_fence_when_activation_intent_can_still_create_owner(tmp_path: Path):
    class UnavailableGoalService(FakeGoalService):
        def create(self, goal: str, key: str) -> dict[str, object]:
            del goal, key
            raise RuntimeError("Goal owner is temporarily unavailable")

    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = store.create_session(
        instruction="仍有待恢复 activation", client_request_id="pending-owner-stop"
    )
    service = AgentSessionService(store, UnavailableGoalService(), recover_on_start=False)

    with pytest.raises(SessionStateConflict, match="没有可控制的 GoalRun binding"):
        service.control(session.id, "stop", "stop-pending-owner")
    assert service.inspect(session.id)["status"] == "STOPPING"


class TerminalGoalService(FakeGoalService):
    def control(self, goal_id: str, action: str, key: str) -> dict[str, object]:
        value = self.inspect(goal_id)
        if value["execution_status"] == "CANCELLED":
            raise RuntimeError("terminal GoalRun must not receive a second stop")
        return super().control(goal_id, action, key)


def test_session_stop_settles_when_bound_goal_run_is_already_terminal(tmp_path: Path):
    goals = TerminalGoalService()
    service = _service(tmp_path, goals)
    created = service.create("停止旧设备探针", "create-terminal-stop")
    goal_id = created["binding"]["goal_run_id"]
    goals.inspect(goal_id)["execution_status"] = "CANCELLED"

    stopped = service.control(created["id"], "stop", "stop-terminal-goal")

    assert stopped["status"] == "STOPPED"
    assert goals.control_calls == []


def test_recover_stopping_settles_when_bound_goal_run_is_already_terminal(tmp_path: Path):
    goals = TerminalGoalService()
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = AgentSessionService(store, goals)
    created = service.create("停止旧设备探针", "create-terminal-recovery")
    goal_id = created["binding"]["goal_run_id"]
    goals.inspect(goal_id)["execution_status"] = "FAILED"
    store.record_control(
        created["id"], action="stop", client_request_id="recover-terminal-goal"
    )
    store.record_stop_directive(
        created["id"], client_request_id="recover-terminal-goal"
    )

    assert service.recover_stopping() == 1
    assert service.inspect(created["id"])["status"] == "STOPPED"
    assert goals.control_calls == []


def test_repeated_stop_recovers_terminal_goal_run_after_prior_owner_failure(tmp_path: Path):
    class DelayedTerminalGoalService(TerminalGoalService):
        def control(self, goal_id: str, action: str, key: str) -> dict[str, object]:
            value = self.inspect(goal_id)
            if value["execution_status"] == "ACCEPTED":
                raise RuntimeError("owner state changed before stop settled")
            return super().control(goal_id, action, key)

    goals = DelayedTerminalGoalService()
    service = _service(tmp_path, goals)
    created = service.create("停止旧设备探针", "create-retry-terminal-stop")
    session_id = created["id"]
    goal_id = created["binding"]["goal_run_id"]

    with pytest.raises(RuntimeError, match="owner state changed"):
        service.control(session_id, "stop", "retry-terminal-stop")
    assert service.inspect(session_id)["status"] == "STOPPING"

    goals.inspect(goal_id)["execution_status"] = "FAILED"
    settled = service.control(session_id, "stop", "retry-terminal-stop")

    assert settled["status"] == "STOPPED"
    assert goals.control_calls == []


def test_session_stop_still_propagates_nonterminal_goal_owner_failure(tmp_path: Path):
    class FailingGoalService(FakeGoalService):
        def control(self, goal_id: str, action: str, key: str) -> dict[str, object]:
            del goal_id, action, key
            raise RuntimeError("owner unavailable")

    goals = FailingGoalService()
    service = _service(tmp_path, goals)
    session_id = service.create("停止旧设备探针", "create-failing-stop")["id"]

    with pytest.raises(RuntimeError, match="owner unavailable"):
        service.control(session_id, "stop", "stop-failing-goal")
    assert service.inspect(session_id)["status"] == "STOPPING"


def test_schema_v6_has_event_preemption_control_and_prior_runtime_tables(tmp_path: Path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    store.initialize()
    with sqlite3.connect(store.database_path) as connection:
        revision = connection.execute(
            "SELECT revision FROM agent_runtime_schema WHERE singleton=1"
        ).fetchone()[0]
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(session_goal_bindings)")}
        foreign_keys = connection.execute("PRAGMA foreign_key_list(goal_nodes)").fetchall()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert revision == 6
    assert "idx_one_active_binding_per_goal_node" in indexes
    assert foreign_keys
    assert {"attention_decisions", "attention_candidates", "goal_continuations", "wake_conditions", "attention_dispatches"} <= tables
    assert {"device_body_bindings", "device_body_capabilities", "device_snapshots", "body_action_commands", "body_execution_receipts"} <= tables
    assert {"preemption_requests", "session_control_transitions"} <= tables
