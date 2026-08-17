from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .domain import require_safe_ref
from .errors import SoulApplicationError


CONTRACT_VERSION = "v1"
SCHEDULER_CONTROLLER_REF = "ai-game-soul-reply-v1"
OwnerTransport = Callable[[str, str, dict[str, Any] | None], Mapping[str, Any]]
_OWNER_STATUSES = {
    "reserved",
    "reserve_replayed",
    "reserve_rejected",
    "confirmed",
    "active_dispatch",
    "uncertain_needs_reconciliation",
    "terminal_no_replay",
    "legacy_scheduler_active",
    "stale_preflight",
    "preclick_rejected",
    "soul_execution_runtime_unavailable",
    "fresh_owner_observation_unavailable",
    "foreground_action_owned",
}
_SAFE_ERROR = re.compile(r"[a-z][a-z0-9_]{0,79}")
_SCHEDULER_DESIRED_STATES = frozenset({"running", "paused", "stopped"})
_SCHEDULER_EFFECTIVE_STATES = frozenset(
    {"running", "paused", "stopping", "stopped"}
)
_SCHEDULER_REPLY_OWNERS = frozenset({"application_runtime", "none"})
_SCHEDULER_RESPONSE_KEYS = frozenset(
    {
        "contract_version",
        "controller_ref",
        "desired_state",
        "effective_state",
        "reply_owner",
        "scheduler_mode",
    }
)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


class SoulOwnerClient:
    """Bounded, no-proxy, no-redirect client for the loopback Soul owner v1."""

    MAX_REQUEST_BYTES = 12 * 1024 * 1024
    # A v1 observation contains one bounded PNG. Other responses are tiny, but
    # the single transport cap must accommodate that owner-held frame.
    MAX_RESPONSE_BYTES = 24 * 1024 * 1024

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        observation_timeout_seconds: float = 90.0,
        transport: OwnerTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SoulApplicationError("soul_owner_url_invalid")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise SoulApplicationError("soul_owner_not_loopback")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/")
        ):
            raise SoulApplicationError("soul_owner_url_invalid")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("timeout_seconds must be between 0 and 120")
        if observation_timeout_seconds <= 0 or observation_timeout_seconds > 120:
            raise ValueError("observation_timeout_seconds must be between 0 and 120")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.timeout_seconds = float(timeout_seconds)
        self.observation_timeout_seconds = float(observation_timeout_seconds)
        self._transport = transport
        self._opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    def capabilities(self) -> Mapping[str, Any]:
        return self._call("GET", "/api/application-owner/v1/capabilities", None)

    def observe(self) -> Mapping[str, Any]:
        return self._call(
            "POST",
            "/api/application-owner/v1/soul/observations",
            {"contract_version": CONTRACT_VERSION},
            timeout_seconds=self.observation_timeout_seconds,
        )

    def reserve(
        self, *, application_intent_id: str, scope_ref: str, draft: str
    ) -> Mapping[str, Any]:
        require_safe_ref(application_intent_id, "application_intent_id_invalid", maximum=160)
        require_safe_ref(scope_ref, "observation_scope_invalid", maximum=96)
        if not isinstance(draft, str) or not draft.strip() or len(draft) > 1600:
            raise SoulApplicationError("single_reply_draft_required")
        return self._call(
            "POST",
            "/api/application-owner/v1/soul/intents",
            {
                "contract_version": CONTRACT_VERSION,
                "application_intent_id": application_intent_id,
                "scope_ref": scope_ref,
                "draft": {"text": draft.strip()},
            },
        )

    def dispatch(
        self,
        owner_ref: str,
        *,
        scope_ref: str,
        conversation_revision: str,
        draft: str,
    ) -> Mapping[str, Any]:
        owner_ref = require_safe_ref(owner_ref, "owner_ref_invalid", maximum=96)
        require_safe_ref(scope_ref, "observation_scope_invalid", maximum=96)
        require_safe_ref(conversation_revision, "conversation_revision_invalid", maximum=160)
        if not isinstance(draft, str) or not draft.strip() or len(draft) > 1600:
            raise SoulApplicationError("single_reply_draft_required")
        return self._call(
            "POST",
            f"/api/application-owner/v1/soul/intents/{owner_ref}/dispatch",
            {
                "contract_version": CONTRACT_VERSION,
                "scope_ref": scope_ref,
                "preflight": {"conversation_revision": conversation_revision},
                # Repeating the draft lets the owner verify its immutable hash
                # after an owner-process restart; it is never logged here.
                "draft": {"text": draft.strip()},
            },
        )

    def inspect(self, owner_ref: str) -> Mapping[str, Any]:
        owner_ref = require_safe_ref(owner_ref, "owner_ref_invalid", maximum=96)
        return self._call(
            "GET", f"/api/application-owner/v1/soul/intents/{owner_ref}", None
        )

    def inspect_application_intent(
        self, application_intent_id: str
    ) -> Mapping[str, Any]:
        application_intent_id = require_safe_ref(
            application_intent_id, "application_intent_id_invalid", maximum=160
        )
        return self._call(
            "GET",
            "/api/application-owner/v1/soul/application-intents/"
            + application_intent_id,
            None,
        )

    def scheduler(self) -> Mapping[str, Any]:
        return _scheduler_response(
            self._call(
                "GET",
                "/api/application-owner/v1/soul/scheduler",
                None,
            )
        )

    def set_scheduler_state(self, desired_state: str) -> Mapping[str, Any]:
        if desired_state not in _SCHEDULER_DESIRED_STATES:
            raise ValueError("desired_state must be running, paused, or stopped")
        response = _scheduler_response(
            self._call(
                "PUT",
                "/api/application-owner/v1/soul/scheduler",
                {
                    "contract_version": CONTRACT_VERSION,
                    "desired_state": desired_state,
                    "controller_ref": SCHEDULER_CONTROLLER_REF,
                },
            )
        )
        if response["desired_state"] != desired_state:
            raise SoulApplicationError("soul_scheduler_state_mismatch")
        return response

    def _call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = (
                self._transport(method, path, payload)
                if self._transport is not None
                else self._request(
                    method,
                    path,
                    payload,
                    timeout_seconds=timeout_seconds,
                )
            )
        except SoulApplicationError:
            raise
        except Exception:
            raise SoulApplicationError("soul_owner_unavailable") from None
        if not isinstance(response, Mapping):
            raise SoulApplicationError("soul_owner_invalid_response")
        try:
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise SoulApplicationError("soul_owner_invalid_response") from None
        if len(encoded) > self.MAX_RESPONSE_BYTES:
            raise SoulApplicationError("soul_owner_response_too_large")
        if response.get("contract_version") != CONTRACT_VERSION:
            raise SoulApplicationError("soul_owner_contract_mismatch")
        status = response.get("status")
        if status is not None and status not in _OWNER_STATUSES:
            raise SoulApplicationError("soul_owner_invalid_response")
        return dict(response)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(body) > self.MAX_REQUEST_BYTES:
                raise SoulApplicationError("soul_owner_request_too_large")
        request = Request(
            self.base_url + path,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with self._opener.open(
                request,
                timeout=(
                    self.timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
            ) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.MAX_RESPONSE_BYTES:
                    raise SoulApplicationError("soul_owner_response_too_large")
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise SoulApplicationError("soul_owner_redirect_rejected") from None
            try:
                raw_error = exc.read(self.MAX_RESPONSE_BYTES + 1)
                decoded_error = json.loads(raw_error.decode("utf-8"))
                code = decoded_error.get("error") if isinstance(decoded_error, Mapping) else None
            except Exception:
                code = None
            raise SoulApplicationError(
                code if isinstance(code, str) and _SAFE_ERROR.fullmatch(code) else "soul_owner_http_error"
            ) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise SoulApplicationError("soul_owner_unavailable") from None
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise SoulApplicationError("soul_owner_response_too_large")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SoulApplicationError("soul_owner_invalid_response") from None
        if not isinstance(decoded, Mapping):
            raise SoulApplicationError("soul_owner_invalid_response")
        return decoded


def _scheduler_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _SCHEDULER_RESPONSE_KEYS:
        raise SoulApplicationError("soul_scheduler_invalid_response")
    controller_ref = payload.get("controller_ref")
    if not (
        controller_ref is None
        or (
            isinstance(controller_ref, str)
            and 1 <= len(controller_ref) <= 192
        )
    ):
        raise SoulApplicationError("soul_scheduler_invalid_response")
    desired_state = payload.get("desired_state")
    effective_state = payload.get("effective_state")
    reply_owner = payload.get("reply_owner")
    scheduler_mode = payload.get("scheduler_mode")
    if (
        payload.get("contract_version") != CONTRACT_VERSION
        or desired_state not in _SCHEDULER_DESIRED_STATES
        or effective_state not in _SCHEDULER_EFFECTIVE_STATES
        or reply_owner not in _SCHEDULER_REPLY_OWNERS
        or scheduler_mode not in {"match", None}
    ):
        raise SoulApplicationError("soul_scheduler_invalid_response")
    return {
        "contract_version": CONTRACT_VERSION,
        "controller_ref": controller_ref,
        "desired_state": desired_state,
        "effective_state": effective_state,
        "reply_owner": reply_owner,
        "scheduler_mode": scheduler_mode,
    }
