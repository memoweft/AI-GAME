from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from ai_game_console.goal_runtime import (
    GoalRepairManager,
    PreflightResult,
    ProductionGoalRepairs,
    SQLiteGoalStore,
)


@dataclass(frozen=True)
class Target:
    external_id: str
    status: str = "ready"


def setup_store(tmp_path: Path):
    store = SQLiteGoalStore(tmp_path / "goals.db")
    goal, _ = store.create(goal="查看电池", idempotency_key="goal-1")
    return store, goal.id


def waiting(*facts):
    return PreflightResult(
        "WAITING_CONFIGURATION", tuple(facts),
        waiting_reason={"code": "execution_capability_not_ready", "message": "wait"},
    )


def completed(command, stdout="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


def test_owned_stopped_model_is_started_once_and_verified(tmp_path: Path):
    store, goal_id = setup_store(tmp_path)
    script = tmp_path / "scripts" / "model-runtime.ps1"
    script.parent.mkdir()
    script.touch()
    state = {"ready": False, "starts": 0}

    def runner(command, timeout):
        del timeout
        if command[-1] == "status":
            return completed(command, "status : stopped\npid : \nmodel_id : \ngpu_free_mib : 24000\n")
        assert command[-1] == "start"
        state["starts"] += 1
        state["ready"] = True
        return completed(command)

    repairs = ProductionGoalRepairs(
        manager=GoalRepairManager(store), project_root=tmp_path,
        capability_snapshot=lambda: {"capabilities": [SimpleNamespace(
            id="model", status="ready" if state["ready"] else "stopped",
            detail="model state",
        )]},
        discover_targets=lambda: SimpleNamespace(targets=()),
        adb_path=None, model_control_script=script, repository_model_enabled=False,
        runner=runner,
    )
    repairs._powershell = lambda: "powershell.exe"
    result = waiting({
        "capability": "local.visual_grounding", "state": "WAITING_CONFIGURATION",
    }, {"capability": "target:one", "state": "READY"})

    assert repairs.attempt(goal_id, result) is True
    assert repairs.attempt(goal_id, result) is True
    assert state["starts"] == 1
    assert store.repairs(goal_id)[0]["state"] == "VERIFIED"


def test_model_pid_mismatch_is_recorded_without_starting_over_it(tmp_path: Path):
    store, goal_id = setup_store(tmp_path)
    script = tmp_path / "scripts" / "model-runtime.ps1"
    script.parent.mkdir()
    script.touch()
    starts = []

    def runner(command, timeout):
        del timeout
        if command[-1] == "status":
            return completed(command, "status=pid_mismatch\npid=4321\ngpu_free_mib=24000\n")
        starts.append(command)
        return completed(command)

    repairs = ProductionGoalRepairs(
        manager=GoalRepairManager(store), project_root=tmp_path,
        capability_snapshot=lambda: {"capabilities": [{"id": "model", "status": "stopped"}]},
        discover_targets=lambda: SimpleNamespace(targets=()), adb_path=None, runner=runner,
    )
    repairs._powershell = lambda: "powershell.exe"

    assert repairs.attempt(goal_id, waiting({
        "capability": "local.visual_grounding", "state": "WAITING_CONFIGURATION",
    }, {"capability": "target:one", "state": "READY"})) is True
    attempt = store.repairs(goal_id)[0]
    assert attempt["state"] == "SKIPPED_IDENTITY"
    assert attempt["identity"]["pid"] == "4321"
    assert starts == []


def test_model_capacity_refusal_is_noop_and_allows_explicit_retry(tmp_path: Path):
    store, goal_id = setup_store(tmp_path)
    script = tmp_path / "scripts" / "model-runtime.ps1"
    script.parent.mkdir()
    script.touch()
    starts = []

    def runner(command, timeout):
        del timeout
        if command[-1] == "status":
            return completed(command, "status=stopped\npid=\ngpu_free_mib=581\n")
        starts.append(command)
        return completed(command, returncode=5)

    repairs = ProductionGoalRepairs(
        manager=GoalRepairManager(store), project_root=tmp_path,
        capability_snapshot=lambda: {"capabilities": [{"id": "model", "status": "stopped"}]},
        discover_targets=lambda: SimpleNamespace(targets=()), adb_path=None, runner=runner,
    )
    repairs._powershell = lambda: "powershell.exe"
    result = waiting(
        {"capability": "local.visual_grounding", "state": "WAITING_CONFIGURATION"},
        {"capability": "target:one", "state": "READY"},
    )

    assert repairs.attempt(goal_id, result) is True
    assert repairs.attempt(goal_id, result) is True
    attempts = store.repairs(goal_id)
    assert len(attempts) == 2
    assert all(item["state"] == "FAILED" and item["applied"] is False for item in attempts)
    assert attempts[1]["repair_key"].endswith(":retry:1")
    assert len(starts) == 2


def test_running_mumu_zero_is_synced_without_lifecycle_command(tmp_path: Path):
    store, goal_id = setup_store(tmp_path)
    root = tmp_path / "MuMuPlayer"
    adb = root / "nx_device" / "15.0" / "shell" / "adb.exe"
    cli = root / "nx_main" / "mumu-cli.exe"
    adb.parent.mkdir(parents=True)
    cli.parent.mkdir(parents=True)
    adb.touch()
    cli.touch()
    script = tmp_path / "scripts" / "sync-mumu-executor.ps1"
    script.parent.mkdir()
    script.touch()
    state = {"connected": False, "syncs": 0}
    commands = []

    def runner(command, timeout):
        del timeout
        commands.append(tuple(command))
        if command[0] == str(cli):
            payload = {
                "index": "0", "is_android_started": True,
                "is_process_started": True, "player_state": "start_finished",
                "adb_host_ip": "127.0.0.1", "adb_port": "16384",
            }
            return completed(command, json.dumps(payload))
        state["syncs"] += 1
        state["connected"] = True
        return completed(command)

    repairs = ProductionGoalRepairs(
        manager=GoalRepairManager(store), project_root=tmp_path,
        capability_snapshot=lambda: {"capabilities": [{"id": "model", "status": "ready"}]},
        discover_targets=lambda: SimpleNamespace(
            targets=(Target("127.0.0.1:16384"),) if state["connected"] else ()
        ),
        adb_path=adb, runner=runner,
    )
    repairs._powershell = lambda: "powershell.exe"

    assert repairs.attempt(goal_id, waiting(
        {"capability": "local.visual_grounding", "state": "READY"},
        {"capability": "android.discovery", "state": "READY"},
    )) is True
    attempt = store.repairs(goal_id)[0]
    assert attempt["state"] == "VERIFIED"
    assert attempt["after"]["expected_serial"] == "127.0.0.1:16384"
    assert state["syncs"] == 1
    assert all("start" not in command and "stop" not in command for command in commands)


def test_nonrunning_mumu_is_never_started_or_synced(tmp_path: Path):
    store, goal_id = setup_store(tmp_path)
    root = tmp_path / "MuMuPlayer"
    adb = root / "nx_device" / "15.0" / "shell" / "adb.exe"
    cli = root / "nx_main" / "mumu-cli.exe"
    adb.parent.mkdir(parents=True)
    cli.parent.mkdir(parents=True)
    adb.touch()
    cli.touch()
    script = tmp_path / "scripts" / "sync-mumu-executor.ps1"
    script.parent.mkdir()
    script.touch()
    mutations = []

    def runner(command, timeout):
        del timeout
        if command[0] == str(cli):
            return completed(command, json.dumps({
                "index": "0", "is_android_started": False,
                "is_process_started": False, "player_state": "stopped",
                "adb_host_ip": "127.0.0.1", "adb_port": "16384",
            }))
        mutations.append(command)
        return completed(command)

    repairs = ProductionGoalRepairs(
        manager=GoalRepairManager(store), project_root=tmp_path,
        capability_snapshot=lambda: {"capabilities": [{"id": "model", "status": "ready"}]},
        discover_targets=lambda: SimpleNamespace(targets=()), adb_path=adb, runner=runner,
    )
    repairs._powershell = lambda: "powershell.exe"

    assert repairs.attempt(goal_id, waiting(
        {"capability": "local.visual_grounding", "state": "READY"},
        {"capability": "android.discovery", "state": "READY"},
    )) is True
    assert store.repairs(goal_id)[0]["state"] == "SKIPPED_IDENTITY"
    assert mutations == []
