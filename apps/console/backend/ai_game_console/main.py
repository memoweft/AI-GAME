from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence

import uvicorn

from .api import create_app
from .config import Settings


DEFAULT_CONSOLE_HOST = "127.0.0.1"
DEFAULT_CONSOLE_PORT = 4310


def resolve_listen_address(
    *,
    host: str | None = None,
    port: int | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, int]:
    """Resolve the only supported local listener address."""

    values = os.environ if env is None else env
    resolved_host = (
        host if host is not None else values.get("AI_GAME_CONSOLE_HOST", "")
    ).strip() or DEFAULT_CONSOLE_HOST
    if resolved_host != DEFAULT_CONSOLE_HOST:
        raise ValueError("AI-GAME console must listen on 127.0.0.1 only")

    if port is None:
        raw_port = values.get("AI_GAME_CONSOLE_PORT", "").strip()
        if not raw_port:
            resolved_port = DEFAULT_CONSOLE_PORT
        else:
            try:
                resolved_port = int(raw_port)
            except ValueError as error:
                raise ValueError("AI_GAME_CONSOLE_PORT must be an integer") from error
    else:
        resolved_port = port
    if not 1024 <= resolved_port <= 65535:
        raise ValueError("AI_GAME_CONSOLE_PORT must be between 1024 and 65535")
    return resolved_host, resolved_port


def create_server(
    *,
    settings: Settings | None = None,
    host: str | None = None,
    port: int | None = None,
    env: Mapping[str, str] | None = None,
) -> uvicorn.Server:
    """Build the managed Uvicorn server used by the local launcher."""

    resolved_host, resolved_port = resolve_listen_address(
        host=host,
        port=port,
        env=env,
    )
    server_box: dict[str, uvicorn.Server] = {}

    def request_shutdown() -> None:
        server_box["server"].should_exit = True

    app = create_app(
        settings=settings or Settings.from_env(env),
        console_shutdown_callback=request_shutdown,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=resolved_host,
            port=resolved_port,
            reload=False,
            access_log=False,
        )
    )
    server_box["server"] = server
    app.state.control_server = server
    return server


def run(host: str | None = None, port: int | None = None) -> None:
    create_server(host=host, port=port).run()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AI-GAME local console.")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    return parser.parse_args(argv)


if __name__ == "__main__":
    arguments = _parse_args()
    run(host=arguments.host, port=arguments.port)
