from __future__ import annotations

import pytest

from ai_game_console.game_learning.profiles import (
    PACKAGE_NAME,
    PROFILE_ID,
    ProfileTaskError,
    compile_stzb_task,
    list_stzb_tasks,
    stzb_game_profile,
    stzb_tutorial_profile,
)


def test_stzb_profile_is_bounded_to_low_frequency_tutorial_navigation() -> None:
    profile = stzb_tutorial_profile()

    assert profile.profile_id == PROFILE_ID == "stzb-tutorial-v1"
    assert profile.package_name == PACKAGE_NAME == "com.netease.stzb.netease"
    assert profile.max_transitions == 25
    assert profile.max_duration_seconds == 180
    assert profile.no_progress_limit == 3
    assert profile.min_verifier_confidence == 0.90
    core_profile = stzb_game_profile(default_target_id="adb-target")
    assert core_profile.profile_id == profile.profile_id
    assert core_profile.allowed_actions == ("tap", "keyevent", "swipe")
    assert core_profile.max_actions == 25
    assert core_profile.max_duration_seconds == 180.0
    assert core_profile.default_target_id == "adb-target"
    assert {task.task_id for task in list_stzb_tasks()} == {
        "continue_tutorial",
        "open_task_list",
        "open_general_list",
        "open_army_list",
        "open_map",
        "return_to_main",
    }


@pytest.mark.parametrize(
    ("instruction", "task_id"),
    [
        ("按照当前教程提示继续教程", "continue_tutorial"),
        ("请打开任务列表看看", "open_task_list"),
        ("查看武将列表", "open_general_list"),
        ("打开部队列表", "open_army_list"),
        ("进入地图", "open_map"),
        ("返回主界面", "return_to_main"),
    ],
)
def test_one_sentence_instruction_compiles_to_finite_catalog(
    instruction: str,
    task_id: str,
) -> None:
    task = compile_stzb_task(instruction)

    assert task.task_id == task_id
    assert set(task.allowed_actions) <= {"tap", "back", "wait", "swipe"}
    assert "text" not in task.allowed_actions
    assert "long_press" not in task.allowed_actions
    assert "home" not in task.allowed_actions
    assert "app_switch" not in task.allowed_actions


@pytest.mark.parametrize(
    "instruction",
    [
        "登录账号",
        "登陆游戏",
        "同意协议",
        "同意用户协议",
        "完成实名认证",
        "输入验证码",
        "充值并购买礼包",
        "去商店抽卡",
        "授权系统权限",
        "打开聊天联系真人玩家",
        "加入联盟",
        "开始公开匹配",
        "打开账号设置",
        "打开设置",
        "领取任务奖励",
        "强化武将",
        "让部队出征",
    ],
)
def test_out_of_scope_instruction_is_blocked_before_policy_use(instruction: str) -> None:
    with pytest.raises(ProfileTaskError) as raised:
        compile_stzb_task(instruction)

    assert raised.value.code == "task_scope_blocked"
    assert instruction not in raised.value.public_message


def test_unknown_ambiguous_and_multi_sentence_tasks_fail_closed() -> None:
    with pytest.raises(ProfileTaskError) as unknown:
        compile_stzb_task("做点事情")
    assert unknown.value.code == "task_not_supported"

    with pytest.raises(ProfileTaskError) as ambiguous:
        compile_stzb_task("打开任务列表并查看武将列表")
    assert ambiguous.value.code == "task_ambiguous"

    with pytest.raises(ProfileTaskError) as multiple:
        compile_stzb_task("打开地图。然后返回主界面")
    assert multiple.value.code == "instruction_not_single_sentence"


def test_swipe_is_available_only_for_catalog_tasks_that_need_scrolling() -> None:
    task_list = compile_stzb_task("打开任务列表")
    map_task = compile_stzb_task("打开地图")

    assert task_list.swipe_policy is not None
    assert task_list.swipe_policy.max_swipes == 2
    assert task_list.swipe_policy.directions == ("up", "down")
    assert map_task.swipe_policy is None
    assert map_task.allowed_actions == ("tap", "back", "wait")
