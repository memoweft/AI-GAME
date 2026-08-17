from __future__ import annotations

import sqlite3
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
    ExecutionError,
    Fact,
    FactScope,
    FactStatus,
    KeyboardState,
    ObservationConsistency,
    Orientation,
    RawObservation,
    RawScreenshot,
    RawUiTree,
    RuntimeKernel,
    StageStatus,
    TaskSource,
    TaskStatus,
    VerificationMethod,
    VerificationVerdict,
)

TIMES = tuple(f"2026-08-17T10:{minute:02d}:00+00:00" for minute in range(60))
SCREENSHOT = b"\x89PNG\r\n\x1a\nrecovery-test-pixels"
UI_TREE = b"<hierarchy><node resource-id='recovery-target' /></hierarchy>"


class FakeObservationProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def capture(self, device_id: str) -> RawObservation:
        self.calls.append(device_id)
        return RawObservation(
            device_id=device_id,
            capture_started_at=TIMES[10],
            capture_completed_at=TIMES[12],
            screenshot=RawScreenshot(
                status=ChannelAvailability.AVAILABLE,
                content=SCREENSHOT,
                width=1080,
                height=2400,
                captured_at=TIMES[11],
            ),
            ui_tree=RawUiTree(
                status=ChannelAvailability.AVAILABLE,
                content=UI_TREE,
                captured_at=TIMES[12],
            ),
            device_state=DeviceState(
                status=ChannelAvailability.AVAILABLE,
                foreground_app="com.example.recovery",
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


def _kernel(tmp_path: Path) -> tuple[RuntimeKernel, SQLiteRuntimeStore]:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=_clock(),
        id_factory=_ids(),
    )
    return kernel, store


def test_executed_action_without_verification_blocks_replay_after_reopen(
    tmp_path: Path,
) -> None:
    """Verify that an EXECUTED Action persisted before process crash blocks replay."""
    kernel, store = _kernel(tmp_path)
    task_id = "task-crash"
    stage_id = "stage-crash"
    action_id = "action-crash"
    device_id = "device-crash"

    kernel.create_task(
        task_id=task_id,
        goal="Test crash recovery",
        source=_source("crash"),
        device_id=device_id,
    )
    kernel.create_stage(
        task_id=task_id,
        stage_id=stage_id,
        objective="Complete action",
        completion_criteria=("action verified",),
    )
    kernel.start_stage(task_id=task_id, stage_id=stage_id)
    observation = kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-crash",
    )
    action = kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id=action_id,
        based_on_observation_id=observation.id,
        action_type=ActionType.TAP,
        params={"x": 100, "y": 200},
        expected_outcome="Action completes",
        proposed_by_call_id="operator-crash",
    )
    assert action.status is ActionStatus.PROPOSED

    # Execute the action but DO NOT verify it
    kernel.prepare_action_execution(task_id=task_id, action_id=action_id)
    kernel.record_action_execution(
        task_id=task_id,
        action_id=action_id,
        execution_id="execution-crash",
        accepted=True,
        adapter_code=0,
        error=None,
    )
    executed = store.load_action(task_id, action_id)
    assert executed.status is ActionStatus.EXECUTED

    # Create checkpoint with unresolved_action_ref to simulate controlled stop
    checkpoint = kernel.create_checkpoint(
        task_id=task_id,
        checkpoint_id="checkpoint-crash",
        reason="simulated crash after execution",
        unresolved_action_ref=action_id,
    )
    assert checkpoint.unresolved_action_ref == action_id
    assert checkpoint.required_fresh_observation is True

    # 💥 Simulate process restart: close and reopen
    store.close()

    reopened_kernel = RuntimeKernel(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    recovered_checkpoint = reopened_kernel.latest_checkpoint(task_id)
    assert recovered_checkpoint is not None
    assert recovered_checkpoint.unresolved_action_ref == action_id

    # ✅ Verify: prepare_action_execution should reject replay
    # The Action is EXECUTED, so it's not PROPOSED and cannot be dispatched
    with pytest.raises(ValueError, match="only a PROPOSED Action can be dispatched"):
        reopened_kernel.prepare_action_execution(task_id=task_id, action_id=action_id)

    # Action status should still be EXECUTED
    recovered_action = reopened_kernel.load_action(task_id, action_id)
    assert recovered_action.status is ActionStatus.EXECUTED


def test_fail_verdict_is_resolved_and_does_not_block_next_action(tmp_path: Path) -> None:
    """Verify that FAIL verdict is treated as 'resolved' and allows new Actions."""
    kernel, store = _kernel(tmp_path)
    task_id = "task-fail-resolved"
    stage_id = "stage-fail-resolved"
    device_id = "device-fail-resolved"

    kernel.create_task(
        task_id=task_id,
        goal="Test FAIL resolution",
        source=_source("fail-resolved"),
        device_id=device_id,
    )
    kernel.create_stage(
        task_id=task_id,
        stage_id=stage_id,
        objective="Try action",
        completion_criteria=("success",),
    )
    kernel.start_stage(task_id=task_id, stage_id=stage_id)

    # First action: propose, execute, verify FAIL
    obs1 = kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-fail-1",
    )
    action1 = kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id="action-fail-1",
        based_on_observation_id=obs1.id,
        action_type=ActionType.TAP,
        params={"x": 50, "y": 50},
        expected_outcome="Will fail",
        proposed_by_call_id="operator-fail-1",
    )
    kernel.prepare_action_execution(task_id=task_id, action_id=action1.id)
    kernel.record_action_execution(
        task_id=task_id,
        action_id=action1.id,
        execution_id="execution-fail-1",
        accepted=True,
        adapter_code=0,
        error=None,
    )
    obs2 = kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-fail-2",
    )

    verification1, checkpoint1 = kernel.verify_action(
        task_id=task_id,
        action_id=action1.id,
        verification_id="verification-fail-1",
        before_observation_id=obs1.id,
        after_observation_id=obs2.id,
        verdict=VerificationVerdict.FAIL,
        reason="Action did not achieve expected outcome",
        evidence_refs=(f"observation:{obs2.id}",),
        method=VerificationMethod.RUNTIME_RULE,
    )

    assert verification1.verdict is VerificationVerdict.FAIL
    assert checkpoint1 is None
    assert store.load_action(task_id, action1.id).status is ActionStatus.FAILED
    assert kernel.load_stage(task_id, stage_id).status is StageStatus.ACTIVE
    assert kernel.load_task(task_id).status is TaskStatus.RUNNING

    # ✅ Verify: can propose and prepare a new action without error
    obs3 = kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-fail-3",
    )
    action2 = kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id="action-fail-2",
        based_on_observation_id=obs3.id,
        action_type=ActionType.TAP,
        params={"x": 150, "y": 150},
        expected_outcome="Retry",
        proposed_by_call_id="operator-fail-2",
    )
    exec_token = kernel.prepare_action_execution(task_id=task_id, action_id=action2.id)
    assert exec_token.id == action2.id

    # FAIL does not create unresolved checkpoint
    assert kernel.latest_checkpoint(task_id) is None


def test_uncertain_verdict_with_checkpoint_blocks_replay_after_reopen(
    tmp_path: Path,
) -> None:
    """Verify that UNCERTAIN verdict + checkpoint blocks replay after process restart."""
    kernel, store = _kernel(tmp_path)
    task_id = "task-uncertain"
    stage_id = "stage-uncertain"
    device_id = "device-uncertain"

    kernel.create_task(
        task_id=task_id,
        goal="Test UNCERTAIN recovery",
        source=_source("uncertain"),
        device_id=device_id,
    )
    kernel.create_stage(
        task_id=task_id,
        stage_id=stage_id,
        objective="Uncertain action",
        completion_criteria=("uncertain outcome",),
    )
    kernel.start_stage(task_id=task_id, stage_id=stage_id)

    obs1 = kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-uncertain-1",
    )
    action = kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id="action-uncertain",
        based_on_observation_id=obs1.id,
        action_type=ActionType.SWIPE,
        params={"start_x": 100, "start_y": 100, "end_x": 500, "end_y": 500},
        expected_outcome="Uncertain physical outcome",
        proposed_by_call_id="operator-uncertain",
    )
    kernel.prepare_action_execution(task_id=task_id, action_id=action.id)
    kernel.record_action_execution(
        task_id=task_id,
        action_id=action.id,
        execution_id="execution-uncertain",
        accepted=True,
        adapter_code=0,
        error=None,
    )
    obs2 = kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-uncertain-2",
    )

    verification, checkpoint_from_verify = kernel.verify_action(
        task_id=task_id,
        action_id=action.id,
        verification_id="verification-uncertain",
        before_observation_id=obs1.id,
        after_observation_id=obs2.id,
        verdict=VerificationVerdict.UNCERTAIN,
        reason="Cannot determine physical outcome",
        evidence_refs=(f"observation:{obs2.id}",),
        method=VerificationMethod.RUNTIME_RULE,
    )

    assert verification.verdict is VerificationVerdict.UNCERTAIN
    assert checkpoint_from_verify is None  # UNCERTAIN does not auto-create checkpoint

    # Manually create checkpoint with unresolved_action_ref
    checkpoint = kernel.create_checkpoint(
        task_id=task_id,
        checkpoint_id="checkpoint-uncertain",
        reason="unresolved physical outcome",
        unresolved_action_ref=action.id,
    )
    assert checkpoint.unresolved_action_ref == action.id
    assert checkpoint.required_fresh_observation is True

    # 💥 Simulate process restart
    store.close()

    reopened_kernel = RuntimeKernel(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    recovered_checkpoint = reopened_kernel.latest_checkpoint(task_id)
    assert recovered_checkpoint is not None
    assert recovered_checkpoint.unresolved_action_ref == action.id

    # ✅ Verify: replay is blocked (UNCERTAIN is not PROPOSED)
    with pytest.raises(ValueError, match="only a PROPOSED Action can be dispatched"):
        reopened_kernel.prepare_action_execution(task_id=task_id, action_id=action.id)

    recovered_action = reopened_kernel.load_action(task_id, action.id)
    assert recovered_action.status is ActionStatus.UNCERTAIN


def test_checkpoint_with_resolved_action_allows_new_action_after_reopen(
    tmp_path: Path,
) -> None:
    """Verify that checkpoint without unresolved_action_ref allows new actions."""
    kernel, store = _kernel(tmp_path)
    task_id = "task-resolved-checkpoint"
    stage_id = "stage-resolved-checkpoint"
    device_id = "device-resolved-checkpoint"

    kernel.create_task(
        task_id=task_id,
        goal="Test resolved checkpoint",
        source=_source("resolved-checkpoint"),
        device_id=device_id,
    )
    kernel.create_stage(
        task_id=task_id,
        stage_id=stage_id,
        objective="Complete stage",
        completion_criteria=("stage completed",),
    )
    kernel.start_stage(task_id=task_id, stage_id=stage_id)

    obs1 = kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-resolved-1",
    )
    action = kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id="action-resolved",
        based_on_observation_id=obs1.id,
        action_type=ActionType.TAP,
        params={"x": 300, "y": 400},
        expected_outcome="Complete stage",
        proposed_by_call_id="operator-resolved",
    )
    kernel.prepare_action_execution(task_id=task_id, action_id=action.id)
    kernel.record_action_execution(
        task_id=task_id,
        action_id=action.id,
        execution_id="execution-resolved",
        accepted=True,
        adapter_code=0,
        error=None,
    )
    obs2 = kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-resolved-2",
    )

    fact = Fact.create(
        fact_id="fact-resolved",
        task_id=task_id,
        key="stage.completed",
        value={"status": "complete"},
        status=FactStatus.VERIFIED,
        confidence=1.0,
        scope=FactScope.STAGE,
        stage_id=stage_id,
        source_refs=(f"verification:verification-resolved",),
        supersedes_fact_id=None,
        created_at=TIMES[20],
    )

    # SUCCESS with complete_stage creates checkpoint without unresolved_action_ref
    verification, checkpoint = kernel.verify_action(
        task_id=task_id,
        action_id=action.id,
        verification_id="verification-resolved",
        before_observation_id=obs1.id,
        after_observation_id=obs2.id,
        verdict=VerificationVerdict.SUCCESS,
        reason="Stage completed successfully",
        evidence_refs=(f"observation:{obs2.id}",),
        method=VerificationMethod.RUNTIME_RULE,
        verified_facts=(fact,),
        complete_stage=True,
        progress_summary="Stage completed",
        checkpoint_id="checkpoint-resolved",
    )

    assert verification.verdict is VerificationVerdict.SUCCESS
    assert checkpoint is not None
    assert checkpoint.unresolved_action_ref is None
    assert checkpoint.required_fresh_observation is False

    # 💥 Simulate process restart
    store.close()

    # Create fresh ID generator and clock for reopened kernel
    reopened_store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    reopened_kernel = RuntimeKernel(
        reopened_store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=_clock(),  # Fresh clock iterator
        id_factory=_ids(),  # Fresh ID generator
    )
    recovered_checkpoint = reopened_kernel.latest_checkpoint(task_id)
    assert recovered_checkpoint is not None
    assert recovered_checkpoint.unresolved_action_ref is None

    # ✅ Verify: can propose new action after reopening
    reopened_kernel.create_stage(
        task_id=task_id,
        stage_id="stage-new",
        objective="New stage",
        completion_criteria=("new stage complete",),
    )
    reopened_kernel.start_stage(task_id=task_id, stage_id="stage-new")
    obs3 = reopened_kernel.capture_observation(
        task_id=task_id,
        device_id=device_id,
        observation_id="observation-new",
    )
    new_action = reopened_kernel.propose_action(
        task_id=task_id,
        stage_id="stage-new",
        action_id="action-new",
        based_on_observation_id=obs3.id,
        action_type=ActionType.BACK,
        params={},
        expected_outcome="Navigate back",
        proposed_by_call_id="operator-new",
    )
    exec_token = reopened_kernel.prepare_action_execution(
        task_id=task_id, action_id=new_action.id
    )
    assert exec_token.id == new_action.id
