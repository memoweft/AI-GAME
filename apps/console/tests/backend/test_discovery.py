from __future__ import annotations

import subprocess
from pathlib import Path

from ai_game_console.discovery import AdbTargetDiscovery, parse_adb_devices_output


ADB_OUTPUT = """List of devices attached
emulator-5554          device product:sdk_gphone64_x86_64 model:Pixel_7 device:emu64xa transport_id:1
127.0.0.1:7555         offline transport_id:2
R58M123ABC              unauthorized usb:1-2 transport_id:3
mystery                 recovery product:test
"""


def test_parse_adb_devices_projects_canonical_states() -> None:
    devices = parse_adb_devices_output(ADB_OUTPUT)

    assert [device.serial for device in devices] == [
        "emulator-5554",
        "127.0.0.1:7555",
        "R58M123ABC",
        "mystery",
    ]
    assert [device.status.value for device in devices] == [
        "ready",
        "offline",
        "unauthorized",
        "unknown",
    ]
    assert devices[0].properties == {
        "product": "sdk_gphone64_x86_64",
        "model": "Pixel_7",
        "device": "emu64xa",
        "transport_id": "1",
    }


def test_discovery_invokes_only_adb_devices_l(tmp_path: Path) -> None:
    adb = tmp_path / "platform-tools" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    commands: list[tuple[str, ...]] = []

    def runner(command):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout=ADB_OUTPUT, stderr="")

    discovery = AdbTargetDiscovery(adb_path=adb, runner=runner).discover()

    assert commands == [(str(adb.resolve()), "devices", "-l")]
    assert discovery.status == "ready"
    assert [target.status for target in discovery.targets] == [
        "ready",
        "offline",
        "unauthorized",
        "unknown",
    ]
    assert discovery.targets[0].id == "adb:emulator-5554"
    assert discovery.targets[0].name == "Pixel 7"
    assert discovery.targets[0].details["adb_state"] == "device"
    assert discovery.targets[0].details["connection_type"] == "emulator"
    assert discovery.targets[0].capabilities == [
        "android_adb",
        "screen_capture",
        "touch_input",
        "ascii_text_input",
    ]
    assert discovery.targets[2].details["connection_type"] == "usb"
    assert discovery.targets[2].capabilities == []


def test_missing_adb_is_non_blocking_and_does_not_run_a_command() -> None:
    called = False

    def runner(command):  # pragma: no cover - must never be reached
        nonlocal called
        called = True
        raise AssertionError(command)

    result = AdbTargetDiscovery(env={"PATH": ""}, runner=runner).discover()

    assert result.status == "not_configured"
    assert result.targets == ()
    assert called is False
