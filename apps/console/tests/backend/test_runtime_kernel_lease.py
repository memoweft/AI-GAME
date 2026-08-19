from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import (
    DeviceExecutionLease,
    LeaseConflict,
    LeaseExpired,
    LeaseNotFound,
    TaskSource,
)

TIMES = tuple(f"2026-08-17T14:{minute:02d}:00+00:00" for minute in range(60))


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


def test_acquire_lease_creates_new_lease(tmp_path: Path) -> None:
    """验证 acquire_lease 成功创建新 Lease"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    process_id = str(os.getpid())
    
    # 创建 Task
    from ai_game_console.runtime_kernel.task import Task
    task = Task.create(
        task_id="task-1",
        goal="Test lease",
        source=_source("1"),
        device_id="device-1",
        created_at=clock(),
    )
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor
    event = RuntimeEventDraft(
        id="event-1",
        type="TaskCreated",
        actor=EventActor.RUNTIME,
        payload={},
        correlation_id=task.id,
        created_at=clock(),
    )
    store.create_task(task, event)
    
    # 获取 Lease
    acquired_at = clock()
    lease = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=process_id,
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=acquired_at,
    )
    
    assert lease.id == "lease-1"
    assert lease.device_id == "device-1"
    assert lease.task_id == "task-1"
    assert lease.holder_process_id == process_id
    assert lease.acquired_at == acquired_at
    assert lease.action_id is None
    
    # 验证可以查询到
    retrieved = store.get_lease_for_device("device-1")
    assert retrieved is not None
    assert retrieved.id == lease.id


def test_acquire_lease_rejects_concurrent_acquisition(tmp_path: Path) -> None:
    """验证同一设备不能被两个 Task 同时 acquire"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    process_id = str(os.getpid())
    
    # 创建两个 Task
    from ai_game_console.runtime_kernel.task import Task
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor
    
    for i in [1, 2]:
        task = Task.create(
            task_id=f"task-{i}",
            goal=f"Test lease {i}",
            source=_source(str(i)),
            device_id="device-1",
            created_at=clock(),
        )
        event = RuntimeEventDraft(
            id=f"event-{i}",
            type="TaskCreated",
            actor=EventActor.RUNTIME,
            payload={},
            correlation_id=task.id,
            created_at=clock(),
        )
        store.create_task(task, event)
    
    # 第一个 Task 获取 Lease 成功
    lease1 = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=process_id,
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=clock(),
    )
    assert lease1.task_id == "task-1"
    
    # 第二个 Task 尝试获取同一设备，应该失败
    with pytest.raises(LeaseConflict) as exc_info:
        store.acquire_lease(
            device_id="device-1",
            task_id="task-2",
            holder_process_id=process_id,
            ttl_seconds=60,
            lease_id="lease-2",
            acquired_at=clock(),
        )
    
    assert exc_info.value.device_id == "device-1"
    assert exc_info.value.existing_task_id == "task-1"
    assert exc_info.value.existing_lease_id == "lease-1"
    assert exc_info.value.requested_task_id == "task-2"


def test_release_lease_frees_device(tmp_path: Path) -> None:
    """验证 release_lease 释放设备，允许新 Task acquire"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    process_id = str(os.getpid())
    
    # 创建两个 Task
    from ai_game_console.runtime_kernel.task import Task
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor
    
    for i in [1, 2]:
        task = Task.create(
            task_id=f"task-{i}",
            goal=f"Test lease {i}",
            source=_source(str(i)),
            device_id="device-1",
            created_at=clock(),
        )
        event = RuntimeEventDraft(
            id=f"event-{i}",
            type="TaskCreated",
            actor=EventActor.RUNTIME,
            payload={},
            correlation_id=task.id,
            created_at=clock(),
        )
        store.create_task(task, event)
    
    # Task 1 获取 Lease
    lease1 = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=process_id,
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=clock(),
    )
    
    # 释放 Lease
    store.release_lease(lease1.id)
    
    # 验证设备已释放
    assert store.get_lease_for_device("device-1") is None
    
    # Task 2 现在可以获取
    lease2 = store.acquire_lease(
        device_id="device-1",
        task_id="task-2",
        holder_process_id=process_id,
        ttl_seconds=60,
        lease_id="lease-2",
        acquired_at=clock(),
    )
    assert lease2.task_id == "task-2"


def test_renew_lease_extends_expiration(tmp_path: Path) -> None:
    """验证 renew_lease 延长过期时间"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    process_id = str(os.getpid())
    
    # 创建 Task
    from ai_game_console.runtime_kernel.task import Task
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor
    
    task = Task.create(
        task_id="task-1",
        goal="Test lease renewal",
        source=_source("1"),
        device_id="device-1",
        created_at=clock(),
    )
    event = RuntimeEventDraft(
        id="event-1",
        type="TaskCreated",
        actor=EventActor.RUNTIME,
        payload={},
        correlation_id=task.id,
        created_at=clock(),
    )
    store.create_task(task, event)
    
    # 获取 Lease（显式长 Deadline，验证续期可正常延长 TTL）
    acquired_at = clock()
    lease = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=process_id,
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=acquired_at,
        deadline_seconds=3600,
    )
    
    original_expires_at = lease.expires_at
    
    # 续期
    new_heartbeat_at = clock()
    new_expires_at = TIMES[10]  # 明确的新过期时间
    renewed = store.renew_lease(lease.id, new_expires_at, new_heartbeat_at)
    
    assert renewed.id == lease.id
    assert renewed.expires_at == new_expires_at
    assert renewed.expires_at != original_expires_at
    assert renewed.last_heartbeat_at == new_heartbeat_at


def test_renew_lease_raises_on_nonexistent_lease(tmp_path: Path) -> None:
    """验证续期不存在的 Lease 抛出异常"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    
    with pytest.raises(LeaseNotFound) as exc_info:
        store.renew_lease("nonexistent-lease", TIMES[5], TIMES[5])
    
    assert exc_info.value.lease_id == "nonexistent-lease"


def test_list_expired_leases_returns_expired_only(tmp_path: Path) -> None:
    """验证 list_expired_leases 只返回已过期的 Lease"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    process_id = str(os.getpid())
    
    # 创建两个 Task
    from ai_game_console.runtime_kernel.task import Task
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor
    
    for i in [1, 2]:
        task = Task.create(
            task_id=f"task-{i}",
            goal=f"Test lease {i}",
            source=_source(str(i)),
            device_id=f"device-{i}",
            created_at=clock(),
        )
        event = RuntimeEventDraft(
            id=f"event-{i}",
            type="TaskCreated",
            actor=EventActor.RUNTIME,
            payload={},
            correlation_id=task.id,
            created_at=clock(),
        )
        store.create_task(task, event)
    
    # Lease 1: 很快过期（ttl=1）
    lease1 = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=process_id,
        ttl_seconds=1,
        lease_id="lease-1",
        acquired_at=TIMES[0],
    )
    
    # Lease 2: 较晚过期（ttl=3600，配同等 Deadline 避免 Week 5 默认 300s 截止）
    lease2 = store.acquire_lease(
        device_id="device-2",
        task_id="task-2",
        holder_process_id=process_id,
        ttl_seconds=3600,
        lease_id="lease-2",
        acquired_at=TIMES[1],
        deadline_seconds=3600,
    )
    
    # 在 Lease 1 过期时间后查询
    expired = store.list_expired_leases(TIMES[2])
    
    # 只有 Lease 1 应该过期
    assert len(expired) == 1
    assert expired[0].id == "lease-1"


def test_update_lease_action_tracks_current_action(tmp_path: Path) -> None:
    """验证 update_lease_action 更新 Action 关联"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    process_id = str(os.getpid())
    
    # 创建 Task 和 Stage
    from ai_game_console.runtime_kernel.task import Task
    from ai_game_console.runtime_kernel.stage import Stage
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor
    from ai_game_console.runtime_kernel.observation import (
        RawObservation,
        RawScreenshot,
        RawUiTree,
        ChannelAvailability,
        DeviceState,
        Orientation,
        KeyboardState,
        ConnectionState,
        ObservationConsistency,
        ConsistencyStatus,
    )
    from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
    from ai_game_console.runtime_kernel.kernel import RuntimeKernel
    
    task = Task.create(
        task_id="task-1",
        goal="Test action tracking",
        source=_source("1"),
        device_id="device-1",
        created_at=clock(),
    )
    event = RuntimeEventDraft(
        id="event-1",
        type="TaskCreated",
        actor=EventActor.RUNTIME,
        payload={},
        correlation_id=task.id,
        created_at=clock(),
    )
    store.create_task(task, event)
    
    stage = Stage.create(
        stage_id="stage-1",
        task_id="task-1",
        ordinal=1,
        objective="Test stage",
        completion_criteria=("done",),
        planner_call_id=None,
    )
    stage_event = RuntimeEventDraft(
        id="event-2",
        type="StageCreated",
        actor=EventActor.RUNTIME,
        payload={},
        correlation_id=task.id,
        created_at=clock(),
    )
    store.create_stage(stage, stage_event)
    
    # 准备 fake observation provider
    raw_obs = RawObservation(
        device_id="device-1",
        capture_started_at=clock(),
        capture_completed_at=clock(),
        screenshot=RawScreenshot(
            status=ChannelAvailability.AVAILABLE,
            content=b"fake-screenshot",
            width=1080,
            height=2400,
            captured_at=clock(),
        ),
        ui_tree=RawUiTree(
            status=ChannelAvailability.AVAILABLE,
            content=b"<hierarchy/>",
            captured_at=clock(),
        ),
        device_state=DeviceState(
            status=ChannelAvailability.AVAILABLE,
            foreground_app="com.test",
            screen_size=(1080, 2400),
            orientation=Orientation.PORTRAIT,
            keyboard_state=KeyboardState.HIDDEN,
            connection_state=ConnectionState.CONNECTED,
            captured_at=clock(),
        ),
        consistency=ObservationConsistency(
            status=ConsistencyStatus.CONSISTENT,
            reason=None,
        ),
    )
    
    class FakeObservationProvider:
        def capture(self, device_id: str) -> RawObservation:
            return raw_obs
    
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
        id_factory=_ids(),
    )
    
    # 启动 Stage
    kernel.start_stage(task_id="task-1", stage_id="stage-1")
    
    # 获取 Lease
    lease = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=process_id,
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=clock(),
    )
    
    assert lease.action_id is None
    
    # 捕获 Observation
    obs = kernel.capture_observation(
        task_id="task-1",
        device_id="device-1",
        observation_id="observation-1",
    )
    
    from ai_game_console.runtime_kernel.action import ActionType
    action = kernel.propose_action(
        task_id="task-1",
        stage_id="stage-1",
        action_id="action-1",
        based_on_observation_id=obs.id,
        action_type=ActionType.TAP,
        params={"x": 100, "y": 200},
        expected_outcome="Test action",
        proposed_by_call_id="operator-1",
    )
    
    # 更新 Lease 关联 Action
    store.update_lease_action(lease.id, action.id)
    
    # 验证更新
    updated_lease = store.get_lease_for_device("device-1")
    assert updated_lease is not None
    assert updated_lease.action_id == action.id
    
    # 清除 Action 关联
    store.update_lease_action(lease.id, None)
    cleared_lease = store.get_lease_for_device("device-1")
    assert cleared_lease is not None
    assert cleared_lease.action_id is None


def test_get_lease_for_task_returns_correct_lease(tmp_path: Path) -> None:
    """验证 get_lease_for_task 返回正确的 Lease"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    process_id = str(os.getpid())
    
    # 创建 Task
    from ai_game_console.runtime_kernel.task import Task
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor
    
    task = Task.create(
        task_id="task-1",
        goal="Test get by task",
        source=_source("1"),
        device_id="device-1",
        created_at=clock(),
    )
    event = RuntimeEventDraft(
        id="event-1",
        type="TaskCreated",
        actor=EventActor.RUNTIME,
        payload={},
        correlation_id=task.id,
        created_at=clock(),
    )
    store.create_task(task, event)
    
    # 获取 Lease
    lease = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=process_id,
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=clock(),
    )
    
    # 通过 task_id 查询
    retrieved = store.get_lease_for_task("task-1")
    assert retrieved is not None
    assert retrieved.id == lease.id
    assert retrieved.task_id == "task-1"
    
    # 不存在的 task
    assert store.get_lease_for_task("nonexistent-task") is None
