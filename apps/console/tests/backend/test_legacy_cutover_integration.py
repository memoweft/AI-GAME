"""Phase 7 切流集成测试：监控端点 + 排空门禁推进 + 切流/回滚序列。

- ``GET /runtime/mode``：三种模式下返回正确的模式与布尔标志。
- 排空门禁推进：DRAINING 期活动任务显式停止后，``/runtime/mode`` 计数归零。
- 切流/回滚序列：LEGACY → DRAINING → KERNEL_ACTIVE → 回滚 LEGACY 的写可用性变化。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.legacy_cutover import drain_gate_satisfied
from ai_game_console.mobile_agent.archive import MobileTaskArchive
from ai_game_console.mobile_agent.store import _SQLiteTaskStore

from conftest import WRITE_HEADERS, build_settings
from test_mobile_task_api import FakeMobileTaskRuntime

_NOW = "2026-07-17T00:00:00.000000+00:00"


def _build_app(tmp_path: Path, *, runtime_mode: str, **kwargs):
    settings = replace(build_settings(tmp_path), runtime_mode=runtime_mode)
    if runtime_mode == "kernel_active":
        adb_path = tmp_path / "adb.exe"
        adb_path.write_bytes(b"test-adb")
        settings = replace(
            settings,
            gui_executor_enabled=True,
            adb_path=str(adb_path),
            adb_serial="emulator-5554",
            mobile_role_endpoint="http://127.0.0.1:9/v1/chat/completions",
            mobile_role_model="test-model",
        )
    return create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        **kwargs,
    )


def _insert_task(db_path: Path, task_id: str, status: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO mobile_tasks (task_id, goal, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, f"goal-{task_id}", status, _NOW, _NOW),
        )
        conn.commit()


@pytest.mark.parametrize(
    "runtime_mode, expected",
    [
        ("legacy", {"legacy_writable": True, "kernel_active": False, "draining": False}),
        ("draining", {"legacy_writable": False, "kernel_active": False, "draining": True}),
        ("kernel_active", {"legacy_writable": False, "kernel_active": True, "draining": False}),
    ],
)
def test_runtime_mode_endpoint_booleans(tmp_path: Path, runtime_mode: str, expected: dict) -> None:
    """/runtime/mode 在三种模式下返回正确的模式与布尔标志。"""
    app = _build_app(tmp_path, runtime_mode=runtime_mode, mobile_task_runtime=FakeMobileTaskRuntime())
    with TestClient(app) as client:
        resp = client.get("/api/v1/runtime/mode")

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == runtime_mode
    for key, value in expected.items():
        assert body[key] is value
    # Fake 运行时不支持 active_tasks 查询，活动计数安全降级为 0。
    assert body["active_legacy_task_count"] == 0
    assert body["active_task_ids"] == []


def test_drain_gate_progression_via_mode_endpoint(tmp_path: Path) -> None:
    """DRAINING 期存量任务停止后，/runtime/mode 计数归零、门禁放行。"""
    db = tmp_path / "mobile-tasks.db"
    archive = MobileTaskArchive(db)
    store = _SQLiteTaskStore(db)
    store.initialize()
    _insert_task(db, "t-running", "running")
    _insert_task(db, "t-queued", "queued")

    app = _build_app(tmp_path, runtime_mode="draining", mobile_task_archive=archive)
    with TestClient(app) as client:
        before = client.get("/api/v1/runtime/mode").json()
        assert before["draining"] is True
        assert before["active_legacy_task_count"] == 2
        assert set(before["active_task_ids"]) == {"t-running", "t-queued"}
        assert drain_gate_satisfied(before["active_legacy_task_count"]) is False

        # 排空：把活动任务显式停止（运维对存量任务执行 stop 的自然结果）。
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE mobile_tasks SET status = 'stopped' WHERE status IN ('queued','running','planning','stopping')"
            )
            conn.commit()

        after = client.get("/api/v1/runtime/mode").json()
        assert after["active_legacy_task_count"] == 0
        assert after["active_task_ids"] == []
        assert drain_gate_satisfied(after["active_legacy_task_count"]) is True


def test_cutover_and_rollback_sequence(tmp_path: Path) -> None:
    """LEGACY → DRAINING → KERNEL_ACTIVE → 回滚 LEGACY 的写可用性序列。"""

    def app_for(mode: str):
        return _build_app(tmp_path, runtime_mode=mode, mobile_task_runtime=FakeMobileTaskRuntime())

    def post_legacy_task(client: TestClient) -> int:
        return client.post(
            "/api/v1/tasks",
            headers=WRITE_HEADERS,
            json={"goal": "完成日常任务", "client_request_id": "req-seq"},
        ).status_code

    with TestClient(app_for("legacy")) as client:
        assert post_legacy_task(client) == 202
        assert client.get("/api/v1/runtime/mode").json()["mode"] == "legacy"

    with TestClient(app_for("draining")) as client:
        assert post_legacy_task(client) == 403
        view = client.get("/api/v1/runtime/mode").json()
        assert view["draining"] is True
        assert view["legacy_writable"] is False

    with TestClient(app_for("kernel_active")) as client:
        # The canonical /tasks path is now Gateway-owned.  A Legacy-only
        # payload is rejected by that explicit contract, while direct Legacy
        # physical writes are fenced.
        assert post_legacy_task(client) == 400
        direct = client.post(
            "/api/v1/executor/actions",
            headers=WRITE_HEADERS,
            json={"target_id": "device-1", "action": "home"},
        )
        assert direct.status_code == 403
        assert direct.json()["error"]["code"] == "LEGACY_DEVICE_WRITE_DISABLED"
        view = client.get("/api/v1/runtime/mode").json()
        assert view["kernel_active"] is True
        assert view["legacy_writable"] is False

    # 回滚：切回 legacy 后恢复可写（运维设 AI_GAME_RUNTIME_MODE=legacy 并重启）。
    with TestClient(app_for("legacy")) as client:
        assert post_legacy_task(client) == 202
        assert client.get("/api/v1/runtime/mode").json()["mode"] == "legacy"
