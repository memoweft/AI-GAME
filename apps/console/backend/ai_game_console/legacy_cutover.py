"""Phase 7 Legacy → Kernel 切流的运维辅助。

Legacy Mobile Task 系统与 Kernel 完全独立（各自独立 SQLite 文件、无共享表、无适配
器），因此数据迁移是非破坏性的：Legacy 库原样保留为只读归档，切流前额外生成一份
SQLite 在线备份快照作为回滚锚点。

本模块提供两个纯辅助：
- :func:`snapshot_legacy_mobile_tasks`：用 SQLite 在线备份 API 生成一致性快照。
- :func:`drain_gate_satisfied`：排空门禁判定（活动任务清零才允许进入 KERNEL_ACTIVE）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

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
