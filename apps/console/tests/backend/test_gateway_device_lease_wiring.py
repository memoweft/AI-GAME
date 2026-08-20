"""Phase 7 切流接线: Kernel Lease → Gateway 设备独占最后一公里。

覆盖:
- ``RuntimeKernel.active_leased_device_ids`` 只列出未过期 Lease 持有的设备;
- ``AdbDeviceRegistry`` 以 Kernel 叠加层把已租设备标记 ``IN_USE``;
- ``TaskGateway.create_task`` 对已租设备抛 ``DeviceNotAvailable``,
  HTTP 层返回 409 ``DEVICE_NOT_AVAILABLE`` (retryable);
- ``build_gateway_composition`` 默认组合把 Kernel 叠加层真正接入
  ``AdbDeviceRegistry`` (白盒断言绑定关系)。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.discovery import AdbDevice, AdbDiscoveryResult
from ai_game_console.domain import TargetStatus
from ai_game_console.gateway import (
    DeviceNotAvailable,
    GatewayStore,
    IdempotencyService,
    TaskGateway,
)
from ai_game_console.gateway_api import GatewayComposition, build_gateway_composition
from ai_game_console.runtime_adapters.android import AdbDeviceRegistry
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import RuntimeKernel, TaskSource

from conftest import WRITE_HEADERS, build_settings

TIMES = tuple(f"2026-08-19T10:{minute:02d}:00+00:00" for minute in range(60))
SERIAL = "EMU56O7F7"
DEVICE_ID = f"adb:{SERIAL}"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class FakeAdbDiscovery:
    """只发现一台 READY 的 mumu 模拟器, 不触碰真实 adb。"""

    def __init__(self, serial: str = SERIAL) -> None:
        self._device = AdbDevice(
            serial=serial,
            raw_state="device",
            status=TargetStatus.READY,
            properties={},
        )

    def discover(self) -> AdbDiscoveryResult:
        return AdbDiscoveryResult(
            status="ready",
            adb_path="adb",
            message="fake discovery",
            devices=(self._device,),
            targets=(),
        )


def _clock() -> Callable[[], str]:
    values = iter(TIMES * 20)
    return lambda: next(values)


def _ids() -> Callable[[], str]:
    return lambda: str(uuid4())


def _kernel(
    tmp_path: Path,
) -> tuple[RuntimeKernel, SQLiteRuntimeStore, Callable[[], str]]:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    clock = _clock()
    kernel = RuntimeKernel(store, clock=clock, id_factory=_ids())
    return kernel, store, clock


def _acquire_lease(
    kernel: RuntimeKernel,
    store: SQLiteRuntimeStore,
    clock: Callable[[], str],
    *,
    device_id: str = DEVICE_ID,
    ttl_seconds: int = 3600,
):
    """新建 Kernel Task 并在目标设备上取得 Lease (与 kernel.execute_action 同路径)。"""
    task = kernel.create_task(
        goal="lease wiring test",
        source=TaskSource(
            client_id="client-lease",
            conversation_id=f"conversation-{uuid4()}",
            initial_message_id="message-lease",
        ),
        device_id=device_id,
    )
    return store.acquire_lease(
        device_id=device_id,
        task_id=task.id,
        holder_process_id=str(os.getpid()),
        ttl_seconds=ttl_seconds,
        lease_id=str(uuid4()),
        acquired_at=clock(),
        # store 约束 deadline >= expires: 长 TTL 时同步放宽绝对截止
        deadline_seconds=max(300, ttl_seconds + 300),
    )


def _registry(kernel: RuntimeKernel) -> AdbDeviceRegistry:
    return AdbDeviceRegistry(
        FakeAdbDiscovery(),
        active_device_ids=kernel.active_leased_device_ids,
    )


# ---------------------------------------------------------------------------
# RuntimeKernel.active_leased_device_ids
# ---------------------------------------------------------------------------


class TestKernelActiveLeasedDeviceIds:
    def test_empty_without_leases(self, tmp_path: Path) -> None:
        kernel, _, _ = _kernel(tmp_path)

        assert kernel.active_leased_device_ids() == frozenset()

    def test_lists_unexpired_leases(self, tmp_path: Path) -> None:
        kernel, store, clock = _kernel(tmp_path)

        lease = _acquire_lease(kernel, store, clock, ttl_seconds=3600)

        assert kernel.active_leased_device_ids() == {lease.device_id}

    def test_excludes_released_leases(self, tmp_path: Path) -> None:
        kernel, store, clock = _kernel(tmp_path)
        lease = _acquire_lease(kernel, store, clock)

        store.release_lease(lease.id)

        assert kernel.active_leased_device_ids() == frozenset()

    def test_excludes_expired_leases(self, tmp_path: Path) -> None:
        kernel, store, clock = _kernel(tmp_path)
        lease = _acquire_lease(kernel, store, clock, ttl_seconds=60)

        # 时钟步进到 expires_at 之后 (分钟粒度时钟, 60s TTL → 下一个 tick 即过期)
        while not lease.is_expired(clock()):
            pass

        assert kernel.active_leased_device_ids() == frozenset()


# ---------------------------------------------------------------------------
# AdbDeviceRegistry 叠加层
# ---------------------------------------------------------------------------


class TestRegistryLeaseOverlay:
    def test_leased_device_reported_in_use(self, tmp_path: Path) -> None:
        kernel, store, clock = _kernel(tmp_path)
        registry = _registry(kernel)

        lease = _acquire_lease(kernel, store, clock)

        devices = {summary.device_id: summary.status for summary in registry.list_devices()}
        assert devices == {DEVICE_ID: "IN_USE"}
        # 规范 id 与裸 serial 两种查询形态都命中叠加层
        assert registry.get_device(DEVICE_ID).status == "IN_USE"
        assert registry.get_device(SERIAL).status == "IN_USE"

        store.release_lease(lease.id)
        assert registry.list_devices()[0].status == "AVAILABLE"

    def test_unknown_device_not_masked_by_overlay(self, tmp_path: Path) -> None:
        kernel, _, _ = _kernel(tmp_path)
        registry = _registry(kernel)

        assert registry.get_device("adb:other-serial") is None


# ---------------------------------------------------------------------------
# TaskGateway 服务层
# ---------------------------------------------------------------------------


def _gateway(
    tmp_path: Path,
) -> tuple[TaskGateway, RuntimeKernel, SQLiteRuntimeStore, Callable[[], str]]:
    kernel, store, clock = _kernel(tmp_path)
    gw_store = GatewayStore(tmp_path / "gateway.db")
    gw_store.initialize()
    gateway = TaskGateway(
        kernel=kernel,
        idempotency=IdempotencyService(gw_store),
        device_registry=_registry(kernel),
    )
    return gateway, kernel, store, clock


def _create(
    gateway: TaskGateway, *, key: str, device_id: str = DEVICE_ID
) -> dict:
    return gateway.create_task(
        client_id="client-1",
        goal="open the game",
        device_id=device_id,
        conversation_id=f"conversation-{uuid4()}",
        message_id="message-1",
        idempotency_key=key,
    )


class TestGatewayLeaseWiring:
    def test_create_task_on_leased_device_rejected(self, tmp_path: Path) -> None:
        gateway, kernel, store, clock = _gateway(tmp_path)
        lease = _acquire_lease(kernel, store, clock)

        with pytest.raises(DeviceNotAvailable):
            _create(gateway, key="k1")

    def test_create_task_succeeds_after_release(self, tmp_path: Path) -> None:
        gateway, kernel, store, clock = _gateway(tmp_path)
        lease = _acquire_lease(kernel, store, clock)
        store.release_lease(lease.id)

        response = _create(gateway, key="k1")

        assert response["task"]["device_id"] == DEVICE_ID
        assert response["task"]["status"] == "CREATED"


# ---------------------------------------------------------------------------
# HTTP 层 (完整挂载, 真实 AdbDeviceRegistry + Kernel 叠加层)
# ---------------------------------------------------------------------------


def _headers(key: str) -> dict[str, str]:
    return {**WRITE_HEADERS, "Idempotency-Key": key, "X-Client-Id": "client-1"}


class TestHttpLeaseWiring:
    def test_create_task_on_leased_device_returns_409(self, tmp_path: Path) -> None:
        kernel, store, clock = _kernel(tmp_path)
        gw_store = GatewayStore(tmp_path / "gateway.db")
        gw_store.initialize()
        registry = _registry(kernel)
        composition = GatewayComposition(
            kernel=kernel,
            gateway=TaskGateway(
                kernel=kernel,
                idempotency=IdempotencyService(gw_store),
                device_registry=registry,
            ),
            device_registry=registry,
            store=gw_store,
        )
        app = create_app(
            settings=build_settings(tmp_path),
            adb_discovery=FakeAdbDiscovery(),
            gateway=composition,
        )
        lease = _acquire_lease(kernel, store, clock)

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/tasks",
                json={
                    "goal": "open the game",
                    "device_id": DEVICE_ID,
                    "conversation_id": f"conversation-{uuid4()}",
                    "message_id": "message-1",
                },
                headers=_headers("k1"),
            )

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "DEVICE_NOT_AVAILABLE"
        assert error["retryable"] is True
        store.release_lease(lease.id)


# ---------------------------------------------------------------------------
# build_gateway_composition 生产组合白盒
# ---------------------------------------------------------------------------


class TestProductionCompositionWiring:
    def test_default_registry_binds_kernel_overlay(self, tmp_path: Path) -> None:
        kernel, store, clock = _kernel(tmp_path)
        composition = build_gateway_composition(
            settings=build_settings(tmp_path),
            kernel=kernel,
        )
        try:
            assert isinstance(composition.device_registry, AdbDeviceRegistry)
            # 绑定方法相等: 同一 __self__ (Kernel) + 同一 __func__
            assert (
                composition.device_registry._active_device_ids
                == composition.kernel.active_leased_device_ids
            )
            # 功能验证: Kernel 租用的设备经生产组合的叠加层立即可见
            _acquire_lease(kernel, store, clock)

            assert composition.device_registry._active_device_ids() == {DEVICE_ID}
            assert composition.kernel.active_leased_device_ids() == {DEVICE_ID}
        finally:
            composition.kernel.close()
            composition.store.close()
