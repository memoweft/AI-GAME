from __future__ import annotations

import socket

import pytest

from ai_game_console.execution import AndroidScreenshot
from ai_game_console.gui_owl_client import (
    GuiOwlClientError,
    OpenAICompatibleGuiOwlClient,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"frame"


def test_gui_owl_request_uses_one_current_image_official_envelope_and_safe_history() -> None:
    captured = {}

    def transport(endpoint, payload, headers, timeout):
        captured.update(
            endpoint=endpoint,
            payload=payload,
            headers=headers,
            timeout=timeout,
        )
        return {
            "choices": [
                {
                    "message": {
                        "content": 'Action: tap Settings\n<tool_call>{"name":"mobile_use","arguments":{"action":"click","coordinate":[500,500]}}</tool_call>'
                    }
                }
            ]
        }

    client = OpenAICompatibleGuiOwlClient(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        api_key="secret-local-key",
        timeout_seconds=7,
        transport=transport,
    )
    output = client.propose_action(
        AndroidScreenshot(png_bytes=PNG, width=1000, height=2000),
        goal="Open Settings",
        action_history=("step 1: transported tap",),
    )

    assert captured["endpoint"] == "http://127.0.0.1:4243/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-local-key"
    assert captured["timeout"] == 7
    payload = captured["payload"]
    assert payload["model"] == "gui-owl"
    assert payload["temperature"] == 0.0
    assert payload["stream"] is False
    contents = payload["messages"][1]["content"]
    images = [part for part in contents if part["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "step 1: transported tap" in contents[0]["text"]
    assert "Visual priority" in contents[0]["text"]
    assert "semi-transparent tutorial hand" in contents[0]["text"]
    assert "before Close/Back/Help" in contents[0]["text"]
    system = payload["messages"][0]["content"][0]["text"]
    assert '"name":"mobile_use"' in system
    assert "0..1000" in system
    assert "CAPTCHA" not in system
    assert "do not request a human handoff" in system
    assert "coordinate and coordinate2 must be JSON arrays [x,y]" in system
    assert "repeated taps in the same screen region" in system
    assert "semi-transparent tutorial hand" in system
    assert "fingertip target takes priority" in system
    assert output.startswith("Action: tap Settings")


def test_gui_owl_history_is_bounded_below_the_live_two_k_context_failure() -> None:
    captured = {}

    def transport(endpoint, payload, headers, timeout):
        captured["prompt"] = payload["messages"][1]["content"][0]["text"]
        return {
            "choices": [
                {
                    "message": {
                        "content": '<tool_call>{"name":"mobile_use","arguments":{"action":"wait","time":0}}</tool_call>'
                    }
                }
            ]
        }

    client = OpenAICompatibleGuiOwlClient(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        transport=transport,
    )
    history = tuple(f"history-{index}" for index in range(12))

    client.propose_action(
        AndroidScreenshot(png_bytes=PNG, width=1000, height=2000),
        goal="Continue",
        action_history=history,
    )

    assert "history-5" not in captured["prompt"]
    assert "history-6" in captured["prompt"]
    assert "history-11" in captured["prompt"]


def test_gui_owl_never_sends_screenshot_to_a_non_loopback_endpoint() -> None:
    client = OpenAICompatibleGuiOwlClient(
        endpoint="https://example.com/v1",
        model="gui-owl",
        transport=lambda *args: pytest.fail("transport must not run"),
    )

    with pytest.raises(GuiOwlClientError) as raised:
        client.propose_action(
            AndroidScreenshot(png_bytes=PNG, width=10, height=10),
            goal="test",
            action_history=(),
        )

    assert raised.value.code == "gui_model_not_local"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": ""}}]},
    ],
)
def test_gui_owl_rejects_malformed_responses_without_leaking_request(response) -> None:
    client = OpenAICompatibleGuiOwlClient(
        endpoint="http://localhost:4243",
        model="gui-owl",
        api_key="never-print-this",
        transport=lambda *args: response,
    )

    with pytest.raises(GuiOwlClientError) as raised:
        client.propose_action(
            AndroidScreenshot(png_bytes=PNG, width=10, height=10),
            goal="test",
            action_history=(),
        )

    assert "never-print-this" not in str(raised.value)
    assert "data:image" not in str(raised.value)


def test_gui_owl_sanitizes_transport_timeout() -> None:
    def timeout(*args):
        raise socket.timeout("secret-local-key data:image/png;base64,private")

    client = OpenAICompatibleGuiOwlClient(
        endpoint="http://127.0.0.1:4243/v1/chat/completions",
        model="gui-owl",
        api_key="secret-local-key",
        transport=timeout,
    )

    with pytest.raises(GuiOwlClientError) as raised:
        client.propose_action(
            AndroidScreenshot(png_bytes=PNG, width=10, height=10),
            goal="test",
            action_history=(),
        )

    assert raised.value.code == "gui_model_unavailable"
    assert "secret-local-key" not in str(raised.value)
    assert "data:image" not in str(raised.value)
