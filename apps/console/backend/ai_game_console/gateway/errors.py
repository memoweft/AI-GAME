"""Gateway error model (frozen contract §14).

Service-layer methods raise ``GatewayError`` subclasses carrying the
contract's error ``code``. The HTTP layer (Week 3) renders them as::

    {"error": {"code": ..., "message": ..., "retryable": ..., "details": ...}}

No vendor, ADB, or traceback details may cross this boundary.
"""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """Base class for every Gateway-facing contract error."""

    code: str = "INTERNAL_ERROR"
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class ValidationError(GatewayError):
    code = "VALIDATION_ERROR"


class TaskNotFound(GatewayError):
    code = "TASK_NOT_FOUND"


class TaskNotActive(GatewayError):
    code = "TASK_NOT_ACTIVE"


class DeviceNotFound(GatewayError):
    code = "DEVICE_NOT_FOUND"


class DeviceNotAvailable(GatewayError):
    code = "DEVICE_NOT_AVAILABLE"
    retryable = True


class ConversationConflict(GatewayError):
    code = "CONVERSATION_CONFLICT"


class IdempotencyConflict(GatewayError):
    code = "IDEMPOTENCY_CONFLICT"


class EventCursorInvalid(GatewayError):
    code = "EVENT_CURSOR_INVALID"


class LegacyTaskWriteDisabled(GatewayError):
    code = "LEGACY_TASK_WRITE_DISABLED"


class InternalError(GatewayError):
    """Unexpected failure; the message never leaks vendor/traceback detail."""

    code = "INTERNAL_ERROR"
