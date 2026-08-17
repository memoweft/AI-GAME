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
    StoreConflict,
    TaskSource,
    TaskStatus,
    VerificationMethod,
    VerificationVerdict,
)

TIMES = tuple(f"2026-08-11T00:{minute:02d}:00+00:00" for minute in range(60))
SCREENSHOT = b"\x89PNG\r\n\x1a\nphase-4-test-pixels"
UI_TREE = b"<hierarchy><node resource-id='target' /></hierarchy>"


class FakeObservationProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def capture(self, device_id: str) -> RawObservation:
        self.calls.append(device_id)
        return RawObservation(
            device_id=device_id,
            capture_started_at=TIMES[20],
            capture_completed_at=TIMES[22],
            screenshot=RawScreenshot(
                status=ChannelAvailability.AVAILABLE,
                content=SCREENSHOT,
                width=1080,
                height=2400,
                captured_at=TIMES[21],
            ),
            ui_tree=RawUiTree(
                status=ChannelAvailability.AVAILABLE,
                content=UI_TREE,
                captured_at=TIMES[22],
            ),
            device_state=DeviceState(
                status=ChannelAvailability.AVAILABLE,
                foreground_app="com.example.target",
                screen_size=(1080, 2400),
                orientation=Orientation.PORTRAIT,
                keyboard_state=KeyboardState.HIDDEN,
                connection_state=ConnectionState.CONNECTED,
                captured_at=TIMES[21],
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
    values = iter(f"generated-{index}" for index in range(1, 500))
    return lambda: next(values)


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


def _prepare_executed_action(
    kernel: RuntimeKernel, *, suffix: str = "1"
) -> tuple[str, str, str, str]:
    task_id = f"task-{suffix}"
    stage_id = f"stage-{suffix}"
    action_id = f"action-{suffix}"
    kernel.create_task(
        task_id=task_id,
        goal="Reach the verified target",
        source=_source(suffix),
        device_id=f"device-{suffix}",
    )
    kernel.create_stage(
        task_id=task_id,
        stage_id=stage_id,
        objective="Reach the target page",
        completion_criteria=("target is visible",),
        planner_call_id=f"planner-{suffix}",
    )
    kernel.start_stage(task_id=task_id, stage_id=stage_id)
    before = kernel.capture_observation(
        task_id=task_id,
        device_id=f"device-{suffix}",
        observation_id=f"observation-before-{suffix}",
    )
    kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id=action_id,
        based_on_observation_id=before.id,
        action_type=ActionType.TAP,
        params={"x": 400, "y": 1200},
        expected_outcome="The target page is visible",
        proposed_by_call_id=f"operator-{suffix}",
    )
    assert kernel.prepare_action_execution(task_id=task_id, action_id=action_id).id == action_id
    kernel.record_action_execution(
        task_id=task_id,
        action_id=action_id,
        execution_id=f"execution-{suffix}",
        accepted=True,
        adapter_code=0,
        error=None,
    )
    after = kernel.capture_observation(
        task_id=task_id,
        device_id=f"device-{suffix}",
        observation_id=f"observation-after-{suffix}",
    )
    return task_id, stage_id, action_id, after.id


def _verified_fact(task_id: str, stage_id: str, suffix: str) -> Fact:
    return Fact.create(
        fact_id=f"fact-{suffix}",
        task_id=task_id,
        key="target.visible",
        value={"text": "verified target"},
        status=FactStatus.VERIFIED,
        confidence=1.0,
        scope=FactScope.STAGE,
        stage_id=stage_id,
        source_refs=(f"verification:verification-{suffix}",),
        supersedes_fact_id=None,
        created_at=TIMES[30],
    )


def test_success_commits_verification_fact_stage_and_checkpoint_atomically(
    tmp_path: Path,
) -> None:
    kernel, store = _kernel(tmp_path)
    task_id, stage_id, action_id, after_observation_id = _prepare_executed_action(kernel)

    verification, checkpoint = kernel.verify_action(
        task_id=task_id,
        action_id=action_id,
        verification_id="verification-1",
        before_observation_id="observation-before-1",
        after_observation_id=after_observation_id,
        verdict=VerificationVerdict.SUCCESS,
        reason="The target is visible in the fresh Observation.",
        evidence_refs=("observation:observation-after-1",),
        method=VerificationMethod.RUNTIME_RULE,
        verified_facts=(_verified_fact(task_id, stage_id, "1"),),
        complete_stage=True,
        progress_summary="Reached the verified target.",
        checkpoint_id="checkpoint-1",
    )

    assert verification.verdict is VerificationVerdict.SUCCESS
    assert checkpoint is not None
    assert checkpoint.through_sequence == 11
    assert checkpoint.verified_facts == (_verified_fact(task_id, stage_id, "1"),)
    assert kernel.load_task(task_id).status is TaskStatus.PLANNING
    assert kernel.current_stage(task_id) is None
    assert kernel.load_stage(task_id, stage_id).status is StageStatus.COMPLETED
    assert store.load_action(task_id, action_id).status is ActionStatus.VERIFIED
    assert store.load_action_execution(action_id).accepted is True
    assert store.load_verification(action_id) == verification
    assert store.list_facts(task_id, verified_only=True) == (
        _verified_fact(task_id, stage_id, "1"),
    )
    assert kernel.latest_checkpoint(task_id) == checkpoint
    assert [event.type for event in kernel.events(task_id)] == [
        "TaskCreated",
        "StageCreated",
        "StageStarted",
        "ObservationReceived",
        "ActionProposed",
        "ActionExecuted",
        "ObservationReceived",
        "ActionVerified",
        "FactAdded",
        "StageCompleted",
        "CheckpointCreated",
    ]
    assert [event.sequence for event in kernel.events(task_id)] == list(range(1, 12))

    store.close()
    reopened = SQLiteRuntimeStore(tmp_path / "runtime.db")
    reopened.initialize()
    assert reopened.load_action(task_id, action_id).status is ActionStatus.VERIFIED
    assert reopened.load_verification(action_id) == verification
    assert reopened.list_facts(task_id, verified_only=True) == (
        _verified_fact(task_id, stage_id, "1"),
    )
    assert reopened.latest_checkpoint(task_id) == checkpoint


@pytest.mark.parametrize(
    ("verdict", "expected_action_status"),
    [
        (VerificationVerdict.FAIL, ActionStatus.FAILED),
        (VerificationVerdict.UNCERTAIN, ActionStatus.UNCERTAIN),
    ],
)
def test_fail_and_uncertain_never_commit_facts_or_stage_progress(
    tmp_path: Path,
    verdict: VerificationVerdict,
    expected_action_status: ActionStatus,
) -> None:
    kernel, store = _kernel(tmp_path)
    suffix = verdict.value.lower()
    task_id, stage_id, action_id, after_observation_id = _prepare_executed_action(
        kernel, suffix=suffix
    )

    verification, checkpoint = kernel.verify_action(
        task_id=task_id,
        action_id=action_id,
        verification_id=f"verification-{suffix}",
        before_observation_id=f"observation-before-{suffix}",
        after_observation_id=after_observation_id,
        verdict=verdict,
        reason=f"{verdict.value} evidence",
        evidence_refs=(f"observation:{after_observation_id}",),
        method=VerificationMethod.RUNTIME_RULE,
    )

    assert checkpoint is None
    assert verification.verdict is verdict
    assert store.load_action(task_id, action_id).status is expected_action_status
    assert kernel.load_stage(task_id, stage_id).status is StageStatus.ACTIVE
    task = kernel.load_task(task_id)
    assert task.status is TaskStatus.RUNNING
    assert task.failure_state is not None
    assert task.failure_state.last_verdict == verdict.value
    assert store.list_facts(task_id, verified_only=True) == ()
    assert [event.type for event in kernel.events(task_id)][-1] == "ActionVerified"

    if verdict is VerificationVerdict.UNCERTAIN:
        checkpoint = kernel.create_checkpoint(
            task_id=task_id,
            checkpoint_id=f"checkpoint-{suffix}",
            reason="unresolved physical outcome",
            unresolved_action_ref=action_id,
        )
        assert checkpoint.required_fresh_observation is True
        assert checkpoint.unresolved_action_ref == action_id
        assert checkpoint.through_sequence == kernel.events(task_id)[-1].sequence


def test_stale_action_is_not_dispatchable_after_a_new_observation(tmp_path: Path) -> None:
    kernel, _ = _kernel(tmp_path)
    task_id, stage_id, action_id, _ = _prepare_executed_action(kernel, suffix="stale")

    # The action has already been reported as executed in this helper; create a fresh
    # proposal to demonstrate that a new Observation invalidates its decision basis.
    new_action = kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id="action-stale-proposed",
        based_on_observation_id="observation-after-stale",
        action_type=ActionType.BACK,
        params={},
        expected_outcome="Return to the previous page",
        proposed_by_call_id="operator-stale-2",
    )
    kernel.capture_observation(
        task_id=task_id,
        device_id="device-stale",
        observation_id="observation-newer-stale",
    )

    with pytest.raises(ValueError, match="stale"):
        kernel.prepare_action_execution(task_id=task_id, action_id=new_action.id)
    assert kernel.load_action(task_id, action_id).status is ActionStatus.EXECUTED
    assert kernel.load_action(task_id, new_action.id).status is ActionStatus.PROPOSED


def test_checkpoint_blocks_replay_of_an_unresolved_action_after_reopen(
    tmp_path: Path,
) -> None:
    kernel, store = _kernel(tmp_path)
    task_id = "task-open-intent"
    stage_id = "stage-open-intent"
    kernel.create_task(
        task_id=task_id,
        goal="Reach target",
        source=_source("open-intent"),
        device_id="device-open-intent",
    )
    kernel.create_stage(
        task_id=task_id,
        stage_id=stage_id,
        objective="Reach target",
        completion_criteria=("target visible",),
    )
    kernel.start_stage(task_id=task_id, stage_id=stage_id)
    observation = kernel.capture_observation(
        task_id=task_id,
        device_id="device-open-intent",
        observation_id="observation-open-intent",
    )
    action = kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id="action-open-intent",
        based_on_observation_id=observation.id,
        action_type=ActionType.TAP,
        params={"x": 1, "y": 1},
        expected_outcome="Reach target",
        proposed_by_call_id="operator-open-intent",
    )
    checkpoint = kernel.create_checkpoint(
        task_id=task_id,
        checkpoint_id="checkpoint-open-intent",
        reason="controlled stop after intent boundary",
        unresolved_action_ref=action.id,
    )
    store.close()

    reopened = RuntimeKernel(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    recovered = reopened.latest_checkpoint(task_id)
    assert recovered == checkpoint
    assert recovered is not None and recovered.required_fresh_observation is True
    with pytest.raises(ValueError, match="cannot be replayed"):
        reopened.prepare_action_execution(task_id=task_id, action_id=action.id)
    assert reopened.load_action(task_id, action.id).status is ActionStatus.PROPOSED


def test_success_commit_rolls_back_every_fact_when_fact_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, store = _kernel(tmp_path)
    task_id, stage_id, action_id, after_observation_id = _prepare_executed_action(
        kernel, suffix="rollback"
    )

    def fail_fact(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("injected Fact failure")

    monkeypatch.setattr(store, "_insert_fact", fail_fact)
    with pytest.raises(StoreConflict, match="injected Fact failure"):
        kernel.verify_action(
            task_id=task_id,
            action_id=action_id,
            verification_id="verification-rollback",
            before_observation_id="observation-before-rollback",
            after_observation_id=after_observation_id,
            verdict=VerificationVerdict.SUCCESS,
            reason="would otherwise succeed",
            evidence_refs=(f"observation:{after_observation_id}",),
            method=VerificationMethod.RUNTIME_RULE,
            verified_facts=(_verified_fact(task_id, stage_id, "rollback"),),
            complete_stage=True,
        )

    assert store.load_action(task_id, action_id).status is ActionStatus.EXECUTED
    with pytest.raises(Exception, match="Verification"):
        store.load_verification(action_id)
    assert store.list_facts(task_id, verified_only=True) == ()
    assert kernel.load_stage(task_id, stage_id).status is StageStatus.ACTIVE
    assert kernel.load_task(task_id).status is TaskStatus.RUNNING
    assert "ActionVerified" not in [event.type for event in kernel.events(task_id)]


def test_rejected_transport_never_completes_a_stage(tmp_path: Path) -> None:
    kernel, store = _kernel(tmp_path)
    task_id = "task-rejected"
    stage_id = "stage-rejected"
    kernel.create_task(
        task_id=task_id,
        goal="Reach target",
        source=_source("rejected"),
        device_id="device-rejected",
    )
    kernel.create_stage(
        task_id=task_id,
        stage_id=stage_id,
        objective="Reach target",
        completion_criteria=("target visible",),
    )
    kernel.start_stage(task_id=task_id, stage_id=stage_id)
    observation = kernel.capture_observation(
        task_id=task_id,
        device_id="device-rejected",
        observation_id="observation-rejected",
    )
    action = kernel.propose_action(
        task_id=task_id,
        stage_id=stage_id,
        action_id="action-rejected",
        based_on_observation_id=observation.id,
        action_type=ActionType.TAP,
        params={"x": 1, "y": 1},
        expected_outcome="Reach target",
        proposed_by_call_id="operator-rejected",
    )
    execution = kernel.record_action_execution(
        task_id=task_id,
        action_id=action.id,
        execution_id="execution-rejected",
        accepted=False,
        adapter_code=7,
        error=ExecutionError(code="transport_rejected", message="not sent", retryable=True),
    )

    assert execution.accepted is False
    assert store.load_action(task_id, action.id).status is ActionStatus.FAILED
    assert kernel.load_stage(task_id, stage_id).status is StageStatus.ACTIVE
    assert kernel.load_task(task_id).status is TaskStatus.RUNNING
    assert "StageCompleted" not in [event.type for event in kernel.events(task_id)]
