from __future__ import annotations

import base64
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ai_game_console.device_lease import DeviceExecutionLease
from ai_game_console.kernel_canary import KernelCanaryCoordinator
from ai_game_console.mobile_agent import (
    ActionDecision,
    PlanDraft,
    ReflectionDecision,
    Verification,
)
from ai_game_console.mobile_task_adapter import LocalMobileEvidenceStore
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import (
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
    ActionType,
    ExecutionError,
    TaskSource,
    TaskStatus,
    VerificationMethod,
    VerificationVerdict,
)


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


class ObservationProvider:
    def capture(self, device_id: str) -> RawObservation:
        now = datetime.now(timezone.utc).isoformat()
        return RawObservation(
            device_id=device_id,
            capture_started_at=now,
            capture_completed_at=now,
            screenshot=RawScreenshot(
                status=ChannelAvailability.AVAILABLE,
                content=_PNG,
                width=1,
                height=1,
                captured_at=now,
            ),
            ui_tree=RawUiTree(
                status=ChannelAvailability.AVAILABLE,
                content=b"<hierarchy/>",
                captured_at=now,
            ),
            device_state=DeviceState(
                status=ChannelAvailability.AVAILABLE,
                foreground_app="com.android.settings",
                screen_size=(1, 1),
                orientation=Orientation.PORTRAIT,
                keyboard_state=KeyboardState.HIDDEN,
                connection_state=ConnectionState.CONNECTED,
                captured_at=now,
            ),
            consistency=ObservationConsistency(
                status=ConsistencyStatus.CONSISTENT,
                reason=None,
            ),
        )


class FinishModel:
    def plan(self, context):
        return PlanDraft(("设置页可见",))

    def decide(self, context):
        return ActionDecision("finish", reason="already visible")

    def verify(self, context):
        return Verification(True, True, evidence="设置页在新画面中可见")

    def reflect(self, context):
        return ReflectionDecision("visual-first")


class PausableFinishModel(FinishModel):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def decide(self, context):
        self.entered.set()
        assert self.release.wait(5)
        return super().decide(context)


def _coordinator(tmp_path: Path, model, *, experience=None):
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    kernel = RuntimeKernel(
        SQLiteRuntimeStore(tmp_path / "runtime.db"),
        observation_provider=ObservationProvider(),
        artifact_store=artifacts,
    )
    coordinator = KernelCanaryCoordinator(
        kernel=kernel,
        model=model,
        artifacts=artifacts,
        evidence=LocalMobileEvidenceStore(tmp_path / "evidence"),
        device_lease=DeviceExecutionLease(),
        settle_seconds=0,
        experience=experience,
    )
    return kernel, coordinator


def _wait_status(coordinator, task_id: str, status: str, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if coordinator.inspect(task_id).status == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"task did not reach {status}: {coordinator.inspect(task_id)}")


def test_kernel_canary_runs_to_candidate_then_commits_only_after_goal_verifier(
    tmp_path: Path,
) -> None:
    kernel, coordinator = _coordinator(tmp_path, FinishModel())
    try:
        accepted = coordinator.start(
            "确认设置页", "request-1", target_id="adb:device-1", goal_id="goal-1"
        )
        _wait_status(coordinator, accepted.task_id, "completed")
        assert kernel.load_task(accepted.task_id).status is TaskStatus.PLANNING
        assert len(kernel.list_actions(accepted.task_id)) == 1
        assert any(
            event.type == "KernelCandidateComplete"
            for event in kernel.events(accepted.task_id)
        )

        coordinator.confirm_goal_completion(accepted.task_id, "goal-1", 1)
        assert kernel.load_task(accepted.task_id).status is TaskStatus.COMPLETED
    finally:
        coordinator.shutdown()
        kernel.close()


def test_pause_fence_prevents_action_proposal_and_resume_requires_fresh_worker_turn(
    tmp_path: Path,
) -> None:
    model = PausableFinishModel()
    kernel, coordinator = _coordinator(tmp_path, model)
    try:
        accepted = coordinator.start(
            "确认设置页", "request-2", target_id="device-1", goal_id="goal-2"
        )
        assert model.entered.wait(5)
        paused = coordinator.control(accepted.task_id, "pause")
        assert paused.status == "paused"
        model.release.set()
        time.sleep(0.1)
        assert kernel.list_actions(accepted.task_id) == ()
        assert kernel.load_task(accepted.task_id).status is TaskStatus.PAUSED

        coordinator.control(accepted.task_id, "resume")
        _wait_status(coordinator, accepted.task_id, "completed")
        assert len(kernel.list_actions(accepted.task_id)) == 1
        linked = [
            event
            for event in kernel.events(accepted.task_id)
            if event.type == "KernelObservationLinked"
        ]
        assert len(linked) >= 4
    finally:
        model.release.set()
        coordinator.shutdown()
        kernel.close()


def test_restart_marks_accepted_unverified_action_uncertain_without_replay(
    tmp_path: Path,
) -> None:
    kernel, coordinator = _coordinator(tmp_path, FinishModel())
    try:
        task = kernel.create_task(
            goal="restart fence",
            source=TaskSource("client", "conversation", "message"),
            device_id="adb:device-1",
        )
        kernel.record_worker_event(
            task_id=task.id,
            event_type="KernelCanaryAccepted",
            payload={"rollback": "mobile_task_compat"},
        )
        stage = kernel.create_stage(
            task_id=task.id,
            objective="one stage",
            completion_criteria=("visible",),
        )
        kernel.start_stage(task_id=task.id, stage_id=stage.id)
        observation = kernel.capture_observation(
            task_id=task.id, device_id=task.device_id
        )
        action = kernel.propose_action(
            task_id=task.id,
            stage_id=stage.id,
            based_on_observation_id=observation.id,
            action_type=ActionType.SCREENSHOT,
            params={},
            expected_outcome="check",
            proposed_by_call_id="call",
        )
        kernel.record_action_execution(
            task_id=task.id,
            action_id=action.id,
            accepted=True,
            adapter_code=0,
            error=None,
        )

        coordinator.recover()

        assert kernel.load_task(task.id).status is TaskStatus.FAILED
        assert coordinator.inspect(task.id).status == "uncertain"
        assert len(kernel.list_actions(task.id)) == 1
        assert any(
            event.type == "TaskUncertain" for event in kernel.events(task.id)
        )
    finally:
        coordinator.shutdown()
        kernel.close()


def _seed_application_cycle(kernel, coordinator, cycle_key: str):
    task = kernel.create_task(
        goal="bounded application cycle",
        source=TaskSource(
            "goal-v2-long-lived-mobile",
            "goal:goal-1",
            cycle_key,
        ),
        device_id="adb:device-1",
    )
    kernel.record_worker_event(
        task_id=task.id,
        event_type="KernelApplicationCycleAccepted",
        payload={
            "goal_id": "goal-1",
            "application_instance_id": "application-1",
            "application_cycle": 1,
            "cycle_key": cycle_key,
            "target_id": "adb:device-1",
        },
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="one bounded action",
        completion_criteria=("fresh verified effect",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    before = coordinator._observe(task.id)
    kernel_before = kernel.latest_observation(task.id)
    kernel.record_worker_event(
        task_id=task.id,
        event_type="KernelApplicationReadinessAssessed",
        payload={
            "application_id": "com.android.settings",
            "expected_application_id": "com.android.settings",
            "ready": True,
            "authenticated": True,
            "application_matches_goal": True,
            "before_observation_id": kernel_before.id,
            "before_evidence_id": before.evidence_id,
            "evidence": "ready",
        },
    )
    action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=kernel_before.id,
        action_type=ActionType.TAP,
        params={"x": 0, "y": 0},
        expected_outcome="one bounded action",
        proposed_by_call_id=f"application-cycle:{cycle_key}",
    )
    return task, action, before


def test_application_cycle_recovery_completes_durable_success_and_keeps_after_link(
    tmp_path: Path,
) -> None:
    kernel, coordinator = _coordinator(tmp_path, FinishModel())
    try:
        cycle_key = "cycle-verified-crash-window"
        task, action, before = _seed_application_cycle(
            kernel, coordinator, cycle_key
        )
        execution = kernel.record_action_execution(
            task_id=task.id,
            action_id=action.id,
            accepted=True,
            adapter_code=0,
            error=None,
        )
        after = coordinator._observe(task.id)
        after_id = kernel.latest_observation(task.id).id

        # If the process dies after the fresh re-observation but before
        # verification, readback still exposes the durable after evidence.
        incomplete = coordinator.inspect_application_cycle(task.id)
        assert incomplete.execution_id == execution.id
        assert incomplete.after_observation_id == after_id
        assert incomplete.after_evidence_id == after.evidence_id

        verification, _ = kernel.verify_action(
            task_id=task.id,
            action_id=action.id,
            before_observation_id=action.based_on_observation_id,
            after_observation_id=after_id,
            verdict=VerificationVerdict.SUCCESS,
            reason="fresh effect verified",
            evidence_refs=(after.evidence_id,),
            method=VerificationMethod.ROLE_ASSISTED,
            complete_stage=True,
            progress_summary="fresh effect verified",
        )
        assert kernel.load_task(task.id).terminal is False

        recovered = coordinator.reconcile_application_cycle(cycle_key)
        assert recovered is not None
        assert recovered.status == "confirmed_success"
        assert recovered.verification_id == verification.id
        assert recovered.after_evidence_id == after.evidence_id
        assert kernel.load_task(task.id).status is TaskStatus.COMPLETED
        assert len(kernel.list_actions(task.id)) == 1
    finally:
        coordinator.shutdown()
        kernel.close()


def test_application_cycle_recovery_treats_retryable_receipt_as_uncertain(
    tmp_path: Path,
) -> None:
    kernel, coordinator = _coordinator(tmp_path, FinishModel())
    try:
        cycle_key = "cycle-timeout-crash-window"
        task, action, _before = _seed_application_cycle(
            kernel, coordinator, cycle_key
        )
        execution = kernel.record_action_execution(
            task_id=task.id,
            action_id=action.id,
            accepted=False,
            adapter_code=-1,
            error=ExecutionError(
                "executor_action_timeout",
                "transport timed out after dispatch began",
                True,
            ),
        )

        recovered = coordinator.reconcile_application_cycle(cycle_key)
        assert recovered is not None
        assert recovered.status == "uncertain"
        assert recovered.execution_id == execution.id
        assert kernel.load_task(task.id).status is TaskStatus.FAILED
        assert len(kernel.list_actions(task.id)) == 1
        assert any(
            event.type == "TaskUncertain" for event in kernel.events(task.id)
        )
    finally:
        coordinator.shutdown()
        kernel.close()


def test_next_application_cycle_receives_prior_verified_no_progress_attempt(
    tmp_path: Path,
) -> None:
    kernel, coordinator = _coordinator(tmp_path, FinishModel())
    try:
        task, action, _before = _seed_application_cycle(
            kernel, coordinator, "cycle-prior-no-progress"
        )
        kernel.record_action_execution(
            task_id=task.id,
            action_id=action.id,
            accepted=True,
            adapter_code=0,
            error=None,
        )
        after = coordinator._observe(task.id)
        after_id = kernel.latest_observation(task.id).id
        kernel.verify_action(
            task_id=task.id,
            action_id=action.id,
            before_observation_id=action.based_on_observation_id,
            after_observation_id=after_id,
            verdict=VerificationVerdict.FAIL,
            reason="no material visual change",
            evidence_refs=(after.evidence_id,),
            method=VerificationMethod.ROLE_ASSISTED,
        )

        attempts = coordinator._application_recent_attempts(
            goal_id="goal-1", current_task_id="new-cycle"
        )

        assert len(attempts) == 1
        assert attempts[0].decision.intent is not None
        assert attempts[0].decision.intent.name == "tap"
        assert attempts[0].verification is not None
        assert attempts[0].verification.progress is False
        assert attempts[0].verification.evidence == "no material visual change"
    finally:
        coordinator.shutdown()
        kernel.close()


class FakePacket:
    retrieval_id = "retrieval-1"
    items = ()


class FakeExperience:
    def __init__(self) -> None:
        self.begun = []
        self.retrieved = []
        self.recorded = []
        self.synced = []
        self.confirmed = []

    def begin_mobile_episode_for_task(self, **kwargs):
        self.begun.append(kwargs)

    def retrieve(self, **kwargs):
        self.retrieved.append(kwargs)
        return FakePacket()

    def record_mobile_attempt(self, **kwargs):
        self.recorded.append(kwargs)

    def sync_mobile_task(self, state):
        self.synced.append(state)

    def confirm_goal_completion(self, task_id, **kwargs):
        self.confirmed.append((task_id, kwargs))


def test_kernel_canary_reuses_experience_ledger_but_promotes_only_after_goal_gate(
    tmp_path: Path,
) -> None:
    experience = FakeExperience()
    kernel, coordinator = _coordinator(
        tmp_path, FinishModel(), experience=experience
    )
    try:
        accepted = coordinator.start(
            "确认设置页",
            "request-experience",
            target_id="device-1",
            goal_id="goal-experience",
            goal_spec_revision=2,
            frozen_criteria_ids=("criterion-1",),
        )
        _wait_status(coordinator, accepted.task_id, "completed")
        assert len(experience.begun) == 1
        assert experience.retrieved
        deadline = time.time() + 5
        while not experience.recorded and time.time() < deadline:
            time.sleep(0.02)
        assert experience.recorded
        assert experience.confirmed == []

        coordinator.confirm_goal_completion(
            accepted.task_id, "goal-experience", 1
        )
        assert experience.synced
        assert experience.confirmed == [
            (
                accepted.task_id,
                {"goal_id": "goal-experience", "completion_revision": 1},
            )
        ]
    finally:
        coordinator.shutdown()
        kernel.close()
