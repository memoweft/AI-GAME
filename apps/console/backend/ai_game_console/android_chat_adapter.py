from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .adb_executor import AdbGuiExecutor
from .android_automation import (
    AndroidAutomationEvent,
    AndroidAutomationGoalSnapshot,
    AndroidAutomationResult,
    GuiOwlAndroidAutomation,
    GuiOwlMultimodalModel,
)
from .chat import (
    AutomationInstructionSnapshot,
    AutomationRunResult,
    AutomationStepUpdate,
)
from .device_lease import (
    DeviceLease,
    DeviceLeaseHandle,
    TargetBusyError,
)
from .domain import TargetKind
from .repository import SQLiteRepository


@dataclass(slots=True)
class RepositoryAndroidAutomationFactory:
    """Bind a persisted Android target to the configured local ADB endpoint."""

    repository: SQLiteRepository
    executor: AdbGuiExecutor
    model: GuiOwlMultimodalModel
    max_steps: int | None = None
    device_lease: DeviceLease | None = None

    def create(
        self,
        *,
        target_id: str,
        goal: Mapping[str, Any],
        on_step: Callable[[AutomationStepUpdate], None],
        is_cancelled: Callable[[], bool],
        instruction_source: Callable[[], AutomationInstructionSnapshot] | None = None,
    ) -> "ChatAndroidAutomationRun":
        target = self.repository.get_target(target_id)
        if target is None:
            raise RuntimeError("chat_target_not_found")
        if target.kind is not TargetKind.ANDROID:
            raise RuntimeError("chat_target_kind_mismatch")
        if target.status != "ready":
            raise RuntimeError("chat_target_not_ready")
        if not target.external_id or target.external_id != self.executor.serial:
            # MuMu's endpoint is dynamic. Never silently redirect a persisted
            # session to a different serial if the runtime configuration moved.
            raise RuntimeError("chat_target_serial_changed")

        instruction = _instruction_from_goal(goal)
        android_goal_source: Callable[[], AndroidAutomationGoalSnapshot] | None = None
        if instruction_source is not None:

            def current_android_goal() -> AndroidAutomationGoalSnapshot:
                snapshot = instruction_source()
                return AndroidAutomationGoalSnapshot(
                    revision=snapshot.revision,
                    goal=_instruction_with_updates(instruction, snapshot.updates),
                )

            android_goal_source = current_android_goal

        loop = GuiOwlAndroidAutomation(
            target_id=target_id,
            goal=instruction,
            device=self.executor,
            model=self.model,
            max_steps=self.max_steps,
            is_cancelled=is_cancelled,
            on_step_event=lambda event: on_step(_timeline_update(event)),
            goal_source=android_goal_source,
        )
        return ChatAndroidAutomationRun(
            loop=loop,
            target_key=self.executor.serial,
            device_lease=self.device_lease,
        )


@dataclass(slots=True)
class ChatAndroidAutomationRun:
    loop: GuiOwlAndroidAutomation
    target_key: str
    device_lease: DeviceLease | None = None

    def run(self) -> AutomationRunResult:
        handle: DeviceLeaseHandle | None = None
        if self.device_lease is not None:
            handle = self.device_lease.acquire(self.target_key)
            if handle is None:
                return _target_busy_result()
        try:
            result = self.loop.run()
        except Exception as exc:
            if isinstance(exc, TargetBusyError):
                return _target_busy_result()
            code = _known_failure_code(exc)
            return AutomationRunResult(
                status="failed",
                detail=_failure_detail(code),
                error_code=code,
            )
        finally:
            if handle is not None:
                handle.release()
        return _map_result(result)


def _instruction_from_goal(goal: Mapping[str, Any]) -> str:
    objective = goal.get("goal")
    if not isinstance(objective, str) or not objective.strip():
        raise RuntimeError("execution_goal_invalid")
    instruction = objective.strip()
    exact_text = goal.get("exact_text")
    if exact_text is not None:
        if not isinstance(exact_text, str) or not exact_text or len(exact_text) > 10_000:
            raise RuntimeError("execution_exact_text_invalid")
        instruction += (
            "\nIf the task requires typing, use this exact text without rewriting it: "
            + exact_text
        )
    return instruction


def _instruction_with_updates(base_instruction: str, updates: tuple[str, ...]) -> str:
    if not updates:
        return base_instruction
    ordered_updates = "\n".join(
        f"{index}. {update}" for index, update in enumerate(updates, start=1)
    )
    return (
        f"{base_instruction}\n\n"
        "User instruction updates, ordered from earliest to latest:\n"
        f"{ordered_updates}\n\n"
        "Priority rule: later updates override conflicting earlier updates and "
        "conflicting base instructions."
    )


def _timeline_update(event: AndroidAutomationEvent) -> AutomationStepUpdate:
    summaries = {
        "observed": "已读取当前设备画面。",
        "proposed": "本地 GUI 模型已提出一个原子动作。",
        "transported": "ADB 已接收该动作；这不代表界面目标已经完成。",
        "observed_after": "动作后已重新获取设备画面。",
        "waiting": "正在等待界面响应。",
        "redirected": "本地 GUI 模型请求人工接管；测试模式未暂停，正在要求它继续选择下一步。",
        "paused": "遇到需要你处理的页面，自动操作已在动作发送前暂停。",
        "terminated": "本地 GUI 模型已在最新画面上结束本轮操作。",
    }
    summary = summaries[event.phase]
    if event.phase == "redirected" and event.action == "invalid_tool_call":
        summary = "本地 GUI 模型输出格式无效，未向 ADB 发送动作；正在自动重新规划。"
    if event.phase == "redirected" and event.action == "instruction_update":
        summary = "收到新的用户指令；旧的动作或结束判断已丢弃，未向 ADB 发送，正在基于最新画面重新规划。"
    return AutomationStepUpdate(
        state=event.phase,
        action_type=event.action,
        summary=summary,
    )


def _map_result(result: AndroidAutomationResult) -> AutomationRunResult:
    if result.status == "cancelled":
        return AutomationRunResult(
            status="cancelled",
            detail="本轮设备操作已停止；停止前已发送的原子动作无法撤回。",
            error_code="automation_cancelled",
        )
    if result.status == "paused":
        blocker = result.blocker or "ambiguous"
        return AutomationRunResult(
            status="awaiting_user",
            detail=_hard_stop_detail(blocker),
            error_code=blocker,
        )
    if result.status == "max_steps":
        return AutomationRunResult(
            status="failed",
            detail="本轮已达到自动操作步数上限，未继续向设备发送动作。",
            error_code="automation_step_limit",
        )
    if result.termination_status == "failure":
        return AutomationRunResult(
            status="failed",
            detail="本地 GUI 模型在最新画面上判断本轮无法完成。",
            error_code="gui_model_reported_failure",
        )
    return AutomationRunResult(
        status="completed",
        detail="本地 GUI 模型在最新画面上结束了本轮设备操作；动作后画面已记录在时间线中。",
        error_code=None,
    )


def _target_busy_result() -> AutomationRunResult:
    return AutomationRunResult(
        status="failed",
        detail="该 Android 目标正在被另一轮自动操作占用；未发送设备动作。",
        error_code="target_busy",
    )


def _hard_stop_detail(reason: str) -> str:
    labels = {
        "credentials_password": "账号凭据或密码",
        "otp_biometric": "验证码或生物识别",
        "identity_verification": "实名或身份核验",
        "payment": "付款或充值",
        "captcha": "CAPTCHA 或人机验证",
        "permission_authorization": "系统权限授权",
        "legal_confirmation": "法律条款确认",
        "ambiguous": "无法可靠判断的页面",
    }
    return f"检测到{labels.get(reason, labels['ambiguous'])}，自动操作已暂停；请在设备上处理后新开一轮继续。"


def _known_failure_code(exc: Exception) -> str:
    candidate = getattr(exc, "code", None)
    if not isinstance(candidate, str) and exc.args and isinstance(exc.args[0], str):
        candidate = exc.args[0]
    allowed_prefixes = (
        "executor_",
        "gui_model_",
        "chat_target_",
        "execution_",
        "target_busy",
    )
    if isinstance(candidate, str) and candidate.startswith(allowed_prefixes):
        return candidate[:100]
    return "android_automation_failed"


def _failure_detail(code: str) -> str:
    if code == "target_busy":
        return "该 Android 目标正在被另一轮自动操作占用；未发送设备动作。"
    if code.startswith("executor_unicode_text_") and code != "executor_unicode_text_uncertain":
        return "无法使用设备的中文输入通道；文本确定未输入，未继续发送后续动作。"
    if code == "executor_unicode_text_uncertain":
        return "中文输入结果不确定，为避免重复输入已停止；未继续发送后续动作。"
    if code.startswith("executor_"):
        return "ADB 执行器在本轮操作中不可用，未继续发送后续动作。"
    if code.startswith("gui_model_"):
        return "本地 GUI 模型在本轮操作中不可用，未继续发送设备动作。"
    if code == "chat_target_serial_changed":
        return "所选 Android 目标的动态连接地址已变化，请重新发现设备后新建对话。"
    if code.startswith("chat_target_"):
        return "所选 Android 目标当前不可用。"
    if code.startswith("execution_"):
        return "云端模型返回的设备目标无效，因此没有操作设备。"
    return "Android 自动化执行失败，未确认目标完成。"
