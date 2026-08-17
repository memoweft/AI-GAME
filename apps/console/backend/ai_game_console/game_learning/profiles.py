from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from .domain import GameProfile


PROFILE_ID: Final = "stzb-tutorial-v1"
PACKAGE_NAME: Final = "com.netease.stzb.netease"

AllowedAction = Literal["tap", "back", "wait", "swipe"]


class ProfileTaskError(ValueError):
    """A stable, fail-closed error raised while compiling a profile task."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class ControlledSwipePolicy:
    max_swipes: int
    directions: tuple[Literal["up", "down"], ...]
    min_distance_normalized: int = 180
    max_distance_normalized: int = 650
    min_duration_ms: int = 150
    max_duration_ms: int = 900


@dataclass(frozen=True, slots=True)
class CompiledProfileTask:
    task_id: str
    canonical_goal: str
    success_marker: str
    progress_markers: tuple[str, ...]
    allowed_actions: tuple[AllowedAction, ...]
    swipe_policy: ControlledSwipePolicy | None


@dataclass(frozen=True, slots=True)
class StzbTutorialProfile:
    profile_id: str = PROFILE_ID
    package_name: str = PACKAGE_NAME
    display_name: str = "率土之滨低频教程与菜单导航"
    max_transitions: int = 25
    max_duration_seconds: int = 180
    no_progress_limit: int = 3
    min_verifier_confidence: float = 0.90

    def compile_task(self, instruction: str) -> CompiledProfileTask:
        return compile_stzb_task(instruction)


_NO_SWIPE = ("tap", "back", "wait")
_CONTROLLED_SWIPE = ("tap", "back", "wait", "swipe")

_TASKS: Final[dict[str, CompiledProfileTask]] = {
    "continue_tutorial": CompiledProfileTask(
        task_id="continue_tutorial",
        canonical_goal="按照当前游戏内教程提示推进一个低频步骤。",
        success_marker="tutorial_step_advanced",
        progress_markers=("tutorial_prompt_changed", "allowed_menu_opened"),
        allowed_actions=_NO_SWIPE,
        swipe_policy=None,
    ),
    "open_task_list": CompiledProfileTask(
        task_id="open_task_list",
        canonical_goal="只打开并查看任务列表，不领取奖励。",
        success_marker="task_list_visible",
        progress_markers=("task_entry_visible", "allowed_menu_opened"),
        allowed_actions=_CONTROLLED_SWIPE,
        swipe_policy=ControlledSwipePolicy(max_swipes=2, directions=("up", "down")),
    ),
    "open_general_list": CompiledProfileTask(
        task_id="open_general_list",
        canonical_goal="只打开并查看武将列表，不执行招募或强化。",
        success_marker="general_list_visible",
        progress_markers=("general_entry_visible", "allowed_menu_opened"),
        allowed_actions=_CONTROLLED_SWIPE,
        swipe_policy=ControlledSwipePolicy(max_swipes=2, directions=("up", "down")),
    ),
    "open_army_list": CompiledProfileTask(
        task_id="open_army_list",
        canonical_goal="只打开并查看部队列表，不进行出征或真人交互。",
        success_marker="army_list_visible",
        progress_markers=("army_entry_visible", "allowed_menu_opened"),
        allowed_actions=_CONTROLLED_SWIPE,
        swipe_policy=ControlledSwipePolicy(max_swipes=2, directions=("up", "down")),
    ),
    "open_map": CompiledProfileTask(
        task_id="open_map",
        canonical_goal="只打开并查看地图，不出征、不匹配、不与真人交互。",
        success_marker="map_visible",
        progress_markers=("map_entry_visible", "allowed_menu_opened"),
        allowed_actions=_NO_SWIPE,
        swipe_policy=None,
    ),
    "return_to_main": CompiledProfileTask(
        task_id="return_to_main",
        canonical_goal="返回已验证的主界面。",
        success_marker="main_scene_visible",
        progress_markers=("allowed_menu_closed", "main_scene_candidate"),
        allowed_actions=_NO_SWIPE,
        swipe_policy=None,
    ),
}

_TASK_ALIASES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "continue_tutorial",
        ("继续教程", "推进教程", "当前教程", "教程提示", "教程下一步"),
    ),
    (
        "open_task_list",
        ("打开任务", "查看任务", "任务列表", "进入任务"),
    ),
    (
        "open_general_list",
        ("打开武将", "查看武将", "武将列表", "进入武将"),
    ),
    (
        "open_army_list",
        ("打开部队", "查看部队", "部队列表", "进入部队"),
    ),
    (
        "open_map",
        ("打开地图", "查看地图", "进入地图"),
    ),
    (
        "return_to_main",
        ("返回主界面", "回到主界面", "返回主城", "回到主城"),
    ),
)

_FORBIDDEN_TERMS: Final[tuple[str, ...]] = (
    "登录",
    "登陆",
    "注册",
    "协议",
    "条款",
    "用户协议",
    "隐私协议",
    "服务条款",
    "同意条款",
    "实名",
    "身份验证",
    "验证码",
    "otp",
    "captcha",
    "人机验证",
    "支付",
    "付款",
    "充值",
    "购买",
    "商城",
    "商店",
    "抽卡",
    "抽将",
    "招募",
    "领取",
    "奖励",
    "礼包",
    "授权",
    "权限",
    "聊天",
    "私聊",
    "联盟",
    "同盟",
    "公开匹配",
    "匹配",
    "排位",
    "真人",
    "其他玩家",
    "账号设置",
    "账户设置",
    "设置",
    "账号",
    "账户",
    "密码",
    "手机号",
    "邮箱",
    "强化",
    "出征",
)

_MULTI_SENTENCE = re.compile(r"[。！？!?].+[^。！？!?\s]", re.DOTALL)


def stzb_tutorial_profile() -> StzbTutorialProfile:
    return StzbTutorialProfile()


def stzb_game_profile(*, default_target_id: str | None = None) -> GameProfile:
    """Return the core profile manifest used by the learning engine.

    ``keyevent`` is included only so the Android Adapter can transport Back;
    that Adapter rejects Home, app-switch, Enter and every other key code.
    Wait and terminate are proposal kinds, not physical ``GuiAction`` values.
    """

    profile = StzbTutorialProfile()
    return GameProfile(
        profile_id=profile.profile_id,
        name=profile.display_name,
        allowed_actions=("tap", "keyevent", "swipe"),
        max_actions=profile.max_transitions,
        max_duration_seconds=float(profile.max_duration_seconds),
        default_target_id=default_target_id,
        revision=1,
    )


def compile_stzb_task(instruction: str) -> CompiledProfileTask:
    if not isinstance(instruction, str):
        raise ProfileTaskError("instruction_invalid", "任务说明必须是一句话。")
    normalized = " ".join(instruction.strip().split())
    if not normalized or len(normalized) > 200 or "\n" in instruction or "\r" in instruction:
        raise ProfileTaskError("instruction_invalid", "任务说明必须是 1 到 200 字的一句话。")
    if _MULTI_SENTENCE.search(normalized):
        raise ProfileTaskError("instruction_not_single_sentence", "一次只允许提交一句任务说明。")

    lowered = normalized.casefold()
    blocked = next((term for term in _FORBIDDEN_TERMS if term.casefold() in lowered), None)
    if blocked is not None:
        raise ProfileTaskError(
            "task_scope_blocked",
            "该任务超出率土之滨低频教程与菜单导航 Profile 的允许范围。",
        )

    matches = {
        task_id
        for task_id, aliases in _TASK_ALIASES
        if any(alias.casefold() in lowered for alias in aliases)
    }
    if len(matches) != 1:
        code = "task_ambiguous" if matches else "task_not_supported"
        raise ProfileTaskError(
            code,
            "任务必须明确对应一个已允许的教程或只读菜单导航目标。",
        )
    return _TASKS[matches.pop()]


def list_stzb_tasks() -> tuple[CompiledProfileTask, ...]:
    return tuple(_TASKS.values())
