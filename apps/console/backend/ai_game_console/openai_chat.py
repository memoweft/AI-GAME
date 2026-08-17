from __future__ import annotations

import json
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from .chat import (
    ChatCompletion,
    ChatProviderError,
    ProviderMessage,
    validate_execution_goal,
)
from .config import Settings


ProviderTransport = Callable[
    [str, Mapping[str, Any], Mapping[str, str], float], Mapping[str, Any]
]


@dataclass(slots=True)
class OpenAIChatProvider:
    """Small OpenAI-compatible transport adapter with sanitized failures."""

    endpoint: str
    model: str
    provider_name: str
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    transport: ProviderTransport | None = field(default=None, repr=False)

    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    @classmethod
    def from_local_settings(cls, settings: Settings) -> "OpenAIChatProvider | None":
        if not settings.local_chat_endpoint or not settings.local_chat_model:
            return None
        return cls(
            endpoint=settings.local_chat_endpoint,
            model=settings.local_chat_model,
            provider_name="local_openai_compatible",
            api_key=settings.local_chat_api_key,
            timeout_seconds=settings.chat_request_timeout_seconds,
        )

    @classmethod
    def from_cloud_settings(cls, settings: Settings) -> "OpenAIChatProvider | None":
        if not (
            settings.cloud_chat_endpoint
            and settings.cloud_chat_model
            and settings.cloud_chat_api_key
        ):
            return None
        return cls(
            endpoint=settings.cloud_chat_endpoint,
            model=settings.cloud_chat_model,
            provider_name="cloud_openai_compatible",
            api_key=settings.cloud_chat_api_key,
            timeout_seconds=settings.chat_request_timeout_seconds,
        )

    def complete(
        self,
        messages: Sequence[ProviderMessage],
        *,
        json_response: bool,
        is_cancelled: Callable[[], bool],
    ) -> ChatCompletion:
        if is_cancelled():
            raise ChatProviderError("provider_cancelled", "对话请求已取消。")
        endpoint = _chat_completions_endpoint(self.endpoint)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": False,
            "temperature": 0.2,
        }
        if json_response:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            decoded = (
                self.transport(endpoint, payload, headers, self.timeout_seconds)
                if self.transport is not None
                else self._request(endpoint, payload, headers, self.timeout_seconds)
            )
        except ChatProviderError:
            raise
        except HTTPError as exc:
            raise ChatProviderError(
                "provider_http_error",
                f"模型服务返回 HTTP {exc.code}。",
            ) from None
        except (URLError, socket.timeout, TimeoutError, OSError):
            raise ChatProviderError(
                "provider_unavailable",
                "模型服务暂时不可用。",
            ) from None
        except Exception:
            # Injected transports and future HTTP clients may fail with arbitrary
            # exception text. Never let that text cross the adapter seam.
            raise ChatProviderError(
                "provider_transport_failed",
                "模型请求失败。",
            ) from None

        if is_cancelled():
            raise ChatProviderError("provider_cancelled", "对话请求已取消。")
        content = _completion_content(decoded)
        if json_response:
            parsed = _parse_planner_json(content)
            assistant_text = parsed.get("assistant_text")
            if not isinstance(assistant_text, str) or not assistant_text.strip():
                raise ChatProviderError(
                    "provider_invalid_response",
                    "云端模型没有返回有效的 assistant_text。",
                )
            raw_goal = parsed.get("execution_goal")
            if raw_goal is not None and not isinstance(raw_goal, Mapping):
                raise ChatProviderError(
                    "provider_invalid_response",
                    "云端模型返回的 execution_goal 无效。",
                )
            goal = validate_execution_goal(raw_goal)
            return ChatCompletion(
                assistant_text=assistant_text.strip()[:50_000],
                provider=self.provider_name,
                model=self.model,
                execution_goal=goal,
            )
        if not content.strip():
            raise ChatProviderError("provider_empty_reply", "本地模型没有返回可用回复。")
        return ChatCompletion(
            assistant_text=content.strip()[:50_000],
            provider=self.provider_name,
            model=self.model,
            execution_goal=None,
        )

    @classmethod
    def _request(
        cls,
        endpoint: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - configured endpoint
            body = response.read(cls.MAX_RESPONSE_BYTES + 1)
        if len(body) > cls.MAX_RESPONSE_BYTES:
            raise ChatProviderError(
                "provider_response_too_large",
                "模型响应超过本地大小限制。",
            )
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ChatProviderError(
                "provider_invalid_response",
                "模型服务返回了无效 JSON。",
            ) from None
        if not isinstance(decoded, Mapping):
            raise ChatProviderError(
                "provider_invalid_response",
                "模型服务返回的数据结构无效。",
            )
        return decoded


def _chat_completions_endpoint(configured: str) -> str:
    value = configured.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ChatProviderError("provider_not_configured", "模型服务地址无效。")
    if parsed.username or parsed.password or parsed.fragment:
        raise ChatProviderError("provider_not_configured", "模型服务地址无效。")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        final_path = path
    elif path.endswith("/v1"):
        final_path = f"{path}/chat/completions"
    elif not path:
        final_path = "/v1/chat/completions"
    else:
        final_path = f"{path}/v1/chat/completions"
    return urlunparse((parsed.scheme, parsed.netloc, final_path, "", "", ""))


def _completion_content(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ChatProviderError("provider_invalid_response", "模型响应缺少 choices。")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise ChatProviderError("provider_invalid_response", "模型响应缺少 message。")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, Mapping) and part.get("type") in {"text", "output_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        if text_parts:
            return "".join(text_parts)
    raise ChatProviderError("provider_invalid_response", "模型响应缺少文字内容。")


def _parse_planner_json(content: str) -> Mapping[str, Any]:
    candidate = content.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError:
        raise ChatProviderError(
            "provider_invalid_response",
            "云端模型没有返回有效 JSON。",
        ) from None
    if not isinstance(decoded, Mapping):
        raise ChatProviderError(
            "provider_invalid_response",
            "云端模型返回的数据结构无效。",
        )
    return decoded

