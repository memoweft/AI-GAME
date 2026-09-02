from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .action import ExecutionError


@dataclass(frozen=True, slots=True)
class ActionExecutionResult:
    """Action 执行结果（ADB transport 层）"""
    accepted: bool  # transport 是否接受命令
    adapter_code: int  # ADB 返回码（0 = 成功）
    error: ExecutionError | None  # 错误详情
    started_at: str
    finished_at: str
    # These opaque references are set only by the R4 DeviceBody bridge.  They
    # deliberately carry no receipt verdict or text payload.
    body_command_id: str | None = None
    body_receipt_id: str | None = None

    def __post_init__(self) -> None:
        if self.accepted and self.error is not None:
            raise ValueError("accepted execution cannot have error")
        if not self.accepted and self.error is None:
            raise ValueError("rejected execution must have error")
        if (self.body_command_id is None) != (self.body_receipt_id is None):
            raise ValueError("body command and receipt references must be paired")
        _utc_timestamp(self.started_at, "result.started_at")
        _utc_timestamp(self.finished_at, "result.finished_at")


class ActionExecutorPort(Protocol):
    """设备 Action 执行端口（ADB 适配器）
    
    每个方法对应一个 Android 动作类型。
    Executor 负责：
    1. 调用 ADB 命令
    2. 处理超时和错误
    3. 返回 transport 结果（不负责验证物理效果）
    """

    def execute_tap(
        self,
        device_id: str,
        x: int,
        y: int,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备坐标 (x, y) 处执行点击"""
        ...

    def execute_long_press(
        self,
        device_id: str,
        x: int,
        y: int,
        duration_ms: int = 600,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备坐标 (x, y) 处执行长按"""
        ...

    def execute_swipe(
        self,
        device_id: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备上执行滑动"""
        ...

    def execute_input_text(
        self,
        device_id: str,
        text: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备上输入文本"""
        ...

    def execute_back(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """执行返回键"""
        ...

    def execute_home(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """执行主页键"""
        ...

    def execute_open_app(
        self,
        device_id: str,
        package: str,
        component: str | None = None,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """Launch an application; transport success is not foreground verification."""
        ...

    def execute_recents(
        self, device_id: str, timeout_ms: int = 5000
    ) -> ActionExecutionResult:
        """Open Android recents/app switcher."""
        ...

    def execute_input_text_unicode(
        self,
        device_id: str,
        text: str,
        target_hint: str | None = None,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """Send Unicode text; a separate read-back observation verifies it."""
        ...

    def execute_capture_snapshot(
        self, device_id: str, timeout_ms: int = 5000
    ) -> ActionExecutionResult:
        """Request a DeviceBody snapshot capture, not a success verdict."""
        ...


def _utc_timestamp(value: str, name: str) -> None:
    from datetime import datetime, timezone
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
