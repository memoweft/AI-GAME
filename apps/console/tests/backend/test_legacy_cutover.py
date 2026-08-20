"""Phase 7 Legacy → Kernel 切流：端点 × 运行时模式 矩阵测试。

覆盖三阶段运行时模式（LEGACY / DRAINING / KERNEL_ACTIVE）下 Legacy Mobile Task
写端点的门控行为，以及全局事件列表的软弃用头：

- LEGACY：POST /tasks、/inputs、/stop 均可用（202）
- DRAINING：POST /tasks 拒绝（403 LEGACY_TASK_WRITE_DISABLED），存量 /inputs、/stop 仍可用
- KERNEL_ACTIVE：三个写端点全部 403，只读归档端点始终 200
- GET /events：三种模式均 200 且携带 Deprecation: true 头
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.config import Settings
from ai_game_console.discovery import AdbTargetDiscovery

from conftest import WRITE_HEADERS, build_settings
from test_mobile_task_api import FakeMobileTaskRuntime


def _build_app(tmp_path: Path, *, runtime_mode: str) -> FastAPI:
    settings = replace(build_settings(tmp_path), runtime_mode=runtime_mode)
    return create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        mobile_task_runtime=FakeMobileTaskRuntime(),
    )


def test_create_app_exposes_runtime_mode_guard(tmp_path: Path) -> None:
    """create_app 在 app.state 暴露与配置一致的 RuntimeModeGuard。"""
    app = _build_app(tmp_path, runtime_mode="draining")
    guard = app.state.runtime_mode_guard
    assert guard.mode == "draining"
    assert guard.is_legacy_writable() is False
    assert guard.is_legacy_runtime_available() is True
    assert guard.is_kernel_active() is False


def test_legacy_mode_all_write_endpoints_available(tmp_path: Path) -> None:
    """LEGACY：三个写端点均可用。"""
    app = _build_app(tmp_path, runtime_mode="legacy")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tasks",
            headers=WRITE_HEADERS,
            json={"goal": "完成日常任务", "client_request_id": "req-1"},
        )
        sent = client.post(
            "/api/v1/tasks/task-1/inputs",
            headers=WRITE_HEADERS,
            json={"content": "先关掉弹窗", "client_request_id": "in-1"},
        )
        stopped = client.post(
            "/api/v1/tasks/task-1/stop",
            headers=WRITE_HEADERS,
            json={"client_request_id": "stop-1"},
        )

    assert created.status_code == 202
    assert sent.status_code == 202
    assert stopped.status_code == 202


def test_draining_mode_blocks_new_tasks_allows_inflight(tmp_path: Path) -> None:
    """DRAINING：拒绝新建，存量输入/停止仍可用。"""
    app = _build_app(tmp_path, runtime_mode="draining")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tasks",
            headers=WRITE_HEADERS,
            json={"goal": "完成日常任务", "client_request_id": "req-1"},
        )
        sent = client.post(
            "/api/v1/tasks/task-1/inputs",
            headers=WRITE_HEADERS,
            json={"content": "先关掉弹窗", "client_request_id": "in-1"},
        )
        stopped = client.post(
            "/api/v1/tasks/task-1/stop",
            headers=WRITE_HEADERS,
            json={"client_request_id": "stop-1"},
        )

    assert created.status_code == 403
    assert created.json()["error"]["code"] == "LEGACY_TASK_WRITE_DISABLED"
    assert sent.status_code == 202
    assert stopped.status_code == 202


def test_kernel_active_mode_blocks_all_legacy_writes(tmp_path: Path) -> None:
    """KERNEL_ACTIVE：三个写端点全部 403，只读归档端点仍 200。"""
    app = _build_app(tmp_path, runtime_mode="kernel_active")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tasks",
            headers=WRITE_HEADERS,
            json={"goal": "完成日常任务", "client_request_id": "req-1"},
        )
        sent = client.post(
            "/api/v1/tasks/task-1/inputs",
            headers=WRITE_HEADERS,
            json={"content": "先关掉弹窗", "client_request_id": "in-1"},
        )
        stopped = client.post(
            "/api/v1/tasks/task-1/stop",
            headers=WRITE_HEADERS,
            json={"client_request_id": "stop-1"},
        )
        listed = client.get("/api/v1/tasks")
        inspected = client.get("/api/v1/tasks/task-1")

    assert created.status_code == 403
    assert created.json()["error"]["code"] == "LEGACY_TASK_WRITE_DISABLED"
    assert sent.status_code == 403
    assert sent.json()["error"]["code"] == "LEGACY_TASK_WRITE_DISABLED"
    assert stopped.status_code == 403
    assert stopped.json()["error"]["code"] == "LEGACY_TASK_WRITE_DISABLED"
    # 只读归档端点在任何模式下都保持可用（数据保留为只读归档）
    assert listed.status_code == 200
    assert inspected.status_code == 200


@pytest.mark.parametrize("runtime_mode", ["legacy", "draining", "kernel_active"])
def test_events_deprecation_header_in_all_modes(
    tmp_path: Path, runtime_mode: str
) -> None:
    """GET /events 在三种模式下均 200 且携带 Deprecation: true 头。"""
    app = _build_app(tmp_path, runtime_mode=runtime_mode)
    with TestClient(app) as client:
        events = client.get("/api/v1/events")

    assert events.status_code == 200
    assert events.headers.get("Deprecation") == "true"
