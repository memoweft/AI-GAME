"""Phase 5 Week 7 Day 4: 性能基准测试（Lease 生命周期）

基准目标：
1. acquire/renew/release 单操作开销（200 轮 acquire → 3×renew → release）
2. 多设备并发获取规模（50 个设备同时持有 Lease）
3. 后台清理线程空闲 CPU 占用（无 busy-loop 检查，场景 6 的 <1% 目标）

设计说明：
- 断言采用宽松上限（防 busy-loop / 病态慢），防止 CI 抖动导致误报；
  精确测量值通过 print 输出到 stdout（pytest -s），Day 5 测试报告直接引用
- SQLite 文件库使用 pytest tmp_path，测试之间完全隔离
- 后台清理线程使用真实墙钟；CPU 用进程级 os.times() 的 (user+system)
  差值 / 墙钟差值计算，并先 warmup 摊销首次开销
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from uuid import uuid4

from ai_game_console.device_lease_manager import DeviceLeaseManager
from ai_game_console.runtime_adapters.sqlite.store import SQLiteRuntimeStore
from ai_game_console.runtime_kernel.kernel import RuntimeKernel
from ai_game_console.runtime_kernel.task import TaskSource

# 宽松上限（毫秒）：本地 SSD 实测应 <1ms，这些上限只在
# busy-loop / 病态慢（如全表锁等待）时才会触发
MEDIAN_CEILING_MS = 50.0
P99_CEILING_MS = 500.0


def _wall_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(samples: list[float], pct: float) -> float:
    """最近秩百分位（输入单位：毫秒）"""
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    rank = max(1, min(len(ordered), round(pct / 100.0 * len(ordered))))
    return ordered[rank - 1]


def _report(label: str, samples_ms: list[float]) -> None:
    """输出一行基准报告，供 Day 5 测试报告引用"""
    print(
        f"    [bench] {label}: n={len(samples_ms)} "
        f"median={median(samples_ms):.3f}ms "
        f"p99={_percentile(samples_ms, 99):.3f}ms "
        f"max={max(samples_ms):.3f}ms",
        flush=True,
    )


def _make_task(kernel: RuntimeKernel, device_id: str, index: int) -> str:
    task = kernel.create_task(
        device_id=device_id,
        goal=f"perf task {index}",
        source=TaskSource(
            client_id="perf-client",
            conversation_id=f"perf-conv-{index}",
            initial_message_id=f"perf-msg-{index}",
        ),
    )
    return task.id


def test_lease_operation_overhead(tmp_path: Path) -> None:
    """基准：acquire/renew/release 单操作开销（200 轮循环）"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    kernel = RuntimeKernel(store=store)

    cycles = 200
    renews_per_cycle = 3
    device_id = "perf-lease-device"
    task_id = _make_task(kernel, device_id, 0)
    holder = str(os.getpid())

    acquire_ms: list[float] = []
    renew_ms: list[float] = []
    release_ms: list[float] = []

    for cycle in range(cycles):
        acquired_at = _wall_now()

        t0 = time.perf_counter()
        lease = store.acquire_lease(
            device_id=device_id,
            task_id=task_id,
            holder_process_id=holder,
            ttl_seconds=60,
            lease_id=str(uuid4()),
            acquired_at=acquired_at,
        )
        acquire_ms.append((time.perf_counter() - t0) * 1000.0)

        for _ in range(renews_per_cycle):
            t0 = time.perf_counter()
            lease = store.renew_lease(
                lease.id,
                (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            )
            renew_ms.append((time.perf_counter() - t0) * 1000.0)

        t0 = time.perf_counter()
        store.release_lease(lease.id)
        release_ms.append((time.perf_counter() - t0) * 1000.0)

    # 健全性检查：无残留 Lease
    assert store.get_lease_for_device(device_id) is None

    _report("acquire_lease", acquire_ms)
    _report("renew_lease", renew_ms)
    _report("release_lease", release_ms)

    for label, samples in (
        ("acquire", acquire_ms),
        ("renew", renew_ms),
        ("release", release_ms),
    ):
        assert median(samples) < MEDIAN_CEILING_MS, (
            f"{label} median 超出上限: {median(samples):.3f}ms"
        )
        assert _percentile(samples, 99) < P99_CEILING_MS, (
            f"{label} p99 超出上限: {_percentile(samples, 99):.3f}ms"
        )

    kernel.close()


def test_many_device_lease_scale(tmp_path: Path) -> None:
    """基准：50 个设备同时持有 Lease 的获取/释放规模"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    kernel = RuntimeKernel(store=store)

    devices = 50
    holder = str(os.getpid())
    lease_ids: list[str] = []
    acquire_ms: list[float] = []

    for index in range(devices):
        device_id = f"perf-scale-device-{index}"
        task_id = _make_task(kernel, device_id, index)
        t0 = time.perf_counter()
        lease = store.acquire_lease(
            device_id=device_id,
            task_id=task_id,
            holder_process_id=holder,
            ttl_seconds=120,
            lease_id=str(uuid4()),
            acquired_at=_wall_now(),
        )
        acquire_ms.append((time.perf_counter() - t0) * 1000.0)
        lease_ids.append(lease.id)

    # 健全性检查：50 个 Lease 同时活跃，每设备一个
    for index in range(devices):
        assert store.get_lease_for_device(f"perf-scale-device-{index}") is not None

    release_ms: list[float] = []
    for lease_id in lease_ids:
        t0 = time.perf_counter()
        store.release_lease(lease_id)
        release_ms.append((time.perf_counter() - t0) * 1000.0)

    for index in range(devices):
        assert store.get_lease_for_device(f"perf-scale-device-{index}") is None

    _report(f"acquire_lease x{devices} (scale)", acquire_ms)
    _report(f"release_lease x{devices} (scale)", release_ms)
    total_ms = sum(acquire_ms) + sum(release_ms)
    print(
        f"    [bench] scale total: {devices} devices acquired+released "
        f"in {total_ms:.1f}ms",
        flush=True,
    )
    assert median(acquire_ms) < MEDIAN_CEILING_MS
    assert median(release_ms) < MEDIAN_CEILING_MS

    kernel.close()


def test_background_cleanup_cpu_idle(tmp_path: Path) -> None:
    """基准：后台清理线程空闲 CPU 占用（无 busy-loop 检查）

    - 数据库无任何 Lease，清理线程每 1 秒只做一次空查询
    - 3s warmup 摊销线程启动/首次查询开销，10s 测量窗口
    - 进程级 CPU = (user+system 差值) / 墙钟差值
    """
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()

    manager = DeviceLeaseManager(store, _wall_now)
    manager.start_background_cleanup(interval_seconds=1)
    assert manager._cleanup_thread is not None
    assert manager._cleanup_thread.is_alive()

    time.sleep(3.0)  # warmup

    t0 = time.perf_counter()
    cpu0 = os.times()
    time.sleep(10.0)
    t1 = time.perf_counter()
    cpu1 = os.times()

    wall_seconds = t1 - t0
    cpu_seconds = (cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)
    cpu_pct = cpu_seconds / wall_seconds * 100.0

    manager.stop_background_cleanup()
    assert manager._cleanup_running is False

    print(
        f"    [bench] background cleanup (idle, no leases): "
        f"process CPU {cpu_pct:.2f}% over {wall_seconds:.1f}s "
        f"(cpu={cpu_seconds:.3f}s)",
        flush=True,
    )

    # 宽松上限：busy-loop 会接近 100%；正常空闲应 <5%
    # （计划目标 <1%，实测值见 stdout，Day 5 报告引用）
    assert cpu_pct < 5.0, f"后台清理 CPU 占用过高: {cpu_pct:.2f}%"

    store.close()
