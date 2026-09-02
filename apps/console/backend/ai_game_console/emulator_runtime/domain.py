from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class EmulatorProfileState(StrEnum):
    READY = "ready"
    OFFLINE = "offline"
    DRIFTED = "drifted"
    DISABLED = "disabled"


class SettingsRunStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RECOVERING = "recovering"
    SUCCEEDED = "succeeded"
    USER_TAKEOVER = "user_takeover"
    REPLANNING = "replanning"
    INTEGRITY_BLOCKED = "integrity_blocked"


@dataclass(frozen=True, slots=True)
class EmulatorFingerprint:
    """The deliberately small, non-secret emulator identity fingerprint."""

    emulator_marker: bool
    api_level: int
    abi: str
    resolution: str
    density: int | None = None
    # Domain-separated digest of ``ro.build.fingerprint``. The raw property
    # never enters Profile storage or any projection.
    android_build_digest: str | None = None

    def __post_init__(self) -> None:
        if self.emulator_marker is not True:
            raise ValueError("emulator_marker must be true")
        if isinstance(self.api_level, bool) or self.api_level < 1:
            raise ValueError("api_level must be positive")
        _text(self.abi, "abi")
        _text(self.resolution, "resolution")
        if self.density is not None and (
            isinstance(self.density, bool) or self.density < 1
        ):
            raise ValueError("density must be positive when supplied")
        if self.android_build_digest is not None and (
            not isinstance(self.android_build_digest, str)
            or len(self.android_build_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.android_build_digest)
        ):
            raise ValueError("android_build_digest must be a SHA-256 digest")

    def as_dict(self) -> dict[str, Any]:
        return {
            "emulator_marker": self.emulator_marker,
            "api_level": self.api_level,
            "abi": self.abi,
            "resolution": self.resolution,
            "density": self.density,
            "android_build_digest": self.android_build_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EmulatorFingerprint":
        return cls(
            emulator_marker=value["emulator_marker"],
            api_level=value["api_level"],
            abi=value["abi"],
            resolution=value["resolution"],
            density=value.get("density"),
            android_build_digest=value.get("android_build_digest"),
        )


@dataclass(frozen=True, slots=True)
class EmulatorProbe:
    """Read-only probe result.  ``transport_serial`` is internal only."""

    transport_serial: str
    state: EmulatorProfileState
    boot_id: str | None
    fingerprint: EmulatorFingerprint | None
    capabilities: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        _serial(self.transport_serial)
        if self.state is EmulatorProfileState.READY:
            _text(self.boot_id or "", "boot_id")
            if self.fingerprint is None:
                raise ValueError("ready emulator probe requires fingerprint")
            if self.error_code is not None:
                raise ValueError("ready emulator probe cannot carry error_code")
        if self.error_code is not None:
            _code(self.error_code)
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        for capability in self.capabilities:
            _code(capability)


@dataclass(frozen=True, slots=True)
class EmulatorProfile:
    """Durable profile owned by one local capability principal.

    The owner pair is a non-secret ``principal_id`` + ``controller_id`` from
    the authenticated local capability context.  It is deliberately internal:
    DSH session IDs, raw ADB locators, and either owner field never become part
    of a profile projection.
    """

    profile_id: str
    owner_principal_id: str
    owner_controller_id: str
    display_name: str
    enabled: bool
    is_default: bool
    transport_serial: str
    canonical_device_id: str
    profile_generation: int
    state: EmulatorProfileState
    boot_id: str | None
    fingerprint: EmulatorFingerprint | None
    capabilities: tuple[str, ...]
    last_error_code: str | None
    last_verified_at: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _text(self.profile_id, "profile_id")
        _principal(self.owner_principal_id)
        _controller(self.owner_controller_id)
        _text(self.display_name, "display_name")
        _serial(self.transport_serial)
        expected_canonical = f"emulator:{self.profile_id}"
        if self.canonical_device_id != expected_canonical:
            raise ValueError("canonical_device_id must derive only from profile_id")
        if isinstance(self.profile_generation, bool) or self.profile_generation < 1:
            raise ValueError("profile_generation must be positive")
        if not isinstance(self.enabled, bool) or not isinstance(self.is_default, bool):
            raise ValueError("enabled and is_default must be booleans")
        if self.state is EmulatorProfileState.READY:
            _text(self.boot_id or "", "boot_id")
            if self.fingerprint is None:
                raise ValueError("ready profile requires fingerprint")
        if self.last_error_code is not None:
            _code(self.last_error_code)

    def public_projection(self) -> dict[str, Any]:
        """Return the renderer-safe shape, deliberately omitting ADB details."""

        return {
            "schema_version": 2,
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "kind": "android_emulator",
            "adapter_id": "emulator_adb_v1",
            "profile_generation": self.profile_generation,
            "state": self.state.value,
            "enabled": self.enabled,
            "is_default": self.is_default,
            "fingerprint": self.fingerprint.as_dict() if self.fingerprint else None,
            "capabilities": list(self.capabilities),
            "last_error_code": self.last_error_code,
            "last_verified_at": self.last_verified_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class EmulatorSettingsRun:
    """A downstream command/verification projection, not a Task truth source."""

    task_id: str
    action_id: str
    profile_id: str
    owner_principal_id: str
    owner_controller_id: str
    profile_generation: int
    boot_id: str
    status: SettingsRunStatus
    before_observation_id: str | None = None
    after_observation_id: str | None = None
    command_id: str | None = None
    frame_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    reason_code: str | None = None
    canonical_device_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for value, label in ((self.task_id, "task_id"), (self.action_id, "action_id"), (self.profile_id, "profile_id"), (self.boot_id, "boot_id")):
            _text(value, label)
        _principal(self.owner_principal_id)
        _controller(self.owner_controller_id)
        if self.profile_generation < 1:
            raise ValueError("profile_generation must be positive")
        if self.canonical_device_id is not None:
            _text(self.canonical_device_id, "canonical_device_id")
            if self.canonical_device_id != f"emulator:{self.profile_id}":
                raise ValueError("Settings run canonical_device_id must match profile_id")
        if self.idempotency_key is not None:
            _text(self.idempotency_key, "idempotency_key")
        for value, label in ((self.before_observation_id, "before_observation_id"), (self.after_observation_id, "after_observation_id"), (self.command_id, "command_id"), (self.frame_ref, "frame_ref"), (self.reason_code, "reason_code")):
            if value is not None:
                _text(value, label)
        if self.status is SettingsRunStatus.SUCCEEDED:
            if not self.after_observation_id or not self.evidence_refs:
                raise ValueError("successful Settings run requires after observation and evidence")

    def public_projection(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action_id": self.action_id,
            "profile_id": self.profile_id,
            "profile_generation": self.profile_generation,
            "status": self.status.value,
            "before_observation_id": self.before_observation_id,
            "after_observation_id": self.after_observation_id,
            "frame_ref": self.frame_ref,
            "evidence_refs": list(self.evidence_refs),
            "reason_code": self.reason_code,
        }


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{label} must be non-blank bounded text")


def _code(value: object) -> None:
    _text(value, "code")
    if any(character.isspace() or character in "/\\:" for character in value):
        raise ValueError("code must be a stable, non-path token")


def _serial(value: object) -> None:
    _text(value, "transport_serial")
    if str(value).startswith("-") or any(character.isspace() for character in str(value)):
        raise ValueError("transport_serial is invalid")


def _principal(value: object) -> None:
    _text(value, "owner_principal_id")
    if any(character.isspace() or character in "/\\:" for character in str(value)):
        raise ValueError("owner_principal_id must be an opaque local identifier")


def _controller(value: object) -> None:
    _text(value, "owner_controller_id")
    if any(character.isspace() or character in "/\\:" for character in str(value)):
        raise ValueError("owner_controller_id must be an opaque local identifier")
