"""R3 Continuation persistence boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .domain import Continuation, ContinuationDraft
from .store import SQLiteAgentRuntimeStore


class ContinuationRepository:
    """Small adapter used by the scheduler without exposing SQLite details."""

    def __init__(self, store: SQLiteAgentRuntimeStore) -> None:
        self.store = store

    def save(
        self, session_id: str, goal_id: str, draft: ContinuationDraft
    ) -> tuple[Continuation, bool]:
        return self.store.append_continuation(session_id, goal_id, draft)

    def latest(self, goal_id: str) -> Continuation | None:
        return self.store.latest_continuation(goal_id)

    def history(self, goal_id: str) -> list[Continuation]:
        return self.store.continuations(goal_id)


class FreshReplanGate:
    """Persist the mandatory observation/replan checkpoint after takeover.

    Package B owns the actual ``release_takeover`` TaskControl transition.
    This adapter records the C-owned dispatch gate before applying release;
    no scheduler or preemption code may dispatch until an integration owner
    supplies a fresh, non-secret observation reference for the resulting Task
    revision.  The table is coordination state only, never a Task truth.
    """

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS long_task_replan_gates (task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, observation_ref TEXT, checkpoint_ref TEXT, released_at TEXT NOT NULL, satisfied_at TEXT)"
            )

    def require_after_release(
        self,
        task_service: Any,
        task_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        requested_by: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Durably block dispatch before applying release pending observation."""

        # The conservative ordering is intentional: a process crash cannot
        # leave a released Task dispatchable without this gate.  If the
        # canonical control subsequently conflicts, the leftover gate only
        # blocks work (safe) and a retry replaces it with the same revision.
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO long_task_replan_gates(task_id, revision, observation_ref, checkpoint_ref, released_at, satisfied_at) VALUES (?, ?, NULL, NULL, ?, NULL) "
                "ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision, observation_ref=NULL, checkpoint_ref=NULL, released_at=excluded.released_at, satisfied_at=NULL",
                (task_id, expected_revision, _now()),
            )

        response = task_service.control_task(
            task_id,
            action="release_takeover",
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            requested_by=requested_by,
        )
        task = dict(response["task"])
        if task.get("status") != "replanning":
            raise ValueError("release_takeover must enter replanning before dispatch")
        revision = int(task["current_revision"])
        if revision != expected_revision:
            raise ValueError("release_takeover changed the task revision unexpectedly")
        return response

    def satisfy(
        self,
        task_id: str,
        *,
        revision: int,
        observation_ref: str,
        checkpoint_ref: str,
    ) -> None:
        if not observation_ref.strip() or not checkpoint_ref.strip():
            raise ValueError("fresh observation and checkpoint references are required")
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT revision FROM long_task_replan_gates WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError("no takeover-release replan gate exists for task")
            if int(row["revision"]) != revision:
                raise ValueError("fresh observation does not match the current task revision")
            connection.execute(
                "UPDATE long_task_replan_gates SET observation_ref=?, checkpoint_ref=?, satisfied_at=? WHERE task_id=?",
                (observation_ref, checkpoint_ref, _now(), task_id),
            )

    def allows_dispatch(self, task_id: str, revision: int) -> bool:
        with self._connection(write=False) as connection:
            row = connection.execute(
                "SELECT revision, satisfied_at FROM long_task_replan_gates WHERE task_id=?", (task_id,)
            ).fetchone()
        # No release record means this task was not handed back from takeover.
        return row is None or (int(row["revision"]) == revision and row["satisfied_at"] is not None)

    def _connection(self, *, write: bool) -> "_GateConnection":
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute("BEGIN IMMEDIATE")
        return _GateConnection(connection)


class _GateConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        if kind is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
