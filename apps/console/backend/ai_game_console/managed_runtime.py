"""The stdin-only runtime contract used by a future WeftMate supervisor.

This module deliberately has no command-line configuration.  Secrets arrive in
one bounded pipe frame and remain process memory only; stdout carries only a
small, nonce-bound readiness record and ordinary diagnostics go to stderr.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import BinaryIO, TextIO

import uvicorn

from .api import create_app
from .config import Settings


MANAGED_PROTOCOL_VERSION = 1
MAX_STARTUP_FRAME_BYTES = 16 * 1024
_NONCE = re.compile(r"^[A-Za-z0-9._-]{16,256}$")
_REPARSE_POINT = 0x0400


class ManagedProtocolError(ValueError):
    """A deliberately non-echoing startup-frame error."""


@dataclass(frozen=True, slots=True)
class ManagedStartupFrame:
    nonce: str
    host: str
    port: int
    writable_root: Path
    data_dir: Path
    runtime_mode: str
    capability: str = field(repr=False)
    shutdown_token: str = field(repr=False)


class ManagedPathGuard:
    """Create and recheck the finite managed writable surface.

    This is intentionally a containment fence, not a claim that user-mode
    pathname checks eliminate every Windows filesystem TOCTOU race.  It
    validates immediately before and after every root/file creation owned by
    the current managed composition and rejects reparse points at either end.
    """

    def __init__(self, *, writable_root: Path, data_dir: Path, immutable_roots: tuple[Path, ...]) -> None:
        self.writable_root = writable_root
        self.data_dir = data_dir
        self.immutable_roots = immutable_roots

    def prepare(self) -> None:
        self._create_directory(self.writable_root, parent_limit=None)
        self._create_directory(self.data_dir, parent_limit=self.writable_root)

    def directory(self, relative: str) -> Path:
        path = self._relative(relative)
        self._create_directory(path, parent_limit=self.data_dir)
        return self._assert_confined(path, require_exists=True)

    def file(self, relative: str) -> Path:
        path = self._relative(relative)
        self._create_directory(path.parent, parent_limit=self.data_dir)
        self._assert_confined(path.parent, require_exists=True)
        try:
            with path.open("xb"):
                pass
        except FileExistsError:
            pass
        except OSError as error:
            raise ManagedProtocolError("managed writable path is unavailable") from error
        return self._assert_confined(path, require_exists=True)

    def _relative(self, relative: str) -> Path:
        candidate = Path(relative)
        if not relative or candidate.is_absolute() or ".." in candidate.parts or "\\" in relative:
            raise ManagedProtocolError("managed writable path is invalid")
        return self.data_dir.joinpath(*candidate.parts)

    def _create_directory(self, path: Path, *, parent_limit: Path | None) -> None:
        if parent_limit is not None:
            self._assert_beneath(path, parent_limit)
        if path.exists():
            self._assert_confined(path, require_exists=True)
            return
        # Make one directory component at a time, rejecting a junction that
        # appeared between any check and creation before proceeding deeper.
        missing: list[Path] = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            if cursor.parent == cursor:
                raise ManagedProtocolError("managed writable path is unavailable")
            cursor = cursor.parent
        if parent_limit is None:
            _reject_reparse_ancestors(cursor)
        else:
            self._assert_confined(cursor, require_exists=True)
        for component in reversed(missing):
            try:
                component.mkdir()
            except FileExistsError:
                pass
            except OSError as error:
                raise ManagedProtocolError("managed writable path is unavailable") from error
            self._assert_confined(component, require_exists=True)

    def _assert_confined(self, path: Path, *, require_exists: bool) -> Path:
        self._assert_beneath(path, self.writable_root)
        for immutable in self.immutable_roots:
            if path.is_relative_to(immutable) or immutable.is_relative_to(path):
                raise ManagedProtocolError("managed path overlaps immutable resources")
        if require_exists and not path.exists():
            raise ManagedProtocolError("managed writable path is unavailable")
        _reject_reparse_ancestors(path)
        return path

    @staticmethod
    def _assert_beneath(path: Path, parent: Path) -> None:
        if path != parent and not path.is_relative_to(parent):
            raise ManagedProtocolError("managed path escapes writable root")


class ManagedDataRootLock:
    """One Windows process per managed data root, released by the OS on exit."""

    def __init__(self, data_dir: Path, *, guard: ManagedPathGuard | None = None) -> None:
        self.path = guard.file(".ai-game-managed.lock") if guard is not None else data_dir / ".ai-game-managed.lock"
        self.guard = guard
        self._handle: io.BufferedRandom | None = None

    def acquire(self) -> None:
        # msvcrt is intentionally imported only on the Windows execution path.
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after creation so a caller cannot swap a missing ancestor for
        # a junction between startup-frame validation and opening the lock.
        _reject_reparse_ancestors(self.path.parent)
        if self.guard is not None:
            self.path = self.guard.file(".ai-game-managed.lock")
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            handle.close()
            raise ManagedProtocolError("managed data directory is already in use") from error
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None


def read_startup_frame(stream: BinaryIO) -> ManagedStartupFrame:
    """Read exactly one finite JSON-line startup frame without retaining bytes."""

    raw = bytearray(stream.readline(MAX_STARTUP_FRAME_BYTES + 1))
    try:
        if not raw or len(raw) > MAX_STARTUP_FRAME_BYTES or not raw.endswith(b"\n"):
            raise ManagedProtocolError("managed startup frame is invalid")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManagedProtocolError("managed startup frame is invalid") from error
    finally:
        # The raw pipe buffer can contain credentials; do not leave a second
        # mutable copy around after parsing.
        raw[:] = b"\x00" * len(raw)
    if not isinstance(payload, dict):
        raise ManagedProtocolError("managed startup frame is invalid")
    required = {
        "protocol_version", "nonce", "host", "port", "writable_root", "data_dir",
        "runtime_mode", "capability", "shutdown_token",
    }
    if set(payload) != required or payload.get("protocol_version") != MANAGED_PROTOCOL_VERSION:
        raise ManagedProtocolError("managed startup frame schema is unsupported")
    nonce = payload["nonce"]
    host = payload["host"]
    port = payload["port"]
    runtime_mode = payload["runtime_mode"]
    capability = payload["capability"]
    shutdown_token = payload["shutdown_token"]
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise ManagedProtocolError("managed startup frame is invalid")
    if host != "127.0.0.1" or isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ManagedProtocolError("managed listener is invalid")
    if runtime_mode != "weftmate-managed-v1":
        raise ManagedProtocolError("managed runtime mode is unsupported")
    if not _secret_shape(capability) or not _secret_shape(shutdown_token):
        raise ManagedProtocolError("managed credential is invalid")
    immutable_roots = immutable_roots_for_runtime()
    writable_root = _managed_path(payload["writable_root"], immutable_roots)
    data_dir = _managed_path(payload["data_dir"], immutable_roots)
    if data_dir == writable_root or not data_dir.is_relative_to(writable_root):
        raise ManagedProtocolError("managed data directory must be inside writable root")
    return ManagedStartupFrame(
        nonce=nonce, host=host, port=port, writable_root=writable_root, data_dir=data_dir,
        runtime_mode=runtime_mode, capability=capability, shutdown_token=shutdown_token,
    )


def resource_root_for_runtime() -> Path:
    """Locate immutable packaged resources without accepting a caller path."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str) and frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parents[4]


def immutable_roots_for_runtime() -> tuple[Path, ...]:
    """Protect both PyInstaller internals and the complete distribution root."""

    resource_root = resource_root_for_runtime()
    roots = {resource_root}
    if getattr(sys, "frozen", False):
        roots.add(Path(sys.executable).resolve().parent)
    return tuple(sorted(roots, key=lambda item: str(item).lower()))


def build_managed_settings(frame: ManagedStartupFrame) -> Settings:
    root = resource_root_for_runtime()
    frontend = root / "frontend"
    if not frontend.is_dir():
        frontend = root / "apps" / "console" / "frontend" / "dist"
    guard = ManagedPathGuard(
        writable_root=frame.writable_root,
        data_dir=frame.data_dir,
        immutable_roots=immutable_roots_for_runtime(),
    )
    guard.prepare()
    return Settings(
        project_root=root,
        data_dir=frame.data_dir,
        database_path=frame.data_dir / "console.db",
        frontend_dist=frontend,
        console_shutdown_token=frame.shutdown_token,
        harness_api_token=frame.capability,
        runtime_mode="kernel_active",
        managed_runtime=True,
        managed_path_guard=guard,
    )


def run_managed(
    *, stdin: BinaryIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None,
) -> int:
    # ``BufferedReader.readline`` may prefetch a second protocol byte.  The
    # real managed entry therefore operates on the raw pipe from the outset.
    input_stream = stdin or sys.stdin.buffer.raw
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    lock: ManagedDataRootLock | None = None
    watcher: _ParentPipeWatcher | None = None
    try:
        frame = read_startup_frame(input_stream)
        settings = build_managed_settings(frame)
        lock = ManagedDataRootLock(frame.data_dir, guard=settings.managed_path_guard)
        lock.acquire()
        server_box: dict[str, uvicorn.Server] = {}

        def request_shutdown() -> None:
            server = server_box.get("server")
            if server is not None:
                server.should_exit = True

        app = create_app(settings=settings, console_shutdown_callback=request_shutdown)
        server = uvicorn.Server(uvicorn.Config(
            app, host=frame.host, port=frame.port, reload=False, access_log=False,
            log_config=None, lifespan="on",
        ))
        server_box["server"] = server
        stopped = threading.Event()
        watcher = _ParentPipeWatcher(input_stream, request_shutdown)
        # Bytes already sent after the startup frame are a protocol close, not
        # a valid ready state.  Probe before Uvicorn starts so a supervisor
        # never sees a stable ready frame for a malformed/closed channel.
        if watcher.has_pending_input():
            raise ManagedProtocolError("managed parent channel closed during startup")
        watcher.start()
        ready_thread = threading.Thread(
            target=_emit_ready_when_listening,
            args=(server, frame.nonce, frame.host, frame.port, output_stream, stopped, watcher.closed),
            daemon=True,
        )
        ready_thread.start()
        try:
            server.run()
        finally:
            stopped.set()
        return 0
    except ManagedProtocolError as error:
        print(f"AI-GAME managed runtime refused startup: {error}", file=error_stream, flush=True)
        return 2
    except (Exception, SystemExit):
        # Do not serialize arbitrary exceptions: their context may contain a
        # startup credential or filesystem path supplied by the host.
        print("AI-GAME managed runtime failed to start.", file=error_stream, flush=True)
        return 2
    finally:
        if watcher is not None:
            watcher.cancel_and_join()
        if lock is not None:
            lock.release()


def _emit_ready_when_listening(
    server: uvicorn.Server, nonce: str, host: str, port: int, stream: TextIO, stopped: threading.Event,
    parent_closed: threading.Event,
) -> None:
    while not stopped.is_set():
        if parent_closed.is_set():
            return
        if server.started:
            # Recheck immediately before writing: an EOF/extra byte racing a
            # completed listen must win over a potentially usable ready frame.
            if parent_closed.is_set():
                return
            payload = {
                "type": "ai_game_managed_ready",
                "protocol_version": MANAGED_PROTOCOL_VERSION,
                "nonce": nonce,
                "host": host,
                "port": port,
            }
            stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            return
        time.sleep(0.01)


class _ParentPipeWatcher:
    """A cancellable raw Windows pipe read; never a daemon BufferedReader."""

    def __init__(self, stream: BinaryIO, request_shutdown: Callable[[], None]) -> None:
        self.stream = stream
        self.request_shutdown = request_shutdown
        self.closed = threading.Event()
        self.cancelled = threading.Event()
        try:
            self.descriptor = int(stream.fileno())
        except (AttributeError, OSError, io.UnsupportedOperation):
            self.descriptor = None
        self.thread = threading.Thread(
            target=self._run,
            name="ai-game-parent-pipe",
            # Test-only in-memory streams lack a cancellable Windows handle.
            # Real managed stdin always has a descriptor and is non-daemon.
            daemon=self.descriptor is None,
        )

    def has_pending_input(self) -> bool:
        try:
            import ctypes
            import msvcrt

            available = ctypes.c_uint32()
            handle = msvcrt.get_osfhandle(self.stream.fileno())
            ok = ctypes.windll.kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
            return bool(ok and available.value)
        except (AttributeError, OSError, io.UnsupportedOperation):
            return False

    def start(self) -> None:
        self.thread.start()

    def cancel_and_join(self) -> None:
        self.cancelled.set()
        if self.thread.is_alive():
            self._cancel_synchronous_read()
            self.thread.join(timeout=5)
        if self.thread.is_alive() and self.descriptor is not None:
            raise RuntimeError("managed parent pipe watcher did not stop")

    def _run(self) -> None:
        try:
            if self.descriptor is None:
                self.stream.read(1)
            else:
                os.read(self.descriptor, 1)
        except OSError:
            pass
        finally:
            if not self.cancelled.is_set():
                self.closed.set()
                self.request_shutdown()

    def _cancel_synchronous_read(self) -> None:
        try:
            import ctypes

            # CancelSynchronousIo requires THREAD_TERMINATE access on the
            # target thread handle (not THREAD_SET_CONTEXT).
            thread_handle = ctypes.windll.kernel32.OpenThread(0x0001, False, self.thread.native_id)
            if thread_handle:
                try:
                    ctypes.windll.kernel32.CancelSynchronousIo(thread_handle)
                finally:
                    ctypes.windll.kernel32.CloseHandle(thread_handle)
        except (AttributeError, OSError):
            # The following join is the integrity fence: do not proceed to
            # interpreter teardown while a non-daemon reader owns stdin.
            pass


def _secret_shape(value: object) -> bool:
    return isinstance(value, str) and 32 <= len(value) <= 512 and "\x00" not in value


def _managed_path(value: object, immutable_roots: tuple[Path, ...]) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ManagedProtocolError("managed path is invalid")
    if value.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
        raise ManagedProtocolError("managed path is invalid")
    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts:
        raise ManagedProtocolError("managed path is invalid")
    resolved = raw.resolve(strict=False)
    _reject_reparse_ancestors(resolved)
    for immutable_root in immutable_roots:
        if resolved.is_relative_to(immutable_root) or immutable_root.is_relative_to(resolved):
            raise ManagedProtocolError("managed path overlaps immutable resources")
    return resolved


def _reject_reparse_ancestors(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            # ``stat`` follows a Windows junction and loses the reparse bit;
            # lstat keeps the link object that must be rejected.
            details = candidate.lstat()
        except OSError:
            continue
        attributes = getattr(details, "st_file_attributes", 0)
        if candidate.is_symlink() or attributes & _REPARSE_POINT:
            raise ManagedProtocolError("managed path crosses a reparse point")


def main() -> int:
    return run_managed()


__all__ = [
    "MANAGED_PROTOCOL_VERSION", "MAX_STARTUP_FRAME_BYTES", "ManagedDataRootLock",
    "ManagedProtocolError", "ManagedStartupFrame", "build_managed_settings", "main",
    "ManagedPathGuard", "immutable_roots_for_runtime", "read_startup_frame",
    "resource_root_for_runtime", "run_managed",
]
