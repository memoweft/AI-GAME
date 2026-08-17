from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ai_game_console.soul_integration import (
    LoopbackSoulHttpTransport,
    SoulHttpResponse,
    SoulIntegration,
    SoulIntegrationError,
    SoulReceiptStore,
    SoulTransportTimeout,
)


class FakeTransport:
    def __init__(self, responses: dict[tuple[str, str], SoulHttpResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, body: dict | None = None) -> SoulHttpResponse:
        self.calls.append((method, path, body))
        outcome = self.responses[(method, path)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _workspace_transport(*, metrics: SoulHttpResponse | Exception | None = None) -> FakeTransport:
    return FakeTransport(
        {
            ("GET", "/api/status"): SoulHttpResponse(
                200,
                {
                    "running": True,
                    "paused": False,
                    "stop_requested": False,
                    "run_state": "running",
                    "health": "running",
                    "platform": "soul",
                    "mode": "auto",
                    "operation_mode": "chat",
                    "health": {
                        "status": "running",
                        "current_action": "polling",
                        "heartbeat_at": "2026-08-09T12:00:00Z",
                        "scheduler_heartbeat_at": "2026-08-09T12:00:01Z",
                        "pause_reason": None,
                        "last_error": None,
                        "last_success": "2026-08-09T12:00:00Z",
                        "next_retry_at": None,
                        "forbidden": "no",
                    },
                    "pending_count": 2,
                    "pending_inbound_count": 3,
                    "human_count": 4,
                    "send_verifying_count": 5,
                    "today_matches": 6,
                    "today_match_acquisitions": 7,
                    "llm_calls": 8,
                    "llm_limit": 99,
                    "send_verification_counts": {"active": 1, "backoff": 2},
                    "match_balance": {"state": "available", "remaining": 2, "observed_at": "2026-08-09T11:00:00Z", "planet_unlocked": True, "raw": "no"},
                    "logs": ["secret log"],
                    "config": {"secret": True},
                    "current_card": {"identity": "forbidden"},
                },
            ),
            ("GET", "/api/matches"): SoulHttpResponse(
                200,
                [
                    {
                        "id": 7,
                        "platform": "soul",
                        "nickname": "展示名",
                        "status": "active",
                        "last_ts": "2026-08-09T12:00:00Z",
                        "msg_count": 9,
                        "pending_inbound": True,
                        "send_verification_state": "backoff",
                        "bio": "forbidden",
                    }
                ],
            ),
            ("GET", "/api/rankings"): SoulHttpResponse(
                200,
                [
                    {
                        "match_id": 7,
                        "overall_score": 8.1,
                        "compatibility_score": 8.0,
                        "engagement_score": 8.2,
                        "evidence": ["allowed summary"],
                        "summary": "allowed",
                        "confidence": 0.8,
                        "status": "ready",
                        "evaluated_at": "2026-08-09T12:00:00Z",
                        "raw": {"forbidden": True},
                    }
                ],
            ),
            ("GET", "/api/metrics"): metrics
            or SoulHttpResponse(
                200,
                [
                    {
                        "match_id": 7,
                        "active_days": 3,
                        "incoming_count": 4,
                        "response_rate": 0.5,
                        "conversation_depth": 6,
                        "engagement_state": "active",
                        "calculated_at": "2026-08-09T12:00:00Z",
                        "raw": "forbidden",
                    }
                ],
            ),
            ("GET", "/api/automation/jobs"): SoulHttpResponse(
                200,
                [
                    {
                        "name": "follow_up",
                        "status": "idle",
                        "last_run": "2026-08-09T12:00:00Z",
                        "last_error": "allowed",
                        "config": {"forbidden": True},
                    }
                ],
            ),
        }
    )


def test_workspace_whitelists_status_and_optional_sections() -> None:
    integration = SoulIntegration(_workspace_transport(), console_url="http://127.0.0.1:5000")

    workspace = integration.workspace()

    assert workspace == {
        "connection": "ready",
        "console_url": "http://127.0.0.1:5000",
        "observed_at": "2026-08-09T12:00:00Z",
        "available_commands": ["pause", "stop", "queue_inventory", "match_mode"],
        "status": {
            "platform": "soul",
            "running": True,
            "paused": False,
            "stop_requested": False,
            "run_state": "running",
            "mode": "auto",
            "operation_mode": "chat",
            "health": {
                "status": "running",
                "current_action": "polling",
                "heartbeat_at": "2026-08-09T12:00:00Z",
                "scheduler_heartbeat_at": "2026-08-09T12:00:01Z",
                "pause_reason": None,
                "last_error": None,
                "last_success": "2026-08-09T12:00:00Z",
                "next_retry_at": None,
            },
            "counts": {"pending_count": 2, "pending_inbound_count": 3, "human_count": 4, "send_verifying_count": 5, "today_matches": 6, "today_match_acquisitions": 7, "llm_calls": 8, "llm_limit": 99},
            "send_verification_counts": {"active": 1, "backoff": 2, "manual_required": 0, "orphaned": 0},
            "match_balance": {"state": "available", "remaining": 2, "observed_at": "2026-08-09T11:00:00Z", "planet_unlocked": True},
        },
        "matches": [
            {
                "id": 7,
                "platform": "soul",
                "nickname": "展示名",
                "status": "active",
                "updated_at": "2026-08-09T12:00:00Z",
                "msg_count": 9,
                "pending_inbound": True,
                "send_verification_state": "backoff",
            }
        ],
        "rankings": [
            {
                "match_id": 7,
                "overall_score": 8.1,
                "compatibility_score": 8.0,
                "engagement_score": 8.2,
                "evidence": ["allowed summary"],
                "summary": "allowed",
                "confidence": 0.8,
                "status": "ready",
                "updated_at": "2026-08-09T12:00:00Z",
            }
        ],
        "metrics": [
            {
                "match_id": 7,
                "active_days": 3,
                "incoming_count": 4,
                "response_rate": 0.5,
                "conversation_depth": 6,
                "engagement_state": "active",
                "calculated_at": "2026-08-09T12:00:00Z",
            }
        ],
        "automation_jobs": [
            {
                "name": "follow_up",
                "status": "idle",
                "updated_at": "2026-08-09T12:00:00Z",
                "detail": "allowed",
            }
        ],
        "section_errors": {},
    }


def test_workspace_requires_status_but_degrades_other_sections() -> None:
    transport = _workspace_transport(metrics=SoulTransportTimeout())
    integration = SoulIntegration(transport)

    workspace = integration.workspace()

    assert workspace["metrics"] == []
    assert workspace["section_errors"] == {"metrics": "soul_metrics_unavailable"}

    transport.responses[("GET", "/api/status")] = SoulHttpResponse(503, {"logs": ["secret"]})
    assert integration.workspace() == {
        "connection": "unavailable",
        "console_url": "http://127.0.0.1:5000",
        "observed_at": None,
        "available_commands": [],
        "status": None,
        "matches": [],
        "rankings": [],
        "metrics": [],
        "automation_jobs": [],
        "section_errors": {"status": "soul_status_unavailable"},
    }


def test_command_maps_exactly_deduplicates_and_times_out_without_retry() -> None:
    transport = FakeTransport(
        {
            ("POST", "/api/control"): SoulHttpResponse(200, {"ok": True, "msg": "started"}),
            ("POST", "/api/operation-mode"): SoulHttpResponse(
                200, {"ok": True, "msg": "mode changed"}
            ),
            ("POST", "/api/inventory"): SoulTransportTimeout(),
        }
    )
    integration = SoulIntegration(transport)

    first = integration.command("start", "request-start")
    duplicate = integration.command("start", "request-start")
    chat_mode = integration.command("chat_mode", "request-chat")
    timeout = integration.command("queue_inventory", "request-inventory")
    timeout_duplicate = integration.command("queue_inventory", "request-inventory")

    assert first == duplicate == {
        "command": "start",
        "client_request_id": "request-start",
        "status": "accepted",
        "detail": "Soul 已接受命令请求；最新状态请以工作台为准。",
        "workspace": None,
    }
    assert chat_mode == {
        "command": "chat_mode",
        "client_request_id": "request-chat",
        "status": "accepted",
        "detail": "Soul 已接受命令请求；最新状态请以工作台为准。",
        "workspace": None,
    }
    assert timeout == timeout_duplicate == {
        "command": "queue_inventory",
        "client_request_id": "request-inventory",
        "status": "uncertain",
        "detail": "Soul 命令结果不确定，未自动重试。",
        "workspace": None,
    }
    assert transport.calls == [
        ("POST", "/api/control", {"action": "start"}),
        ("POST", "/api/operation-mode", {"mode": "chat"}),
        ("POST", "/api/inventory", {}),
    ]
    with pytest.raises(SoulIntegrationError, match="^soul_client_request_id_conflict$"):
        integration.command("stop", "request-start")


def test_conversation_is_read_only_whitelisted_transcript_projection() -> None:
    transport = FakeTransport(
        {
            ("GET", "/api/matches/7"): SoulHttpResponse(
                200,
                {
                    "match": {
                        "id": 7,
                        "platform": "soul",
                        "nickname": "展示名",
                        "status": "active",
                        "matched_at": "2026-08-09T12:00:00Z",
                        "card_image": "forbidden",
                    },
                    "messages": [
                        {
                            "role": "them",
                            "content": "你好",
                            "created_at": "2026-08-09T12:00:00Z",
                            "raw": "forbidden",
                        }
                    ],
                    "profile": {"bio": "forbidden"},
                    "analysis": "forbidden",
                    "relationship": {"forbidden": True},
                },
            )
        }
    )

    projection = SoulIntegration(transport).conversation("7")

    assert projection == {
        "id": "7",
        "match": {
            "id": 7,
            "platform": "soul",
            "nickname": "展示名",
            "status": "active",
            "updated_at": "2026-08-09T12:00:00Z",
        },
        "messages": [
            {
                "role": "them",
                "content": "你好",
                "created_at": "2026-08-09T12:00:00Z",
            }
        ],
    }


def test_workspace_derives_only_currently_legal_commands_and_exposes_incompatibility() -> None:
    transport = _workspace_transport()
    integration = SoulIntegration(transport)
    status = transport.responses[("GET", "/api/status")].payload

    status["paused"] = True
    assert integration.workspace()["available_commands"] == ["resume", "stop", "match_mode"]

    status["running"] = False
    status["paused"] = False
    assert integration.workspace()["available_commands"] == ["start", "match_mode"]

    status["running"] = True
    status["stop_requested"] = True
    assert integration.workspace()["available_commands"] == []

    transport.responses[("GET", "/api/status")] = SoulHttpResponse(200, [])
    assert integration.workspace() == {
        "connection": "incompatible",
        "console_url": "http://127.0.0.1:5000",
        "observed_at": None,
        "available_commands": [],
        "status": None,
        "matches": [],
        "rankings": [],
        "metrics": [],
        "automation_jobs": [],
        "section_errors": {"status": "soul_status_incompatible"},
    }


def test_soul_transport_constraints_and_nonleaking_command_receipts() -> None:
    with pytest.raises(ValueError, match="^Soul console URL must use loopback HTTP$"):
        LoopbackSoulHttpTransport("https://example.test", timeout_seconds=1)

    transport = _workspace_transport()
    transport.responses[("GET", "/api/status")].payload["health"]["heartbeat_at"] = None
    workspace = SoulIntegration(transport).workspace()
    assert isinstance(workspace["observed_at"], str)
    assert workspace["observed_at"].endswith("Z")

    secret = "upstream-message-must-not-echo"
    command_transport = FakeTransport(
        {("POST", "/api/control"): SoulHttpResponse(400, {"ok": False, "msg": secret})}
    )
    receipt = SoulIntegration(command_transport).command("start", "nonleaking-request")
    assert receipt["status"] == "rejected"
    assert secret not in str(receipt)


def test_conversation_timeout_is_503() -> None:
    unavailable = SoulIntegration(
        FakeTransport({("GET", "/api/matches/7"): SoulTransportTimeout()})
    )
    with pytest.raises(SoulIntegrationError, match="^soul_conversation_unavailable$") as captured:
        unavailable.conversation("7")
    assert captured.value.status_code == 503



def test_http_transport_never_follows_redirect_to_second_loopback_server() -> None:
    capture_hits = 0

    class CaptureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal capture_hits
            capture_hits += 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            del format, args

    capture = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    capture_thread = threading.Thread(target=capture.serve_forever, daemon=True)
    capture_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{capture.server_port}/capture")
            self.end_headers()

        def log_message(self, format, *args):
            del format, args

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        response = LoopbackSoulHttpTransport(
            f"http://127.0.0.1:{redirect.server_port}", timeout_seconds=1
        ).request("GET", "/api/status")
    finally:
        redirect.shutdown()
        capture.shutdown()
        redirect.server_close()
        capture.server_close()
        redirect_thread.join(timeout=1)
        capture_thread.join(timeout=1)

    assert response.status_code == 302
    assert capture_hits == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("running", 1),
        ("paused", "false"),
        ("stop_requested", None),
        ("operation_mode", "unknown"),
        ("operation_mode", []),
        ("mode", []),
        ("platform", "tantan"),
    ],
)
def test_workspace_rejects_invalid_control_state_before_deriving_commands(key, value) -> None:
    transport = _workspace_transport()
    transport.responses[("GET", "/api/status")].payload[key] = value

    workspace = SoulIntegration(transport).workspace()

    assert workspace["connection"] == "incompatible"
    assert workspace["available_commands"] == []


def test_workspace_rejects_non_string_public_health_fields() -> None:
    transport = _workspace_transport()
    transport.responses[("GET", "/api/status")].payload["health"]["current_action"] = []

    workspace = SoulIntegration(transport).workspace()

    assert workspace["connection"] == "incompatible"


def test_receipt_ledger_survives_restart_and_more_than_256_commands(tmp_path) -> None:
    ledger_path = tmp_path / "soul-integration.db"
    accepted_transport = FakeTransport(
        {("POST", "/api/control"): SoulHttpResponse(200, {"ok": True})}
    )
    first = SoulIntegration(accepted_transport, receipt_store=SoulReceiptStore(ledger_path))
    uncertain_transport = FakeTransport(
        {("POST", "/api/inventory"): SoulTransportTimeout()}
    )
    uncertain_first = SoulIntegration(
        uncertain_transport, receipt_store=SoulReceiptStore(ledger_path)
    )
    uncertain = uncertain_first.command("queue_inventory", "restart-uncertain")
    accepted = first.command("start", "restart-accepted")
    for index in range(257):
        first.command("start", f"durable-{index}")

    class NeverTransport:
        def request(self, method, path, body=None):
            raise AssertionError(f"must not replay persisted command: {method} {path}")

    restarted = SoulIntegration(NeverTransport(), receipt_store=SoulReceiptStore(ledger_path))
    assert restarted.command("queue_inventory", "restart-uncertain") == uncertain
    assert restarted.command("start", "restart-accepted") == accepted
    assert restarted.command("start", "durable-0")["status"] == "accepted"
    with pytest.raises(SoulIntegrationError, match="^soul_client_request_id_conflict$"):
        restarted.command("stop", "restart-accepted")


def test_health_event_objects_are_publicly_sanitized_for_api_response(tmp_path) -> None:
    transport = _workspace_transport()
    health = transport.responses[("GET", "/api/status")].payload["health"]
    health["last_error"] = {
        "at": "2026-08-09T12:03:00Z",
        "type": "TimeoutError",
        "message": "first line\nsecond line",
    }
    health["last_success"] = {"at": "2026-08-09T12:02:00Z", "message": "hidden"}

    workspace = SoulIntegration(transport).workspace()

    assert workspace["status"]["health"]["last_error"] == "first line second line"
    assert workspace["status"]["health"]["last_success"] == "2026-08-09T12:02:00Z"
