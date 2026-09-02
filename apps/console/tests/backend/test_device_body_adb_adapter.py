from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_game_console.device_body.adb_adapter import AdbDeviceBodyAdapter
from ai_game_console.device_body.domain import (
    BodyActionCommand,
    BodyActionType,
    BodyCommandStatus,
    CapabilityKind,
    CapabilityState,
    ConnectionState,
    DeviceBodyBinding,
    ExecutionReportState,
    ReceiptAcknowledgement,
    TransportDisposition,
)


TIME = "2026-08-24T12:00:00Z"
BOOT_ID = "3b7e5431-6a2d-45c1-8751-121212121212"


def _binding(**overrides: object) -> DeviceBodyBinding:
    values: dict[str, object] = {
        "id": "binding-1",
        "session_id": "session-1",
        "device_id": "adb:127.0.0.1:16384",
        "adapter_id": "adb-r4-compat",
        "device_boot_id": BOOT_ID,
        "connection_state": ConnectionState.CONNECTED,
        "capability_revision": 0,
        "event_cursor": 0,
        "action_cursor": 0,
        "bound_at": TIME,
        "updated_at": TIME,
    }
    values.update(overrides)
    return DeviceBodyBinding(**values)  # type: ignore[arg-type]


def _command(**overrides: object) -> BodyActionCommand:
    values: dict[str, object] = {
        "id": "body-command-1",
        "binding_id": "binding-1",
        "session_id": "session-1",
        "device_id": "adb:127.0.0.1:16384",
        "device_boot_id": BOOT_ID,
        "kernel_action_id": "kernel-action-1",
        "action_cursor": 1,
        "action_type": BodyActionType.OPEN_APP,
        "parameters": {"package": "com.android.settings"},
        "expected_state": {},
        "required_capabilities": (
            CapabilityKind.OPEN_APP,
            CapabilityKind.DEVICE_SNAPSHOT,
            CapabilityKind.FOREGROUND_APPLICATION,
        ),
        "status": BodyCommandStatus.DISPATCHING,
        "issued_at": TIME,
        "dispatched_at": TIME,
    }
    values.update(overrides)
    return BodyActionCommand(**values)  # type: ignore[arg-type]


def _adapter(tmp_path: Path, runner):
    adb = tmp_path / "adb.exe"
    adb.touch()
    ids = iter(("receipt-1", "source-1", "receipt-2", "source-2", "snapshot-1"))
    return adb, AdbDeviceBodyAdapter(
        adb_path=adb,
        runner=runner,
        clock=lambda: TIME,
        id_factory=lambda: next(ids),
    )


def test_package_catalog_resolves_only_the_exact_requested_package(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command, timeout):
        assert timeout == 5.0
        commands.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="priority=0\ncom.android.settings/.Settings\n",
            stderr="",
        )

    adb, adapter = _adapter(tmp_path, runner)
    resolved = adapter.package_catalog.resolve(
        "adb:127.0.0.1:16384", "com.android.settings"
    )

    assert resolved is not None
    assert resolved.package == "com.android.settings"
    assert resolved.component == "com.android.settings/.Settings"
    assert commands == [
        (
            str(adb.resolve()), "-s", "127.0.0.1:16384", "shell", "cmd", "package",
            "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c",
            "android.intent.category.LAUNCHER", "com.android.settings",
        )
    ]


def test_open_app_dispatches_exact_package_and_optional_component(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command, _timeout):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    adb, adapter = _adapter(tmp_path, runner)
    package_only = adapter.execute(_command())
    component = adapter.execute(
        _command(
            id="body-command-2",
            kernel_action_id="kernel-action-2",
            parameters={"package": "com.android.settings", "component": ".Settings"},
        )
    )

    assert package_only.transport is TransportDisposition.ACCEPTED
    assert package_only.execution is ExecutionReportState.REPORTED
    assert component.acknowledgement is ReceiptAcknowledgement.ACKNOWLEDGED
    assert commands == [
        (
            str(adb.resolve()), "-s", "127.0.0.1:16384", "shell", "monkey", "-p",
            "com.android.settings", "-c", "android.intent.category.LAUNCHER", "1",
        ),
        (
            str(adb.resolve()), "-s", "127.0.0.1:16384", "shell", "am", "start", "-n",
            "com.android.settings/.Settings",
        ),
    ]


def test_recents_is_one_explicit_keyevent_argument_array(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command, _timeout):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    adb, adapter = _adapter(tmp_path, runner)
    receipt = adapter.execute(
        _command(
            action_type=BodyActionType.RECENTS,
            parameters={},
            expected_state={},
            required_capabilities=(CapabilityKind.RECENTS,),
        )
    )

    assert receipt.transport is TransportDisposition.ACCEPTED
    assert commands == [
        (
            str(adb.resolve()), "-s", "127.0.0.1:16384", "shell", "input", "keyevent",
            "KEYCODE_APP_SWITCH",
        )
    ]


def test_capabilities_report_plain_adb_unicode_unsupported_and_readback_separately(
    tmp_path: Path,
) -> None:
    def runner(command, _timeout):
        assert command[-1] == "get-state"
        return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")

    _, adapter = _adapter(tmp_path, runner)
    capabilities = {item.kind: item for item in adapter.discover_capabilities(_binding())}

    assert set(capabilities) == set(CapabilityKind)
    assert capabilities[CapabilityKind.INPUT_TEXT_UNICODE].state is CapabilityState.UNSUPPORTED
    assert capabilities[CapabilityKind.INPUT_TEXT_UNICODE].reason_code == "adb_unicode_transport_unsupported"
    assert capabilities[CapabilityKind.TEXT_READ_BACK].state is CapabilityState.UNSUPPORTED
    assert capabilities[CapabilityKind.TEXT_READ_BACK].reason_code == "adb_text_read_back_unsupported"
    assert capabilities[CapabilityKind.OPEN_APP].state is CapabilityState.READY


def test_mumu_unicode_transport_and_text_readback_are_distinct_capabilities(
    tmp_path: Path,
) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    cli = mumu_root / "nx_main" / "mumu-cli.exe"
    cli.parent.mkdir()
    cli.touch()

    def runner(command, _timeout):
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        assert command == (str(cli.resolve()), "info", "--vmindex", "all")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"0":{"adb_host_ip":"127.0.0.1","adb_port":16384,'
                '"index":"0","android_version":"15.0"}}'
            ),
            stderr="",
        )

    ids = iter(("cap-1", "cap-2", "cap-3"))
    adapter = AdbDeviceBodyAdapter(
        adb_path=adb,
        runner=runner,
        clock=lambda: TIME,
        id_factory=lambda: next(ids),
    )
    capabilities = {item.kind: item for item in adapter.discover_capabilities(_binding())}

    assert capabilities[CapabilityKind.INPUT_TEXT_UNICODE].state is CapabilityState.READY
    assert capabilities[CapabilityKind.TEXT_READ_BACK].state is CapabilityState.UNSUPPORTED


def test_unicode_rejection_and_timeout_are_non_retryable_and_never_echo_plaintext(
    tmp_path: Path,
) -> None:
    entered_text = "不要在回执里泄露"
    commands: list[tuple[str, ...]] = []

    def runner(command, _timeout):
        commands.append(tuple(command))
        raise subprocess.TimeoutExpired(command, _timeout)

    _, adapter = _adapter(tmp_path, runner)
    unicode_command = _command(
        action_type=BodyActionType.INPUT_TEXT_UNICODE,
        parameters={
            "text_ref": "kernel-action:kernel-action-1:input",
            "text_sha256": "a" * 64,
            "text_length": len(entered_text),
        },
        expected_state={"read_back_text_sha256": "a" * 64, "read_back_text_length": len(entered_text)},
        required_capabilities=(CapabilityKind.INPUT_TEXT_UNICODE, CapabilityKind.TEXT_READ_BACK),
    )
    unicode_receipt = adapter.execute(unicode_command)
    timeout_receipt = adapter.execute(_command())

    assert unicode_receipt.reason_code == "adb_text_read_back_unsupported"
    assert unicode_receipt.retryable is False
    assert entered_text not in repr(unicode_receipt)
    assert timeout_receipt.execution is ExecutionReportState.UNKNOWN
    assert timeout_receipt.retryable is False
    assert len(commands) == 1


def test_fresh_snapshot_captures_current_boot_package_and_activity(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command, _timeout):
        commands.append(tuple(command))
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        if command[-1].endswith("/boot_id"):
            return subprocess.CompletedProcess(command, 0, stdout=f"{BOOT_ID}\n", stderr="")
        if command[-2:] == ("window", "windows"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="mCurrentFocus=Window{a u0 com.android.settings/.Settings}",
                stderr="",
            )
        raise AssertionError(command)

    _, adapter = _adapter(tmp_path, runner)
    snapshot = adapter.capture_snapshot(
        _binding(),
        capture_request_id="capture:kernel-action-1",
        caused_by_command_id="body-command-1",
    )

    assert snapshot.sequence == 1
    assert snapshot.foreground_package == "com.android.settings"
    assert snapshot.foreground_activity == ".Settings"
    assert snapshot.caused_by_command_id == "body-command-1"
    assert any(command[-1].endswith("/boot_id") for command in commands)
    assert any(command[-2:] == ("window", "windows") for command in commands)


def test_invalid_component_cannot_change_the_requested_package(tmp_path: Path) -> None:
    _, adapter = _adapter(
        tmp_path,
        lambda command, timeout: pytest.fail(f"no transport expected: {command!r}"),
    )
    receipt = adapter.execute(
        _command(parameters={"package": "com.android.settings", "component": "other.app/.Main"})
    )

    assert receipt.transport is TransportDisposition.REJECTED
    assert receipt.reason_code == "adb_command_invalid"


def test_unclaimed_or_uncertain_command_never_reaches_adb_transport(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    _, adapter = _adapter(
        tmp_path,
        lambda command, timeout: calls.append(tuple(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    receipt = adapter.execute(_command(status=BodyCommandStatus.PREPARED, dispatched_at=None))

    assert receipt.transport is TransportDisposition.REJECTED
    assert receipt.reason_code == "body_command_not_claimed"
    assert calls == []
