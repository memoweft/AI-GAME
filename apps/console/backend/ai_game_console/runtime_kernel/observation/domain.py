from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class ChannelAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class Orientation(StrEnum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"
    UNKNOWN = "unknown"


class KeyboardState(StrEnum):
    SHOWN = "shown"
    HIDDEN = "hidden"
    UNKNOWN = "unknown"


class ConnectionState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"


class ConsistencyStatus(StrEnum):
    CONSISTENT = "consistent"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    reference: str
    content_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _required(self.reference, "artifact_ref.reference")
        if self.reference.startswith(("/", "\\")) or "\\" in self.reference:
            raise ValueError("artifact reference must be a portable relative reference")
        if any(part in {"", ".", ".."} for part in self.reference.split("/")):
            raise ValueError("artifact reference contains an invalid path component")
        _required(self.content_type, "artifact_ref.content_type")
        if self.size_bytes < 1:
            raise ValueError("artifact_ref.size_bytes must be positive")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("artifact_ref.sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ScreenshotChannel:
    status: ChannelAvailability
    artifact: ArtifactRef
    width: int
    height: int
    mime_type: str
    captured_at: str

    def __post_init__(self) -> None:
        if self.status is not ChannelAvailability.AVAILABLE:
            raise ValueError("a committed screenshot channel must be AVAILABLE")
        if self.width < 1 or self.height < 1:
            raise ValueError("screenshot dimensions must be positive")
        _required(self.mime_type, "screenshot.mime_type")
        if self.mime_type != self.artifact.content_type:
            raise ValueError("screenshot mime_type must match its ArtifactRef")
        _utc_timestamp(self.captured_at, "screenshot.captured_at")


@dataclass(frozen=True, slots=True)
class UiTreeChannel:
    status: ChannelAvailability
    artifact: ArtifactRef | None
    captured_at: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        _utc_timestamp(self.captured_at, "ui_tree.captured_at")
        if self.status is ChannelAvailability.AVAILABLE:
            if self.artifact is None:
                raise ValueError("AVAILABLE UI Tree requires an ArtifactRef")
            if self.error_code is not None:
                raise ValueError("AVAILABLE UI Tree cannot carry error_code")
        elif self.artifact is not None:
            raise ValueError("unavailable/failed UI Tree cannot carry an ArtifactRef")


@dataclass(frozen=True, slots=True)
class DeviceState:
    status: ChannelAvailability
    foreground_app: str | None
    screen_size: tuple[int, int]
    orientation: Orientation
    keyboard_state: KeyboardState
    connection_state: ConnectionState
    captured_at: str

    def __post_init__(self) -> None:
        if self.status is not ChannelAvailability.AVAILABLE:
            raise ValueError("a committed Observation requires AVAILABLE Device State")
        if (
            len(self.screen_size) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                for value in self.screen_size
            )
        ):
            raise ValueError("device_state.screen_size must contain positive dimensions")
        _utc_timestamp(self.captured_at, "device_state.captured_at")


@dataclass(frozen=True, slots=True)
class ObservationConsistency:
    status: ConsistencyStatus
    reason: str | None

    def __post_init__(self) -> None:
        if self.status is ConsistencyStatus.CONSISTENT and self.reason is not None:
            raise ValueError("consistent Observation cannot carry a degradation reason")
        if self.status is ConsistencyStatus.DEGRADED:
            _required(self.reason or "", "consistency.reason")


@dataclass(frozen=True, slots=True)
class Observation:
    id: str
    task_id: str
    device_id: str
    captured_at: str
    capture_started_at: str
    capture_completed_at: str
    screenshot: ScreenshotChannel
    ui_tree: UiTreeChannel
    device_state: DeviceState
    consistency: ObservationConsistency

    def __post_init__(self) -> None:
        _required(self.id, "observation.id")
        _required(self.task_id, "observation.task_id")
        _required(self.device_id, "observation.device_id")
        captured = _utc_timestamp(self.captured_at, "observation.captured_at")
        started = _utc_timestamp(
            self.capture_started_at, "observation.capture_started_at"
        )
        completed = _utc_timestamp(
            self.capture_completed_at, "observation.capture_completed_at"
        )
        if started > completed:
            raise ValueError("Observation capture window is reversed")
        if captured != completed:
            raise ValueError("captured_at must equal capture_completed_at")


@dataclass(frozen=True, slots=True)
class RawScreenshot:
    status: ChannelAvailability
    content: bytes | None
    width: int | None
    height: int | None
    captured_at: str
    error_code: str | None = None
    content_type: str = "image/png"

    def __post_init__(self) -> None:
        _utc_timestamp(self.captured_at, "raw_screenshot.captured_at")
        if self.status is ChannelAvailability.AVAILABLE:
            if not self.content or not self.width or not self.height:
                raise ValueError("AVAILABLE screenshot requires bytes and dimensions")
            if self.error_code is not None:
                raise ValueError("AVAILABLE screenshot cannot carry error_code")
        elif self.content is not None:
            raise ValueError("failed screenshot cannot carry content")


@dataclass(frozen=True, slots=True)
class RawUiTree:
    status: ChannelAvailability
    content: bytes | None
    captured_at: str
    error_code: str | None = None
    content_type: str = "application/xml"

    def __post_init__(self) -> None:
        _utc_timestamp(self.captured_at, "raw_ui_tree.captured_at")
        if self.status is ChannelAvailability.AVAILABLE:
            if not self.content:
                raise ValueError("AVAILABLE UI Tree requires content")
            if self.error_code is not None:
                raise ValueError("AVAILABLE UI Tree cannot carry error_code")
        elif self.content is not None:
            raise ValueError("unavailable/failed UI Tree cannot carry content")


@dataclass(frozen=True, slots=True)
class RawObservation:
    device_id: str
    capture_started_at: str
    capture_completed_at: str
    screenshot: RawScreenshot
    ui_tree: RawUiTree
    device_state: DeviceState
    consistency: ObservationConsistency

    def __post_init__(self) -> None:
        _required(self.device_id, "raw_observation.device_id")
        started = _utc_timestamp(
            self.capture_started_at, "raw_observation.capture_started_at"
        )
        completed = _utc_timestamp(
            self.capture_completed_at, "raw_observation.capture_completed_at"
        )
        if started > completed:
            raise ValueError("raw Observation capture window is reversed")


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def _utc_timestamp(value: str, name: str) -> datetime:
    _required(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
    return parsed
