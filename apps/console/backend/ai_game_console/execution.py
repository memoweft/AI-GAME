from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


GuiActionName = Literal["tap", "keyevent", "text", "swipe", "long_press"]


@dataclass(frozen=True, slots=True)
class GuiAction:
    """One deliberately small, indivisible device-input request.

    ``text`` is permitted only for the text action. Consumers must never place
    it in an event, diagnostic message, or response payload.
    """

    target_id: str
    action: GuiActionName
    x: int | None = None
    y: int | None = None
    end_x: int | None = None
    end_y: int | None = None
    duration_ms: int | None = None
    keycode: str | None = None
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AndroidScreenshot:
    """One decoded Android screenshot captured at a particular loop step."""

    png_bytes: bytes
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ExecutorProbeResult:
    status: Literal["ready", "not_configured", "stopped", "unavailable"]
    configured: bool
    detail: str
    blocker: dict[str, str] | None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True, slots=True)
class ActionTransportResult:
    """Transport acknowledgement, explicitly not a UI-completion assertion."""

    accepted: bool
    detail: str


class GuiExecutor(Protocol):
    """Injectable physical-execution boundary used by the control plane."""

    serial: str | None

    def probe(self) -> ExecutorProbeResult: ...

    def execute(self, action: GuiAction) -> ActionTransportResult: ...
