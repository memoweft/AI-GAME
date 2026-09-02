from __future__ import annotations

import subprocess

from ai_game_console.runtime_adapters.android.observation import AndroidObservationProvider


PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR"
    b"\x00\x00\x04\x38\x00\x00\x07\x80"
)


def test_emulator_observation_resolves_canonical_identity_and_is_fresh_without_local_adb() -> None:
    timestamps = iter((
        "2026-08-30T00:00:00+00:00", "2026-08-30T00:00:01+00:00",
        "2026-08-30T00:00:02+00:00", "2026-08-30T00:00:03+00:00",
        "2026-08-30T00:00:04+00:00", "2026-08-30T00:00:05+00:00",
    ))
    commands: list[tuple[str, ...]] = []

    def runner(command, _timeout, binary):
        commands.append(tuple(command))
        if command[-1] == "get-state":
            return subprocess.CompletedProcess(command, 0, stdout="device\n", stderr="")
        if command[-3:] == ("exec-out", "screencap", "-p"):
            return subprocess.CompletedProcess(command, 0, stdout=PNG, stderr=b"")
        if command[-2:] == ("wm", "size"):
            return subprocess.CompletedProcess(command, 0, stdout="Physical size: 1080x1920\n", stderr="")
        if command[-2:] == ("window", "windows"):
            return subprocess.CompletedProcess(command, 0, stdout="mCurrentFocus=Window{ u0 com.android.settings/.Settings}\n", stderr="")
        if command[-2:] == ("dumpsys", "input"):
            return subprocess.CompletedProcess(command, 0, stdout="SurfaceOrientation: 0\n", stderr="")
        if command[-1] == "input_method":
            return subprocess.CompletedProcess(command, 0, stdout="mInputShown=false\n", stderr="")
        if command[-2:] == ("dump", "/dev/tty"):
            return subprocess.CompletedProcess(command, 0, stdout="<?xml version='1.0'?><hierarchy><node/></hierarchy>", stderr="")
        raise AssertionError(command)

    provider = AndroidObservationProvider(
        adb_path="unused-fake-adb.exe",
        runner=runner,
        clock=lambda: next(timestamps),
        transport_device_id_resolver=lambda device_id: "adb:emulator-5554" if device_id == "emulator:profile-a" else device_id,
    )
    observation = provider.capture("emulator:profile-a")

    assert observation.device_id == "emulator:profile-a"
    assert observation.capture_started_at < observation.capture_completed_at
    assert observation.device_state.foreground_app == "com.android.settings"
    assert observation.screenshot.width == 1080
    assert observation.ui_tree.content is not None
    assert all("emulator-5554" in command for command in commands)
