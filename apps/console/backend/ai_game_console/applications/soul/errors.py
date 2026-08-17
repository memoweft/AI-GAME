from __future__ import annotations


class SoulApplicationError(RuntimeError):
    """Sanitized Soul adapter failure safe for durable runtime state."""

    def __init__(self, code: str, public_message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.public_message = public_message or code

