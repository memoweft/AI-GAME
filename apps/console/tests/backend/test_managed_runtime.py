from __future__ import annotations

import io
import json
import socket
import subprocess
from pathlib import Path
import threading
import time

import pytest

import ai_game_console.managed_runtime as managed_runtime
from ai_game_console.config import Settings
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
    _validate_notices,
    verify_managed_runtime,
)


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
    assert token not in repr(frame)
    payload = json.loads(_frame(tmp_path).decode("utf-8"))
    payload["extra"] = True
    with pytest.raises(ManagedProtocolError):
        read_startup_frame(io.BytesIO((json.dumps(payload) + "\n").encode()))
    with pytest.raises(ManagedProtocolError):
        read_startup_frame(io.BytesIO(b"x" * (MAX_STARTUP_FRAME_BYTES + 1)))


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


def test_managed_manifest_closure_detects_tampering_and_excludes_machine_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "apps/console/backend").mkdir(parents=True)
    (source / "apps/console/backend/pyproject.toml").write_text(
        '[project]\nversion = "0.1.0"\n', encoding="utf-8"
    )
    runtime = tmp_path / "runtime-dist"
    runtime.mkdir()
    (runtime / "ai-game-managed-runtime.exe").write_bytes(b"fixture executable")
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


def test_notice_validator_rejects_direct_dependency_and_evidence_mutations(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    notices_path = runtime / "THIRD_PARTY_NOTICES.json"
    notices = [
        {"name": name, "version": "1", "source": {"registry": "https://pypi.org/simple"}, "legal_review": "metadata-only", "evidence": {"frozen_top_level_modules": [name], "locked_runtime_dependency": True}}
        for name in ("fastapi", "uvicorn")
    ]
    notices_path.write_text(json.dumps(notices), encoding="utf-8")
    manifest = {"third_party_notices": notices_path.name, "files": [_file_entry(runtime, notices_path)]}
    for mutate in (
        lambda value: value.pop(0),
        lambda value: value.pop(1),
        lambda value: value.append(dict(value[0])),
        lambda value: value[0].pop("version"),
        lambda value: value[0].pop("evidence"),
        lambda value: value[0].pop("source"),
    ):
        candidate = json.loads(json.dumps(notices))
        mutate(candidate)
        notices_path.write_text(json.dumps(candidate), encoding="utf-8")
        manifest["files"] = [_file_entry(runtime, notices_path)]
        with pytest.raises(ManagedRuntimeBuildError):
            _validate_notices(runtime, manifest, source_root=None)


def test_managed_guard_rejects_writable_root_inside_an_install_root(tmp_path: Path, monkeypatch) -> None:
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(managed_runtime, "immutable_roots_for_runtime", lambda: (install,))
    with pytest.raises(ManagedProtocolError, match="immutable"):
        read_startup_frame(io.BytesIO(_frame(install / "writable")))


def test_manifest_semantic_mutations_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "apps/console/backend").mkdir(parents=True)
    (source / "apps/console/backend/pyproject.toml").write_text(
        '[project]\nversion = "0.1.0"\n', encoding="utf-8"
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    executable = runtime / "ai-game-managed-runtime.exe"
    executable.write_bytes(b"fixture")
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
