from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .domain import (
    Approval,
    ApprovalStatus,
    Event,
    Run,
    RunStatus,
    Target,
    TargetKind,
    Workflow,
)


class RepositoryError(RuntimeError):
    """Base class for persistence failures with domain meaning."""


class RecordNotFound(RepositoryError):
    pass


class ConcurrentUpdate(RepositoryError):
    pass


class ChatSessionBusy(ConcurrentUpdate):
    pass


class IdempotencyConflict(ConcurrentUpdate):
    pass


def utc_now() -> str:
    """Return a stable, sortable UTC timestamp without local-time ambiguity."""

    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


WORKFLOW_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "id": "windows-general",
        "name": "通用 Windows 软件",
        "description": "用于 Windows 原生软件的通用工作流。",
        "target_kind": "windows",
        "enabled": True,
        "integration_status": "ready",
        "requires_approval": False,
    },
    {
        "id": "android-emulator",
        "name": "Android 模拟器",
        "description": "用于已连接 Android 模拟器的通用工作流。",
        "target_kind": "android",
        "enabled": True,
        "integration_status": "ready",
        "requires_approval": False,
    },
    {
        "id": "soul",
        "name": "Soul 本地应用",
        "description": (
            "已通过独立 Soul 工作台接入；传统任务队列不执行该应用，"
            "请从 Soul 工作台查看和控制。"
        ),
        "target_kind": "android",
        "enabled": False,
        "integration_status": "external",
        "requires_approval": True,
    },
)


_RUN_SELECT = """
SELECT r.*, w.name AS workflow_name, t.name AS target_name
FROM runs AS r
JOIN workflows AS w ON w.id = r.workflow_id
JOIN targets AS t ON t.id = r.target_id
"""


class SQLiteRepository:
    """The single persistence boundary for the local control plane.

    A new SQLite connection is used for each operation so FastAPI worker threads
    never share connection state. Multi-row state changes use ``BEGIN IMMEDIATE``
    and append their audit event in the same transaction.
    """

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS workflows (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL,
                        target_kind TEXT NOT NULL CHECK (target_kind IN ('windows', 'android')),
                        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                        integration_status TEXT NOT NULL,
                        requires_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0, 1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS targets (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('windows', 'android')),
                        status TEXT NOT NULL,
                        source TEXT NOT NULL,
                        external_id TEXT,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        discovered_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        workflow_id TEXT NOT NULL REFERENCES workflows(id),
                        target_id TEXT NOT NULL REFERENCES targets(id),
                        instruction TEXT NOT NULL,
                        exact_text TEXT,
                        requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0, 1)),
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'awaiting_approval', 'queued', 'running', 'paused',
                                'completed', 'failed', 'cancelled'
                            )
                        ),
                        blocker TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_runs_created_at
                    ON runs(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_runs_status
                    ON runs(status);

                    CREATE TABLE IF NOT EXISTS approvals (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                        status TEXT NOT NULL CHECK (
                            status IN ('pending', 'approved', 'rejected', 'withdrawn')
                        ),
                        note TEXT,
                        created_at TEXT NOT NULL,
                        decided_at TEXT,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_approvals_status
                    ON approvals(status);

                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT REFERENCES runs(id),
                        event_type TEXT NOT NULL,
                        message TEXT NOT NULL,
                        level TEXT NOT NULL DEFAULT 'info',
                        data_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_events_created_at
                    ON events(created_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_events_run_id
                    ON events(run_id, id DESC);

                    CREATE TABLE IF NOT EXISTS chat_sessions (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK (mode IN ('local_chat', 'cloud_execute')),
                        target_id TEXT REFERENCES targets(id),
                        auto_execute INTEGER NOT NULL CHECK (auto_execute IN (0, 1)),
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated_at
                    ON chat_sessions(updated_at DESC, id DESC);

                    CREATE TABLE IF NOT EXISTS chat_turns (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES chat_sessions(id),
                        client_request_id TEXT NOT NULL,
                        request_content_sha256 TEXT NOT NULL,
                        input_revision INTEGER NOT NULL DEFAULT 1 CHECK (input_revision >= 1),
                        mode TEXT NOT NULL CHECK (mode IN ('local_chat', 'cloud_execute')),
                        target_id TEXT REFERENCES targets(id),
                        auto_execute INTEGER NOT NULL CHECK (auto_execute IN (0, 1)),
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'accepted', 'queued', 'thinking', 'planning', 'executing',
                                'awaiting_user', 'stopping', 'completed', 'failed', 'cancelled'
                            )
                        ),
                        reply_status TEXT,
                        execution_status TEXT,
                        step_count INTEGER NOT NULL DEFAULT 0,
                        blocker TEXT,
                        detail TEXT,
                        error_code TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
                        provider TEXT,
                        model TEXT,
                        execution_goal_json TEXT,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, client_request_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_chat_turns_session
                    ON chat_turns(session_id, created_at, id);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_turns_one_active
                    ON chat_turns(session_id)
                    WHERE status IN (
                        'accepted', 'queued', 'thinking', 'planning', 'executing', 'stopping'
                    );

                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES chat_sessions(id),
                        turn_id TEXT REFERENCES chat_turns(id),
                        role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                        content TEXT NOT NULL,
                        client_request_id TEXT,
                        content_sha256 TEXT,
                        input_revision INTEGER,
                        delivery_status TEXT CHECK (
                            delivery_status IS NULL
                            OR delivery_status IN ('queued', 'applied', 'rejected')
                        ),
                        applied_at TEXT,
                        provider TEXT,
                        model TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_chat_messages_session
                    ON chat_messages(session_id, created_at, id);

                    CREATE TABLE IF NOT EXISTS chat_steps (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        turn_id TEXT NOT NULL REFERENCES chat_turns(id),
                        step_index INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        action_type TEXT,
                        summary TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(turn_id, step_index)
                    );

                    CREATE INDEX IF NOT EXISTS idx_chat_steps_turn
                    ON chat_steps(turn_id, step_index, id);

                    CREATE TABLE IF NOT EXISTS cloud_chat_config (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        endpoint TEXT,
                        model TEXT,
                        api_key_protected BLOB,
                        revision INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK (
                            (endpoint IS NULL AND model IS NULL AND api_key_protected IS NULL)
                            OR
                            (endpoint IS NOT NULL AND model IS NOT NULL AND api_key_protected IS NOT NULL)
                        )
                    );

                    """
                )
                self._migrate_chat_schema_v4(connection)
                connection.execute("PRAGMA user_version = 4")
                self._seed(connection)
                connection.commit()
            self._initialized = True

    @staticmethod
    def _migrate_chat_schema_v4(connection: sqlite3.Connection) -> None:
        """Add the unified-inbox fields without rebuilding populated v3 tables."""

        turn_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(chat_turns)").fetchall()
        }
        if "input_revision" not in turn_columns:
            connection.execute(
                "ALTER TABLE chat_turns "
                "ADD COLUMN input_revision INTEGER NOT NULL DEFAULT 1"
            )

        message_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(chat_messages)").fetchall()
        }
        additions = {
            "client_request_id": "TEXT",
            "content_sha256": "TEXT",
            "input_revision": "INTEGER",
            "delivery_status": "TEXT",
            "applied_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in message_columns:
                connection.execute(
                    f"ALTER TABLE chat_messages ADD COLUMN {name} {declaration}"
                )

        # A v3 user message was the sole input of its Turn and was immediately
        # visible to the worker. Preserve that meaning as applied revision 1.
        connection.execute(
            """
            UPDATE chat_messages
            SET client_request_id = COALESCE(
                    client_request_id,
                    (SELECT t.client_request_id FROM chat_turns AS t WHERE t.id = turn_id)
                ),
                content_sha256 = COALESCE(
                    content_sha256,
                    (SELECT t.request_content_sha256 FROM chat_turns AS t WHERE t.id = turn_id)
                ),
                input_revision = COALESCE(input_revision, 1),
                delivery_status = COALESCE(delivery_status, 'applied'),
                applied_at = COALESCE(applied_at, created_at)
            WHERE role = 'user' AND turn_id IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_request
            ON chat_messages(session_id, client_request_id)
            WHERE client_request_id IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_turn_revision
            ON chat_messages(turn_id, input_revision)
            WHERE input_revision IS NOT NULL
            """
        )

    def _seed(self, connection: sqlite3.Connection) -> None:
        now = utc_now()
        for item in WORKFLOW_SEEDS:
            connection.execute(
                """
                INSERT INTO workflows (
                    id, name, description, target_kind, enabled,
                    integration_status, requires_approval, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    target_kind = excluded.target_kind,
                    enabled = excluded.enabled,
                    integration_status = excluded.integration_status,
                    requires_approval = excluded.requires_approval,
                    updated_at = excluded.updated_at
                """,
                (
                    item["id"],
                    item["name"],
                    item["description"],
                    item["target_kind"],
                    int(item["enabled"]),
                    item["integration_status"],
                    int(item["requires_approval"]),
                    now,
                    now,
                ),
            )

        details = json.dumps(
            {
                "address": "localhost",
                "detail": "当前这台 Windows 电脑",
                "capabilities": ["windows_desktop"],
            },
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO targets (
                id, name, kind, status, source, external_id, details_json,
                discovered_at, last_seen_at, updated_at
            ) VALUES ('windows-local', '本机 Windows 桌面', 'windows', 'ready',
                      'local', 'localhost', ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                kind = excluded.kind,
                status = excluded.status,
                source = excluded.source,
                external_id = excluded.external_id,
                details_json = excluded.details_json,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (details, now, now, now),
        )

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def ping(self) -> bool:
        with self._connection() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    def get_cloud_chat_config(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM cloud_chat_config WHERE id = 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def write_cloud_chat_config(
        self,
        *,
        endpoint: str,
        model: str,
        api_key_protected: bytes,
        expected_revision: int,
    ) -> dict[str, Any]:
        return self._replace_cloud_chat_config(
            endpoint=endpoint,
            model=model,
            api_key_protected=api_key_protected,
            expected_revision=expected_revision,
        )

    def clear_cloud_chat_config(self, *, expected_revision: int) -> dict[str, Any]:
        return self._replace_cloud_chat_config(
            endpoint=None,
            model=None,
            api_key_protected=None,
            expected_revision=expected_revision,
        )

    def _replace_cloud_chat_config(
        self,
        *,
        endpoint: str | None,
        model: str | None,
        api_key_protected: bytes | None,
        expected_revision: int,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT revision FROM cloud_chat_config WHERE id = 1"
            ).fetchone()
            current_revision = int(row["revision"]) if row is not None else 0
            if current_revision != expected_revision:
                raise ConcurrentUpdate("cloud chat config revision changed")
            next_revision = current_revision + 1
            if row is None:
                connection.execute(
                    """
                    INSERT INTO cloud_chat_config (
                        id, endpoint, model, api_key_protected, revision, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (
                        endpoint,
                        model,
                        api_key_protected,
                        next_revision,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE cloud_chat_config
                    SET endpoint = ?, model = ?, api_key_protected = ?,
                        revision = ?, updated_at = ?
                    WHERE id = 1 AND revision = ?
                    """,
                    (
                        endpoint,
                        model,
                        api_key_protected,
                        next_revision,
                        now,
                        current_revision,
                    ),
                )
            stored = connection.execute(
                "SELECT * FROM cloud_chat_config WHERE id = 1"
            ).fetchone()
        return dict(stored)

    def list_workflows(self) -> list[Workflow]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workflows ORDER BY enabled DESC, name COLLATE NOCASE"
            ).fetchall()
        return [self._workflow_from_row(row) for row in rows]

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        return self._workflow_from_row(row) if row else None

    def list_targets(self) -> list[Target]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM targets
                ORDER BY CASE status
                    WHEN 'ready' THEN 0
                    WHEN 'offline' THEN 1
                    WHEN 'unauthorized' THEN 2
                    ELSE 3 END,
                    name COLLATE NOCASE
                """
            ).fetchall()
        return [self._target_from_row(row) for row in rows]

    def get_target(self, target_id: str) -> Target | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM targets WHERE id = ?", (target_id,)
            ).fetchone()
        return self._target_from_row(row) if row else None

    def replace_adb_targets(self, targets: Sequence[Target]) -> list[Target]:
        now = utc_now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                UPDATE targets
                SET status = 'unknown', updated_at = ?
                WHERE source = 'adb'
                """,
                (now,),
            )
            for target in targets:
                connection.execute(
                    """
                    INSERT INTO targets (
                        id, name, kind, status, source, external_id, details_json,
                        discovered_at, last_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        kind = excluded.kind,
                        status = excluded.status,
                        source = excluded.source,
                        external_id = excluded.external_id,
                        details_json = excluded.details_json,
                        last_seen_at = excluded.last_seen_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        target.id,
                        target.name,
                        target.kind.value,
                        target.status,
                        target.source,
                        target.external_id,
                        json.dumps(target.details, separators=(",", ":"), ensure_ascii=False),
                        target.discovered_at,
                        target.last_seen_at,
                        target.updated_at,
                    ),
                )
        return self.list_targets()

    def create_run(self, run: Run, approval: Approval | None) -> Run:
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, name, workflow_id, target_id, instruction, exact_text,
                    requires_approval, status, blocker, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.name,
                    run.workflow_id,
                    run.target_id,
                    run.instruction,
                    run.exact_text,
                    int(run.requires_approval),
                    run.status.value,
                    run.blocker,
                    run.created_at,
                    run.updated_at,
                ),
            )
            self._insert_event(
                connection,
                run_id=run.id,
                event_type="run_created",
                message="运行已创建。",
                level="info",
                data={
                    "workflow_id": run.workflow_id,
                    "target_id": run.target_id,
                    "status": run.status.value,
                    "requires_approval": run.requires_approval,
                },
                created_at=run.created_at,
            )
            if approval is not None:
                connection.execute(
                    """
                    INSERT INTO approvals (
                        id, run_id, status, note, created_at, decided_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval.id,
                        approval.run_id,
                        approval.status.value,
                        approval.note,
                        approval.created_at,
                        approval.decided_at,
                        approval.updated_at,
                    ),
                )
                self._insert_event(
                    connection,
                    run_id=run.id,
                    event_type="approval_requested",
                    message="已发起本地审批。",
                    level="info",
                    data={"approval_id": approval.id},
                    created_at=approval.created_at,
                )
        created = self.get_run(run.id)
        if created is None:  # pragma: no cover - SQLite write/read invariant
            raise RepositoryError("created run could not be read")
        return created

    def list_runs(self, *, limit: int = 100) -> list[Run]:
        with self._connection() as connection:
            rows = connection.execute(
                _RUN_SELECT + " ORDER BY r.created_at DESC, r.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def get_run(self, run_id: str) -> Run | None:
        with self._connection() as connection:
            row = connection.execute(
                _RUN_SELECT + " WHERE r.id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(row) if row else None

    def transition_run(
        self,
        *,
        run_id: str,
        expected_status: RunStatus,
        new_status: RunStatus,
        blocker: str | None,
        event_type: str,
        message: str,
    ) -> Run:
        now = utc_now()
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, blocker = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (new_status.value, blocker, now, run_id, expected_status.value),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if exists is None:
                    raise RecordNotFound(f"run {run_id!r} was not found")
                raise ConcurrentUpdate(f"run {run_id!r} changed concurrently")

            if new_status is RunStatus.CANCELLED:
                withdrawn = connection.execute(
                    """
                    UPDATE approvals
                    SET status = 'withdrawn', note = COALESCE(note, '运行已取消'),
                        decided_at = ?, updated_at = ?
                    WHERE run_id = ? AND status = 'pending'
                    """,
                    (now, now, run_id),
                )
                if withdrawn.rowcount:
                    self._insert_event(
                        connection,
                        run_id=run_id,
                        event_type="approval_withdrawn",
                        message="运行已取消，待处理审批同步撤回。",
                        level="info",
                        data={},
                        created_at=now,
                    )

            self._insert_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                message=message,
                level="info",
                data={"from_status": expected_status.value, "to_status": new_status.value},
                created_at=now,
            )
        updated = self.get_run(run_id)
        if updated is None:  # pragma: no cover
            raise RepositoryError("updated run could not be read")
        return updated

    def list_approvals(self) -> list[Approval]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approvals
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                         created_at DESC, id DESC
                """
            ).fetchall()
        return [self._approval_from_row(row) for row in rows]

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return self._approval_from_row(row) if row else None

    def decide_approval(
        self,
        *,
        approval_id: str,
        decision: ApprovalStatus,
        note: str | None,
    ) -> tuple[Approval, Run]:
        if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("decision must be approved or rejected")
        now = utc_now()
        run_status = (
            RunStatus.QUEUED if decision is ApprovalStatus.APPROVED else RunStatus.CANCELLED
        )
        blocker = (
            "workflow_executor_not_connected"
            if decision is ApprovalStatus.APPROVED
            else "approval_rejected"
        )
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"approval {approval_id!r} was not found")
            approval = self._approval_from_row(row)
            if approval.status is not ApprovalStatus.PENDING:
                raise ConcurrentUpdate(
                    f"approval {approval_id!r} is already {approval.status.value}"
                )

            run_row = connection.execute(
                "SELECT status FROM runs WHERE id = ?", (approval.run_id,)
            ).fetchone()
            if run_row is None:
                raise RecordNotFound(f"run {approval.run_id!r} was not found")
            if run_row["status"] != RunStatus.AWAITING_APPROVAL.value:
                raise ConcurrentUpdate(
                    f"run {approval.run_id!r} is already {run_row['status']}"
                )

            connection.execute(
                """
                UPDATE approvals
                SET status = ?, note = ?, decided_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (decision.value, note, now, now, approval_id),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, blocker = ?, updated_at = ?
                WHERE id = ? AND status = 'awaiting_approval'
                """,
                (run_status.value, blocker, now, approval.run_id),
            )
            self._insert_event(
                connection,
                run_id=approval.run_id,
                event_type=f"approval_{decision.value}",
                message=("审批已批准。" if decision is ApprovalStatus.APPROVED else "审批已拒绝。"),
                level="info" if decision is ApprovalStatus.APPROVED else "warning",
                data={"approval_id": approval_id, "decision": decision.value},
                created_at=now,
            )

        updated_approval = self.get_approval(approval_id)
        updated_run = self.get_run(approval.run_id)
        if updated_approval is None or updated_run is None:  # pragma: no cover
            raise RepositoryError("approval decision could not be read")
        return updated_approval, updated_run

    def append_event(
        self,
        *,
        event_type: str,
        message: str,
        level: str = "info",
        run_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Event:
        now = utc_now()
        with self._connection(write=True) as connection:
            event_id = self._insert_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                message=message,
                level=level,
                data=data or {},
                created_at=now,
            )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:  # pragma: no cover
            raise RepositoryError("created event could not be read")
        return self._event_from_row(row)

    def list_events(self, *, limit: int = 100, run_id: str | None = None) -> list[Event]:
        query = "SELECT * FROM events"
        parameters: tuple[Any, ...]
        if run_id is not None:
            query += " WHERE run_id = ?"
            parameters = (run_id, limit)
        else:
            parameters = (limit,)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._event_from_row(row) for row in rows]

    def create_chat_session(
        self,
        *,
        session_id: str,
        title: str,
        mode: str,
        target_id: str | None,
        auto_execute: bool,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO chat_sessions (
                    id, title, mode, target_id, auto_execute, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (session_id, title, mode, target_id, int(auto_execute), now, now),
            )
            row = connection.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - SQLite write invariant
            raise RepositoryError("created chat session could not be read")
        return self._chat_session_from_row(row)

    def list_chat_sessions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_sessions
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._chat_session_from_row(row) for row in rows]

    def get_chat_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._chat_session_from_row(row) if row else None

    def get_chat_turn(self, turn_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return self._chat_turn_from_row(row) if row else None

    def get_chat_turn_by_request(
        self,
        *,
        session_id: str,
        client_request_id: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT t.*, m.client_request_id AS matched_client_request_id,
                       m.content_sha256 AS matched_content_sha256
                FROM chat_messages AS m
                JOIN chat_turns AS t ON t.id = m.turn_id
                WHERE m.session_id = ? AND m.client_request_id = ?
                """,
                (session_id, client_request_id),
            ).fetchone()
        if row is None:
            return None
        item = self._chat_turn_from_row(row)
        item["client_request_id"] = row["matched_client_request_id"]
        item["request_content_sha256"] = row["matched_content_sha256"]
        item["matched_content_sha256"] = row["matched_content_sha256"]
        return item

    def get_chat_transcript(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            session_row = connection.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session_row is None:
                return None
            message_rows = connection.execute(
                """
                SELECT * FROM chat_messages
                WHERE session_id = ?
                ORDER BY rowid
                """,
                (session_id,),
            ).fetchall()
            turn_rows = connection.execute(
                """
                SELECT * FROM chat_turns
                WHERE session_id = ?
                ORDER BY rowid
                """,
                (session_id,),
            ).fetchall()
            step_rows = connection.execute(
                """
                SELECT s.* FROM chat_steps AS s
                JOIN chat_turns AS t ON t.id = s.turn_id
                WHERE t.session_id = ?
                ORDER BY t.created_at, t.id, s.step_index, s.id
                """,
                (session_id,),
            ).fetchall()
        return {
            "session": self._chat_session_from_row(session_row),
            "messages": [self._chat_message_from_row(row) for row in message_rows],
            "turns": [self._chat_turn_from_row(row) for row in turn_rows],
            "steps": [self._chat_step_from_row(row) for row in step_rows],
        }

    def accept_chat_turn(
        self,
        *,
        turn_id: str,
        message_id: str,
        session_id: str,
        client_request_id: str,
        request_content_sha256: str,
        content: str,
        create_if_missing: bool = True,
    ) -> tuple[dict[str, Any] | None, bool]:
        now = utc_now()
        with self._connection(write=True) as connection:
            session_row = connection.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session_row is None:
                raise RecordNotFound(f"chat session {session_id!r} was not found")
            existing_message = connection.execute(
                """
                SELECT turn_id, content_sha256 FROM chat_messages
                WHERE session_id = ? AND client_request_id = ?
                """,
                (session_id, client_request_id),
            ).fetchone()
            if existing_message is not None:
                if existing_message["content_sha256"] != request_content_sha256:
                    raise IdempotencyConflict(client_request_id)
                existing_turn = connection.execute(
                    "SELECT * FROM chat_turns WHERE id = ?",
                    (existing_message["turn_id"],),
                ).fetchone()
                if existing_turn is None:  # pragma: no cover - foreign-key invariant
                    raise RepositoryError("idempotent chat input has no owning turn")
                return self._chat_turn_from_row(existing_turn), False

            active = connection.execute(
                """
                SELECT * FROM chat_turns
                WHERE session_id = ?
                  AND status IN ('accepted', 'queued', 'thinking', 'planning', 'executing', 'stopping')
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if active is not None:
                if active["status"] == "stopping":
                    raise ChatSessionBusy(session_id)
                next_revision = int(active["input_revision"]) + 1
                connection.execute(
                    """
                    UPDATE chat_turns
                    SET input_revision = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_revision, now, active["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO chat_messages (
                        id, session_id, turn_id, role, content,
                        client_request_id, content_sha256, input_revision,
                        delivery_status, applied_at, provider, model, created_at
                    ) VALUES (?, ?, ?, 'user', ?, ?, ?, ?, 'queued', NULL, NULL, NULL, ?)
                    """,
                    (
                        message_id,
                        session_id,
                        active["id"],
                        content,
                        client_request_id,
                        request_content_sha256,
                        next_revision,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                joined = connection.execute(
                    "SELECT * FROM chat_turns WHERE id = ?", (active["id"],)
                ).fetchone()
                return self._chat_turn_from_row(joined), False

            if not create_if_missing:
                return None, False

            mode = session_row["mode"]
            auto_execute = bool(session_row["auto_execute"])
            connection.execute(
                """
                INSERT INTO chat_turns (
                    id, session_id, client_request_id, request_content_sha256,
                    input_revision, mode, target_id, auto_execute, status, reply_status,
                    execution_status, step_count, cancel_requested, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'accepted', 'pending', ?, 0, 0, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    client_request_id,
                    request_content_sha256,
                    mode,
                    session_row["target_id"],
                    int(auto_execute),
                    "pending" if mode == "cloud_execute" and auto_execute else "not_requested",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO chat_messages (
                    id, session_id, turn_id, role, content,
                    client_request_id, content_sha256, input_revision,
                    delivery_status, applied_at, provider, model, created_at
                ) VALUES (?, ?, ?, 'user', ?, ?, ?, 1, 'queued', NULL, NULL, NULL, ?)
                """,
                (
                    message_id,
                    session_id,
                    turn_id,
                    content,
                    client_request_id,
                    request_content_sha256,
                    now,
                ),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        if row is None:  # pragma: no cover
            raise RepositoryError("accepted chat turn could not be read")
        return self._chat_turn_from_row(row), True

    def claim_chat_turn_inputs(
        self,
        turn_id: str,
        *,
        after_revision: int | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Apply queued inputs and return one transactionally consistent snapshot."""

        now = utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(turn_id)
            current = self._chat_turn_from_row(row)
            if current["status"] not in {
                "accepted",
                "queued",
                "thinking",
                "planning",
                "executing",
            } or current["cancel_requested"]:
                return current, []
            connection.execute(
                """
                UPDATE chat_messages
                SET delivery_status = 'applied', applied_at = ?
                WHERE turn_id = ? AND role = 'user' AND delivery_status = 'queued'
                """,
                (now, turn_id),
            )
            parameters: list[Any] = [turn_id]
            revision_clause = ""
            if after_revision is not None:
                revision_clause = "AND input_revision > ?"
                parameters.append(after_revision)
            message_rows = connection.execute(
                f"""
                SELECT * FROM chat_messages
                WHERE turn_id = ? AND role = 'user'
                  AND delivery_status = 'applied'
                  {revision_clause}
                ORDER BY input_revision, created_at, id
                """,
                parameters,
            ).fetchall()
            updated = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return self._chat_turn_from_row(updated), [
            self._chat_message_from_row(message) for message in message_rows
        ]

    def begin_chat_turn(
        self, turn_id: str, phase: str
    ) -> tuple[dict[str, Any], bool]:
        if phase not in {"thinking", "planning"}:
            raise ValueError("invalid initial chat phase")
        now = utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(turn_id)
            current = self._chat_turn_from_row(row)
            if current["cancel_requested"] or current["status"] != "accepted":
                return current, False
            connection.execute(
                """
                UPDATE chat_turns
                SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ? AND status = 'accepted' AND cancel_requested = 0
                """,
                (phase, now, now, turn_id),
            )
            updated = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return self._chat_turn_from_row(updated), True

    def record_chat_reply(
        self,
        *,
        turn_id: str,
        expected_input_revision: int,
        message_id: str,
        content: str,
        provider: str,
        model: str,
        execution_goal: Mapping[str, Any] | None,
        next_status: str,
        execution_status: str,
        blocker: str | None,
        detail: str | None,
        error_code: str | None,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        goal_json = (
            json.dumps(execution_goal, ensure_ascii=False, separators=(",", ":"))
            if execution_goal is not None
            else None
        )
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(turn_id)
            current = self._chat_turn_from_row(row)
            if (
                current["input_revision"] != expected_input_revision
                or current["cancel_requested"]
                or current["status"]
                not in {"thinking", "planning", "executing"}
            ):
                return current, False
            connection.execute(
                """
                INSERT INTO chat_messages (
                    id, session_id, turn_id, role, content, provider, model, created_at
                ) VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?)
                """,
                (
                    message_id,
                    current["session_id"],
                    turn_id,
                    content,
                    provider,
                    model,
                    now,
                ),
            )
            finished_at = now if next_status in {"completed", "failed", "cancelled"} else None
            connection.execute(
                """
                UPDATE chat_turns
                SET status = ?, reply_status = 'completed', execution_status = ?,
                    blocker = ?, detail = ?, error_code = ?, provider = ?, model = ?,
                    execution_goal_json = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    execution_status,
                    blocker,
                    detail,
                    error_code,
                    provider,
                    model,
                    goal_json,
                    finished_at,
                    now,
                    turn_id,
                ),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, current["session_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return self._chat_turn_from_row(updated), True

    def append_chat_step(
        self,
        *,
        turn_id: str,
        step_index: int,
        state: str,
        action_type: str | None,
        summary: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connection(write=True) as connection:
            turn = connection.execute(
                "SELECT session_id, status, cancel_requested FROM chat_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if turn is None:
                raise RecordNotFound(turn_id)
            if turn["status"] != "executing" or bool(turn["cancel_requested"]):
                return None
            connection.execute(
                """
                INSERT INTO chat_steps (
                    turn_id, step_index, state, action_type, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id, step_index) DO UPDATE SET
                    state = excluded.state,
                    action_type = excluded.action_type,
                    summary = excluded.summary
                """,
                (turn_id, step_index, state, action_type, summary, now),
            )
            connection.execute(
                """
                UPDATE chat_turns
                SET step_count = (SELECT COUNT(*) FROM chat_steps WHERE turn_id = ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (turn_id, now, turn_id),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, turn["session_id"]),
            )
            row = connection.execute(
                """
                SELECT * FROM chat_steps
                WHERE turn_id = ? AND step_index = ?
                """,
                (turn_id, step_index),
            ).fetchone()
        return self._chat_step_from_row(row) if row else None

    def finish_chat_turn(
        self,
        *,
        turn_id: str,
        status: str,
        reply_status: str,
        execution_status: str,
        blocker: str | None,
        detail: str | None,
        error_code: str | None,
        expected_input_revision: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(turn_id)
            current = self._chat_turn_from_row(row)
            if current["status"] in {"completed", "failed", "cancelled"}:
                return current
            if current["cancel_requested"]:
                if status != "cancelled":
                    status = "cancelled"
                    reply_status = (
                        "completed"
                        if current["reply_status"] == "completed"
                        else "cancelled"
                    )
                    execution_status = (
                        "not_requested"
                        if current["execution_status"] == "not_requested"
                        else "cancelled"
                    )
                    blocker = "chat_turn_cancelled"
                    detail = "聊天 turn 已取消。"
                    error_code = None
            elif (
                expected_input_revision is not None
                and current["input_revision"] != expected_input_revision
            ):
                return current
            finished_at = now if status in {"completed", "failed", "cancelled"} else None
            if finished_at is not None:
                connection.execute(
                    """
                    UPDATE chat_messages
                    SET delivery_status = 'rejected'
                    WHERE turn_id = ? AND role = 'user' AND delivery_status = 'queued'
                    """,
                    (turn_id,),
                )
            connection.execute(
                """
                UPDATE chat_turns
                SET status = ?, reply_status = ?, execution_status = ?, blocker = ?,
                    detail = ?, error_code = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    reply_status,
                    execution_status,
                    blocker,
                    detail,
                    error_code,
                    finished_at,
                    now,
                    turn_id,
                ),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, current["session_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return self._chat_turn_from_row(updated)

    def request_chat_turn_cancel(self, turn_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(turn_id)
            current = self._chat_turn_from_row(row)
            if current["status"] in {"completed", "failed", "cancelled"}:
                return current
            immediate = current["status"] in {"accepted", "queued", "awaiting_user"}
            new_status = "cancelled" if immediate else "stopping"
            reply_status = (
                current["reply_status"]
                if not immediate or current["reply_status"] == "completed"
                else "cancelled"
            )
            execution_status = current["execution_status"]
            if immediate and execution_status != "not_requested":
                execution_status = "cancelled"
            connection.execute(
                """
                UPDATE chat_messages
                SET delivery_status = 'rejected'
                WHERE turn_id = ? AND role = 'user' AND delivery_status = 'queued'
                """,
                (turn_id,),
            )
            connection.execute(
                """
                UPDATE chat_turns
                SET status = ?, cancel_requested = 1, reply_status = ?,
                    execution_status = ?, blocker = 'chat_turn_cancelled',
                    detail = '聊天 turn 已取消。',
                    finished_at = CASE WHEN ? THEN ? ELSE finished_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    reply_status,
                    execution_status,
                    int(immediate),
                    now,
                    now,
                    turn_id,
                ),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, current["session_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return self._chat_turn_from_row(updated)

    def finalize_chat_turn_cancel(self, turn_id: str) -> dict[str, Any]:
        current = self.get_chat_turn(turn_id)
        if current is None:
            raise RecordNotFound(turn_id)
        return self.finish_chat_turn(
            turn_id=turn_id,
            status="cancelled",
            reply_status=(
                "completed" if current["reply_status"] == "completed" else "cancelled"
            ),
            execution_status=(
                "not_requested"
                if current["execution_status"] == "not_requested"
                else "cancelled"
            ),
            blocker="chat_turn_cancelled",
            detail="聊天 turn 已取消。",
            error_code=None,
        )

    def recover_interrupted_chat_turns(self) -> int:
        now = utc_now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                UPDATE chat_messages
                SET delivery_status = 'rejected'
                WHERE role = 'user' AND delivery_status = 'queued'
                  AND turn_id IN (
                      SELECT id FROM chat_turns
                      WHERE status IN (
                          'accepted', 'queued', 'thinking', 'planning',
                          'executing', 'stopping'
                      )
                  )
                """
            )
            cancelled = connection.execute(
                """
                UPDATE chat_turns
                SET status = 'cancelled', cancel_requested = 1,
                    reply_status = CASE WHEN reply_status = 'completed' THEN 'completed' ELSE 'cancelled' END,
                    execution_status = CASE
                        WHEN execution_status = 'not_requested' THEN 'not_requested'
                        ELSE 'cancelled' END,
                    blocker = 'coordinator_restarted',
                    detail = '控制台重启时取消了未完成的聊天 turn。',
                    finished_at = ?, updated_at = ?
                WHERE status = 'stopping'
                """,
                (now, now),
            ).rowcount
            failed = connection.execute(
                """
                UPDATE chat_turns
                SET status = 'failed',
                    reply_status = CASE WHEN reply_status = 'completed' THEN 'completed' ELSE 'failed' END,
                    execution_status = CASE
                        WHEN execution_status = 'not_requested' THEN 'not_requested'
                        ELSE 'failed' END,
                    blocker = 'coordinator_restarted', error_code = 'coordinator_restarted',
                    detail = '控制台重启，无法安全恢复此前未完成的聊天 turn。',
                    finished_at = ?, updated_at = ?
                WHERE status IN ('accepted', 'queued', 'thinking', 'planning', 'executing')
                """,
                (now, now),
            ).rowcount
        return cancelled + failed

    def finalize_chat_shutdown(self) -> int:
        now = utc_now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                UPDATE chat_messages
                SET delivery_status = 'rejected'
                WHERE role = 'user' AND delivery_status = 'queued'
                  AND turn_id IN (
                      SELECT id FROM chat_turns
                      WHERE status IN (
                          'accepted', 'queued', 'thinking', 'planning',
                          'executing', 'stopping'
                      )
                  )
                """
            )
            count = connection.execute(
                """
                UPDATE chat_turns
                SET status = 'cancelled', cancel_requested = 1,
                    reply_status = CASE WHEN reply_status = 'completed' THEN 'completed' ELSE 'cancelled' END,
                    execution_status = CASE
                        WHEN execution_status = 'not_requested' THEN 'not_requested'
                        ELSE 'cancelled' END,
                    blocker = 'chat_coordinator_shutdown',
                    detail = '后台对话协调器已关闭。',
                    finished_at = ?, updated_at = ?
                WHERE status IN ('accepted', 'queued', 'thinking', 'planning', 'executing', 'stopping')
                """,
                (now, now),
            ).rowcount
        return count

    def overview_counts(self) -> dict[str, Any]:
        with self._connection() as connection:
            workflow_count = connection.execute(
                "SELECT COUNT(*) FROM workflows"
            ).fetchone()[0]
            target_count = connection.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            # ``active_run_count`` is retained as the public v1 field for
            # compatibility. Its established meaning is every non-terminal
            # run, including saved or paused work; it does not mean that GUI
            # execution is currently happening.
            unfinished_run_count = connection.execute(
                """
                SELECT COUNT(*) FROM runs
                WHERE status IN ('awaiting_approval', 'queued', 'running', 'paused')
                """
            ).fetchone()[0]
            pending_approval_count = connection.execute(
                "SELECT COUNT(*) FROM approvals WHERE status = 'pending'"
            ).fetchone()[0]
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM runs GROUP BY status"
            ).fetchall()
        return {
            "workflow_count": workflow_count,
            "target_count": target_count,
            "active_run_count": unfinished_run_count,
            "pending_approval_count": pending_approval_count,
            "run_status_counts": {row["status"]: row["count"] for row in status_rows},
        }

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        run_id: str | None,
        event_type: str,
        message: str,
        level: str,
        data: dict[str, Any],
        created_at: str,
    ) -> int:
        # Callers deliberately construct a small audit payload. Exact user text is
        # stored only on the run and is never copied into this append-only stream.
        cursor = connection.execute(
            """
            INSERT INTO events (run_id, event_type, message, level, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                message,
                level,
                json.dumps(data, separators=(",", ":"), ensure_ascii=False),
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _chat_session_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "mode": row["mode"],
            "target_id": row["target_id"],
            "auto_execute": bool(row["auto_execute"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _chat_turn_from_row(row: sqlite3.Row) -> dict[str, Any]:
        raw_goal = row["execution_goal_json"]
        execution_goal = None
        if raw_goal:
            try:
                decoded = json.loads(raw_goal)
                execution_goal = decoded if isinstance(decoded, dict) else None
            except json.JSONDecodeError:
                execution_goal = None
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "client_request_id": row["client_request_id"],
            "request_content_sha256": row["request_content_sha256"],
            "input_revision": int(row["input_revision"]),
            "mode": row["mode"],
            "target_id": row["target_id"],
            "auto_execute": bool(row["auto_execute"]),
            "status": row["status"],
            "reply_status": row["reply_status"],
            "execution_status": row["execution_status"],
            "step_count": int(row["step_count"]),
            "blocker": row["blocker"],
            "detail": row["detail"],
            "error_code": row["error_code"],
            "cancel_requested": bool(row["cancel_requested"]),
            "provider": row["provider"],
            "model": row["model"],
            "execution_goal": execution_goal,
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _chat_message_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "turn_id": row["turn_id"],
            "role": row["role"],
            "content": row["content"],
            "client_request_id": row["client_request_id"],
            "content_sha256": row["content_sha256"],
            "input_revision": (
                int(row["input_revision"])
                if row["input_revision"] is not None
                else None
            ),
            "delivery_status": row["delivery_status"],
            "applied_at": row["applied_at"],
            "provider": row["provider"],
            "model": row["model"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _chat_step_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "turn_id": row["turn_id"],
            "step_index": int(row["step_index"]),
            "state": row["state"],
            "action_type": row["action_type"],
            "summary": row["summary"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _workflow_from_row(row: sqlite3.Row) -> Workflow:
        return Workflow(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            target_kind=TargetKind(row["target_kind"]),
            enabled=bool(row["enabled"]),
            integration_status=row["integration_status"],
            requires_approval=bool(row["requires_approval"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _target_from_row(row: sqlite3.Row) -> Target:
        return Target(
            id=row["id"],
            name=row["name"],
            kind=TargetKind(row["kind"]),
            status=row["status"],
            source=row["source"],
            external_id=row["external_id"],
            details=json.loads(row["details_json"] or "{}"),
            discovered_at=row["discovered_at"],
            last_seen_at=row["last_seen_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> Run:
        blocker = row["blocker"]
        if blocker == "executor_not_configured":
            # v1 stored this code for the unimplemented workflow queue, even
            # though the independent chat/ADB executor may already be ready.
            blocker = "workflow_executor_not_connected"
        return Run(
            id=row["id"],
            name=row["name"],
            workflow_id=row["workflow_id"],
            target_id=row["target_id"],
            instruction=row["instruction"],
            exact_text=row["exact_text"],
            requires_approval=bool(row["requires_approval"]),
            status=RunStatus(row["status"]),
            blocker=blocker,
            workflow_name=row["workflow_name"],
            target_name=row["target_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> Approval:
        return Approval(
            id=row["id"],
            run_id=row["run_id"],
            status=ApprovalStatus(row["status"]),
            note=row["note"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            run_id=row["run_id"],
            event_type=row["event_type"],
            message=row["message"],
            level=row["level"],
            data=json.loads(row["data_json"] or "{}"),
            created_at=row["created_at"],
        )
