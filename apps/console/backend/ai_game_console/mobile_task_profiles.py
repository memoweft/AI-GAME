from __future__ import annotations

import hashlib
import re


_SPACE = re.compile(r"\s+")
_STZB_NAMES = ("率土之滨", "率土")
_SOUL_NAMES = ("soul", "Soul", "SOUL")
_STZB_LAUNCH = re.compile(r"(?:打开|启动|进入|登录)(?:《)?(?:游戏)?率土(?:之滨)?")


def resolve_mobile_skill_scope(goal: str, target_id: str | None) -> str | None:
    """Derive an internal, stable experience scope without exposing raw IDs.

    The scope describes reusable task knowledge, not a device connection.  A
    task remains bound to its selected target separately, so verified game
    experience can later transfer from an emulator to a compatible tablet.
    Soul is deliberately excluded because its conversation and relationship
    learning is owned by dating-copilot rather than MobileTask.
    """

    del target_id
    normalized = _SPACE.sub(" ", goal.strip())
    if not normalized:
        return None
    if any(name in normalized for name in _SOUL_NAMES):
        return None
    if any(name in normalized for name in _STZB_NAMES):
        if any(word in normalized for word in ("奖励", "领取", "签到", "日常")):
            return "stzb/daily-rewards/v1"
        if any(word in normalized for word in ("教程", "引导", "新手", "下一阶段")):
            return "stzb/tutorial/v1"
        if any(
            phrase in normalized
            for phrase in (
                "任务面板",
                "任务列表",
                "城池地图",
                "地图主界面",
                "返回地图",
                "左侧任务",
            )
        ):
            return "stzb/ui-navigation/v1"
        if _STZB_LAUNCH.search(normalized) or "从当前桌面" in normalized:
            return "stzb/launch/v1"
        return "stzb/general/v1"

    fingerprint = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()[:24]
    return f"generic/exact-goal/v1/{fingerprint}"
