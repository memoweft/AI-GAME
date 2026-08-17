from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.chat import ChatCompletion, ChatProviderError
from ai_game_console.cloud_config import (
    CloudChatConfiguration,
    CloudConfigError,
    DpapiSecretProtector,
)
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.repository import SQLiteRepository

from conftest import WRITE_HEADERS, build_settings


class FakeProtector:
    prefix = b"fake-protected-v1:"

    def protect(self, value: str) -> bytes:
        return self.prefix + value.encode("utf-8")[::-1]

    def unprotect(self, value: bytes) -> str:
        if not value.startswith(self.prefix):
            raise RuntimeError("invalid fake ciphertext")
        return value.removeprefix(self.prefix)[::-1].decode("utf-8")


class FakeCloudProvider:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def complete(self, messages, *, json_response, is_cancelled):
        self.calls += 1
        return ChatCompletion(
            assistant_text="云端配置已生效",
            provider="cloud-test",
            model=self.snapshot.model,
            execution_goal=None,
        )


class FakeProviderFactory:
    def __init__(self) -> None:
        self.providers: list[FakeCloudProvider] = []

    def __call__(self, snapshot):
        provider = FakeCloudProvider(snapshot)
        self.providers.append(provider)
        return provider


class FailingCloudProvider:
    def complete(self, messages, *, json_response, is_cancelled):
        raise ChatProviderError(
            "cloud_provider_unavailable",
            "连接失败，请检查地址、模型和密钥。",
        )


def failing_provider_factory(_snapshot):
    return FailingCloudProvider()


def wait_for_turn(client: TestClient, session_id: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        transcript = client.get(f"/api/v1/chat/sessions/{session_id}").json()
        if transcript["turns"] and transcript["turns"][-1]["status"] in {
            "completed",
            "failed",
            "cancelled",
        }:
            return transcript
        time.sleep(0.005)
    raise AssertionError("cloud turn did not finish")


def test_cloud_configuration_persists_encrypted_secret_and_reloads(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    factory = FakeProviderFactory()
    configuration = CloudChatConfiguration(
        repository,
        settings,
        protector=FakeProtector(),
        provider_factory=factory,
    )
    configuration.start()

    initial = configuration.public_view()
    assert initial["configured"] is False
    assert initial["revision"] == 0

    secret = "unique-cloud-secret-for-persistence"
    saved = configuration.configure(
        endpoint="https://cloud.example/v1/",
        model="planner-model",
        api_key=secret,
        expected_revision=0,
    )
    assert saved == {
        "endpoint": "https://cloud.example/v1",
        "model": "planner-model",
        "has_api_key": True,
        "configured": True,
        "credential_source": "console",
        "status": "unknown",
        "detail": "配置已保存；首次发送或连接测试时验证。",
        "revision": 1,
        "updated_at": saved["updated_at"],
    }
    assert secret not in repr(configuration)
    assert secret.encode() not in settings.database_path.read_bytes()

    restarted_factory = FakeProviderFactory()
    restarted = CloudChatConfiguration(
        repository,
        settings,
        protector=FakeProtector(),
        provider_factory=restarted_factory,
    )
    restarted.start()
    assert restarted.public_view()["configured"] is True
    assert restarted_factory.providers[-1].snapshot.api_key == secret

    updated = restarted.configure(
        endpoint="https://new.example/v1",
        model="planner-model-2",
        api_key=None,
        expected_revision=1,
    )
    assert updated["revision"] == 2
    assert restarted_factory.providers[-1].snapshot.api_key == secret
    with pytest.raises(CloudConfigError) as stale:
        restarted.configure(
            endpoint="https://stale.example/v1",
            model="stale-model",
            api_key="stale-secret",
            expected_revision=1,
        )
    assert stale.value.code == "cloud_config_changed"

    cleared = restarted.clear(expected_revision=2)
    assert cleared["configured"] is False
    assert cleared["revision"] == 3

    after_clear = CloudChatConfiguration(
        repository,
        replace(
            settings,
            cloud_chat_endpoint="https://startup.example/v1",
            cloud_chat_model="startup-model",
            cloud_chat_api_key="startup-secret",
        ),
        protector=FakeProtector(),
        provider_factory=FakeProviderFactory(),
    )
    after_clear.start()
    assert after_clear.public_view()["configured"] is False


def test_cloud_settings_api_is_redacted_and_hot_enables_chat(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    repository = SQLiteRepository(settings.database_path)
    factory = FakeProviderFactory()
    configuration = CloudChatConfiguration(
        repository,
        settings,
        protector=FakeProtector(),
        provider_factory=factory,
    )
    app = create_app(
        settings=settings,
        repository=repository,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        cloud_configuration=configuration,
    )
    secret = "api-secret-never-return-this"

    with TestClient(app) as client:
        initial = client.get("/api/v1/settings/cloud")
        assert initial.status_code == 200
        assert initial.json()["configured"] is False

        missing_header = client.post(
            "/api/v1/settings/cloud",
            json={
                "endpoint": "https://cloud.example/v1",
                "model": "planner-model",
                "api_key": secret,
                "expected_revision": 0,
            },
        )
        assert missing_header.status_code == 403
        assert secret not in missing_header.text

        invalid = client.post(
            "/api/v1/settings/cloud",
            headers=WRITE_HEADERS,
            json={
                "endpoint": "not-a-url",
                "model": "planner-model",
                "api_key": secret,
                "expected_revision": 0,
            },
        )
        assert invalid.status_code == 400
        assert secret not in invalid.text

        saved = client.post(
            "/api/v1/settings/cloud",
            headers=WRITE_HEADERS,
            json={
                "endpoint": "https://cloud.example/v1",
                "model": "planner-model",
                "api_key": secret,
                "expected_revision": 0,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["configured"] is True
        assert secret not in saved.text
        assert "api_key" not in saved.json()

        planner = next(
            item
            for item in client.get("/api/v1/runtime").json()["capabilities"]
            if item["id"] == "planner"
        )
        assert planner["configured"] is True
        assert planner["status"] == "unknown"

        tested = client.post(
            "/api/v1/settings/cloud/test",
            headers=WRITE_HEADERS,
        )
        assert tested.status_code == 200
        assert tested.json()["ok"] is True
        assert tested.json()["status"] == "ready"

        session_response = client.post(
            "/api/v1/chat/sessions",
            headers=WRITE_HEADERS,
            json={
                "title": "热加载云端会话",
                "mode": "cloud_execute",
                "target_id": None,
                "auto_execute": False,
            },
        )
        assert session_response.status_code == 201
        session = session_response.json()
        turn = client.post(
            f"/api/v1/chat/sessions/{session['id']}/turns",
            headers=WRITE_HEADERS,
            json={"content": "你好", "client_request_id": "cloud-config-1"},
        )
        assert turn.status_code == 202
        transcript = wait_for_turn(client, session["id"])
        assert transcript["messages"][-1]["content"] == "云端配置已生效"
        assert transcript["messages"][-1]["model"] == "planner-model"

        cleared = client.post(
            "/api/v1/settings/cloud/clear",
            headers=WRITE_HEADERS,
            json={"expected_revision": 1},
        )
        assert cleared.status_code == 200
        assert cleared.json()["configured"] is False
        assert client.get(f"/api/v1/chat/sessions/{session['id']}").status_code == 200
        rejected = client.post(
            "/api/v1/chat/sessions",
            headers=WRITE_HEADERS,
            json={
                "title": "不能创建",
                "mode": "cloud_execute",
                "target_id": None,
                "auto_execute": False,
            },
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "cloud_model_not_configured"


def test_cloud_config_validation_never_reflects_rejected_key(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    configuration = CloudChatConfiguration(
        SQLiteRepository(settings.database_path),
        settings,
        protector=FakeProtector(),
        provider_factory=FakeProviderFactory(),
    )
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        cloud_configuration=configuration,
    )
    secret = "validation-secret-never-echo"
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/settings/cloud",
            headers=WRITE_HEADERS,
            json={
                "endpoint": "https://cloud.example/v1",
                "model": "planner-model",
                "api_key": secret,
                "expected_revision": -1,
            },
        )
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_cloud_chat_config",
            "message": "云端模型配置无效。",
        }
    }
    assert secret not in response.text


def test_failed_connection_is_reported_without_clearing_saved_config(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    configuration = CloudChatConfiguration(
        repository,
        settings,
        protector=FakeProtector(),
        provider_factory=failing_provider_factory,
    )
    configuration.start()
    configuration.configure(
        endpoint="https://unavailable.example/v1",
        model="planner-model",
        api_key="failure-test-secret",
        expected_revision=0,
    )

    result = configuration.test_connection()
    view = configuration.public_view()

    assert result.ok is False
    assert result.status == "error"
    assert result.detail == "连接失败，请检查地址、模型和密钥。"
    assert view["configured"] is True
    assert view["status"] == "error"
    assert view["has_api_key"] is True
    assert "failure-test-secret" not in repr(view)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI production adapter")
def test_windows_dpapi_round_trip_does_not_store_plaintext() -> None:
    protector = DpapiSecretProtector()
    secret = "dpapi-round-trip-secret"
    protected = protector.protect(secret)
    assert secret.encode("utf-8") not in protected
    assert protector.unprotect(protected) == secret


def test_cloud_config_schema_v4_preserves_existing_chat_tables(tmp_path: Path) -> None:
    database = tmp_path / "console.db"
    repository = SQLiteRepository(database)
    repository.initialize()
    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cloud_chat_config'"
        ).fetchone()
    assert version == 4
    assert table == ("cloud_chat_config",)
