"""Strict, safe wire models for the long-lived execution v2 adapter.

The objects here deliberately model an *adapter* boundary.  Canonical task
state, revisions and events remain owned by AgentRuntime; this module only
validates the externally visible request shape before it is handed to a port.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


TASK_STATUSES = frozenset({
    "scheduled", "running", "waiting_time", "waiting_event", "recovering",
    "replanning", "paused", "user_takeover", "needs_user_input", "succeeded",
    "failed", "cancelled",
})
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
CONTROL_ACTIONS = frozenset({
    "pause", "resume", "cancel", "takeover", "release_takeover",
})
REVISION_KINDS = frozenset({
    "add", "revise", "reprioritize", "reschedule", "change_stop_condition",
})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DSHOriginIdentity(StrictModel):
    """DSH provenance only; it is never the v2 authorization subject."""
    dsh_session_id: str = Field(min_length=1, max_length=256)
    dsh_turn_id: str | int
    tool_call_id: str = Field(min_length=1, max_length=256)
    root_call_id: str = Field(min_length=1, max_length=256)

    @field_validator("dsh_session_id", "tool_call_id", "root_call_id")
    @classmethod
    def inert(cls, value: str) -> str:
        return _inert(value)

    @field_validator("dsh_turn_id")
    @classmethod
    def turn(cls, value: str | int) -> str | int:
        if isinstance(value, bool):
            raise ValueError("dsh_turn_id must be a string or integer")
        if isinstance(value, int):
            if value < 0:
                raise ValueError("dsh_turn_id must not be negative")
            return value
        return _inert(value)


class CapabilityAuthContext(StrictModel):
    """Authenticated local capability/controller identity injected by the host."""

    principal_id: str = Field(min_length=1, max_length=256)
    controller_id: str = Field(min_length=1, max_length=256)

    @field_validator("principal_id", "controller_id")
    @classmethod
    def inert(cls, value: str) -> str:
        return _inert(value)

class V2Goal(StrictModel):
    summary: str = Field(min_length=1, max_length=10_000)
    task_kind: Literal["bounded", "open_ended"] = "bounded"
    stop_condition: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] | None = None

    @field_validator("summary")
    @classmethod
    def nonblank_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be blank")
        return value


class CreateTaskRequest(StrictModel):
    origin: DSHOriginIdentity
    goal: V2Goal
    client_request_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    # Pydantic otherwise coerces ``True`` to ``1`` and strings to integers.
    # Priority is persisted into the immutable create envelope, so its type is
    # part of the recovery contract rather than a convenience input coercion.
    priority: int = Field(default=50, ge=0, le=100, strict=True)
    device_profile_id: str | None = Field(default=None, max_length=256)
    # The caller chooses a supported runner family, never an implementation
    # version.  The execution service resolves and freezes the version before
    # the canonical Task is created.
    runner_kind: Literal["android_ui_agent"] | None = None
    authorization_mode: Literal["full-access", "allowed-once"]

    @field_validator("client_request_id", "idempotency_key", "device_profile_id")
    @classmethod
    def request_key(cls, value: str | None) -> str | None:
        return _inert(value) if value is not None else None


class RevisionRequest(StrictModel):
    origin: DSHOriginIdentity
    revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    base_revision: int = Field(ge=0)
    kind: Literal[
        "add", "revise", "reprioritize", "reschedule", "change_stop_condition",
    ]
    instruction: str = Field(min_length=1, max_length=10_000)
    effective_boundary: Literal["after_current_action", "at_checkpoint"] = "after_current_action"
    authorization_mode: Literal["full-access", "allowed-once"]

    @field_validator("revision_id", "idempotency_key")
    @classmethod
    def operation_key(cls, value: str) -> str:
        return _inert(value)

    @field_validator("instruction")
    @classmethod
    def nonblank_instruction(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("instruction must not be blank")
        return value


class ControlRequest(StrictModel):
    origin: DSHOriginIdentity
    control_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=0)
    action: Literal["pause", "resume", "cancel", "takeover", "release_takeover"]
    authorization_mode: Literal["full-access", "allowed-once"]

    @field_validator("control_id", "idempotency_key")
    @classmethod
    def operation_key(cls, value: str) -> str:
        return _inert(value)


class AnswerV2Request(StrictModel):
    origin: DSHOriginIdentity
    question_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=128)
    value: Any
    authorization_mode: Literal["full-access", "allowed-once"]

    @field_validator("question_id", "idempotency_key")
    @classmethod
    def operation_key(cls, value: str) -> str:
        return _inert(value)


class ArchiveRequest(StrictModel):
    origin: DSHOriginIdentity
    idempotency_key: str = Field(min_length=1, max_length=128)
    authorization_mode: Literal["full-access", "allowed-once"]

    @field_validator("idempotency_key")
    @classmethod
    def operation_key(cls, value: str) -> str:
        return _inert(value)


class DeviceProfileRequest(StrictModel):
    origin: DSHOriginIdentity
    idempotency_key: str = Field(min_length=1, max_length=128)
    profile: dict[str, Any] = Field(default_factory=dict)
    authorization_mode: Literal["full-access", "allowed-once"]

    @field_validator("idempotency_key")
    @classmethod
    def operation_key(cls, value: str) -> str:
        return _inert(value)


def _inert(value: str) -> str:
    value = value.strip()
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
    if not value or any(character not in allowed for character in value):
        raise ValueError("identity contains unsupported characters")
    return value


__all__ = [
    "AnswerV2Request", "ArchiveRequest", "CONTROL_ACTIONS", "CapabilityAuthContext", "ControlRequest",
    "DSHOriginIdentity",
    "CreateTaskRequest", "DeviceProfileRequest", "REVISION_KINDS",
    "RevisionRequest", "TASK_STATUSES", "TERMINAL_TASK_STATUSES", "V2Goal",
]
