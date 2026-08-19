from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_game_console.device_lease_manager import DeviceLeaseManager
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_adapters.sqlite import store as store_module
from ai_game_console.runtime_kernel import (
    ActionType,
    RuntimeKernel,
    TaskSource,
)
from ai_game_console.runtime_kernel.lease.errors import LeaseExpired

TIMES = tuple(f"2026-08-17T14:{minute:02d}:00+00:00" for minute in range(60))


def _clock() -> Callable[[], str]:
    values = iter(TIMES * 20)
    return lambda: next(values)


def _ids() -> Callable[[], str]:
    import uuid
    return lambda: str(uuid.uuid4())


def _source(suffix: str) -> TaskSource:
    return TaskSource(
        client_id=f"client-{suffix}",
        conversation_id=f"conversation-{suffix}",
        initial_message_id=f"message-{suffix}",
    )


class FakeObservationProvider:
    def capture(self, device_id: str):
        from ai_game_console.runtime_kernel import (
            RawObservation, RawScreenshot, RawUiTree,
            ChannelAvailability, DeviceState, Orientation,
            KeyboardState, ConnectionState, ObservationConsistency,
            ConsistencyStatus,
        )
        return RawObservation(
            device_id=device_id,
            capture_started_at=TIMES[10],
            capture_completed_at=TIMES[12],
            screenshot=RawScreenshot(
                status=ChannelAvailability.AVAILABLE,
                content=b"fake",
                width=1080,
                height=2400,
                captured_at=TIMES[11],
            ),
            ui_tree=RawUiTree(
                status=ChannelAvailability.AVAILABLE,
                content=b"<hierarchy/>",
                captured_at=TIMES[12],
            ),
            device_state=DeviceState(
                status=ChannelAvailability.AVAILABLE,
                foreground_app="com.test",
                screen_size=(1080, 2400),
                orientation=Orientation.PORTRAIT,
                keyboard_state=KeyboardState.HIDDEN,
                connection_state=ConnectionState.CONNECTED,
                captured_at=TIMES[11],
            ),
            consistency=ObservationConsistency(
                status=ConsistencyStatus.CONSISTENT,
                reason=None,
            ),
        )


def _make_task(
    store: SQLiteRuntimeStore,
    clock: Callable[[], str],
    *,
    task_id: str,
    device_id: str,
    suffix: str,
):
    from ai_game_console.runtime_kernel.task import Task
    from ai_game_console.runtime_kernel.event import RuntimeEventDraft, EventActor

    task = Task.create(
        task_id=task_id,
        goal=f"Test {suffix}",
        source=_source(suffix),
        device_id=device_id,
        created_at=clock(),
    )
    event = RuntimeEventDraft(
        id=f"event-{suffix}",
        type="TaskCreated",
        actor=EventActor.RUNTIME,
        payload={},
        correlation_id=task.id,
        created_at=clock(),
    )
    store.create_task(task, event)
    return task


def test_acquire_lease_default_deadline_is_300_seconds(tmp_path: Path) -> None:
    """默认 Deadline = acquired_at + 300 秒，且晚于 expires_at"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    _make_task(
        store, clock, task_id="task-1", device_id="device-1", suffix="default"
    )

    lease = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=str(os.getpid()),
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=TIMES[0],
    )

    assert lease.expires_at == "2026-08-17T14:01:00+00:00"
    assert lease.deadline_at == "2026-08-17T14:05:00+00:00"  # +300s
    assert lease.deadline_at > lease.expires_at


def test_acquire_lease_custom_deadline_seconds(tmp_path: Path) -> None:
    """显式 deadline_seconds 覆盖默认值"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    _make_task(
        store, clock, task_id="task-1", device_id="device-1", suffix="custom"
    )

    lease = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=str(os.getpid()),
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=TIMES[0],
        deadline_seconds=120,
    )

    assert lease.expires_at == "2026-08-17T14:01:00+00:00"
    assert lease.deadline_at == "2026-08-17T14:02:00+00:00"  # +120s


def test_acquire_lease_rejects_deadline_shorter_than_ttl(tmp_path: Path) -> None:
    """deadline_seconds 短于 ttl_seconds 时拒绝（不变式 deadline >= expires）"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    _make_task(
        store, clock, task_id="task-1", device_id="device-1", suffix="short"
    )

    with pytest.raises(ValueError, match="deadline_seconds must not be shorter"):
        store.acquire_lease(
            device_id="device-1",
            task_id="task-1",
            holder_process_id=str(os.getpid()),
            ttl_seconds=60,
            lease_id="lease-1",
            acquired_at=TIMES[0],
            deadline_seconds=30,
        )

    # 失败后事务回滚，设备未被占用
    assert store.get_lease_for_device("device-1") is None


def test_renew_clamps_expiration_to_deadline(tmp_path: Path) -> None:
    """Deadline 前续期：expires_at 被钳制在 deadline_at"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    _make_task(
        store, clock, task_id="task-1", device_id="device-1", suffix="clamp"
    )

    lease = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=str(os.getpid()),
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=TIMES[0],
    )
    assert lease.deadline_at == "2026-08-17T14:05:00+00:00"

    # 心跳 14:01（deadline 前），请求 expires 14:10（超过 deadline）
    renewed = store.renew_lease(
        lease.id, TIMES[10], TIMES[1]
    )

    assert renewed.expires_at == "2026-08-17T14:05:00+00:00"  # 钳制到 deadline
    assert renewed.deadline_at == lease.deadline_at  # deadline 永不改变


def test_renew_after_deadline_raises_lease_expired(tmp_path: Path) -> None:
    """超过 Deadline 后拒绝续期，LeaseExpired 携带 deadline_at"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    _make_task(
        store, clock, task_id="task-1", device_id="device-1", suffix="past"
    )

    lease = store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=str(os.getpid()),
        ttl_seconds=60,
        lease_id="lease-1",
        acquired_at=TIMES[0],
    )

    # 心跳 14:10 已晚于 deadline 14:05
    with pytest.raises(LeaseExpired) as exc_info:
        store.renew_lease(lease.id, TIMES[10], TIMES[10])

    assert exc_info.value.lease_id == lease.id
    assert exc_info.value.deadline_at == "2026-08-17T14:05:00+00:00"
    assert "deadline" in str(exc_info.value)

    # Lease 仍存在于库中（等待 Manager 清理释放）
    assert store.get_lease(lease.id) is not None


def test_manager_releases_deadline_exceeded_lease_even_when_process_alive(
    tmp_path: Path,
) -> None:
    """Deadline 超限后即使进程存活也立即释放（不再无限续期）"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    _make_task(
        store, clock, task_id="task-1", device_id="device-1", suffix="alive"
    )

    # 活进程持有：ttl=1s 过期 14:00:01，默认 deadline 14:05
    store.acquire_lease(
        device_id="device-1",
        task_id="task-1",
        holder_process_id=str(os.getpid()),
        ttl_seconds=1,
        lease_id="lease-1",
        acquired_at=TIMES[0],
    )
    assert store.get_lease_for_device("device-1") is not None

    # 清理时刻 14:06：已过期且已超 Deadline
    manager = DeviceLeaseManager(store, lambda: TIMES[6])
    summary = manager._cleanup_orphaned_leases()

    assert store.get_lease_for_device("device-1") is None
    assert summary["expired_found"] == 1
    assert summary["deadline_exceeded"] == 1
    assert summary["released"] == 1


def test_manager_checkpoint_uses_deadline_exceeded_reason(tmp_path: Path) -> None:
    """Deadline 超限的关联 Action 创建 Checkpoint 且原因为 deadline_exceeded"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()

    kernel = RuntimeKernel(
        store,
        observation_provider=FakeObservationProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
        id_factory=_ids(),
    )
    task = kernel.create_task(
        goal="Test deadline checkpoint",
        source=_source("deadline"),
        device_id="device-deadline",
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="Test",
        completion_criteria=("done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    obs = kernel.capture_observation(task_id=task.id, device_id="device-deadline")
    action = kernel.propose_action(
        task_id=task.id,
        stage_id=stage.id,
        based_on_observation_id=obs.id,
        action_type=ActionType.TAP,
        params={"x": 100, "y": 100},
        expected_outcome="Test",
        proposed_by_call_id="op",
    )

    # 活进程持有 + 关联 Action；ttl=1s，默认 deadline 14:05
    lease = store.acquire_lease(
        device_id="device-deadline",
        task_id=task.id,
        holder_process_id=str(os.getpid()),
        ttl_seconds=1,
        lease_id="lease-deadline",
        acquired_at=TIMES[0],
    )
    store.update_lease_action(lease.id, action.id)

    # 清理时刻 14:06：过期且超 Deadline
    manager = DeviceLeaseManager(store, lambda: TIMES[6])
    manager._cleanup_orphaned_leases()

    checkpoint = kernel.latest_checkpoint(task.id)
    assert checkpoint is not None
    assert checkpoint.unresolved_action_ref == action.id
    assert checkpoint.required_fresh_observation is True
    assert checkpoint.resume_reason == "deadline_exceeded"


def test_list_deadline_exceeded_leases(tmp_path: Path) -> None:
    """list_deadline_exceeded_leases 只返回已超 Deadline 的 Lease"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    for suffix in ("a", "b", "c"):
        _make_task(
            store,
            clock,
            task_id=f"task-{suffix}",
            device_id=f"device-{suffix}",
            suffix=suffix,
        )

    # A: deadline 14:02（已超）
    store.acquire_lease(
        device_id="device-a",
        task_id="task-a",
        holder_process_id="999999",
        ttl_seconds=1,
        lease_id="lease-a",
        acquired_at=TIMES[0],
        deadline_seconds=120,
    )
    # B: 默认 deadline 14:05（已超）
    store.acquire_lease(
        device_id="device-b",
        task_id="task-b",
        holder_process_id="999998",
        ttl_seconds=60,
        lease_id="lease-b",
        acquired_at=TIMES[0],
    )
    # C: deadline 14:09（未超）
    store.acquire_lease(
        device_id="device-c",
        task_id="task-c",
        holder_process_id="999997",
        ttl_seconds=60,
        lease_id="lease-c",
        acquired_at=TIMES[4],
    )

    exceeded = store.list_deadline_exceeded_leases(TIMES[6])
    assert {lease.id for lease in exceeded} == {"lease-a", "lease-b"}


def test_lease_stats_counts(tmp_path: Path) -> None:
    """lease_stats 统计当前行（release 即删除，无历史）"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.initialize()
    clock = _clock()
    for suffix in ("a", "b"):
        _make_task(
            store,
            clock,
            task_id=f"task-{suffix}",
            device_id=f"device-{suffix}",
            suffix=suffix,
        )

    # A: 14:00:01 过期、14:02 超 Deadline
    store.acquire_lease(
        device_id="device-a",
        task_id="task-a",
        holder_process_id="999999",
        ttl_seconds=1,
        lease_id="lease-a",
        acquired_at=TIMES[0],
        deadline_seconds=120,
    )
    # B: 长 TTL，保持活跃
    store.acquire_lease(
        device_id="device-b",
        task_id="task-b",
        holder_process_id="999998",
        ttl_seconds=3600,
        lease_id="lease-b",
        acquired_at=TIMES[0],
        deadline_seconds=3600,
    )

    stats = store.lease_stats(TIMES[6])
    assert stats.total == 2
    assert stats.active == 1
    assert stats.expired == 1
    assert stats.deadline_exceeded == 1
    # 平均持有 = (14:06 - 14:00) × 2 / 2 = 360s
    assert stats.avg_current_hold_seconds == pytest.approx(360.0)

    # 释放后统计随之减少
    store.release_lease("lease-a")
    stats = store.lease_stats(TIMES[6])
    assert stats.total == 1
    assert stats.deadline_exceeded == 0


def _build_v4_database(path: Path) -> None:
    """手工构造 v4（Phase 5 迁移前）数据库，含一行无 deadline_at 的 Lease"""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(store_module._PHASE_2_SCHEMA)
    now = "2026-08-17T14:00:00+00:00"
    connection.execute(
        "INSERT INTO runtime_schema(revision, applied_at) VALUES (1, ?)", (now,)
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()

    migrations = {
        2: store_module._MIGRATION_1_TO_2,
        3: store_module._MIGRATION_2_TO_3,
        4: store_module._MIGRATION_3_TO_4,
    }
    for target_revision, statements in migrations.items():
        connection.execute("BEGIN IMMEDIATE")
        for statement in statements.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT INTO runtime_schema(revision, applied_at) VALUES (?, ?)",
            (target_revision, now),
        )
        connection.execute(f"PRAGMA user_version = {target_revision}")
        connection.commit()

    connection.execute(
        """
        INSERT INTO runtime_device_leases (
            id, device_id, task_id, holder_process_id,
            acquired_at, expires_at, last_heartbeat_at, action_id
        ) VALUES (
            'lease-legacy', 'device-legacy', 'task-legacy', '12345',
            '2026-08-17T14:00:00+00:00', '2026-08-17T14:01:00+00:00',
            '2026-08-17T14:00:00+00:00', NULL
        )
        """
    )
    connection.commit()
    connection.close()


def test_migration_4_to_5_backfills_deadline_at(tmp_path: Path) -> None:
    """v4 库迁移到 v5：schema 升版 + 存量 Lease 回填 deadline_at = acquired_at + 300s"""
    db_path = tmp_path / "runtime.db"
    _build_v4_database(db_path)

    store = SQLiteRuntimeStore(db_path)
    store.initialize()

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        user_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        max_revision = int(
            connection.execute("SELECT MAX(revision) FROM runtime_schema").fetchone()[0]
        )
        assert user_version == 5
        assert max_revision == 5

    lease = store.get_lease("lease-legacy")
    assert lease is not None
    assert lease.deadline_at == "2026-08-17T14:05:00+00:00"  # 14:00 + 300s
