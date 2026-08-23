from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PreflightResult:
    state: str
    facts: tuple[dict[str, Any], ...]
    selected_target_id: str | None = None
    selected_serial: str | None = None
    waiting_reason: dict[str, Any] | None = None
    target_options: tuple[dict[str, Any], ...] = ()

    def projection(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "facts": list(self.facts),
            "selected_target_id": self.selected_target_id,
            "target_options": list(self.target_options),
        }


class GoalPreflight:
    """Read-only U2 environment assessment over current runtime facts."""

    def __init__(
        self,
        *,
        capability_snapshot: Callable[[], dict[str, Any]],
        discover_targets: Callable[[], Any],
        lease_is_held: Callable[[str], bool],
        runtime_available: Callable[[], bool],
        preferred_serial: str | None = None,
        runtime_kind: str = "mobile_task_compat",
    ) -> None:
        self.capability_snapshot = capability_snapshot
        self.discover_targets = discover_targets
        self.lease_is_held = lease_is_held
        self.runtime_available = runtime_available
        self.preferred_serial = preferred_serial.strip() if preferred_serial else None
        self.runtime_kind = runtime_kind

    def assess(self) -> PreflightResult:
        snapshot = self.capability_snapshot()
        capabilities = {
            str(_value(item, "id")): item
            for item in snapshot.get("capabilities", [])
            if _value(item, "id") is not None
        }
        model = capabilities.get("model") or capabilities.get("model_runtime")
        facts: list[dict[str, Any]] = []
        model_ready = bool(model and _value(model, "status") == "ready")
        facts.append(
            _fact(
                "local.visual_grounding",
                "READY" if model_ready else "WAITING_CONFIGURATION",
                str(_value(model, "detail") or "本地视觉模型尚未就绪。"),
            )
        )
        runtime_ready = self.runtime_available()
        kernel_runtime = self.runtime_kind in {
            "runtime_kernel",
            "runtime_kernel_canary",
        }
        facts.append(
            _fact(
                self.runtime_kind,
                "READY" if runtime_ready else "WAITING_CONFIGURATION",
                (
                    "RuntimeKernel 执行运行时已就绪。"
                    if runtime_ready and kernel_runtime
                    else "RuntimeKernel 执行运行时尚未就绪。"
                    if kernel_runtime
                    else "兼容执行运行时已就绪。"
                    if runtime_ready
                    else "兼容执行运行时尚未就绪。"
                ),
            )
        )

        discovery = self.discover_targets()
        targets = tuple(getattr(discovery, "targets", ()))
        discovery_fact = getattr(discovery, "discovery", discovery)
        discovery_status = str(getattr(discovery_fact, "status", "unknown"))
        facts.append(
            _fact(
                "android.discovery",
                "READY" if discovery_status == "ready" else "WAITING_CONFIGURATION",
                str(getattr(discovery_fact, "message", "Android 目标发现状态未知。")),
            )
        )

        ready: list[tuple[Any, str]] = []
        unauthorized = False
        for target in targets:
            status = str(getattr(target, "status", "unknown"))
            kind = str(getattr(target, "kind", "")).lower()
            if kind not in {"android", "emulator"}:
                continue
            serial = str(getattr(target, "external_id", "") or "").strip()
            if status == "unauthorized":
                unauthorized = True
            if status != "ready" or not serial:
                continue
            if self.lease_is_held(serial):
                facts.append(
                    _fact(
                        f"target:{getattr(target, 'id', serial)}",
                        "BUSY",
                        "该 Android 目标正由另一个执行会话占用。",
                    )
                )
                continue
            ready.append((target, serial))
            facts.append(
                _fact(
                    f"target:{getattr(target, 'id', serial)}",
                    "READY",
                    "Android 目标已由当前只读发现确认可用且空闲。",
                )
            )

        if not model_ready or not runtime_ready:
            return PreflightResult(
                "WAITING_CONFIGURATION",
                tuple(facts),
                waiting_reason={
                    "code": "execution_capability_not_ready",
                    "message": "本地模型或所选执行运行时尚未就绪。",
                },
            )
        if not ready:
            if unauthorized:
                reason = {
                    "code": "USB_DEBUG_AUTHORIZATION",
                    "message": "请在手机上允许这台电脑进行 USB 调试，然后重试。",
                }
                state = "WAITING_EXTERNAL"
            else:
                reason = {
                    "code": "android_target_not_ready",
                    "message": "没有发现可用且空闲的 Android 目标。请启动或连接设备后重试。",
                }
                state = "WAITING_CONFIGURATION"
            return PreflightResult(state, tuple(facts), waiting_reason=reason)
        preferred = next(
            ((target, serial) for target, serial in ready if serial == self.preferred_serial),
            None,
        )
        if preferred is not None:
            target, serial = preferred
            return PreflightResult(
                "READY",
                tuple(facts),
                selected_target_id=str(getattr(target, "id")),
                selected_serial=serial,
            )
        if len(ready) > 1:
            options = tuple(
                {
                    "target_id": str(getattr(target, "id")),
                    "name": str(getattr(target, "name", serial)),
                    "connection": serial,
                }
                for target, serial in ready
            )
            return PreflightResult(
                "WAITING_EXTERNAL",
                tuple(facts),
                waiting_reason={
                    "code": "TARGET_SELECTION_REQUIRED",
                    "message": "发现多个可用设备，请选择这次目标使用哪一台。",
                },
                target_options=options,
            )
        target, serial = ready[0]
        return PreflightResult(
            "READY",
            tuple(facts),
            selected_target_id=str(getattr(target, "id")),
            selected_serial=serial,
        )


def _fact(capability: str, state: str, detail: str) -> dict[str, Any]:
    return {"capability": capability, "state": state, "detail": detail}


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)
