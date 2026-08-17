from __future__ import annotations

import pytest

from ai_game_console import main

from conftest import build_settings


def test_resolve_listen_address_defaults_to_the_launcher_port() -> None:
    assert main.resolve_listen_address(env={}) == ("127.0.0.1", 4310)
    assert main.resolve_listen_address(
        env={
            "AI_GAME_CONSOLE_HOST": "127.0.0.1",
            "AI_GAME_CONSOLE_PORT": "4311",
        }
    ) == ("127.0.0.1", 4311)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"host": "localhost"}, "127.0.0.1"),
        ({"port": 80}, "between 1024 and 65535"),
        ({"env": {"AI_GAME_CONSOLE_PORT": "not-a-port"}}, "must be an integer"),
    ],
)
def test_resolve_listen_address_rejects_nonlocal_or_invalid_values(
    kwargs, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        main.resolve_listen_address(**kwargs)


def test_create_server_exposes_a_graceful_shutdown_callback(tmp_path) -> None:
    server = main.create_server(
        settings=build_settings(tmp_path),
        host="127.0.0.1",
        port=4310,
    )

    assert server.config.host == "127.0.0.1"
    assert server.config.port == 4310
    assert server.should_exit is False
    assert server.config.app.state.control_server is server

    server.config.app.state.console_shutdown_callback()

    assert server.should_exit is True
