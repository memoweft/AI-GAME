from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .domain import Target, TargetKind, TargetStatus
from .repository import utc_now


@dataclass(frozen=True, slots=True)
class AdbDevice:
    serial: str
    raw_state: str
    status: TargetStatus
    properties: dict[str, str]


@dataclass(frozen=True, slots=True)
class AdbDiscoveryResult:
    status: str
    adb_path: str | None
    message: str
    devices: tuple[AdbDevice, ...]
    targets: tuple[Target, ...]


def is_emulator_adb_device(device: AdbDevice) -> bool:
    """Classify only high-confidence local Android emulator ADB targets.

    This classifier is intentionally conservative: a USB, wireless, or opaque
    target cannot become a profile merely because it is currently ``device``.
    It performs no extra subprocess work and keeps the raw transport internal.
    """

    serial = device.serial.casefold()
    properties = {key.casefold(): value.casefold() for key, value in device.properties.items()}
    if serial.startswith("emulator-"):
        return True
    marker_values = " ".join(
        properties.get(key, "") for key in ("product", "model", "device")
    )
    return any(
        marker in marker_values
        for marker in (
            "sdk_gphone",
            "emulator",
            "goldfish",
            "ranchu",
            # MuMu 12/15 currently reports a product family/model marker rather
            # than ro.kernel.qemu=1.  Keep these markers in the one shared
            # classifier used by both discovery and the save-time probe.
            "mumu",
            "v2241a",
        )
    )


def emulator_adb_devices(devices: Sequence[AdbDevice]) -> tuple[AdbDevice, ...]:
    """Return ready, high-confidence emulator candidates only."""

    return tuple(
        device
        for device in devices
        if device.status is TargetStatus.READY and is_emulator_adb_device(device)
    )


def parse_adb_devices_output(output: str) -> list[AdbDevice]:
    """Parse ``adb devices -l`` without inferring readiness from a serial alone."""

    devices: list[AdbDevice] = []
    state_map = {
        "device": TargetStatus.READY,
        "offline": TargetStatus.OFFLINE,
        "unauthorized": TargetStatus.UNAUTHORIZED,
    }
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices attached"):
            continue
        if line.startswith("*") or line.startswith("adb server"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, raw_state = parts[0], parts[1].lower()
        properties: dict[str, str] = {}
        for token in parts[2:]:
            if ":" not in token:
                continue
            key, value = token.split(":", 1)
            if key:
                properties[key] = value
        devices.append(
            AdbDevice(
                serial=serial,
                raw_state=raw_state,
                status=state_map.get(raw_state, TargetStatus.UNKNOWN),
                properties=properties,
            )
        )
    return devices


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class AdbTargetDiscovery:
    """Read-only Android target discovery.

    The adapter resolves one executable and invokes exactly ``adb devices -l``.
    No server lifecycle, connection, shell, package, or input command is issued.
    """

    def __init__(
        self,
        *,
        adb_path: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._adb_path = str(adb_path) if adb_path is not None else None
        self._env = dict(os.environ if env is None else env)
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    def resolve_adb_path(self) -> Path | None:
        configured = self._adb_path or self._env.get("AI_GAME_ADB_PATH", "").strip()
        if configured:
            expanded = os.path.expandvars(os.path.expanduser(configured))
            candidate = Path(expanded)
            if candidate.is_dir():
                candidate = candidate / "adb.exe"
            if candidate.is_file():
                return candidate.resolve()
            found = shutil.which(expanded, path=self._env.get("PATH"))
            return Path(found).resolve() if found else None

        path_value = self._env.get("PATH")
        found = shutil.which("adb.exe", path=path_value) or shutil.which(
            "adb", path=path_value
        )
        return Path(found).resolve() if found else None

    def discover(self) -> AdbDiscoveryResult:
        adb_path = self.resolve_adb_path()
        if adb_path is None:
            return AdbDiscoveryResult(
                status="not_configured",
                adb_path=None,
                message="未在 AI_GAME_ADB_PATH 或 PATH 中找到 ADB。",
                devices=(),
                targets=(),
            )

        command = (str(adb_path), "devices", "-l")
        try:
            completed = (
                self._runner(command)
                if self._runner is not None
                else self._run_read_only_command(command)
            )
        except subprocess.TimeoutExpired:
            return AdbDiscoveryResult(
                status="error",
                adb_path=str(adb_path),
                message="ADB 目标发现超时。",
                devices=(),
                targets=(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return AdbDiscoveryResult(
                status="error",
                adb_path=str(adb_path),
                message=f"ADB 目标发现失败：{str(exc)[:300]}",
                devices=(),
                targets=(),
            )

        if completed.returncode != 0:
            reason = (completed.stderr or completed.stdout or "unknown error").strip()
            return AdbDiscoveryResult(
                status="error",
                adb_path=str(adb_path),
                message=f"ADB 目标发现退出码 {completed.returncode}：{reason[:300]}",
                devices=(),
                targets=(),
            )

        devices = tuple(parse_adb_devices_output(completed.stdout or ""))
        emulator_devices = emulator_adb_devices(devices)
        now = utc_now()
        targets = tuple(self._to_target(device, now) for device in emulator_devices)
        return AdbDiscoveryResult(
            status="ready",
            adb_path=str(adb_path),
            message=f"发现 {len(emulator_devices)} 个 Android 模拟器。",
            devices=devices,
            targets=targets,
        )

    def discover_emulators(self) -> AdbDiscoveryResult:
        """Discover emulators, restoring confirmed local MuMu transports.

        ``discover()`` deliberately remains a read-only inventory operation.
        The product's explicit emulator-discovery path may additionally restore
        the host-side ADB transports after a Windows reboot, but only for fully
        started VMs whose MuMu CLI records contain valid loopback ADB endpoints.
        The second inventory plus the shared high-confidence classifier remains
        the authority for whether profile candidates exist.
        """

        initial = self.discover()
        if initial.status != "ready":
            return initial
        endpoints = self._running_mumu_endpoints()
        adb_path = self.resolve_adb_path()
        if not endpoints or adb_path is None:
            return initial
        ready_serials = {
            device.serial
            for device in initial.devices
            if device.status is TargetStatus.READY
        }
        attempted = False
        connected = False
        for endpoint in endpoints:
            if endpoint in ready_serials:
                continue
            attempted = True
            command = (str(adb_path), "connect", endpoint)
            try:
                completed = (
                    self._runner(command)
                    if self._runner is not None
                    else self._run_read_only_command(command)
                )
            except (OSError, subprocess.SubprocessError):
                continue
            connected = connected or completed.returncode == 0
        if not attempted or not connected:
            return initial
        return self.discover()

    def _running_mumu_endpoints(self) -> tuple[str, ...]:
        adb_path = self.resolve_adb_path()
        if adb_path is None:
            return ()
        cli_roots = [adb_path.parent]
        cli_roots.extend(parent / "nx_main" for parent in adb_path.parents[:4])
        cli_roots = list(dict.fromkeys(cli_roots))
        cli = next(
            (
                candidate
                for cli_root in cli_roots
                for candidate in (
                    cli_root / "MuMuManager.exe", cli_root / "mumu-cli.exe"
                )
                if candidate.is_file()
            ),
            None,
        )
        if cli is None:
            return ()
        command = (str(cli), "info", "--vmindex", "all")
        try:
            completed = (
                self._runner(command)
                if self._runner is not None
                else self._run_read_only_command(command)
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if completed.returncode != 0:
            return ()
        try:
            payload = json.loads(completed.stdout or "null")
        except (TypeError, json.JSONDecodeError):
            return ()
        entries = (
            tuple(payload.values())
            if isinstance(payload, dict)
            else tuple(payload)
            if isinstance(payload, list)
            else ()
        )
        endpoints: list[str] = []
        for entry in entries:
            endpoint = self._running_mumu_endpoint(entry)
            if endpoint is not None and endpoint not in endpoints:
                endpoints.append(endpoint)
        return tuple(endpoints)

    @staticmethod
    def _running_mumu_endpoint(entry: object) -> str | None:
        if not isinstance(entry, dict):
            return None
        host = entry.get("adb_host_ip")
        port = entry.get("adb_port")
        if not isinstance(host, str):
            return None
        try:
            address = ipaddress.ip_address(host.strip())
        except ValueError:
            return None
        if not address.is_loopback:
            return None
        if isinstance(port, bool) or not isinstance(port, (str, int)):
            return None
        port_text = str(port)
        if not port_text.isdecimal() or not 1 <= int(port_text) <= 65535:
            return None
        if (
            entry.get("is_android_started") is not True
            or entry.get("is_process_started") is not True
            or entry.get("player_state") != "start_finished"
        ):
            return None
        normalized_host = f"[{address}]" if address.version == 6 else str(address)
        return f"{normalized_host}:{port_text}"

    def _run_read_only_command(
        self, command: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
            check=False,
            creationflags=creation_flags,
        )

    @staticmethod
    def _to_target(device: AdbDevice, now: str) -> Target:
        model = device.properties.get("model")
        name = model.replace("_", " ") if model else device.serial
        connection_type = _connection_type(device.serial, device.properties)
        capabilities = (
            ["android_adb", "screen_capture", "touch_input", "ascii_text_input"]
            if device.status is TargetStatus.READY
            else []
        )
        details = {
            "address": device.serial,
            "detail": f"ADB 报告的原始状态：{device.raw_state}。",
            "capabilities": capabilities,
            "adb_state": device.raw_state,
            "connection_type": connection_type,
            "properties": dict(device.properties),
        }
        return Target(
            id=f"adb:{device.serial}",
            name=name,
            kind=TargetKind.ANDROID,
            status=device.status.value,
            source="adb",
            external_id=device.serial,
            details=details,
            discovered_at=now,
            last_seen_at=now,
            updated_at=now,
        )


def _connection_type(serial: str, properties: Mapping[str, str]) -> str:
    if serial.startswith("emulator-") or serial.startswith("127.0.0.1:"):
        return "emulator"
    if "usb" in properties or ":" not in serial:
        return "usb"
    return "wireless"
