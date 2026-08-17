from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.config import Settings
from ai_game_console.discovery import AdbTargetDiscovery


WRITE_HEADERS = {"X-AI-Game-Client": "console-v1"}


def build_settings(root: Path, *, frontend: bool = False) -> Settings:
    project_root = root / "project"
    data_dir = root / "data"
    frontend_dist = project_root / "apps" / "console" / "frontend" / "dist"
    if frontend:
        frontend_dist.mkdir(parents=True, exist_ok=True)
    return Settings(
        project_root=project_root,
        data_dir=data_dir,
        database_path=data_dir / "console.db",
        frontend_dist=frontend_dist,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )
    with TestClient(app) as test_client:
        yield test_client

