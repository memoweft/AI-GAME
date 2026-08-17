from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from ai_game_console.device_lease_manager import DeviceLeaseManager
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import (
    ActionStatus,
    ActionType,
    RuntimeKernel,
    TaskSource,
)

TIMES = tuple(f"2026-08-17T16:{minute:02d}:00+00:00" for minute in range(60))


def _clock() -> Callable[[], str]:
    values = iter(TIMES * 20)
    return lambda: next(values)


def _ids() -> Callable[[], str]:
    import uuid
    return lambda: str(uuid.uuid4())


def _source(suffix: str) -> TaskSource:
    return TaskSource(
        client_id=f"client-{suffix}",
        conversation_id=f"conversation-{suffix}",
        initial_message_id=f"message-{suffix}",
    )


class FakeObservationProvider:
    def capture(self, device_id: str):
        from ai_game_console.runtime_kernel import (
            RawObservation, RawScreenshot, RawUiTree,
            ChannelAvailability, DeviceState, Orientation,
            KeyboardState, ConnectionState, ObservationConsistency,
            ConsistencyStatus,
        )
        return RawObservation(
            device_id=device_id,
            capture_started_at=TIMES[10],
            capture_completed_at=TIMES[12],
            screenshot=RawScreenshot(
                status=ChannelAvailability.AVAILABLE,
                content=b"fake",
                width=1080,
                height=2400,
                captured_at=TIMES[11],
            ),
            ui_tree=RawUiTree(
                status=ChannelAvailability.AVAILABLE,
                content=b"<hierarchy/>",
                captured_at=TIMES[12],
            ),
            device_state=DeviceState(
                status=ChannelAvailability.AVAILABLE,
                foreground_app="com.test",
                screen_size=(1080, 2400),
                orientation=Orientation.PORTRAIT,
                keyboard_state=KeyboardState.HIDDEN,
                connection_state=ConnectionState.CONNECTED,
                captured_at=TIMES[11],
            ),
            consistency=ObservationConsistency(
                status=ConsistencyStatus.CONSISTENT,
                reason=None,
            ),
        )


def test_lease_manager_cleans_expired_leases(tmp_path: Path) -> None:
    """验证 DeviceLeaseManager 清理过期 Lease"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    
    # 创建 Task
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
        id_factory=_ids(),
    )
    task = kernel.create_task(
        goal="Test cleanup",
        source=_source("cleanup"),
        device_id="device-cleanup",
    )
    
    # 创建一个很快过期的 Lease (TTL=1秒)
    lease = store.acquire_lease(
        device_id="device-cleanup",
        task_id=task.id,
        holder_process_id=str(os.getpid()),
        ttl_seconds=1,
        lease_id="lease-cleanup",
        acquired_at=TIMES[0],
    )
    
    # 验证 Lease 存在
    assert store.get_lease_for_device("device-cleanup") is not None
    
    # 创建 Manager 并手动触发清理
    manager = DeviceLeaseManager(store, clock)
    
    # 等待 Lease 过期
    time.sleep(2)
    
    # 手动触发清理
    manager._cleanup_orphaned_leases()
    
    # 验证 Lease 已被清理
    assert store.get_lease_for_device("device-cleanup") is None


def test_lease_manager_creates_checkpoint_for_orphaned_action(tmp_path: Path) -> None:
    """验证为孤立 Action 创建恢复 Checkpoint"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    
    # 创建完整的 Task → Stage → Observation → Action
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
        id_factory=_ids(),
    )
    task = kernel.create_task(
        goal="Test orphan",
        source=_source("orphan"),
        device_id="device-orphan",
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Test",
        completion_criteria=("done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    obs = kernel.capture_observation(task_id=task.id, device_id="device-orphan")
    action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs.id,
        action_type=ActionType.TAP,
        params={"x": 100, "y": 100},
        expected_outcome="Test",
        proposed_by_call_id="op",
    )
    
    # 模拟已死进程持有 Lease
    dead_process_id = "999999"  # 不存在的 PID
    lease = store.acquire_lease(
        device_id="device-orphan",
        task_id=task.id,
        holder_process_id=dead_process_id,
        ttl_seconds=1,
        lease_id="lease-orphan",
        acquired_at=TIMES[0],
    )
    store.update_lease_action(lease.id, action.id)
    
    # 等待过期
    time.sleep(2)
    
    # 创建 Manager 并触发清理
    manager = DeviceLeaseManager(store, clock)
    manager._cleanup_orphaned_leases()
    
    # 验证创建了 Checkpoint
    checkpoint = kernel.latest_checkpoint(task.id)
    assert checkpoint is not None
    assert checkpoint.unresolved_action_ref == action.id
    assert checkpoint.required_fresh_observation is True
    assert checkpoint.resume_reason == "process_crash_during_lease_hold"


def test_lease_manager_background_thread_starts_and_stops(tmp_path: Path) -> None:
    """验证后台线程启动和停止"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    
    manager = DeviceLeaseManager(store, _clock())
    
    # 启动后台清理
    manager.start_background_cleanup(interval_seconds=1)
    assert manager._cleanup_thread is not None
    assert manager._cleanup_running is True
    
    # 等待一会儿确保线程运行
    time.sleep(0.5)
    assert manager._cleanup_thread.is_alive()
    
    # 停止
    manager.stop_background_cleanup()
    assert manager._cleanup_thread is None
    assert manager._cleanup_running is False


def test_lease_manager_handles_cleanup_exceptions_gracefully(tmp_path: Path) -> None:
    """验证清理过程中的异常被正确处理"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    
    manager = DeviceLeaseManager(store, _clock())
    
    # Mock list_expired_leases 抛出异常
    original_method = store.list_expired_leases
    store.list_expired_leases = Mock(side_effect=RuntimeError("Test error"))
    
    # 清理应该捕获异常而不崩溃
    manager._cleanup_orphaned_leases()  # 不应该抛异常
    
    # 恢复
    store.list_expired_leases = original_method


def test_lease_manager_is_process_alive_detection(tmp_path: Path) -> None:
    """验证进程存活检测"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    
    manager = DeviceLeaseManager(store, _clock())
    
    # 当前进程应该存活
    assert manager._is_process_alive(str(os.getpid())) is True
    
    # 不存在的进程应该不存活
    assert manager._is_process_alive("999999") is False
    
    # 无效 PID
    assert manager._is_process_alive("invalid") is False
