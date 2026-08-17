from __future__ import annotations

from ai_game_console.mobile_task_profiles import resolve_mobile_skill_scope


def test_rate_soil_goals_use_stable_task_kind_scopes_across_targets() -> None:
    emulator = resolve_mobile_skill_scope(
        "打开率土之滨，领取今天所有能领取的奖励",
        "adb:127.0.0.1:16384",
    )
    tablet = resolve_mobile_skill_scope(
        "打开率土之滨，领取今天所有能领取的奖励",
        "adb:R58M1234AB",
    )

    assert emulator == "stzb/daily-rewards/v1"
    assert tablet == emulator


def test_rate_soil_internal_navigation_does_not_pollute_launch_memory() -> None:
    assert (
        resolve_mobile_skill_scope(
            "从当前桌面打开率土之滨，进入游戏主界面",
            "adb:127.0.0.1:16384",
        )
        == "stzb/launch/v1"
    )
    assert (
        resolve_mobile_skill_scope(
            "在当前率土之滨城池地图主界面，打开左侧任务面板，确认后返回地图",
            "adb:127.0.0.1:16384",
        )
        == "stzb/ui-navigation/v1"
    )


def test_soul_keeps_learning_in_its_owned_application() -> None:
    assert resolve_mobile_skill_scope("打开 Soul 继续聊天", None) is None


def test_generic_scope_is_stable_but_does_not_contain_the_goal() -> None:
    goal = "打开系统设置，把屏幕亮度调到 40%"
    first = resolve_mobile_skill_scope(goal, "adb:one")
    second = resolve_mobile_skill_scope("  打开系统设置，把屏幕亮度调到   40%  ", "adb:two")

    assert first == second
    assert first is not None
    assert goal not in first
    assert first.startswith("generic/exact-goal/v1/")
