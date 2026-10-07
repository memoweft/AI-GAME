from __future__ import annotations

import io
import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

import ai_game_console.managed_runtime as managed_runtime
import ai_game_console.managed_runtime_builder as managed_runtime_builder
from ai_game_console.config import Settings
from ai_game_console.api import create_app
from ai_game_console.discovery import AdbDiscoveryResult
from ai_game_console.execution_contract import ExecutionContractError, V2ExecutionContractService
from ai_game_console.managed_runtime import (
    MAX_STARTUP_FRAME_BYTES,
    ManagedDataRootLock,
    ManagedPathGuard,
    ManagedProtocolError,
    read_startup_frame,
    run_managed,
)
from ai_game_console.managed_runtime_builder import (
    MANAGED_MANIFEST_NAME,
    ManagedRuntimeBuildError,
    _scrub_machine_build_metadata,
    _write_manifest,
    _file_entry,
    verify_managed_runtime,
)


_MANAGED_NOTICE_MODULES = {
    "annotated-doc": "annotated_doc",
    "annotated-types": "annotated_types",
    "anyio": "anyio",
    "click": "click",
    "colorama": "colorama",
    "fastapi": "fastapi",
    "h11": "h11",
    "idna": "idna",
    "pydantic": "pydantic",
    "pydantic-core": "pydantic_core",
    "starlette": "starlette",
    "typing-extensions": "typing_extensions",
    "typing-inspection": "typing_inspection",
    "uvicorn": "uvicorn",
}


def _frame(root: Path, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "protocol_version": 1,
        "nonce": "nonce-for-managed-runtime-0001",
        "host": "127.0.0.1",
        "port": 4311,
        "writable_root": str(root),
        "data_dir": str(root / "data"),
        "runtime_mode": "weftmate-managed-v1",
        "capability": "c" * 32,
        "shutdown_token": "s" * 32,
    }
    payload.update(overrides)
    return (json.dumps(payload) + "\n").encode("utf-8")


@pytest.mark.parametrize("changes", [
    {"host": "0.0.0.0"}, {"port": 80}, {"runtime_mode": "legacy"},
    {"writable_root": r"\\server\share"}, {"data_dir": "relative/data"},
])
def test_managed_startup_rejects_non_local_or_unsafe_frame(tmp_path: Path, changes: dict[str, object]) -> None:
    with pytest.raises(ManagedProtocolError):
        read_startup_frame(io.BytesIO(_frame(tmp_path, **changes)))


def test_managed_startup_requires_a_finite_exact_schema_and_hides_secrets(tmp_path: Path) -> None:
    token = "secret-capability-" + "x" * 32
    frame = read_startup_frame(io.BytesIO(_frame(tmp_path, capability=token)))
    assert frame.data_dir == (tmp_path / "data").resolve()
    assert frame.execution is None
    assert token not in repr(frame)
    payload = json.loads(_frame(tmp_path).decode("utf-8"))
    payload["extra"] = True
    with pytest.raises(ManagedProtocolError):
        read_startup_frame(io.BytesIO((json.dumps(payload) + "\n").encode()))
    with pytest.raises(ManagedProtocolError):
        read_startup_frame(io.BytesIO(b"x" * (MAX_STARTUP_FRAME_BYTES + 1)))


@pytest.mark.parametrize("execution", ["absent", None, {"enabled": False}])
def test_managed_execution_disabled_or_null_preserves_needs_setup_settings(
    tmp_path: Path, execution: object,
) -> None:
    encoded = _frame(tmp_path) if execution == "absent" else _frame(
        tmp_path, execution=execution
    )
    frame = read_startup_frame(io.BytesIO(encoded))
    settings = managed_runtime.build_managed_settings(frame)

    assert settings.managed_runtime is True
    assert settings.gui_executor_enabled is False
    assert settings.adb_path is None
    assert settings.adb_serial is None
    assert settings.mobile_role_endpoint is None
    assert settings.mobile_role_model is None
    assert settings.mobile_role_api_key is None

    app = create_app(
        settings=settings,
        adb_discovery=SimpleNamespace(
            discover=lambda: AdbDiscoveryResult("not_configured", None, "fixture", (), ())
        ),
        long_task_scheduler=SimpleNamespace(poll_once=lambda: 0),
    )
    health = app.state.execution_contract_v2.health()
    assert health["status"] == "needs_setup"
    assert health["capabilities"]["android_ui_agent"]["available"] is False
    assert health["setup_reasons"] == ["managed_executor_verification_required"]


def test_managed_execution_enabled_maps_only_memory_settings_and_makes_v2_ready(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"fixture")
    api_key = "model-secret-that-must-remain-in-memory"
    frame = read_startup_frame(io.BytesIO(_frame(tmp_path, execution={
        "enabled": True,
        "adb_path": str(adb),
        "adb_serial": "127.0.0.1:16384",
        "model": {
            "endpoint": "http://127.0.0.1:18080/v1",
            "name": "qwen3.8-27b",
            "api_key": api_key,
        },
    })))
    settings = managed_runtime.build_managed_settings(frame)

    assert settings.gui_executor_enabled is True
    assert settings.adb_path == str(adb.resolve())
    assert settings.adb_serial == "127.0.0.1:16384"
    assert settings.mobile_role_endpoint == "http://127.0.0.1:18080/v1"
    assert settings.mobile_role_model == "qwen3.8-27b"
    assert settings.mobile_role_api_key == api_key
    assert api_key not in repr(frame)
    assert api_key not in repr(settings)

    app = create_app(
        settings=settings,
        adb_discovery=SimpleNamespace(
            discover=lambda: AdbDiscoveryResult("ready", str(adb), "fixture", (), ())
        ),
        long_task_scheduler=SimpleNamespace(poll_once=lambda: 0),
    )
    health = app.state.execution_contract_v2.health()
    assert health["status"] == "ready"
    assert health["capabilities"]["android_ui_agent"] == {
        "state": "ready", "available": True, "version": "1",
    }
    assert health["setup_reasons"] == []


@pytest.mark.parametrize("mutation", [
    {"enabled": False, "model": {}},
    {"enabled": True, "adb_path": "missing-adb.exe", "model": {
        "endpoint": "http://127.0.0.1:18080/v1", "name": "qwen",
    }},
    {"enabled": True, "adb_path": "{adb}", "adb_serial": "127.0.0.1:70000", "model": {
        "endpoint": "http://127.0.0.1:18080/v1", "name": "qwen",
    }},
    {"enabled": True, "adb_path": "{adb}", "model": {
        "endpoint": "http://example.com:18080/v1", "name": "qwen",
    }},
    {"enabled": True, "adb_path": "{adb}", "model": {
        "endpoint": "http://127.0.0.1:18080/v1", "name": "",
    }},
])
def test_managed_execution_invalid_shapes_fail_closed(
    tmp_path: Path, mutation: dict[str, object],
) -> None:
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"fixture")
    serialized = json.loads(json.dumps(mutation))
    if serialized.get("adb_path") == "{adb}":
        serialized["adb_path"] = str(adb)

    with pytest.raises(ManagedProtocolError):
        read_startup_frame(io.BytesIO(_frame(tmp_path, execution=serialized)))


def test_managed_startup_rejects_data_dir_outside_root_and_reparse_ancestors(tmp_path: Path) -> None:
    with pytest.raises(ManagedProtocolError):
        read_startup_frame(io.BytesIO(_frame(tmp_path, data_dir=str(tmp_path.parent / "outside"))))
    linked = tmp_path / "linked"
    target = tmp_path / "target"
    target.mkdir()
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows test host")
    with pytest.raises(ManagedProtocolError):
        read_startup_frame(io.BytesIO(_frame(linked)))


def test_managed_data_root_lock_refuses_a_second_instance_and_releases(tmp_path: Path) -> None:
    first = ManagedDataRootLock(tmp_path / "data")
    second = ManagedDataRootLock(tmp_path / "data")
    first.acquire()
    try:
        with pytest.raises(ManagedProtocolError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
    second.release()


class _LivenessPipe:
    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.closed = threading.Event()

    def readline(self, _: int) -> bytes:
        value, self.frame = self.frame, b""
        return value

    def read(self, _: int) -> bytes:
        self.closed.wait(timeout=10)
        return b""


def _free_loopback_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


def _notice_records() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "version": "1.0",
            "license_expression": "MIT",
            "license_metadata": {"license": None, "license_expression": "MIT"},
            "source": {"registry": "https://pypi.org/simple"},
            "legal_review": "metadata-only",
            "evidence": {
                "frozen_top_level_modules": [module],
                "locked_runtime_dependency": True,
            },
        }
        for name, module in _MANAGED_NOTICE_MODULES.items()
    ]


def _notice_record(notices: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(item for item in notices if item["name"] == name)


def _write_notice_fixture(runtime: Path, notices: list[dict[str, object]]) -> None:
    runtime.mkdir()
    executable = runtime / "ai-game-managed-runtime.exe"
    executable.write_bytes(b"fixture executable")
    notices_path = runtime / "THIRD_PARTY_NOTICES.json"
    notices_path.write_text(json.dumps(notices, sort_keys=True), encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "artifact_kind": "windows_x64_self_contained_managed_runtime",
        "source": {"name": "fixture", "version": "1", "revision": "unavailable"},
        "managed_protocol_version": 1,
        "execution_api_version": "2.0",
        "platform": "windows",
        "architecture": "x64",
        "entry": executable.name,
        "third_party_notices": notices_path.name,
        "files": sorted(
            [_file_entry(runtime, executable), _file_entry(runtime, notices_path)],
            key=lambda item: item["path"],
        ),
    }
    (runtime / MANAGED_MANIFEST_NAME).write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )


def _rewrite_notices_and_manifest(runtime: Path, notices: list[dict[str, object]]) -> None:
    notices_path = runtime / "THIRD_PARTY_NOTICES.json"
    notices_path.write_text(json.dumps(notices, sort_keys=True), encoding="utf-8")
    manifest_path = runtime / MANAGED_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [
        _file_entry(runtime, notices_path) if item["path"] == notices_path.name else item
        for item in manifest["files"]
    ]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def test_managed_ready_waits_for_listen_and_eof_stops_idempotently(tmp_path: Path) -> None:
    port = _free_loopback_port()
    secret = "private-managed-token-" + "x" * 32
    pipe = _LivenessPipe(_frame(tmp_path, port=port, capability=secret))
    stdout, stderr, result = io.StringIO(), io.StringIO(), []
    thread = threading.Thread(target=lambda: result.append(
        run_managed(stdin=pipe, stdout=stdout, stderr=stderr)
    ))
    thread.start()
    deadline = time.monotonic() + 10
    while "ai_game_managed_ready" not in stdout.getvalue() and time.monotonic() < deadline:
        time.sleep(0.02)
    ready = json.loads(stdout.getvalue())
    assert ready["nonce"] == "nonce-for-managed-runtime-0001"
    assert ready["port"] == port
    assert secret not in stdout.getvalue() + stderr.getvalue()
    pipe.closed.set()
    pipe.closed.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result == [0]


def test_managed_port_conflict_returns_no_ready_frame(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    pipe = _LivenessPipe(_frame(tmp_path, port=port))
    stdout, stderr, result = io.StringIO(), io.StringIO(), []
    thread = threading.Thread(target=lambda: result.append(
        run_managed(stdin=pipe, stdout=stdout, stderr=stderr)
    ))
    try:
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert result == [2]
        assert stdout.getvalue() == ""
    finally:
        pipe.closed.set()
        listener.close()


def test_unready_v2_health_is_honest_and_new_execution_has_no_side_effect() -> None:
    class _Store:
        def v2_runner_admission(self, **_: object) -> None:
            return None

    service = V2ExecutionContractService(
        _Store(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        runner_ready=False,
        runner_setup_reasons=("managed_executor_verification_required",),
    )
    health = service.health()
    assert health["status"] == "needs_setup"
    assert health["capabilities"]["android_ui_agent"] == {
        "state": "needs_setup", "available": False, "version": "1",
    }
    with pytest.raises(ExecutionContractError, match="CAPABILITY_UNAVAILABLE"):
        service.create_task({}, auth_context={})
    assert service.runner_admission("task", principal_id="p", controller_id="c") is None


def test_managed_manifest_closure_detects_tampering_and_excludes_machine_paths(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    (source / "apps/console/backend").mkdir(parents=True)
    (source / "apps/console/backend/pyproject.toml").write_text(
        '[project]\nversion = "0.1.0"\n', encoding="utf-8"
    )
    runtime = tmp_path / "runtime-dist"
    runtime.mkdir()
    (runtime / "ai-game-managed-runtime.exe").write_bytes(b"fixture executable")
    monkeypatch.setattr(managed_runtime_builder, "_third_party_notices", lambda *_: _notice_records())
    monkeypatch.setattr(managed_runtime_builder, "_frozen_modules", lambda _: set(_MANAGED_NOTICE_MODULES.values()))
    _write_manifest(runtime, source)
    assert (runtime / MANAGED_MANIFEST_NAME).is_file()
    assert verify_managed_runtime(runtime, source_root=source)["verified"] is True
    assert str(source).encode() not in (runtime / MANAGED_MANIFEST_NAME).read_bytes()
    (runtime / "ai-game-managed-runtime.exe").write_bytes(b"tampered")
    with pytest.raises(ManagedRuntimeBuildError, match="hash"):
        verify_managed_runtime(runtime, source_root=source)


def test_managed_builder_removes_editable_install_machine_metadata(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-dist"
    metadata = runtime / "_internal" / "demo.dist-info"
    metadata.mkdir(parents=True)
    marker = metadata / "direct_url.json"
    marker.write_text('{"url":"file:///D:/machine/source"}', encoding="utf-8")
    (metadata / "METADATA").write_text("Name: demo\n", encoding="utf-8")
    _scrub_machine_build_metadata(runtime)
    assert not marker.exists()
    assert (metadata / "METADATA").is_file()


def test_managed_guard_rejects_preexisting_logs_junction(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    guard = ManagedPathGuard(writable_root=root, data_dir=root / "data", immutable_roots=())
    guard.prepare()
    outside = tmp_path / "outside"
    outside.mkdir()
    logs = root / "data" / "logs"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(logs), str(outside)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        pytest.skip("junction creation is unavailable on this Windows test host")
    with pytest.raises(ManagedProtocolError, match="reparse"):
        guard.file("logs/runtime-mode.jsonl")
    logs.rmdir()


def test_managed_guard_rejects_lock_and_sqlite_junction_escape(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    guard = ManagedPathGuard(writable_root=root, data_dir=root / "data", immutable_roots=())
    guard.prepare()
    outside = tmp_path / "outside"
    outside.mkdir()
    lock = root / "data" / ".ai-game-managed.lock"
    database = root / "data" / "console.db"
    for candidate in (lock, database):
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(candidate), str(outside)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            pytest.skip("junction creation is unavailable on this Windows test host")
    with pytest.raises(ManagedProtocolError, match="reparse"):
        guard.file(".ai-game-managed.lock")
    with pytest.raises(ManagedProtocolError, match="reparse"):
        guard.file("console.db")
    lock.rmdir()
    database.rmdir()


def test_managed_guard_rejects_nested_runtime_and_local_application_junctions(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    guard = ManagedPathGuard(writable_root=root, data_dir=root / "data", immutable_roots=())
    guard.prepare()
    outside = tmp_path / "outside"
    outside.mkdir()
    targets = (
        ("emulator-runtime/runtime", "directory"),
        ("emulator-runtime/runtime/artifacts", "directory"),
        ("emulator-runtime/emulator-profiles.db", "file"),
        ("local-managed-application-runtime.db", "file"),
    )
    for relative, kind in targets:
        candidate = root / "data" / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(candidate), str(outside)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            pytest.skip("junction creation is unavailable on this Windows test host")
        with pytest.raises(ManagedProtocolError, match="reparse"):
            getattr(guard, kind)(relative)
        candidate.rmdir()


def test_notice_consumer_verifier_accepts_complete_distribution_closure(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    _write_notice_fixture(runtime, _notice_records())
    monkeypatch.setattr(
        managed_runtime_builder, "_frozen_modules", lambda _: set(_MANAGED_NOTICE_MODULES.values())
    )
    assert verify_managed_runtime(runtime)["verified"] is True


def test_notice_consumer_verifier_rejects_closure_consistent_mutations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        managed_runtime_builder, "_frozen_modules", lambda _: set(_MANAGED_NOTICE_MODULES.values())
    )

    def remove_distribution(name: str):
        return lambda notices: notices.__setitem__(
            slice(None), [item for item in notices if item["name"] != name]
        )

    mutations = (
        ("empty-distributions", lambda notices: notices.clear()),
        ("missing-fastapi", remove_distribution("fastapi")),
        ("missing-uvicorn", remove_distribution("uvicorn")),
        ("duplicate", lambda notices: notices.append(json.loads(json.dumps(notices[0])))),
        ("missing-version", lambda notices: _notice_record(notices, "fastapi").pop("version")),
        ("missing-evidence", lambda notices: _notice_record(notices, "fastapi").pop("evidence")),
        ("missing-source", lambda notices: _notice_record(notices, "fastapi").pop("source")),
        (
            "missing-license-fields",
            lambda notices: [
                _notice_record(notices, "fastapi").pop(field)
                for field in ("license_expression", "license_metadata", "legal_review")
            ],
        ),
        (
            "empty-license-provenance",
            lambda notices: _notice_record(notices, "fastapi").update(
                {"license_expression": None, "license_metadata": {}, "legal_review": ""}
            ),
        ),
        (
            "whitespace-license-provenance",
            lambda notices: _notice_record(notices, "fastapi").update(
                {"license_expression": None, "license_metadata": {}, "legal_review": "   \t"}
            ),
        ),
        (
            "missing-frozen-module",
            lambda notices: _notice_record(notices, "fastapi")["evidence"].update(
                {"frozen_top_level_modules": ["definitely_missing"]}
            ),
        ),
        (
            "misattributed-frozen-module",
            lambda notices: _notice_record(notices, "fastapi")["evidence"].update(
                {"frozen_top_level_modules": ["uvicorn"]}
            ),
        ),
    )
    for case, mutate in mutations:
        runtime = tmp_path / case
        notices = _notice_records()
        _write_notice_fixture(runtime, notices)
        assert verify_managed_runtime(runtime)["verified"] is True
        mutate(notices)
        _rewrite_notices_and_manifest(runtime, notices)
        with pytest.raises(ManagedRuntimeBuildError) as raised:
            verify_managed_runtime(runtime)
        assert "hash" not in str(raised.value).casefold()


def test_managed_guard_rejects_writable_root_inside_an_install_root(tmp_path: Path, monkeypatch) -> None:
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(managed_runtime, "immutable_roots_for_runtime", lambda: (install,))
    with pytest.raises(ManagedProtocolError, match="immutable"):
        read_startup_frame(io.BytesIO(_frame(install / "writable")))


def test_manifest_semantic_mutations_are_rejected(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    (source / "apps/console/backend").mkdir(parents=True)
    (source / "apps/console/backend/pyproject.toml").write_text(
        '[project]\nversion = "0.1.0"\n', encoding="utf-8"
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    executable = runtime / "ai-game-managed-runtime.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(managed_runtime_builder, "_third_party_notices", lambda *_: _notice_records())
    monkeypatch.setattr(managed_runtime_builder, "_frozen_modules", lambda _: set(_MANAGED_NOTICE_MODULES.values()))
    _write_manifest(runtime, source)
    manifest_path = runtime / MANAGED_MANIFEST_NAME

    def mutate(change) -> None:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        change(payload)
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ManagedRuntimeBuildError):
            verify_managed_runtime(runtime)
        _write_manifest(runtime, source)

    mutate(lambda payload: payload.__setitem__("entry", "missing-entry.exe"))
    mutate(lambda payload: payload.pop("source"))
    mutate(lambda payload: payload.__setitem__("third_party_notices", "missing.json"))
    mutate(lambda payload: payload["files"].append(dict(payload["files"][0])))

    extra = runtime / "machine-path.txt"
    extra.write_bytes(b"file:///D:/build-machine/private")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"].append(_file_entry(runtime, extra))
    payload["files"].sort(key=lambda item: item["path"])
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManagedRuntimeBuildError, match="absolute path"):
        verify_managed_runtime(runtime)
