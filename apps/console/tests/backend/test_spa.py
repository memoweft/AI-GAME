from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.discovery import AdbTargetDiscovery

from conftest import build_settings


def test_missing_frontend_returns_clear_503_but_api_still_wins(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )

    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 503
        assert root.json()["error"]["code"] == "frontend_not_built"
        assert client.get("/api/v1/health").status_code == 200
        missing_api = client.get("/api/v1/not-a-route")
        assert missing_api.status_code == 404
        assert missing_api.json()["error"]["code"] == "api_route_not_found"


def test_frontend_files_and_spa_routes_are_served_same_origin(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, frontend=True)
    index = settings.frontend_dist / "index.html"
    assets = settings.frontend_dist / "assets"
    assets.mkdir()
    index.write_text("<!doctype html><title>AI-GAME</title>", encoding="utf-8")
    (assets / "app.js").write_text("window.aiGame = true;", encoding="utf-8")

    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )
    with TestClient(app) as client:
        assert client.get("/").text == index.read_text(encoding="utf-8")
        assert client.get("/runs/some-client-route").text == index.read_text(
            encoding="utf-8"
        )
        assert client.get("/assets/app.js").text == "window.aiGame = true;"
        assert client.get("/health").headers["content-type"].startswith("application/json")

