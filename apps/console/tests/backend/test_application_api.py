from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.application_runtime import Input, Pause, Resume, Stop
from ai_game_console.application_runtime.domain import (
    IdempotencyConflict,
    QueueFull,
    RuntimeClosed,
    RuntimeNotFound,
)
from ai_game_console.discovery import AdbTargetDiscovery

from conftest import WRITE_HEADERS, build_settings


PRIVATE = "SENTINEL_identity_message_draft_screenshot_hierarchy_prompt_owner"


def _instance(*, status: str = "running", revision: int = 1) -> dict[str, object]:
    return {
        "instance_id": "instance-1",
        "profile_id": "soul-reply-v1",
        "target_id": "private-target-identity",
        "initial_input": PRIVATE,
        "status": status,
        "revision": revision,
        "degraded": True,
        "hard_risk": False,
        "detail": PRIVATE,
        "error_code": None,
        "memory_version": 2,
        "inputs": (PRIVATE,),
        "intents": (
            {
                "intent_id": "intent-1",
                "cycle": 3,
                "revision": revision,
                "intent": {
                    "name": "reply",
                    "arguments": {
                        "identity": PRIVATE,
                        "message": PRIVATE,
                        "draft": PRIVATE,
                        "screenshot": PRIVATE,
                        "hierarchy": PRIVATE,
                        "prompt": PRIVATE,
                        "owner": PRIVATE,
                    },
                    "hard_risk": False,
                },
                "phase": "dispatched",
                "reservation_id": PRIVATE,
                "receipt": {
                    "receipt_id": PRIVATE,
                    "accepted": True,
                    "detail": PRIVATE,
                },
                "created_at": "2026-08-10T01:00:01Z",
                "finalized_at": "2026-08-10T01:00:02Z",
            },
        ),
        "outcomes": (
            {
                "cycle": 3,
                "status": "confirmed_success",
                "evidence": PRIVATE,
                "hard_risk": False,
                "terminal": False,
                "after_evidence_id": PRIVATE,
                "created_at": "2026-08-10T01:00:03Z",
            },
        ),
        "events": (
            {
                "sequence": 1,
                "event_type": "cycle_finished",
                "data": {"owner_payload": PRIVATE},
                "created_at": "2026-08-10T01:00:03Z",
            },
        ),
        "created_at": "2026-08-10T01:00:00Z",
        "updated_at": "2026-08-10T01:00:03Z",
        "finished_at": None,
        "wake_at": "2026-08-10T01:05:00Z",
    }


class FakeApplicationRuntime:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.command_calls: list[dict[str, object]] = []
        self.shutdown_calls = 0

    def start(
        self,
        profile_id: str,
        client_request_id: str,
        target_id: str | None = None,
        initial_input: str | None = None,
    ):
        self.start_calls.append(
            {
                "profile_id": profile_id,
                "client_request_id": client_request_id,
                "target_id": target_id,
                "initial_input": initial_input,
            }
        )
        return _instance()

    def command(self, instance_id: str, command, client_request_id: str):
        self.command_calls.append(
            {
                "instance_id": instance_id,
                "command": command,
                "client_request_id": client_request_id,
            }
        )
        return _instance(
            status={
                "Pause": "paused",
                "Resume": "queued",
                "Stop": "stopping",
            }.get(command.tag, "running"),
            revision=2 if command.tag == "Input" else 1,
        )

    def inspect(self, instance_id: str):
        if instance_id == "missing":
            raise RuntimeNotFound(PRIVATE)
        return _instance()

    def list(self, limit: int = 100):
        return [_instance()][:limit]

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _app(tmp_path: Path, *, runtime=None, archive=None):
    return create_app(
        settings=build_settings(tmp_path),
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        application_runtime=runtime,
        application_runtime_archive=archive,
    )


def _assert_private_payload_absent(response) -> None:
    assert PRIVATE not in response.text
    lowered = response.text.lower()
    for forbidden in (
        "identity",
        "message",
        "draft",
        "screenshot",
        "hierarchy",
        "prompt",
        "owner_payload",
        "arguments",
        "reservation_id",
        "receipt",
        "after_evidence_id",
    ):
        assert forbidden not in lowered


def test_application_instance_routes_use_injected_runtime_and_safe_projection(
    tmp_path: Path,
) -> None:
    runtime = FakeApplicationRuntime()
    app = _app(tmp_path, runtime=runtime)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/application-instances",
            headers=WRITE_HEADERS,
            json={
                "profile_id": "  soul-reply-v1  ",
                "client_request_id": "application-start-1",
                "target_id": "  adb:tablet-1  ",
                "initial_input": f"  {PRIVATE}  ",
            },
        )
        listed = client.get("/api/v1/application-instances?limit=20")
        inspected = client.get("/api/v1/application-instances/instance-1")

    assert accepted.status_code == 202
    assert accepted.json() == {
        "id": "instance-1",
        "profile_id": "soul-reply-v1",
        "status": "running",
        "revision": 1,
        "degraded": True,
        "hard_risk": False,
        "error_code": None,
        "memory_version": 2,
        "input_count": 1,
        "intent_count": 1,
        "outcome_count": 1,
        "event_count": 1,
        "intents": [
            {
                "id": "intent-1",
                "cycle": 3,
                "revision": 1,
                "phase": "dispatched",
                "hard_risk": False,
                "created_at": "2026-08-10T01:00:01Z",
                "finalized_at": "2026-08-10T01:00:02Z",
            }
        ],
        "outcomes": [
            {
                "cycle": 3,
                "status": "confirmed_success",
                "hard_risk": False,
                "terminal": False,
                "created_at": "2026-08-10T01:00:03Z",
            }
        ],
        "created_at": "2026-08-10T01:00:00Z",
        "updated_at": "2026-08-10T01:00:03Z",
        "finished_at": None,
        "wake_at": "2026-08-10T01:05:00Z",
    }
    assert runtime.start_calls == [
        {
            "profile_id": "soul-reply-v1",
            "client_request_id": "application-start-1",
            "target_id": "adb:tablet-1",
            "initial_input": PRIVATE,
        }
    ]
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert inspected.status_code == 200
    for response in (accepted, listed, inspected):
        _assert_private_payload_absent(response)
    assert runtime.shutdown_calls == 1


@pytest.mark.parametrize(
    ("payload", "command_type", "content", "expected_status"),
    [
        (
            {
                "command": "Input",
                "content": "  中途补充一句  ",
                "client_request_id": "command-input-1",
            },
            Input,
            "中途补充一句",
            "running",
        ),
        (
            {"command": "Pause", "client_request_id": "command-pause-1"},
            Pause,
            None,
            "paused",
        ),
        (
            {"command": "Resume", "client_request_id": "command-resume-1"},
            Resume,
            None,
            "queued",
        ),
        (
            {"command": "Stop", "client_request_id": "command-stop-1"},
            Stop,
            None,
            "stopping",
        ),
    ],
)
def test_application_command_route_builds_domain_command(
    tmp_path: Path,
    payload: dict[str, str],
    command_type,
    content: str | None,
    expected_status: str,
) -> None:
    runtime = FakeApplicationRuntime()
    app = _app(tmp_path, runtime=runtime)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/application-instances/instance-1/commands",
            headers=WRITE_HEADERS,
            json=payload,
        )

    assert response.status_code == 202
    assert response.json()["status"] == expected_status
    received = runtime.command_calls[0]
    assert received["instance_id"] == "instance-1"
    assert received["client_request_id"] == payload["client_request_id"]
    assert isinstance(received["command"], command_type)
    assert getattr(received["command"], "content", None) == content


def test_application_history_uses_archive_when_execution_runtime_is_unavailable(
    tmp_path: Path,
) -> None:
    archive = FakeApplicationRuntime()
    app = _app(tmp_path, runtime=None, archive=archive)

    with TestClient(app) as client:
        listed = client.get("/api/v1/application-instances")
        inspected = client.get("/api/v1/application-instances/instance-1")
        unavailable = client.post(
            "/api/v1/application-instances",
            headers=WRITE_HEADERS,
            json={
                "profile_id": "soul-reply-v1",
                "client_request_id": "offline-start-1",
            },
        )

    assert listed.status_code == 200
    assert inspected.status_code == 200
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == (
        "application_runtime_not_configured"
    )
    assert archive.start_calls == []


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (RuntimeNotFound(PRIVATE), 404, "application_runtime_not_found"),
        (
            IdempotencyConflict(PRIVATE),
            409,
            "application_runtime_request_id_conflict",
        ),
        (QueueFull(PRIVATE), 429, "application_runtime_queue_full"),
        (RuntimeClosed(PRIVATE), 503, "application_runtime_closed"),
    ],
)
def test_application_runtime_errors_have_stable_redacted_mapping(
    tmp_path: Path, error, status_code: int, code: str
) -> None:
    class RaisingRuntime(FakeApplicationRuntime):
        def start(self, *args, **kwargs):
            del args, kwargs
            raise error

    app = _app(tmp_path, runtime=RaisingRuntime())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/application-instances",
            headers=WRITE_HEADERS,
            json={
                "profile_id": "soul-reply-v1",
                "client_request_id": "stable-error-1",
            },
        )

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert PRIVATE not in response.text


def test_invalid_application_request_is_redacted(tmp_path: Path) -> None:
    app = _app(tmp_path, runtime=FakeApplicationRuntime())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/application-instances/instance-1/commands",
            headers=WRITE_HEADERS,
            json={
                "command": "Pause",
                "content": PRIVATE,
                "client_request_id": "invalid request id",
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_application_runtime_request",
            "message": "Application 运行请求无效。",
        }
    }
    assert PRIVATE not in response.text


@pytest.mark.parametrize(
    ("operation", "error", "expected_code"),
    [
        (
            "start",
            sqlite3.OperationalError(PRIVATE),
            "application_runtime_start_unavailable",
        ),
        (
            "command",
            OSError(PRIVATE),
            "application_runtime_command_unavailable",
        ),
    ],
)
def test_application_writer_infrastructure_failures_are_stable_503_and_redacted(
    tmp_path: Path,
    operation: str,
    error: Exception,
    expected_code: str,
) -> None:
    class InfrastructureFailureRuntime(FakeApplicationRuntime):
        def start(self, *args, **kwargs):
            del args, kwargs
            if operation == "start":
                raise error
            return super().start("soul-reply-v1", "fallback-start")

        def command(self, *args, **kwargs):
            del args, kwargs
            if operation == "command":
                raise error
            return _instance()

    app = _app(tmp_path, runtime=InfrastructureFailureRuntime())
    with TestClient(app) as client:
        if operation == "start":
            response = client.post(
                "/api/v1/application-instances",
                headers=WRITE_HEADERS,
                json={
                    "profile_id": "soul-reply-v1",
                    "client_request_id": "infra-start-1",
                },
            )
        else:
            response = client.post(
                "/api/v1/application-instances/instance-1/commands",
                headers=WRITE_HEADERS,
                json={
                    "command": "Pause",
                    "client_request_id": "infra-command-1",
                },
            )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": expected_code,
            "message": "Application 运行时当前不可用。",
        }
    }
    assert PRIVATE not in response.text


def test_application_archive_infrastructure_failure_is_stable_503_and_redacted(
    tmp_path: Path,
) -> None:
    class BrokenArchive(FakeApplicationRuntime):
        def list(self, limit: int = 100):
            del limit
            raise OSError(PRIVATE)

    app = _app(tmp_path, runtime=None, archive=BrokenArchive())
    with TestClient(app) as client:
        response = client.get("/api/v1/application-instances")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "application_runtime_history_unavailable",
            "message": "Application 运行历史当前不可用。",
        }
    }
    assert PRIVATE not in response.text
