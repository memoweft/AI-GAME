from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from uuid import uuid4

from ..discovery import AdbDevice, is_emulator_adb_device
from .domain import EmulatorProbe, EmulatorProfile, EmulatorProfileState
from .store import EmulatorProfileStore, EmulatorProfileStoreOwnershipError


class EmulatorProfileServiceError(ValueError):
    """A safe, stable service error suitable for a redacted projection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


ProbeProvider = Callable[[str], EmulatorProbe]
Clock = Callable[[], str]


# These are deliberately finite, renderer-safe observations from the production
# probe.  They are availability facts, not proof that a saved profile belongs
# to another task or device.  Keep this list local to the profile boundary so
# callers never need to inspect raw subprocess failures.
SAFE_PROBE_ERROR_CODES = frozenset(
    {
        "emulator_adb_unavailable",
        "emulator_offline",
        "emulator_probe_failed",
        "emulator_probe_timeout",
        "emulator_identity_unavailable",
    }
)

# These codes represent a comparison against durable task/profile identity
# facts.  The resident runner may fence only these exact values; it must not
# infer an identity failure from a substring in a generic availability code.
IDENTITY_PROFILE_ERROR_CODES = frozenset(
    {
        "emulator_profile_not_found",
        "emulator_profile_generation_mismatch",
        "emulator_identity_drift",
        "emulator_profile_owner_mismatch",
        "emulator_profile_binding_mismatch",
        "emulator_canonical_identity_mismatch",
    }
)


class EmulatorProfileService:
    """Authority for emulator profile selection and transport revalidation."""

    def __init__(
        self,
        *,
        store: EmulatorProfileStore,
        probe: ProbeProvider,
        clock: Clock,
    ) -> None:
        self._store = store
        self._probe = probe
        self._clock = clock

    def discover_candidates(
        self,
        *,
        principal_id: str,
        controller_id: str,
        devices: Iterable[AdbDevice],
    ) -> tuple[AdbDevice, ...]:
        """Filter out physical and unclassifiable ADB targets before selection."""

        _owner_pair(principal_id, controller_id)
        return tuple(
            device
            for device in devices
            if device.status.value == "ready" and is_emulator_adb_device(device)
        )

    def validate_owner(self, *, principal_id: str, controller_id: str) -> None:
        """Validate the frozen capability owner pair without probing a target."""

        _owner_pair(principal_id, controller_id)

    def save_selected(
        self,
        *,
        principal_id: str,
        controller_id: str,
        candidate: AdbDevice,
        display_name: str,
        is_default: bool = False,
        profile_id: str | None = None,
    ) -> EmulatorProfile:
        _owner_pair(principal_id, controller_id)
        if profile_id is not None and self._store.profile_id_claimed(profile_id=profile_id):
            # A caller is never allowed to turn an ID collision into a profile
            # existence oracle or an ADB probe against another principal's row.
            raise EmulatorProfileServiceError("emulator_profile_not_found")
        if not is_emulator_adb_device(candidate):
            raise EmulatorProfileServiceError("emulator_target_required")
        probe = self._probe(candidate.serial)
        if probe.state is not EmulatorProfileState.READY:
            raise EmulatorProfileServiceError(_safe_probe_error(probe))
        now = self._clock()
        resolved_id = profile_id or f"profile_{uuid4().hex}"
        profile = EmulatorProfile(
            profile_id=resolved_id,
            owner_principal_id=principal_id,
            owner_controller_id=controller_id,
            display_name=display_name,
            enabled=True,
            is_default=is_default,
            transport_serial=probe.transport_serial,
            canonical_device_id=f"emulator:{resolved_id}",
            profile_generation=1,
            state=EmulatorProfileState.READY,
            boot_id=probe.boot_id,
            fingerprint=probe.fingerprint,
            capabilities=probe.capabilities,
            last_error_code=None,
            last_verified_at=now,
            created_at=now,
            updated_at=now,
        )
        return self._save_profile(profile)

    def set_default(
        self, *, principal_id: str, controller_id: str, profile_id: str
    ) -> EmulatorProfile:
        profile = self.require_profile(
            principal_id=principal_id,
            controller_id=controller_id,
            profile_id=profile_id,
        )
        if not profile.enabled:
            raise EmulatorProfileServiceError("emulator_profile_disabled")
        return self._save_profile(
            replace(profile, is_default=True, updated_at=self._clock())
        )

    def disable(
        self, *, principal_id: str, controller_id: str, profile_id: str
    ) -> EmulatorProfile:
        profile = self.require_profile(
            principal_id=principal_id,
            controller_id=controller_id,
            profile_id=profile_id,
        )
        return self._save_profile(
            replace(
                profile,
                enabled=False,
                is_default=False,
                state=EmulatorProfileState.DISABLED,
                updated_at=self._clock(),
            )
        )

    def verify(
        self, *, principal_id: str, controller_id: str, profile_id: str
    ) -> EmulatorProfile:
        """Revalidate an explicit saved profile without silently switching device."""

        profile = self.require_profile(
            principal_id=principal_id,
            controller_id=controller_id,
            profile_id=profile_id,
        )
        if not profile.enabled:
            raise EmulatorProfileServiceError("emulator_profile_disabled")
        probe = self._probe(profile.transport_serial)
        now = self._clock()
        if probe.state is not EmulatorProfileState.READY:
            return self._save_profile(
                replace(
                    profile,
                    state=EmulatorProfileState.OFFLINE,
                    last_error_code=_safe_probe_error(probe),
                    updated_at=now,
                )
            )
        if probe.boot_id != profile.boot_id or probe.fingerprint != profile.fingerprint:
            return self._save_profile(
                replace(
                    profile,
                    state=EmulatorProfileState.DRIFTED,
                    last_error_code="emulator_identity_drift",
                    updated_at=now,
                )
            )
        changed_transport = probe.transport_serial != profile.transport_serial
        return self._save_profile(
            replace(
                profile,
                transport_serial=probe.transport_serial,
                profile_generation=(profile.profile_generation + 1 if changed_transport else profile.profile_generation),
                state=EmulatorProfileState.READY,
                capabilities=probe.capabilities,
                last_error_code=None,
                last_verified_at=now,
                updated_at=now,
            )
        )

    def resolve_ready(
        self,
        *,
        principal_id: str,
        controller_id: str,
        profile_id: str,
        expected_generation: int | None = None,
    ) -> EmulatorProfile:
        profile = self.verify(
            principal_id=principal_id,
            controller_id=controller_id,
            profile_id=profile_id,
        )
        if profile.state is EmulatorProfileState.OFFLINE:
            # ``offline`` is the durable state written by ``verify``.  Preserve
            # its finite underlying probe code for the runner so a transient
            # timeout cannot be mistaken for an identity crosswire.
            raise EmulatorProfileServiceError(
                _safe_probe_error_code(profile.last_error_code)
            )
        if profile.state is EmulatorProfileState.DRIFTED:
            raise EmulatorProfileServiceError("emulator_identity_drift")
        if profile.state is not EmulatorProfileState.READY:
            raise EmulatorProfileServiceError("emulator_profile_unavailable")
        if expected_generation is not None and profile.profile_generation != expected_generation:
            raise EmulatorProfileServiceError("emulator_profile_generation_mismatch")
        return profile

    def require_profile(
        self, *, principal_id: str, controller_id: str, profile_id: str
    ) -> EmulatorProfile:
        _owner_pair(principal_id, controller_id)
        profile = self._store.get_profile(
            owner_principal_id=principal_id,
            owner_controller_id=controller_id,
            profile_id=profile_id,
        )
        if profile is None:
            raise EmulatorProfileServiceError("emulator_profile_not_found")
        return profile

    def default_profile(
        self, *, principal_id: str, controller_id: str
    ) -> EmulatorProfile:
        _owner_pair(principal_id, controller_id)
        profile = self._store.get_default_profile(
            owner_principal_id=principal_id, owner_controller_id=controller_id
        )
        if profile is None:
            raise EmulatorProfileServiceError("emulator_profile_not_found")
        return profile

    def projections(
        self, *, principal_id: str, controller_id: str
    ) -> tuple[dict[str, object], ...]:
        _owner_pair(principal_id, controller_id)
        return tuple(
            profile.public_projection()
            for profile in self._store.list_profiles(
                owner_principal_id=principal_id,
                owner_controller_id=controller_id,
            )
        )

    def _save_profile(self, profile: EmulatorProfile) -> EmulatorProfile:
        try:
            return self._store.save_profile(profile)
        except EmulatorProfileStoreOwnershipError as error:
            raise EmulatorProfileServiceError(error.args[0]) from error


def _safe_probe_error(probe: EmulatorProbe) -> str:
    return _safe_probe_error_code(probe.error_code)


def _safe_probe_error_code(value: str | None) -> str:
    return value if value in SAFE_PROBE_ERROR_CODES else "emulator_probe_failed"


def _owner_pair(principal_id: str, controller_id: str) -> None:
    _owner_field(principal_id, "principal")
    _owner_field(controller_id, "controller")


def _owner_field(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise EmulatorProfileServiceError(f"capability_{label}_invalid")
    if any(character.isspace() or character in "/\\:" for character in value):
        raise EmulatorProfileServiceError(f"capability_{label}_invalid")
