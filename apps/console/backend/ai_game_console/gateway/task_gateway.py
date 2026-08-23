"""TaskGateway — application service for the frozen Gateway contract.

Responsibilities (contract §1):

* translate client requests into Runtime Kernel commands;
* persist idempotency keys and replay the canonical first response;
* enforce the unique conversation <-> active Task association;
* project the Gateway Snapshot (contract §6);
* map Kernel errors to the frozen §14 error codes.

The Gateway never touches Kernel Store tables, never acquires Device
Leases, and keeps no second Active Task state machine (contract §15).
All task state reads and writes go through ``RuntimeKernel`` public
methods.
"""

from __future__ import annotations

from typing import Any

from ..runtime_kernel import (
    ControlCommand,
    ControlError,
    InvalidControlTransition,
    RecordNotFound,
    RuntimeKernel,
    Task,
    TaskSource,
)
from .conversations import ConversationService
from .device_registry import DeviceRegistry
from .errors import (
    ConversationConflict,
    DeviceNotAvailable,
    DeviceNotFound,
    TaskNotActive,
    TaskNotFound,
    ValidationError,
)
from .idempotency import (
    SCOPE_CONVERSATION_MESSAGE,
    SCOPE_TASK_CONTROL,
    SCOPE_TASK_CREATE,
    SCOPE_TASK_MESSAGE,
    IdempotencyService,
)
from .snapshot import build_task_snapshot


def _require(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must not be blank")
    return value


class TaskGateway:
    def __init__(
        self,
        *,
        kernel: RuntimeKernel,
        idempotency: IdempotencyService,
        conversation: ConversationService | None = None,
        device_registry: DeviceRegistry | None = None,
        worker: Any | None = None,
    ) -> None:
        self._kernel = kernel
        self._idempotency = idempotency
        self._conversation = conversation or ConversationService(kernel)
        self._device_registry = device_registry
        self._worker = worker

    # -- create (contract §5) ------------------------------------------------

    def create_task(
        self,
        *,
        client_id: str,
        goal: str,
        device_id: str,
        conversation_id: str,
        message_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _require(idempotency_key, "Idempotency-Key")
        return self._idempotency.execute_once(
            scope=SCOPE_TASK_CREATE,
            key=idempotency_key,
            payload={
                "client_id": client_id,
                "goal": goal,
                "device_id": device_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
            },
            operation=lambda: self._create_task_inner(
                client_id=client_id,
                goal=goal,
                device_id=device_id,
                conversation_id=conversation_id,
                message_id=message_id,
            ),
        )

    def _create_task_inner(
        self,
        *,
        client_id: str,
        goal: str,
        device_id: str,
        conversation_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        _require(client_id, "client_id")
        _require(goal, "goal")
        _require(device_id, "device_id")
        _require(conversation_id, "conversation_id")
        _require(message_id, "message_id")
        self._validate_device(device_id)
        # A conversation may own at most one active Task (contract §8);
        # refusing a second creation keeps that invariant unbreakable.
        if self._conversation.resolve(conversation_id) is not None:
            raise ConversationConflict(
                f"Conversation {conversation_id} already has an active Task"
            )
        task = self._kernel.create_task(
            goal=goal,
            source=TaskSource(
                client_id=client_id,
                conversation_id=conversation_id,
                initial_message_id=message_id,
            ),
            device_id=device_id,
        )
        self._submit_worker(task)
        events = self._kernel.events(task.id)
        return {
            "task": {
                "id": task.id,
                "goal": task.goal,
                "status": task.status.value,
                "device_id": task.device_id,
                "current_stage": None,
                "last_event_sequence": events[-1].sequence if events else 1,
            }
        }

    # -- get as Snapshot (contract §6) ---------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        _require(task_id, "task_id")
        task = self._load_task_or_error(task_id)
        snapshot = build_task_snapshot(self._kernel, task)
        return {"task": snapshot.to_dict()}

    # -- message (contract §7) -----------------------------------------------

    def send_message(
        self,
        *,
        task_id: str,
        message_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _require(idempotency_key, "Idempotency-Key")
        return self._idempotency.execute_once(
            scope=SCOPE_TASK_MESSAGE,
            key=idempotency_key,
            payload={
                "task_id": task_id,
                "message_id": message_id,
                "conversation_id": conversation_id,
                "text": text,
            },
            operation=lambda: self._send_message_inner(
                task_id=task_id,
                message_id=message_id,
                conversation_id=conversation_id,
                text=text,
            ),
        )

    def _send_message_inner(
        self,
        *,
        task_id: str,
        message_id: str,
        conversation_id: str,
        text: str,
    ) -> dict[str, Any]:
        _require(task_id, "task_id")
        _require(message_id, "message_id")
        _require(conversation_id, "conversation_id")
        _require(text, "text")
        task = self._load_task_or_error(task_id)
        if task.terminal:
            raise TaskNotActive(
                f"Task {task_id} is terminal ({task.status.value})"
            )
        if task.source.conversation_id != conversation_id:
            raise ConversationConflict(
                f"Task {task_id} belongs to a different conversation"
            )
        event = self._kernel.record_user_message(
            task_id=task_id,
            message_id=message_id,
            conversation_id=conversation_id,
            text=text,
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "message_id": message_id,
            "event_sequence": event.sequence,
        }

    # -- control (contract §9) ------------------------------------------------

    def control(
        self,
        *,
        task_id: str,
        command: str,
        reason: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _require(idempotency_key, "Idempotency-Key")
        return self._idempotency.execute_once(
            scope=SCOPE_TASK_CONTROL,
            key=idempotency_key,
            payload={
                "task_id": task_id,
                "command": command,
                "reason": reason,
            },
            operation=lambda: self._control_inner(
                task_id=task_id, command=command, reason=reason
            ),
        )

    def _control_inner(
        self, *, task_id: str, command: str, reason: str | None
    ) -> dict[str, Any]:
        _require(task_id, "task_id")
        _require(command, "command")
        try:
            parsed_command = ControlCommand(command)
            if self._worker is not None:
                state = self._worker.control(
                    task_id,
                    "stop" if parsed_command is ControlCommand.CANCEL else parsed_command.value,
                )
                del state
                task = self._kernel.load_task(task_id)
                events = self._kernel.events(task_id)
                return {
                    "accepted": True,
                    "task_id": task_id,
                    "command": parsed_command.value,
                    "status": task.status.value,
                    "event_sequence": events[-1].sequence if events else 0,
                }
            result = self._kernel.apply_control(
                task_id=task_id, command=parsed_command, reason=reason
            )
        except RecordNotFound as exc:
            raise TaskNotFound(f"Task {task_id} was not found") from exc
        except InvalidControlTransition as exc:
            raise TaskNotActive(str(exc)) from exc
        except ControlError as exc:
            raise ValidationError(str(exc)) from exc
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return {
            "accepted": True,
            "task_id": task_id,
            "command": (
                command.value if isinstance(command, ControlCommand) else command
            ),
            "status": result.task.status.value,
            "event_sequence": result.event.sequence,
        }

    # -- conversation entry (contract §8) --------------------------------------

    def send_conversation_message(
        self,
        *,
        client_id: str,
        conversation_id: str,
        message_id: str,
        text: str,
        device_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _require(idempotency_key, "Idempotency-Key")
        return self._idempotency.execute_once(
            scope=SCOPE_CONVERSATION_MESSAGE,
            key=idempotency_key,
            payload={
                "client_id": client_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "text": text,
                "device_id": device_id,
            },
            operation=lambda: self._send_conversation_message_inner(
                client_id=client_id,
                conversation_id=conversation_id,
                message_id=message_id,
                text=text,
                device_id=device_id,
            ),
        )

    def _send_conversation_message_inner(
        self,
        *,
        client_id: str,
        conversation_id: str,
        message_id: str,
        text: str,
        device_id: str,
    ) -> dict[str, Any]:
        _require(client_id, "client_id")
        _require(conversation_id, "conversation_id")
        _require(message_id, "message_id")
        _require(text, "text")
        active = self._conversation.resolve(conversation_id)
        if active is not None:
            # Rule 1: user input for the existing active Task, never a
            # new goal.
            event = self._kernel.record_user_message(
                task_id=active.id,
                message_id=message_id,
                conversation_id=conversation_id,
                text=text,
            )
            return {
                "accepted": True,
                "task_id": active.id,
                "message_id": message_id,
                "event_sequence": event.sequence,
                "created": False,
            }
        # Rule 2: no active Task, so the message is a new goal.
        _require(device_id, "device_id")
        self._validate_device(device_id)
        task = self._kernel.create_task(
            goal=text,
            source=TaskSource(
                client_id=client_id,
                conversation_id=conversation_id,
                initial_message_id=message_id,
            ),
            device_id=device_id,
        )
        self._submit_worker(task)
        events = self._kernel.events(task.id)
        return {
            "accepted": True,
            "task_id": task.id,
            "message_id": message_id,
            "event_sequence": events[-1].sequence if events else 1,
            "created": True,
        }

    # -- shared helpers ---------------------------------------------------------

    def _validate_device(self, device_id: str) -> None:
        if self._device_registry is None:
            return
        device = self._device_registry.get_device(device_id)
        if device is None:
            raise DeviceNotFound(f"Device {device_id} was not found")
        if device.status != "AVAILABLE":
            raise DeviceNotAvailable(
                f"Device {device_id} is {device.status}"
            )

    def _load_task_or_error(self, task_id: str) -> Task:
        try:
            return self._kernel.load_task(task_id)
        except RecordNotFound as exc:
            raise TaskNotFound(f"Task {task_id} was not found") from exc

    def _submit_worker(self, task: Task) -> None:
        if self._worker is None:
            return
        try:
            self._worker.submit(task.id)
        except Exception:
            self._kernel.fail_task(
                task_id=task.id,
                code="worker_submission_failed",
                summary="Gateway could not submit the Task to its configured worker.",
            )
            raise
