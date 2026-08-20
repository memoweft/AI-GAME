"""Phase 6 Week 4: Gateway SSE event stream (frozen contract §11).

Covers the Week-4 slice of the §17 acceptance list (item 6: SSE
断线续传 + Snapshot 校准):

- frozen frame shape: ``id: <sequence>``, ``event: runtime_event``,
  ``data: {"sequence", "type", "payload"}`` (3 keys, not the §10 page
  shape), blank-line terminator;
- pre-stream validation: unknown task is a 404 ``TASK_NOT_FOUND`` JSON
  response, an invalid cursor a 400 ``EVENT_CURSOR_INVALID`` — never a
  broken stream;
- resume after disconnect: reconnecting with ``after_sequence=<last
  successfully processed id>`` delivers exactly the new events — no
  duplicates, no gaps;
- heartbeats are comment frames (``: heartbeat``), are NOT Runtime
  Events, and consume no Task sequence: the next real event keeps
  sequential numbering;
- slow-client isolation: events committed by the server while a stream
  is open are delivered on the next poll — the stream is read-only
  polling and never blocks the Kernel;
- stream response headers (``text/event-stream``, ``no-cache``).

Why a real server: starlette's TestClient blocks its transport until
the ASGI app call *completes* — a §11 stream never completes, so the
endpoint is exercised over a real socket with uvicorn on a daemon
thread. All task activity happens through the HTTP surface (create →
message → cancel), so every Kernel write lands on the server thread.

The composition's ``poll_interval`` is shortened to 20 ms so every
scenario completes in milliseconds.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from ai_game_console.api import create_app
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.gateway import (
    GatewayStore,
    IdempotencyService,
    TaskGateway,
)
from ai_game_console.gateway_api import GatewayComposition
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import RuntimeKernel
from conftest import WRITE_HEADERS, build_settings
from test_gateway_task_gateway import (
    FakeDeviceRegistry,
    FakeObservationProvider,
)

POLL_INTERVAL = 0.02


# ---------------------------------------------------------------------------
# server fixture
# ---------------------------------------------------------------------------


class _AppServer:
    """Real uvicorn server on a daemon thread (see module docstring)."""

    def __init__(self, app) -> None:
        self.port = self._free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, daemon=True, name="sse-test-server"
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def __enter__(self) -> "_AppServer":
        self.thread.start()
        deadline = time.monotonic() + 15.0
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("uvicorn server thread exited during startup")
            if time.monotonic() > deadline:
                raise TimeoutError("uvicorn server did not start")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        return False


@pytest.fixture()
def sse_env(tmp_path: Path):
    kernel = RuntimeKernel(
        SQLiteRuntimeStore(tmp_path / "runtime.db"),
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    store = GatewayStore(tmp_path / "gateway.db")
    store.initialize()
    registry = FakeDeviceRegistry({"device-1": "AVAILABLE"})
    gateway = TaskGateway(
        kernel=kernel,
        idempotency=IdempotencyService(store),
        device_registry=registry,
    )
    composition = GatewayComposition(
        kernel=kernel,
        gateway=gateway,
        device_registry=registry,
        store=store,
        poll_interval=POLL_INTERVAL,
    )
    app = create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        gateway=composition,
    )
    with _AppServer(app) as server:
        # trust_env=False: loopback traffic must never be routed through
        # a proxy picked up from environment variables (a local proxy
        # on 127.0.0.1:7890 answered the loopback URL with 502).
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.port}",
            timeout=10.0,
            trust_env=False,
        ) as client:
            yield client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# Fixed ids the create helper submits. The frozen task response shape does
# NOT carry ``conversation_id`` back, so tests address the conversation by
# this known id.
_CONVERSATION_ID = "conversation-1"
_DEVICE_ID = "device-1"


def _write_headers(key: str) -> dict[str, str]:
    return {**WRITE_HEADERS, "Idempotency-Key": key, "X-Client-Id": "client-1"}


def _create_task(client: httpx.Client, *, key: str = "key-create") -> dict:
    response = client.post(
        "/api/v1/tasks",
        json={
            "goal": "open the game and reach the main page",
            "device_id": _DEVICE_ID,
            "conversation_id": _CONVERSATION_ID,
            "message_id": "message-1",
        },
        headers=_write_headers(key),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _stream_url(task_id: str, after_sequence: int | None = None) -> str:
    url = f"/api/v1/tasks/{task_id}/events/stream"
    if after_sequence is not None:
        url += f"?after_sequence={after_sequence}"
    return url


class _SseReader:
    """Read one open SSE response line by line.

    httpx marks a response stream consumed the moment its raw iterator
    starts, so a response supports exactly ONE ``iter_lines()``
    iterator — this wrapper owns that single iterator for the lifetime
    of the stream.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._lines = response.iter_lines()

    def read_line(self) -> str:
        return next(self._lines)

    def read_lines(self, count: int) -> list[str]:
        return [self.read_line() for _ in range(count)]

    def read_event(self) -> dict[str, Any]:
        """Read until the next real event, skipping heartbeat frames.

        The poll cadence and the test thread race on who goes first
        after a commit, so zero or more heartbeats may legally precede
        the awaited event.
        """
        while True:
            line = self.read_line()
            if line == ": heartbeat":
                assert self.read_line() == ""
                continue
            assert line.startswith("id: "), line
            return _parse_event_frame([line] + self.read_lines(3))


def _parse_event_frame(lines: list[str]) -> dict[str, Any]:
    """Parse one 4-line SSE event frame into {id, sequence, data}."""
    assert lines[0].startswith("id: "), lines
    assert lines[1] == "event: runtime_event", lines
    assert lines[2].startswith("data: "), lines
    assert lines[3] == "", lines
    data = json.loads(lines[2][len("data: "):])
    return {
        "id": lines[0][len("id: "):].strip(),
        "data": data,
        "sequence": data["sequence"],
    }


# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------


def test_stream_frozen_framing_and_payload_shape(sse_env) -> None:
    client = sse_env
    body = _create_task(client)
    task_id = body["task"]["id"]

    with client.stream("GET", _stream_url(task_id)) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        reader = _SseReader(response)
        parsed = reader.read_event()
    # Frozen §11 data shape: exactly 3 keys, not the §10 page shape.
    assert set(parsed["data"]) == {"sequence", "type", "payload"}
    assert parsed["id"] == str(parsed["sequence"])
    assert parsed["sequence"] == 1
    assert parsed["data"]["type"] == "TaskCreated"


def test_stream_response_headers(sse_env) -> None:
    client = sse_env
    body = _create_task(client, key="key-create-2")
    task_id = body["task"]["id"]

    with client.stream("GET", _stream_url(task_id)) as response:
        assert "text/event-stream" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        _SseReader(response).read_lines(4)


# ---------------------------------------------------------------------------
# resume after disconnect (§17 item 6)
# ---------------------------------------------------------------------------


def test_stream_resume_after_disconnect_no_duplicates_no_gaps(sse_env) -> None:
    client = sse_env
    body = _create_task(client, key="key-create-3")
    task = body["task"]
    task_id = task["id"]

    # First connection: receive the initial event, then disconnect.
    with client.stream("GET", _stream_url(task_id)) as response:
        first = _SseReader(response).read_event()
    assert first["sequence"] == 1

    # A new event is committed while the client is disconnected.
    message = client.post(
        f"/api/v1/conversations/{_CONVERSATION_ID}/messages",
        json={"message_id": "msg-sse-3", "text": "check progress please"},
        headers=_write_headers("key-sse-message"),
    )
    assert message.status_code == 200, message.text

    # Reconnect carrying the last successfully processed sequence.
    with client.stream(
        "GET", _stream_url(task_id, after_sequence=1)
    ) as response:
        resumed = _SseReader(response).read_event()

    # Exactly the new event: no replay of sequence 1 (no duplicates),
    # no gap.
    assert resumed["id"] == "2"
    assert resumed["sequence"] == 2
    assert resumed["data"]["type"] == "UserMessageReceived"


def test_stream_reconnect_from_zero_replays_full_log(sse_env) -> None:
    """A client that lost its log entirely re-reads the full sequence.

    (The Snapshot + Event 校准 duty is the client's: the stream simply
    serves whatever the cursor asks for.)
    """
    client = sse_env
    body = _create_task(client, key="key-create-4")
    task = body["task"]
    task_id = task["id"]

    message = client.post(
        f"/api/v1/conversations/{_CONVERSATION_ID}/messages",
        json={"message_id": "msg-sse-4", "text": "hello"},
        headers=_write_headers("key-sse-message-4"),
    )
    assert message.status_code == 200, message.text

    with client.stream("GET", _stream_url(task_id)) as response:
        reader = _SseReader(response)
        sequences = [reader.read_event()["sequence"] for _ in range(2)]

    assert sequences == [1, 2]


# ---------------------------------------------------------------------------
# heartbeats (§11)
# ---------------------------------------------------------------------------


def test_heartbeat_is_comment_frame_and_consumes_no_sequence(sse_env) -> None:
    client = sse_env
    body = _create_task(client, key="key-create-5")
    task_id = body["task"]["id"]

    with client.stream(
        "GET", _stream_url(task_id, after_sequence=1)
    ) as response:
        reader = _SseReader(response)
        # No new events yet: a heartbeat comment frame — not a Runtime
        # Event, and not counted in the sequence.
        heartbeat = reader.read_lines(2)
        assert heartbeat[0] == ": heartbeat"
        assert heartbeat[1] == ""

        # A new event is committed by the server while the stream is
        # open: it must keep sequential numbering (2), with no gap
        # caused by the heartbeat.
        control = client.post(
            f"/api/v1/tasks/{task_id}/controls",
            json={"command": "cancel", "reason": "sse test"},
            headers=_write_headers("key-sse-cancel"),
        )
        assert control.status_code == 200, control.text

        # The awaited event may be preceded by more heartbeats (poll
        # cadence races the commit) — skip them.
        parsed = reader.read_event()

    assert parsed["id"] == "2"
    assert parsed["sequence"] == 2
    assert parsed["data"]["type"] == "TaskCancelled"


# ---------------------------------------------------------------------------
# pre-stream validation
# ---------------------------------------------------------------------------


def test_stream_unknown_task_404_pre_stream(sse_env) -> None:
    client = sse_env

    response = client.get(_stream_url("task-missing"))

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "TASK_NOT_FOUND"
    assert error["retryable"] is False


def test_stream_invalid_cursor_400_pre_stream(sse_env) -> None:
    client = sse_env
    body = _create_task(client, key="key-create-6")
    task_id = body["task"]["id"]

    response = client.get(_stream_url(task_id) + "?after_sequence=abc")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "EVENT_CURSOR_INVALID"
    # A malformed cursor is a client validation error, not a transient
    # condition: retrying the same request cannot succeed.
    assert error["retryable"] is False
