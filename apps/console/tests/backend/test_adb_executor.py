from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_game_console.adb_executor import AdbGuiExecutor
from ai_game_console.adb_executor import parse_png_size
from ai_game_console.api import create_app
from ai_game_console.config import Settings
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.domain import Target, TargetKind
from ai_game_console.execution import ActionTransportResult, ExecutorProbeResult, GuiAction
from ai_game_console.repository import utc_now
from ai_game_console.service import RuntimeProbe

from conftest import WRITE_HEADERS, build_settings


class FakeReadyExecutor:
    serial = "127.0.0.1:16384"

    def __init__(self) -> None:
        self.actions: list[GuiAction] = []
        self.probe_calls = 0

    def probe(self) -> ExecutorProbeResult:
        self.probe_calls += 1
        return ExecutorProbeResult(
            status="ready",
            configured=True,
            detail="fake target ready",
            blocker=None,
        )

    def execute(self, action: GuiAction) -> ActionTransportResult:
        self.actions.append(action)
        return ActionTransportResult(
            accepted=True,
            detail="ADB 已接受该单原子输入；目标界面结果尚未验证。",
        )


def configured_settings(tmp_path: Path, adb: Path) -> Settings:
    base = build_settings(tmp_path)
    return Settings(
        project_root=base.project_root,
        data_dir=base.data_dir,
        database_path=base.database_path,
        frontend_dist=base.frontend_dist,
        gui_executor_enabled=True,
        adb_path=str(adb),
        adb_serial="127.0.0.1:16384",
    )


def ready_android_target(serial: str = "127.0.0.1:16384") -> Target:
    now = utc_now()
    return Target(
        id=f"adb:{serial}",
        name="fake Android",
        kind=TargetKind.ANDROID,
        status="ready",
        source="adb",
        external_id=serial,
        details={"capabilities": ["android_adb"]},
        discovered_at=now,
        last_seen_at=now,
        updated_at=now,
    )


def test_probe_invokes_exact_get_state_and_only_device_is_ready(tmp_path: Path) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    commands: list[tuple[str, ...]] = []

    def runner(command, timeout):
        assert timeout == 3.0
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")

    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )

    assert executor.probe().status == "ready"
    assert commands == [
        (str(adb.resolve()), "-s", "127.0.0.1:16384", "get-state")
    ]

    stopped = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=lambda command, timeout: subprocess.CompletedProcess(
            command, 0, stdout="offline\n", stderr=""
        ),
    ).probe()
    assert stopped.status == "stopped"
    assert stopped.blocker == {
        "code": "executor_target_not_ready",
        "message": "配置的 Android 目标尚未就绪。",
    }


def test_for_serial_preserves_transport_and_targets_a_discovered_usb_device(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    commands: list[tuple[str, ...]] = []

    def runner(command, _timeout):
        commands.append(tuple(command))
        stdout = "device\n" if command[-1] == "get-state" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    default = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )
    tablet = default.for_serial("R58M1234AB")

    result = tablet.execute(
        GuiAction(target_id="adb:R58M1234AB", action="tap", x=12, y=34)
    )

    assert result.accepted is True
    assert tablet.serial == "R58M1234AB"
    assert commands == [
        (str(adb.resolve()), "-s", "R58M1234AB", "get-state"),
        (str(adb.resolve()), "-s", "R58M1234AB", "shell", "input", "tap", "12", "34"),
    ]


def test_action_uses_argument_array_and_adb_success_means_transport_only(
    tmp_path: Path, monkeypatch
) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    invocations: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        invocations.append((command, kwargs))
        stdout = "device\n" if command[-1] == "get-state" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("ai_game_console.adb_executor.subprocess.run", fake_run)
    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
    )

    result = executor.execute(GuiAction(target_id="adb:fake", action="tap", x=12, y=34))

    assert result.accepted is True
    assert "完成" not in result.detail
    assert [call[0] for call in invocations] == [
        [str(adb.resolve()), "-s", "127.0.0.1:16384", "get-state"],
        [
            str(adb.resolve()),
            "-s",
            "127.0.0.1:16384",
            "shell",
            "input",
            "tap",
            "12",
            "34",
        ],
    ]
    assert all(call[1]["shell"] is False for call in invocations)


def test_swipe_long_press_and_png_screenshot_use_restricted_adb_argument_arrays(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    commands: list[tuple[str, ...]] = []
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\x0dIHDR"
        b"\x00\x00\x04\x38\x00\x00\x07\x80"
    )

    def runner(command, timeout):
        commands.append(tuple(command))
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        if command[-3:] == ("exec-out", "screencap", "-p"):
            return subprocess.CompletedProcess(command, 0, stdout=png, stderr=b"")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )
    executor.execute(
        GuiAction(
            target_id="adb:target",
            action="swipe",
            x=10,
            y=20,
            end_x=30,
            end_y=40,
            duration_ms=500,
        )
    )
    executor.execute(
        GuiAction(target_id="adb:target", action="long_press", x=8, y=9, duration_ms=700)
    )
    frame = executor.capture_screenshot()

    assert frame.width == 1080
    assert frame.height == 1920
    assert parse_png_size(png) == (1080, 1920)
    assert (str(adb.resolve()), "-s", "127.0.0.1:16384", "shell", "input", "swipe", "10", "20", "30", "40", "500") in commands
    assert (str(adb.resolve()), "-s", "127.0.0.1:16384", "shell", "input", "swipe", "8", "9", "8", "9", "700") in commands
    assert (str(adb.resolve()), "-s", "127.0.0.1:16384", "exec-out", "screencap", "-p") in commands


def test_runtime_is_not_configured_by_default_and_ready_needs_probe(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    default_executor = next(
        item
        for item in RuntimeProbe(
            settings,
            adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        ).snapshot()["capabilities"]
        if item.id == "executor"
    )
    assert default_executor.status == "not_configured"

    adb = tmp_path / "adb.exe"
    adb.touch()
    fake = FakeReadyExecutor()
    ready_executor = next(
        item
        for item in RuntimeProbe(
            configured_settings(tmp_path, adb),
            adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
            adb_executor=fake,
        ).snapshot()["capabilities"]
        if item.id == "executor"
    )
    assert ready_executor.status == "ready"
    assert ready_executor.configured is True


def test_executor_action_requires_header_current_android_target_and_never_records_text(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    fake = FakeReadyExecutor()
    app = create_app(settings=configured_settings(tmp_path, adb), adb_executor=fake)
    secret = "private text 42"

    with TestClient(app) as client:
        repository = client.app.state.repository
        repository.replace_adb_targets([ready_android_target()])

        missing_header = client.post(
            "/api/v1/executor/actions",
            json={"target_id": "adb:127.0.0.1:16384", "action": "tap", "x": 1, "y": 2},
        )
        assert missing_header.status_code == 403

        response = client.post(
            "/api/v1/executor/actions",
            headers=WRITE_HEADERS,
            json={
                "target_id": "adb:127.0.0.1:16384",
                "action": "text",
                "text": secret,
            },
        )
        assert response.status_code == 202
        assert response.json()["transport_status"] == "accepted"
        assert secret not in response.text
        assert fake.actions == [
            GuiAction(
                target_id="adb:127.0.0.1:16384",
                action="text",
                text=secret,
            )
        ]
        assert client.get("/api/v1/runs").json()["count"] == 0

        events = client.get("/api/v1/events").json()["items"]
        assert secret not in str(events)
        assert events[0]["type"] == "executor_action_transported"
        assert events[0]["data"] == {
            "target_id": "adb:127.0.0.1:16384",
            "action": "text",
        }


def test_executor_rejects_mismatched_target_and_redacts_invalid_text(tmp_path: Path) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    fake = FakeReadyExecutor()
    app = create_app(settings=configured_settings(tmp_path, adb), adb_executor=fake)
    invalid_text = "this must not be reflected &"

    with TestClient(app) as client:
        client.app.state.repository.replace_adb_targets(
            [ready_android_target("emulator-5554")]
        )
        mismatch = client.post(
            "/api/v1/executor/actions",
            headers=WRITE_HEADERS,
            json={"target_id": "adb:emulator-5554", "action": "tap", "x": 1, "y": 2},
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "executor_target_mismatch"

        invalid = client.post(
            "/api/v1/executor/actions",
            headers=WRITE_HEADERS,
            json={
                "target_id": "adb:emulator-5554",
                "action": "text",
                "text": invalid_text,
            },
        )
        assert invalid.status_code == 422
        assert invalid_text not in invalid.text
        assert invalid.json()["error"]["code"] == "invalid_executor_action"


def test_unicode_text_uses_mumu_cli_single_raw_text_argument_and_redacts_action_repr(
    tmp_path: Path,
) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    mumu_cli = mumu_root / "nx_main" / "mumu-cli.exe"
    mumu_cli.parent.mkdir()
    mumu_cli.touch()
    commands: list[tuple[str, ...]] = []
    entered_text = "仗剑传说"

    def runner(command, timeout):
        commands.append(tuple(command))
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        if command[1:] == ("info", "--vmindex", "all"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"0":{"adb_host_ip":"127.0.0.1","adb_port":16384,'
                    '"index":"0","android_version":"15.0","error_code":0}}'
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout='{"errcode": 0}', stderr="")

    action = GuiAction(target_id="adb:target", action="text", text=entered_text)
    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )

    result = executor.execute(action)

    assert result.accepted is True
    assert entered_text not in result.detail
    assert entered_text not in repr(action)
    assert commands == [
        (str(adb.resolve()), "-s", "127.0.0.1:16384", "get-state"),
        (str(mumu_cli.resolve()), "info", "--vmindex", "all"),
        (
            str(mumu_cli.resolve()),
            "control",
            "--vmindex",
            "0",
            "--version",
            "15",
            "tool",
            "cmd",
            "--cmd",
            "input_text",
            "--text",
            entered_text,
        ),
    ]
    assert commands[-1].count(entered_text) == 1
    assert "shell" not in commands[-1]


def test_ascii_text_remains_restricted_adb_input(tmp_path: Path) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    commands: list[tuple[str, ...]] = []

    def runner(command, timeout):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="device\n" if command[-1] == "get-state" else "",
            stderr="",
        )

    AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    ).execute(GuiAction(target_id="adb:target", action="text", text="game name-42"))

    assert commands == [
        (str(adb.resolve()), "-s", "127.0.0.1:16384", "get-state"),
        (
            str(adb.resolve()),
            "-s",
            "127.0.0.1:16384",
            "shell",
            "input",
            "text",
            "game%sname-42",
        ),
    ]


def test_unicode_text_transport_reports_stable_errors_without_adb_fallback(tmp_path: Path) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    mumu_cli = mumu_root / "nx_main" / "mumu-cli.exe"
    mumu_cli.parent.mkdir()
    mumu_cli.touch()

    def runner(command, timeout):
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        if command[1:] == ("info", "--vmindex", "all"):
            return subprocess.CompletedProcess(command, 0, stdout="not json", stderr="")
        raise AssertionError(f"unexpected transport command: {command!r}")

    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )

    try:
        executor.execute(GuiAction(target_id="adb:target", action="text", text="仗剑传说"))
    except RuntimeError as exc:
        assert str(exc) == "executor_unicode_text_discovery_invalid"
    else:
        raise AssertionError("expected Unicode discovery failure")


def test_public_executor_request_accepts_printable_unicode_and_redacts_invalid_control_text(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "adb.exe"
    adb.touch()
    fake = FakeReadyExecutor()
    app = create_app(settings=configured_settings(tmp_path, adb), adb_executor=fake)
    text = "仗剑传说"
    invalid_text = "do not echo\nthis"

    with TestClient(app) as client:
        client.app.state.repository.replace_adb_targets([ready_android_target()])
        accepted = client.post(
            "/api/v1/executor/actions",
            headers=WRITE_HEADERS,
            json={"target_id": "adb:127.0.0.1:16384", "action": "text", "text": text},
        )
        assert accepted.status_code == 202
        assert fake.actions[-1].text == text

        invalid = client.post(
            "/api/v1/executor/actions",
            headers=WRITE_HEADERS,
            json={
                "target_id": "adb:127.0.0.1:16384",
                "action": "text",
                "text": invalid_text,
            },
        )
        assert invalid.status_code == 422
        assert invalid_text not in invalid.text


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("info_timeout", "executor_unicode_text_discovery_timeout"),
        ("info_nonzero", "executor_unicode_text_discovery_unavailable"),
        ("duplicate_serial", "executor_unicode_text_target_unresolved"),
        ("input_timeout", "executor_unicode_text_uncertain"),
        ("input_nonzero", "executor_unicode_text_uncertain"),
        ("input_errcode", "executor_unicode_text_rejected"),
    ],
)
def test_unicode_text_failure_codes_never_fall_back_to_adb_text(
    tmp_path: Path,
    scenario: str,
    expected_code: str,
) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    mumu_cli = mumu_root / "nx_main" / "mumu-cli.exe"
    mumu_cli.parent.mkdir()
    mumu_cli.touch()
    commands: list[tuple[str, ...]] = []

    def runner(command, timeout):
        commands.append(tuple(command))
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        if command[1:] == ("info", "--vmindex", "all"):
            if scenario == "info_timeout":
                raise subprocess.TimeoutExpired(command, timeout)
            if scenario == "info_nonzero":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="offline")
            if scenario == "duplicate_serial":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '{"0":{"adb_host_ip":"127.0.0.1","adb_port":16384,'
                        '"index":"0","android_version":"15.0"},'
                        '"1":{"adb_host_ip":"127.0.0.1","adb_port":16384,'
                        '"index":"1","android_version":"15.0"}}'
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"0":{"adb_host_ip":"127.0.0.1","adb_port":16384,'
                    '"index":"0","android_version":"15.0"}}'
                ),
                stderr="",
            )
        if scenario == "input_timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        if scenario == "input_nonzero":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="rejected")
        if scenario == "input_errcode":
            return subprocess.CompletedProcess(command, 0, stdout='{"errcode": 7}', stderr="")
        raise AssertionError(f"unexpected scenario command: {command!r}")

    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )

    with pytest.raises(RuntimeError, match=f"^{expected_code}$"):
        executor.execute(GuiAction(target_id="adb:target", action="text", text="仗剑传说"))

    assert not any(command[-3:-1] == ("input", "text") for command in commands)


def test_unicode_text_requires_detected_mumu_cli_before_transport(tmp_path: Path) -> None:
    adb = tmp_path / "nx_device" / "15.0" / "shell" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    commands: list[tuple[str, ...]] = []

    def runner(command, timeout):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")

    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="^executor_unicode_text_cli_unavailable$"):
        executor.execute(GuiAction(target_id="adb:target", action="text", text="仗剑传说"))

    assert commands == [(str(adb.resolve()), "-s", "127.0.0.1:16384", "get-state")]


@pytest.mark.parametrize(
    "stdout",
    ["", "not json", "[]", "{}", '{"errcode": "0"}', '{"error_code": null}'],
)
def test_unicode_text_requires_explicit_zero_result_object_to_be_accepted(
    tmp_path: Path,
    stdout: str,
) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    mumu_cli = mumu_root / "nx_main" / "mumu-cli.exe"
    mumu_cli.parent.mkdir()
    mumu_cli.touch()

    def runner(command, timeout):
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        if command[1:] == ("info", "--vmindex", "all"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"0":{"adb_host_ip":"127.0.0.1","adb_port":16384,'
                    '"index":"0","android_version":"15.0"}}'
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="^executor_unicode_text_uncertain$"):
        executor.execute(GuiAction(target_id="adb:target", action="text", text="仗剑传说"))


def test_unicode_text_timeout_does_not_retain_raw_text_in_exception_chain(tmp_path: Path) -> None:
    mumu_root = tmp_path / "MuMuPlayer"
    adb = mumu_root / "nx_device" / "15.0" / "shell" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    mumu_cli = mumu_root / "nx_main" / "mumu-cli.exe"
    mumu_cli.parent.mkdir()
    mumu_cli.touch()
    sentinel = "不要泄露这段输入"

    def runner(command, timeout):
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        if command[1:] == ("info", "--vmindex", "all"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"0":{"adb_host_ip":"127.0.0.1","adb_port":16384,'
                    '"index":"0","android_version":"15.0"}}'
                ),
                stderr="",
            )
        raise subprocess.TimeoutExpired(command, timeout)

    executor = AdbGuiExecutor(
        enabled=True,
        adb_path=adb,
        serial="127.0.0.1:16384",
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="^executor_unicode_text_uncertain$") as captured:
        executor.execute(GuiAction(target_id="adb:target", action="text", text=sentinel))

    assert captured.value.__cause__ is None
    assert sentinel not in repr(captured.value)
    assert sentinel not in str(captured.value)
