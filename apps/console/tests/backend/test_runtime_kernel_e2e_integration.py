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
from ai_game_console.runtime_kernel.fact import Fact, FactScope, FactStatus
from ai_game_console.runtime_kernel.kernel import RuntimeKernel
from ai_game_console.runtime_kernel.stage import StageStatus
from ai_game_console.runtime_kernel.task import TaskSource, TaskStatus
from ai_game_console.runtime_kernel.verify import VerificationMethod, VerificationVerdict
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


class FullFakeExecutor(ActionExecutorPort):
    """记录调用的完整类型化 Executor

    与 FakeActionExecutor（仅通用 execute()）不同，这里实现 execute_action()
    按 Action 类型分发所要求的全部五个端口方法。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def _record(
        self, method: str, device_id: str, params: dict
    ) -> ActionExecutionResult:
        self.calls.append((method, device_id, params))
        return ActionExecutionResult(
            accepted=True,
            adapter_code=0,
            error=None,
            started_at=_clock(),
            finished_at=_clock(),
        )

    def execute_tap(
        self, device_id: str, x: int, y: int, timeout_ms: int = 5000
    ) -> ActionExecutionResult:
        return self._record("tap", device_id, {"x": x, "y": y})

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
        return self._record("swipe", device_id, {
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "duration_ms": duration_ms,
        })

    def execute_input_text(
        self, device_id: str, text: str, timeout_ms: int = 5000
    ) -> ActionExecutionResult:
        return self._record("input_text", device_id, {"text": text})

    def execute_back(self, device_id: str, timeout_ms: int = 5000) -> ActionExecutionResult:
        return self._record("back", device_id, {})

    def execute_home(self, device_id: str, timeout_ms: int = 5000) -> ActionExecutionResult:
        return self._record("home", device_id, {})


def test_execute_action_complete_flow_integration(tmp_path: Path) -> None:
    """集成测试（Day 3）：propose → execute → verify → commit 完整流程（场景 9）

    场景：
    1. 创建 Task/Stage 并启动 Stage
    2. 捕获 Observation 并 propose TAP Action（PROPOSED）
    3. execute_action() 获取独占 Lease、按类型分发执行、完成后自动释放 Lease
    4. 捕获新的 after-observation
    5. verify_action() SUCCESS 裁决并提交（commit Facts、推进 Stage）
    6. Stage COMPLETED、Task 回到 PLANNING、Checkpoint(stage_completed) 物化
    """
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    executor = FullFakeExecutor()
    kernel = RuntimeKernel(
        store=store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=executor,
    )

    device_id = "e2e-flow-device"
    task = kernel.create_task(
        device_id=device_id,
        goal="Complete flow: tap the claim button",
        source=TaskSource(
            client_id="e2e-flow-client",
            conversation_id="e2e-flow-conv",
            initial_message_id="e2e-flow-msg",
        ),
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Tap the claim button",
        completion_criteria=("claim button tapped",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)

    # 2. 观察 → 决策
    obs = kernel.capture_observation(task_id=task.id, device_id=device_id)
    action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs.id,
        action_type=ActionType.TAP,
        params={"x": 540, "y": 1200},
        expected_outcome="Claim button is tapped",
        proposed_by_call_id="e2e-flow-propose",
    )
    assert action.status is ActionStatus.PROPOSED

    # 3. 执行：Lease 获取 + 类型化分发 + 自动释放
    execution = kernel.execute_action(task_id=task.id, action_id=action.id)
    assert execution.accepted is True
    assert execution.lease_ref is not None
    assert execution.device_id == device_id
    assert executor.calls == [("tap", device_id, {"x": 540, "y": 1200})]
    assert store.load_action(task.id, action.id).status is ActionStatus.EXECUTED
    # 执行完成后 Lease 已自动释放
    assert store.get_lease_for_device(device_id) is None

    # 4. 捕获新的 after-observation
    after_obs = kernel.capture_observation(task_id=task.id, device_id=device_id)
    assert after_obs.id != obs.id

    # 5. verify SUCCESS + 提交
    fact = Fact.create(
        fact_id="fact-e2e-flow-1",
        task_id=task.id,
        key="claim_button_state",
        value="tapped",
        status=FactStatus.VERIFIED,
        confidence=0.95,
        scope=FactScope.TASK,
        stage_id=None,
        source_refs=(f"observation:{after_obs.id}",),
        supersedes_fact_id=None,
        created_at=_clock(),
    )
    verification, checkpoint = kernel.verify_action(
        task_id=task.id,
        action_id=action.id,
        before_observation_id=obs.id,
        after_observation_id=after_obs.id,
        verdict=VerificationVerdict.SUCCESS,
        reason="Claim button shows the tapped state",
        evidence_refs=(f"observation:{after_obs.id}",),
        method=VerificationMethod.RUNTIME_RULE,
        verified_facts=(fact,),
        complete_stage=True,
    )

    # 6. 提交结果：Stage COMPLETED、Task 回到 PLANNING、Checkpoint 物化
    assert verification.verdict is VerificationVerdict.SUCCESS
    assert checkpoint is not None
    assert checkpoint.resume_reason == "stage_completed"
    assert kernel.load_stage(task.id, stage.id).status is StageStatus.COMPLETED
    assert kernel.load_task(task.id).status is TaskStatus.PLANNING
    committed = store.list_facts(task.id, verified_only=True)
    assert [f.id for f in committed] == [fact.id]

    kernel.close()


def test_execute_action_complete_flow_fail_verdict_integration(tmp_path: Path) -> None:
    """集成测试（Day 3）：执行后 FAIL 裁决与恢复（场景 9）

    场景：
    1. 正常执行（Lease 获取/释放）
    2. verify_action() FAIL 裁决 → Action FAILED、无 Checkpoint
    3. Stage 保持 ACTIVE、Task 保持 RUNNING（失败被记录但不阻断）
    4. 基于新 Observation 重新 propose（恢复路径未被阻断）
    """
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    executor = FullFakeExecutor()
    kernel = RuntimeKernel(
        store=store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        action_executor=executor,
    )

    device_id = "e2e-fail-device"
    task = kernel.create_task(
        device_id=device_id,
        goal="Fail flow: tap a button that will not respond",
        source=TaskSource(
            client_id="e2e-fail-client",
            conversation_id="e2e-fail-conv",
            initial_message_id="e2e-fail-msg",
        ),
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Tap the unresponsive button",
        completion_criteria=("button responds",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)

    obs = kernel.capture_observation(task_id=task.id, device_id=device_id)
    action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs.id,
        action_type=ActionType.TAP,
        params={"x": 100, "y": 200},
        expected_outcome="Button responds",
        proposed_by_call_id="e2e-fail-propose",
    )

    # 1. 正常执行
    execution = kernel.execute_action(task_id=task.id, action_id=action.id)
    assert execution.accepted is True
    assert store.get_lease_for_device(device_id) is None

    # 2. verify FAIL
    after_obs = kernel.capture_observation(task_id=task.id, device_id=device_id)
    verification, checkpoint = kernel.verify_action(
        task_id=task.id,
        action_id=action.id,
        before_observation_id=obs.id,
        after_observation_id=after_obs.id,
        verdict=VerificationVerdict.FAIL,
        reason="Button state is unchanged",
        evidence_refs=(f"observation:{after_obs.id}",),
        method=VerificationMethod.RUNTIME_RULE,
    )
    assert verification.verdict is VerificationVerdict.FAIL
    assert checkpoint is None
    assert store.load_action(task.id, action.id).status is ActionStatus.FAILED
    assert kernel.load_stage(task.id, stage.id).status is StageStatus.ACTIVE
    assert kernel.load_task(task.id).status is TaskStatus.RUNNING

    # 4. 恢复路径未被阻断：基于新 Observation 重新 propose
    retry_obs = kernel.capture_observation(task_id=task.id, device_id=device_id)
    retry_action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=retry_obs.id,
        action_type=ActionType.TAP,
        params={"x": 101, "y": 201},
        expected_outcome="Retry with adjusted coordinates",
        proposed_by_call_id="e2e-fail-retry",
    )
    assert retry_action.status is ActionStatus.PROPOSED

    kernel.close()
