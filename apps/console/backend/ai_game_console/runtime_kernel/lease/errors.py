from __future__ import annotations


class LeaseConflict(Exception):
    """设备已被另一个 Task 占用"""
    def __init__(
        self,
        device_id: str,
        existing_task_id: str,
        existing_lease_id: str,
        requested_task_id: str,
    ) -> None:
        self.device_id = device_id
        self.existing_task_id = existing_task_id
        self.existing_lease_id = existing_lease_id
        self.requested_task_id = requested_task_id
        super().__init__(
            f"Device {device_id} is held by task {existing_task_id} "
            f"(lease {existing_lease_id}); cannot acquire for task {requested_task_id}"
        )


class LeaseExpired(Exception):
    """Lease 已过期或已超过绝对 Deadline（Week 5）"""
    def __init__(
        self,
        lease_id: str,
        expires_at: str,
        now: str,
        deadline_at: str | None = None,
    ) -> None:
        self.lease_id = lease_id
        self.expires_at = expires_at
        self.now = now
        self.deadline_at = deadline_at
        if deadline_at is not None:
            super().__init__(
                f"Lease {lease_id} exceeded deadline {deadline_at} (now: {now}); "
                f"renewal is no longer allowed"
            )
        else:
            super().__init__(f"Lease {lease_id} expired at {expires_at} (now: {now})")


class LeaseNotFound(Exception):
    """Lease 不存在"""
    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        super().__init__(f"Lease {lease_id} not found")
