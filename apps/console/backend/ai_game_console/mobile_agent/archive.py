from __future__ import annotations

from pathlib import Path

from .domain import MobileTaskState
from .store import _SQLiteTaskStore


class MobileTaskArchive:
    """Read persisted task truth without starting workers or device adapters."""

    def __init__(self, database_path: Path | str) -> None:
        self._store = _SQLiteTaskStore(database_path)
        self._store.initialize()

    @property
    def database_path(self) -> Path:
        return self._store.database_path

    def inspect(self, task_id: str) -> MobileTaskState:
        return self._store.inspect(task_id)

    def list(self, limit: int = 100) -> list[MobileTaskState]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return self._store.list(limit)
