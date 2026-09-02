from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ai_game_console.discovery import (
    AdbDevice,
    AdbTargetDiscovery,
    emulator_adb_devices,
    is_emulator_adb_device,
    parse_adb_devices_output,
)
from ai_game_console.domain import TargetStatus


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


def test_emulator_classifier_accepts_mumu_properties_without_trusting_loopback_or_abi() -> None:
    assert is_emulator_adb_device(
        AdbDevice(
            "127.0.0.1:16384",
            "device",
            TargetStatus.READY,
            {"product": "MuMu", "model": "V2241A", "device": "V2241A"},
        )
    )
    assert is_emulator_adb_device(
        AdbDevice("emulator-5554", "device", TargetStatus.READY, {})
    )
    assert not is_emulator_adb_device(
        AdbDevice(
            "127.0.0.1:16384",
            "device",
            TargetStatus.READY,
            {"model": "Pixel_9", "abi": "x86_64", "api": "35"},
        )
    )
    assert not is_emulator_adb_device(
        AdbDevice(
            "R58M123ABC",
            "device",
            TargetStatus.READY,
            {"model": "Pixel_9", "usb": "1-2", "abi": "x86_64"},
        )
    )


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
    # The public target projection contains only ready, high-confidence local
    # emulators. Raw transport states remain available internally for
    # diagnostics and must not become saveable product targets.
    assert [target.status for target in discovery.targets] == ["ready"]
    assert [device.status for device in discovery.devices] == [
        TargetStatus.READY,
        TargetStatus.OFFLINE,
        TargetStatus.UNAUTHORIZED,
        TargetStatus.UNKNOWN,
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
    assert all(target.details["connection_type"] == "emulator" for target in discovery.targets)


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


def test_emulator_discovery_restores_one_confirmed_running_mumu_transport(tmp_path: Path) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    cli = mumu_root / "nx_main" / "mumu-cli.exe"
    adb.parent.mkdir(parents=True)
    cli.parent.mkdir(parents=True)
    adb.touch()
    cli.touch()
    commands: list[tuple[str, ...]] = []
    inventories = iter(
        (
            "List of devices attached\n",
            "List of devices attached\n"
            "127.0.0.1:16384 device product:MuMu model:V2241A device:V2241A\n",
        )
    )

    def runner(command):
        command = tuple(command)
        commands.append(command)
        if command == (str(adb.resolve()), "devices", "-l"):
            return subprocess.CompletedProcess(command, 0, stdout=next(inventories), stderr="")
        if command == (str(cli), "info", "--vmindex", "all"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({
                    "0": {
                        "index": "0",
                        "adb_port": 16384,
                        "is_android_started": True,
                        "is_process_started": True,
                        "player_state": "start_finished",
                    }
                }),
                stderr="",
            )
        if command == (str(adb.resolve()), "connect", "127.0.0.1:16384"):
            return subprocess.CompletedProcess(command, 0, stdout="connected", stderr="")
        raise AssertionError(command)

    result = AdbTargetDiscovery(adb_path=adb, runner=runner).discover_emulators()

    assert commands == [
        (str(adb.resolve()), "devices", "-l"),
        (str(cli), "info", "--vmindex", "all"),
        (str(adb.resolve()), "connect", "127.0.0.1:16384"),
        (str(adb.resolve()), "devices", "-l"),
    ]
    assert [item.properties["model"] for item in emulator_adb_devices(result.devices)] == ["V2241A"]


def test_emulator_discovery_does_not_connect_without_exact_running_mumu_identity(tmp_path: Path) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    cli = mumu_root / "nx_main" / "mumu-cli.exe"
    adb.parent.mkdir(parents=True)
    cli.parent.mkdir(parents=True)
    adb.touch()
    cli.touch()
    unsafe_entries = (
        {"0": {"index": "0", "adb_host_ip": "192.168.1.2", "adb_port": 16384,
               "is_android_started": True, "is_process_started": True, "player_state": "start_finished"}},
        {"0": {"index": "0", "adb_port": 16384,
               "is_android_started": False, "is_process_started": True, "player_state": "start_finished"}},
        {"0": {"index": "0", "adb_port": 16384,
               "is_android_started": True, "is_process_started": True, "player_state": "start_finished"},
         "1": {"index": "1", "adb_port": 16416,
               "is_android_started": True, "is_process_started": True, "player_state": "start_finished"}},
    )

    for payload in unsafe_entries:
        commands: list[tuple[str, ...]] = []

        def runner(command):
            command = tuple(command)
            commands.append(command)
            if command == (str(adb.resolve()), "devices", "-l"):
                return subprocess.CompletedProcess(command, 0, stdout="List of devices attached\n", stderr="")
            if command == (str(cli), "info", "--vmindex", "all"):
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
            raise AssertionError(command)

        result = AdbTargetDiscovery(adb_path=adb, runner=runner).discover_emulators()

        assert result.devices == ()
        assert all(command[1:2] != ("connect",) for command in commands)


def test_emulator_discovery_does_not_reconnect_when_an_emulator_is_already_ready(tmp_path: Path) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    cli = mumu_root / "nx_main" / "mumu-cli.exe"
    adb.parent.mkdir(parents=True)
    cli.parent.mkdir(parents=True)
    adb.touch()
    cli.touch()
    commands: list[tuple[str, ...]] = []

    def runner(command):
        command = tuple(command)
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=("List of devices attached\n"
                    "127.0.0.1:16384 device product:MuMu model:V2241A device:V2241A\n"),
            stderr="",
        )

    result = AdbTargetDiscovery(adb_path=adb, runner=runner).discover_emulators()

    assert len(emulator_adb_devices(result.devices)) == 1
    assert commands == [(str(adb.resolve()), "devices", "-l")]
