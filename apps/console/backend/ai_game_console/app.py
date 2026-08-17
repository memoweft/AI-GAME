"""Compatibility ASGI entry point for direct Uvicorn use.

The Windows launcher uses ``ai_game_console.main`` so it can request a
per-process graceful Uvicorn shutdown.
"""

from .api import create_app

app = create_app()
