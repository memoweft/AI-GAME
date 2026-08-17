from __future__ import annotations

import subprocess
from pathlib import Path

from ...runtime_kernel.action import ExecutionError
from ...runtime_kernel.executor import ActionExecutionResult, ActionExecutorPort


class AdbActionExecutor:
    """基于 ADB 的 Action 执行器
    
    实现 ActionExecutorPort 接口，调用真实 ADB 命令。
    每个方法对应一个 Android 动作类型。
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
            device_id,
            "shell",
            "input",
            "tap",
            str(x),
            str(y),
        )
        
        result = self._run_command(command, timeout_ms / 1000.0, started_at)
        return result
    
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
            device_id,
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
            device_id,
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
            device_id,
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
            device_id,
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
        
        except Exception as e:
            finished_at = datetime.now(timezone.utc).isoformat()
            return ActionExecutionResult(
                accepted=False,
                adapter_code=-1,
                error=ExecutionError(
                    code="adb_execution_error",
                    message=str(e),
                    retryable=False,
                ),
                started_at=started_at,
                finished_at=finished_at,
            )
