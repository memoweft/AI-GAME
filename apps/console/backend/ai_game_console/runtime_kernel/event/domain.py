from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


class EventActor(StrEnum):
    USER = "user"
    GATEWAY = "gateway"
    RUNTIME = "runtime"
    MODEL = "model"
    DEVICE = "device"


@dataclass(frozen=True, slots=True)
class RuntimeEventDraft:
    id: str
    type: str
    actor: EventActor
    payload: Mapping[str, Any] = field(default_factory=dict)
    causation_id: str | None = None
    correlation_id: str | None = None
    created_at: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        _required(self.id, "event.id")
        _required(self.type, "event.type")
        _utc_timestamp(self.created_at, "event.created_at")
        if self.schema_version < 1:
            raise ValueError("event.schema_version must be positive")


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    id: str
    task_id: str
    sequence: int
    type: str
    actor: EventActor
    payload: Mapping[str, Any]
    causation_id: str | None
    correlation_id: str | None
    created_at: str
    schema_version: int

    def __post_init__(self) -> None:
        _required(self.id, "event.id")
        _required(self.task_id, "event.task_id")
        if self.sequence < 1:
            raise ValueError("event.sequence must be positive")
        _required(self.type, "event.type")
        _utc_timestamp(self.created_at, "event.created_at")
        if self.schema_version < 1:
            raise ValueError("event.schema_version must be positive")


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def _utc_timestamp(value: str, name: str) -> str:
    _required(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
    return value

