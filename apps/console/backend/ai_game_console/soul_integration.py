from __future__ import annotations

import json
import socket
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


SoulCommand = Literal[
    "start",
    "pause",
    "resume",
    "stop",
    "queue_inventory",
    "chat_mode",
    "match_mode",
]

AVAILABLE_COMMANDS: tuple[SoulCommand, ...] = (
    "start",
    "pause",
    "resume",
    "stop",
    "queue_inventory",
    "chat_mode",
    "match_mode",
)
MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class SoulHttpResponse:
    status_code: int
    payload: Any


class SoulTransport(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> SoulHttpResponse: ...


class SoulTransportTimeout(RuntimeError):
    """No response was received before the configured upstream timeout."""


class SoulTransportUnavailable(RuntimeError):
    """The configured loopback Soul console could not be reached."""


class SoulIntegrationError(RuntimeError):
    def __init__(self, *, code: str, message: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}


class SoulReceiptStore:
    """Durable, crash-conservative command receipts for one AI-GAME console."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        else:
            self._memory_connection = sqlite3.connect(
                self._path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._memory_connection.row_factory = sqlite3.Row
        self._initialize()

    def reserve_or_get(self, command: str, client_request_id: str) -> dict[str, Any] | None:
        """Atomically create an uncertain receipt before any upstream POST."""

        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT command, client_request_id, status, detail FROM soul_command_receipts "
                "WHERE client_request_id=?",
                (client_request_id,),
            ).fetchone()
            if row is not None:
                return _stored_receipt(row)
            receipt = _command_receipt(command, client_request_id, "uncertain")
            connection.execute(
                "INSERT INTO soul_command_receipts "
                "(client_request_id, command, status, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    client_request_id,
                    command,
                    receipt["status"],
                    receipt["detail"],
                    now,
                    now,
                ),
            )
            return None

    def update(self, receipt: dict[str, Any]) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE soul_command_receipts SET status=?, detail=?, updated_at=? "
                "WHERE client_request_id=? AND command=?",
                (
                    receipt["status"],
                    receipt["detail"],
                    _utc_now(),
                    receipt["client_request_id"],
                    receipt["command"],
                ),
            )

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > self.SCHEMA_VERSION:
                raise RuntimeError("soul_receipt_store_version_unsupported")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS soul_command_receipts ("
                "client_request_id TEXT PRIMARY KEY, "
                "command TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "detail TEXT NOT NULL, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
            connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    @contextmanager
    def _connection(self):
        connection = self._memory_connection
        owns_connection = connection is None
        if connection is None:
            connection = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
        try:
            yield connection
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        else:
            if connection.in_transaction:
                connection.commit()
        finally:
            if owns_connection:
                connection.close()


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        del request, fp, code, msg, headers, newurl
        return None


class LoopbackSoulHttpTransport:
    """One shell-free, non-retrying HTTP adapter for a local Soul console."""

    def __init__(self, base_url: str, *, timeout_seconds: float, opener: Any | None = None) -> None:
        self._base_url = _validated_loopback_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._opener = opener or build_opener(_NoRedirectHandler())

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> SoulHttpResponse:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                return SoulHttpResponse(response.status, _json_payload(_read_limited(response)))
        except HTTPError as error:
            return SoulHttpResponse(error.code, _json_payload(_read_limited(error)))
        except (TimeoutError, socket.timeout):
            raise SoulTransportTimeout() from None
        except (URLError, OSError):
            raise SoulTransportUnavailable() from None


class SoulIntegration:
    """Deep module for a small, privacy-filtered Soul console interface.

    ``workspace()``, ``conversation()`` and ``command()`` are the only public
    interface. They own upstream endpoint selection, field filtering, partial
    read degradation and command idempotency so AI-GAME routes never proxy raw
    dating-copilot payloads.
    """

    def __init__(
        self,
        transport: SoulTransport,
        *,
        console_url: str = "http://127.0.0.1:5000",
        receipt_store: SoulReceiptStore | None = None,
    ) -> None:
        self._transport = transport
        self._console_url = _validated_loopback_url(console_url)
        self._receipt_store = receipt_store or SoulReceiptStore()

    @classmethod
    def from_settings(cls, settings: Any) -> "SoulIntegration":
        return cls(
            LoopbackSoulHttpTransport(
                settings.soul_console_url,
                timeout_seconds=settings.soul_request_timeout_seconds,
            ),
            console_url=settings.soul_console_url,
            receipt_store=SoulReceiptStore(settings.data_dir / "soul-integration.db"),
        )

    def workspace(self) -> dict[str, Any]:
        try:
            status_response = self._request("GET", "/api/status")
        except (SoulTransportTimeout, SoulTransportUnavailable):
            return _unavailable_workspace("unavailable", "soul_status_unavailable", self._console_url)
        if not _is_success(status_response):
            return _unavailable_workspace("unavailable", "soul_status_unavailable", self._console_url)
        if not _status_is_compatible(status_response.payload):
            return _unavailable_workspace("incompatible", "soul_status_incompatible", self._console_url)
        status = _status_projection(status_response.payload)
        sections: dict[str, list[dict[str, Any]]] = {}
        section_errors: dict[str, str] = {}
        for name, path, projector in (
            ("matches", "/api/matches", _matches_projection),
            ("rankings", "/api/rankings", _rankings_projection),
            ("metrics", "/api/metrics", _metrics_projection),
            ("automation_jobs", "/api/automation/jobs", _automation_jobs_projection),
        ):
            try:
                response = self._request("GET", path)
                if not _is_success(response):
                    raise SoulTransportUnavailable()
                sections[name] = projector(response.payload)
            except (SoulTransportTimeout, SoulTransportUnavailable, ValueError):
                sections[name] = []
                section_errors[name] = f"soul_{name}_unavailable"
        return {
            "connection": "ready",
            "console_url": self._console_url,
            "observed_at": status["health"]["heartbeat_at"] or _utc_now(),
            "available_commands": _available_commands(status),
            "status": status,
            **sections,
            "section_errors": section_errors,
        }

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        if not conversation_id.isdecimal() or int(conversation_id) < 1:
            raise SoulIntegrationError(
                code="soul_conversation_invalid",
                message="Soul 对话标识无效。",
                status_code=422,
            )
        try:
            response = self._request("GET", f"/api/matches/{conversation_id}")
        except (SoulTransportTimeout, SoulTransportUnavailable):
            raise SoulIntegrationError(
                code="soul_conversation_unavailable",
                message="Soul 对话当前不可用。",
                status_code=503,
            ) from None
        if response.status_code == 404:
            raise SoulIntegrationError(
                code="soul_conversation_not_found",
                message="Soul 对话不存在。",
                status_code=404,
            )
        if not _is_success(response) or not isinstance(response.payload, dict):
            raise SoulIntegrationError(
                code="soul_conversation_unavailable",
                message="Soul 对话当前不可用。",
                status_code=503,
            )
        source_match = response.payload.get("match")
        source_messages = response.payload.get("messages")
        if not isinstance(source_match, dict) or not isinstance(source_messages, list):
            raise SoulIntegrationError(
                code="soul_conversation_unavailable",
                message="Soul 对话当前不可用。",
                status_code=503,
            )
        return {
            "id": conversation_id,
            "match": {
                "id": source_match.get("id"),
                "platform": source_match.get("platform"),
                "nickname": source_match.get("nickname"),
                "status": source_match.get("status"),
                "updated_at": source_match.get("matched_at"),
            },
            "messages": [
                {
                    "role": item.get("role"),
                    "content": item.get("content"),
                    "created_at": item.get("created_at"),
                }
                for item in source_messages
                if isinstance(item, dict)
            ],
        }

    def command(self, command: SoulCommand, client_request_id: str) -> dict[str, Any]:
        existing = self._receipt_store.reserve_or_get(command, client_request_id)
        if existing is not None:
            if existing["command"] != command:
                raise SoulIntegrationError(
                    code="soul_client_request_id_conflict",
                    message="同一 client_request_id 已用于不同的 Soul 命令。",
                    status_code=409,
                )
            return existing
        path, body = _command_request(command)
        result = self._dispatch_command(command, client_request_id, path, body)
        try:
            self._receipt_store.update(result)
        except sqlite3.Error:
            # The durable reservation remains uncertain. Returning it avoids
            # claiming a terminal result that cannot survive process loss.
            return _command_receipt(command, client_request_id, "uncertain")
        return result

    def _dispatch_command(
        self,
        command: SoulCommand,
        client_request_id: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = self._request("POST", path, body)
        except (SoulTransportTimeout, SoulTransportUnavailable):
            return _command_receipt(command, client_request_id, "uncertain")
        payload = response.payload
        explicit_ok = isinstance(payload, dict) and payload.get("ok") is True
        explicit_rejected = isinstance(payload, dict) and payload.get("ok") is False
        if _is_success(response) and explicit_ok:
            return _command_receipt(command, client_request_id, "accepted")
        if explicit_rejected or 400 <= response.status_code < 500:
            return _command_receipt(command, client_request_id, "rejected")
        return _command_receipt(command, client_request_id, "uncertain")

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> SoulHttpResponse:
        return self._transport.request(method, path, body)


def _json_payload(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _read_limited(response: Any) -> bytes:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SoulTransportUnavailable()
    return raw


def _validated_loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Soul console URL must use loopback HTTP")
    return value.rstrip("/")


def _is_success(response: SoulHttpResponse) -> bool:
    return 200 <= response.status_code < 300


def _status_projection(source: dict[str, Any]) -> dict[str, Any]:
    health_source = source.get("health") if isinstance(source.get("health"), dict) else {}
    verification_counts = (
        source.get("send_verification_counts")
        if isinstance(source.get("send_verification_counts"), dict)
        else {}
    )
    health = _projection(
        health_source,
        (
            "status",
            "current_action",
            "heartbeat_at",
            "scheduler_heartbeat_at",
            "pause_reason",
            "last_error",
            "last_success",
            "next_retry_at",
        ),
    )
    health["last_error"] = _public_message(health_source.get("last_error"))
    health["last_success"] = _success_at(health_source.get("last_success"))
    return {
        "platform": source.get("platform"),
        "running": source.get("running"),
        "paused": source.get("paused"),
        "stop_requested": source.get("stop_requested"),
        "run_state": source.get("run_state", health["status"]),
        "mode": source.get("mode"),
        "operation_mode": source.get("operation_mode"),
        "health": health,
        "counts": _projection(
            source,
            (
                "pending_count",
                "pending_inbound_count",
                "human_count",
                "send_verifying_count",
                "today_matches",
                "today_match_acquisitions",
                "llm_calls",
                "llm_limit",
            ),
        ),
        "send_verification_counts": {
            key: verification_counts.get(key, 0)
            for key in ("active", "backoff", "manual_required", "orphaned")
        },
        "match_balance": _projection(
            source.get("match_balance") if isinstance(source.get("match_balance"), dict) else {},
            ("state", "remaining", "observed_at", "planet_unlocked"),
        ),
    }


def _status_is_compatible(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    required = ("platform", "running", "paused", "stop_requested", "mode", "operation_mode", "health")
    return (
        all(key in payload for key in required)
        and payload["platform"] == "soul"
        and type(payload["running"]) is bool
        and type(payload["paused"]) is bool
        and type(payload["stop_requested"]) is bool
        and isinstance(payload["mode"], str)
        and isinstance(payload["operation_mode"], str)
        and payload["operation_mode"] in {"chat", "match"}
        and isinstance(payload.get("health"), dict)
        and _health_is_compatible(payload["health"])
    )


def _health_is_compatible(health: dict[str, Any]) -> bool:
    plain_fields = (
        "status",
        "current_action",
        "heartbeat_at",
        "scheduler_heartbeat_at",
        "pause_reason",
        "next_retry_at",
    )
    if any(value is not None and not isinstance(value, str) for value in (health.get(key) for key in plain_fields)):
        return False
    return _is_error_value(health.get("last_error")) and _is_success_value(health.get("last_success"))


def _is_error_value(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, dict) and (value.get("message") is None or isinstance(value.get("message"), str))


def _is_success_value(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, dict) and (value.get("at") is None or isinstance(value.get("at"), str))


def _unavailable_workspace(
    connection: Literal["unavailable", "incompatible"],
    status_error: str,
    console_url: str,
) -> dict[str, Any]:
    return {
        "connection": connection,
        "console_url": console_url,
        "observed_at": None,
        "available_commands": [],
        "status": None,
        "matches": [],
        "rankings": [],
        "metrics": [],
        "automation_jobs": [],
        "section_errors": {"status": status_error},
    }


def _available_commands(status: dict[str, Any]) -> list[str]:
    if status["stop_requested"]:
        return []
    operation_mode = status["operation_mode"]
    alternate_mode = "match_mode" if operation_mode == "chat" else "chat_mode"
    if status["running"]:
        if status["paused"]:
            return ["resume", "stop", alternate_mode]
        commands = ["pause", "stop"]
        if status["platform"] == "soul":
            commands.append("queue_inventory")
        commands.append(alternate_mode)
        return commands
    return ["start", alternate_mode]


def _matches_projection(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("matches payload must be a list")
    return [
        {
            "id": item.get("id"),
            "platform": item.get("platform"),
            "nickname": item.get("nickname"),
            "status": item.get("status"),
            "updated_at": item.get("last_ts"),
            "msg_count": item.get("msg_count"),
            "pending_inbound": item.get("pending_inbound"),
            "send_verification_state": item.get("send_verification_state"),
        }
        for item in payload
        if isinstance(item, dict) and item.get("platform") == "soul"
    ]


def _rankings_projection(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("rankings payload must be a list")
    return [
        {
            **_projection(
                item,
                (
                    "match_id",
                    "overall_score",
                    "compatibility_score",
                    "engagement_score",
                    "evidence",
                    "summary",
                    "confidence",
                    "status",
                ),
            ),
            "updated_at": item.get("evaluated_at"),
        }
        for item in payload
        if isinstance(item, dict)
    ]


def _metrics_projection(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("metrics payload must be a list")
    return [
        _projection(
            item,
            (
                "match_id",
                "active_days",
                "incoming_count",
                "response_rate",
                "conversation_depth",
                "engagement_state",
                "calculated_at",
            ),
        )
        for item in payload
        if isinstance(item, dict)
    ]


def _automation_jobs_projection(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("automation jobs payload must be a list")
    return [
        {
            "name": item.get("name"),
            "status": item.get("status"),
            "updated_at": item.get("last_run"),
            "detail": _public_message(item.get("last_error")),
        }
        for item in payload
        if isinstance(item, dict)
    ]


def _projection(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys}


def _public_message(value: Any, *, maximum: int = 240) -> str | None:
    if isinstance(value, dict):
        value = value.get("message")
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:maximum] or None


def _success_at(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("at")
    return value if isinstance(value, str) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _command_request(command: SoulCommand) -> tuple[str, dict[str, Any]]:
    if command in {"start", "pause", "resume", "stop"}:
        return "/api/control", {"action": command}
    if command == "queue_inventory":
        return "/api/inventory", {}
    if command == "chat_mode":
        return "/api/operation-mode", {"mode": "chat"}
    if command == "match_mode":
        return "/api/operation-mode", {"mode": "match"}
    raise ValueError(f"unsupported Soul command: {command}")


def _command_receipt(
    command: SoulCommand,
    client_request_id: str,
    status: Literal["accepted", "rejected", "uncertain"],
) -> dict[str, Any]:
    detail = {
        "accepted": "Soul 已接受命令请求；最新状态请以工作台为准。",
        "rejected": "Soul 拒绝该命令。",
        "uncertain": "Soul 命令结果不确定，未自动重试。",
    }[status]
    return {
        "command": command,
        "client_request_id": client_request_id,
        "status": status,
        "detail": detail,
        "workspace": None,
    }


def _stored_receipt(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "command": row["command"],
        "client_request_id": row["client_request_id"],
        "status": row["status"],
        "detail": row["detail"],
        "workspace": None,
    }
