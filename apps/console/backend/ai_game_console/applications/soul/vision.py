from __future__ import annotations

import base64
import json
import socket
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ...gui_owl_client import (
    GuiOwlClientError,
    GuiOwlTransport,
    _completion_content,
    _loopback_chat_completions_endpoint,
)
from .domain import SoulVisualFacts
from .errors import SoulApplicationError


class _NoVisionRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(slots=True)
class LoopbackSoulVisionClient:
    """Local-only screenshot classifier with a closed, identity-free schema."""

    endpoint: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    transport: GuiOwlTransport | None = field(default=None, repr=False)

    MAX_IMAGE_BYTES = 16 * 1024 * 1024
    MAX_REQUEST_BYTES = 24 * 1024 * 1024
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    def extract(self, screenshot_png: bytes) -> SoulVisualFacts:
        try:
            endpoint = _loopback_chat_completions_endpoint(self.endpoint)
        except GuiOwlClientError as exc:
            raise SoulApplicationError(exc.code) from None
        if not self.model.strip():
            raise SoulApplicationError("local_soul_vision_not_configured")
        if (
            not isinstance(screenshot_png, bytes)
            or not screenshot_png.startswith(b"\x89PNG\r\n\x1a\n")
            or len(screenshot_png) > self.MAX_IMAGE_BYTES
        ):
            raise SoulApplicationError("local_soul_vision_image_invalid")
        schema = (
            "Inspect this Soul screenshot only as a local visual classifier. "
            "Return exactly one JSON object and no markdown with exactly these keys: "
            "page, pending_inbound_visible, conversation_stage, tone, cues, confidence. "
            "page is one of conversation_detail, conversation_list, other, unknown. "
            "conversation_stage is one of new, early, ongoing, unknown. "
            "tone is one of warm, neutral, playful, reserved, unknown. "
            "cues contains at most 8 unique values chosen only from greeting, "
            "question_present, emoji_present, long_message, short_message, composer_empty, "
            "send_control_visible, visual_obstruction. Never return a name, title, ID, "
            "message text, coordinates, or an action. confidence is 0..1."
        )
        payload: dict[str, Any] = {
            "model": self.model.strip(),
            "messages": [
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "You are a local, non-acting UI classifier."}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": schema},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(screenshot_png).decode("ascii")
                            },
                        },
                    ],
                },
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": 256,
            "response_format": {"type": "json_object"},
        }
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            decoded = (
                self.transport(endpoint, payload, headers, self.timeout_seconds)
                if self.transport is not None
                else self._request_local(endpoint, payload, headers)
            )
            return _parse_visual_facts(_completion_content(decoded))
        except SoulApplicationError:
            raise
        except GuiOwlClientError as exc:
            raise SoulApplicationError(exc.code) from None
        except Exception:
            raise SoulApplicationError("local_soul_vision_failed") from None

    def _request_local(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(body) > self.MAX_REQUEST_BYTES:
            raise SoulApplicationError("local_soul_vision_request_too_large")
        request = Request(
            endpoint,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), _NoVisionRedirectHandler())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.MAX_RESPONSE_BYTES:
                    raise SoulApplicationError(
                        "local_soul_vision_response_too_large"
                    )
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except SoulApplicationError:
            raise
        except HTTPError as exc:
            raise SoulApplicationError(
                "local_soul_vision_redirect_rejected"
                if 300 <= exc.code < 400
                else "local_soul_vision_http_error"
            ) from None
        except (URLError, socket.timeout, TimeoutError, OSError, ValueError):
            raise SoulApplicationError("local_soul_vision_unavailable") from None
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise SoulApplicationError("local_soul_vision_response_too_large")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SoulApplicationError("local_soul_vision_invalid_response") from None
        if not isinstance(decoded, Mapping):
            raise SoulApplicationError("local_soul_vision_invalid_response")
        return decoded


def _parse_visual_facts(content: str) -> SoulVisualFacts:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        raise SoulApplicationError("local_soul_vision_invalid_response") from None
    if not isinstance(decoded, Mapping) or set(decoded) != {
        "page",
        "pending_inbound_visible",
        "conversation_stage",
        "tone",
        "cues",
        "confidence",
    }:
        raise SoulApplicationError("local_soul_vision_invalid_response")
    cues = decoded["cues"]
    if not isinstance(cues, list) or any(not isinstance(item, str) for item in cues):
        raise SoulApplicationError("local_soul_vision_invalid_response")
    return SoulVisualFacts(
        page=decoded["page"],
        pending_inbound_visible=decoded["pending_inbound_visible"],
        conversation_stage=decoded["conversation_stage"],
        tone=decoded["tone"],
        cues=tuple(cues),
        confidence=decoded["confidence"],
    )
