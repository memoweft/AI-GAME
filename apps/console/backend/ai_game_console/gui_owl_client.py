from __future__ import annotations

import base64
import json
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from .execution import AndroidScreenshot


GuiOwlTransport = Callable[
    [str, Mapping[str, Any], Mapping[str, str], float], Mapping[str, Any]
]


class GuiOwlClientError(RuntimeError):
    """Sanitized local vision-model failure safe to expose to the coordinator."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_SYSTEM_PROMPT = """You operate one Android touchscreen through mobile_use.
The screenshot coordinate space is normalized to 0..1000 on both axes.
Return exactly one short Action line followed by exactly one tool call:
<tool_call>
{"name":"mobile_use","arguments":{"action":"..."}}
</tool_call>
Allowed actions are click, long_press, swipe, type, system_button, wait, and
terminate. click and long_press use coordinate [x,y]. swipe uses
coordinate [x,y] and coordinate2 [x,y]. type uses text. system_button uses the
button field, for example {"action":"system_button","button":"Home"}; allowed
buttons are Back, Home, Menu, Enter. wait uses time in seconds. terminate uses status
success or failure.
The tool envelope must be valid JSON. coordinate and coordinate2 must be JSON arrays [x,y],
for example {"action":"click","coordinate":[503,446]}; never write coordinate:503,446
or a scalar coordinate value.
Choose only one atomic next action from the current screenshot. This is an
unattended test mode: do not request a human handoff and do not emit interact.
Before ordinary buttons, inspect for a semi-transparent tutorial hand, pointing
finger, or pulsing golden ring. When one is visible, click the control directly
under its fingertip; the fingertip target takes priority over labels, Help/Back,
close buttons, and a guessed main control.
For every screen, continue with a physical action, wait, or terminate based on
the current screenshot. If a prior action history says interact is unavailable,
you must choose one of the allowed actions on this turn. If history reports
repeated taps in the same screen region, do not repeat that region; prefer a
visible tutorial hand's fingertip target, then another highlighted control that
advances the instruction."""


@dataclass(slots=True)
class OpenAICompatibleGuiOwlClient:
    """One-current-frame OpenAI-compatible Adapter for GUI-Owl mobile actions."""

    endpoint: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    transport: GuiOwlTransport | None = field(default=None, repr=False)

    MAX_IMAGE_BYTES = 16 * 1024 * 1024
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    # The live 2K profile rejected 12 repeated format-repair entries at 2092
    # input tokens. Six keeps recent motion context while leaving deterministic
    # headroom for the current image and action schema.
    MAX_HISTORY_ITEMS = 6

    def propose_action(
        self,
        screenshot: AndroidScreenshot,
        *,
        goal: str,
        action_history: tuple[str, ...],
    ) -> str:
        endpoint = _loopback_chat_completions_endpoint(self.endpoint)
        if not self.model.strip():
            raise GuiOwlClientError("gui_model_not_configured", "本地 GUI 模型名称未配置。")
        if not goal.strip():
            raise GuiOwlClientError("gui_goal_invalid", "设备操作目标不能为空。")
        image = screenshot.png_bytes
        if (
            not image.startswith(b"\x89PNG\r\n\x1a\n")
            or len(image) > self.MAX_IMAGE_BYTES
        ):
            raise GuiOwlClientError("gui_image_invalid", "设备截图无效或超过大小限制。")

        safe_history = tuple(action_history[-self.MAX_HISTORY_ITEMS :])
        previous = "\n".join(safe_history) if safe_history else "None"
        user_prompt = (
            "Generate the next move from the current screenshot.\n\n"
            f"Instruction: {goal.strip()}\n\n"
            "Visual priority: if a semi-transparent tutorial hand, pointing finger, "
            "or pulsing golden ring is visible, click the control under its fingertip "
            "before Close/Back/Help or ordinary controls.\n\n"
            f"Previous actions (text only):\n{previous}"
        )
        image_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        payload: dict[str, Any] = {
            "model": self.model.strip(),
            "messages": [
                {
                    "role": "system",
                    "content": [{"type": "text", "text": _SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": 384,
        }
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
        except GuiOwlClientError:
            raise
        except HTTPError as exc:
            raise GuiOwlClientError(
                "gui_model_http_error", f"本地 GUI 模型返回 HTTP {exc.code}。"
            ) from None
        except (URLError, socket.timeout, TimeoutError, OSError):
            raise GuiOwlClientError(
                "gui_model_unavailable", "本地 GUI 模型暂时不可用。"
            ) from None
        except Exception:
            raise GuiOwlClientError("gui_model_request_failed", "本地 GUI 模型请求失败。") from None

        return _completion_content(decoded)

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
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - loopback enforced
            body = response.read(cls.MAX_RESPONSE_BYTES + 1)
        if len(body) > cls.MAX_RESPONSE_BYTES:
            raise GuiOwlClientError("gui_model_response_too_large", "本地 GUI 模型响应过大。")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GuiOwlClientError("gui_model_invalid_response", "本地 GUI 模型返回了无效 JSON。") from None
        if not isinstance(decoded, Mapping):
            raise GuiOwlClientError("gui_model_invalid_response", "本地 GUI 模型响应结构无效。")
        return decoded


def _loopback_chat_completions_endpoint(configured: str) -> str:
    parsed = urlparse(configured.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise GuiOwlClientError(
            "gui_model_not_local", "设备截图只允许发送到本机 GUI 模型。"
        )
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
        raise GuiOwlClientError("gui_model_invalid_response", "本地 GUI 模型响应缺少 choices。")
    message = choices[0].get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
        raise GuiOwlClientError("gui_model_invalid_response", "本地 GUI 模型响应缺少动作内容。")
    content = message["content"].strip()
    if not content or len(content) > 100_000:
        raise GuiOwlClientError("gui_model_invalid_response", "本地 GUI 模型动作内容无效。")
    return content
