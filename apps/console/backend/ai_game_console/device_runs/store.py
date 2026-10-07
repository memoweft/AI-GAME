"""Small execution journal; goals and executable scripts belong to the host."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


class DeviceRunStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS direct_device_runs (
                    run_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS direct_device_commands (
                    run_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY (run_id, command_id)
                );
            """)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT payload FROM direct_device_runs WHERE run_id=?", (run_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def save(self, run: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO direct_device_runs VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload",
                (run["run_id"], json.dumps(run, ensure_ascii=False)),
            )

    def command(self, run_id: str, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT request_hash, result FROM direct_device_commands WHERE run_id=? AND command_id=?",
                (run_id, command_id),
            ).fetchone()
            return {"request_hash": row["request_hash"], "result": json.loads(row["result"])} if row else None

    def save_command(self, run_id: str, command_id: str, request_hash: str, result: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO direct_device_commands VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id, command_id) DO UPDATE SET result=excluded.result",
                (run_id, command_id, request_hash, json.dumps(result, ensure_ascii=False)),
            )

    def recover(self, now: str) -> None:
        """An interrupted command has an unknown effect and is never replayed."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT payload FROM direct_device_runs").fetchall()
            for row in rows:
                run = json.loads(row["payload"])
                if run["status"] == "active":
                    run.update(status="paused", requires_observation=True, updated_at=now, reason="process_restarted")
                    self.save(run)
            commands = self._db.execute("SELECT * FROM direct_device_commands").fetchall()
            for row in commands:
                result = json.loads(row["result"])
                if result["outcome"] == "pending":
                    result.update(outcome="uncertain", accepted=False, requires_observation=True, status="paused")
                    result["detail"] = "The process stopped during this command. Observe the device before continuing; this command will not run again."
                    self.save_command(row["run_id"], row["command_id"], row["request_hash"], result)

    def close(self) -> None:
        with self._lock:
            self._db.close()
