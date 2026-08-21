from __future__ import annotations

import re


STZB_DAILY_GOAL_FAMILY = "stzb/daily/vnext"

_APP_HINT = re.compile(r"(?:率土之滨|率土|\bstzb\b)", re.IGNORECASE)
_DAILY_HINT = re.compile(
    r"(?:今天|今日|当天|每日|每天|日常|签到|活跃|daily)", re.IGNORECASE
)
_WORK_HINT = re.compile(r"(?:任务|奖励|领取|完成|清单|task|reward|claim)", re.IGNORECASE)


def normalize_goal_family(goal: str) -> str | None:
    """Recognize the family from semantic variants, not one exact sentence."""

    normalized = re.sub(r"\s+", " ", goal.strip())
    if _APP_HINT.search(normalized) and _DAILY_HINT.search(normalized) and _WORK_HINT.search(
        normalized
    ):
        return STZB_DAILY_GOAL_FAMILY
    return None
