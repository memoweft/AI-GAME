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
    TaskSource,
    TaskStatus,
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
