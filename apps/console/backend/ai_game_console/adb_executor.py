from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import Settings
from .execution import (
    ActionTransportResult,
    AndroidScreenshot,
    ExecutorProbeResult,
    GuiAction,
)
from .text_input import is_restricted_ascii_text, is_valid_text_input, requires_unicode_text_transport


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]

_ALLOWED_KEYCODES = {
    "KEYCODE_APP_SWITCH",
    "KEYCODE_BACK",
    "KEYCODE_DEL",
    "KEYCODE_DPAD_CENTER",
    "KEYCODE_DPAD_DOWN",
    "KEYCODE_DPAD_LEFT",
    "KEYCODE_DPAD_RIGHT",
    "KEYCODE_DPAD_UP",
    "KEYCODE_ENTER",
    "KEYCODE_HOME",
    "KEYCODE_TAB",
}


class AdbGuiExecutor:
    """Restricted, directly-invoked ADB input adapter.

    This adapter owns neither a queue nor a workflow state transition. Every
    command is an argument array and is launched with ``shell=False``.
    """

    COMMAND_TIMEOUT_SECONDS = 3.0
    LONG_PRESS_DURATION_MS = 600

    def __init__(
        self,
        *,
        enabled: bool,
        adb_path: str | Path | None,
        serial: str | None,
        runner: CommandRunner | None = None,
        timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self.enabled = enabled
        self.adb_path = Path(adb_path) if adb_path else None
        self.serial = serial.strip() if serial else None
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> "AdbGuiExecutor":
        return cls(
            enabled=settings.gui_executor_enabled,
            adb_path=settings.adb_path,
            serial=settings.adb_serial,
        )

    def for_serial(self, serial: str) -> "AdbGuiExecutor":
        """Create a target-bound executor while preserving the ADB transport.

        The configured serial is only the default target.  A discovered USB or
        wireless device can therefore use the same ADB executable without
        mutating process-wide settings or redirecting another active session.
        """
        cleaned = serial.strip()
        if (
            not cleaned
            or len(cleaned) > 256
            or cleaned.startswith("-")
            or any(character.isspace() or ord(character) < 33 for character in cleaned)
        ):
            raise ValueError("invalid adb serial")
        return AdbGuiExecutor(
            enabled=self.enabled,
            adb_path=self.adb_path,
            serial=cleaned,
            runner=self._runner,
            timeout_seconds=self._timeout_seconds,
        )

    def probe(self) -> ExecutorProbeResult:
        if not self.enabled:
            return ExecutorProbeResult(
                status="not_configured",
                configured=False,
                detail="GUI 执行器未启用（AI_GAME_GUI_EXECUTOR_ENABLED=1）。",
                blocker={"code": "executor_not_configured", "message": "GUI 执行器尚未配置。"},
            )
        if self.adb_path is None or not self.serial:
            return ExecutorProbeResult(
                status="not_configured",
                configured=False,
                detail="GUI 执行器缺少 AI_GAME_ADB_PATH 或 AI_GAME_ADB_SERIAL。",
                blocker={"code": "executor_not_configured", "message": "ADB 路径或目标序列号尚未配置。"},
            )
        if not self.adb_path.is_file():
            return ExecutorProbeResult(
                status="unavailable",
                configured=True,
                detail="已配置的 ADB 可执行文件不可用。",
                blocker={"code": "executor_unavailable", "message": "ADB 可执行文件不可用。"},
            )

        try:
            completed = self._run((str(self.adb_path.resolve()), "-s", self.serial, "get-state"))
        except subprocess.TimeoutExpired:
            return ExecutorProbeResult(
                status="unavailable",
                configured=True,
                detail="ADB 就绪探测超时。",
                blocker={"code": "executor_unavailable", "message": "ADB 执行器暂时不可用。"},
            )
        except (OSError, subprocess.SubprocessError):
            return ExecutorProbeResult(
                status="unavailable",
                configured=True,
                detail="ADB 就绪探测无法启动。",
                blocker={"code": "executor_unavailable", "message": "ADB 执行器暂时不可用。"},
            )

        if completed.returncode == 0 and (completed.stdout or "").strip() == "device":
            return ExecutorProbeResult(
                status="ready",
                configured=True,
                detail="已通过 ADB get-state 验证配置的 Android 目标就绪。",
                blocker=None,
            )
        return ExecutorProbeResult(
            status="stopped",
            configured=True,
            detail="ADB 已启动，但配置的 Android 目标当前不是 device 状态。",
            blocker={"code": "executor_target_not_ready", "message": "配置的 Android 目标尚未就绪。"},
        )

    def execute(self, action: GuiAction) -> ActionTransportResult:
        probe = self.probe()
        if not probe.ready:
            raise RuntimeError(probe.blocker["code"] if probe.blocker else "executor_not_ready")
        if action.action == "text" and action.text is not None and requires_unicode_text_transport(action.text):
            if not is_valid_text_input(action.text):
                raise ValueError("invalid text action")
            return self._execute_unicode_text(action.text)
        command = self._command_for(action)
        try:
            completed = self._run(command)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("executor_action_timeout") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("executor_action_unavailable") from exc
        if completed.returncode != 0:
            raise RuntimeError("executor_action_rejected")
        return ActionTransportResult(
            accepted=True,
            detail="ADB 已接受该单原子输入；目标界面结果尚未验证。",
        )

    def _execute_unicode_text(self, text: str) -> ActionTransportResult:
        mumu_cli = self._mumu_cli_path()
        if mumu_cli is None:
            raise RuntimeError("executor_unicode_text_cli_unavailable")
        try:
            info_result = self._run((str(mumu_cli), "info", "--vmindex", "all"))
        except subprocess.TimeoutExpired:
            raise RuntimeError("executor_unicode_text_discovery_timeout") from None
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("executor_unicode_text_cli_unavailable") from None
        if info_result.returncode != 0:
            raise RuntimeError("executor_unicode_text_discovery_unavailable")
        vm_index, android_major = _resolve_mumu_vm(info_result.stdout or "", self.serial or "")
        command = (
            str(mumu_cli),
            "control",
            "--vmindex",
            vm_index,
            "--version",
            android_major,
            "tool",
            "cmd",
            "--cmd",
            "input_text",
            "--text",
            text,
        )
        try:
            completed = self._run(command)
        except subprocess.TimeoutExpired:
            # A timed-out transport might already have delivered the text.
            # This non-idempotent action must never be replayed automatically.
            raise RuntimeError("executor_unicode_text_uncertain") from None
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("executor_unicode_text_input_unavailable") from None
        outcome = _mumu_text_result_outcome(completed.stdout or "")
        if outcome == "rejected":
            raise RuntimeError("executor_unicode_text_rejected")
        if completed.returncode != 0 or outcome != "accepted":
            raise RuntimeError("executor_unicode_text_uncertain")
        return ActionTransportResult(
            accepted=True,
            detail="MuMu 已接受该单原子文本输入；目标界面结果尚未验证。",
        )

    def _mumu_cli_path(self) -> Path | None:
        if self.adb_path is None:
            return None
        try:
            candidate = self.adb_path.resolve().parents[3] / "nx_main" / "mumu-cli.exe"
        except IndexError:
            return None
        return candidate if candidate.is_file() else None

    def capture_screenshot(self) -> AndroidScreenshot:
        """Capture one fresh PNG frame without invoking a host shell."""

        probe = self.probe()
        if not probe.ready:
            raise RuntimeError(probe.blocker["code"] if probe.blocker else "executor_not_ready")
        command = (
            str(self.adb_path.resolve()),
            "-s",
            self.serial or "",
            "exec-out",
            "screencap",
            "-p",
        )
        try:
            completed = self._run_binary(command)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("executor_screenshot_timeout") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("executor_screenshot_unavailable") from exc
        if completed.returncode != 0:
            raise RuntimeError("executor_screenshot_rejected")
        payload = completed.stdout
        if not isinstance(payload, bytes):
            raise RuntimeError("executor_screenshot_invalid")
        try:
            width, height = parse_png_size(payload)
        except ValueError as exc:
            raise RuntimeError("executor_screenshot_invalid") from exc
        return AndroidScreenshot(png_bytes=payload, width=width, height=height)

    def _command_for(self, action: GuiAction) -> tuple[str, ...]:
        # Schema validation and the service boundary establish the action shape.
        # Keep the construction here explicit so the adapter can never execute an
        # arbitrary shell fragment supplied by a caller.
        prefix = (str(self.adb_path.resolve()), "-s", self.serial or "", "shell", "input")
        if action.action == "tap":
            if (
                isinstance(action.x, bool)
                or isinstance(action.y, bool)
                or not isinstance(action.x, int)
                or not isinstance(action.y, int)
                or not (0 <= action.x <= 10_000 and 0 <= action.y <= 10_000)
            ):
                raise ValueError("invalid tap action")
            return (*prefix, "tap", str(action.x), str(action.y))
        if action.action == "keyevent":
            if action.keycode not in _ALLOWED_KEYCODES:
                raise ValueError("invalid keyevent action")
            return (*prefix, "keyevent", action.keycode or "")
        if action.action == "text":
            if not is_valid_text_input(action.text) or not is_restricted_ascii_text(action.text):
                raise ValueError("invalid text action")
            # Android's input command uses %s for literal spaces. The schema
            # permits only inert ASCII word/punctuation characters.
            return (*prefix, "text", (action.text or "").replace(" ", "%s"))
        if action.action == "swipe":
            _validate_coordinate_pair(action.x, action.y)
            _validate_coordinate_pair(action.end_x, action.end_y)
            duration = _validated_duration(action.duration_ms)
            return (
                *prefix,
                "swipe",
                str(action.x),
                str(action.y),
                str(action.end_x),
                str(action.end_y),
                str(duration),
            )
        if action.action == "long_press":
            _validate_coordinate_pair(action.x, action.y)
            duration = _validated_duration(
                self.LONG_PRESS_DURATION_MS
                if action.duration_ms is None
                else action.duration_ms
            )
            return (
                *prefix,
                "swipe",
                str(action.x),
                str(action.y),
                str(action.x),
                str(action.y),
                str(duration),
            )
        raise ValueError(f"unsupported action: {action.action}")

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if self._runner is not None:
            return self._runner(command, self._timeout_seconds)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
            check=False,
            shell=False,
            creationflags=creation_flags,
        )

    def _run_binary(self, command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        if self._runner is not None:
            # Test adapters provide the same explicit command-array boundary;
            # their stdout may be bytes for an exec-out screenshot.
            return self._runner(command, self._timeout_seconds)  # type: ignore[return-value]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.run(
            list(command),
            capture_output=True,
            text=False,
            timeout=self._timeout_seconds,
            check=False,
            shell=False,
            creationflags=creation_flags,
        )


def _resolve_mumu_vm(raw_info: str, serial: str) -> tuple[str, str]:
    """Find one and only one MuMu VM corresponding to the configured ADB serial."""

    try:
        parsed = json.loads(raw_info)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("executor_unicode_text_discovery_invalid") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("executor_unicode_text_discovery_invalid")
    matches: list[tuple[str, str]] = []
    for entry in parsed.values():
        if not isinstance(entry, dict):
            continue
        host = entry.get("adb_host_ip")
        port = entry.get("adb_port")
        index = entry.get("index")
        android_version = entry.get("android_version")
        if not isinstance(host, str) or not isinstance(port, (str, int)):
            continue
        if f"{host}:{port}" != serial:
            continue
        if not isinstance(index, (str, int)) or not isinstance(android_version, str):
            continue
        major = android_version.split(".", 1)[0]
        if not major.isdecimal() or int(major) < 1:
            continue
        matches.append((str(index), major))
    if len(matches) != 1:
        raise RuntimeError("executor_unicode_text_target_unresolved")
    return matches[0]


def _mumu_text_result_outcome(stdout: str) -> str:
    """Classify the CLI acknowledgement without inferring success from silence.

    A text command is non-idempotent. Only a JSON object explicitly carrying
    one or both integer result codes set to zero confirms acceptance. Every
    opaque or malformed response remains uncertain.
    """

    try:
        parsed = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return "uncertain"
    if not isinstance(parsed, dict):
        return "uncertain"
    codes: list[int] = []
    for key in ("errcode", "error_code"):
        if key not in parsed:
            continue
        value = parsed[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return "uncertain"
        codes.append(value)
    if not codes:
        return "uncertain"
    if any(value != 0 for value in codes):
        return "rejected"
    return "accepted"


def parse_png_size(payload: bytes) -> tuple[int, int]:
    """Read IHDR dimensions without image-library or filesystem side effects."""

    signature = b"\x89PNG\r\n\x1a\n"
    if (
        len(payload) < 24
        or payload[:8] != signature
        or payload[8:12] != b"\x00\x00\x00\x0d"
        or payload[12:16] != b"IHDR"
    ):
        raise ValueError("payload is not a PNG with an IHDR chunk")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if width < 1 or height < 1:
        raise ValueError("PNG dimensions must be positive")
    return width, height


def _validate_coordinate_pair(x: int | None, y: int | None) -> None:
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, int)
        or not isinstance(y, int)
        or not (0 <= x <= 10_000 and 0 <= y <= 10_000)
    ):
        raise ValueError("invalid coordinate pair")


def _validated_duration(value: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
        raise ValueError("invalid gesture duration")
    return value
