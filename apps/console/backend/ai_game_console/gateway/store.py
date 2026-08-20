"""Gateway-owned SQLite persistence (idempotency records only).

The Gateway owns a separate database file and never reads or writes
Runtime Kernel Store tables (contract §15). Schema versioning mirrors
the Runtime Store pattern: a ``gateway_schema`` revision table plus
``PRAGMA user_version``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_REVISION = 1

_SCHEMA = """
CREATE TABLE gateway_schema (
    revision INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE gateway_idempotency (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


class GatewayStoreError(RuntimeError):
    """Base error for the Gateway-owned store."""


class GatewayStoreConflict(GatewayStoreError):
    """Raised when the existing database state contradicts expectations."""


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    scope: str
    key: str
    payload_hash: str
    response_json: str
    created_at: str


class GatewayStore:
    """SQLite adapter for Gateway-owned idempotency records."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not tables:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO gateway_schema(revision, applied_at) VALUES (1, ?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            elif "gateway_schema" not in tables:
                raise GatewayStoreConflict(
                    "existing database has no gateway_schema version fact"
                )

            row = connection.execute(
                "SELECT MAX(revision) FROM gateway_schema"
            ).fetchone()
            revision = int(row[0]) if row and row[0] is not None else 0
            if revision > _SCHEMA_REVISION:
                raise GatewayStoreConflict(
                    f"gateway schema revision {revision} is newer than supported"
                )
            if revision < 1:
                raise GatewayStoreConflict("gateway schema revision is missing or invalid")

    def close(self) -> None:
        # Connections are per-operation; nothing to close here.
        return None

    def get_idempotency(self, scope: str, key: str) -> IdempotencyRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT scope, key, payload_hash, response_json, created_at"
                " FROM gateway_idempotency WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            scope=row["scope"],
            key=row["key"],
            payload_hash=row["payload_hash"],
            response_json=row["response_json"],
            created_at=row["created_at"],
        )

    def put_idempotency(
        self,
        *,
        scope: str,
        key: str,
        payload_hash: str,
        response_json: str,
    ) -> None:
        """Insert a record; raises ``sqlite3.IntegrityError`` on duplicate (scope, key)."""
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO gateway_idempotency"
                " (scope, key, payload_hash, response_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    scope,
                    key,
                    payload_hash,
                    response_json,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
