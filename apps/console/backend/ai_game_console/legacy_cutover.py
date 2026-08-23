"""Phase 7 Legacy → Kernel 切流的运维辅助。

Legacy Mobile Task 系统与 Kernel 完全独立（各自独立 SQLite 文件、无共享表、无适配
器），因此数据迁移是非破坏性的：Legacy 库原样保留为只读归档，切流前额外生成一份
SQLite 在线备份快照作为回滚锚点。

本模块提供两个纯辅助：
- :func:`snapshot_legacy_mobile_tasks`：用 SQLite 在线备份 API 生成一致性快照。
- :func:`drain_gate_satisfied`：排空门禁判定（活动任务清零才允许进入 KERNEL_ACTIVE）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 切流前必须排空的活动状态：排队中、规划中、运行中、停止中。
ACTIVE_LEGACY_STATUSES = ("queued", "planning", "running", "stopping")


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")


def snapshot_legacy_mobile_tasks(
    source_path: Path | str,
    backup_dir: Path | str,
) -> Path:
    """生成 Legacy Mobile Task 库的一致性快照。

    使用 SQLite 在线备份 API（``sqlite3.Connection.backup``）而非文件复制，确保即使
    库正在被写入也能得到一份逻辑一致、可独立打开的快照。源库不存在（从未创建过
    Legacy 任务）时，仍生成一个空快照占位，保证回滚 runbook 总有产物可引用。

    返回快照文件的绝对路径（形如 ``<backup_dir>/mobile-tasks-<ts>.db``）。
    """
    source = Path(source_path)
    backup_dir_path = Path(backup_dir)
    backup_dir_path.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir_path / f"mobile-tasks-{_timestamp()}.db"

    if not source.exists():
        # 空库：仅初始化一个合法的 SQLite 文件作为占位快照。
        placeholder = sqlite3.connect(backup_path)
        try:
            placeholder.execute(
                "CREATE TABLE IF NOT EXISTS mobile_task_snapshot_placeholder (singleton INTEGER PRIMARY KEY)"
            )
            placeholder.execute("INSERT OR IGNORE INTO mobile_task_snapshot_placeholder (singleton) VALUES (1)")
            placeholder.commit()
        finally:
            placeholder.close()
        return backup_path

    source_conn = sqlite3.connect(source)
    backup_conn = sqlite3.connect(backup_path)
    try:
        with backup_conn:
            source_conn.backup(backup_conn)
    finally:
        source_conn.close()
        backup_conn.close()
    return backup_path


def drain_gate_satisfied(active_count: int) -> bool:
    """排空门禁：当且仅当活动任务数为 0 时返回 True。

    这是从 DRAINING 推进到 KERNEL_ACTIVE 的前置条件：所有存量任务必须已自然
    完成或被显式停止（即不再处于 :data:`ACTIVE_LEGACY_STATUSES` 任一状态）。
    """
    return active_count == 0


def sqlite_logical_digest(database_path: Path | str) -> str:
    """Return a deterministic digest of one readable SQLite database.

    The digest uses SQLite's logical dump instead of file bytes, because a real
    backup/restore can legitimately change page layout while preserving every
    schema object and row.
    """
    path = Path(database_path).resolve()
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {path}")
        dump = "\n".join(connection.iterdump()).encode("utf-8")
    return hashlib.sha256(dump).hexdigest()


def restore_sqlite_snapshot(
    snapshot_path: Path | str,
    destination_path: Path | str,
) -> Path:
    """Actually restore a snapshot through SQLite's backup API.

    U7 uses this against a disposable controlled destination.  The function
    refuses an in-place source/destination identity so a verification exercise
    cannot accidentally replace the archived snapshot itself.
    """
    source = Path(snapshot_path).resolve()
    destination = Path(destination_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source == destination:
        raise ValueError("snapshot and restore destination must be different")
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        integrity = source_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError(f"Snapshot integrity check failed: {source}")
        with destination_connection:
            source_connection.backup(destination_connection)
    finally:
        source_connection.close()
        destination_connection.close()

    # Reopen the restored copy independently; a successful backup call alone
    # is not restore evidence.
    restored_digest = sqlite_logical_digest(destination)
    source_digest = sqlite_logical_digest(source)
    if restored_digest != source_digest:
        raise RuntimeError("Restored SQLite copy does not match the snapshot")
    return destination


def exercise_snapshot_restore(
    snapshot_path: Path | str,
    restore_dir: Path | str,
) -> dict[str, Any]:
    """Restore into a disposable copy and return independently checkable facts."""
    source = Path(snapshot_path).resolve()
    destination = (
        Path(restore_dir).resolve()
        / f"mobile-tasks-restored-{_timestamp()}.db"
    )
    restore_sqlite_snapshot(source, destination)
    return {
        "snapshot": str(source),
        "restored_copy": str(destination),
        "logical_sha256": sqlite_logical_digest(destination),
        "integrity": "ok",
        "restored_at": datetime.now(UTC).isoformat(),
    }


def append_mode_journal(
    journal_path: Path | str,
    *,
    mode: str,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one durable, non-truncating cutover mode record."""
    path = Path(journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "mode": mode,
        "event": event,
        "details": details or {},
    }
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        stream.write("\n")
        stream.flush()
