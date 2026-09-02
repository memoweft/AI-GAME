"""Phase 6 Week 2: Gateway application service (frozen contract §3-§9).

Covers TaskGateway behavior against the Runtime Kernel:
- create Task (§5) with canonical response shape, device validation, and
  the one-active-Task-per-conversation invariant (§8);
- idempotency: same (scope, key, payload) replays the stored response
  verbatim; same key + different payload -> IDEMPOTENCY_CONFLICT (§3);
- get returns the §6 Gateway Snapshot, not a raw Store row;
- message (§7) accepts and records a UserMessageReceived event;
- control (§9) maps pause/resume/cancel/takeover onto Kernel transitions;
- conversation entry (§8) attaches to the single active Task, creates a
  Task when none is active, and errors on ambiguity;
- error mapping to the frozen §14 codes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from ai_game_console.gateway import (
    ConversationConflict,
    DeviceNotAvailable,
    DeviceNotFound,
    GatewayStore,
    IdempotencyConflict,
    IdempotencyService,
    TaskGateway,
    TaskNotActive,
    TaskNotFound,
    ValidationError,
)
from ai_game_console.gateway.device_registry import DeviceRegistry, DeviceSummary
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import (
    ActionType,
    ChannelAvailability,
    ConnectionState,
    ConsistencyStatus,
    DeviceState,
    EventActor,
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
    TaskSource,
    TaskStatus,
    VerificationMethod,
    VerificationVerdict,
)

TIMES = tuple(f"2026-08-18T09:{minute:02d}:00+00:00" for minute in range(60))
SCREENSHOT = b"\x89PNG\r\n\x1a\ngateway-test-pixels"
UI_TREE = b"<hierarchy><node resource-id='target' /></hierarchy>"


class FakeObservationProvider:
    def capture(self, device_id: str) -> RawObservation:
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
                status=ConsistencyStatus.CONSISTENT, reason=None
            ),
        )


class FakeDeviceRegistry:
    def __init__(self, devices: dict[str, str]) -> None:
        self._devices = dict(devices)

    def list_devices(self) -> tuple[DeviceSummary, ...]:
        return tuple(
            DeviceSummary(device_id, status)
            for device_id, status in self._devices.items()
        )

    def get_device(self, device_id: str) -> DeviceSummary | None:
        status = self._devices.get(device_id)
        if status is None:
            return None
        return DeviceSummary(device_id, status)

    def set_status(self, device_id: str, status: str) -> None:
        self._devices[device_id] = status


def _clock() -> Callable[[], str]:
    values = iter(TIMES * 20)
    return lambda: next(values)


def _ids() -> Callable[[], str]:
    return lambda: str(uuid4())


def _kernel(tmp_path: Path) -> RuntimeKernel:
    return RuntimeKernel(
        SQLiteRuntimeStore(tmp_path / "runtime.db"),
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=_clock(),
        id_factory=_ids(),
    )


def _harness(
    tmp_path: Path,
    devices: dict[str, str] | None = {"device-1": "AVAILABLE"},
) -> tuple[TaskGateway, RuntimeKernel, FakeDeviceRegistry | None]:
    kernel = _kernel(tmp_path)
    store = GatewayStore(tmp_path / "gateway.db")
    store.initialize()
    registry = FakeDeviceRegistry(devices) if devices is not None else None
    gateway = TaskGateway(
        kernel=kernel,
        idempotency=IdempotencyService(store),
        device_registry=registry,
    )
    return gateway, kernel, registry


def _create(
    gateway: TaskGateway,
    *,
    key: str = "key-create-1",
    goal: str = "open the game and reach the main page",
    device_id: str = "device-1",
    conversation_id: str = "conversation-1",
    client_id: str = "client-1",
    message_id: str = "message-1",
) -> dict:
    return gateway.create_task(
        client_id=client_id,
        goal=goal,
        device_id=device_id,
        conversation_id=conversation_id,
        message_id=message_id,
        idempotency_key=key,
    )


def _running_task(kernel: RuntimeKernel, device_id: str, suffix: str) -> str:
    task = kernel.create_task(
        goal=f"control {suffix}",
        source=TaskSource(
            client_id=f"client-{suffix}",
            conversation_id=f"conversation-{suffix}",
            initial_message_id=f"message-{suffix}",
        ),
        device_id=device_id,
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="running",
        completion_criteria=("done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    return task.id


# ---------------------------------------------------------------------------
# create Task (§5)
# ---------------------------------------------------------------------------


class TestCreateTask:
    def test_canonical_response_shape(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)

        response = _create(gateway)

        assert set(response) == {"task"}
        task = response["task"]
        assert set(task) == {
            "id",
            "goal",
            "status",
            "device_id",
            "current_stage",
            "last_event_sequence",
        }
        assert task["goal"] == "open the game and reach the main page"
        assert task["status"] == "CREATED"
        assert task["device_id"] == "device-1"
        assert task["current_stage"] is None
        assert task["last_event_sequence"] == 1
        stored = kernel.load_task(task["id"])
        assert stored.status is TaskStatus.CREATED

    def test_idempotent_replay_returns_identical_response(
        self, tmp_path: Path
    ) -> None:
        gateway, kernel, _ = _harness(tmp_path)

        first = _create(gateway)
        second = _create(gateway)

        assert second == first
        tasks = kernel.list_tasks_by_conversation("conversation-1")
        assert len(tasks) == 1
        assert [event.sequence for event in kernel.events(tasks[0].id)] == [1]

    def test_same_key_different_payload_conflicts(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        _create(gateway)

        with pytest.raises(IdempotencyConflict):
            _create(gateway, goal="a different goal")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("client_id", ""),
            ("goal", "   "),
            ("device_id", ""),
            ("conversation_id", ""),
            ("message_id", ""),
        ],
    )
    def test_blank_fields_rejected(
        self, tmp_path: Path, field: str, value: str
    ) -> None:
        gateway, _, _ = _harness(tmp_path)
        kwargs = dict(
            client_id="client-1",
            goal="goal",
            device_id="device-1",
            conversation_id="conversation-1",
            message_id="message-1",
        )
        kwargs[field] = value
        with pytest.raises(ValidationError):
            gateway.create_task(**kwargs, idempotency_key="key")

    def test_blank_idempotency_key_rejected(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        with pytest.raises(ValidationError):
            _create(gateway, key="   ")

    def test_unknown_device_rejected(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        with pytest.raises(DeviceNotFound):
            _create(gateway, device_id="device-missing")

    def test_unavailable_device_rejected(self, tmp_path: Path) -> None:
        gateway, _, registry = _harness(tmp_path)
        assert registry is not None
        registry.set_status("device-1", "IN_USE")
        with pytest.raises(DeviceNotAvailable):
            _create(gateway)

    def test_second_active_task_in_same_conversation_rejected(
        self, tmp_path: Path
    ) -> None:
        gateway, _, _ = _harness(tmp_path)
        _create(gateway)

        with pytest.raises(ConversationConflict):
            _create(gateway, key="key-create-2", goal="another goal")

    def test_replay_skips_revalidation(self, tmp_path: Path) -> None:
        """A replay returns the stored response even if the device is no
        longer AVAILABLE (the original request already succeeded)."""
        gateway, _, registry = _harness(tmp_path)
        assert registry is not None
        first = _create(gateway)
        registry.set_status("device-1", "UNAVAILABLE")

        second = _create(gateway)

        assert second == first

    def test_without_registry_device_check_is_skipped(self, tmp_path: Path) -> None:
        gateway, kernel, registry = _harness(tmp_path, devices=None)
        assert registry is None

        response = _create(gateway, device_id="any-device")

        assert response["task"]["device_id"] == "any-device"


# ---------------------------------------------------------------------------
# get as Gateway Snapshot (§6)
# ---------------------------------------------------------------------------


class TestGetTaskSnapshot:
    def test_fresh_task_snapshot_shape(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        created = _create(gateway)
        task_id = created["task"]["id"]

        response = gateway.get_task(task_id)

        assert set(response) == {"task"}
        snapshot = response["task"]
        assert set(snapshot) == {
            "id",
            "goal",
            "status",
            "device_id",
            "constraints",
            "current_stage",
            "completed_stages",
            "verified_facts",
            "last_observation_id",
            "last_event_sequence",
            "updated_at",
        }
        assert snapshot["id"] == task_id
        assert snapshot["status"] == "CREATED"
        assert snapshot["constraints"] == []
        assert snapshot["current_stage"] is None
        assert snapshot["completed_stages"] == []
        assert snapshot["verified_facts"] == []
        assert snapshot["last_observation_id"] is None
        assert snapshot["last_event_sequence"] == 1

    def test_snapshot_projects_stages_and_verified_facts(
        self, tmp_path: Path
    ) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        created = _create(gateway)
        task_id = created["task"]["id"]
        stage1 = kernel.create_stage(
            task_id=task_id,
            objective="reach the target page",
            completion_criteria=("target is visible",),
        )
        kernel.start_stage(task_id=task_id, stage_id=stage1.id)
        before = kernel.capture_observation(
            task_id=task_id, device_id="device-1"
        )
        kernel.propose_action(
            task_id=task_id,
            stage_id=stage1.id,
            action_id="action-1",
            based_on_observation_id=before.id,
            action_type=ActionType.TAP,
            params={"x": 400, "y": 1200},
            expected_outcome="The target page is visible",
            proposed_by_call_id="operator-1",
        )
        kernel.prepare_action_execution(task_id=task_id, action_id="action-1")
        kernel.record_action_execution(
            task_id=task_id,
            action_id="action-1",
            execution_id="execution-1",
            accepted=True,
            adapter_code=0,
            error=None,
        )
        after = kernel.capture_observation(task_id=task_id, device_id="device-1")
        fact = Fact.create(
            fact_id="fact-1",
            task_id=task_id,
            key="target.visible",
            value={"text": "verified"},
            status=FactStatus.VERIFIED,
            confidence=1.0,
            scope=FactScope.TASK,
            stage_id=None,
            source_refs=("verification:verification-1",),
            supersedes_fact_id=None,
            created_at=TIMES[30],
        )
        kernel.verify_action(
            task_id=task_id,
            action_id="action-1",
            before_observation_id=before.id,
            after_observation_id=after.id,
            verdict=VerificationVerdict.SUCCESS,
            reason="verified",
            evidence_refs=(after.screenshot.artifact.reference,),
            method=VerificationMethod.RUNTIME_RULE,
            verified_facts=(fact,),
            complete_stage=True,
        )
        stage2 = kernel.create_stage(
            task_id=task_id,
            objective="press the start button",
            completion_criteria=("game started",),
        )
        kernel.start_stage(task_id=task_id, stage_id=stage2.id)

        snapshot = gateway.get_task(task_id)["task"]

        assert snapshot["status"] == "RUNNING"
        assert snapshot["current_stage"] == {
            "id": stage2.id,
            "objective": "press the start button",
            "completion_criteria": ["game started"],
        }
        assert snapshot["completed_stages"] == [
            {
                "id": stage1.id,
                "objective": "reach the target page",
                "completion_criteria": ["target is visible"],
                "completed_at": kernel.load_stage(task_id, stage1.id).completed_at,
            }
        ]
        assert snapshot["verified_facts"] == [
            {
                "id": fact.id,
                "key": "target.visible",
                "value": {"text": "verified"},
                "confidence": 1.0,
                "created_at": fact.created_at,
            }
        ]
        assert snapshot["last_observation_id"] == after.id
        # 11 pipeline events + StageCreated + StageStarted for stage 2.
        assert snapshot["last_event_sequence"] == 13

    def test_unknown_task_not_found(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        with pytest.raises(TaskNotFound) as exc_info:
            gateway.get_task("task-missing")
        assert exc_info.value.to_dict()["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# message (§7)
# ---------------------------------------------------------------------------


class TestSendMessage:
    def test_message_accepted_and_recorded(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _create(gateway)["task"]["id"]

        response = gateway.send_message(
            task_id=task_id,
            message_id="message-2",
            conversation_id="conversation-1",
            text="please hurry up",
            idempotency_key="key-message-1",
        )

        assert response == {
            "accepted": True,
            "task_id": task_id,
            "message_id": "message-2",
            "event_sequence": 2,
        }
        event = kernel.events(task_id)[-1]
        assert event.type == "UserMessageReceived"
        assert event.actor is EventActor.USER
        assert event.payload == {
            "message_id": "message-2",
            "conversation_id": "conversation-1",
            "text": "please hurry up",
        }
        # The message alone must not move the Task state machine.
        assert kernel.load_task(task_id).status is TaskStatus.CREATED

    def test_terminal_task_rejected(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "terminal")
        kernel.apply_control(task_id=task_id, command="cancel", reason="done")

        with pytest.raises(TaskNotActive):
            gateway.send_message(
                task_id=task_id,
                message_id="message-2",
                conversation_id="conversation-terminal",
                text="keep going",
                idempotency_key="key",
            )

    def test_wrong_conversation_rejected(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        task_id = _create(gateway)["task"]["id"]

        with pytest.raises(ConversationConflict):
            gateway.send_message(
                task_id=task_id,
                message_id="message-2",
                conversation_id="conversation-other",
                text="hello",
                idempotency_key="key",
            )

    def test_idempotent_replay_and_conflict(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _create(gateway)["task"]["id"]

        first = gateway.send_message(
            task_id=task_id,
            message_id="message-2",
            conversation_id="conversation-1",
            text="note",
            idempotency_key="key-message-1",
        )
        second = gateway.send_message(
            task_id=task_id,
            message_id="message-2",
            conversation_id="conversation-1",
            text="note",
            idempotency_key="key-message-1",
        )

        assert second == first
        assert [event.sequence for event in kernel.events(task_id)] == [1, 2]
        with pytest.raises(IdempotencyConflict):
            gateway.send_message(
                task_id=task_id,
                message_id="message-2",
                conversation_id="conversation-1",
                text="different text",
                idempotency_key="key-message-1",
            )

    def test_unknown_task_not_found(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        with pytest.raises(TaskNotFound):
            gateway.send_message(
                task_id="task-missing",
                message_id="m",
                conversation_id="conversation-1",
                text="t",
                idempotency_key="key",
            )


# ---------------------------------------------------------------------------
# control (§9)
# ---------------------------------------------------------------------------


class TestControl:
    def test_pause_resume_takeover_cancel_sequence(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "control")

        paused = gateway.control(
            task_id=task_id, command="pause", reason="user", idempotency_key="k1"
        )
        assert paused == {
            "accepted": True,
            "task_id": task_id,
            "command": "pause",
            "status": "PAUSED",
            "event_sequence": 4,
        }
        resumed = gateway.control(
            task_id=task_id, command="resume", idempotency_key="k2"
        )
        assert resumed["status"] == "RUNNING"
        assert resumed["event_sequence"] == 5
        taken = gateway.control(
            task_id=task_id, command="takeover", idempotency_key="k3"
        )
        assert taken["status"] == "PAUSED"
        assert taken["event_sequence"] == 6
        cancelled = gateway.control(
            task_id=task_id, command="cancel", reason="giving up", idempotency_key="k4"
        )
        assert cancelled["status"] == "CANCELLED"
        assert cancelled["event_sequence"] == 7

        types = [event.type for event in kernel.events(task_id)]
        assert types[3:] == [
            "TaskPaused",
            "TaskResumed",
            "UserTakeover",
            "TaskCancelled",
        ]

    def test_pause_from_created_and_resume_return_to_created(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        task_id = _create(gateway)["task"]["id"]
        paused = gateway.control(
            task_id=task_id, command="pause", idempotency_key="key"
        )
        assert paused["status"] == "PAUSED"
        resumed = gateway.control(
            task_id=task_id, command="resume", idempotency_key="key-2"
        )
        assert resumed["status"] == "CREATED"

    def test_unknown_command_rejected(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "bogus")
        with pytest.raises(ValidationError):
            gateway.control(task_id=task_id, command="bogus", idempotency_key="key")

    def test_unknown_task_not_found(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        with pytest.raises(TaskNotFound):
            gateway.control(
                task_id="task-missing", command="pause", idempotency_key="key"
            )

    def test_idempotent_replay_and_conflict(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "idem")

        first = gateway.control(
            task_id=task_id, command="pause", idempotency_key="key-control-1"
        )
        second = gateway.control(
            task_id=task_id, command="pause", idempotency_key="key-control-1"
        )

        assert second == first
        assert [event.type for event in kernel.events(task_id)][3:] == ["TaskPaused"]
        with pytest.raises(IdempotencyConflict):
            gateway.control(
                task_id=task_id, command="resume", idempotency_key="key-control-1"
            )

    def test_replay_after_task_terminal(self, tmp_path: Path) -> None:
        """Idempotency replays the stored response; it does not re-apply
        the control to a now-terminal Task."""
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "replay")
        paused = gateway.control(
            task_id=task_id, command="pause", idempotency_key="key-pause"
        )
        gateway.control(task_id=task_id, command="cancel", idempotency_key="key-cancel")

        replayed = gateway.control(
            task_id=task_id, command="pause", idempotency_key="key-pause"
        )

        assert replayed == paused


# ---------------------------------------------------------------------------
# conversation entry (§8)
# ---------------------------------------------------------------------------


class TestConversationMessage:
    def test_creates_task_when_none_active(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)

        response = gateway.send_conversation_message(
            client_id="client-1",
            conversation_id="conversation-new",
            message_id="message-1",
            text="open the game for me",
            device_id="device-1",
            idempotency_key="key-conv-1",
        )

        assert response["accepted"] is True
        assert response["created"] is True
        assert response["message_id"] == "message-1"
        assert response["event_sequence"] == 1
        task = kernel.load_task(response["task_id"])
        assert task.goal == "open the game for me"
        assert task.status is TaskStatus.CREATED
        assert task.source.conversation_id == "conversation-new"
        assert task.source.initial_message_id == "message-1"
        assert task.device_id == "device-1"

    def test_attaches_to_active_task(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _create(gateway)["task"]["id"]

        response = gateway.send_conversation_message(
            client_id="client-1",
            conversation_id="conversation-1",
            message_id="message-2",
            text="any progress?",
            device_id="device-1",
            idempotency_key="key-conv-2",
        )

        assert response == {
            "accepted": True,
            "task_id": task_id,
            "message_id": "message-2",
            "event_sequence": 2,
            "created": False,
        }
        event = kernel.events(task_id)[-1]
        assert event.type == "UserMessageReceived"
        assert kernel.list_tasks_by_conversation("conversation-1") == (
            kernel.load_task(task_id),
        )

    def test_conflict_when_multiple_active(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        _create(gateway)
        # Simulate the ambiguous state directly at the Kernel (e.g. a
        # legacy import); the Gateway must never guess.
        kernel.create_task(
            goal="second active task",
            source=TaskSource(
                client_id="client-2",
                conversation_id="conversation-1",
                initial_message_id="message-9",
            ),
            device_id="device-1",
        )

        with pytest.raises(ConversationConflict):
            gateway.send_conversation_message(
                client_id="client-1",
                conversation_id="conversation-1",
                message_id="message-2",
                text="which task is this for?",
                device_id="device-1",
                idempotency_key="key",
            )

    def test_after_terminal_task_creates_new(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)
        task_id = _create(gateway)["task"]["id"]
        kernel.apply_control(task_id=task_id, command="cancel", reason="done")

        response = gateway.send_conversation_message(
            client_id="client-1",
            conversation_id="conversation-1",
            message_id="message-3",
            text="now do the other thing",
            device_id="device-1",
            idempotency_key="key",
        )

        assert response["created"] is True
        assert response["task_id"] != task_id
        assert kernel.load_task(response["task_id"]).goal == "now do the other thing"

    def test_create_path_requires_available_device(self, tmp_path: Path) -> None:
        gateway, _, _ = _harness(tmp_path)
        with pytest.raises(DeviceNotFound):
            gateway.send_conversation_message(
                client_id="client-1",
                conversation_id="conversation-1",
                message_id="message-1",
                text="open the game",
                device_id="device-missing",
                idempotency_key="key",
            )

    def test_idempotent_replay(self, tmp_path: Path) -> None:
        gateway, kernel, _ = _harness(tmp_path)

        first = gateway.send_conversation_message(
            client_id="client-1",
            conversation_id="conversation-1",
            message_id="message-1",
            text="open the game",
            device_id="device-1",
            idempotency_key="key-conv-1",
        )
        second = gateway.send_conversation_message(
            client_id="client-1",
            conversation_id="conversation-1",
            message_id="message-1",
            text="open the game",
            device_id="device-1",
            idempotency_key="key-conv-1",
        )

        assert second == first
        assert len(kernel.list_tasks_by_conversation("conversation-1")) == 1
        with pytest.raises(IdempotencyConflict):
            gateway.send_conversation_message(
                client_id="client-1",
                conversation_id="conversation-1",
                message_id="message-1",
                text="different goal",
                device_id="device-1",
                idempotency_key="key-conv-1",
            )


class RecordingKernelWorker:
    def __init__(self, kernel: RuntimeKernel) -> None:
        self.kernel = kernel
        self.submitted: list[str] = []
        self.controls: list[tuple[str, str]] = []

    def submit(self, task_id: str) -> None:
        self.submitted.append(task_id)
        stage = self.kernel.create_stage(
            task_id=task_id,
            objective="worker-owned stage",
            completion_criteria=("visible",),
        )
        self.kernel.start_stage(task_id=task_id, stage_id=stage.id)

    def control(self, task_id: str, action: str):
        self.controls.append((task_id, action))
        command = "cancel" if action == "stop" else action
        return self.kernel.apply_control(task_id=task_id, command=command)


class FailingKernelWorker:
    def submit(self, task_id: str) -> None:
        del task_id
        raise RuntimeError("queue unavailable")


def test_gateway_worker_hook_submits_created_task_and_owns_control(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    store = GatewayStore(tmp_path / "gateway.db")
    store.initialize()
    worker = RecordingKernelWorker(kernel)
    gateway = TaskGateway(
        kernel=kernel,
        idempotency=IdempotencyService(store),
        worker=worker,
    )

    task_id = _create(gateway)["task"]["id"]
    assert worker.submitted == [task_id]
    assert kernel.load_task(task_id).status is TaskStatus.RUNNING

    response = gateway.control(
        task_id=task_id,
        command="pause",
        reason="owner pause",
        idempotency_key="worker-pause",
    )
    assert response["status"] == "PAUSED"
    assert worker.controls == [(task_id, "pause")]


def test_gateway_worker_submission_failure_terminalizes_created_task(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    store = GatewayStore(tmp_path / "gateway.db")
    store.initialize()
    gateway = TaskGateway(
        kernel=kernel,
        idempotency=IdempotencyService(store),
        worker=FailingKernelWorker(),
    )

    with pytest.raises(RuntimeError, match="queue unavailable"):
        _create(gateway)

    tasks = kernel.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].status is TaskStatus.FAILED
    assert tasks[0].failure_state is not None
    assert tasks[0].failure_state.code == "worker_submission_failed"
    assert kernel.events(tasks[0].id)[-1].type == "TaskFailed"
