from __future__ import annotations

import pytest

from ai_game_console.config import Settings, RuntimeMode
from ai_game_console.runtime_mode import (
    RuntimeModeError,
    RuntimeModeGuard,
    validate_runtime_mode,
)


def test_parse_runtime_mode_from_env() -> None:
    """验证从环境变量解析 runtime_mode"""
    # 默认 legacy
    settings = Settings.from_env({})
    assert settings.runtime_mode == "legacy"
    
    # 显式 legacy
    settings = Settings.from_env({"AI_GAME_RUNTIME_MODE": "legacy"})
    assert settings.runtime_mode == "legacy"
    
    # draining
    settings = Settings.from_env({"AI_GAME_RUNTIME_MODE": "draining"})
    assert settings.runtime_mode == "draining"
    
    # drain 别名
    settings = Settings.from_env({"AI_GAME_RUNTIME_MODE": "drain"})
    assert settings.runtime_mode == "draining"
    
    # kernel_active
    settings = Settings.from_env({"AI_GAME_RUNTIME_MODE": "kernel_active"})
    assert settings.runtime_mode == "kernel_active"
    
    # kernel 别名
    settings = Settings.from_env({"AI_GAME_RUNTIME_MODE": "kernel"})
    assert settings.runtime_mode == "kernel_active"
    
    # new 别名
    settings = Settings.from_env({"AI_GAME_RUNTIME_MODE": "new"})
    assert settings.runtime_mode == "kernel_active"
    
    # 无效值回退到 legacy
    settings = Settings.from_env({"AI_GAME_RUNTIME_MODE": "invalid"})
    assert settings.runtime_mode == "legacy"
    
    # 大小写不敏感
    settings = Settings.from_env({"AI_GAME_RUNTIME_MODE": "DRAINING"})
    assert settings.runtime_mode == "draining"


def test_validate_runtime_mode_accepts_all_valid_modes() -> None:
    """验证 validate_runtime_mode 接受所有合法模式"""
    validate_runtime_mode("legacy")
    validate_runtime_mode("draining")
    validate_runtime_mode("kernel_active")


def test_validate_runtime_mode_rejects_invalid_mode() -> None:
    """验证 validate_runtime_mode 拒绝非法模式"""
    with pytest.raises(RuntimeModeError, match="Unknown runtime mode"):
        validate_runtime_mode("invalid")  # type: ignore


def test_runtime_mode_guard_legacy_mode() -> None:
    """验证 LEGACY 模式的行为"""
    guard = RuntimeModeGuard("legacy")
    
    # Legacy 写入可用
    guard.require_legacy_writable()  # 不抛异常
    assert guard.is_legacy_writable() is True
    
    # Kernel 不可用
    with pytest.raises(RuntimeModeError, match="disabled in LEGACY mode"):
        guard.require_kernel_active()
    assert guard.is_kernel_active() is False
    
    assert guard.is_draining() is False


def test_runtime_mode_guard_draining_mode() -> None:
    """验证 DRAINING 模式的行为"""
    guard = RuntimeModeGuard("draining")
    
    # Legacy 写入不可用
    with pytest.raises(RuntimeModeError, match="disabled in DRAINING mode"):
        guard.require_legacy_writable()
    assert guard.is_legacy_writable() is False
    
    # Kernel 不可用（还在排空）
    with pytest.raises(RuntimeModeError, match="not yet active in DRAINING mode"):
        guard.require_kernel_active()
    assert guard.is_kernel_active() is False
    
    assert guard.is_draining() is True


def test_runtime_mode_guard_kernel_active_mode() -> None:
    """验证 KERNEL_ACTIVE 模式的行为"""
    guard = RuntimeModeGuard("kernel_active")
    
    # Legacy 写入不可用
    with pytest.raises(RuntimeModeError, match="permanently disabled in KERNEL_ACTIVE mode"):
        guard.require_legacy_writable()
    assert guard.is_legacy_writable() is False
    
    # Kernel 可用
    guard.require_kernel_active()  # 不抛异常
    assert guard.is_kernel_active() is True
    
    assert guard.is_draining() is False


def test_runtime_mode_guard_error_messages_are_actionable() -> None:
    """验证错误消息包含可操作的指导"""
    guard_legacy = RuntimeModeGuard("legacy")
    try:
        guard_legacy.require_kernel_active()
    except RuntimeModeError as e:
        assert "AI_GAME_RUNTIME_MODE=kernel_active" in str(e)
    
    guard_draining = RuntimeModeGuard("draining")
    try:
        guard_draining.require_legacy_writable()
    except RuntimeModeError as e:
        assert "waiting for existing tasks" in str(e)
    
    guard_kernel = RuntimeModeGuard("kernel_active")
    try:
        guard_kernel.require_legacy_writable()
    except RuntimeModeError as e:
        assert "permanently disabled" in str(e)
