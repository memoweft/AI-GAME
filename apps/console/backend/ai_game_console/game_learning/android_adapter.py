from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..adb_executor import AdbGuiExecutor
from ..android_automation import parse_gui_owl_tool_call
from ..execution import AndroidScreenshot, GuiAction
from ..gui_owl_client import OpenAICompatibleGuiOwlClient
from ..device_lease import (
    DeviceLease,
    DeviceLeaseHandle,
)
from .domain import (
    ActionProposal,
    GameProfile,
    Observation,
    OutcomeVerifier,
    PolicyMemory,
    Session,
    TransportReceipt,
)
from .profiles import (
    PACKAGE_NAME,
    PROFILE_ID,
    CompiledProfileTask,
    StzbTutorialProfile,
)
from .verifier import LocalEvidenceAssessor, StrictStzbOutcomeVerifier


ForegroundProbe = Callable[[AdbGuiExecutor], str]
ForegroundRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
CancellationCheck = Callable[[], bool]

_FOREGROUND_PACKAGE = re.compile(
    r"(?:mResumedActivity|topResumedActivity|mFocusedApp|mCurrentFocus)"
    r"[^\n]*?\s([A-Za-z0-9_.]+)/(?:[A-Za-z0-9_.$]+)"
)


class StzbAndroidAdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(slots=True)
class StzbAndroidEnvironmentFactory:
    """Production Android Environment Adapter for the bounded STZB profile."""

    executor: AdbGuiExecutor
    model: OpenAICompatibleGuiOwlClient
    assessor: LocalEvidenceAssessor
    foreground_probe: ForegroundProbe = field(
        default_factory=lambda: probe_foreground_package
    )
    post_action_settle_seconds: float = 1.0
    settle_wait: Callable[[float], None] = field(default=time.sleep, repr=False)
    device_lease: DeviceLease | None = None

    def open(
        self,
        *,
        profile: GameProfile,
        target_id: str | None,
        is_cancelled: CancellationCheck,
    ) -> Session:
        if profile.profile_id != PROFILE_ID:
            raise StzbAndroidAdapterError(
                "game_profile_mismatch",
                "Android Adapter 不支持该 GameProfile。",
            )
        if tuple(profile.allowed_actions) != ("tap", "keyevent", "swipe"):
            raise StzbAndroidAdapterError(
                "game_profile_actions_invalid",
                "率土之滨 Profile 的物理动作集合无效。",
            )
        serial = _require_target_binding(target_id, self.executor.serial)
        lease_handle: DeviceLeaseHandle | None = None
        if self.device_lease is not None:
            lease_handle = self.device_lease.acquire(serial)
            if lease_handle is None:
                raise StzbAndroidAdapterError(
                    "target_busy",
                    "该 Android 目标正在被另一轮自动操作占用；未发送设备动作。",
                )
        try:
            # The lease starts before *all* calls into executor/model/device.
            _require_executor_ready(self.executor)
            _require_foreground_package(self.executor, self.foreground_probe)
            return StzbAndroidSession(
                profile=profile,
                target_id=serial,
                executor=self.executor,
                model=self.model,
                assessor=self.assessor,
                foreground_probe=self.foreground_probe,
                is_cancelled=is_cancelled,
                post_action_settle_seconds=self.post_action_settle_seconds,
                settle_wait=self.settle_wait,
                lease_handle=lease_handle,
            )
        except Exception:
            if lease_handle is not None:
                lease_handle.release()
            raise


@dataclass(slots=True)
class StzbAndroidSession:
    profile: GameProfile
    target_id: str
    executor: AdbGuiExecutor
    model: OpenAICompatibleGuiOwlClient
    assessor: LocalEvidenceAssessor
    foreground_probe: ForegroundProbe
    is_cancelled: CancellationCheck
    post_action_settle_seconds: float = 1.0
    settle_wait: Callable[[float], None] = field(default=time.sleep, repr=False)
    lease_handle: DeviceLeaseHandle | None = field(default=None, repr=False)
    profile_rules: StzbTutorialProfile = field(default_factory=StzbTutorialProfile)
    _task: CompiledProfileTask | None = field(default=None, init=False, repr=False)
    _verifier: OutcomeVerifier | None = field(default=None, init=False, repr=False)
    _last_screenshot: AndroidScreenshot | None = field(default=None, init=False, repr=False)
    _action_history: list[str] = field(default_factory=list, init=False, repr=False)
    _swipe_count: int = field(default=0, init=False, repr=False)
    _pending_action: GuiAction | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def verifier(self) -> OutcomeVerifier:
        if self._verifier is None:
            raise StzbAndroidAdapterError(
                "outcome_verifier_not_bound",
                "任务尚未编译，OutcomeVerifier 不可用。",
            )
        return self._verifier

    def observe(self) -> Observation:
        self._require_open()
        self._require_not_cancelled()
        _require_foreground_package(self.executor, self.foreground_probe)
        screenshot = self.executor.capture_screenshot()
        self._last_screenshot = screenshot
        return Observation(
            payload=screenshot.png_bytes,
            summary=f"foreground={PACKAGE_NAME};frame={screenshot.width}x{screenshot.height}",
            mime_type="image/png",
        )

    def propose_action(
        self,
        *,
        instruction: str,
        observation: Observation,
        policy_memory: PolicyMemory,
    ) -> ActionProposal:
        self._require_open()
        self._require_not_cancelled()
        if policy_memory.profile_id != self.profile.profile_id:
            raise StzbAndroidAdapterError(
                "policy_memory_profile_mismatch",
                "PolicyMemory 与率土之滨 Profile 不兼容。",
            )
        task = self.profile_rules.compile_task(instruction)
        if self._task is None:
            self._task = task
            self._verifier = StrictStzbOutcomeVerifier(
                task=task,
                assessor=self.assessor,
                profile=self.profile_rules,
            )
        elif self._task.task_id != task.task_id:
            raise StzbAndroidAdapterError(
                "episode_task_changed",
                "LearningEpisode 运行期间不能更换任务。",
            )

        screenshot = self._matching_screenshot(observation)
        preflight = self.assessor.assess(
            observation,
            profile_id=self.profile.profile_id,
            task_id=task.task_id,
        )
        if preflight.package_name != PACKAGE_NAME:
            raise StzbAndroidAdapterError(
                "foreground_package_changed",
                "前台应用已变化，未向设备发送动作。",
            )
        if preflight.unsafe_reason:
            raise StzbAndroidAdapterError(
                "unsafe_scene",
                "当前画面超出率土之滨 Profile 的允许范围。",
            )
        if (
            not preflight.scene
            or preflight.failure_reason
            or preflight.confidence < self.profile_rules.min_verifier_confidence
        ):
            raise StzbAndroidAdapterError(
                "scene_unconfirmed",
                "当前画面无法被本地证据验证器可靠确认。",
            )

        response = self.model.propose_action(
            screenshot,
            goal=task.canonical_goal,
            action_history=tuple(
                (
                    self._action_history[-3:]
                    + _policy_memory_hints(policy_memory, screenshot)
                )[-6:]
            ),
        )
        self._require_not_cancelled()
        try:
            decision = parse_gui_owl_tool_call(
                response,
                target_id=self.target_id,
                screenshot=screenshot,
            )
        except ValueError as exc:
            raise StzbAndroidAdapterError(
                "policy_action_invalid",
                "本地 GUI Policy 返回了无效动作，未向设备发送输入。",
            ) from exc
        finally:
            # Never retain the raw model response beyond strict parsing.
            response = ""

        if decision.kind == "execute":
            action = decision.action
            if action is None:  # pragma: no cover - parser invariant
                raise StzbAndroidAdapterError("policy_action_invalid", "Policy 动作无效。")
            self._validate_action(action, screenshot, consume_swipe=False)
            self._pending_action = action
            return ActionProposal(kind="execute", action=action)
        if decision.kind == "wait":
            wait_seconds = min(decision.wait_seconds or 0.0, 0.5)
            self._action_history.append("policy requested bounded wait")
            return ActionProposal(kind="wait", wait_seconds=wait_seconds)
        if decision.kind == "terminate":
            # Terminate is only a proposal to stop. The engine must still call
            # the independent OutcomeVerifier on fresh local evidence.
            self._action_history.append("policy requested terminate; outcome still unverified")
            return ActionProposal(kind="terminate")
        raise StzbAndroidAdapterError(
            "policy_interact_forbidden",
            "率土之滨 Profile 不允许人工接管型 Policy 动作。",
        )

    def execute(self, action: GuiAction) -> TransportReceipt:
        self._require_open()
        self._require_not_cancelled()
        screenshot = self._last_screenshot
        if screenshot is None:
            raise StzbAndroidAdapterError(
                "observation_required",
                "执行动作前必须获取当前画面。",
            )
        if self._pending_action != action:
            raise StzbAndroidAdapterError(
                "proposal_action_mismatch",
                "只能执行本轮由 Policy 提出的同一个动作。",
            )
        self._validate_action(action, screenshot, consume_swipe=True)
        _require_foreground_package(self.executor, self.foreground_probe)
        try:
            receipt = self.executor.execute(action)
        except RuntimeError as exc:
            code = str(exc.args[0]) if exc.args else "executor_action_uncertain"
            self._pending_action = None
            if code == "executor_action_rejected":
                return TransportReceipt(status="rejected", detail="executor_action_rejected")
            return TransportReceipt(status="uncertain", detail="executor_action_uncertain")
        self._pending_action = None
        if not receipt.accepted:
            return TransportReceipt(status="rejected", detail="executor_action_rejected")
        if self.post_action_settle_seconds > 0:
            self.settle_wait(self.post_action_settle_seconds)
        self._action_history.append(f"transport accepted: {_safe_action_name(action)}")
        return TransportReceipt(status="accepted", detail="transport_accepted")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._last_screenshot = None
        self._pending_action = None
        self._action_history.clear()
        if self.lease_handle is not None:
            self.lease_handle.release()

    def _matching_screenshot(self, observation: Observation) -> AndroidScreenshot:
        screenshot = self._last_screenshot
        if (
            screenshot is None
            or observation.payload is None
            or observation.payload != screenshot.png_bytes
            or observation.mime_type != "image/png"
        ):
            raise StzbAndroidAdapterError(
                "observation_mismatch",
                "Policy 只能使用本轮最新的本机 Android 画面。",
            )
        return screenshot

    def _validate_action(
        self,
        action: GuiAction,
        screenshot: AndroidScreenshot,
        *,
        consume_swipe: bool,
    ) -> None:
        task = self._task
        if task is None:
            raise StzbAndroidAdapterError("task_not_compiled", "执行动作前必须编译任务。")
        if action.action == "tap":
            if "tap" not in task.allowed_actions:
                raise _action_blocked()
            return
        if action.action == "keyevent":
            if action.keycode != "KEYCODE_BACK" or "back" not in task.allowed_actions:
                raise _action_blocked()
            return
        if action.action == "swipe":
            self._validate_swipe(action, screenshot, task, consume=consume_swipe)
            return
        # text, long_press, Home/app-switch and all future actions fail closed.
        raise _action_blocked()

    def _validate_swipe(
        self,
        action: GuiAction,
        screenshot: AndroidScreenshot,
        task: CompiledProfileTask,
        *,
        consume: bool,
    ) -> None:
        policy = task.swipe_policy
        if policy is None or "swipe" not in task.allowed_actions:
            raise _action_blocked()
        if self._swipe_count >= policy.max_swipes:
            raise StzbAndroidAdapterError(
                "controlled_swipe_limit",
                "本轮已达到受控滑动上限。",
            )
        coordinates = (action.x, action.y, action.end_x, action.end_y)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in coordinates):
            raise _action_blocked()
        assert action.x is not None and action.y is not None
        assert action.end_x is not None and action.end_y is not None
        if action.duration_ms is None or not policy.min_duration_ms <= action.duration_ms <= policy.max_duration_ms:
            raise _action_blocked()
        start_x = _normalize(action.x, screenshot.width)
        end_x = _normalize(action.end_x, screenshot.width)
        start_y = _normalize(action.y, screenshot.height)
        end_y = _normalize(action.end_y, screenshot.height)
        distance = abs(end_y - start_y)
        if (
            not policy.min_distance_normalized <= distance <= policy.max_distance_normalized
            or abs(end_x - start_x) > 180
        ):
            raise _action_blocked()
        direction = "up" if end_y < start_y else "down"
        if direction not in policy.directions:
            raise _action_blocked()
        if consume:
            self._swipe_count += 1

    def _require_open(self) -> None:
        if self._closed:
            raise StzbAndroidAdapterError("environment_closed", "Android 环境已关闭。")

    def _require_not_cancelled(self) -> None:
        if self.is_cancelled():
            raise StzbAndroidAdapterError("user_cancelled", "LearningEpisode 已停止。")


def probe_foreground_package(
    executor: AdbGuiExecutor,
    *,
    runner: ForegroundRunner | None = None,
) -> str:
    """Read only the resumed Android package with an explicit shell-free ADB call."""

    if executor.adb_path is None or not executor.serial:
        raise StzbAndroidAdapterError(
            "executor_not_configured",
            "ADB 路径或目标序列号尚未配置。",
        )
    command = (
        str(executor.adb_path.resolve()),
        "-s",
        executor.serial,
        "shell",
        "dumpsys",
        "activity",
        "activities",
    )
    invoke = runner or _run_foreground_command
    try:
        completed = invoke(command, executor.COMMAND_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StzbAndroidAdapterError(
            "foreground_probe_failed",
            "无法验证 Android 前台应用。",
        ) from exc
    if completed.returncode != 0:
        raise StzbAndroidAdapterError(
            "foreground_probe_failed",
            "无法验证 Android 前台应用。",
        )
    match = _FOREGROUND_PACKAGE.search(completed.stdout or "")
    if match is None:
        raise StzbAndroidAdapterError(
            "foreground_unconfirmed",
            "Android 前台应用无法确认。",
        )
    return match.group(1)


def _run_foreground_command(
    command: Sequence[str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(
        tuple(command),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        shell=False,
        creationflags=creation_flags,
    )


def _require_executor_ready(executor: AdbGuiExecutor) -> None:
    result = executor.probe()
    if not result.ready:
        code = result.blocker["code"] if result.blocker else "executor_not_ready"
        raise StzbAndroidAdapterError(code, "ADB 执行器当前不可用。")


def _require_foreground_package(
    executor: AdbGuiExecutor,
    foreground_probe: ForegroundProbe,
) -> None:
    if foreground_probe(executor) != PACKAGE_NAME:
        raise StzbAndroidAdapterError(
            "foreground_package_mismatch",
            "率土之滨不是当前 Android 前台应用，未发送动作。",
        )


def _require_target_binding(target_id: str | None, serial: str | None) -> str:
    if not serial:
        raise StzbAndroidAdapterError("executor_not_configured", "ADB 目标尚未配置。")
    if target_id is not None and target_id not in {serial, f"adb:{serial}"}:
        raise StzbAndroidAdapterError(
            "target_binding_mismatch",
            "LearningEpisode 目标与当前 ADB 目标不一致。",
        )
    return serial


def _normalize(value: int, size: int) -> int:
    if size <= 1 or not 0 <= value < size:
        raise _action_blocked()
    return round(value * 1000 / (size - 1))


def _action_blocked() -> StzbAndroidAdapterError:
    return StzbAndroidAdapterError(
        "profile_action_blocked",
        "该动作超出率土之滨低频教程与菜单导航 Profile 的允许范围。",
    )


def _safe_action_name(action: GuiAction) -> str:
    if action.action == "keyevent":
        return "back"
    return action.action


def _policy_memory_hints(
    memory: PolicyMemory,
    screenshot: AndroidScreenshot,
) -> list[str]:
    """Render at most three positive verified memories as non-executable hints."""

    hints: list[str] = []
    eligible = (
        item
        for item in reversed(memory.trajectory)
        if item.reward > 0 and item.verifier_detail.startswith("confirmed_")
    )
    for item in eligible:
        action = item.action
        try:
            if action.action == "tap" and action.x is not None and action.y is not None:
                description = (
                    "tap normalized "
                    f"[{_normalize(action.x, screenshot.width)},"
                    f"{_normalize(action.y, screenshot.height)}]"
                )
            elif (
                action.action == "swipe"
                and action.x is not None
                and action.y is not None
                and action.end_x is not None
                and action.end_y is not None
            ):
                description = (
                    "swipe normalized "
                    f"[{_normalize(action.x, screenshot.width)},"
                    f"{_normalize(action.y, screenshot.height)}] to "
                    f"[{_normalize(action.end_x, screenshot.width)},"
                    f"{_normalize(action.end_y, screenshot.height)}]"
                )
            elif action.action == "keyevent" and action.keycode == "KEYCODE_BACK":
                description = "Back"
            else:
                continue
        except StzbAndroidAdapterError:
            continue
        status = item.verifier_detail.partition(":")[0]
        hints.append(
            "verified policy memory hint only; re-evaluate current frame: "
            f"{description}; reward={item.reward:.3f}; outcome={status}"
        )
        if len(hints) == 3:
            break
    hints.reverse()
    return hints
