from __future__ import annotations

from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.soul_integration import SoulHttpResponse, SoulIntegration, SoulTransportTimeout

from conftest import WRITE_HEADERS, build_settings
from test_soul_integration import FakeTransport, _workspace_transport


def test_soul_routes_keep_read_diagnostics_and_disable_all_legacy_writes(tmp_path) -> None:
    transport = _workspace_transport()
    transport.responses[("GET", "/api/matches/7")] = SoulHttpResponse(
        200,
        {
                "match": {"id": 7, "platform": "soul", "nickname": "展示名", "status": "active", "matched_at": "now"},
            "messages": [],
        },
    )
    app = create_app(
        settings=build_settings(tmp_path),
        soul_integration=SoulIntegration(transport),
    )

    with TestClient(app) as client:
        workspace = client.get("/api/v1/integrations/soul")
        transcript = client.get("/api/v1/integrations/soul/conversations/7")
        missing_header = client.post(
            "/api/v1/integrations/soul/commands",
            json={"command": "start", "client_request_id": "soul-api-1"},
        )
        disabled_control = client.post(
            "/api/v1/integrations/soul/commands",
            headers=WRITE_HEADERS,
            json={"command": "start", "client_request_id": "soul-api-1"},
        )
        disabled_inventory = client.post(
            "/api/v1/soul/commands",
            headers=WRITE_HEADERS,
            json={
                "command": "queue_inventory",
                "client_request_id": "soul-api-inventory-1",
            },
        )

    assert workspace.status_code == 200
    assert "logs" not in workspace.text
    assert workspace.json()["available_commands"] == []
    assert transcript.json() == {
        "id": "7",
        "match": {"id": 7, "platform": "soul", "nickname": "展示名", "status": "active", "updated_at": "now"},
        "messages": [],
    }
    assert missing_header.status_code == 403
    for disabled in (disabled_control, disabled_inventory):
        assert disabled.status_code == 410
        assert disabled.json() == {
            "error": {
                "code": "legacy_soul_write_disabled",
                "message": "旧 Soul 写入口已停用，请使用 ApplicationRuntime。",
            }
        }
    assert all(method == "GET" for method, _path, _body in transport.calls)


def test_soul_command_validation_is_redacted_on_both_legacy_paths(tmp_path) -> None:
    app = create_app(
        settings=build_settings(tmp_path),
        soul_integration=SoulIntegration(FakeTransport({})),
    )
    sentinel = "SENTINEL_SOUL_COMMAND"
    with TestClient(app) as client:
        for path in (
            "/api/v1/soul/commands",
            "/api/v1/integrations/soul/commands",
        ):
            invalid = client.post(
                path,
                headers=WRITE_HEADERS,
                json={"command": sentinel, "client_request_id": "soul-api-1"},
            )
            assert invalid.status_code == 422
            assert invalid.json() == {
                "error": {
                    "code": "invalid_legacy_soul_request",
                    "message": "旧 Soul 写请求无效。",
                }
            }
            assert sentinel not in invalid.text


def test_soul_workspace_is_a_200_offline_snapshot(tmp_path) -> None:
    app = create_app(
        settings=build_settings(tmp_path),
        soul_integration=SoulIntegration(
            FakeTransport({("GET", "/api/status"): SoulTransportTimeout()})
        ),
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/integrations/soul")
    assert response.status_code == 200
    assert response.json()["connection"] == "unavailable"
    assert response.json()["status"] is None
    assert response.json()["available_commands"] == []


def test_soul_api_accepts_real_health_event_shapes(tmp_path) -> None:
    transport = _workspace_transport()
    transport.responses[("GET", "/api/status")].payload["health"].update(
        {
            "last_error": {"at": "2026-08-09T12:03:00Z", "type": "Timeout", "message": "a\nb"},
            "last_success": {"at": "2026-08-09T12:02:00Z", "message": "hidden"},
        }
    )
    app = create_app(
        settings=build_settings(tmp_path),
        soul_integration=SoulIntegration(transport),
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/integrations/soul")
    assert response.status_code == 200
    assert response.json()["status"]["health"]["last_error"] == "a b"
    assert response.json()["status"]["health"]["last_success"] == "2026-08-09T12:02:00Z"
