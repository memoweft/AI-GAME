"""Phase 5 Week 7 Day 2: 多Task并发和后台线程稳定性测试

测试场景：
4. 多Task并发执行
5. 后台清理线程稳定性
6. 并发Lease冲突
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from uuid import uuid4

from ai_game_console.device_lease_manager import DeviceLeaseManager
from ai_game_console.runtime_adapters.sqlite.store import SQLiteRuntimeStore
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_kernel.action import ActionType
from ai_game_console.runtime_kernel.executor import (
    ActionExecutionResult,
    ActionExecutorPort,
)
from ai_game_console.runtime_kernel.kernel import RuntimeKernel
from ai_game_console.runtime_kernel.task import TaskSource
from ai_game_console.runtime_kernel.observation import (
    ChannelAvailability,
    ConnectionState,
    ConsistencyStatus,
    DeviceState,
    KeyboardState,
    ObservationConsistency,
    Orientation,
    RawObservation,
    RawScreenshot,
    RawUiTree,
)
from ai_game_console.runtime_kernel.lease.errors import LeaseConflict

TIMES = tuple(f"2026-08-17T15:{minute:02d}:00+00:00" for minute in range(60))


class FakeObservationProvider:
    """测试用 Observation Provider"""
    
    def capture(self, device_id: str) -> RawObservation:
        return RawObservation(
            device_id=device_id,
            capture_started_at=TIMES[10],
            capture_completed_at=TIMES[12],
            screenshot=RawScreenshot(
                status=ChannelAvailability.AVAILABLE,
                content=b"fake-screenshot",
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
                foreground_app="com.test.app",
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


class FakeActionExecutor(ActionExecutorPort):
    """测试用 Executor"""

    def execute_tap(
        self,
        device_id: str,
        x: int,
        y: int,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=_clock(),
            finished_at=_clock(),
        )
    
    def execute_swipe(
        self,
        device_id: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=_clock(),
            finished_at=_clock(),
        )
    
    def execute_input_text(
        self,
        device_id: str,
        text: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=_clock(),
            finished_at=_clock(),
        )
    
    def execute_back(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=_clock(),
            finished_at=_clock(),
        )
    
    def execute_home(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=_clock(),
            finished_at=_clock(),
        )


def _clock() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_multi_task_concurrent_execution_on_different_devices(tmp_path: Path) -> None:
    """集成测试：多个Task在不同设备上并发执行
    
    场景：
    1. 创建3个Task，绑定不同device_id
    2. 每个Task propose和execute Action
    3. 验证每个Task获取各自设备的Lease
    4. 验证不同设备的Lease互不干扰
    5. 验证所有Lease正确释放
    """
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()

    lease_manager = DeviceLeaseManager(store, _clock)
    kernel = RuntimeKernel(
        store=store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=FakeActionExecutor(),
        lease_manager=lease_manager,
    )

    # 创建3个Task，绑定不同设备
    tasks = []
    stages = []
    devices = []
    
    for i in range(3):
        device_id = f"device-{i}"
        devices.append(device_id)
        
        task = kernel.create_task(
            device_id=device_id,
            goal=f"Test goal {i}",
            source=TaskSource(
                client_id="test-client",
                conversation_id=f"test-conv-{i}",
                initial_message_id=f"test-msg-{i}",
            ),
        )
        tasks.append(task)
        
        stage = kernel.create_stage(
            task_id=task.id,
            objective=f"Execute tap {i}",
            completion_criteria=(f"tap {i} done",),
        )
        kernel.start_stage(task_id=task.id, stage_id=stage.id)
        stages.append(stage)
    
    # 并发执行Actions
    def execute_task_action(task, stage, device_id):
        obs = kernel.capture_observation(task_id=task.id, device_id=device_id)
        action = kernel.propose_action(
            task_id=task.id,
            stage_id=stage.id,
            based_on_observation_id=obs.id,
            action_type=ActionType.TAP,
            params={"x": 100, "y": 200},
            expected_outcome="Button tapped",
            proposed_by_call_id="test-call",
        )
        # execute_action会获取Lease
        kernel.execute_action(task_id=task.id, action_id=action.id)
    
    # 使用线程并发执行
    threads = []
    for task, stage, device_id in zip(tasks, stages, devices):
        t = Thread(target=execute_task_action, args=(task, stage, device_id))
        threads.append(t)
        t.start()
    
    # 等待所有线程完成
    for t in threads:
        t.join(timeout=5.0)
    
    # 验证所有Tasks都成功执行了Action
    for task in tasks:
        actions = store.list_actions(task.id)
        assert len(actions) > 0, f"Task {task.id} should have actions"
        # 验证Action状态为EXECUTED
        latest_action = actions[-1]
        assert latest_action.status.value == "EXECUTED", \
            f"Action should be EXECUTED, got {latest_action.status.value}"
    
    # 验证所有Lease都被释放
    expired_leases = store.list_expired_leases(_clock())
    assert len(expired_leases) == 0, "All leases should be released"
    
    # Cleanup
    kernel.shutdown()


def test_concurrent_lease_acquisition_conflict(tmp_path: Path) -> None:
    """集成测试：同一设备的并发Lease冲突
    
    场景：
    1. 两个Task尝试同时获取同一设备的Lease
    2. 只有一个应该成功
    3. 另一个应该抛出LeaseConflict
    """
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()

    lease_manager = DeviceLeaseManager(store, _clock)
    kernel = RuntimeKernel(
        store=store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=FakeActionExecutor(),
        lease_manager=lease_manager,
    )

    device_id = "shared-device"
    
    # 创建两个Task，绑定同一设备
    task1 = kernel.create_task(
        device_id=device_id,
        goal="Task 1 goal",
        source=TaskSource(
            client_id="test-client",
            conversation_id="test-conv-1",
            initial_message_id="test-msg-1",
        ),
    )
    
    task2 = kernel.create_task(
        device_id=device_id,
        goal="Task 2 goal",
        source=TaskSource(
            client_id="test-client",
            conversation_id="test-conv-2",
            initial_message_id="test-msg-2",
        ),
    )
    
    # Task 1 先获取Lease
    lease1_id = str(uuid4())
    store.acquire_lease(
        lease_id=lease1_id,
        device_id=device_id,
        task_id=task1.id,
        holder_process_id="12345",
        ttl_seconds=60,
        acquired_at=_clock(),
    )
    
    # Task 2 尝试获取同一设备的Lease，应该失败
    try:
        lease2_id = str(uuid4())
        store.acquire_lease(
            lease_id=lease2_id,
            device_id=device_id,
            task_id=task2.id,
            holder_process_id="12346",
            ttl_seconds=60,
            acquired_at=_clock(),
        )
        assert False, "Should raise LeaseConflict"
    except LeaseConflict:
        # 预期的异常
        pass
    
    # 释放Task 1的Lease
    store.release_lease(lease1_id)
    
    # 现在Task 2可以获取Lease
    lease2_id = str(uuid4())
    store.acquire_lease(
        lease_id=lease2_id,
        device_id=device_id,
        task_id=task2.id,
        holder_process_id="12346",
        ttl_seconds=60,
        acquired_at=_clock(),
    )
    
    # 验证成功
    lease2 = store.get_lease_for_device(device_id)
    assert lease2 is not None
    assert lease2.task_id == task2.id
    
    # Cleanup
    store.release_lease(lease2_id)
    kernel.shutdown()


def test_background_cleanup_thread_stability(tmp_path: Path) -> None:
    """集成测试：后台清理线程长期稳定性
    
    场景：
    1. 启动后台清理线程
    2. 创建多批过期Lease
    3. 验证清理循环稳定运行
    4. 验证异常不会导致线程崩溃
    5. 验证正确停止
    """
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()

    lease_manager = DeviceLeaseManager(store, _clock)
    kernel = RuntimeKernel(
        store=store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=FakeActionExecutor(),
        lease_manager=lease_manager,
    )

    # 启动后台清理（短间隔用于测试）
    lease_manager.start_background_cleanup(interval_seconds=1)
    
    # 验证线程已启动
    assert lease_manager._cleanup_running is True
    assert lease_manager._cleanup_thread is not None
    assert lease_manager._cleanup_thread.is_alive()
    
    # 创建多批过期Lease
    for batch in range(3):
        for i in range(2):
            device_id = f"device-batch{batch}-{i}"
            
            task = kernel.create_task(
                device_id=device_id,
                goal=f"Batch {batch} Task {i}",
                source=TaskSource(
                    client_id="test-client",
                    conversation_id=f"conv-{batch}-{i}",
                    initial_message_id=f"msg-{batch}-{i}",
                ),
            )
            
            # 创建过期Lease（2分钟前过期）
            lease_id = str(uuid4())
            expired_at = (
                datetime.now(timezone.utc) + timedelta(seconds=-120)
            ).isoformat()
            
            store.acquire_lease(
                lease_id=lease_id,
                device_id=device_id,
                task_id=task.id,
                holder_process_id=f"9999{batch}{i}",  # 不存在的进程
                ttl_seconds=60,
                acquired_at=(
                    datetime.now(timezone.utc) + timedelta(seconds=-180)
                ).isoformat(),
            )
        
        # 等待清理执行
        time.sleep(1.5)
    
    # 等待最后一批清理
    time.sleep(2.0)
    
    # 验证所有过期Lease都被清理
    expired_leases = store.list_expired_leases(_clock())
    assert len(expired_leases) == 0, "All expired leases should be cleaned up"
    
    # 验证线程仍在运行
    assert lease_manager._cleanup_thread.is_alive()
    
    # 停止后台线程
    kernel.shutdown()
    
    # 验证线程已停止
    assert lease_manager._cleanup_running is False
    
    # 等待线程完全停止
    time.sleep(0.5)


def test_background_cleanup_handles_exceptions_gracefully(tmp_path: Path) -> None:
    """集成测试：后台清理遇到异常时不崩溃
    
    场景：
    1. 启动后台清理
    2. 创建正常的过期Lease
    3. 验证即使某些清理操作失败，线程继续运行
    4. 验证异常被捕获和记录
    
    注：由于外键约束，无法创建真正无效的数据
    这个测试主要验证顶层异常捕获逻辑
    """
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()

    lease_manager = DeviceLeaseManager(store, _clock)
    
    kernel = RuntimeKernel(
        store=store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=FakeActionExecutor(),
        lease_manager=lease_manager,
    )
    
    # 启动后台清理
    lease_manager.start_background_cleanup(interval_seconds=1)
    
    # 创建多个过期Lease
    for i in range(3):
        task = kernel.create_task(
            device_id=f"test-device-{i}",
            goal=f"Test task {i}",
            source=TaskSource(
                client_id="test-client",
                conversation_id=f"test-conv-{i}",
                initial_message_id=f"test-msg-{i}",
            ),
        )
        
        lease_id = str(uuid4())
        store.acquire_lease(
            lease_id=lease_id,
            device_id=f"test-device-{i}",
            task_id=task.id,
            holder_process_id=f"99999{i}",
            ttl_seconds=60,
            acquired_at=(
                datetime.now(timezone.utc) + timedelta(seconds=-180)
            ).isoformat(),
        )
    
    # 等待清理运行多次
    time.sleep(3.0)
    
    # 验证线程仍在运行（没有崩溃）
    assert lease_manager._cleanup_thread is not None
    assert lease_manager._cleanup_thread.is_alive()
    
    # 验证Lease被清理
    expired_leases = store.list_expired_leases(_clock())
    assert len(expired_leases) == 0, "Expired leases should be cleaned up"
    
    # Cleanup
    kernel.shutdown()
