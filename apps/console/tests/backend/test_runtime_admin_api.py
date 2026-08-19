"""Week 6: Runtime Lease 管理 API 集成测试。

注意：管理端点使用真实当前时钟（_utc_now_iso），因此种子 Lease 的时间戳
基于真实 now 偏移生成，而不是测试假时钟。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.config import Settings
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.runtime_admin import LeaseAdminService
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from conftest import WRITE_HEADERS, build_settings

BASE = "/api/v1/runtime/leases"


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _runtime_db(settings: Settings) -> Path:
    return settings.data_dir / "runtime" / "runtime.db"


def _make_task(
    store: SQLiteRuntimeStore,
    *,
    task_id: str,
    device_id: str,
    suffix: str,
) -> None:
    from ai_game_console.runtime_kernel.task import Task, TaskSource
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor

    task = Task.create(
        task_id=task_id,
        goal=f"Test {suffix}",
        source=TaskSource(
            client_id=f"client-{suffix}",
            conversation_id=f"conversation-{suffix}",
            initial_message_id=f"message-{suffix}",
        ),
        device_id=device_id,
        created_at=_now_iso(),
    )
    event = RuntimeEventDraft(
        id=f"event-{suffix}",
        type="TaskCreated",
        actor=EventActor.RUNTIME,
        payload={},
        correlation_id=task.id,
        created_at=_now_iso(),
    )
    store.create_task(task, event)


def _seed(
    settings: Settings,
    *,
    active: bool = True,
    expired: bool = True,
) -> None:
    """预置 Lease 行：active（真实 now 获取，1 小时 TTL）+ expired（已过期且超 Deadline）"""
    db_path = _runtime_db(settings)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteRuntimeStore(db_path)
    store.initialize()
    try:
        if active:
            _make_task(store, task_id="task-active", device_id="device-active", suffix="active")
            store.acquire_lease(
                device_id="device-active",
                task_id="task-active",
                holder_process_id=str(os.getpid()),
                ttl_seconds=3600,
                lease_id="lease-active",
                acquired_at=_now_iso(),
                deadline_seconds=7200,
            )
        if expired:
            _make_task(store, task_id="task-expired", device_id="device-expired", suffix="expired")
            # 400 秒前获取：ttl=60 已过期，默认 deadline 300s 也已超过
            store.acquire_lease(
                device_id="device-expired",
                task_id="task-expired",
                holder_process_id="999999",
                ttl_seconds=60,
                lease_id="lease-expired",
                acquired_at=_now_iso(-400),
            )
    finally:
        store.close()


@pytest.fixture
def admin_settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture
def admin_client(admin_settings: Settings) -> Iterator[TestClient]:
    admin = LeaseAdminService(
        admin_settings.data_dir / "runtime" / "runtime.db",
        background_cleanup=False,
    )
    app = create_app(
        settings=admin_settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        runtime_admin=admin,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_list_leases_lazy_and_empty(admin_client: TestClient, admin_settings: Settings) -> None:
    """首次请求前不创建数据库文件（懒初始化）；空列表返回 count=0"""
    assert not _runtime_db(admin_settings).exists()

    response = admin_client.get(BASE)
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 0
    assert payload["leases"] == []
    assert "now" in payload

    # 首次请求后才初始化数据库
    assert _runtime_db(admin_settings).exists()


def test_list_leases_returns_status_fields(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    _seed(admin_settings)

    response = admin_client.get(BASE)
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    by_id = {lease["lease_id"]: lease for lease in payload["leases"]}

    active = by_id["lease-active"]
    assert active["device_id"] == "device-active"
    assert active["task_id"] == "task-active"
    assert active["holder_process_id"] == str(os.getpid())
    assert active["status"] == "active"
    assert active["deadline_exceeded"] is False
    assert active["holder_process_alive"] is True

    expired = by_id["lease-expired"]
    assert expired["status"] == "expired"
    assert expired["deadline_exceeded"] is True
    assert expired["holder_process_alive"] is False
    assert expired["action_id"] is None


def test_list_leases_filters(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    _seed(admin_settings)

    response = admin_client.get(BASE, params={"device_id": "device-active"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["leases"][0]["lease_id"] == "lease-active"

    response = admin_client.get(BASE, params={"task_id": "task-expired"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["leases"][0]["lease_id"] == "lease-expired"

    # 组合过滤（跨设备/任务）→ 无匹配
    response = admin_client.get(
        BASE, params={"device_id": "device-active", "task_id": "task-expired"}
    )
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_get_lease_detail_and_not_found(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    _seed(admin_settings)

    response = admin_client.get(f"{BASE}/lease-active")
    assert response.status_code == 200
    payload = response.json()
    assert payload["lease_id"] == "lease-active"
    assert payload["status"] == "active"
    # deadline_at 晚于 expires_at
    assert payload["deadline_at"] > payload["expires_at"]

    response = admin_client.get(f"{BASE}/lease-missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "lease_not_found"


def test_stats_route_not_swallowed_and_counts(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    """GET /stats 必须先于 /{lease_id} 注册，且返回统计 + 清理指标"""
    _seed(admin_settings)

    response = admin_client.get(f"{BASE}/stats")
    assert response.status_code == 200
    payload = response.json()
    stats = payload["stats"]
    assert stats["total"] == 2
    assert stats["active"] == 1
    assert stats["expired"] == 1
    assert stats["deadline_exceeded"] == 1
    assert stats["avg_current_hold_seconds"] is not None
    # 平均持有 ≈ (0 + 400) / 2 = 200s（± 请求耗时容差）
    assert stats["avg_current_hold_seconds"] == pytest.approx(200.0, abs=30.0)
    assert payload["cleanup"]["runs"] >= 0
    assert payload["cleanup"]["errors"] == 0


def test_release_requires_write_header(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    _seed(admin_settings)

    response = admin_client.post(f"{BASE}/lease-expired/release")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "console_client_required"


def test_release_lease_removes_row(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    _seed(admin_settings)

    response = admin_client.post(
        f"{BASE}/lease-expired/release", headers=WRITE_HEADERS
    )
    assert response.status_code == 200
    result = response.json()
    assert result["found"] is True
    assert result["released"] is True
    assert result["checkpointed"] is False  # 未关联 Action
    assert result["error"] is None

    # 释放后行已删除
    assert admin_client.get(f"{BASE}/lease-expired").status_code == 404
    payload = admin_client.get(BASE).json()
    assert payload["count"] == 1
    assert payload["leases"][0]["lease_id"] == "lease-active"


def test_release_missing_lease_404(admin_client: TestClient) -> None:
    response = admin_client.post(
        f"{BASE}/lease-missing/release", headers=WRITE_HEADERS
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "lease_not_found"


def test_trigger_cleanup_releases_expired(
    admin_client: TestClient, admin_settings: Settings
) -> None:
    """手动触发清理：过期且超 Deadline 的 Lease 被释放（无论持有进程是否存活）"""
    _seed(admin_settings)

    # 无写保护头 → 403
    response = admin_client.post(f"{BASE}/cleanup")
    assert response.status_code == 403

    response = admin_client.post(f"{BASE}/cleanup", headers=WRITE_HEADERS)
    assert response.status_code == 200
    summary = response.json()
    assert summary["expired_found"] == 1
    assert summary["deadline_exceeded"] == 1
    assert summary["released"] == 1
    assert summary["checkpointed"] == 0
    assert summary["errors"] == 0

    # 清理后只剩活跃 Lease
    payload = admin_client.get(BASE).json()
    assert payload["count"] == 1
    assert payload["leases"][0]["lease_id"] == "lease-active"

    # 再次清理：无过期 Lease，runs 计数递增
    response = admin_client.post(f"{BASE}/cleanup", headers=WRITE_HEADERS)
    assert response.status_code == 200
    assert response.json()["expired_found"] == 0

    stats = admin_client.get(f"{BASE}/stats").json()
    assert stats["cleanup"]["runs"] >= 2
    assert stats["cleanup"]["errors"] == 0
