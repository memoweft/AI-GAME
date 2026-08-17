from __future__ import annotations

from pathlib import Path

import pytest

from ai_game_console.chat import ChatProviderError, ProviderMessage
from ai_game_console.config import Settings
from ai_game_console.openai_chat import OpenAIChatProvider


def completion_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_settings_load_local_and_cloud_chat_without_reading_cloud_key_from_file(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    config = project / "config"
    config.mkdir(parents=True)
    (config / "model-runtime.env").write_text(
        "\n".join(
            (
                "GUI_MODEL_HOST=127.0.0.1",
                "GUI_MODEL_PORT=4243",
                "GUI_MODEL_SERVED_NAME=gui-owl",
                "GUI_MODEL_API_KEY=local-secret",
            )
        ),
        encoding="utf-8",
    )
    (config / "cloud-runtime.env").write_text(
        "\n".join(
            (
                "CLOUD_CHAT_ENDPOINT=https://planner.example/v1",
                "CLOUD_CHAT_MODEL=planner-model",
                "CLOUD_CHAT_API_KEY=must-not-be-read",
            )
        ),
        encoding="utf-8",
    )

    without_key = Settings.from_env({"AI_GAME_PROJECT_ROOT": str(project)})
    assert without_key.local_chat_endpoint == "http://127.0.0.1:4243/v1/chat/completions"
    assert without_key.local_chat_model == "gui-owl"
    assert without_key.local_chat_api_key == "local-secret"
    assert without_key.cloud_chat_endpoint == "https://planner.example/v1"
    assert without_key.cloud_chat_model == "planner-model"
    assert without_key.cloud_chat_api_key is None
    assert "local-secret" not in repr(without_key)
    assert "must-not-be-read" not in repr(without_key)

    configured = Settings.from_env(
        {
            "AI_GAME_PROJECT_ROOT": str(project),
            "AI_GAME_CLOUD_CHAT_API_KEY": "process-only-secret",
        }
    )
    assert configured.cloud_chat_api_key == "process-only-secret"
    assert OpenAIChatProvider.from_cloud_settings(configured) is not None
    assert "process-only-secret" not in repr(configured)


def test_openai_provider_uses_chat_completions_and_persists_provenance() -> None:
    calls: list[tuple[str, dict, dict, float]] = []

    def transport(endpoint, payload, headers, timeout):
        calls.append((endpoint, dict(payload), dict(headers), timeout))
        return completion_payload("本地回复")

    provider = OpenAIChatProvider(
        endpoint="http://127.0.0.1:4243/v1",
        model="local-model",
        provider_name="local-test",
        api_key="secret-key",
        timeout_seconds=4.0,
        transport=transport,
    )
    result = provider.complete(
        [ProviderMessage(role="user", content="你好")],
        json_response=False,
        is_cancelled=lambda: False,
    )

    assert result.assistant_text == "本地回复"
    assert result.provider == "local-test"
    assert result.model == "local-model"
    endpoint, payload, headers, timeout = calls[0]
    assert endpoint == "http://127.0.0.1:4243/v1/chat/completions"
    assert payload["model"] == "local-model"
    assert payload["stream"] is False
    assert headers["Authorization"] == "Bearer secret-key"
    assert timeout == 4.0


def test_cloud_provider_requires_strict_assistant_and_goal_json() -> None:
    provider = OpenAIChatProvider(
        endpoint="https://planner.example/v1/chat/completions",
        model="cloud-model",
        provider_name="cloud-test",
        api_key="cloud-secret",
        transport=lambda *_: completion_payload(
            '```json\n{"assistant_text":"准备执行",'
            '"execution_goal":{"goal":"打开设置", "exact_text":null}}\n```'
        ),
    )

    result = provider.complete(
        [ProviderMessage(role="user", content="打开设置")],
        json_response=True,
        is_cancelled=lambda: False,
    )

    assert result.assistant_text == "准备执行"
    assert result.execution_goal == {"goal": "打开设置", "exact_text": None}

    provider.transport = lambda *_: completion_payload(
        '{"assistant_text":"bad","execution_goal":{"task":"不稳定字段"}}'
    )
    with pytest.raises(ChatProviderError) as invalid:
        provider.complete(
            [ProviderMessage(role="user", content="x")],
            json_response=True,
            is_cancelled=lambda: False,
        )
    assert invalid.value.code == "provider_invalid_goal"

    provider.transport = lambda *_: completion_payload(
        '{"assistant_text":"bad","execution_goal":{"goal":"打开设置","exact_text":""}}'
    )
    with pytest.raises(ChatProviderError) as empty_exact_text:
        provider.complete(
            [ProviderMessage(role="user", content="x")],
            json_response=True,
            is_cancelled=lambda: False,
        )
    assert empty_exact_text.value.code == "provider_invalid_goal"


def test_provider_errors_never_include_transport_secret_or_key() -> None:
    def failing_transport(*_):
        raise RuntimeError("leaked-provider-body secret-key")

    provider = OpenAIChatProvider(
        endpoint="https://planner.example/v1",
        model="cloud-model",
        provider_name="cloud-test",
        api_key="secret-key",
        transport=failing_transport,
    )

    with pytest.raises(ChatProviderError) as failure:
        provider.complete(
            [ProviderMessage(role="user", content="hello")],
            json_response=True,
            is_cancelled=lambda: False,
        )

    assert failure.value.code == "provider_transport_failed"
    assert "secret-key" not in failure.value.public_message
    assert "leaked-provider-body" not in failure.value.public_message
    assert "secret-key" not in repr(provider)
