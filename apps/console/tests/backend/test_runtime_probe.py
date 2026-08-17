from __future__ import annotations

from pathlib import Path

from ai_game_console.config import Settings
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.service import RuntimeProbe

from conftest import build_settings


def write_model_runtime_config(project_root: Path, *, served_name: str = "gui-owl") -> None:
    config_path = project_root / "config" / "model-runtime.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            (
                "GUI_MODEL_API_KEY=secret-not-for-status",
                "GUI_MODEL_HOST=127.0.0.1",
                "GUI_MODEL_PORT=4243",
                f"GUI_MODEL_SERVED_NAME={served_name}",
            )
        ),
        encoding="utf-8",
    )


def model_capability(snapshot: dict) -> dict:
    return next(item for item in snapshot["capabilities"] if item.id == "model")


def test_runtime_probe_reports_ready_when_configured_model_is_served(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    write_model_runtime_config(settings.project_root)
    calls: list[tuple[str, float, str]] = []

    def transport(url: str, timeout: float, api_key: str) -> dict:
        calls.append((url, timeout, api_key))
        return {"data": [{"id": "gui-owl"}]}

    snapshot = RuntimeProbe(
        settings,
        AdbTargetDiscovery(env={"PATH": ""}),
        model_transport=transport,
    ).snapshot()

    model = model_capability(snapshot)
    assert snapshot["overall_status"] == "ready"
    assert model.status == "ready"
    assert model.blocker is None
    assert calls == [("http://127.0.0.1:4243/v1/models", 0.5, "secret-not-for-status")]
    assert "secret-not-for-status" not in str(snapshot)


def test_runtime_probe_reports_stopped_when_model_endpoint_cannot_be_reached(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    write_model_runtime_config(settings.project_root)

    def transport(url: str, timeout: float, api_key: str) -> dict:
        raise OSError("connection refused")

    model = model_capability(
        RuntimeProbe(
            settings,
            AdbTargetDiscovery(env={"PATH": ""}),
            model_transport=transport,
        ).snapshot()
    )

    assert model.status == "stopped"
    assert model.configured is True
    assert model.blocker == {
        "code": "model_runtime_stopped",
        "message": "模型运行时尚未启动。",
    }


def test_runtime_probe_reports_not_configured_without_model_runtime_config(
    tmp_path: Path,
) -> None:
    settings: Settings = build_settings(tmp_path)
    called = False

    def transport(url: str, timeout: float, api_key: str) -> dict:  # pragma: no cover
        nonlocal called
        called = True
        raise AssertionError("missing configuration must not probe")

    model = model_capability(
        RuntimeProbe(
            settings,
            AdbTargetDiscovery(env={"PATH": ""}),
            model_transport=transport,
        ).snapshot()
    )

    assert model.status == "not_configured"
    assert model.configured is False
    assert called is False
