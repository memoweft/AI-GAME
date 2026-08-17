from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ai_game_console.api import CONSOLE_SHUTDOWN_TOKEN_HEADER, create_app

from conftest import WRITE_HEADERS, build_settings


def test_console_shutdown_requires_console_header_and_ephemeral_token(tmp_path) -> None:
    token = "a" * 32
    calls: list[str] = []
    app = create_app(
        settings=replace(build_settings(tmp_path), console_shutdown_token=token),
        console_shutdown_callback=lambda: calls.append("requested"),
    )
    authorized_headers = {
        **WRITE_HEADERS,
        CONSOLE_SHUTDOWN_TOKEN_HEADER: token,
    }

    with TestClient(app) as client:
        missing_client_header = client.post("/api/v1/shutdown")
        missing_token = client.post("/api/v1/shutdown", headers=WRITE_HEADERS)
        wrong_token = client.post(
            "/api/v1/shutdown",
            headers={
                **WRITE_HEADERS,
                CONSOLE_SHUTDOWN_TOKEN_HEADER: "wrong-token",
            },
        )
        accepted = client.post("/api/v1/shutdown", headers=authorized_headers)
        method_not_allowed = client.get("/api/v1/shutdown")

    assert missing_client_header.status_code == 403
    assert missing_client_header.json()["error"]["code"] == "console_client_required"
    for forbidden in (missing_token, wrong_token):
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "console_shutdown_forbidden"
    assert accepted.status_code == 202
    assert accepted.json() == {"status": "accepted"}
    assert calls == ["requested"]
    # The SPA GET fallback owns unmatched browser paths before FastAPI's method
    # matcher can return 405, so the shutdown route remains non-discoverable by GET.
    assert method_not_allowed.status_code == 404


def test_console_shutdown_requires_a_launcher_callback(tmp_path) -> None:
    app = create_app(
        settings=replace(
            build_settings(tmp_path), console_shutdown_token="b" * 32
        ),
    )
    headers = {
        **WRITE_HEADERS,
        CONSOLE_SHUTDOWN_TOKEN_HEADER: "b" * 32,
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/shutdown", headers=headers)

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "console_shutdown_unavailable",
            "message": "当前控制台不支持优雅关闭。",
        }
    }
