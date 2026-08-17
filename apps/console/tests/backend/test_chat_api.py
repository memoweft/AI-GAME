from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.android_chat_adapter import RepositoryAndroidAutomationFactory
from ai_game_console.chat import ChatCompletion, ChatCoordinator
from ai_game_console.config import Settings
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.repository import SQLiteRepository

from conftest import WRITE_HEADERS, build_settings


class ApiProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, json_response, is_cancelled):
        self.calls += 1
        return ChatCompletion(
            assistant_text="API 本地回复",
            provider="api-fake",
            model="api-model",
        )


class BlockingApiProvider(ApiProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.messages = []

    def complete(self, messages, *, json_response, is_cancelled):
        self.calls += 1
        self.messages.append(list(messages))
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError("API provider release timed out")
        return ChatCompletion(
            assistant_text="API 合并回复",
            provider="api-blocking",
            model="api-model",
        )


def wait_for_api_turn(client: TestClient, session_id: str, timeout: float = 2) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/v1/chat/sessions/{session_id}").json()
        if payload["turns"] and payload["turns"][-1]["status"] in {
            "completed",
            "failed",
            "cancelled",
        }:
            return payload
        time.sleep(0.005)
    raise AssertionError("chat turn did not finish")


def build_chat_app(tmp_path: Path, provider=None):
    settings: Settings = build_settings(tmp_path)
    repository = SQLiteRepository(settings.database_path)
    provider = provider or ApiProvider()
    coordinator = ChatCoordinator(
        repository,
        local_provider=provider,
        cloud_provider=None,
    )
    app = create_app(
        settings=settings,
        repository=repository,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        chat_coordinator=coordinator,
    )
    return app, provider


def test_chat_api_contract_accepts_202_and_returns_transcript(tmp_path: Path) -> None:
    app, provider = build_chat_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/chat/sessions",
            headers=WRITE_HEADERS,
            json={
                "title": None,
                "mode": "local_chat",
                "target_id": None,
                "auto_execute": False,
            },
        )
        assert created.status_code == 201
        session = created.json()
        assert session["title"] == "新对话"
        assert session["mode"] == "local_chat"

        accepted = client.post(
            f"/api/v1/chat/sessions/{session['id']}/turns",
            headers=WRITE_HEADERS,
            json={"content": "你好", "client_request_id": "api-request-1"},
        )
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "accepted"

        transcript = wait_for_api_turn(client, session["id"])
        assert set(transcript) == {"session", "messages", "turns", "steps"}
        assert [item["role"] for item in transcript["messages"]] == [
            "user",
            "assistant",
        ]
        assert transcript["messages"][-1]["provider"] == "api-fake"
        user_message = transcript["messages"][0]
        assert user_message["client_request_id"] == "api-request-1"
        assert user_message["content_sha256"] == hashlib.sha256(
            "你好".encode("utf-8")
        ).hexdigest()
        assert user_message["input_revision"] == 1
        assert user_message["delivery_status"] == "applied"
        assert user_message["applied_at"] is not None
        assistant_message = transcript["messages"][-1]
        assert assistant_message["client_request_id"] is None
        assert assistant_message["content_sha256"] is None
        assert assistant_message["input_revision"] is None
        assert assistant_message["delivery_status"] is None
        assert assistant_message["applied_at"] is None
        assert transcript["turns"][-1]["input_revision"] == 1
        assert transcript["turns"][-1]["reply_status"] == "completed"
        assert transcript["turns"][-1]["execution_status"] == "not_requested"

        sessions = client.get("/api/v1/chat/sessions").json()
        assert sessions["count"] == 1
        assert sessions["items"][0]["id"] == session["id"]
        assert provider.calls == 1


def test_chat_api_joined_send_returns_same_turn_and_transcript_lifecycle(
    tmp_path: Path,
) -> None:
    provider = BlockingApiProvider()
    app, _ = build_chat_app(tmp_path, provider=provider)
    with TestClient(app) as client:
        session = client.post(
            "/api/v1/chat/sessions",
            headers=WRITE_HEADERS,
            json={
                "title": "API 统一收件箱",
                "mode": "local_chat",
                "target_id": None,
                "auto_execute": False,
            },
        ).json()
        first = client.post(
            f"/api/v1/chat/sessions/{session['id']}/turns",
            headers=WRITE_HEADERS,
            json={"content": "第一条", "client_request_id": "api-joined-1"},
        )
        assert first.status_code == 202
        assert provider.started.wait(timeout=1)
        joined = client.post(
            f"/api/v1/chat/sessions/{session['id']}/turns",
            headers=WRITE_HEADERS,
            json={"content": "第二条", "client_request_id": "api-joined-2"},
        )
        assert joined.status_code == 202
        assert joined.json()["id"] == first.json()["id"]
        assert joined.json()["input_revision"] == 2
        duplicate = client.post(
            f"/api/v1/chat/sessions/{session['id']}/turns",
            headers=WRITE_HEADERS,
            json={"content": "第二条", "client_request_id": "api-joined-2"},
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["id"] == first.json()["id"]
        conflict = client.post(
            f"/api/v1/chat/sessions/{session['id']}/turns",
            headers=WRITE_HEADERS,
            json={"content": "冲突", "client_request_id": "api-joined-2"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "client_request_id_conflict"
        provider.release.set()
        transcript = wait_for_api_turn(client, session["id"])

    assert len(transcript["turns"]) == 1
    assert transcript["turns"][0]["input_revision"] == 2
    user_messages = [
        message for message in transcript["messages"] if message["role"] == "user"
    ]
    assert [message["input_revision"] for message in user_messages] == [1, 2]
    assert [message["delivery_status"] for message in user_messages] == [
        "applied",
        "applied",
    ]
    assert provider.calls == 2


def test_chat_api_idempotency_and_write_header(tmp_path: Path) -> None:
    app, provider = build_chat_app(tmp_path)
    with TestClient(app) as client:
        missing_header = client.post(
            "/api/v1/chat/sessions",
            json={
                "title": None,
                "mode": "local_chat",
                "target_id": None,
                "auto_execute": False,
            },
        )
        assert missing_header.status_code == 403

        session = client.post(
            "/api/v1/chat/sessions",
            headers=WRITE_HEADERS,
            json={
                "title": "幂等",
                "mode": "local_chat",
                "target_id": None,
                "auto_execute": False,
            },
        ).json()
        body = {"content": "同一请求", "client_request_id": "api-idempotent"}
        first = client.post(
            f"/api/v1/chat/sessions/{session['id']}/turns",
            headers=WRITE_HEADERS,
            json=body,
        )
        wait_for_api_turn(client, session["id"])
        duplicate = client.post(
            f"/api/v1/chat/sessions/{session['id']}/turns",
            headers=WRITE_HEADERS,
            json=body,
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["id"] == first.json()["id"]
        assert provider.calls == 1


def test_cloud_session_is_rejected_when_cloud_provider_is_not_configured(
    tmp_path: Path,
) -> None:
    app, _ = build_chat_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/sessions",
            headers=WRITE_HEADERS,
            json={
                "title": "云端",
                "mode": "cloud_execute",
                "target_id": None,
                "auto_execute": False,
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "cloud_model_not_configured"


def test_chat_validation_never_echoes_rejected_content(tmp_path: Path) -> None:
    app, _ = build_chat_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/sessions/not-real/turns",
            headers=WRITE_HEADERS,
            json={
                "content": "sensitive-message-must-not-echo",
                "client_request_id": "contains a space",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_chat_request"
        assert "sensitive-message-must-not-echo" not in response.text


def test_default_app_composes_android_automation_only_when_fully_configured(
    tmp_path: Path,
) -> None:
    base = build_settings(tmp_path)
    configured = replace(
        base,
        gui_executor_enabled=True,
        adb_path=str(tmp_path / "adb.exe"),
        adb_serial="127.0.0.1:16384",
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl",
    )
    configured_app = create_app(
        settings=configured,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )
    factory = configured_app.state.chat_coordinator.automation_factory
    assert isinstance(factory, RepositoryAndroidAutomationFactory)
    assert factory.repository is configured_app.state.repository
    assert factory.executor is configured_app.state.control_plane.adb_executor
    assert factory.model.endpoint == "http://127.0.0.1:4243/v1"
    assert factory.model.model == "gui-owl"

    disabled_app = create_app(
        settings=replace(configured, gui_executor_enabled=False),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )
    assert disabled_app.state.chat_coordinator.automation_factory is None


def test_runtime_cloud_planner_is_unknown_without_network_probe_when_configured(
    tmp_path: Path,
) -> None:
    base = build_settings(tmp_path)
    configured = replace(
        base,
        cloud_chat_endpoint="https://planner.example/v1",
        cloud_chat_model="planner-model",
        cloud_chat_api_key="process-secret",
    )
    repository = SQLiteRepository(configured.database_path)
    coordinator = ChatCoordinator(
        repository,
        local_provider=ApiProvider(),
        cloud_provider=None,
    )
    app = create_app(
        settings=configured,
        repository=repository,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        chat_coordinator=coordinator,
    )

    with TestClient(app) as client:
        runtime = client.get("/api/v1/runtime").json()

    planner = next(item for item in runtime["capabilities"] if item["id"] == "planner")
    assert planner == {
        "id": "planner",
        "name": "云端规划器",
        "status": "unknown",
        "configured": True,
        "detail": "配置已加载；首次发送时验证连接。",
        "blocker": None,
    }
    assert runtime["overall_status"] == "ready"
