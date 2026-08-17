from __future__ import annotations

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
        now = utc_now()
        targets = tuple(self._to_target(device, now) for device in devices)
        return AdbDiscoveryResult(
            status="ready",
            adb_path=str(adb_path),
            message=f"发现 {len(devices)} 个 Android 目标。",
            devices=devices,
            targets=targets,
        )

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
