"""Phase 6 集成阶段：§17 验收清单 item 8-10 逐项证据。

Week 3 (test_gateway_api.py) 覆盖 §17 前 5 项（create/get、message/control、
conversation 唯一关联、幂等、Event sequence 与分页）；Week 4
(test_gateway_sse.py) 覆盖 item 6-7（SSE 断线续传、Snapshot + Event 校准）。
本文件补齐集成阶段剩余的 3 项验收证据：

- item 8  Web/Hermes 共享语义：同一 Task 对不同 X-Client-Id 返回完全一致的
  任务语义（status / current_stage / last_event_sequence / events）。Gateway
  不按 Client 类型切换语义——契约 §1「Gateway 不负责让不同 Client 拥有不同
  任务语义」，§13「微信/Hermes 默认消费用户可读投影」。
- item 9  Client 无法访问 ADB：Gateway 路由集完全落在 §2 canonical 路径内，
  不存在任何 ADB 执行 / 截图 / shell 端点；设备摘要只暴露 ``{id, availability}``，
  ADB 原始输出永不越过 API 边界——契约 §1「Gateway 不负责执行 ADB」、§14。
- item 10 legacy write 不会绕过 Kernel：Gateway 启用后 canonical 任务路径
  （POST/GET /tasks、GET /tasks/{id}）全部由 Gateway 服务并落到 Kernel，
  legacy 重复实现不可达——契约 §2（cutover）、§15（Legacy Adapter 边界）。

这些是集成验收证据，不新增产品能力。
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
from ai_game_console.gateway_api import (
    GatewayComposition,
    create_gateway_router,
)
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import RuntimeKernel
from conftest import WRITE_HEADERS, build_settings
from test_gateway_task_gateway import (
    FakeDeviceRegistry,
    FakeObservationProvider,
)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _composition(
    tmp_path: Path, devices: dict[str, str] | None = {"device-1": "AVAILABLE"}
) -> GatewayComposition:
    kernel = RuntimeKernel(
        SQLiteRuntimeStore(tmp_path / "runtime.db"),
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
    )
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


def _harness(
    tmp_path: Path, devices: dict[str, str] | None = {"device-1": "AVAILABLE"}
):
    app = create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        gateway=_composition(tmp_path, devices),
    )
    return TestClient(app)


def _post_headers(key: str, client_id: str | None = "client-1") -> dict[str, str]:
    headers = {**WRITE_HEADERS, "Idempotency-Key": key}
    if client_id is not None:
        headers["X-Client-Id"] = client_id
    return headers


def _create_task(client: TestClient, key: str, **overrides: Any) -> dict:
    body = {
        "goal": "open the game and reach the main page",
        "device_id": "device-1",
        "conversation_id": "conversation-1",
        "message_id": "message-1",
    }
    body.update(overrides)
    response = client.post("/api/v1/tasks", json=body, headers=_post_headers(key))
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# item 8 — Web/Hermes 共享语义
# ---------------------------------------------------------------------------


class TestSharedSemantics:
    """契约 §1 / §13：Gateway 对不同 Client 返回完全一致的任务语义。"""

    def test_snapshot_identical_across_clients(self, tmp_path: Path) -> None:
        client = _harness(tmp_path)
        task_id = _create_task(client, "k-shared")["task"]["id"]

        payloads = []
        for client_id in ("web-1", "hermes-1", "wechat-1"):
            response = client.get(
                f"/api/v1/tasks/{task_id}", headers={"X-Client-Id": client_id}
            )
            assert response.status_code == 200, (client_id, response.text)
            payloads.append(response.json())

        # Byte-identical: no per-client branching anywhere in the gateway.
        assert payloads[0] == payloads[1] == payloads[2]
        task = payloads[0]["task"]
        # The shared semantics carry the canonical projection fields.
        for field in (
            "id",
            "goal",
            "status",
            "device_id",
            "current_stage",
            "last_event_sequence",
        ):
            assert field in task

    def test_event_page_identical_across_clients(self, tmp_path: Path) -> None:
        client = _harness(tmp_path)
        task_id = _create_task(client, "k-events")["task"]["id"]

        payloads = []
        for client_id in ("web-1", "hermes-1"):
            response = client.get(
                f"/api/v1/tasks/{task_id}/events",
                headers={"X-Client-Id": client_id},
            )
            assert response.status_code == 200, (client_id, response.text)
            payloads.append(response.json())

        assert payloads[0] == payloads[1]
        # At least the create event is projected to every consumer.
        assert payloads[0]["items"][0]["type"] == "TaskCreated"


# ---------------------------------------------------------------------------
# item 9 — Client 无法访问 ADB
# ---------------------------------------------------------------------------

# The full §2 canonical surface, as path -> HTTP methods.
CANONICAL_GATEWAY_ROUTES: dict[str, set[str]] = {
    "/api/v1/devices": {"GET"},
    "/api/v1/tasks": {"POST", "GET"},
    "/api/v1/tasks/{task_id}": {"GET"},
    "/api/v1/tasks/{task_id}/messages": {"POST"},
    "/api/v1/tasks/{task_id}/controls": {"POST"},
    "/api/v1/tasks/{task_id}/events": {"GET"},
    "/api/v1/tasks/{task_id}/events/stream": {"GET"},
    "/api/v1/tasks/{task_id}/observations/{observation_id}": {"GET"},
    "/api/v1/conversations/{conversation_id}/messages": {"POST"},
}


class TestNoAdbSurface:
    """契约 §1 / §14：Gateway 不执行 ADB，原始输出不越过 API 边界。"""

    def test_gateway_router_is_exactly_the_canonical_set(
        self, tmp_path: Path
    ) -> None:
        router = create_gateway_router(_composition(tmp_path))
        surface: dict[str, set[str]] = {}
        for route in router.routes:
            methods = {m for m in route.methods if m in {"GET", "POST"}}
            surface[route.path] = surface.get(route.path, set()) | methods

        # Every mounted route is canonical; nothing extra is exposed.
        assert surface == CANONICAL_GATEWAY_ROUTES

    def test_no_adb_execution_endpoint(self, tmp_path: Path) -> None:
        router = create_gateway_router(_composition(tmp_path))
        for route in router.routes:
            lowered = route.path.lower()
            for token in ("adb", "screenshot", "shell", "exec", "input", "tap", "swipe"):
                assert token not in lowered, f"ADB-like surface on {route.path}"

    def test_devices_summary_exposes_no_adb_fields(self, tmp_path: Path) -> None:
        client = _harness(tmp_path)
        response = client.get("/api/v1/devices")
        assert response.status_code == 200

        items = response.json()["items"]
        assert items, "device registry should expose at least one device"
        for item in items:
            # Week-3 port-only shape: id + availability, nothing ADB-raw.
            assert set(item) == {"id", "availability"}
            assert item["availability"] in {"available", "in_use", "unavailable"}

    def test_error_envelope_never_carries_adb_detail(self, tmp_path: Path) -> None:
        # An unknown device yields a canonical 404 with a generic message and
        # structured details — no ADB serial, stdout/stderr, or traceback.
        client = _harness(tmp_path, devices={"device-1": "AVAILABLE"})
        response = client.post(
            "/api/v1/tasks",
            json={
                "goal": "g",
                "device_id": "does-not-exist",
                "conversation_id": "c",
                "message_id": "m",
            },
            headers=_post_headers("k-unknown-device"),
        )
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "DEVICE_NOT_FOUND"
        # The message and details are client-safe; no vendor/ADB leakage.
        text = f"{error['message']} {error.get('details')}"
        for token in ("adb", "traceback", "stderr", "serial"):
            assert token.lower() not in text.lower()


# ---------------------------------------------------------------------------
# item 10 — legacy write 不会绕过 Kernel
# ---------------------------------------------------------------------------

# Canonical snapshot keys (gateway/snapshot.py TaskSnapshot.to_dict) that the
# legacy MobileTaskSchema does NOT carry.
GATEWAY_SNAPSHOT_KEYS = {
    "device_id",
    "constraints",
    "completed_stages",
    "verified_facts",
    "last_observation_id",
    "last_event_sequence",
}
# Legacy MobileTaskSchema keys that the canonical snapshot does NOT carry.
LEGACY_ONLY_KEYS = {
    "target_id",
    "skill_id",
    "input_revision",
    "active_subgoal_index",
    "strategy",
    "skill_memory_version",
}


class TestLegacyWritesCannotBypassKernel:
    """契约 §2 / §15：Gateway 启用后 canonical 任务路径全部落到 Kernel。"""

    def test_create_task_served_by_gateway_not_legacy(
        self, tmp_path: Path
    ) -> None:
        # The legacy ``POST /tasks`` (create_mobile_task) and the gateway
        # ``POST /tasks`` share a path. With the gateway mounted (registered
        # first) the canonical snapshot shape must win.
        client = _harness(tmp_path)
        body = _create_task(client, "k-legacy-create")
        assert set(body) == {"task"}
        task = body["task"]
        # Canonical create shape, not the legacy MobileTaskSchema: it carries
        # the canonical projection fields and none of the legacy ones.
        assert set(task) == {
            "id",
            "goal",
            "status",
            "device_id",
            "current_stage",
            "last_event_sequence",
        }
        assert not (set(task) & LEGACY_ONLY_KEYS), "legacy shape leaked"

    def test_list_tasks_served_by_gateway_not_legacy(
        self, tmp_path: Path
    ) -> None:
        client = _harness(tmp_path)
        created = _create_task(client, "k-legacy-list")["task"]["id"]
        _create_task(
            client,
            "k-legacy-list-2",
            goal="second goal",
            conversation_id="conversation-2",
        )

        response = client.get("/api/v1/tasks")
        assert response.status_code == 200
        data = response.json()
        assert set(data) == {"items", "count"}
        assert data["count"] == 2

        for item in data["items"]:
            # Each item is a canonical TaskSnapshot, not a MobileTaskSchema.
            assert "device_id" in item
            assert "last_event_sequence" in item
            assert not (set(item) & LEGACY_ONLY_KEYS), "legacy shape leaked"

        # The tasks we created through the gateway are the ones listed.
        assert any(item["id"] == created for item in data["items"])

    def test_get_task_served_by_gateway_not_legacy(self, tmp_path: Path) -> None:
        client = _harness(tmp_path)
        task_id = _create_task(client, "k-legacy-get")["task"]["id"]

        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()["task"]
        assert "device_id" in task
        assert "last_event_sequence" in task
        assert not (set(task) & LEGACY_ONLY_KEYS), "legacy shape leaked"
