"""Phase 5 Week 7: 端到端集成测试

测试完整的 Lease 生命周期和恢复流程：
1. 进程崩溃恢复（孤立 Lease）
2. Lease 过期清理
3. Checkpoint 去重
4. 多 Task 并发执行
5. 后台清理线程稳定性
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ai_game_console.device_lease_manager import DeviceLeaseManager
from ai_game_console.runtime_adapters.sqlite.store import SQLiteRuntimeStore
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_kernel.action import ActionStatus, ActionType
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

    def execute(self, action, device_id: str) -> ActionExecutionResult:
        return ActionExecutionResult(
            success=True,
            stdout=f"Executed {action.type.value} on {device_id}",
            stderr="",
        )


def _clock() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clock_offset(offset_seconds: int) -> str:
    """返回偏移指定秒数的时间"""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def test_orphaned_lease_recovery_integration(tmp_path: Path) -> None:
    """集成测试：进程崩溃后孤立 Lease 自动恢复
    
    场景：
    1. Task 获取 Lease 并 propose Action
    2. 进程崩溃（模拟为不存在的 PID）
    3. 后台清理检测到孤立 Lease
    4. 自动创建 Checkpoint 并释放 Lease
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

    device_id = "test-device"
    
    # 1. 创建 Task 和 Stage
    task = kernel.create_task(
        device_id=device_id,
        goal="Test orphaned lease recovery",
        source=TaskSource(
            client_id="test-client",
            conversation_id="test-conv",
            initial_message_id="test-msg",
        ),
    )
    
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Execute tap",
        completion_criteria=("tap done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    
    # 2. Capture observation and propose action
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
    
    # 3. 手动创建一个过期的孤立 Lease（模拟进程崩溃）
    expired_at = _clock_offset(-120)  # 2 分钟前过期
    lease_id = str(uuid4())
    
    store.acquire_lease(
        lease_id=lease_id,
        device_id=device_id,
        task_id=task.id,
        holder_process_id="999999",  # 不存在的进程
        ttl_seconds=60,
        acquired_at=_clock_offset(-180),  # 3 分钟前获取
    )
    
    # 关联 Action 到 Lease
    store.update_lease_action(lease_id, action.id)
    
    # 4. 启动后台清理
    lease_manager.start_background_cleanup(interval_seconds=1)
    
    # 等待清理执行（最多 3 秒）
    max_wait = 3.0
    start = time.time()
    checkpoint_created = False
    
    while time.time() - start < max_wait:
        checkpoint = store.latest_checkpoint(task.id)
        if checkpoint and checkpoint.unresolved_action_ref == action.id:
            checkpoint_created = True
            break
        time.sleep(0.2)
    
    # 5. 验证结果
    assert checkpoint_created, "Checkpoint should be created for orphaned lease"
    
    # 验证 Checkpoint 内容
    recovery_checkpoint = store.latest_checkpoint(task.id)
    assert recovery_checkpoint is not None
    assert recovery_checkpoint.task_id == task.id
    assert recovery_checkpoint.current_stage_id == stage.id
    assert recovery_checkpoint.unresolved_action_ref == action.id
    assert recovery_checkpoint.resume_reason == "process_crash_during_lease_hold"
    assert recovery_checkpoint.required_fresh_observation is True
    
    # 验证 Lease 被释放
    expired_leases = store.list_expired_leases(_clock())
    assert not any(lease.id == lease_id for lease in expired_leases)
    
    # Cleanup
    kernel.shutdown()


def test_expired_lease_cleanup_with_live_process(tmp_path: Path) -> None:
    """集成测试：Lease 过期但进程仍存活的情况
    
    场景：
    1. Task 持有 Lease
    2. Lease 过期但进程还活着（可能是续期失败）
    3. 后台清理检测到过期 Lease
    4. 记录警告并释放 Lease
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

    device_id = "test-device"
    
    # 创建 Task
    task = kernel.create_task(
        device_id=device_id,
        goal="Test expired lease cleanup",
        source=TaskSource(
            client_id="test-client",
            conversation_id="test-conv",
            initial_message_id="test-msg",
        ),
    )
    
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Test objective",
        completion_criteria=("test done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    
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
    
    # 创建过期 Lease，使用当前进程 PID（模拟续期失败）
    import os
    expired_at = _clock_offset(-120)  # 2 分钟前过期
    lease_id = str(uuid4())
    
    store.acquire_lease(
        lease_id=lease_id,
        device_id=device_id,
        task_id=task.id,
        holder_process_id=str(os.getpid()),  # 当前进程（存活）
        ttl_seconds=60,
        acquired_at=_clock_offset(-180),
    )
    
    store.update_lease_action(lease_id, action.id)
    
    # 启动后台清理
    lease_manager.start_background_cleanup(interval_seconds=1)
    
    # 等待清理执行
    max_wait = 3.0
    start = time.time()
    lease_released = False
    
    while time.time() - start < max_wait:
        expired_leases = store.list_expired_leases(_clock())
        if not any(lease.id == lease_id for lease in expired_leases):
            lease_released = True
            break
        time.sleep(0.2)
    
    # 验证 Lease 被释放（即使进程存活）
    assert lease_released, "Expired lease should be released even if process is alive"
    
    # Cleanup
    kernel.shutdown()


def test_checkpoint_deduplication_on_repeated_cleanup(tmp_path: Path) -> None:
    """集成测试：重复清理不会创建重复 Checkpoint
    
    场景：
    1. 创建孤立 Lease
    2. 第一次清理创建 Checkpoint
    3. 第二次清理检测到已存在 Checkpoint，跳过
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

    device_id = "test-device"
    
    # 创建 Task 和 Action
    task = kernel.create_task(
        device_id=device_id,
        goal="Test checkpoint deduplication",
        source=TaskSource(
            client_id="test-client",
            conversation_id="test-conv",
            initial_message_id="test-msg",
        ),
    )
    
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Test objective",
        completion_criteria=("test done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    
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
    
    # 创建孤立 Lease
    expired_at = _clock_offset(-120)
    lease_id = str(uuid4())
    
    store.acquire_lease(
        lease_id=lease_id,
        device_id=device_id,
        task_id=task.id,
        holder_process_id="999999",
        ttl_seconds=60,
        acquired_at=_clock_offset(-180),
    )
    
    store.update_lease_action(lease_id, action.id)
    
    # 第一次清理（应该创建 Checkpoint）
    lease_manager._cleanup_orphaned_leases()
    
    checkpoint_after_first = store.latest_checkpoint(task.id)
    assert checkpoint_after_first is not None, "First cleanup should create checkpoint"
    assert checkpoint_after_first.unresolved_action_ref == action.id
    
    # 第二次清理（不应该创建重复 Checkpoint，因为 Lease 已释放）
    # 为了测试去重逻辑，我们需要再次创建相同的孤立 Lease
    lease_id_2 = str(uuid4())
    store.acquire_lease(
        lease_id=lease_id_2,
        device_id=device_id,
        task_id=task.id,
        holder_process_id="999998",
        ttl_seconds=60,
        acquired_at=_clock_offset(-180),
    )
    store.update_lease_action(lease_id_2, action.id)
    
    # 第二次清理
    lease_manager._cleanup_orphaned_leases()
    
    checkpoint_after_second = store.latest_checkpoint(task.id)
    
    # 应该仍然是同一个 Checkpoint（去重生效）
    assert checkpoint_after_second.id == checkpoint_after_first.id, \
        "Second cleanup should not create duplicate checkpoint"
    
    # Cleanup
    kernel.shutdown()
