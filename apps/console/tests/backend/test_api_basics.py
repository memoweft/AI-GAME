from __future__ import annotations

import subprocess
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.config import Settings
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.repository import SQLiteRepository

from conftest import WRITE_HEADERS, build_settings


def test_startup_migrates_and_seeds_idempotently(settings: Settings) -> None:
    first = SQLiteRepository(settings.database_path)
    first.initialize()
    second = SQLiteRepository(settings.database_path)
    second.initialize()

    assert len(second.list_workflows()) == 2
    assert [target.id for target in second.list_targets()] == ["windows-local"]


def test_health_seed_contract_and_local_host_guard(client: TestClient) -> None:
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "ai-game-console",
        "version": "0.1.0",
        "database": "ready",
    }
    assert client.get("/api/v1/health").status_code == 200
    runtime = client.get("/api/v1/runtime").json()
    assert runtime["overall_status"] == "ready"
    assert next(
        capability
        for capability in runtime["capabilities"]
        if capability["id"] == "executor"
    )["status"] == "not_configured"

    workflows = client.get("/api/v1/workflows").json()
    assert workflows["count"] == 2
    by_id = {item["id"]: item for item in workflows["items"]}
    assert by_id["windows-general"]["name"] == "通用 Windows 软件"
    assert by_id["windows-general"]["status"] == "available"
    assert by_id["windows-general"]["target_kinds"] == ["windows"]
    assert set(by_id) == {"windows-general", "android-emulator"}

    targets = client.get("/api/v1/targets").json()
    assert targets["count"] == 1
    assert targets["items"][0]["name"] == "本机 Windows 桌面"
    assert targets["items"][0]["status"] == "ready"
    assert targets["items"][0]["capabilities"] == ["windows_desktop"]

    rejected = client.get("/health", headers={"Host": "evil.example"})
    assert rejected.status_code == 400


def test_all_post_routes_require_console_client_header(client: TestClient) -> None:
    response = client.post(
        "/api/v1/runs",
        json={
            "workflow_id": "windows-general",
            "target_id": "windows-local",
            "instruction": "打开设置",
            "exact_text": None,
            "requires_approval": False,
            "name": None,
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "console_client_required"

    wrong = client.post(
        "/api/v1/targets/discover",
        headers={"X-AI-Game-Client": "other-client"},
    )
    assert wrong.status_code == 403


def test_target_discovery_persists_ready_and_non_ready_states(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    adb = tmp_path / "adb.exe"
    adb.touch()

    def runner(command):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "List of devices attached\n"
                "emulator-5554 device model:Pixel_9 transport_id:1\n"
                "emulator-5556 offline transport_id:2\n"
                "emulator-5558 unauthorized transport_id:3\n"
            ),
            stderr="",
        )

    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(adb_path=adb, runner=runner),
    )
    with TestClient(app) as client:
        result = client.post("/api/v1/targets/discover", headers=WRITE_HEADERS)
        assert result.status_code == 200
        payload = result.json()
        assert payload["discovery"]["adb_status"] == "ready"
        assert payload["discovery"]["device_count"] == 1
        assert {item["status"] for item in payload["items"]} == {"ready"}
        assert any(item["id"] == "windows-local" for item in payload["items"])

        events = client.get("/api/v1/events").json()["items"]
        assert events[0]["type"] == "targets_discovered"
        assert events[0]["level"] == "info"


def test_settings_honor_project_and_data_overrides(tmp_path: Path) -> None:
    project_root = tmp_path / "custom-project"
    data_dir = tmp_path / "custom-data"
    settings = Settings.from_env(
        {
            "AI_GAME_PROJECT_ROOT": str(project_root),
            "AI_GAME_DATA_DIR": str(data_dir),
            "AI_GAME_CONSOLE_SHUTDOWN_TOKEN": "local-test-token",
        }
    )

    assert settings.project_root == project_root.resolve()
    assert settings.database_path == data_dir.resolve() / "console.db"
    assert settings.console_shutdown_token == "local-test-token"
    assert settings.frontend_dist == (
        project_root.resolve() / "apps" / "console" / "frontend" / "dist"
    )


def test_settings_load_explicit_loopback_mobile_role_binding_without_echoing_key(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps({
        "host": "127.0.0.1", "port": 8080, "alias": "qwen-role",
        "api_keys": ["SENTINEL_LOCAL_KEY"],
    }), encoding="utf-8")
    (config_dir / "mobile-role-runtime.env").write_text(
        f"AI_GAME_MOBILE_ROLE_CONFIG_PATH={binding}\n"
        f"AI_GAME_MOBILE_ROLE_CONTROL_SCRIPT={tmp_path / 'control.ps1'}\n",
        encoding="utf-8",
    )

    settings = Settings.from_env({"AI_GAME_PROJECT_ROOT": str(project_root)})

    assert settings.mobile_role_endpoint == "http://127.0.0.1:8080/v1/chat/completions"
    assert settings.mobile_role_model == "qwen-role"
    assert settings.mobile_role_api_key == "SENTINEL_LOCAL_KEY"
    assert settings.mobile_role_control_script == str(tmp_path / "control.ps1")
    assert "SENTINEL" not in repr(settings)
