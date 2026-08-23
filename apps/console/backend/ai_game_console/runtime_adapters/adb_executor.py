from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..execution import GuiAction, GuiExecutor
from ..runtime_kernel.action import ExecutionError
from ..runtime_kernel.executor import ActionExecutionResult, ActionExecutorPort


def _serial_from_device_id(device_id: str) -> str:
    """从设备 id 中提取真实 ADB 序列号。

    网关规范形式为 ``adb:<serial>``；与 AndroidObservationProvider 一致，
    同时接受规范形式与裸序列号（adb 客户端无法识别带前缀的序列号）。
    """
    prefix = "adb:"
    if device_id.startswith(prefix):
        return device_id[len(prefix) :]
    return device_id


class AdbActionExecutor:
    """基于 ADB 的 Action 执行器
    
    实现 ActionExecutorPort 接口，调用真实 ADB 命令。
    每个方法对应一个 Android 动作类型。
    device_id 接受网关规范形式 ``adb:<serial>`` 或裸序列号。
    """
    
    DEFAULT_TIMEOUT_SECONDS = 5.0
    
    def __init__(
        self,
        adb_path: str | Path,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.adb_path = Path(adb_path)
        self.timeout_seconds = timeout_seconds
    
    def execute_tap(
        self,
        device_id: str,
        x: int,
        y: int,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备坐标 (x, y) 处执行点击"""
        from datetime import datetime, timezone
        
        if not (0 <= x <= 10000 and 0 <= y <= 10000):
            raise ValueError(f"invalid tap coordinates: ({x}, {y})")
        
        started_at = datetime.now(timezone.utc).isoformat()
        
        command = (
            str(self.adb_path.resolve()),
            "-s",
            _serial_from_device_id(device_id),
            "shell",
            "input",
            "tap",
            str(x),
            str(y),
        )
        
        result = self._run_command(command, timeout_ms / 1000.0, started_at)
        return result

    def execute_long_press(
        self,
        device_id: str,
        x: int,
        y: int,
        duration_ms: int = 600,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """Execute one long press through Android's bounded input gesture."""

        if not (0 <= x <= 10000 and 0 <= y <= 10000):
            raise ValueError(f"invalid long press coordinates: ({x}, {y})")
        if not (100 <= duration_ms <= 5000):
            raise ValueError(f"invalid long press duration: {duration_ms}")
        started_at = datetime.now(timezone.utc).isoformat()
        command = (
            str(self.adb_path.resolve()),
            "-s",
            _serial_from_device_id(device_id),
            "shell",
            "input",
            "swipe",
            str(x),
            str(y),
            str(x),
            str(y),
            str(duration_ms),
        )
        return self._run_command(command, timeout_ms / 1000.0, started_at)
    
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
        from datetime import datetime, timezone
        
        if not (0 <= start_x <= 10000 and 0 <= start_y <= 10000):
            raise ValueError(f"invalid swipe start: ({start_x}, {start_y})")
        if not (0 <= end_x <= 10000 and 0 <= end_y <= 10000):
            raise ValueError(f"invalid swipe end: ({end_x}, {end_y})")
        if not (0 < duration_ms <= 10000):
            raise ValueError(f"invalid swipe duration: {duration_ms}ms")
        
        started_at = datetime.now(timezone.utc).isoformat()
        
        command = (
            str(self.adb_path.resolve()),
            "-s",
            _serial_from_device_id(device_id),
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        )
        
        result = self._run_command(command, timeout_ms / 1000.0, started_at)
        return result
    
    def execute_input_text(
        self,
        device_id: str,
        text: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """在设备上输入文本（仅限 ASCII）"""
        from datetime import datetime, timezone
        
        if not text or len(text) > 1000:
            raise ValueError(f"invalid text length: {len(text)}")
        
        # 简化：只支持基本 ASCII，空格替换为 %s
        sanitized = text.replace(" ", "%s")
        if not all(32 <= ord(c) <= 126 or c == '%' for c in sanitized):
            raise ValueError("text contains non-ASCII characters")
        
        started_at = datetime.now(timezone.utc).isoformat()
        
        command = (
            str(self.adb_path.resolve()),
            "-s",
            _serial_from_device_id(device_id),
            "shell",
            "input",
            "text",
            sanitized,
        )
        
        result = self._run_command(command, timeout_ms / 1000.0, started_at)
        return result
    
    def execute_back(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """执行返回键"""
        from datetime import datetime, timezone
        
        started_at = datetime.now(timezone.utc).isoformat()
        
        command = (
            str(self.adb_path.resolve()),
            "-s",
            _serial_from_device_id(device_id),
            "shell",
            "input",
            "keyevent",
            "KEYCODE_BACK",
        )
        
        result = self._run_command(command, timeout_ms / 1000.0, started_at)
        return result
    
    def execute_home(
        self,
        device_id: str,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        """执行主页键"""
        from datetime import datetime, timezone
        
        started_at = datetime.now(timezone.utc).isoformat()
        
        command = (
            str(self.adb_path.resolve()),
            "-s",
            _serial_from_device_id(device_id),
            "shell",
            "input",
            "keyevent",
            "KEYCODE_HOME",
        )
        
        result = self._run_command(command, timeout_ms / 1000.0, started_at)
        return result
    
    def _run_command(
        self,
        command: tuple[str, ...],
        timeout_seconds: float,
        started_at: str,
    ) -> ActionExecutionResult:
        """运行 ADB 命令并返回结果"""
        from datetime import datetime, timezone
        
        try:
            completed = subprocess.run(
                command,
                timeout=timeout_seconds,
                capture_output=True,
                check=False,
            )
            finished_at = datetime.now(timezone.utc).isoformat()
            
            if completed.returncode == 0:
                return ActionExecutionResult(
                    accepted=True,
                    adapter_code=completed.returncode,
                    error=None,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            else:
                stderr = completed.stderr.decode("utf-8", errors="replace").strip()
                return ActionExecutionResult(
                    accepted=False,
                    adapter_code=completed.returncode,
                    error=ExecutionError(
                        code="adb_command_failed",
                        message=stderr or f"ADB command returned {completed.returncode}",
                        retryable=True,
                    ),
                    started_at=started_at,
                    finished_at=finished_at,
                )
        
        except subprocess.TimeoutExpired:
            finished_at = datetime.now(timezone.utc).isoformat()
            return ActionExecutionResult(
                accepted=False,
                adapter_code=-1,
                error=ExecutionError(
                    code="adb_timeout",
                    message=f"ADB command timeout after {timeout_seconds}s",
                    retryable=True,
                ),
                started_at=started_at,
                finished_at=finished_at,
            )

        except Exception as error:
            finished_at = datetime.now(timezone.utc).isoformat()
            return ActionExecutionResult(
                accepted=False,
                adapter_code=-1,
                error=ExecutionError(
                    code="adb_execution_error",
                    message=str(error),
                    retryable=False,
                ),
                started_at=started_at,
                finished_at=finished_at,
            )


class GuiExecutorActionAdapter:
    """Expose the validated GUI executor through RuntimeKernel's action port.

    This keeps Kernel's durable action/lease spine while reusing the normal
    launcher executor's target binding and Unicode-safe text transport.  The
    adapter converts every transport exception into a durable rejected or
    uncertain result; callers can therefore fence ambiguous effects without
    replaying them.
    """

    def __init__(self, executor_for_serial: Callable[[str], GuiExecutor]) -> None:
        self._executor_for_serial = executor_for_serial

    def execute_tap(
        self, device_id: str, x: int, y: int, timeout_ms: int = 5000
    ) -> ActionExecutionResult:
        del timeout_ms
        return self._execute(
            device_id, GuiAction(target_id=device_id, action="tap", x=x, y=y)
        )

    def execute_long_press(
        self,
        device_id: str,
        x: int,
        y: int,
        duration_ms: int = 600,
        timeout_ms: int = 5000,
    ) -> ActionExecutionResult:
        del timeout_ms
        return self._execute(
            device_id,
            GuiAction(
                target_id=device_id,
                action="long_press",
                x=x,
                y=y,
                duration_ms=duration_ms,
            ),
        )

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
        del timeout_ms
        return self._execute(
            device_id,
            GuiAction(
                target_id=device_id,
                action="swipe",
                x=start_x,
                y=start_y,
                end_x=end_x,
                end_y=end_y,
                duration_ms=duration_ms,
            ),
        )

    def execute_input_text(
        self, device_id: str, text: str, timeout_ms: int = 5000
    ) -> ActionExecutionResult:
        del timeout_ms
        return self._execute(
            device_id, GuiAction(target_id=device_id, action="text", text=text)
        )

    def execute_back(
        self, device_id: str, timeout_ms: int = 5000
    ) -> ActionExecutionResult:
        del timeout_ms
        return self._execute(
            device_id,
            GuiAction(
                target_id=device_id,
                action="keyevent",
                keycode="KEYCODE_BACK",
            ),
        )

    def execute_home(
        self, device_id: str, timeout_ms: int = 5000
    ) -> ActionExecutionResult:
        del timeout_ms
        return self._execute(
            device_id,
            GuiAction(
                target_id=device_id,
                action="keyevent",
                keycode="KEYCODE_HOME",
            ),
        )

    def _execute(self, device_id: str, action: GuiAction) -> ActionExecutionResult:
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            executor = self._executor_for_serial(_serial_from_device_id(device_id))
            transport = executor.execute(action)
            finished_at = datetime.now(timezone.utc).isoformat()
            if transport.accepted:
                return ActionExecutionResult(
                    accepted=True,
                    adapter_code=0,
                    error=None,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            return self._rejected(
                "executor_action_rejected", started_at, finished_at, retryable=False
            )
        except (ValueError, RuntimeError, OSError) as error:
            finished_at = datetime.now(timezone.utc).isoformat()
            code = str(error).strip() or "executor_action_unavailable"
            uncertain = "uncertain" in code.casefold() or "timeout" in code.casefold()
            return self._rejected(
                code[:200], started_at, finished_at, retryable=uncertain
            )

    @staticmethod
    def _rejected(
        code: str, started_at: str, finished_at: str, *, retryable: bool
    ) -> ActionExecutionResult:
        return ActionExecutionResult(
            accepted=False,
            adapter_code=-1,
            error=ExecutionError(
                code=code,
                message="validated GUI executor did not confirm the action",
                retryable=retryable,
            ),
            started_at=started_at,
            finished_at=finished_at,
        )
