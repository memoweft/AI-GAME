from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.mobile_agent.domain import IdempotencyConflict, TaskNotFound
from ai_game_console.mobile_agent import MobileTaskRuntime
from ai_game_console.mobile_task_adapter import (
    MobileTaskAndroidDriver,
    OpenAICompatibleMobileRoleModel,
)
from ai_game_console.mobile_task_profiles import resolve_mobile_skill_scope

from conftest import WRITE_HEADERS, build_settings


def _state(*, status: str = "running", revision: int = 0) -> dict[str, object]:
    return {
        "task_id": "task-1",
        "goal": "完成今天的星铁日常",
        "target_id": "adb:127.0.0.1:16384",
        "skill_id": "starrail-daily",
        "status": status,
        "input_revision": revision,
        "plan": {
            "revision": 1,
            "subgoals": (
                {"index": 0, "description": "打开每日训练", "status": "completed"},
                {"index": 1, "description": "领取奖励", "status": "active"},
            ),
        },
        "active_subgoal_index": 1,
        "strategy": "从主菜单进入每日训练",
        "no_progress_count": 1,
        "reflection_count": 1,
        "attempt_count": 2,
        "cancel_requested": status == "stopping",
        "verification_satisfied": status == "completed",
        "detail": None,
        "error_code": None,
        "skill_memory_version": 2,
        "inputs": (
            {
                "revision": 1,
                "content": "先关掉活动弹窗",
                "lifecycle": "applied",
                "client_request_id": "input-1",
                "created_at": "2026-08-10T00:00:01Z",
                "applied_at": "2026-08-10T00:00:02Z",
            },
        ) if revision else (),
        "attempts": (
            {
                "attempt_id": "attempt-1",
                "sequence": 1,
                "plan_revision": 1,
                "subgoal_index": 0,
                "input_revision": 0,
                "decision": {
                    "kind": "act",
                    "intent": {
                        "name": "text",
                        "arguments": {"text": "SENTINEL_PRIVATE_TEXT"},
                    },
                    "reason": "",
                },
                "before": {"evidence_id": "before-1", "summary": "主菜单"},
                "transport": {
                    "status": "accepted",
                    "receipt_id": "receipt-1",
                    "detail": "transport accepted",
                },
                "after": {"evidence_id": "after-1", "summary": "每日训练页"},
                "verification": {
                    "satisfied": True,
                    "progress": True,
                    "uncertain": False,
                    "evidence": "每日训练入口可见",
                },
                "created_at": "2026-08-10T00:00:00Z",
                "finalized_at": "2026-08-10T00:00:01Z",
            },
        ),
        "reflections": (
            {
                "sequence": 1,
                "previous_strategy": "直接点击",
                "strategy": "先关闭遮挡弹窗",
                "reason": "连续三次无进展",
                "consecutive_no_progress": 3,
                "created_at": "2026-08-10T00:00:00Z",
            },
        ),
        "events": (
            {
                "sequence": 1,
                "event_type": "task_accepted",
                "data": {"private": "SENTINEL_PRIVATE_TEXT"},
                "created_at": "2026-08-10T00:00:00Z",
            },
        ),
        "created_at": "2026-08-10T00:00:00Z",
        "updated_at": "2026-08-10T00:00:02Z",
        "finished_at": None,
    }


class FakeMobileTaskRuntime:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.send_calls: list[dict[str, object]] = []
        self.stop_calls: list[dict[str, object]] = []
        self.shutdown_calls = 0

    def start(self, goal, client_request_id, *, target_id=None, skill_id=None):
        self.start_calls.append({
            "goal": goal,
            "client_request_id": client_request_id,
            "target_id": target_id,
            "skill_id": skill_id,
        })
        return _state()

    def send(self, task_id, content, client_request_id):
        self.send_calls.append({
            "task_id": task_id,
            "content": content,
            "client_request_id": client_request_id,
        })
        return _state(revision=1)

    def stop(self, task_id, client_request_id):
        self.stop_calls.append({
            "task_id": task_id,
            "client_request_id": client_request_id,
        })
        return _state(status="stopping")

    def inspect(self, task_id):
        if task_id == "missing":
            raise TaskNotFound("missing")
        return _state()

    def list(self, limit=100):
        return [_state()][:limit]

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class RaisingShutdownMobileTaskRuntime(FakeMobileTaskRuntime):
    def shutdown(self) -> None:
        super().shutdown()
        raise TimeoutError("mobile shutdown sentinel")


class LifecycleSpy:
    def __init__(self) -> None:
        self.start_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_mobile_shutdown_failure_does_not_skip_other_runtime_shutdowns(
    tmp_path: Path,
) -> None:
    mobile = RaisingShutdownMobileTaskRuntime()
    chat = LifecycleSpy()
    learning = LifecycleSpy()
    app = create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=mobile,
        chat_coordinator=chat,
        game_learner=learning,
    )

    with pytest.raises(TimeoutError, match="mobile shutdown sentinel"):
        with TestClient(app):
            pass

    assert mobile.shutdown_calls == 1
    assert learning.shutdown_calls == 1
    assert chat.shutdown_calls == 1


def test_mobile_task_routes_project_durable_state_without_raw_action_arguments(
    tmp_path: Path,
) -> None:
    runtime = FakeMobileTaskRuntime()
    app = create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=runtime,
    )

    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/tasks",
            headers=WRITE_HEADERS,
            json={
                "goal": "  完成今天的星铁日常  ",
                "client_request_id": "task-start-1",
                "target_id": " adb:127.0.0.1:16384 ",
                "skill_id": " starrail-daily ",
            },
        )
        assert accepted.status_code == 202
        payload = accepted.json()
        assert payload["id"] == "task-1"
        assert payload["plan"]["subgoals"][1]["status"] == "active"
        assert payload["attempts"] == [
            {
                "id": "attempt-1",
                "sequence": 1,
                "subgoal_index": 0,
                "action_type": "text",
                "transport_status": "accepted",
                "verification": {
                    "satisfied": True,
                    "progress": True,
                    "uncertain": False,
                    "evidence": "每日训练入口可见",
                },
                "created_at": "2026-08-10T00:00:00Z",
                "finalized_at": "2026-08-10T00:00:01Z",
            }
        ]
        assert "SENTINEL_PRIVATE_TEXT" not in accepted.text
        assert "arguments" not in accepted.text
        assert runtime.start_calls == [{
            "goal": "完成今天的星铁日常",
            "client_request_id": "task-start-1",
            "target_id": "adb:127.0.0.1:16384",
            "skill_id": "starrail-daily",
        }]

        listed = client.get("/api/v1/tasks?limit=20")
        assert listed.status_code == 200
        assert listed.json()["count"] == 1

        inspected = client.get("/api/v1/tasks/task-1")
        assert inspected.status_code == 200

        updated = client.post(
            "/api/v1/tasks/task-1/inputs",
            headers=WRITE_HEADERS,
            json={"content": "  先关掉活动弹窗  ", "client_request_id": "input-1"},
        )
        assert updated.status_code == 202
        assert updated.json()["input_revision"] == 1
        assert runtime.send_calls[0]["content"] == "先关掉活动弹窗"

        stopping = client.post(
            "/api/v1/tasks/task-1/stop",
            headers=WRITE_HEADERS,
            json={"client_request_id": "stop-1"},
        )
        assert stopping.status_code == 202
        assert stopping.json()["status"] == "stopping"
        assert runtime.stop_calls == [
            {"task_id": "task-1", "client_request_id": "stop-1"}
        ]

        missing = client.get("/api/v1/tasks/missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "mobile_task_not_found"

    assert runtime.shutdown_calls == 1


class ConflictingMobileTaskRuntime(FakeMobileTaskRuntime):
    def start(self, goal, client_request_id, *, target_id=None, skill_id=None):
        raise IdempotencyConflict("conflict")


def test_mobile_task_idempotency_conflict_is_a_stable_409(tmp_path: Path) -> None:
    app = create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=ConflictingMobileTaskRuntime(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/tasks",
            headers=WRITE_HEADERS,
            json={"goal": "继续", "client_request_id": "same-request"},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mobile_task_request_id_conflict"


def test_invalid_mobile_task_request_is_redacted(tmp_path: Path) -> None:
    app = create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=FakeMobileTaskRuntime(),
    )
    sentinel = "SENTINEL_INVALID_TASK_CONTENT"

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/tasks",
            headers=WRITE_HEADERS,
            json={"goal": sentinel, "client_request_id": "not allowed whitespace"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_mobile_task_request",
            "message": "智能任务请求无效。",
        }
    }
    assert sentinel not in response.text


def test_task_history_remains_readable_when_execution_dependencies_are_missing(
    tmp_path: Path,
) -> None:
    archive = FakeMobileTaskRuntime()
    app = create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_archive=archive,
    )

    with TestClient(app) as client:
        listed = client.get("/api/v1/tasks")
        inspected = client.get("/api/v1/tasks/task-1")
        rejected_start = client.post(
            "/api/v1/tasks",
            headers=WRITE_HEADERS,
            json={"goal": "继续率土任务", "client_request_id": "offline-start"},
        )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == "task-1"
    assert inspected.status_code == 200
    assert rejected_start.status_code == 503
    assert rejected_start.json()["error"]["code"] == "mobile_task_runtime_not_configured"


def test_production_composition_supports_target_selection_without_a_global_serial(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        gui_executor_enabled=True,
        adb_path=str(tmp_path / "adb.exe"),
        adb_serial=None,
        local_chat_endpoint="http://127.0.0.1:9999/v1",
        local_chat_model="gui-owl",
    )

    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )
    runtime = app.state.mobile_task_runtime

    assert isinstance(runtime, MobileTaskRuntime)
    assert isinstance(runtime._driver, MobileTaskAndroidDriver)
    assert isinstance(runtime._model, OpenAICompatibleMobileRoleModel)
    assert runtime._driver.device_lease is app.state.device_execution_lease
    assert runtime._driver.executor.serial is None
    assert runtime._model.model == "gui-owl"
    assert runtime._max_attempts == 2_048
    assert runtime._max_reflections == 64
    assert runtime._scope_resolver is resolve_mobile_skill_scope
    assert runtime._store.database_path == settings.data_dir / "mobile-tasks.db"
    runtime.shutdown()
