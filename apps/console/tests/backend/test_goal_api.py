from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.goal_runtime import GoalService, PreflightResult, SQLiteGoalStore

from conftest import WRITE_HEADERS, build_settings


def _task_state(status: str = "queued", *, events=()):
    return {
        "task_id": "mobile-1",
        "status": status,
        "detail": None,
        "error_code": None,
        "events": events,
    }


class FakeMobileRuntime:
    def __init__(self, status: str = "queued") -> None:
        self.status = status
        self.start_calls = []
        self.send_calls = []
        self.stop_calls = []

    def start(self, goal, client_request_id, target_id=None, skill_id=None, **options):
        self.start_calls.append({
            "goal": goal,
            "client_request_id": client_request_id,
            "target_id": target_id,
            "skill_id": skill_id,
            **options,
        })
        return _task_state(self.status)

    def inspect(self, task_id):
        assert task_id == "mobile-1"
        return _task_state(self.status, events=(
            {"sequence": 1, "event_type": "task_accepted", "created_at": "2026-08-20T00:00:00Z"},
        ))

    def send(self, task_id, content, client_request_id):
        self.send_calls.append((task_id, content, client_request_id))
        return self.inspect(task_id)

    def stop(self, task_id, client_request_id):
        self.stop_calls.append((task_id, client_request_id))
        self.status = "stopping"
        return self.inspect(task_id)

    def list(self, limit=100):
        return [self.inspect("mobile-1")]

    def shutdown(self):
        pass


def _app(tmp_path: Path, runtime: FakeMobileRuntime, *, configured: bool):
    settings = build_settings(tmp_path)
    if configured:
        settings = replace(settings, adb_serial="127.0.0.1:16384")
    return create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=runtime,
    )


def test_v2_goal_waits_without_configured_default_and_has_no_task_side_effect(tmp_path: Path):
    runtime = FakeMobileRuntime()
    with TestClient(_app(tmp_path, runtime, configured=False)) as client:
        response = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "打开设置查看电池后回桌面", "idempotency_key": "goal-1"},
        )
    assert response.status_code == 202
    payload = response.json()
    assert payload["execution_status"] == "WAITING_CONFIGURATION"
    assert payload["binding"]["task_id"] is None
    assert payload["waiting_reason"]["code"] == "default_android_target_not_configured"
    assert runtime.start_calls == []


def test_v2_goal_binds_once_projects_candidate_and_survives_restart(tmp_path: Path):
    runtime = FakeMobileRuntime(status="queued")
    body = {"goal": "打开设置查看电池后回桌面", "idempotency_key": "goal-1"}
    with TestClient(_app(tmp_path, runtime, configured=True)) as client:
        first = client.post("/api/v2/goals", headers=WRITE_HEADERS, json=body)
        replay = client.post("/api/v2/goals", headers=WRITE_HEADERS, json=body)
        goal_id = first.json()["id"]
        assert replay.json()["id"] == goal_id
        assert len(runtime.start_calls) == 1
        assert runtime.start_calls[0]["execution_origin"] == "v2_goal_compat"
        assert runtime.start_calls[0]["promote_success_memory"] is False
        runtime.status = "completed"
        completed = client.get(f"/api/v2/goals/{goal_id}")
        assert completed.json()["execution_status"] == "CANDIDATE_COMPLETE"
        assert completed.json()["terminal_at"] is None
        assert completed.json()["uncompleted_items"]
        events = client.get(f"/api/v2/goals/{goal_id}/events?after=0")
        cursors = [item["cursor"] for item in events.json()["items"]]
        assert cursors == sorted(cursors)
        assert len(cursors) == len(set(cursors))

    restarted_runtime = FakeMobileRuntime(status="completed")
    with TestClient(_app(tmp_path, restarted_runtime, configured=True)) as client:
        restored = client.get(f"/api/v2/goals/{goal_id}")
        assert restored.json()["binding"]["task_id"] == "mobile-1"
        assert restored.json()["execution_status"] == "CANDIDATE_COMPLETE"
    assert restarted_runtime.start_calls == []


def test_v2_idempotency_conflict_controls_messages_and_redaction(tmp_path: Path):
    runtime = FakeMobileRuntime(status="running")
    with TestClient(_app(tmp_path, runtime, configured=True)) as client:
        created = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "查看电池", "idempotency_key": "same"},
        )
        goal_id = created.json()["id"]
        conflict = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "打开相机", "idempotency_key": "same"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "goal_idempotency_conflict"

        message = {"content": "完成后一定回桌面", "idempotency_key": "message-1"}
        assert client.post(
            f"/api/v2/goals/{goal_id}/messages", headers=WRITE_HEADERS, json=message
        ).status_code == 202
        assert client.post(
            f"/api/v2/goals/{goal_id}/messages", headers=WRITE_HEADERS, json=message
        ).status_code == 202
        assert len({call[2] for call in runtime.send_calls}) == 1

        unsupported = client.post(
            f"/api/v2/goals/{goal_id}/controls", headers=WRITE_HEADERS,
            json={"action": "pause", "idempotency_key": "pause-1"},
        )
        assert unsupported.status_code == 409
        assert unsupported.json()["error"]["code"] == "goal_control_unsupported"
        stopped = client.post(
            f"/api/v2/goals/{goal_id}/controls", headers=WRITE_HEADERS,
            json={"action": "stop", "idempotency_key": "stop-1"},
        )
        assert stopped.status_code == 202
        assert stopped.json()["control_state"] == "STOP_REQUESTED"

        sentinel = "SENTINEL_GOAL_PRIVATE_TEXT"
        invalid = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": sentinel, "idempotency_key": "contains whitespace"},
        )
        assert invalid.status_code == 422
        assert sentinel not in invalid.text


def test_v2_create_rejects_internal_selection_fields(tmp_path: Path):
    runtime = FakeMobileRuntime()
    with TestClient(_app(tmp_path, runtime, configured=True)) as client:
        response = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "查看电池", "idempotency_key": "goal-1", "device_serial": "x"},
        )
    assert response.status_code == 422
    assert runtime.start_calls == []


def test_v2_unusable_configured_target_waits_before_mobile_task_creation(tmp_path: Path):
    runtime = FakeMobileRuntime()
    settings = replace(build_settings(tmp_path), adb_serial="offline-serial")
    service = GoalService(
        SQLiteGoalStore(settings.data_dir / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial=settings.adb_serial,
        target_probe=lambda: (
            False,
            "executor_target_offline",
            "已配置的默认 Android 目标当前离线。",
        ),
    )
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=runtime,
        goal_service=service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "查看电池", "idempotency_key": "offline-target"},
        )

    assert response.status_code == 202
    assert response.json()["execution_status"] == "WAITING_CONFIGURATION"
    assert response.json()["waiting_reason"]["code"] == "executor_target_offline"
    assert runtime.start_calls == []


def test_u2_preflight_selects_sole_target_and_persists_environment(tmp_path: Path):
    runtime = FakeMobileRuntime()
    settings = build_settings(tmp_path)
    service = GoalService(
        SQLiteGoalStore(settings.data_dir / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial=None,
        preflight=lambda: PreflightResult(
            "READY",
            ({"capability": "target:adb:one", "state": "READY", "detail": "ready"},),
            selected_target_id="adb:one",
            selected_serial="one",
        ),
    )
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=runtime,
        goal_service=service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "查看电池", "idempotency_key": "sole-target"},
        )

    assert response.status_code == 202
    assert response.json()["binding"]["target_id"] == "adb:one"
    assert response.json()["environment_state"]["state"] == "READY"
    assert runtime.start_calls[0]["target_id"] == "adb:one"


def test_u2_multiple_target_gate_requires_fresh_valid_selection(tmp_path: Path):
    runtime = FakeMobileRuntime()
    settings = build_settings(tmp_path)
    result = PreflightResult(
        "WAITING_EXTERNAL",
        (),
        waiting_reason={
            "code": "TARGET_SELECTION_REQUIRED",
            "message": "请选择设备。",
        },
        target_options=(
            {"target_id": "adb:one", "name": "Phone", "connection": "one"},
            {"target_id": "adb:two", "name": "Tablet", "connection": "two"},
        ),
    )
    service = GoalService(
        SQLiteGoalStore(settings.data_dir / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial=None,
        preflight=lambda: result,
    )
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=runtime,
        goal_service=service,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "查看电池", "idempotency_key": "choose-target"},
        )
        goal_id = created.json()["id"]
        assert created.json()["waiting_reason"]["code"] == "TARGET_SELECTION_REQUIRED"
        assert runtime.start_calls == []

        invalid = client.post(
            f"/api/v2/goals/{goal_id}/preflight/selection",
            headers=WRITE_HEADERS,
            json={"target_id": "adb:stale"},
        )
        assert invalid.status_code == 409
        selected = client.post(
            f"/api/v2/goals/{goal_id}/preflight/selection",
            headers=WRITE_HEADERS,
            json={"target_id": "adb:two"},
        )

    assert selected.status_code == 202
    assert selected.json()["binding"]["target_id"] == "adb:two"
    assert runtime.start_calls[0]["target_id"] == "adb:two"


def test_u2_retry_resumes_the_same_goal_after_readiness_changes(tmp_path: Path):
    runtime = FakeMobileRuntime()
    settings = build_settings(tmp_path)
    results = [
        PreflightResult(
            "WAITING_CONFIGURATION",
            (),
            waiting_reason={"code": "android_target_not_ready", "message": "连接设备。"},
        ),
        PreflightResult(
            "READY",
            (),
            selected_target_id="adb:one",
            selected_serial="one",
        ),
    ]
    service = GoalService(
        SQLiteGoalStore(settings.data_dir / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial=None,
        preflight=lambda: results.pop(0),
    )
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=runtime,
        goal_service=service,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "查看电池", "idempotency_key": "retry-ready"},
        )
        goal_id = created.json()["id"]
        resumed = client.post(
            f"/api/v2/goals/{goal_id}/preflight/retry", headers=WRITE_HEADERS,
        )

    assert resumed.status_code == 202
    assert resumed.json()["id"] == goal_id
    assert resumed.json()["binding"]["task_id"] == "mobile-1"
    assert len(runtime.start_calls) == 1


def test_u2_managed_repair_reassesses_and_binds_the_same_goal(tmp_path: Path):
    runtime = FakeMobileRuntime()
    settings = build_settings(tmp_path)
    waiting = PreflightResult(
        "WAITING_CONFIGURATION", (),
        waiting_reason={"code": "execution_capability_not_ready", "message": "wait"},
    )
    ready = PreflightResult(
        "READY", (), selected_target_id="adb:one", selected_serial="one",
    )
    results = [waiting, ready]
    repairs = []
    service = GoalService(
        SQLiteGoalStore(settings.data_dir / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial=None,
        preflight=lambda: results.pop(0),
        repair=lambda goal_id, result: repairs.append((goal_id, result.state)) or True,
    )
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=runtime,
        goal_service=service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/goals", headers=WRITE_HEADERS,
            json={"goal": "查看电池", "idempotency_key": "repair-and-bind"},
        )

    assert response.status_code == 202
    assert response.json()["execution_status"] == "ACCEPTED"
    assert response.json()["environment_state"]["state"] == "READY"
    assert len(repairs) == 1
    assert repairs[0][0] == response.json()["id"]
    assert runtime.start_calls[0]["target_id"] == "adb:one"
