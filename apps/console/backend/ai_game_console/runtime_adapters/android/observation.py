from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from ...adb_executor import parse_png_size
from ...runtime_kernel.observation import (
    ChannelAvailability,
    ConnectionState,
    ConsistencyStatus,
    DeviceState,
    KeyboardState,
    ObservationConsistency,
    Orientation,
    RawObservation,
    RawScreenshot,
    RawUiTree,
)


CommandRunner = Callable[
    [Sequence[str], float, bool], subprocess.CompletedProcess[str | bytes]
]


class AndroidObservationError(RuntimeError):
    """A read-only Android fact could not be captured safely."""


class AndroidObservationProvider:
    """New Runtime read-only ADB adapter bound to an explicit device_id."""

    def __init__(
        self,
        *,
        adb_path: str | Path,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 5.0,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.adb_path = Path(adb_path)
        self._runner = runner
        self._timeout_seconds = timeout_seconds
        self._clock = clock or _utc_now

    def capture(self, device_id: str) -> RawObservation:
        serial = _serial_from_device_id(device_id)
        started_at = self._clock()
        self._require_connected(serial)

        screenshot_result = self._run(
            (self._adb(), "-s", serial, "exec-out", "screencap", "-p"),
            binary=True,
        )
        screenshot_at = self._clock()
        if screenshot_result.returncode != 0 or not isinstance(
            screenshot_result.stdout, bytes
        ):
            raise AndroidObservationError("android_screenshot_failed")
        screenshot_bytes = screenshot_result.stdout
        try:
            width, height = parse_png_size(screenshot_bytes)
        except ValueError as exc:
            raise AndroidObservationError("android_screenshot_invalid") from exc

        device_state = self._read_device_state(
            serial, fallback_screen_size=(width, height)
        )
        ui_tree = self._read_ui_tree(serial)
        completed_at = self._clock()

        reasons: list[str] = []
        if device_state.screen_size != (width, height):
            reasons.append("screenshot_and_device_screen_size_differ")
        expected_orientation = (
            Orientation.PORTRAIT if width <= height else Orientation.LANDSCAPE
        )
        if device_state.orientation not in {
            Orientation.UNKNOWN,
            expected_orientation,
        }:
            reasons.append("orientation_changed_within_capture_window")
        consistency = ObservationConsistency(
            status=(
                ConsistencyStatus.DEGRADED
                if reasons
                else ConsistencyStatus.CONSISTENT
            ),
            reason=";".join(reasons) if reasons else None,
        )
        return RawObservation(
            device_id=device_id,
            capture_started_at=started_at,
            capture_completed_at=completed_at,
            screenshot=RawScreenshot(
                status=ChannelAvailability.AVAILABLE,
                content=screenshot_bytes,
                width=width,
                height=height,
                captured_at=screenshot_at,
            ),
            ui_tree=ui_tree,
            device_state=device_state,
            consistency=consistency,
        )

    def read_device_state(self, device_id: str) -> DeviceState:
        serial = _serial_from_device_id(device_id)
        self._require_connected(serial)
        return self._read_device_state(serial, fallback_screen_size=None)

    def _read_device_state(
        self,
        serial: str,
        *,
        fallback_screen_size: tuple[int, int] | None,
    ) -> DeviceState:
        screen_result = self._run(
            (self._adb(), "-s", serial, "shell", "wm", "size")
        )
        screen_size = _parse_screen_size(_text(screen_result.stdout))
        if screen_result.returncode != 0 or screen_size is None:
            screen_size = fallback_screen_size
        if screen_size is None:
            raise AndroidObservationError("android_screen_size_unavailable")

        window_result = self._run(
            (self._adb(), "-s", serial, "shell", "dumpsys", "window", "windows")
        )
        foreground_app = (
            _parse_foreground_app(_text(window_result.stdout))
            if window_result.returncode == 0
            else None
        )

        input_result = self._run(
            (self._adb(), "-s", serial, "shell", "dumpsys", "input")
        )
        orientation = (
            _parse_orientation(_text(input_result.stdout))
            if input_result.returncode == 0
            else Orientation.UNKNOWN
        )
        if orientation is Orientation.UNKNOWN:
            orientation = (
                Orientation.PORTRAIT
                if screen_size[0] <= screen_size[1]
                else Orientation.LANDSCAPE
            )

        keyboard_result = self._run(
            (
                self._adb(),
                "-s",
                serial,
                "shell",
                "dumpsys",
                "input_method",
            )
        )
        keyboard_state = (
            _parse_keyboard_state(_text(keyboard_result.stdout))
            if keyboard_result.returncode == 0
            else KeyboardState.UNKNOWN
        )
        return DeviceState(
            status=ChannelAvailability.AVAILABLE,
            foreground_app=foreground_app,
            screen_size=screen_size,
            orientation=orientation,
            keyboard_state=keyboard_state,
            connection_state=ConnectionState.CONNECTED,
            captured_at=self._clock(),
        )

    def _read_ui_tree(self, serial: str) -> RawUiTree:
        captured_at = self._clock()
        try:
            result = self._run(
                (
                    self._adb(),
                    "-s",
                    serial,
                    "exec-out",
                    "uiautomator",
                    "dump",
                    "/dev/tty",
                )
            )
        except AndroidObservationError:
            return RawUiTree(
                status=ChannelAvailability.FAILED,
                content=None,
                captured_at=captured_at,
                error_code="android_ui_tree_transport_failed",
            )
        if result.returncode != 0:
            return RawUiTree(
                status=ChannelAvailability.FAILED,
                content=None,
                captured_at=captured_at,
                error_code="android_ui_tree_command_failed",
            )
        xml = _extract_ui_xml(_text(result.stdout))
        if xml is None:
            return RawUiTree(
                status=ChannelAvailability.UNAVAILABLE,
                content=None,
                captured_at=captured_at,
                error_code="android_ui_tree_unavailable",
            )
        return RawUiTree(
            status=ChannelAvailability.AVAILABLE,
            content=xml.encode("utf-8"),
            captured_at=captured_at,
        )

    def _require_connected(self, serial: str) -> None:
        result = self._run((self._adb(), "-s", serial, "get-state"))
        state = _text(result.stdout).strip().lower()
        if result.returncode != 0 or state != "device":
            if "unauthorized" in state or "unauthorized" in _text(result.stderr).lower():
                code = "android_device_unauthorized"
            elif "offline" in state or "offline" in _text(result.stderr).lower():
                code = "android_device_disconnected"
            else:
                code = "android_device_not_ready"
            raise AndroidObservationError(code)

    def _adb(self) -> str:
        if self._runner is None and not self.adb_path.is_file():
            raise AndroidObservationError("android_adb_unavailable")
        return str(self.adb_path.resolve())

    def _run(
        self, command: Sequence[str], *, binary: bool = False
    ) -> subprocess.CompletedProcess[str | bytes]:
        try:
            if self._runner is not None:
                return self._runner(command, self._timeout_seconds, binary)
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            )
            return subprocess.run(
                list(command),
                capture_output=True,
                text=not binary,
                timeout=self._timeout_seconds,
                check=False,
                shell=False,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise AndroidObservationError("android_read_timeout") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise AndroidObservationError("android_read_unavailable") from exc


def _serial_from_device_id(device_id: str) -> str:
    if not isinstance(device_id, str) or not device_id.strip():
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


def _parse_screen_size(output: str) -> tuple[int, int] | None:
    matches = re.findall(r"(?:Physical|Override)?\s*size:\s*(\d+)x(\d+)", output, re.I)
    if not matches:
        return None
    width, height = matches[-1]
    return int(width), int(height)


def _parse_foreground_app(output: str) -> str | None:
    patterns = (
        r"mCurrentFocus=.*?\s(?:u\d+\s+)?([A-Za-z0-9_.]+)/(?:[A-Za-z0-9_.$]+)",
        r"mFocusedApp=.*?\s(?:u\d+\s+)?([A-Za-z0-9_.]+)/(?:[A-Za-z0-9_.$]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return match.group(1)
    return None


def _parse_orientation(output: str) -> Orientation:
    match = re.search(r"SurfaceOrientation:\s*([0-3])", output)
    if not match:
        return Orientation.UNKNOWN
    return (
        Orientation.PORTRAIT
        if int(match.group(1)) in {0, 2}
        else Orientation.LANDSCAPE
    )


def _parse_keyboard_state(output: str) -> KeyboardState:
    shown = re.search(r"(?:mInputShown|mIsInputViewShown)=(true|false)", output, re.I)
    if not shown:
        return KeyboardState.UNKNOWN
    return KeyboardState.SHOWN if shown.group(1).lower() == "true" else KeyboardState.HIDDEN


def _extract_ui_xml(output: str) -> str | None:
    start_candidates = [index for index in (output.find("<?xml"), output.find("<hierarchy")) if index >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    end = output.rfind("</hierarchy>")
    if end < start:
        return None
    xml = output[start : end + len("</hierarchy>")].strip()
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    if root.tag != "hierarchy":
        return None
    return xml


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
