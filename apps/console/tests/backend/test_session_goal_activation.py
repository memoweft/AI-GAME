from __future__ import annotations

import pytest

from test_session_planner import PreparedGoalService, _service


def test_crash_after_graph_commit_replays_activation_outbox_once(tmp_path) -> None:
    goals = PreparedGoalService()

    def crash(point: str, _: dict[str, object]) -> None:
        if point == "after_goal_graph_committed":
            raise RuntimeError("test crash")

    interrupted = _service(tmp_path, goals, crash_hook=crash, recover_on_start=False)
    with pytest.raises(RuntimeError, match="test crash"):
        interrupted.create("投简历；维护微信", "crash-1")
    assert goals.calls == []

    recovered = _service(tmp_path, goals)
    snapshot = recovered.list()[0]
    assert len(snapshot["bindings"]) == 2
    assert [kind for kind, _ in goals.calls].count("prepare") == 2
    assert [kind for kind, _ in goals.calls].count("activate") == 2


def test_reprioritize_creates_graph_revision_without_another_goal_activation(tmp_path) -> None:
    goals = PreparedGoalService()
    service = _service(tmp_path, goals)
    first = service.create("投简历；维护微信", "priority-1")
    calls_before = len(goals.calls)

    revised = service.send_message(
        first["id"], "把找工作放在第一位", "priority-2", "reprioritize"
    )

    assert revised["goal_graph"]["revision"] == 2
    assert len(revised["goal_nodes"]) == 2
    assert len(goals.calls) == calls_before
    assert revised["directives"][-1]["directive_kind"] == "reprioritize"
    assert next(item for item in revised["goal_nodes"] if item["title"] == "找工作")["explicit_priority"] == 100


def test_revise_updates_the_existing_goal_without_another_activation(tmp_path) -> None:
    goals = PreparedGoalService()
    service = _service(tmp_path, goals)
    first = service.create("投简历；空闲的时候玩游戏", "revise-1")
    game = next(item for item in first["goal_nodes"] if item["title"] == "空闲游戏")
    calls_before = len(goals.calls)

    revised = service.send_message(
        first["id"], "把空闲游戏修改为空闲时只玩20分钟游戏", "revise-2", "revise"
    )

    changed = next(item for item in revised["goal_nodes"] if item["id"] == game["id"])
    assert changed["title"] == "空闲时只玩20分钟游戏"
    assert revised["goal_graph"]["revision"] == 2
    assert len(revised["goal_nodes"]) == 2
    assert len(goals.calls) == calls_before


def test_retried_message_recovers_activation_after_graph_commit(tmp_path) -> None:
    goals = PreparedGoalService()
    service = _service(tmp_path, goals)
    created = service.create("投简历", "retry-1")

    def crash(point: str, _: dict[str, object]) -> None:
        if point == "after_goal_graph_committed":
            raise RuntimeError("test crash")

    service.crash_hook = crash
    with pytest.raises(RuntimeError, match="test crash"):
        service.send_message(created["id"], "再玩一会游戏", "retry-2", "add")
    calls_before = len(goals.calls)
    service.crash_hook = None

    recovered = service.send_message(created["id"], "再玩一会游戏", "retry-2", "add")

    assert recovered["goal_graph"]["revision"] == 2
    assert len(recovered["bindings"]) == 2
    assert len(goals.calls) == calls_before + 2
