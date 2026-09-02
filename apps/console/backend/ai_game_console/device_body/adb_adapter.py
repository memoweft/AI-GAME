"""R4 clean-room ADB DeviceBody adapter.

The adapter intentionally owns only transport facts.  It never retries an
uncertain command, does not decide a Goal outcome, and keeps Unicode plaintext
out of command, receipt, event, and exception representations.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..adb_executor import AdbGuiExecutor
from ..runtime_adapters.android.observation import parse_foreground_component
from .domain import (
    BodyActionCommand,
    BodyActionType,
    BodyCommandStatus,
    BodyExecutionReceipt,
    CapabilityKind,
    CapabilityState,
    DeviceBodyBinding,
    DeviceBodyCapability,
    DeviceSnapshot,
    ExecutionReportState,
    HumanPresenceState,
    NetworkState,
    Orientation,
    ReceiptAcknowledgement,
    TransportDisposition,
)


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
Clock = Callable[[], str]


class AdbDeviceBodyError(RuntimeError):
    """A bounded ADB read could not produce a reliable DeviceBody fact."""


@dataclass(frozen=True, slots=True)
class ResolvedAndroidPackage:
    """Read-only launcher resolution for exactly one requested package."""

    package: str
    component: str | None


class AdbPackageCatalog:
    """Read-only package resolver; it never enumerates or launches packages."""

    def __init__(
        self,
        *,
        adb_path: str | Path,
        runner: CommandRunner,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._adb_path = Path(adb_path)
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    def resolve(self, device_id: str, package: str) -> ResolvedAndroidPackage | None:
        serial = _serial_from_device_id(device_id)
        package = _validated_package(package)
        result = self._run(
            (
                self._adb(),
                "-s",
                serial,
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                package,
            )
        )
        if result.returncode != 0:
            return None
        component = _parse_resolved_component(result.stdout or "", package)
        return ResolvedAndroidPackage(package=package, component=component)

    def _adb(self) -> str:
        return str(self._adb_path.resolve())

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(command, self._timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise AdbDeviceBodyError("adb_read_timeout") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdbDeviceBodyError("adb_read_unavailable") from exc


class AdbDeviceBodyAdapter:
    """ADB transport adapter for one explicit DeviceBody binding.

    ``runner`` is injected so protocol tests exercise exact argument arrays
    without sending any host or device commands.  In production it defaults to
    a direct ``shell=False`` subprocess call.
    """

    adapter_id = "adb-r4-compat"

    def __init__(
        self,
        *,
        adb_path: str | Path,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 5.0,
        clock: Clock | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._adb_path = Path(adb_path)
        self._runner = runner or _subprocess_runner
        self._timeout_seconds = timeout_seconds
        self._clock = clock or _utc_now
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._sequences: dict[str, int] = {}
        self.package_catalog = AdbPackageCatalog(
            adb_path=self._adb_path,
            runner=self._runner,
            timeout_seconds=timeout_seconds,
        )

    def read_boot_id(self, device_id: str) -> str:
        """Read the Android boot correlation id without changing device state."""

        serial = _serial_from_device_id(device_id)
        self._require_ready(serial)
        result = self._run(
            (self._adb(), "-s", serial, "shell", "cat", "/proc/sys/kernel/random/boot_id")
        )
        boot_id = (result.stdout or "").strip().lower()
        if result.returncode != 0 or not boot_id or len(boot_id) > 256:
            raise AdbDeviceBodyError("adb_boot_id_unavailable")
        return boot_id

    def discover_capabilities(
        self, binding: DeviceBodyBinding
    ) -> tuple[DeviceBodyCapability, ...]:
        """Return one complete revision, separating transport from read-back."""

        serial = _serial_from_device_id(binding.device_id)
        connected = self._is_ready(serial)
        unicode_state, unicode_reason = self._unicode_transport_capability(binding)
        revision = binding.capability_revision + 1
        capabilities: list[DeviceBodyCapability] = []
        for kind in CapabilityKind:
            state, reason = self._capability_state(
                kind,
                connected=connected,
                unicode_state=unicode_state,
                unicode_reason=unicode_reason,
            )
            capabilities.append(
                DeviceBodyCapability(
                    id=f"{binding.id}:capability:{revision}:{kind.value}",
                    binding_id=binding.id,
                    device_id=binding.device_id,
                    device_boot_id=binding.device_boot_id,
                    revision=revision,
                    kind=kind,
                    state=state,
                    reason_code=reason,
                    evidence_ref="adb:capability-probe" if state is CapabilityState.READY else None,
                    observed_at=self._clock(),
                )
            )
        return tuple(capabilities)

    def execute(self, command: BodyActionCommand) -> BodyExecutionReceipt:
        """Dispatch once and return only a transport/execution report.

        Timeouts remain ``UNKNOWN`` effects and receive no internal replay.
        Unicode input is rejected before resolving its plaintext because R4's
        ADB compatibility path has no independently verified read-back channel.
        """

        started_at = self._clock()
        if command.status is not BodyCommandStatus.DISPATCHING:
            return self._rejected_receipt(
                command,
                started_at=started_at,
                reason_code="body_command_not_claimed",
            )
        if command.action_type is BodyActionType.INPUT_TEXT_UNICODE:
            return self._rejected_receipt(
                command,
                started_at=started_at,
                reason_code="adb_text_read_back_unsupported",
            )
        try:
            adb_command = self._command_for(command)
        except ValueError:
            return self._rejected_receipt(
                command,
                started_at=started_at,
                reason_code="adb_command_invalid",
            )
        try:
            result = self._run(adb_command)
        except subprocess.TimeoutExpired:
            return self._uncertain_receipt(command, started_at, "adb_transport_timeout")
        except (OSError, subprocess.SubprocessError):
            return self._uncertain_receipt(command, started_at, "adb_transport_unavailable")
        finished_at = self._clock()
        if result.returncode != 0:
            return self._rejected_receipt(
                command,
                started_at=started_at,
                finished_at=finished_at,
                reason_code="adb_transport_rejected",
                adapter_code=str(result.returncode),
            )
        return BodyExecutionReceipt(
            id=self._id_factory(),
            source_receipt_id=f"adb:{command.id}:{self._id_factory()}",
            command_id=command.id,
            kernel_action_id=command.kernel_action_id,
            binding_id=command.binding_id,
            device_id=command.device_id,
            device_boot_id=command.device_boot_id,
            adapter_id=self.adapter_id,
            acknowledgement=ReceiptAcknowledgement.ACKNOWLEDGED,
            transport=TransportDisposition.ACCEPTED,
            execution=ExecutionReportState.REPORTED,
            started_at=started_at,
            finished_at=finished_at,
            received_at=self._clock(),
            retryable=False,
            adapter_code=str(result.returncode),
            evidence_ref="adb:transport-receipt",
        )

    def capture_snapshot(
        self,
        binding: DeviceBodyBinding,
        *,
        capture_request_id: str,
        caused_by_command_id: str | None = None,
    ) -> DeviceSnapshot:
        """Capture a new foreground fact after the caller's action boundary."""

        requested_at = self._clock()
        serial = _serial_from_device_id(binding.device_id)
        self._require_ready(serial)
        actual_boot_id = self.read_boot_id(binding.device_id)
        if actual_boot_id != binding.device_boot_id:
            raise AdbDeviceBodyError("device_boot_id_changed")
        started_at = self._clock()
        foreground = self._read_foreground(serial)
        completed_at = self._clock()
        sequence = self._sequences.get(binding.id, 0) + 1
        self._sequences[binding.id] = sequence
        return DeviceSnapshot(
            id=self._id_factory(),
            binding_id=binding.id,
            device_id=binding.device_id,
            device_boot_id=binding.device_boot_id,
            capture_request_id=capture_request_id,
            sequence=sequence,
            foreground_package=foreground.package if foreground is not None else None,
            foreground_activity=foreground.activity if foreground is not None else None,
            screen_on=False,
            locked=False,
            network_state=NetworkState.UNKNOWN,
            orientation=Orientation.UNKNOWN,
            human_presence=HumanPresenceState.UNKNOWN,
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_completed_at=completed_at,
            received_at=self._clock(),
            observed_at=completed_at,
            caused_by_command_id=caused_by_command_id,
        )

    def _capability_state(
        self,
        kind: CapabilityKind,
        *,
        connected: bool,
        unicode_state: CapabilityState,
        unicode_reason: str,
    ) -> tuple[CapabilityState, str | None]:
        companion_only = {
            CapabilityKind.NOTIFICATION_LISTENER,
            CapabilityKind.GESTURE_DISPATCH,
            CapabilityKind.UNICODE_SET_TEXT,
            CapabilityKind.UNICODE_IME,
            CapabilityKind.HUMAN_PRESENCE,
            CapabilityKind.SCREEN_STATE,
            CapabilityKind.LOCK_STATE,
            CapabilityKind.NETWORK_STATE,
            CapabilityKind.ORIENTATION_STATE,
            CapabilityKind.EVENT_JOURNAL,
            CapabilityKind.COMMAND_JOURNAL,
            CapabilityKind.HEARTBEAT,
            CapabilityKind.COMPANION_BRIDGE,
        }
        if kind in companion_only:
            return CapabilityState.UNSUPPORTED, "adb_companion_capability_unsupported"
        if not connected:
            return CapabilityState.TEMPORARILY_UNAVAILABLE, "adb_target_not_ready"
        if kind is CapabilityKind.INPUT_TEXT_UNICODE:
            return unicode_state, unicode_reason if unicode_state is not CapabilityState.READY else None
        if kind is CapabilityKind.TEXT_READ_BACK:
            return CapabilityState.UNSUPPORTED, "adb_text_read_back_unsupported"
        if kind in {CapabilityKind.SCREEN_CAPTURE, CapabilityKind.ACCESSIBILITY_TREE}:
            return CapabilityState.UNKNOWN, "adb_channel_not_probed"
        return CapabilityState.READY, None

    def _unicode_transport_capability(
        self, binding: DeviceBodyBinding
    ) -> tuple[CapabilityState, str]:
        executor = AdbGuiExecutor(
            enabled=True,
            adb_path=self._adb_path,
            serial=_serial_from_device_id(binding.device_id),
            runner=self._runner,
            timeout_seconds=self._timeout_seconds,
        )
        result = executor.probe_unicode_text_transport()
        if result == "ready":
            return CapabilityState.READY, ""
        if result == "temporarily_unavailable":
            return CapabilityState.TEMPORARILY_UNAVAILABLE, "mumu_unicode_transport_unavailable"
        return CapabilityState.UNSUPPORTED, "adb_unicode_transport_unsupported"

    def _command_for(self, command: BodyActionCommand) -> tuple[str, ...]:
        serial = _serial_from_device_id(command.device_id)
        prefix = (self._adb(), "-s", serial, "shell")
        values = command.parameters
        if command.action_type is BodyActionType.OPEN_APP:
            package = _validated_package(values["package"])
            component = _validated_component(package, values.get("component"))
            return (
                (*prefix, "am", "start", "-n", component)
                if component is not None
                else (*prefix, "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
            )
        if command.action_type is BodyActionType.RECENTS:
            return (*prefix, "input", "keyevent", "KEYCODE_APP_SWITCH")
        if command.action_type is BodyActionType.BACK:
            return (*prefix, "input", "keyevent", "KEYCODE_BACK")
        if command.action_type is BodyActionType.HOME:
            return (*prefix, "input", "keyevent", "KEYCODE_HOME")
        if command.action_type is BodyActionType.TAP:
            return (*prefix, "input", "tap", str(values["x"]), str(values["y"]))
        if command.action_type is BodyActionType.LONG_PRESS:
            return (
                *prefix,
                "input",
                "swipe",
                str(values["x"]),
                str(values["y"]),
                str(values["x"]),
                str(values["y"]),
                str(values["duration_ms"]),
            )
        if command.action_type is BodyActionType.SWIPE:
            return (
                *prefix,
                "input",
                "swipe",
                str(values["start_x"]), str(values["start_y"]),
                str(values["end_x"]), str(values["end_y"]),
                str(values["duration_ms"]),
            )
        if command.action_type in {BodyActionType.WAIT, BodyActionType.CAPTURE_SNAPSHOT}:
            # The transport receipt represents Kernel coordination.  It does
            # not sleep or capture stale data inside the adapter.
            return (self._adb(), "-s", serial, "get-state")
        raise ValueError("unsupported DeviceBody action")

    def _read_foreground(self, serial: str):
        result = self._run(
            (self._adb(), "-s", serial, "shell", "dumpsys", "window", "windows")
        )
        foreground = (
            parse_foreground_component(result.stdout or "") if result.returncode == 0 else None
        )
        if foreground is not None:
            return foreground
        result = self._run(
            (self._adb(), "-s", serial, "shell", "dumpsys", "activity", "activities")
        )
        return parse_foreground_component(result.stdout or "") if result.returncode == 0 else None

    def _is_ready(self, serial: str) -> bool:
        try:
            result = self._run((self._adb(), "-s", serial, "get-state"))
        except AdbDeviceBodyError:
            return False
        return result.returncode == 0 and (result.stdout or "").strip() == "device"

    def _require_ready(self, serial: str) -> None:
        if not self._is_ready(serial):
            raise AdbDeviceBodyError("adb_target_not_ready")

    def _rejected_receipt(
        self,
        command: BodyActionCommand,
        *,
        started_at: str,
        reason_code: str,
        finished_at: str | None = None,
        adapter_code: str | None = None,
    ) -> BodyExecutionReceipt:
        finished_at = finished_at or self._clock()
        return BodyExecutionReceipt(
            id=self._id_factory(),
            source_receipt_id=f"adb:{command.id}:{self._id_factory()}",
            command_id=command.id,
            kernel_action_id=command.kernel_action_id,
            binding_id=command.binding_id,
            device_id=command.device_id,
            device_boot_id=command.device_boot_id,
            adapter_id=self.adapter_id,
            acknowledgement=ReceiptAcknowledgement.NOT_ACKNOWLEDGED,
            transport=TransportDisposition.REJECTED,
            execution=ExecutionReportState.UNKNOWN,
            started_at=started_at,
            finished_at=finished_at,
            received_at=self._clock(),
            retryable=False,
            adapter_code=adapter_code,
            reason_code=reason_code,
        )

    def _uncertain_receipt(
        self, command: BodyActionCommand, started_at: str, reason_code: str
    ) -> BodyExecutionReceipt:
        # A command can have reached Android even when local transport timed
        # out.  Mark it accepted/unknown so the durable command reconciles;
        # never request automatic execution replay.
        finished_at = self._clock()
        return BodyExecutionReceipt(
            id=self._id_factory(),
            source_receipt_id=f"adb:{command.id}:{self._id_factory()}",
            command_id=command.id,
            kernel_action_id=command.kernel_action_id,
            binding_id=command.binding_id,
            device_id=command.device_id,
            device_boot_id=command.device_boot_id,
            adapter_id=self.adapter_id,
            acknowledgement=ReceiptAcknowledgement.ACKNOWLEDGED,
            transport=TransportDisposition.ACCEPTED,
            execution=ExecutionReportState.UNKNOWN,
            started_at=started_at,
            finished_at=finished_at,
            received_at=self._clock(),
            retryable=False,
            reason_code=reason_code,
        )

    def _adb(self) -> str:
        return str(self._adb_path.resolve())

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(command, self._timeout_seconds)
        except subprocess.TimeoutExpired:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdbDeviceBodyError("adb_transport_unavailable") from exc


def _subprocess_runner(
    command: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _serial_from_device_id(device_id: str) -> str:
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("device_id must be explicit")
    serial = device_id[4:] if device_id.startswith("adb:") else device_id
    if (
        not serial
        or len(serial) > 256
        or serial.startswith("-")
        or any(character.isspace() or ord(character) < 33 for character in serial)
    ):
        raise ValueError("invalid Android device_id")
    return serial


def _validated_package(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value.startswith("-")
        or any(character.isspace() or ord(character) < 33 for character in value)
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_." for character in value)
    ):
        raise ValueError("invalid Android package")
    return value


def _validated_component(package: str, value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value.startswith("-")
        or any(character.isspace() or ord(character) < 33 for character in value)
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.$/" for character in value)
    ):
        raise ValueError("invalid Android component")
    component = value if "/" in value else f"{package}/{value}"
    component_package, _, component_class = component.partition("/")
    if component_package != package or not component_class:
        raise ValueError("component must belong to requested Android package")
    return component


def _parse_resolved_component(output: str, package: str) -> str | None:
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate or "/" not in candidate:
            continue
        component_package, _, component_class = candidate.partition("/")
        if component_package == package and component_class:
            return candidate
    return None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
