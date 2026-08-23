"""Phase 7 切流辅助测试：排空门禁查询 + 切流前快照。

- ``active_tasks()``：仅统计未终结（queued/planning/running/stopping）任务。
- ``snapshot_legacy_mobile_tasks``：SQLite 在线备份一致性快照；源库缺失时生成空占位。
- ``drain_gate_satisfied``：活动任务清零才放行进入 KERNEL_ACTIVE。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ai_game_console.legacy_cutover import (
    append_mode_journal,
    drain_gate_satisfied,
    exercise_snapshot_restore,
    sqlite_logical_digest,
    snapshot_legacy_mobile_tasks,
)
from ai_game_console.mobile_agent.store import _SQLiteTaskStore

_NOW = "2026-07-17T00:00:00.000000+00:00"


def _insert_task(db_path: Path, task_id: str, status: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO mobile_tasks (task_id, goal, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, f"goal-{task_id}", status, _NOW, _NOW),
        )
        conn.commit()
    finally:
        conn.close()


def test_active_tasks_counts_only_unfinished_statuses(tmp_path: Path) -> None:
    """active_tasks 仅统计活动状态，排除已终结状态。"""
    db_path = tmp_path / "mobile-tasks.db"
    store = _SQLiteTaskStore(db_path)
    store.initialize()

    for task_id, status in [
        ("t-queued", "queued"),
        ("t-planning", "planning"),
        ("t-running", "running"),
        ("t-stopping", "stopping"),
        ("t-completed", "completed"),
        ("t-failed", "failed"),
        ("t-stopped", "stopped"),
        ("t-uncertain", "uncertain"),
    ]:
        _insert_task(db_path, task_id, status)

    count, ids = store.active_tasks()
    assert count == 4
    assert set(ids) == {"t-queued", "t-planning", "t-running", "t-stopping"}


def test_active_tasks_empty_when_no_rows(tmp_path: Path) -> None:
    """空库的活动任务计数为 0。"""
    db_path = tmp_path / "mobile-tasks.db"
    store = _SQLiteTaskStore(db_path)
    store.initialize()
    assert store.active_tasks() == (0, [])


def test_drain_gate_only_passes_at_zero() -> None:
    """排空门禁仅在活动任务数为 0 时放行。"""
    assert drain_gate_satisfied(0) is True
    assert drain_gate_satisfied(1) is False
    assert drain_gate_satisfied(7) is False


def test_snapshot_produces_consistent_copy(tmp_path: Path) -> None:
    """快照是源库的一致性副本，保留行数据且可独立打开。"""
    source = tmp_path / "mobile-tasks.db"
    store = _SQLiteTaskStore(source)
    store.initialize()
    _insert_task(source, "t-running", "running")
    _insert_task(source, "t-completed", "completed")

    backup_dir = tmp_path / "runtime" / "backups"
    snapshot_path = snapshot_legacy_mobile_tasks(source, backup_dir)

    assert snapshot_path.exists()
    assert snapshot_path.parent == backup_dir
    assert re.fullmatch(r"mobile-tasks-\d{8}T\d{12}\.db", snapshot_path.name)

    with sqlite3.connect(snapshot_path) as conn:
        rows = conn.execute(
            "SELECT task_id, status FROM mobile_tasks ORDER BY task_id"
        ).fetchall()
    assert rows == [("t-completed", "completed"), ("t-running", "running")]


def test_snapshot_missing_source_creates_placeholder(tmp_path: Path) -> None:
    """源库不存在时仍生成一个合法的空占位快照。"""
    missing = tmp_path / "does-not-exist.db"
    assert not missing.exists()

    backup_dir = tmp_path / "backups"
    snapshot_path = snapshot_legacy_mobile_tasks(missing, backup_dir)

    assert snapshot_path.exists()
    # 占位快照是可打开的合法 SQLite 文件。
    with sqlite3.connect(snapshot_path) as conn:
        conn.execute("SELECT 1")


def test_snapshot_restore_returns_db_to_prestore_state(tmp_path: Path) -> None:
    """数据回滚：用快照覆盖原库后，恢复为切流前的行数/状态。"""
    source = tmp_path / "mobile-tasks.db"
    store = _SQLiteTaskStore(source)
    store.initialize()
    _insert_task(source, "t-running", "running")
    _insert_task(source, "t-queued", "queued")
    _insert_task(source, "t-completed", "completed")

    snapshot_path = snapshot_legacy_mobile_tasks(source, tmp_path / "backups")

    # 模拟切流后原库发生「坏写」/ 数据漂移（显式关闭连接，避免 Windows 文件锁）。
    conn = sqlite3.connect(source)
    try:
        conn.execute("DELETE FROM mobile_tasks WHERE task_id = 't-completed'")
        conn.execute("UPDATE mobile_tasks SET status = 'failed' WHERE task_id = 't-running'")
        conn.commit()
    finally:
        conn.close()

    # 回滚：用快照覆盖原库（文件级恢复）。
    snapshot_path.replace(source)

    with sqlite3.connect(source) as conn:
        rows = conn.execute(
            "SELECT task_id, status FROM mobile_tasks ORDER BY task_id"
        ).fetchall()
    assert rows == [
        ("t-completed", "completed"),
        ("t-queued", "queued"),
        ("t-running", "running"),
    ]


def test_controlled_restore_exercise_performs_real_sqlite_restore(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mobile-tasks.db"
    store = _SQLiteTaskStore(source)
    store.initialize()
    _insert_task(source, "t-completed", "completed")
    snapshot = snapshot_legacy_mobile_tasks(source, tmp_path / "backups")

    result = exercise_snapshot_restore(snapshot, tmp_path / "controlled")

    restored = Path(result["restored_copy"])
    assert restored.exists()
    assert restored != source
    assert result["integrity"] == "ok"
    assert result["logical_sha256"] == sqlite_logical_digest(snapshot)
    with sqlite3.connect(restored) as connection:
        assert connection.execute(
            "SELECT task_id, status FROM mobile_tasks"
        ).fetchall() == [("t-completed", "completed")]


def test_mode_journal_is_append_only(tmp_path: Path) -> None:
    journal = tmp_path / "logs" / "runtime-mode.jsonl"
    append_mode_journal(
        journal,
        mode="draining",
        event="runtime_started",
        details={"active": 0},
    )
    first_size = journal.stat().st_size
    append_mode_journal(
        journal,
        mode="kernel_active",
        event="runtime_started",
        details={"binding": "runtime_kernel"},
    )

    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert journal.stat().st_size > first_size
    assert '"mode": "draining"' in lines[0]
    assert '"mode": "kernel_active"' in lines[1]
