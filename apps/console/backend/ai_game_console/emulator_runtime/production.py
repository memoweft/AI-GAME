from __future__ import annotations

import hmac
import hashlib
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adb_executor import AdbGuiExecutor
from ..agent_runtime.service import CanonicalTaskService
from ..discovery import AdbDevice, is_emulator_adb_device
from ..domain import TargetStatus
from ..execution_v2_composition import (
    EmulatorProfilePortAdapter,
    VerifiedFrameBinding,
    VerifiedFramePort,
)
from ..runtime_adapters.adb_executor import GuiExecutorActionAdapter
from ..runtime_adapters.android.observation import (
    AndroidObservationError,
    AndroidObservationProvider,
)
from ..runtime_adapters.artifacts.filesystem import FilesystemArtifactStore
from ..runtime_adapters.sqlite.store import SQLiteRuntimeStore
from ..runtime_kernel.action import ActionStatus, ActionType
from ..runtime_kernel.ports import RecordNotFound
from ..runtime_kernel.stage import StageStatus
from ..runtime_kernel.task import TaskSource
from ..runtime_kernel.verify import VerificationMethod, VerificationVerdict
from ..runtime_kernel.kernel import RuntimeKernel
from .domain import (
    EmulatorFingerprint,
    EmulatorProbe,
    EmulatorProfile,
    EmulatorProfileState,
)
from .general_production import (
    ProductionAndroidUiComposition,
    compose_production_android_ui_runner,
)
from .service import EmulatorProfileService
from .store import EmulatorProfileStore


_SIZE = re.compile(r"(?:Physical size|Override size):\s*(\d+)x(\d+)")
_DENSITY = re.compile(r"(?:Physical density|Override density):\s*(\d+)")
_PACKAGE_VERSION = re.compile(r"^\s*versionName=([^\s]+)\s*$", re.MULTILINE)
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")


class ProductionEmulatorError(RuntimeError):
    """Stable internal failure code; raw ADB output is never propagated."""


ProbeRunner = Callable[
    [Sequence[str], float], subprocess.CompletedProcess[str]
]


class AdbEmulatorProbe:
    """Explicit, read-only identity probe for a selected running emulator."""

    def __init__(
        self,
        *,
        adb_path: Callable[[], Path | None],
        runner: ProbeRunner | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._adb_path = adb_path
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    def __call__(self, serial: str) -> EmulatorProbe:
        adb = self._adb_path()
        if adb is None:
            return EmulatorProbe(
                serial,
                EmulatorProfileState.OFFLINE,
                None,
                None,
                error_code="emulator_adb_unavailable",
            )
        try:
            state = self._read(adb, serial, ("get-state",))
            if state != "device":
                return self._offline(serial, "emulator_offline")
            boot_id = self._read(
                adb,
                serial,
                ("shell", "cat", "/proc/sys/kernel/random/boot_id"),
            )
            emulator_marker = self._read(
                adb, serial, ("shell", "getprop", "ro.kernel.qemu")
            )
            product_properties = {
                "product": self._read(
                    adb, serial, ("shell", "getprop", "ro.product.name")
                )[:128],
                "model": self._read(
                    adb, serial, ("shell", "getprop", "ro.product.model")
                )[:128],
                "device": self._read(
                    adb, serial, ("shell", "getprop", "ro.product.device")
                )[:128],
            }
            api_level = self._positive_int(
                self._read(
                    adb, serial, ("shell", "getprop", "ro.build.version.sdk")
                )
            )
            abi = self._read(
                adb, serial, ("shell", "getprop", "ro.product.cpu.abi")
            )
            size_text = self._read(adb, serial, ("shell", "wm", "size"))
            density_text = self._read(adb, serial, ("shell", "wm", "density"))
            size_match = _SIZE.search(size_text)
            density_match = _DENSITY.search(density_text)
            property_marker_matches = is_emulator_adb_device(
                AdbDevice(
                    # Save-time verification deliberately removes the transport
                    # serial from classification.  emulator-*, loopback and a
                    # device ABI therefore cannot independently pass this gate.
                    serial="",
                    raw_state=state,
                    status=TargetStatus.READY,
                    properties=product_properties,
                )
            )
            if (
                not (
                    emulator_marker == "1"
                    or (emulator_marker == "" and property_marker_matches)
                )
                or not boot_id
                or api_level is None
                or not abi
                or size_match is None
            ):
                return self._offline(serial, "emulator_identity_unavailable")
            # Read version/build metadata only after this transport has passed
            # the emulator-only identity gate.  The digest is kept locally and
            # no raw build string crosses the profile boundary.
            android_build = self._read(
                adb, serial, ("shell", "getprop", "ro.build.fingerprint")
            )
            resolution = f"{int(size_match.group(1))}x{int(size_match.group(2))}"
            fingerprint = EmulatorFingerprint(
                emulator_marker=True,
                api_level=api_level,
                abi=abi[:128],
                resolution=resolution,
                density=(int(density_match.group(1)) if density_match else None),
                android_build_digest=_metadata_digest("android-build", android_build),
            )
            return EmulatorProbe(
                serial,
                EmulatorProfileState.READY,
                boot_id[:512],
                fingerprint,
                (
                    "screen",
                    "ui_tree",
                    "open_app",
                    "tap",
                    "swipe",
                    "back",
                    "home",
                ),
            )
        except subprocess.TimeoutExpired:
            return self._offline(serial, "emulator_probe_timeout")
        except (OSError, subprocess.SubprocessError, ValueError, ProductionEmulatorError):
            return self._offline(serial, "emulator_probe_failed")

    def _read(self, adb: Path, serial: str, suffix: Sequence[str]) -> str:
        command = (str(adb), "-s", serial, *suffix)
        completed = (
            self._runner(command, self._timeout_seconds)
            if self._runner is not None
            else subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
        )
        if completed.returncode != 0:
            raise ProductionEmulatorError("emulator_probe_failed")
        return str(completed.stdout or "").strip()

    @staticmethod
    def _positive_int(value: str) -> int | None:
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _offline(serial: str, code: str) -> EmulatorProbe:
        return EmulatorProbe(
            serial,
            EmulatorProfileState.OFFLINE,
            None,
            None,
            error_code=code,
        )


class ProfileTransportResolver:
    """Resolve only a saved canonical profile into its internal ADB locator."""

    def __init__(self, store: EmulatorProfileStore) -> None:
        self._store = store

    def __call__(self, canonical_device_id: str) -> str:
        prefix = "emulator:"
        if not canonical_device_id.startswith(prefix):
            raise ProductionEmulatorError("emulator_canonical_device_invalid")
        profile = self._store.get_profile_internal(
            profile_id=canonical_device_id.removeprefix(prefix)
        )
        if (
            profile is None
            or not profile.enabled
            or profile.state is not EmulatorProfileState.READY
            or profile.canonical_device_id != canonical_device_id
        ):
            raise ProductionEmulatorError("emulator_profile_unavailable")
        return profile.transport_serial


class ProfileBoundObservationProvider:
    """Create the existing Android provider only when an observation is requested."""

    def __init__(
        self,
        *,
        adb_path: Callable[[], Path | None],
        transport: ProfileTransportResolver,
        provider_factory: Callable[..., AndroidObservationProvider] = AndroidObservationProvider,
    ) -> None:
        self._adb_path = adb_path
        self._transport = transport
        self._provider_factory = provider_factory

    def capture(self, device_id: str):
        adb = self._adb_path()
        if adb is None:
            raise AndroidObservationError("emulator_adb_unavailable")
        return self._provider_factory(
            adb_path=adb,
            transport_device_id_resolver=self._transport,
        ).capture(device_id)


@dataclass(frozen=True, slots=True)
class AndroidPackageRuntimeMetadata:
    """Private, digest-only version facts for one Profile-bound foreground App."""

    app_package: str
    app_version_digest: str
    android_build_digest: str


class ProfileBoundRuntimeMetadataProvider:
    """Read metadata through the exact saved Profile; ordinary failure is cold."""

    def __init__(
        self,
        *,
        adb_path: Callable[[], Path | None],
        transport: ProfileTransportResolver,
        runner: ProbeRunner | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        self._adb_path = adb_path
        self._transport = transport
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    def read(
        self, *, profile: EmulatorProfile, app_package: str,
    ) -> AndroidPackageRuntimeMetadata | None:
        if (
            not _PACKAGE.fullmatch(app_package)
            or not profile.enabled
            or profile.state is not EmulatorProfileState.READY
            or profile.fingerprint is None
            or profile.fingerprint.android_build_digest is None
        ):
            return None
        adb = self._adb_path()
        if adb is None:
            return None
        try:
            serial = self._transport(profile.canonical_device_id)
            if self._read(adb, serial, ("get-state",)) != "device":
                return None
            build_digest = _metadata_digest(
                "android-build",
                self._read(adb, serial, ("shell", "getprop", "ro.build.fingerprint")),
            )
            if build_digest != profile.fingerprint.android_build_digest:
                return None
            package_dump = self._read(
                adb, serial, ("shell", "dumpsys", "package", app_package),
            )
            version = _PACKAGE_VERSION.search(package_dump)
            if version is None or not version.group(1):
                return None
            return AndroidPackageRuntimeMetadata(
                app_package=app_package,
                app_version_digest=_metadata_digest(
                    "android-package-version", app_package, version.group(1),
                ),
                android_build_digest=build_digest,
            )
        except (OSError, subprocess.SubprocessError, ProductionEmulatorError):
            return None

    def _read(self, adb: Path, serial: str, suffix: Sequence[str]) -> str:
        completed = (
            self._runner((str(adb), "-s", serial, *suffix), self._timeout_seconds)
            if self._runner is not None
            else subprocess.run(
                [str(adb), "-s", serial, *suffix], capture_output=True,
                text=True, timeout=self._timeout_seconds, check=False,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )
        )
        if completed.returncode != 0:
            raise ProductionEmulatorError("emulator_metadata_probe_failed")
        return str(completed.stdout or "").strip()


def _metadata_digest(namespace: str, *parts: str) -> str:
    if not parts or any(not isinstance(value, str) or not value.strip() for value in parts):
        raise ProductionEmulatorError("emulator_metadata_unavailable")
    return hashlib.sha256(
        (namespace + "\x00" + "\x00".join(parts)).encode("utf-8"),
    ).hexdigest()


class ProductionSettingsKernelPort:
    """Settings v1 adapter over the existing RuntimeKernel and one shared SQLite spine."""

    def __init__(
        self,
        *,
        kernel: RuntimeKernel,
        agent_runtime_store: Any,
        profile_store: EmulatorProfileStore,
    ) -> None:
        self.kernel = kernel
        self.agent_runtime_store = agent_runtime_store
        self.profile_store = profile_store

    def authorize_profile_binding(
        self,
        *,
        principal_id: str,
        controller_id: str,
        task_id: str,
        profile_id: str,
    ) -> None:
        self._authorize_profile_binding(
            principal_id=principal_id,
            controller_id=controller_id,
            task_id=task_id,
            profile_id=profile_id,
            allow_release_takeover=False,
        )

    def _authorize_profile_binding(
        self,
        *,
        principal_id: str,
        controller_id: str,
        task_id: str,
        profile_id: str,
        allow_release_takeover: bool,
    ) -> None:
        """Require the canonical Task's sole Settings subtask Profile.

        This check deliberately precedes Profile discovery/probe and every
        RuntimeKernel/device side effect.  The caller-provided Profile is never
        accepted merely because it belongs to the same capability owner.
        """

        service = CanonicalTaskService(
            self.agent_runtime_store,
            principal_id=principal_id,
            controller_id=controller_id,
        )
        try:
            task = service.inspect_task(task_id)
        except Exception as error:
            try:
                self.agent_runtime_store.get_task_for_owner(
                    task_id,
                    owner_principal_id=principal_id,
                    controller_id=controller_id,
                )
            except Exception:
                raise ProductionEmulatorError(
                    "canonical_task_owner_mismatch"
                ) from error
            # The owner-scoped Task exists, so a projection failure at this
            # point is conservatively treated as an unusable canonical binding.
            raise ProductionEmulatorError(
                "canonical_profile_binding_malformed"
            ) from error
        if str(task.get("task_id")) != task_id:
            raise ProductionEmulatorError("canonical_task_owner_mismatch")
        subtasks = task.get("subtasks")
        if not isinstance(subtasks, list):
            raise ProductionEmulatorError("canonical_profile_binding_malformed")
        settings = [
            item
            for item in subtasks
            if isinstance(item, dict) and item.get("kind") == "emulator_settings_v1"
        ]
        if not settings:
            raise ProductionEmulatorError("canonical_profile_binding_missing")
        if len(settings) != 1:
            raise ProductionEmulatorError("canonical_profile_binding_duplicate")
        bound_profile_id = settings[0].get("object_ref")
        if (
            not isinstance(bound_profile_id, str)
            or not bound_profile_id.strip()
            or len(bound_profile_id) > 512
        ):
            raise ProductionEmulatorError("canonical_profile_binding_malformed")
        if not hmac.compare_digest(bound_profile_id, profile_id):
            raise ProductionEmulatorError("canonical_profile_binding_mismatch")
        status = str(task.get("status") or "")
        release_takeover = (
            allow_release_takeover
            and status == "replanning"
            and settings[0].get("status") == "user_takeover"
        )
        if status != "running" and not release_takeover:
            raise ProductionEmulatorError("canonical_task_not_runnable")

    def capture_fresh(
        self, *, task_id: str, profile: EmulatorProfile
    ) -> FreshObservation:
        self._authorize_task(task_id, profile, allow_release_takeover=True)
        self._ensure_kernel_task(task_id, profile)
        observation = self.kernel.capture_observation(
            task_id=task_id, device_id=profile.canonical_device_id
        )
        refs = self._artifact_references(observation)
        self._verify_after_if_needed(task_id, observation, refs)
        return FreshObservation(
            observation_id=observation.id,
            canonical_device_id=observation.device_id,
            profile_generation=profile.profile_generation,
            boot_id=profile.boot_id or "",
            foreground_package=observation.device_state.foreground_app,
            frame_ref=observation.screenshot.artifact.reference,
            evidence_refs=refs,
        )

    def dispatch_or_reconcile_open_app(
        self,
        *,
        task_id: str,
        action_id: str,
        profile: EmulatorProfile,
        before_observation_id: str,
        package: str,
    ) -> CommandReconciliation:
        if package != SETTINGS_PACKAGE:
            return CommandReconciliation(
                action_id, True, False, "settings_package_policy_rejected"
            )
        self._authorize_task(task_id, profile, allow_release_takeover=False)
        self._ensure_kernel_task(task_id, profile)
        try:
            action = self.kernel.load_action(task_id, action_id)
        except RecordNotFound:
            stage = self.kernel.current_stage(task_id)
            if stage is None:
                raise ProductionEmulatorError("settings_stage_unavailable")
            action = self.kernel.propose_action(
                task_id=task_id,
                stage_id=stage.id,
                based_on_observation_id=before_observation_id,
                action_type=ActionType.OPEN_APP,
                params={"package": SETTINGS_PACKAGE},
                expected_outcome="com.android.settings is the fresh foreground package",
                proposed_by_call_id="emulator-settings-v1",
                action_id=action_id,
            )
        self._validate_action(
            action,
            task_id=task_id,
            before_observation_id=before_observation_id,
        )
        run = self.profile_store.get_settings_run(
            owner_principal_id=profile.owner_principal_id,
            owner_controller_id=profile.owner_controller_id,
            task_id=task_id,
            action_id=action_id,
        )
        if run is None or run.idempotency_key is None:
            raise ProductionEmulatorError("settings_run_binding_missing")
        claim, created = self.profile_store.claim_settings_command(
            owner_principal_id=profile.owner_principal_id,
            owner_controller_id=profile.owner_controller_id,
            task_id=task_id,
            action_id=action_id,
            idempotency_key=run.idempotency_key,
        )
        self._validate_claim(claim, run)
        if not created:
            return self.reconcile_command(
                task_id=task_id, action_id=action_id, command_id=action_id
            )
        try:
            execution = self.kernel.execute_action(
                task_id=task_id, action_id=action_id
            )
        except Exception:
            # The command claim crossed the durable no-replay fence.  An
            # unknown outcome can only be reconciled, never dispatched again.
            return CommandReconciliation(
                action_id, False, False, "command_outcome_unknown"
            )
        reason = execution.error.code if execution.error is not None else None
        self.profile_store.settle_settings_command(
            task_id=task_id,
            action_id=action_id,
            accepted=execution.accepted,
            reason_code=reason,
        )
        return CommandReconciliation(
            action_id, True, execution.accepted, reason
        )

    def reconcile_command(
        self, *, task_id: str, action_id: str, command_id: str
    ) -> CommandReconciliation:
        if command_id != action_id:
            return CommandReconciliation(
                command_id, True, False, "command_identity_mismatch"
            )
        claim = self.profile_store.settings_command_claim(
            task_id=task_id, action_id=action_id
        )
        if claim is None or claim.get("command_id") != command_id:
            return CommandReconciliation(
                command_id, True, False, "command_binding_missing"
            )
        if claim["status"] == "settled":
            accepted = bool(claim["accepted"])
            if accepted:
                try:
                    verification = self.kernel.load_verification(action_id)
                except RecordNotFound:
                    verification = None
                if (
                    verification is not None
                    and verification.verdict is not VerificationVerdict.SUCCESS
                ):
                    return CommandReconciliation(
                        command_id,
                        True,
                        False,
                        "settings_foreground_unverified",
                    )
            return CommandReconciliation(
                command_id, True, accepted, claim.get("reason_code")
            )
        try:
            execution = self.kernel.load_action_execution(action_id)
        except RecordNotFound:
            return CommandReconciliation(
                command_id, False, False, "command_outcome_unknown"
            )
        reason = execution.error.code if execution.error is not None else None
        self.profile_store.settle_settings_command(
            task_id=task_id,
            action_id=action_id,
            accepted=execution.accepted,
            reason_code=reason,
        )
        return CommandReconciliation(
            command_id, True, execution.accepted, reason
        )

    def release_write_lease(self, *, task_id: str, action_id: str) -> None:
        self.kernel.release_exact_action_lease(
            task_id=task_id, action_id=action_id
        )

    def create_checkpoint(
        self, *, task_id: str, reason: str, observation_id: str
    ) -> None:
        latest = self.kernel.latest_observation(task_id)
        if latest is None or latest.id != observation_id:
            raise ProductionEmulatorError("checkpoint_observation_stale")
        self.kernel.create_checkpoint(task_id=task_id, reason=reason)

    def _authorize_task(
        self,
        task_id: str,
        profile: EmulatorProfile,
        *,
        allow_release_takeover: bool,
    ) -> None:
        self._authorize_profile_binding(
            principal_id=profile.owner_principal_id,
            controller_id=profile.owner_controller_id,
            task_id=task_id,
            profile_id=profile.profile_id,
            allow_release_takeover=allow_release_takeover,
        )

    def _ensure_kernel_task(self, task_id: str, profile: EmulatorProfile) -> None:
        try:
            task = self.kernel.load_task(task_id)
        except RecordNotFound:
            task = self.kernel.create_task(
                task_id=task_id,
                goal="Open Android Settings once and verify the fresh foreground.",
                source=TaskSource(
                    client_id="weftmate-execution-v2",
                    conversation_id=f"task:{task_id}",
                    initial_message_id=f"profile:{profile.profile_id}",
                ),
                device_id=profile.canonical_device_id,
            )
        if (
            task.id != task_id
            or task.device_id != profile.canonical_device_id
            or task.source.client_id != "weftmate-execution-v2"
            or task.source.conversation_id != f"task:{task_id}"
            or task.source.initial_message_id != f"profile:{profile.profile_id}"
        ):
            raise ProductionEmulatorError("kernel_task_binding_mismatch")
        stages = self.kernel.list_stages(task_id)
        if not stages:
            self.kernel.create_stage(
                task_id=task_id,
                stage_id=self._stage_id(task_id),
                objective="Open Android system Settings once",
                completion_criteria=(
                    "one OPEN_APP com.android.settings action is accepted",
                    "a fresh after observation verifies Settings foreground",
                ),
                planner_call_id="emulator-settings-v1",
            )
            stages = self.kernel.list_stages(task_id)
        if len(stages) != 1 or stages[0].id != self._stage_id(task_id):
            raise ProductionEmulatorError("kernel_stage_binding_mismatch")
        if stages[0].status is StageStatus.PENDING:
            self.kernel.start_stage(task_id=task_id, stage_id=stages[0].id)

    def _verify_after_if_needed(
        self, task_id: str, observation: Any, refs: tuple[str, ...]
    ) -> None:
        actions = self.kernel.list_actions(task_id)
        if not actions:
            return
        if len(actions) != 1:
            raise ProductionEmulatorError("settings_action_count_conflict")
        action = actions[0]
        if action.status is not ActionStatus.EXECUTED:
            return
        try:
            self.kernel.load_verification(action.id)
            return
        except RecordNotFound:
            pass
        verdict = (
            VerificationVerdict.SUCCESS
            if observation.device_state.foreground_app == SETTINGS_PACKAGE
            else VerificationVerdict.FAIL
        )
        self.kernel.verify_action(
            task_id=task_id,
            action_id=action.id,
            before_observation_id=action.based_on_observation_id,
            after_observation_id=observation.id,
            verdict=verdict,
            reason=(
                "settings_foreground_verified"
                if verdict is VerificationVerdict.SUCCESS
                else "settings_foreground_unverified"
            ),
            evidence_refs=refs,
            method=VerificationMethod.RUNTIME_RULE,
            verification_call_id="emulator-settings-v1",
            complete_stage=verdict is VerificationVerdict.SUCCESS,
            progress_summary=(
                "Android Settings foreground verified from a fresh observation."
                if verdict is VerificationVerdict.SUCCESS
                else None
            ),
        )
        # The canonical AgentRuntime Task remains the only top-level lifecycle
        # authority.  RuntimeKernel records the verified action/stage and
        # checkpoint, but this adapter never terminalizes a second Task truth.

    @staticmethod
    def _validate_action(
        action: Any, *, task_id: str, before_observation_id: str
    ) -> None:
        if (
            action.task_id != task_id
            or action.id == ""
            or action.type is not ActionType.OPEN_APP
            or action.params.get("package") != SETTINGS_PACKAGE
            or action.based_on_observation_id != before_observation_id
        ):
            raise ProductionEmulatorError("settings_action_binding_mismatch")

    @staticmethod
    def _validate_claim(claim: dict[str, Any], run: Any) -> None:
        if (
            claim.get("owner_principal_id") != run.owner_principal_id
            or claim.get("owner_controller_id") != run.owner_controller_id
            or claim.get("task_id") != run.task_id
            or claim.get("action_id") != run.action_id
            or claim.get("idempotency_key") != run.idempotency_key
        ):
            raise ProductionEmulatorError("settings_command_binding_mismatch")

    @staticmethod
    def _artifact_references(observation: Any) -> tuple[str, ...]:
        refs = [str(observation.screenshot.artifact.reference)]
        if observation.ui_tree.artifact is not None:
            refs.append(str(observation.ui_tree.artifact.reference))
        return tuple(refs)

    @staticmethod
    def _stage_id(task_id: str) -> str:
        return f"emulator-settings-stage:{task_id}"


class ProductionFrameBindings:
    """Resolve only the latest succeeded Settings observation for its exact owner."""

    def __init__(
        self,
        *,
        store: EmulatorProfileStore,
        profiles: EmulatorProfileService,
        kernel: RuntimeKernel,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.kernel = kernel

    def resolve(
        self, principal_id: str, controller_id: str, task_id: str
    ) -> VerifiedFrameBinding | None:
        run = self.store.latest_succeeded_settings_run(
            owner_principal_id=principal_id,
            owner_controller_id=controller_id,
            task_id=task_id,
        )
        if (
            run is None
            or run.after_observation_id is None
            or run.frame_ref is None
            or run.canonical_device_id is None
        ):
            return None
        try:
            profile = self.profiles.require_profile(
                principal_id=principal_id,
                controller_id=controller_id,
                profile_id=run.profile_id,
            )
            observation = self.kernel.load_observation(run.after_observation_id)
            latest = self.kernel.latest_observation(task_id)
        except Exception:
            return None
        if (
            profile.profile_generation != run.profile_generation
            or profile.boot_id != run.boot_id
            or profile.canonical_device_id != run.canonical_device_id
            or observation.task_id != task_id
            or observation.device_id != run.canonical_device_id
            or latest is None
            or latest.id != observation.id
            or observation.screenshot.artifact.reference != run.frame_ref
        ):
            return None
        return VerifiedFrameBinding(
            task_id=task_id,
            profile_id=run.profile_id,
            observation_id=observation.id,
            frame_reference=run.frame_ref,
        )

    def load_observation(self, observation_id: str):
        run = self.store.succeeded_settings_run_for_observation(
            observation_id=observation_id
        )
        if run is None:
            raise ProductionEmulatorError("frame_observation_binding_missing")
        observation = self.kernel.load_observation(observation_id)
        latest = self.kernel.latest_observation(run.task_id)
        if (
            observation.task_id != run.task_id
            or observation.device_id != run.canonical_device_id
            or latest is None
            or latest.id != observation_id
        ):
            raise ProductionEmulatorError("frame_observation_binding_mismatch")
        return observation


@dataclass(slots=True)
class ProductionEmulatorComposition:
    store: EmulatorProfileStore
    profiles: EmulatorProfileService
    profile_port: EmulatorProfilePortAdapter
    settings_kernel: None
    settings_operator: None
    frame_bindings: ProductionFrameBindings
    frame_port: VerifiedFramePort
    kernel: RuntimeKernel
    artifacts: FilesystemArtifactStore
    runtime_metadata: ProfileBoundRuntimeMetadataProvider
    general_ui: ProductionAndroidUiComposition | None

    def close(self) -> None:
        self.kernel.close()
        self.store.close()


def compose_production_emulator_runtime(
    *,
    data_dir: Path,
    adb_discovery: Any,
    agent_runtime_store: Any,
    clock: Callable[[], str],
    gui_executor_enabled: bool,
    role_model: Any | None = None,
    mobile_evidence: Any | None = None,
    experience_service: Any | None = None,
    managed_path_guard: Any | None = None,
    managed_path_prefix: str = "emulator-runtime",
) -> ProductionEmulatorComposition:
    """Construct production ports without discovering or touching a device."""

    resolve_adb_path = getattr(adb_discovery, "resolve_adb_path", None)
    if not callable(resolve_adb_path):
        resolve_adb_path = lambda: None
    # ``data_dir`` remains the developer-compatible composition root.  A
    # managed caller must additionally provide the startup-owned guard so that
    # every nested database/directory is checked immediately before use.
    def managed_directory(relative: str) -> Path:
        if managed_path_guard is None:
            return data_dir / relative
        return managed_path_guard.directory(f"{managed_path_prefix}/{relative}")

    def managed_file(relative: str) -> Path:
        if managed_path_guard is None:
            return data_dir / relative
        return managed_path_guard.file(f"{managed_path_prefix}/{relative}")

    store = EmulatorProfileStore(managed_file("emulator-profiles.db"))
    profiles = EmulatorProfileService(
        store=store,
        probe=AdbEmulatorProbe(adb_path=resolve_adb_path),
        clock=clock,
    )
    discover_emulators = getattr(adb_discovery, "discover_emulators", None)
    profile_port = EmulatorProfilePortAdapter(
        profiles=profiles,
        store=store,
        discover=(discover_emulators if callable(discover_emulators) else adb_discovery.discover),
        clock=clock,
    )
    runtime_dir = managed_directory("runtime")
    artifacts = FilesystemArtifactStore(managed_directory("runtime/artifacts"))
    transport = ProfileTransportResolver(store)
    observation = ProfileBoundObservationProvider(
        adb_path=resolve_adb_path,
        transport=transport,
    )
    runtime_metadata = ProfileBoundRuntimeMetadataProvider(
        adb_path=resolve_adb_path,
        transport=transport,
    )

    def executor_for_serial(serial: str) -> AdbGuiExecutor:
        return AdbGuiExecutor(
            enabled=gui_executor_enabled,
            adb_path=resolve_adb_path(),
            serial=serial,
        )

    kernel = RuntimeKernel(
        SQLiteRuntimeStore(managed_file("runtime/runtime.db")),
        observation_provider=observation,
        artifact_store=artifacts,
        action_executor=GuiExecutorActionAdapter(
            executor_for_serial,
            transport_device_id_resolver=transport,
        ),
    )
    general_ui = (
        compose_production_android_ui_runner(
            data_dir=data_dir,
            kernel=kernel,
            artifacts=artifacts,
            runtime_store=agent_runtime_store,
            profiles=profiles,
            role_model=role_model,
            evidence=mobile_evidence,
            experience_service=experience_service,
            runtime_metadata=runtime_metadata,
            managed_file=managed_file if managed_path_guard is not None else None,
        )
        if role_model is not None and mobile_evidence is not None
        else None
    )
    frame_bindings = ProductionFrameBindings(
        store=store, profiles=profiles, kernel=kernel
    )
    frame_port = VerifiedFramePort(
        resolve_binding=frame_bindings.resolve,
        load_observation=frame_bindings.load_observation,
        profiles=profiles,
        artifacts=artifacts,
    )
    return ProductionEmulatorComposition(
        store=store,
        profiles=profiles,
        profile_port=profile_port,
        settings_kernel=None,
        settings_operator=None,
        frame_bindings=frame_bindings,
        frame_port=frame_port,
        kernel=kernel,
        artifacts=artifacts,
        runtime_metadata=runtime_metadata,
        general_ui=general_ui,
    )


__all__ = [
    "AdbEmulatorProbe",
    "ProductionEmulatorComposition",
    "ProductionFrameBindings",
    "ProductionSettingsKernelPort",
    "ProfileBoundObservationProvider",
    "ProfileTransportResolver",
    "compose_production_emulator_runtime",
]
