from __future__ import annotations

import ast
import hashlib
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from ai_game_console.runtime_adapters.android import AndroidObservationProvider
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_adapters.sqlite import store as sqlite_store_module
from ai_game_console.runtime_kernel import (
    ChannelAvailability,
    ConnectionState,
    ConsistencyStatus,
    DeviceState,
    EventActor,
    KeyboardState,
    ObservationConsistency,
    Orientation,
    RawObservation,
    RawScreenshot,
    RawUiTree,
    RuntimeEventDraft,
    RuntimeKernel,
    StoreConflict,
    TaskSource,
)


TIMES = tuple(f"2026-08-10T02:{minute:02d}:00+00:00" for minute in range(40))
SCREENSHOT = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + (1080).to_bytes(
    4, "big"
) + (2400).to_bytes(4, "big") + b"phase-3-test-pixels"
UI_TREE = b'<?xml version="1.0"?><hierarchy rotation="0"><node text="" /></hierarchy>'


class FakeProvider:
    def __init__(self, result: RawObservation | BaseException) -> None:
        self.result = result
        self.device_ids: list[str] = []

    def capture(self, device_id: str) -> RawObservation:
        self.device_ids.append(device_id)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _source(suffix: str = "1") -> TaskSource:
    return TaskSource(
        client_id=f"client-{suffix}",
        conversation_id=f"conversation-{suffix}",
        initial_message_id=f"message-{suffix}",
    )


def _clock() -> Callable[[], str]:
    values = iter(TIMES * 20)
    return lambda: next(values)


def _raw(
    device_id: str = "adb:device-1",
    *,
    ui_status: ChannelAvailability = ChannelAvailability.AVAILABLE,
    screenshot_status: ChannelAvailability = ChannelAvailability.AVAILABLE,
) -> RawObservation:
    return RawObservation(
        device_id=device_id,
        capture_started_at=TIMES[1],
        capture_completed_at=TIMES[5],
        screenshot=RawScreenshot(
            status=screenshot_status,
            content=SCREENSHOT if screenshot_status is ChannelAvailability.AVAILABLE else None,
            width=1080 if screenshot_status is ChannelAvailability.AVAILABLE else None,
            height=2400 if screenshot_status is ChannelAvailability.AVAILABLE else None,
            captured_at=TIMES[2],
            error_code=(
                None
                if screenshot_status is ChannelAvailability.AVAILABLE
                else "screenshot_failed"
            ),
        ),
        ui_tree=RawUiTree(
            status=ui_status,
            content=UI_TREE if ui_status is ChannelAvailability.AVAILABLE else None,
            captured_at=TIMES[4],
            error_code=(
                None if ui_status is ChannelAvailability.AVAILABLE else "ui_unavailable"
            ),
        ),
        device_state=DeviceState(
            status=ChannelAvailability.AVAILABLE,
            foreground_app="com.example.current",
            screen_size=(1080, 2400),
            orientation=Orientation.PORTRAIT,
            keyboard_state=KeyboardState.HIDDEN,
            connection_state=ConnectionState.CONNECTED,
            captured_at=TIMES[3],
        ),
        consistency=ObservationConsistency(
            status=ConsistencyStatus.CONSISTENT,
            reason=None,
        ),
    )


def _kernel(
    tmp_path: Path,
    raw: RawObservation | BaseException,
) -> tuple[RuntimeKernel, SQLiteRuntimeStore, FilesystemArtifactStore, FakeProvider]:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = FilesystemArtifactStore(tmp_path / "observations")
    provider = FakeProvider(raw)
    kernel = RuntimeKernel(
        store,
        observation_provider=provider,
        artifact_store=artifacts,
        clock=_clock(),
    )
    return kernel, store, artifacts, provider


def _create_task(kernel: RuntimeKernel, task_id: str = "task-1", device_id: str = "adb:device-1"):
    return kernel.create_task(
        task_id=task_id,
        goal=f"Observe {task_id}",
        source=_source(task_id),
        device_id=device_id,
    )


def test_full_observation_persists_and_recovers_with_artifacts(tmp_path: Path) -> None:
    process_a, _, artifacts, _ = _kernel(tmp_path, _raw())
    _create_task(process_a)
    expected = process_a.capture_observation(
        task_id="task-1", device_id="adb:device-1", observation_id="obs-1"
    )
    process_a.close()

    process_b = RuntimeKernel(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    recovered = process_b.load_observation("obs-1")

    assert recovered == expected
    assert process_b.observations("task-1") == (expected,)
    assert process_b.latest_observation("task-1") == expected
    assert process_b.load_task("task-1").last_observation_id == "obs-1"
    assert artifacts.read(recovered.screenshot.artifact) == SCREENSHOT
    assert recovered.ui_tree.artifact is not None
    assert artifacts.read(recovered.ui_tree.artifact) == UI_TREE


def test_ui_tree_unavailable_still_commits_observation(tmp_path: Path) -> None:
    kernel, _, artifacts, _ = _kernel(
        tmp_path, _raw(ui_status=ChannelAvailability.UNAVAILABLE)
    )
    _create_task(kernel)

    observation = kernel.capture_observation(
        task_id="task-1", device_id="adb:device-1", observation_id="obs-no-ui"
    )

    assert observation.ui_tree.status is ChannelAvailability.UNAVAILABLE
    assert observation.ui_tree.artifact is None
    assert artifacts.resolve(observation.screenshot.artifact).is_file()
    assert not (tmp_path / "observations" / "obs-no-ui" / "ui-tree.xml").exists()


def test_screenshot_failure_commits_nothing_and_writes_no_artifact(tmp_path: Path) -> None:
    kernel, _, _, _ = _kernel(
        tmp_path, _raw(screenshot_status=ChannelAvailability.FAILED)
    )
    _create_task(kernel)

    with pytest.raises(RuntimeError, match="screenshot_failed"):
        kernel.capture_observation(
            task_id="task-1", device_id="adb:device-1", observation_id="obs-failed"
        )

    assert kernel.observations("task-1") == ()
    assert [event.type for event in kernel.events("task-1")] == ["TaskCreated"]
    assert kernel.load_task("task-1").last_observation_id is None
    assert not (tmp_path / "observations").exists()


def test_observation_event_sequence_is_continuous_after_reopen(tmp_path: Path) -> None:
    process_a, store, artifacts, provider = _kernel(tmp_path, _raw())
    _create_task(process_a)
    store.append_event(
        "task-1",
        RuntimeEventDraft(
            id="event-stage-started",
            type="StageStarted",
            actor=EventActor.RUNTIME,
            created_at=TIMES[0],
        ),
    )
    process_a.capture_observation(
        task_id="task-1", device_id="adb:device-1", observation_id="obs-1"
    )
    process_a.close()

    process_b = RuntimeKernel(
        SQLiteRuntimeStore(tmp_path / "runtime.db"),
        observation_provider=provider,
        artifact_store=artifacts,
        clock=_clock(),
    )
    process_b.capture_observation(
        task_id="task-1", device_id="adb:device-1", observation_id="obs-2"
    )

    assert [(event.type, event.sequence) for event in process_b.events("task-1")] == [
        ("TaskCreated", 1),
        ("StageStarted", 2),
        ("ObservationReceived", 3),
        ("ObservationReceived", 4),
    ]


def test_observations_are_isolated_by_task(tmp_path: Path) -> None:
    kernel, _, _, provider = _kernel(tmp_path, _raw("adb:device-a"))
    _create_task(kernel, "task-a", "adb:device-a")
    _create_task(kernel, "task-b", "adb:device-b")
    kernel.capture_observation(
        task_id="task-a", device_id="adb:device-a", observation_id="obs-a"
    )
    provider.result = _raw("adb:device-b")
    kernel.capture_observation(
        task_id="task-b", device_id="adb:device-b", observation_id="obs-b"
    )

    assert [item.id for item in kernel.observations("task-a")] == ["obs-a"]
    assert [item.id for item in kernel.observations("task-b")] == ["obs-b"]
    assert [event.sequence for event in kernel.events("task-a")] == [1, 2]
    assert [event.sequence for event in kernel.events("task-b")] == [1, 2]


def test_artifact_reference_has_real_size_and_checksum(tmp_path: Path) -> None:
    kernel, _, artifacts, _ = _kernel(tmp_path, _raw())
    _create_task(kernel)
    observation = kernel.capture_observation(
        task_id="task-1", device_id="adb:device-1", observation_id="obs-integrity"
    )

    path = artifacts.resolve(observation.screenshot.artifact)
    assert path.read_bytes() == SCREENSHOT
    assert observation.screenshot.artifact.size_bytes == len(SCREENSHOT)
    assert observation.screenshot.artifact.sha256 == hashlib.sha256(SCREENSHOT).hexdigest()


def test_artifact_store_never_overwrites_immutable_history(tmp_path: Path) -> None:
    artifacts = FilesystemArtifactStore(tmp_path / "observations")
    original = artifacts.write(
        artifact_id="obs-immutable/screenshot",
        content_type="image/png",
        content=SCREENSHOT,
    )

    with pytest.raises(FileExistsError):
        artifacts.write(
            artifact_id="obs-immutable/screenshot",
            content_type="image/png",
            content=SCREENSHOT + b"changed",
        )

    assert artifacts.read(original) == SCREENSHOT


def test_database_failure_rolls_back_and_cleans_finalized_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, store, _, _ = _kernel(tmp_path, _raw())
    _create_task(kernel)

    def fail_event(*_args, **_kwargs):
        raise sqlite3.IntegrityError("injected observation event failure")

    monkeypatch.setattr(store, "_insert_event", fail_event)
    with pytest.raises(StoreConflict, match="injected observation event failure"):
        kernel.capture_observation(
            task_id="task-1", device_id="adb:device-1", observation_id="obs-db-fail"
        )

    assert store.list_observations("task-1") == ()
    assert [event.type for event in store.list_events("task-1")] == ["TaskCreated"]
    assert store.load_task("task-1").last_observation_id is None
    assert list((tmp_path / "observations").rglob("*")) == []


def test_provider_failure_leaves_task_store_and_artifacts_clean(tmp_path: Path) -> None:
    kernel, _, _, _ = _kernel(tmp_path, RuntimeError("device read failed"))
    expected_task = _create_task(kernel)

    with pytest.raises(RuntimeError, match="device read failed"):
        kernel.capture_observation(
            task_id="task-1", device_id="adb:device-1", observation_id="obs-provider-fail"
        )

    assert kernel.load_task("task-1") == expected_task
    assert kernel.observations("task-1") == ()
    assert not (tmp_path / "observations").exists()


def test_restart_recovers_task_stage_observation_and_events(tmp_path: Path) -> None:
    process_a, _, artifacts, _ = _kernel(tmp_path, _raw())
    _create_task(process_a)
    stage = process_a.create_stage(
        task_id="task-1",
        stage_id="stage-1",
        objective="Observe the current screen",
        completion_criteria=("screenshot captured",),
    )
    active = process_a.start_stage(task_id="task-1", stage_id=stage.id)
    observation = process_a.capture_observation(
        task_id="task-1", device_id="adb:device-1", observation_id="obs-restart"
    )
    expected_task = process_a.load_task("task-1")
    expected_events = process_a.events("task-1")
    process_a.close()

    process_b = RuntimeKernel(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    assert process_b.load_task("task-1") == expected_task
    assert process_b.load_stage("task-1", "stage-1") == active
    assert process_b.load_observation("obs-restart") == observation
    assert process_b.events("task-1") == expected_events
    assert artifacts.read(observation.screenshot.artifact) == SCREENSHOT


def test_phase_2_database_migrates_once_without_losing_facts(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(sqlite_store_module._PHASE_2_SCHEMA)
        connection.execute(
            "INSERT INTO runtime_schema(revision, applied_at) VALUES (1, ?)",
            (TIMES[0],),
        )
        connection.execute(
            """
            INSERT INTO runtime_tasks VALUES (
                'task-old', 1, 'Old goal', 'RUNNING', 'client', 'conversation',
                'message', 'adb:device-1', 'stage-old', NULL, NULL, NULL, NULL,
                ?, ?, NULL
            )
            """,
            (TIMES[0], TIMES[1]),
        )
        connection.execute(
            """
            INSERT INTO runtime_stages VALUES (
                'stage-old', 'task-old', 1, 'Old stage', '["done"]', 'ACTIVE',
                NULL, NULL, ?, NULL, '[]'
            )
            """,
            (TIMES[1],),
        )
        connection.execute(
            """
            INSERT INTO runtime_events VALUES (
                'event-old', 'task-old', 1, 'TaskCreated', 'runtime', '{}',
                NULL, 'task-old', ?, 1
            )
            """,
            (TIMES[0],),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    store = SQLiteRuntimeStore(database_path)
    store.initialize()
    original_task = store.load_task("task-old")
    original_stage = store.load_stage("task-old", "stage-old")
    original_events = store.list_events("task-old")
    artifacts = FilesystemArtifactStore(tmp_path / "observations")
    kernel = RuntimeKernel(
        store,
        observation_provider=FakeProvider(_raw()),
        artifact_store=artifacts,
        clock=_clock(),
    )
    kernel.capture_observation(
        task_id="task-old", device_id="adb:device-1", observation_id="obs-migrated"
    )
    store.close()

    reopened = SQLiteRuntimeStore(database_path)
    reopened.initialize()
    assert reopened.load_task("task-old").id == original_task.id
    assert reopened.load_stage("task-old", "stage-old") == original_stage
    assert reopened.list_events("task-old")[0] == original_events[0]
    assert [event.sequence for event in reopened.list_events("task-old")] == [1, 2]
    assert reopened.load_observation("obs-migrated").id == "obs-migrated"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT MAX(revision) FROM runtime_schema").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM runtime_schema WHERE revision=2").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_android_adapter_uses_only_explicit_read_only_commands() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(
        command: Sequence[str], _timeout: float, binary: bool
    ) -> subprocess.CompletedProcess[str | bytes]:
        normalized = tuple(command)
        commands.append(normalized)
        suffix = normalized[3:]
        if suffix == ("get-state",):
            output: str | bytes = "device\n"
        elif suffix == ("exec-out", "screencap", "-p"):
            assert binary
            output = SCREENSHOT
        elif suffix == ("shell", "wm", "size"):
            output = "Physical size: 1080x2400\n"
        elif suffix == ("shell", "dumpsys", "window", "windows"):
            output = "mCurrentFocus=Window{123 u0 com.example.current/.MainActivity}\n"
        elif suffix == ("shell", "dumpsys", "input"):
            output = "SurfaceOrientation: 0\n"
        elif suffix == ("shell", "dumpsys", "input_method"):
            output = "mInputShown=false\n"
        elif suffix == ("exec-out", "uiautomator", "dump", "/dev/tty"):
            output = UI_TREE.decode() + "\nUI hierchary dumped to: /dev/tty\n"
        else:
            raise AssertionError(f"unexpected command: {normalized}")
        return subprocess.CompletedProcess(normalized, 0, stdout=output, stderr=b"" if binary else "")

    provider = AndroidObservationProvider(
        adb_path="C:/test/adb.exe", runner=runner, clock=_clock()
    )
    observation = provider.capture("adb:serial-123")

    assert observation.device_id == "adb:serial-123"
    assert observation.screenshot.content == SCREENSHOT
    assert observation.ui_tree.status is ChannelAvailability.AVAILABLE
    assert observation.device_state.foreground_app == "com.example.current"
    assert all(command[1:3] == ("-s", "serial-123") for command in commands)
    assert not any(command[3:5] == ("shell", "input") for command in commands)
    assert not any(
        {"tap", "swipe", "keyevent", "am", "monkey"}.intersection(command[3:])
        for command in commands
    )


def test_runtime_kernel_dependency_boundary_includes_observation_code() -> None:
    source_root = Path(__file__).parents[2] / "backend" / "ai_game_console" / "runtime_kernel"
    imported_modules: set[str] = set()
    source_text = ""
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        source_text += text
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    for forbidden_module in ("sqlite3", "subprocess", "fastapi", "pathlib"):
        assert forbidden_module not in imported_modules
    for forbidden_concept in (
        "AdbGuiExecutor",
        "MobileTaskRuntime",
        "ApplicationRuntime",
        "GUI-Owl",
    ):
        assert forbidden_concept not in source_text
