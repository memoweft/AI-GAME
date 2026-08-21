from __future__ import annotations

from pathlib import Path
import sqlite3

from ai_game_console.goal_runtime import GoalRepairManager, SQLiteGoalStore


def goal_store(tmp_path: Path) -> tuple[SQLiteGoalStore, str]:
    store = SQLiteGoalStore(tmp_path / "goals.db")
    goal, created = store.create(goal="打开设置查看电池后回桌面", idempotency_key="goal-1")
    assert created is True
    return store, goal.id


def test_repair_is_persisted_verified_and_never_reapplied(tmp_path: Path):
    store, goal_id = goal_store(tmp_path)
    manager = GoalRepairManager(store)
    readiness = iter((
        {"ready": False, "status": "stopped"},
        {"ready": True, "status": "ready"},
    ))
    calls = []

    first = manager.run(
        goal_id, repair_key="model:start:v1", component="model-runtime",
        readiness_probe=lambda: next(readiness),
        identity_probe=lambda: {"confirmed": True, "owner": "AI-GAME"},
        apply_repair=lambda: calls.append("start") or {"detail": "started owned runtime"},
    )
    replay = manager.run(
        goal_id, repair_key="model:start:v1", component="model-runtime",
        readiness_probe=lambda: (_ for _ in ()).throw(AssertionError("replay probed")),
        identity_probe=lambda: (_ for _ in ()).throw(AssertionError("replay probed")),
        apply_repair=lambda: calls.append("duplicate"),
    )

    assert first["state"] == "VERIFIED"
    assert first["applied"] is True
    assert replay == first
    assert calls == ["start"]
    assert store.repairs(goal_id) == [first]


def test_repair_refuses_mutation_without_confirmed_identity(tmp_path: Path):
    store, goal_id = goal_store(tmp_path)
    calls = []

    result = GoalRepairManager(store).run(
        goal_id, repair_key="mumu:sync:v1", component="mumu-player",
        readiness_probe=lambda: {"ready": False, "status": "not_configured"},
        identity_probe=lambda: {"confirmed": False, "reason": "foreign process"},
        apply_repair=lambda: calls.append("mutated"),
    )

    assert result["state"] == "SKIPPED_IDENTITY"
    assert result["applied"] is False
    assert calls == []


def test_repair_requires_post_action_readiness(tmp_path: Path):
    store, goal_id = goal_store(tmp_path)

    result = GoalRepairManager(store).run(
        goal_id, repair_key="adb:connect:v1", component="adb-target",
        readiness_probe=lambda: {"ready": False, "status": "offline"},
        identity_probe=lambda: {"confirmed": True, "serial": "emulator-1"},
        apply_repair=lambda: {"detail": "connect returned without readiness"},
    )

    assert result["state"] == "FAILED"
    assert result["applied"] is True
    assert result["after"]["ready"] is False


def test_already_ready_component_records_a_verified_noop(tmp_path: Path):
    store, goal_id = goal_store(tmp_path)

    result = GoalRepairManager(store).run(
        goal_id, repair_key="adb:ready:v1", component="adb-target",
        readiness_probe=lambda: {"ready": True, "status": "device"},
        identity_probe=lambda: (_ for _ in ()).throw(AssertionError("identity not needed")),
        apply_repair=lambda: (_ for _ in ()).throw(AssertionError("must not mutate")),
    )

    assert result["state"] == "VERIFIED"
    assert result["applied"] is False
    assert result["identity"]["not_required"] is True


def test_schema_v2_is_upgraded_without_losing_goal_runs(tmp_path: Path):
    database = tmp_path / "goals.db"
    original = SQLiteGoalStore(database)
    goal, _ = original.create(goal="保留的目标", idempotency_key="goal-1")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE goal_repair_attempts")
        connection.execute("UPDATE goal_schema SET version = 2 WHERE singleton = 1")

    reopened = SQLiteGoalStore(database)
    assert reopened.inspect(goal.id).original_goal == "保留的目标"
    assert reopened.repairs(goal.id) == []
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version FROM goal_schema WHERE singleton = 1"
        ).fetchone()[0]
    assert version == 5
