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
import stat
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


class ManagedDataRootLock:
    """One Windows process per managed data root, released by the OS on exit."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / ".ai-game-managed.lock"
        self._handle: io.BufferedRandom | None = None

    def acquire(self) -> None:
        # msvcrt is intentionally imported only on the Windows execution path.
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after creation so a caller cannot swap a missing ancestor for
        # a junction between startup-frame validation and opening the lock.
        _reject_reparse_ancestors(self.path.parent)
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
    resource_root = resource_root_for_runtime()
    writable_root = _managed_path(payload["writable_root"], resource_root)
    data_dir = _managed_path(payload["data_dir"], resource_root)
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


def build_managed_settings(frame: ManagedStartupFrame) -> Settings:
    root = resource_root_for_runtime()
    frontend = root / "frontend"
    if not frontend.is_dir():
        frontend = root / "apps" / "console" / "frontend" / "dist"
    return Settings(
        project_root=root,
        data_dir=frame.data_dir,
        database_path=frame.data_dir / "console.db",
        frontend_dist=frontend,
        console_shutdown_token=frame.shutdown_token,
        harness_api_token=frame.capability,
        runtime_mode="kernel_active",
        managed_runtime=True,
    )


def run_managed(
    *, stdin: BinaryIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None,
) -> int:
    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    lock: ManagedDataRootLock | None = None
    try:
        frame = read_startup_frame(input_stream)
        lock = ManagedDataRootLock(frame.data_dir)
        lock.acquire()
        settings = build_managed_settings(frame)
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
        ready_thread = threading.Thread(
            target=_emit_ready_when_listening,
            args=(server, frame.nonce, frame.host, frame.port, output_stream, stopped),
            daemon=True,
        )
        parent_thread = threading.Thread(
            target=_watch_parent_pipe, args=(input_stream, request_shutdown), daemon=True,
        )
        ready_thread.start()
        parent_thread.start()
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
        if lock is not None:
            lock.release()


def _emit_ready_when_listening(
    server: uvicorn.Server, nonce: str, host: str, port: int, stream: TextIO, stopped: threading.Event,
) -> None:
    while not stopped.is_set():
        if server.started:
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


def _watch_parent_pipe(stream: BinaryIO, request_shutdown: Callable[[], None]) -> None:
    # After the one startup line, the pipe is only a liveness channel.  Extra
    # bytes and EOF both close rather than becoming a second command protocol.
    try:
        stream.read(1)
    finally:
        request_shutdown()


def _secret_shape(value: object) -> bool:
    return isinstance(value, str) and 32 <= len(value) <= 512 and "\x00" not in value


def _managed_path(value: object, resource_root: Path) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ManagedProtocolError("managed path is invalid")
    if value.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
        raise ManagedProtocolError("managed path is invalid")
    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts:
        raise ManagedProtocolError("managed path is invalid")
    resolved = raw.resolve(strict=False)
    _reject_reparse_ancestors(resolved)
    if resolved.is_relative_to(resource_root) or resource_root.is_relative_to(resolved):
        raise ManagedProtocolError("managed path overlaps immutable resources")
    return resolved


def _reject_reparse_ancestors(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            details = candidate.stat()
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
    "read_startup_frame", "resource_root_for_runtime", "run_managed",
]
