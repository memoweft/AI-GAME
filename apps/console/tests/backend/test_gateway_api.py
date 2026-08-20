"""Phase 6 Week 3: Gateway HTTP API surface (frozen contract §2-§12, §14).

Covers the Week-3 slice of the §17 acceptance list (items 1-5):

- Task create/get (§5 / §6 canonical shapes);
- message / control (§7 / §9);
- conversation unique association (§8 attach / create / conflict);
- idempotent create / message / control (§3);
- Event sequence and pagination (§10).

SSE 断线续传 (§17 item 6) belongs to Week 4.

Also covers the Week-3 design decisions:

- default-OFF mounting: the default app composition is byte-identical to
  the legacy app (gateway routes absent, legacy behavior untouched);
- the §14 ten-code error mapping and the exact
  ``{"error": {code, message, retryable, details}}`` envelope;
- header validation (``Idempotency-Key`` required for create/message/
  control, ``X-Client-Id`` for create and the conversation entry);
- the existing ``X-AI-Game-Client`` write middleware still guards every
  POST (403 ``console_client_required``);
- the §12 observation canonical shape;
- manual body parsing: malformed JSON is a §14 ``VALIDATION_ERROR``
  (400), never a framework 422.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.gateway import (
    GatewayStore,
    IdempotencyService,
    TaskGateway,
)
from ai_game_console.gateway.device_registry import DeviceSummary
from ai_game_console.gateway_api import (
    GATEWAY_ERROR_STATUS,
    GatewayComposition,
    create_gateway_router,
)
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import RuntimeKernel
from conftest import WRITE_HEADERS, build_settings
from test_gateway_task_gateway import (
    _running_task,
    FakeDeviceRegistry,
    FakeObservationProvider,
)

from ai_game_console.runtime_kernel import TaskSource

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _kernel(tmp_path: Path) -> RuntimeKernel:
    return RuntimeKernel(
        SQLiteRuntimeStore(tmp_path / "runtime.db"),
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
    )


def _composition(
    tmp_path: Path,
    devices: dict[str, str] | None = {"device-1": "AVAILABLE"},
) -> GatewayComposition:
    kernel = _kernel(tmp_path)
    store = GatewayStore(tmp_path / "gateway.db")
    store.initialize()
    registry = FakeDeviceRegistry(devices) if devices is not None else None
    gateway = TaskGateway(
        kernel=kernel,
        idempotency=IdempotencyService(store),
        device_registry=registry,
    )
    return GatewayComposition(
        kernel=kernel,
        gateway=gateway,
        device_registry=registry,
        store=store,
    )


def _app(tmp_path: Path, devices: dict[str, str] | None = {"device-1": "AVAILABLE"}):
    return create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        gateway=_composition(tmp_path, devices),
    )


def _harness(
    tmp_path: Path, devices: dict[str, str] | None = {"device-1": "AVAILABLE"}
):
    app = _app(tmp_path, devices)
    composition = app.state.gateway
    return TestClient(app), composition.kernel, composition.device_registry


def _post_headers(key: str, client_id: str | None = "client-1") -> dict[str, str]:
    headers = {**WRITE_HEADERS, "Idempotency-Key": key}
    if client_id is not None:
        headers["X-Client-Id"] = client_id
    return headers


def _create_task(client: TestClient, *, key: str = "key-create", **overrides: Any) -> dict:
    body = {
        "goal": "open the game and reach the main page",
        "device_id": "device-1",
        "conversation_id": "conversation-1",
        "message_id": "message-1",
    }
    body.update(overrides)
    response = client.post(
        "/api/v1/tasks",
        json=body,
        headers=_post_headers(key),
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# default-OFF mounting strategy
# ---------------------------------------------------------------------------


class TestDefaultOff:
    def test_gateway_routes_absent_on_default_app(self, tmp_path: Path) -> None:
        app = create_app(
            settings=build_settings(tmp_path),
            adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        )
        client = TestClient(app)

        for path in (
            "/api/v1/devices",
            "/api/v1/tasks/unknown/events",
            "/api/v1/tasks/unknown/observations/obs-1",
            "/api/v1/tasks/unknown/controls",
        ):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json() == {
                "error": {
                    "code": "api_route_not_found",
                    "message": "请求的 API 路由不存在。",
                }
            }

    def test_default_app_has_no_gateway_state(self, tmp_path: Path) -> None:
        app = create_app(
            settings=build_settings(tmp_path),
            adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        )
        assert app.state.gateway is None

    def test_gateway_wins_conflicting_canonical_path(self, tmp_path: Path) -> None:
        # Cutover preview: POST /api/v1/tasks exists in both routers; with
        # the gateway enabled the gateway handler must serve it (gateway
        # router registered first), not the legacy one.
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers=_post_headers("k1"),
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"task"}
        assert set(body["task"]) == {
            "id",
            "goal",
            "status",
            "device_id",
            "current_stage",
            "last_event_sequence",
        }


# ---------------------------------------------------------------------------
# GET /devices (contract §4, Week-3 port-only shape)
# ---------------------------------------------------------------------------


class TestDevices:
    def test_port_only_shape(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.get("/api/v1/devices")

        assert response.status_code == 200
        body = response.json()
        assert body == {"items": [{"id": "device-1", "availability": "available"}]}
        for item in body["items"]:
            assert set(item) == {"id", "availability"}

    def test_availability_mapping(self, tmp_path: Path) -> None:
        client, _, _ = _harness(
            tmp_path,
            devices={
                "adb:serial-in-use": "IN_USE",
                "adb:serial-off": "UNAVAILABLE",
                "adb:serial-ready": "AVAILABLE",
            },
        )

        response = client.get("/api/v1/devices")

        assert response.status_code == 200
        items = {item["id"]: item["availability"] for item in response.json()["items"]}
        assert items == {
            "adb:serial-in-use": "in_use",
            "adb:serial-off": "unavailable",
            "adb:serial-ready": "available",
        }


# ---------------------------------------------------------------------------
# POST /tasks (contract §5)
# ---------------------------------------------------------------------------


class TestCreateTask:
    def test_canonical_response_shape(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        body = _create_task(client)

        assert set(body) == {"task"}
        task = body["task"]
        assert set(task) == {
            "id",
            "goal",
            "status",
            "device_id",
            "current_stage",
            "last_event_sequence",
        }
        assert task["goal"] == "open the game and reach the main page"
        assert task["status"] == "CREATED"
        assert task["device_id"] == "device-1"
        assert task["current_stage"] is None
        assert task["last_event_sequence"] == 1

    def test_requires_idempotency_key(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers={**WRITE_HEADERS, "X-Client-Id": "client-1"},
        )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["retryable"] is False

    def test_rejects_blank_idempotency_key(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers={**WRITE_HEADERS, "Idempotency-Key": "   ", "X-Client-Id": "c1"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_requires_client_id(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers={**WRITE_HEADERS, "Idempotency-Key": "k1"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_replay_returns_identical_response(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        first = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers=_post_headers("k1"),
        )
        second = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers=_post_headers("k1"),
        )

        assert first.status_code == 200
        assert second.json() == first.json()

    def test_same_key_different_payload_conflicts(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)
        client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers=_post_headers("k1"),
        )

        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": "different",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers=_post_headers("k1"),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    def test_unknown_device_404(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-2",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers=_post_headers("k1"),
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DEVICE_NOT_FOUND"

    def test_unavailable_device_409(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path, devices={"device-1": "UNAVAILABLE"})

        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "device-1",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers=_post_headers("k1"),
        )

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "DEVICE_NOT_AVAILABLE"
        assert error["retryable"] is True

    def test_malformed_json_body_is_validation_error(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks",
            content=b"this is not json",
            headers=_post_headers("k1"),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_non_object_json_body_is_validation_error(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks",
            json=["not", "an", "object"],
            headers=_post_headers("k1"),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# GET /tasks and GET /tasks/{id} (contract §6)
# ---------------------------------------------------------------------------


class TestGetTask:
    def test_snapshot_shape(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)
        task_id = _create_task(client)["task"]["id"]

        response = client.get(f"/api/v1/tasks/{task_id}")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"task"}
        assert set(body["task"]) == {
            "id",
            "goal",
            "status",
            "device_id",
            "constraints",
            "current_stage",
            "completed_stages",
            "verified_facts",
            "last_observation_id",
            "last_event_sequence",
            "updated_at",
        }
        assert body["task"]["id"] == task_id
        assert body["task"]["status"] == "CREATED"
        assert body["task"]["last_event_sequence"] == 1

    def test_unknown_task_404(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.get("/api/v1/tasks/nope")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"

    def test_list_tasks_shape(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)
        _create_task(client, key="k1")
        _create_task(client, key="k2", conversation_id="conversation-2")

        response = client.get("/api/v1/tasks")

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 2
        assert len(body["items"]) == 2
        for snapshot in body["items"]:
            assert "id" in snapshot
            assert "last_event_sequence" in snapshot


# ---------------------------------------------------------------------------
# POST /tasks/{id}/messages (contract §7)
# ---------------------------------------------------------------------------


class TestMessages:
    def test_message_accepted_shape(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "msg")

        response = client.post(
            f"/api/v1/tasks/{task_id}/messages",
            json={
                "message_id": "m-1",
                "conversation_id": "conversation-msg",
                "text": "hello",
            },
            headers=_post_headers("key-msg"),
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"accepted", "task_id", "message_id", "event_sequence"}
        assert body["accepted"] is True
        assert body["task_id"] == task_id
        assert body["message_id"] == "m-1"
        assert body["event_sequence"] == 4  # created + stage + start + message

    def test_message_records_user_event(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "evt")
        client.post(
            f"/api/v1/tasks/{task_id}/messages",
            json={
                "message_id": "m-1",
                "conversation_id": "conversation-evt",
                "text": "go",
            },
            headers=_post_headers("key-msg"),
        )

        last = kernel.events(task_id)[-1]

        assert last.type == "UserMessageReceived"
        assert last.actor.value == "user"
        assert last.payload == {
            "message_id": "m-1",
            "conversation_id": "conversation-evt",
            "text": "go",
        }

    def test_requires_idempotency_key(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "hdr")

        response = client.post(
            f"/api/v1/tasks/{task_id}/messages",
            json={"message_id": "m", "conversation_id": "c", "text": "t"},
            headers={**WRITE_HEADERS, "X-Client-Id": "c1"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_task_404(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks/nope/messages",
            json={"message_id": "m", "conversation_id": "c", "text": "t"},
            headers=_post_headers("key-msg"),
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"

    def test_message_on_cancelled_task_409(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "term")
        client.post(
            f"/api/v1/tasks/{task_id}/controls",
            json={"command": "cancel"},
            headers=_post_headers("key-cancel"),
        )

        response = client.post(
            f"/api/v1/tasks/{task_id}/messages",
            json={"message_id": "m", "conversation_id": "c", "text": "t"},
            headers=_post_headers("key-msg"),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_NOT_ACTIVE"

    def test_message_wrong_conversation_409(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "conflict")

        response = client.post(
            f"/api/v1/tasks/{task_id}/messages",
            json={
                "message_id": "m",
                "conversation_id": "conversation-other",
                "text": "t",
            },
            headers=_post_headers("key-msg"),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONVERSATION_CONFLICT"

    def test_replay_does_not_duplicate_event(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "replay")
        body = {
            "message_id": "m-1",
            "conversation_id": "conversation-replay",
            "text": "hello",
        }

        first = client.post(
            f"/api/v1/tasks/{task_id}/messages",
            json=body,
            headers=_post_headers("key-msg"),
        )
        second = client.post(
            f"/api/v1/tasks/{task_id}/messages",
            json=body,
            headers=_post_headers("key-msg"),
        )

        assert second.json() == first.json()
        assert len(kernel.events(task_id)) == 4


# ---------------------------------------------------------------------------
# POST /tasks/{id}/controls (contract §9)
# ---------------------------------------------------------------------------


class TestControls:
    def _control(self, client: TestClient, task_id: str, command: str, key: str) -> dict:
        response = client.post(
            f"/api/v1/tasks/{task_id}/controls",
            json={"command": command},
            headers=_post_headers(key),
        )
        return response

    def test_pause_shape(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "pause")

        response = self._control(client, task_id, "pause", "k1")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"accepted", "task_id", "command", "status", "event_sequence"}
        assert body["accepted"] is True
        assert body["command"] == "pause"
        assert body["status"] == "PAUSED"
        assert body["event_sequence"] == 4

    def test_resume_after_pause(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "resume")
        self._control(client, task_id, "pause", "k1")

        response = self._control(client, task_id, "resume", "k2")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "RUNNING"
        assert body["event_sequence"] == 5

    def test_cancel_is_terminal_cancelled(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "cancel")

        response = self._control(client, task_id, "cancel", "k1")

        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"

        again = self._control(client, task_id, "pause", "k2")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "TASK_NOT_ACTIVE"

    def test_takeover_pauses_task(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "takeover")

        response = self._control(client, task_id, "takeover", "k1")

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True
        assert body["command"] == "takeover"
        assert body["status"] == "PAUSED"
        assert body["event_sequence"] == 4
        assert kernel.events(task_id)[-1].type == "UserTakeover"

    def test_control_on_created_task_409(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)
        task_id = _create_task(client)["task"]["id"]

        response = self._control(client, task_id, "pause", "k1")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_NOT_ACTIVE"

    def test_unknown_command_400(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "badcmd")

        response = self._control(client, task_id, "stop", "k1")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_task_404(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = self._control(client, "nope", "pause", "k1")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"

    def test_requires_idempotency_key(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "hdr")

        response = client.post(
            f"/api/v1/tasks/{task_id}/controls",
            json={"command": "pause"},
            headers={**WRITE_HEADERS, "X-Client-Id": "c1"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_control_replay(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "replay")

        first = self._control(client, task_id, "pause", "k1")
        second = self._control(client, task_id, "pause", "k1")

        assert second.status_code == 200
        assert second.json() == first.json()
        assert len(kernel.events(task_id)) == 4


# ---------------------------------------------------------------------------
# GET /tasks/{id}/events (contract §10)
# ---------------------------------------------------------------------------


class TestEvents:
    def test_full_page_strictly_ascending(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "ev")
        client.post(
            f"/api/v1/tasks/{task_id}/controls",
            json={"command": "pause"},
            headers=_post_headers("k1"),
        )

        response = client.get(f"/api/v1/tasks/{task_id}/events")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"items", "next_after_sequence"}
        assert [item["sequence"] for item in body["items"]] == [1, 2, 3, 4]
        for item in body["items"]:
            assert set(item) == {
                "id",
                "task_id",
                "sequence",
                "type",
                "actor",
                "payload",
                "created_at",
            }
            assert item["task_id"] == task_id
        assert body["next_after_sequence"] == 4

    def test_after_sequence_cursor(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "cur")
        client.post(
            f"/api/v1/tasks/{task_id}/controls",
            json={"command": "pause"},
            headers=_post_headers("k1"),
        )

        response = client.get(
            f"/api/v1/tasks/{task_id}/events", params={"after_sequence": "2"}
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["sequence"] for item in body["items"]] == [3, 4]
        assert body["next_after_sequence"] == 4

    def test_empty_page_returns_cursor(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "empty")

        response = client.get(
            f"/api/v1/tasks/{task_id}/events", params={"after_sequence": "3"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["next_after_sequence"] == 3

    def test_limit_pages(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "limit")

        response = client.get(f"/api/v1/tasks/{task_id}/events", params={"limit": "2"})

        assert response.status_code == 200
        body = response.json()
        assert [item["sequence"] for item in body["items"]] == [1, 2]
        assert body["next_after_sequence"] == 2

    def test_invalid_cursor_400(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "badcur")

        for value in ("abc", "-1"):
            response = client.get(
                f"/api/v1/tasks/{task_id}/events",
                params={"after_sequence": value},
            )
            assert response.status_code == 400, value
            assert response.json()["error"]["code"] == "EVENT_CURSOR_INVALID"

    def test_invalid_limit_400(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "badlim")

        for value in ("0", "501", "xyz"):
            response = client.get(
                f"/api/v1/tasks/{task_id}/events", params={"limit": value}
            )
            assert response.status_code == 400, value
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_task_404_not_empty_page(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.get("/api/v1/tasks/nope/events")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"

    def test_user_event_actor_is_lowercase(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "actor")
        client.post(
            f"/api/v1/tasks/{task_id}/messages",
            json={
                "message_id": "m",
                "conversation_id": "conversation-actor",
                "text": "t",
            },
            headers=_post_headers("k1"),
        )

        response = client.get(f"/api/v1/tasks/{task_id}/events")

        assert response.json()["items"][-1]["actor"] == "user"


# ---------------------------------------------------------------------------
# GET /tasks/{id}/observations/{observation_id} (contract §12)
# ---------------------------------------------------------------------------


class TestObservations:
    def test_canonical_shape(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "obs")
        observation = kernel.capture_observation(task_id=task_id, device_id="device-1")

        response = client.get(
            f"/api/v1/tasks/{task_id}/observations/{observation.id}"
        )

        assert response.status_code == 200
        body = response.json()["observation"]
        assert set(body) == {
            "id",
            "captured_at",
            "screenshot_url",
            "width",
            "height",
            "foreground_app",
            "ui_tree_available",
            "consistency_status",
        }
        assert body["id"] == observation.id
        assert body["screenshot_url"].startswith("/api/v1/artifacts/")
        assert body["width"] == 1080
        assert body["height"] == 2400
        assert body["foreground_app"] == "com.example.target"
        assert body["ui_tree_available"] is True
        assert body["consistency_status"] == "consistent"

    def test_unknown_observation_404(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_id = _running_task(kernel, "device-1", "obs404")

        response = client.get(f"/api/v1/tasks/{task_id}/observations/nope")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"

    def test_observation_of_another_task_404(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        task_a = _running_task(kernel, "device-1", "taskA")
        task_b = _running_task(kernel, "device-1", "taskB")
        observation = kernel.capture_observation(task_id=task_a, device_id="device-1")

        response = client.get(f"/api/v1/tasks/{task_b}/observations/{observation.id}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"

    def test_unknown_task_404(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.get("/api/v1/tasks/nope/observations/obs-1")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /conversations/{id}/messages (contract §8)
# ---------------------------------------------------------------------------


class TestConversations:
    def _post(self, client: TestClient, conversation_id: str, body: dict, key: str):
        return client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=body,
            headers=_post_headers(key),
        )

    def test_attach_to_single_active_task(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)
        task_id = _create_task(client)["task"]["id"]

        response = self._post(
            client,
            "conversation-1",
            {"message_id": "m-2", "text": "keep going", "device_id": "device-1"},
            "k1",
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "accepted",
            "task_id",
            "message_id",
            "event_sequence",
            "created",
        }
        assert body["created"] is False
        assert body["task_id"] == task_id

    def test_create_when_no_active_task(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = self._post(
            client,
            "conversation-new",
            {"message_id": "m-1", "text": "start", "device_id": "device-1"},
            "k1",
        )

        assert response.status_code == 200
        body = response.json()
        assert body["created"] is True
        new_task_id = body["task_id"]
        assert new_task_id

        snapshot = client.get(f"/api/v1/tasks/{new_task_id}")
        assert snapshot.status_code == 200
        assert snapshot.json()["task"]["status"] == "CREATED"

    def test_create_requires_device_id(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = self._post(
            client, "conversation-new", {"message_id": "m", "text": "t"}, "k1"
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_two_active_tasks_conflict(self, tmp_path: Path) -> None:
        client, kernel, _ = _harness(tmp_path)
        for suffix in ("a", "b"):
            kernel.create_task(
                goal=f"goal {suffix}",
                source=TaskSource(
                    client_id=f"client-{suffix}",
                    conversation_id="conversation-amb",
                    initial_message_id=f"message-{suffix}",
                ),
                device_id="device-1",
            )

        response = self._post(
            client,
            "conversation-amb",
            {"message_id": "m", "text": "t", "device_id": "device-1"},
            "k1",
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONVERSATION_CONFLICT"

    def test_requires_client_id(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/conversations/conversation-x/messages",
            json={"message_id": "m", "text": "t", "device_id": "device-1"},
            headers={**WRITE_HEADERS, "Idempotency-Key": "k1"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_requires_idempotency_key(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/conversations/conversation-x/messages",
            json={"message_id": "m", "text": "t", "device_id": "device-1"},
            headers={**WRITE_HEADERS, "X-Client-Id": "c1"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_replay_identical(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)
        task_id = _create_task(client)["task"]["id"]
        body = {"message_id": "m-2", "text": "again", "device_id": "device-1"}

        first = self._post(client, "conversation-1", body, "k1")
        second = self._post(client, "conversation-1", body, "k1")

        assert first.status_code == 200
        assert second.json() == first.json()
        assert second.json()["task_id"] == task_id


# ---------------------------------------------------------------------------
# §14 error model: mapping, envelope, write middleware, lifecycle
# ---------------------------------------------------------------------------


class TestErrorModel:
    def test_ten_code_mapping(self) -> None:
        assert GATEWAY_ERROR_STATUS == {
            "VALIDATION_ERROR": 400,
            "EVENT_CURSOR_INVALID": 400,
            "TASK_NOT_FOUND": 404,
            "DEVICE_NOT_FOUND": 404,
            "TASK_NOT_ACTIVE": 409,
            "CONVERSATION_CONFLICT": 409,
            "DEVICE_NOT_AVAILABLE": 409,
            "IDEMPOTENCY_CONFLICT": 409,
            "LEGACY_TASK_WRITE_DISABLED": 403,
            "INTERNAL_ERROR": 500,
        }

    def test_error_envelope_exact(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.get("/api/v1/tasks/nope")

        body = response.json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message", "retryable", "details"}
        assert body["error"]["code"] == "TASK_NOT_FOUND"
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["message"]
        assert isinstance(body["error"]["retryable"], bool)

    def test_unexpected_failure_is_internal_error_500(
        self, tmp_path: Path
    ) -> None:
        class ExplodingRegistry:
            def list_devices(self):
                raise RuntimeError("boom")

            def get_device(self, device_id: str):
                return DeviceSummary(device_id, "AVAILABLE")

        kernel = _kernel(tmp_path)
        store = GatewayStore(tmp_path / "gateway.db")
        store.initialize()
        gateway = TaskGateway(
            kernel=kernel, idempotency=IdempotencyService(store)
        )
        composition = GatewayComposition(
            kernel=kernel,
            gateway=gateway,
            device_registry=ExplodingRegistry(),
            store=store,
        )
        app = create_app(
            settings=build_settings(tmp_path),
            adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
            gateway=composition,
        )
        client = TestClient(app)

        response = client.get("/api/v1/devices")

        assert response.status_code == 500
        error = response.json()["error"]
        assert error["code"] == "INTERNAL_ERROR"
        assert "boom" not in response.text
        assert "RuntimeError" not in response.text

    def test_post_without_client_header_403(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        response = client.post(
            "/api/v1/tasks",
            json={"goal": "g"},
            headers={"Idempotency-Key": "k1"},
        )

        assert response.status_code == 403
        assert response.json() == {
            "error": {
                "code": "console_client_required",
                "message": "写操作需要请求头 X-AI-Game-Client: console-v1。",
            }
        }

    def test_get_devices_without_client_header_ok(self, tmp_path: Path) -> None:
        client, _, _ = _harness(tmp_path)

        assert client.get("/api/v1/devices").status_code == 200

    def test_lifespan_shutdown_is_clean(self, tmp_path: Path) -> None:
        app = _app(tmp_path)

        with TestClient(app):
            pass  # startup + shutdown (closes gateway store and kernel)

        assert app.state.gateway is not None
