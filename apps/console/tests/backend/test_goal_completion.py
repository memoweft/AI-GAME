from __future__ import annotations

from pathlib import Path

from ai_game_console.goal_runtime import (
    CriterionAssessment,
    GoalCompletionAssessment,
    GoalService,
    GoalSpecificationDraft,
    SQLiteGoalStore,
    SuccessCriterion,
)


class CompletedArchive:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.start_calls: list[dict] = []

    def start(self, goal, client_request_id, **options):
        self.start_calls.append({"goal": goal, "client_request_id": client_request_id, **options})
        return self.state

    def inspect(self, task_id):
        assert task_id == self.state["task_id"]
        return self.state


def _state(goal: str, subgoals: list[str], satisfied_sequences: list[int]) -> dict:
    return {
        "task_id": "mobile-complete-1",
        "goal": goal,
        "status": "completed",
        "detail": "全部 Subgoal 已由新鲜观察验证。",
        "error_code": None,
        "plan": {
            "revision": 1,
            "subgoals": [
                {"index": index, "description": description, "status": "completed"}
                for index, description in enumerate(subgoals)
            ],
        },
        "attempts": [
            {
                "sequence": sequence,
                "verification": {
                    "satisfied": True,
                    "progress": True,
                    "uncertain": False,
                    "evidence": f"verified evidence {sequence}",
                },
            }
            for sequence in satisfied_sequences
        ],
        "events": [],
    }


def test_historical_launch_only_daily_plan_remains_partial_and_unlearned(
    tmp_path: Path,
) -> None:
    goal = "打开游戏率土之滨，并领取今日奖励，做今日任务"
    state = _state(goal, ["点击桌面上的率土之滨应用图标以打开游戏"], [1])
    runtime = CompletedArchive(state)
    promotions = []

    def specify(_: str) -> GoalSpecificationDraft:
        return GoalSpecificationDraft(
            {"classification": "finite_phone_goal"},
            (
                SuccessCriterion("game_open", "率土之滨已经打开", "新鲜画面", "打开游戏率土之滨"),
                SuccessCriterion("rewards_claimed", "今日可领取奖励已经领取", "新鲜画面", "领取今日奖励"),
                SuccessCriterion("daily_tasks_done", "今日任务已经完成", "清单复查", "做今日任务"),
            ),
        )

    def verify(*_):
        return GoalCompletionAssessment(
            "partial",
            (
                CriterionAssessment("game_open", True, (0,), (1,), "启动画面已验证"),
                CriterionAssessment(
                    "rewards_claimed", False, (), (), "没有奖励领取完成证据"
                ),
                CriterionAssessment(
                    "daily_tasks_done", False, (), (), "没有每日任务清单完成证据"
                ),
            ),
            (),
            "只验证了游戏启动；奖励和每日任务尚未完成。",
        )

    service = GoalService(
        SQLiteGoalStore(tmp_path / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial="127.0.0.1:16384",
        specify_goal=specify,
        verify_completion=verify,
        promote_verified_success=lambda *args: promotions.append(args),
    )

    record = service.create(goal, "historical-launch-only")
    completion = service.store.completion(record.id)

    assert record.execution_status == "CANDIDATE_COMPLETE"
    assert completion is not None
    assert completion["verdict"] == "partial"
    assert [item["satisfied"] for item in completion["criteria"]] == [True, False, False]
    assert runtime.start_calls[0]["promote_success_memory"] is False
    assert promotions == []


def test_all_frozen_criteria_with_valid_plan_and_attempt_evidence_complete_goal(
    tmp_path: Path,
) -> None:
    goal = "打开设置，查看当前可见的电池信息，告诉我，然后返回桌面。"
    state = _state(goal, ["打开设置", "查看电池信息", "返回桌面"], [1, 2, 3])
    runtime = CompletedArchive(state)
    promotions = []
    criteria = (
        SuccessCriterion("settings_open", "设置已经打开", "新鲜画面", "打开设置"),
        SuccessCriterion("battery_visible", "至少一个当前电池事实可见", "新鲜画面", "查看当前可见的电池信息"),
        SuccessCriterion("result_reported", "结果包含已验证电池事实", "结果文本与电池画面", "告诉我"),
        SuccessCriterion("home_restored", "最终返回桌面", "最终新鲜画面", "返回桌面"),
    )

    def verify(*_):
        return GoalCompletionAssessment(
            "verified",
            (
                CriterionAssessment("settings_open", True, (0,), (1,), "设置已打开"),
                CriterionAssessment("battery_visible", True, (1,), (2,), "电量可见"),
                CriterionAssessment("result_reported", True, (1,), (2,), "结果引用电池事实"),
                CriterionAssessment("home_restored", True, (2,), (3,), "桌面已验证"),
            ),
            ("电量 73%", "未在充电", "省电模式关闭", "最终位于桌面"),
            "当前电量 73%，未在充电，省电模式关闭；已返回桌面。",
        )

    service = GoalService(
        SQLiteGoalStore(tmp_path / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial="127.0.0.1:16384",
        specify_goal=lambda _: GoalSpecificationDraft(
            {"classification": "finite_phone_goal"}, criteria
        ),
        verify_completion=verify,
        promote_verified_success=lambda *args: promotions.append(args),
    )

    record = service.create(goal, "battery-complete")

    assert record.execution_status == "COMPLETED"
    assert record.terminal_at is not None
    assert service.store.completion(record.id)["verified_facts"][0] == "电量 73%"
    assert service.inspect(record.id).execution_status == "COMPLETED"
    assert promotions
    assert set(promotions) == {("mobile-complete-1", record.id, 1)}


def test_invalid_model_evidence_reference_cannot_complete_goal(tmp_path: Path) -> None:
    goal = "打开设置"
    state = _state(goal, ["打开设置"], [1])
    runtime = CompletedArchive(state)

    service = GoalService(
        SQLiteGoalStore(tmp_path / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial="127.0.0.1:16384",
        specify_goal=lambda _: GoalSpecificationDraft(
            {"classification": "finite_phone_goal"},
            (SuccessCriterion("settings_open", "设置已经打开", "新鲜画面", "打开设置"),),
        ),
        verify_completion=lambda *_: GoalCompletionAssessment(
            "verified",
            (CriterionAssessment("settings_open", True, (0,), (999,), "模型声称完成"),),
            ("设置已打开",),
            "设置已打开。",
        ),
    )

    record = service.create(goal, "invalid-reference")
    completion = service.store.completion(record.id)

    assert record.execution_status == "CANDIDATE_COMPLETE"
    assert completion["verdict"] == "uncertain"
    assert completion["verified_facts"] == []

    service.verify_completion = lambda *_: GoalCompletionAssessment(
        "verified",
        (CriterionAssessment("settings_open", True, (0,), (1,), "设置画面已验证"),),
        ("设置已打开",),
        "设置已打开。",
    )
    retried = service.retry_completion(record.id)

    assert retried.execution_status == "COMPLETED"
    assert [item["verdict"] for item in service.store.completions(record.id)] == [
        "uncertain", "verified",
    ]


def test_specifier_cannot_silently_drop_an_original_goal_clause(tmp_path: Path) -> None:
    goal = "打开游戏率土之滨，并领取今日奖励，做今日任务"
    runtime = CompletedArchive(_state(goal, ["打开率土之滨"], [1]))
    service = GoalService(
        SQLiteGoalStore(tmp_path / "goals.db"),
        mobile_runtime=runtime,
        mobile_archive=runtime,
        configured_serial="127.0.0.1:16384",
        specify_goal=lambda _: GoalSpecificationDraft(
            {"classification": "finite_phone_goal"},
            (SuccessCriterion("game_open", "率土已打开", "新鲜画面", "打开游戏率土之滨"),),
        ),
        verify_completion=lambda *_: None,
    )

    record = service.create(goal, "omitted-clauses")

    assert record.execution_status == "WAITING_CONFIGURATION"
    assert record.waiting_reason["code"] == "goal_specification_unavailable"
    assert runtime.start_calls == []
