from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import (
    ActionStatus,
    ActionType,
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
    RuntimeKernel,
    TaskSource,
    ActionExecutionResult,
    ExecutionError,
)

TIMES = tuple(f"2026-08-17T15:{minute:02d}:00+00:00" for minute in range(60))


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


class FakeActionExecutor:
    """模拟 ADB 执行器，用于测试"""
    
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []  # (method, device_id, params)
    
    def execute_tap(
        self,
        device_id: str,
        x: int,
        y: int,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        self.calls.append(("tap", device_id, {"x": x, "y": y}))
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=TIMES[20],
            finished_at=TIMES[21],
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
        self.calls.append(("swipe", device_id, {
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "duration_ms": duration_ms,
        }))
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=TIMES[20],
            finished_at=TIMES[21],
        )
    
    def execute_input_text(
        self,
        device_id: str,
        text: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        self.calls.append(("input_text", device_id, {"text": text}))
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=TIMES[20],
            finished_at=TIMES[21],
        )
    
    def execute_back(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        self.calls.append(("back", device_id, {}))
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=TIMES[20],
            finished_at=TIMES[21],
        )
    
    def execute_home(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        self.calls.append(("home", device_id, {}))
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=TIMES[20],
            finished_at=TIMES[21],
        )


def test_execute_action_full_flow_tap(tmp_path: Path) -> None:
    """验证 execute_action() 完整流程：栅栏检查 → Lease → 执行 → 记录"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    
    executor = FakeActionExecutor()
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=executor,
        clock=_clock(),
        id_factory=_ids(),
    )
    
    # 准备 Task 和 Stage
    task = kernel.create_task(
        goal="Test execute action",
        source=_source("exec"),
        device_id="device-exec",
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Execute tap",
        completion_criteria=("tap done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    
    # 捕获 Observation
    obs = kernel.capture_observation(
        task_id=task.id,
        device_id="device-exec",
    )
    
    # Propose Action
    action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs.id,
        action_type=ActionType.TAP,
        params={"x": 500, "y": 1000},
        expected_outcome="Tap executed",
        proposed_by_call_id="operator-exec",
    )
    
    # 执行 Action
    execution = kernel.execute_action(task_id=task.id, action_id=action.id)
    
    # 验证结果
    assert execution.accepted is True
    assert execution.adapter_code == 0
    assert execution.error is None
    
    # 验证 Action 状态
    loaded_action = kernel.load_action(task.id, action.id)
    assert loaded_action.status == ActionStatus.EXECUTED
    
    # 验证 executor 被调用
    assert len(executor.calls) == 1
    assert executor.calls[0] == ("tap", "device-exec", {"x": 500, "y": 1000})
    
    # 验证 Lease 已释放
    assert store.get_lease_for_device("device-exec") is None


def test_execute_action_different_types(tmp_path: Path) -> None:
    """验证 execute_action() 支持不同 Action 类型"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    
    executor = FakeActionExecutor()
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=executor,
        clock=_clock(),
        id_factory=_ids(),
    )
    
    # 准备
    task = kernel.create_task(
        goal="Test multiple actions",
        source=_source("multi"),
        device_id="device-multi",
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Execute multiple actions",
        completion_criteria=("all done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    
    # 测试 SWIPE
    obs1 = kernel.capture_observation(task_id=task.id, device_id="device-multi")
    action1 = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs1.id,
        action_type=ActionType.SWIPE,
        params={"start_x": 100, "start_y": 500, "end_x": 900, "end_y": 500, "duration_ms": 200},
        expected_outcome="Swipe done",
        proposed_by_call_id="op-1",
    )
    kernel.execute_action(task_id=task.id, action_id=action1.id)
    
    # 测试 BACK
    obs2 = kernel.capture_observation(task_id=task.id, device_id="device-multi")
    action2 = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs2.id,
        action_type=ActionType.BACK,
        params={},
        expected_outcome="Back done",
        proposed_by_call_id="op-2",
    )
    kernel.execute_action(task_id=task.id, action_id=action2.id)
    
    # 验证
    assert len(executor.calls) == 2
    assert executor.calls[0][0] == "swipe"
    assert executor.calls[1][0] == "back"


def test_execute_action_releases_lease_on_executor_failure(tmp_path: Path) -> None:
    """验证执行器失败时 Lease 正确释放"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    
    class FailingExecutor:
        def execute_tap(self, device_id, x, y, timeout_ms=5000):
            return ActionExecutionResult(
                accepted=False,
                adapter_code=1,
                error=ExecutionError(
                    code="adb_failed",
                    message="Simulated failure",
                    retryable=True,
                ),
                started_at=TIMES[20],
                finished_at=TIMES[21],
            )
    
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=FailingExecutor(),
        clock=_clock(),
        id_factory=_ids(),
    )
    
    # 准备
    task = kernel.create_task(
        goal="Test failure",
        source=_source("fail"),
        device_id="device-fail",
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Execute failing action",
        completion_criteria=("done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    obs = kernel.capture_observation(task_id=task.id, device_id="device-fail")
    action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs.id,
        action_type=ActionType.TAP,
        params={"x": 100, "y": 100},
        expected_outcome="Will fail",
        proposed_by_call_id="op-fail",
    )
    
    # 执行（失败）
    execution = kernel.execute_action(task_id=task.id, action_id=action.id)
    
    # 验证
    assert execution.accepted is False
    assert execution.error is not None
    assert execution.error.code == "adb_failed"
    
    # Action 状态应该是 FAILED
    loaded_action = kernel.load_action(task.id, action.id)
    assert loaded_action.status == ActionStatus.FAILED
    
    # Lease 应该已释放
    assert store.get_lease_for_device("device-fail") is None


def test_execute_action_raises_if_executor_not_configured(tmp_path: Path) -> None:
    """验证未配置执行器时抛出异常"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=None,  # 未配置
        clock=_clock(),
        id_factory=_ids(),
    )
    
    # 准备
    task = kernel.create_task(
        goal="Test no executor",
        source=_source("no-exec"),
        device_id="device-no-exec",
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Will fail",
        completion_criteria=("done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    obs = kernel.capture_observation(task_id=task.id, device_id="device-no-exec")
    action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs.id,
        action_type=ActionType.TAP,
        params={"x": 100, "y": 100},
        expected_outcome="Will fail",
        proposed_by_call_id="op-no-exec",
    )
    
    # 应该抛出异常
    with pytest.raises(RuntimeError, match="action executor is not configured"):
        kernel.execute_action(task_id=task.id, action_id=action.id)
