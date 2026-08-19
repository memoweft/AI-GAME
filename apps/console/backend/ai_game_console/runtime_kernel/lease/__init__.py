from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class DeviceExecutionLease:
    """设备执行独占权证明
    
    一个设备同一时刻最多存在一个自动执行 Lease。
    Lease 绑定唯一 Runtime Kernel Task 和设备会话。
    """
    id: str
    device_id: str
    task_id: str
    holder_process_id: str
    acquired_at: str
    expires_at: str
    deadline_at: str  # 绝对截止时刻（Week 5）：续期永远无法推迟
    last_heartbeat_at: str
    action_id: str | None  # 当前执行的 Action（用于恢复检测）

    def __post_init__(self) -> None:
        _required(self.id, "lease.id")
        _required(self.device_id, "lease.device_id")
        _required(self.task_id, "lease.task_id")
        _required(self.holder_process_id, "lease.holder_process_id")
        _utc_timestamp(self.acquired_at, "lease.acquired_at")
        _utc_timestamp(self.expires_at, "lease.expires_at")
        _utc_timestamp(self.deadline_at, "lease.deadline_at")
        _utc_timestamp(self.last_heartbeat_at, "lease.last_heartbeat_at")
        if self.action_id is not None:
            _required(self.action_id, "lease.action_id")
        if self.deadline_at < self.expires_at:
            raise ValueError(
                "lease.deadline_at must not be earlier than lease.expires_at"
            )

    def is_expired(self, now: str) -> bool:
        """检查 Lease 是否已过期"""
        return self.expires_at <= now

    def is_deadline_exceeded(self, now: str) -> bool:
        """检查 Lease 是否已超过绝对 Deadline（Week 5）"""
        return self.deadline_at <= now

    def renew(self, new_expires_at: str, new_heartbeat_at: str) -> DeviceExecutionLease:
        """续期 Lease，返回新实例（deadline_at 不变，续期不可推迟绝对截止）"""
        return DeviceExecutionLease(
            id=self.id,
            device_id=self.device_id,
            task_id=self.task_id,
            holder_process_id=self.holder_process_id,
            acquired_at=self.acquired_at,
            expires_at=new_expires_at,
            deadline_at=self.deadline_at,
            last_heartbeat_at=new_heartbeat_at,
            action_id=self.action_id,
        )

    def with_action(self, action_id: str | None) -> DeviceExecutionLease:
        """更新关联的 Action，返回新实例"""
        return DeviceExecutionLease(
            id=self.id,
            device_id=self.device_id,
            task_id=self.task_id,
            holder_process_id=self.holder_process_id,
            acquired_at=self.acquired_at,
            expires_at=self.expires_at,
            deadline_at=self.deadline_at,
            last_heartbeat_at=self.last_heartbeat_at,
            action_id=action_id,
        )


@dataclass(frozen=True, slots=True)
class LeaseStats:
    """当前 Lease 统计（Week 6 管理查询；release 即删除，无历史记录）"""

    total: int
    active: int
    expired: int
    deadline_exceeded: int
    avg_current_hold_seconds: float | None


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def _utc_timestamp(value: str, name: str) -> datetime:
    _required(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
    return parsed
