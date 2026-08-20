"""Runtime mode validation and transition logic for Legacy → Kernel cutover.

This module implements the three-phase device ownership transition:
1. LEGACY_ACTIVE: old runtime runs, new kernel inactive
2. DRAINING: reject new legacy tasks, wait for completion
3. KERNEL_ACTIVE: new kernel owns devices, legacy APIs disabled
"""

from __future__ import annotations

import logging
from typing import Literal

from .config import RuntimeMode

logger = logging.getLogger(__name__)


RuntimeModeError = type("RuntimeModeError", (RuntimeError,), {})


def validate_runtime_mode(mode: RuntimeMode) -> None:
    """验证 runtime_mode 配置的一致性
    
    在应用启动时调用，确保模式配置合法。
    
    Raises:
        RuntimeModeError: 模式配置不一致
    """
    if mode not in ("legacy", "draining", "kernel_active"):
        raise RuntimeModeError(f"Unknown runtime mode: {mode}")
    
    if mode == "legacy":
        logger.info("Runtime mode: LEGACY (old runtime active, kernel inactive)")
    elif mode == "draining":
        logger.warning(
            "Runtime mode: DRAINING (rejecting new legacy tasks, waiting for drain)"
        )
    elif mode == "kernel_active":
        logger.info("Runtime mode: KERNEL_ACTIVE (kernel owns devices, legacy disabled)")


class RuntimeModeGuard:
    """Runtime mode guard for request handlers
    
    使用场景：
    - Legacy Task 创建 API：DRAINING/KERNEL_ACTIVE 时拒绝（require_legacy_writable）
    - Legacy Task 输入/停止：KERNEL_ACTIVE 时拒绝（require_legacy_runtime_available）
    - Kernel Action 执行：LEGACY 时拒绝（require_kernel_active）
    """
    
    def __init__(self, mode: RuntimeMode) -> None:
        self.mode = mode
    
    def require_legacy_writable(self) -> None:
        """要求 Legacy 写入可用（仅 LEGACY 模式）"""
        if self.mode == "draining":
            raise RuntimeModeError(
                "Legacy task creation is disabled in DRAINING mode; "
                "waiting for existing tasks to complete"
            )
        if self.mode == "kernel_active":
            raise RuntimeModeError(
                "Legacy task creation is permanently disabled in KERNEL_ACTIVE mode"
            )

    def require_legacy_runtime_available(self) -> None:
        """要求 Legacy 运行时仍可用（LEGACY/DRAINING 可用，KERNEL_ACTIVE 禁用）

        用于存量任务的输入/停止等控制端点：DRAINING 期存量任务仍需可被
        输入或停止以自然排空，仅在 KERNEL_ACTIVE 期完全禁用 Legacy 写路径。
        """
        if self.mode == "kernel_active":
            raise RuntimeModeError(
                "Legacy task control (inputs/stop) is permanently disabled "
                "in KERNEL_ACTIVE mode"
            )
    
    def require_kernel_active(self) -> None:
        """要求 Kernel 激活（仅 KERNEL_ACTIVE 模式）"""
        if self.mode == "legacy":
            raise RuntimeModeError(
                "Kernel action execution is disabled in LEGACY mode; "
                "set AI_GAME_RUNTIME_MODE=kernel_active to enable"
            )
        if self.mode == "draining":
            raise RuntimeModeError(
                "Kernel action execution is not yet active in DRAINING mode; "
                "wait for legacy drain to complete"
            )
    
    def is_legacy_writable(self) -> bool:
        """Legacy Task 创建是否可用"""
        return self.mode == "legacy"

    def is_legacy_runtime_available(self) -> bool:
        """Legacy 运行时（输入/停止等控制）是否可用"""
        return self.mode != "kernel_active"
    
    def is_kernel_active(self) -> bool:
        """Kernel Action 执行是否可用"""
        return self.mode == "kernel_active"
    
    def is_draining(self) -> bool:
        """是否处于排空模式"""
        return self.mode == "draining"
