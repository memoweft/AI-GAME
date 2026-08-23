from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from ai_game_console.production_release import (
    MANIFEST_NAME,
    ReleaseBuildError,
    build_release_candidate,
    verify_release_candidate,
)


def _release_project(root: Path) -> Path:
    project = root / "project"
    files = {
        "apps/console/backend/pyproject.toml": """[project]\nname = \"ai-game-console-backend\"\nversion = \"0.1.0\"\n""",
        "apps/console/backend/ai_game_console/__init__.py": "",
        "apps/console/backend/ai_game_console/main.py": "def run(): pass\n",
        "apps/console/backend/ai_game_console/application_runtime/store.py": "",
        "apps/console/backend/ai_game_console/experience_runtime/store.py": "",
        "apps/console/backend/ai_game_console/goal_runtime/store.py": "",
        "apps/console/backend/ai_game_console/mobile_agent/store.py": "",
        "apps/console/backend/ai_game_console/runtime_adapters/sqlite/store.py": "",
        "apps/console/frontend/index.html": "<div id=\"root\"></div>\n",
        "apps/console/frontend/package.json": "{}\n",
        "apps/console/frontend/package-lock.json": "{}\n",
        "apps/console/frontend/tsconfig.json": "{}\n",
        "apps/console/frontend/vite.config.ts": "export default {}\n",
        "apps/console/frontend/src/App.tsx": "export const App = () => null\n",
        "apps/console/frontend/dist/index.html": "<div id=\"root\"></div>\n",
        "config/model-runtime.env.example": "GUI_MODEL_HOST=127.0.0.1\n",
        "config/console.env.example": "AI_GAME_CONSOLE_PORT=4310\n",
        "scripts/console.ps1": "param()\n",
        "scripts/production-release.ps1": "param()\n",
        "启动控制台.cmd": "@echo off\n",
        "停止控制台.cmd": "@echo off\n",
        "README.md": "# AI-GAME\n",
    }
    for document in (
        "01_PRODUCT_SPEC.md",
        "04_AUTONOMY_AND_LEARNING.md",
        "06_IMPLEMENTATION_ROADMAP.md",
        "07_ACCEPTANCE_AND_EVIDENCE.md",
        "09_DECISIONS_AND_OPEN_QUESTIONS.md",
        "work-orders/U9_PRODUCTION_HARDENING.md",
    ):
        files[f"docs/product/{document}"] = f"# {document}\n"
    for relative, content in files.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # Make the fixture frontend bundle meaningfully newer than its source.
    dist = project / "apps/console/frontend/dist/index.html"
    os.utime(dist, ns=(dist.stat().st_atime_ns, dist.stat().st_mtime_ns + 5_000_000))
    return project


def test_release_candidate_contains_only_deployable_inputs_and_verifies(tmp_path: Path) -> None:
    project = _release_project(tmp_path)
    secret = project / "config/model-runtime.env"
    secret.write_text("GUI_MODEL_API_KEY=private-value\n", encoding="utf-8")
    runtime = project / "runtime/console/console.db"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"private runtime")

    artifact = build_release_candidate(project, tmp_path / "releases")
    verified = verify_release_candidate(artifact.archive)

    assert artifact.archive.is_file()
    assert artifact.manifest.is_file()
    assert artifact.checksum.is_file()
    assert verified["verified"] is True
    assert verified["deployment_status"] == "NOT_DEPLOYED"
    with zipfile.ZipFile(artifact.archive) as bundle:
        manifest = json.loads(bundle.read(MANIFEST_NAME))
        names = set(bundle.namelist())
    assert manifest["artifact_kind"] == "local_installation_candidate"
    assert manifest["source_control"]["state"] == "unavailable"
    assert manifest["included_schema_sources"] == [
        "apps/console/backend/ai_game_console/application_runtime/store.py",
        "apps/console/backend/ai_game_console/experience_runtime/store.py",
        "apps/console/backend/ai_game_console/goal_runtime/store.py",
        "apps/console/backend/ai_game_console/mobile_agent/store.py",
        "apps/console/backend/ai_game_console/runtime_adapters/sqlite/store.py",
    ]
    assert "config/model-runtime.env.example" in names
    assert "config/model-runtime.env" not in names
    assert "runtime/console/console.db" not in names
    assert "apps/console/frontend/src/App.tsx" in names
    assert "apps/console/frontend/dist/index.html" in names


def test_release_candidate_refuses_stale_frontend_or_artifact_overwrite(tmp_path: Path) -> None:
    project = _release_project(tmp_path)
    source = project / "apps/console/frontend/src/App.tsx"
    dist = project / "apps/console/frontend/dist/index.html"
    os.utime(source, ns=(source.stat().st_atime_ns, dist.stat().st_mtime_ns + 5_000_000))

    with pytest.raises(ReleaseBuildError, match="older than its source"):
        build_release_candidate(project, tmp_path / "releases")

    os.utime(dist, ns=(dist.stat().st_atime_ns, source.stat().st_mtime_ns + 5_000_000))
    build_release_candidate(project, tmp_path / "releases")
    with pytest.raises(ReleaseBuildError, match="will not be overwritten"):
        build_release_candidate(project, tmp_path / "releases")


def test_release_candidate_detects_tampering(tmp_path: Path) -> None:
    project = _release_project(tmp_path)
    artifact = build_release_candidate(project, tmp_path / "releases")

    with zipfile.ZipFile(artifact.archive, "a") as bundle:
        bundle.writestr("unexpected.txt", "tampered")

    with pytest.raises(ReleaseBuildError, match="checksum"):
        verify_release_candidate(artifact.archive)
