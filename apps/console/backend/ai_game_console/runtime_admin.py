"""Week 6: Runtime Lease 管理 API。

提供 Lease 状态查询（列表/详情/统计）和手动干预（强制释放/触发清理），
复用 Kernel 的 runtime.db（data_dir/runtime/runtime.db）。

设计要点：
- 懒初始化：create_app 构造时不创建数据库文件，首次访问管理端点才初始化
  store 与 manager（避免破坏现有 create_app 测试的无副作用约定）。
- 路由挂在 /api/v1/runtime/leases 下，POST 自动受写保护中间件约束
  （X-AI-Game-Client: console-v1）。
- 后台清理线程：默认随首次初始化启动（Week 4 能力），测试可关闭。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .device_lease_manager import DeviceLeaseManager
from .runtime_adapters.sqlite import SQLiteRuntimeStore
from .runtime_kernel.lease import DeviceExecutionLease

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """当前 UTC 时间（ISO 8601，+00:00 偏移，与 Kernel 时间戳格式一致）"""
    return datetime.now(timezone.utc).isoformat()


class LeaseAdminService:
    """Lease 管理服务：懒初始化 + 线程安全的 store/manager 访问"""

    def __init__(
        self,
        database_path: Path,
        *,
        background_cleanup: bool = True,
        cleanup_interval_seconds: int = 30,
    ) -> None:
        self._database_path = database_path
        self._background_cleanup = background_cleanup
        self._cleanup_interval_seconds = cleanup_interval_seconds
        self._lock = Lock()
        self._store: SQLiteRuntimeStore | None = None
        self._manager: DeviceLeaseManager | None = None

    def ensure_ready(self) -> tuple[SQLiteRuntimeStore, DeviceLeaseManager]:
        """首次调用时初始化 store/manager；之后返回同一实例"""
        with self._lock:
            if self._store is None or self._manager is None:
                self._database_path.parent.mkdir(parents=True, exist_ok=True)
                store = SQLiteRuntimeStore(self._database_path)
                store.initialize()
                manager = DeviceLeaseManager(store, _utc_now_iso)
                if self._background_cleanup:
                    manager.start_background_cleanup(
                        interval_seconds=self._cleanup_interval_seconds
                    )
                self._store = store
                self._manager = manager
                logger.info(
                    "Lease admin ready (db=%s, background_cleanup=%s)",
                    self._database_path,
                    self._background_cleanup,
                )
            assert self._store is not None and self._manager is not None
            return self._store, self._manager

    def shutdown(self) -> None:
        """停止后台清理并关闭数据库连接；未初始化时为空操作"""
        with self._lock:
            if self._manager is not None:
                self._manager.stop_background_cleanup()
            if self._store is not None:
                self._store.close()
            self._store = None
            self._manager = None


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """与 console 错误载荷约定一致：顶层 {"error": {"code", "message"}}"""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _lease_payload(
    lease: DeviceExecutionLease,
    manager: DeviceLeaseManager,
    now: str,
) -> dict[str, Any]:
    """Lease → JSON 载荷（含派生状态字段）"""
    return {
        "lease_id": lease.id,
        "device_id": lease.device_id,
        "task_id": lease.task_id,
        "holder_process_id": lease.holder_process_id,
        "acquired_at": lease.acquired_at,
        "expires_at": lease.expires_at,
        "deadline_at": lease.deadline_at,
        "last_heartbeat_at": lease.last_heartbeat_at,
        "action_id": lease.action_id,
        "status": "expired" if lease.is_expired(now) else "active",
        "deadline_exceeded": lease.is_deadline_exceeded(now),
        "holder_process_alive": manager.is_process_alive(lease.holder_process_id),
    }


def create_lease_admin_router(admin: LeaseAdminService) -> APIRouter:
    """Lease 管理路由（前缀 /api/v1/runtime/leases）"""
    router = APIRouter(prefix="/api/v1/runtime/leases", tags=["runtime-leases"])

    @router.get("")
    def list_leases(
        device_id: str | None = Query(default=None),
        task_id: str | None = Query(default=None),
    ):
        store, manager = admin.ensure_ready()
        now = _utc_now_iso()
        leases = store.list_leases(device_id=device_id, task_id=task_id)
        return {
            "now": now,
            "count": len(leases),
            "leases": [_lease_payload(lease, manager, now) for lease in leases],
        }

    # 注意：/stats 必须在 /{lease_id} 之前注册，避免被路径参数吞掉
    @router.get("/stats")
    def lease_stats():
        store, manager = admin.ensure_ready()
        now = _utc_now_iso()
        stats = store.lease_stats(now)
        return {
            "now": now,
            "stats": {
                "total": stats.total,
                "active": stats.active,
                "expired": stats.expired,
                "deadline_exceeded": stats.deadline_exceeded,
                "avg_current_hold_seconds": stats.avg_current_hold_seconds,
            },
            "cleanup": manager.cleanup_stats(),
        }

    @router.get("/{lease_id}")
    def get_lease(lease_id: str):
        store, manager = admin.ensure_ready()
        now = _utc_now_iso()
        lease = store.get_lease(lease_id)
        if lease is None:
            return _error_response(404, "lease_not_found", f"未找到 Lease：{lease_id}")
        return _lease_payload(lease, manager, now)

    @router.post("/{lease_id}/release")
    def release_lease(lease_id: str):
        _, manager = admin.ensure_ready()
        result = manager.force_release(lease_id)
        if not result["found"]:
            return _error_response(404, "lease_not_found", f"未找到 Lease：{lease_id}")
        if not result["released"]:
            return _error_response(
                500, "lease_release_failed", f"强制释放失败：{result['error']}"
            )
        return result

    @router.post("/cleanup")
    def trigger_cleanup():
        _, manager = admin.ensure_ready()
        return manager.trigger_cleanup()

    return router
