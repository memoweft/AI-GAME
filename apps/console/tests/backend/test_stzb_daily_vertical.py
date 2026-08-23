from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ai_game_console.goal_families import (
    STZB_DAILY_GOAL_FAMILY,
    is_stzb_discovery_only_goal,
    normalize_goal_family,
)
from ai_game_console.goal_runtime import StructuredGoalModel
from ai_game_console.goal_runtime.domain import (
    CriterionAssessment,
    GoalCompletionAssessment,
    GoalRecord,
)
from ai_game_console.goal_runtime.service import (
    GoalService,
    _daily_checklist_gate,
    _looks_like_daily_checklist_evidence,
)
from ai_game_console.goal_runtime.stzb_daily import (
    DailyChecklistItem,
    DailyChecklistSnapshot,
    SQLiteDailyChecklistStore,
    StzbDailyProgressController,
    _canonical_activity_item,
    _is_explicit_activity_daily_item,
)
from ai_game_console.goal_runtime.stzb_daily_benchmark import (
    run_stzb_daily_fixture_benchmark,
)
from ai_game_console.mobile_agent import Observation, PlanContext, Subgoal, TaskPlan
from ai_game_console.mobile_task_adapter import (
    LocalMobileEvidenceStore,
    OpenAICompatibleToolRoleModel,
    _executor_prompt,
    _planner_prompt,
    _reflection_prompt,
    _stzb_daily_verdict_guard,
    _stzb_daily_verification_instruction,
)
from ai_game_console.execution import AndroidScreenshot


def _item(title: str, status: str) -> DailyChecklistItem:
    from ai_game_console.goal_runtime.stzb_daily import checklist_item_id

    return DailyChecklistItem(
        checklist_item_id(title), title, status, f"画面显示{title}:{status}"  # type: ignore[arg-type]
    )


def _snapshot(
    evidence_id: str, *items: DailyChecklistItem, date_label: str = "今日"
) -> DailyChecklistSnapshot:
    return DailyChecklistSnapshot(
        evidence_id, date_label, True, tuple(items), f"2026-08-21T00:00:0{evidence_id[-1]}+00:00"
    )


def test_stzb_daily_family_normalizes_wording_variants_without_reclassifying_tutorial() -> None:
    variants = (
        "帮我把率土之滨今天的每日任务做完",
        "率土今天的日常奖励都领取掉",
        "Finish all STZB daily tasks for today",
        "进入率土，把当天活跃任务完成",
    )
    assert {normalize_goal_family(item) for item in variants} == {
        STZB_DAILY_GOAL_FAMILY
    }
    assert normalize_goal_family("打开率土之滨完成新手教程") is None


def test_stzb_discovery_only_goal_does_not_expand_into_item_execution() -> None:
    goal = "在率土之滨只发现并冻结今天的每日任务清单，先不要执行清单条目"
    assert is_stzb_discovery_only_goal(goal) is True
    draft = StructuredGoalModel(SimpleNamespace()).specify(goal)
    assert draft.normalized_intent["execution_policy"] == "discovery_only"
    assert [item.criterion_id for item in draft.success_criteria] == [
        "complete_daily_checklist_discovered",
        "daily_items_preserved_without_execution",
    ]
    assert all("完成清单中当前可完成" not in item.description for item in draft.success_criteria)

    prompt = _planner_prompt(PlanContext(
        task_id="task-1", goal=goal, target_id="adb:emulator-5554",
        input_revision=1, owner_inputs=(),
        observation=Observation("frame-1", "fresh Android frame 1280x720"),
        skill_memory=None,
    ))
    assert "This owner goal is discovery_only" in prompt
    assert "do not execute, claim, complete, recruit" in prompt


def test_discovery_only_completion_gate_accepts_a_frozen_bounded_manifest() -> None:
    proposed = GoalCompletionAssessment(
        "verified",
        (
            CriterionAssessment("discover", True, (0,), (1,), "bounded manifest"),
            CriterionAssessment("no_execute", True, (0,), (1,), "navigation only"),
        ),
        ("manifest frozen",),
        "manifest frozen without item execution",
    )
    accepted = _daily_checklist_gate(
        {
            "normalized_intent": {
                "goal_family": STZB_DAILY_GOAL_FAMILY,
                "execution_policy": "discovery_only",
            },
            "success_criteria": [],
        },
        proposed,
        {"state": "FROZEN", "final_verified": False},
    )
    assert accepted == proposed


def test_checklist_requires_distinct_complete_final_reread(tmp_path: Path) -> None:
    store = SQLiteDailyChecklistStore(tmp_path / "daily.db")
    store.record(
        "goal-1",
        _snapshot("frame-1", _item("签到", "completed"), _item("演武", "incomplete")),
    )
    frozen = store.state("goal-1")
    assert frozen["state"] == "FROZEN"
    assert frozen["final_verified"] is False
    assert [item["title"] for item in frozen["remaining_items"]] == ["演武"]

    store.record(
        "goal-1",
        _snapshot("frame-2", _item("签到", "completed"), _item("演武", "completed")),
    )
    final = store.state("goal-1")
    assert final["state"] == "VERIFIED_COMPLETE"
    assert final["final_verified"] is True
    assert final["remaining_items"] == []
    assert final["frozen"]["evidence_id"] != final["latest"]["evidence_id"]


def test_paginated_daily_surface_freezes_only_after_bounded_start_to_end_walk(
    tmp_path: Path,
) -> None:
    store = SQLiteDailyChecklistStore(tmp_path / "daily.db")
    store.record(
        "goal-1",
        DailyChecklistSnapshot(
            "frame-1", "current-daily-cycle", False,
            (_item("登录奖励", "completed"), _item("心愿征程", "incomplete")),
            "2026-08-21T00:00:01+00:00",
            surface_id="activity_carousel", coverage_start=True,
        ),
    )
    assert store.state("goal-1")["state"] == "NOT_DISCOVERED"

    store.record(
        "goal-1",
        DailyChecklistSnapshot(
            "frame-2", "current-daily-cycle", False, (),
            "2026-08-21T00:00:02+00:00",
            surface_id="activity_carousel", coverage_end=True,
        ),
    )
    frozen = store.state("goal-1")
    assert frozen["state"] == "FROZEN"
    assert frozen["frozen"]["evidence_ids"] == ["frame-1", "frame-2"]
    assert [item["title"] for item in frozen["remaining_items"]] == ["心愿征程"]

    store.record(
        "goal-1",
        DailyChecklistSnapshot(
            "frame-3", "current-daily-cycle", False,
            (_item("登录奖励", "completed"), _item("心愿征程", "completed")),
            "2026-08-21T00:00:03+00:00",
            surface_id="activity_carousel", coverage_start=True,
        ),
    )
    store.record(
        "goal-1",
        DailyChecklistSnapshot(
            "frame-4", "current-daily-cycle", False, (),
            "2026-08-21T00:00:04+00:00",
            surface_id="activity_carousel", coverage_end=True,
        ),
    )
    verified = store.state("goal-1")
    assert verified["state"] == "VERIFIED_COMPLETE"
    assert verified["latest"]["evidence_ids"] == ["frame-3", "frame-4"]


def test_multi_surface_manifest_unions_activity_and_patrol_and_is_immutable(
    tmp_path: Path,
) -> None:
    store = SQLiteDailyChecklistStore(tmp_path / "daily.db")
    goal_id = "goal-multi"
    store.register_multi_surface_goal(goal_id)
    login = _item("登录奖励", "incomplete")
    wish = _item("心愿征程", "incomplete")
    patrol = _item("巡察", "incomplete")
    store.record(
        goal_id,
        DailyChecklistSnapshot(
            "discover-left", "current-daily-cycle", False, (login,),
            "2026-08-23T00:00:01+00:00", surface_id="activity_carousel",
            coverage_start=True,
        ),
        phase="discovery",
    )
    store.record(
        goal_id,
        DailyChecklistSnapshot(
            "discover-right", "current-daily-cycle", False, (wish,),
            "2026-08-23T00:00:02+00:00", surface_id="activity_carousel",
            coverage_end=True,
        ),
        phase="discovery",
    )
    store.record(
        goal_id,
        DailyChecklistSnapshot(
            "discover-patrol", "current-daily-cycle", True, (patrol,),
            "2026-08-23T00:00:03+00:00", surface_id="migrated_affairs",
            coverage_start=True, coverage_end=True,
        ),
        phase="discovery",
    )
    for key in (
        "task_major", "task_affairs", "task_reputation", "activity_start",
        "activity_middle", "activity_end", "activity_detail", "patrol",
        f"activity_detail:{login.item_id}", f"activity_detail:{wish.item_id}",
    ):
        store.record_coverage(
            goal_id, key, f"coverage-{key}", phase="discovery"
        )

    frozen = store.freeze_if_complete(goal_id)
    assert frozen["state"] == "FROZEN"
    assert {item["title"] for item in frozen["frozen"]["items"]} == {
        "登录奖励", "心愿征程", "巡察",
    }
    assert set(frozen["frozen"]["activity_item_ids"]) == {
        login.item_id, wish.item_id,
    }

    store.record(
        goal_id,
        DailyChecklistSnapshot(
            "late-discovery", "current-daily-cycle", True,
            (_item("不应漂入冻结清单", "incomplete"),),
            "2026-08-23T00:00:04+00:00", surface_id="migrated_affairs",
            coverage_start=True, coverage_end=True,
        ),
        phase="discovery",
    )
    assert {item["title"] for item in store.state(goal_id)["frozen"]["items"]} == {
        "登录奖励", "心愿征程", "巡察",
    }

    completed_login = _item("登录奖励", "completed")
    completed_wish = _item("心愿征程", "completed")
    completed_patrol = _item("巡察", "completed")
    store.record(
        goal_id,
        DailyChecklistSnapshot(
            "final-left", "current-daily-cycle", False, (completed_login,),
            "2026-08-23T00:01:01+00:00", surface_id="activity_carousel",
            coverage_start=True,
        ),
        phase="final",
    )
    store.record(
        goal_id,
        DailyChecklistSnapshot(
            "final-right", "current-daily-cycle", False, (completed_wish,),
            "2026-08-23T00:01:02+00:00", surface_id="activity_carousel",
            coverage_end=True,
        ),
        phase="final",
    )
    store.record(
        goal_id,
        DailyChecklistSnapshot(
            "final-patrol", "current-daily-cycle", True, (completed_patrol,),
            "2026-08-23T00:01:03+00:00", surface_id="migrated_affairs",
            coverage_start=True, coverage_end=True,
        ),
        phase="final",
    )
    for key in frozen["coverage"]["required"]:
        store.record_coverage(goal_id, key, f"final-{key}", phase="final")

    verified = store.state(goal_id)
    assert verified["state"] == "VERIFIED_COMPLETE"
    assert verified["final_verified"] is True
    assert verified["remaining_items"] == []
    assert verified["frozen"]["evidence_id"] != verified["latest"]["evidence_id"]


def test_progress_controller_expands_only_real_manifest_items_and_guards_finish(
    tmp_path: Path,
) -> None:
    store = SQLiteDailyChecklistStore(tmp_path / "daily.db")
    controller = StzbDailyProgressController(
        store,
        inspect_daily_checklist=lambda goal, observations: (),
        is_execution_stage=lambda value: "执行" in value or "完成冻结清单条目" in value,
    )
    goal_id = "goal-controller"
    task_id = "task-controller"
    goal = "帮我把率土之滨今天的每日任务做完"
    controller.bind(task_id, goal_id, goal)
    login = _item("登录奖励", "incomplete")
    wish = _item("心愿征程", "completed")
    for snapshot in (
        DailyChecklistSnapshot(
            "left", "current-daily-cycle", False, (login,),
            "2026-08-23T00:00:01+00:00", surface_id="activity_carousel",
            coverage_start=True,
        ),
        DailyChecklistSnapshot(
            "right", "current-daily-cycle", False, (wish,),
            "2026-08-23T00:00:02+00:00", surface_id="activity_carousel",
            coverage_end=True,
        ),
    ):
        store.record(goal_id, snapshot, phase="discovery")
    for key in (
        "task_major", "task_affairs", "task_reputation", "activity_start",
        "activity_middle", "activity_end", "activity_detail", "patrol",
    ):
        store.record_coverage(goal_id, key, f"coverage-{key}", phase="discovery")

    manifest_stage = "汇总以上各独立表面，冻结并记录今天完整每日目标清单及覆盖边界"
    plan = TaskPlan(1, (
        Subgoal(0, manifest_stage, "active"),
        Subgoal(1, "执行未知占位条目", "pending"),
    ))
    state = SimpleNamespace(task_id=task_id, plan=plan, active_subgoal_index=0)

    placeholder_plan = TaskPlan(1, (
        Subgoal(
            0,
            "打开轮播中第一个明确每日候选卡片详情，确认其是否含每日/今日身份",
            "active",
        ),
        Subgoal(
            1,
            "打开轮播中第二个明确每日候选卡片详情，确认其是否含每日/今日身份",
            "pending",
        ),
        Subgoal(
            2,
            "打开轮播中第三个明确每日候选卡片详情，确认其是否含每日/今日身份",
            "pending",
        ),
        Subgoal(3, "进入任务面板主要事宜页签，确认其每日身份", "pending"),
    ))
    placeholder_state = SimpleNamespace(
        task_id=task_id, plan=placeholder_plan, active_subgoal_index=0,
    )
    candidate_details = controller.before_subgoal(
        placeholder_state, placeholder_plan.subgoals[0]
    )
    assert candidate_details is not None
    assert candidate_details.kind == "replace_plan"
    assert candidate_details.plan is not None
    assert set(candidate_details.plan.subgoals[:4]) == {
        "在精彩活动轮播中定位并清晰显示每日候选《登录奖励》卡片。",
        "打开每日候选《登录奖励》的可见详情，确认其每日、每天或今日周期机制及当前条目状态。",
        "在精彩活动轮播中定位并清晰显示每日候选《心愿征程》卡片。",
        "打开每日候选《心愿征程》的可见详情，确认其每日、每天或今日周期机制及当前条目状态。",
    }
    assert candidate_details.plan.subgoals[4] == (
        "进入任务面板主要事宜页签，确认其每日身份"
    )
    assert not any("第一个" in item or "第二个" in item or "第三个" in item
                   for item in candidate_details.plan.subgoals)

    detail_directive = controller.before_subgoal(state, plan.subgoals[0])
    assert detail_directive is not None
    assert detail_directive.kind == "replace_plan"
    assert detail_directive.plan is not None
    assert set(detail_directive.plan.subgoals[:4]) == {
        "在精彩活动轮播中定位并清晰显示每日候选《登录奖励》卡片。",
        "打开每日候选《登录奖励》的可见详情，确认其每日、每天或今日周期机制及当前条目状态。",
        "在精彩活动轮播中定位并清晰显示每日候选《心愿征程》卡片。",
        "打开每日候选《心愿征程》的可见详情，确认其每日、每天或今日周期机制及当前条目状态。",
    }

    blocked = controller.before_subgoal(
        state, Subgoal(0, "执行未知占位条目", "active")
    )
    assert blocked is not None
    assert blocked.kind == "fail"
    assert blocked.error_code == "daily_checklist_not_frozen"

    for item in (login, wish):
        store.record_coverage(
            goal_id, f"activity_detail:{item.item_id}",
            f"detail-{item.item_id}", phase="discovery",
        )
        store.record_item_status(
            goal_id, item.item_id, item.status, item.evidence,
            f"detail-{item.item_id}", phase="discovery",
        )
    expanded = controller.before_subgoal(state, plan.subgoals[0])
    assert expanded is not None
    assert expanded.kind == "replace_plan"
    assert expanded.plan is not None
    assert any("《登录奖励》" in item for item in expanded.plan.subgoals)
    assert not any("《心愿征程》" in item and item.startswith("完成冻结清单条目")
                   for item in expanded.plan.subgoals)
    assert not any("第一个" in item or "第二个" in item for item in expanded.plan.subgoals)
    assert expanded.plan.subgoals[-1].find("作为清单完成收尾画面") >= 0

    final_gate = controller.before_subgoal(
        state, Subgoal(0, expanded.plan.subgoals[-1], "active")
    )
    assert final_gate is not None
    assert final_gate.kind == "fail"
    assert final_gate.error_code == "daily_checklist_final_verification_incomplete"


def test_progress_controller_never_accepts_one_activity_boundary_as_complete(
    tmp_path: Path,
) -> None:
    extracted = DailyChecklistSnapshot(
        "left-frame", "current-daily-cycle", True,
        (_item("登录奖励", "incomplete"),),
        "2026-08-23T00:00:01+00:00", surface_id="single_daily_list",
        coverage_start=True, coverage_end=True,
    )
    store = SQLiteDailyChecklistStore(tmp_path / "daily.db")
    controller = StzbDailyProgressController(
        store,
        inspect_daily_checklist=lambda goal, observations: (extracted,),
        is_execution_stage=lambda value: "执行" in value,
    )
    goal_id = "goal-boundary"
    task_id = "task-boundary"
    controller.bind(task_id, goal_id, "帮我把率土之滨今天的每日任务做完")
    subgoal = Subgoal(
        0, "将精彩活动轮播移动到左侧边界并记录明确每日候选", "active"
    )
    state = SimpleNamespace(
        task_id=task_id,
        goal="帮我把率土之滨今天的每日任务做完",
        plan=TaskPlan(1, (subgoal,)),
        active_subgoal_index=0,
    )
    attempt = SimpleNamespace(
        after=Observation("left-frame", "fresh Android frame 1280x720"),
        verification=SimpleNamespace(
            satisfied=True, evidence="左侧边界显示登录奖励，每天登录领取奖励"
        ),
        subgoal_index=0,
        decision=SimpleNamespace(intent=None),
    )

    controller.after_attempt(state, attempt)
    checklist = store.state(goal_id)
    assert checklist["state"] == "NOT_DISCOVERED"
    assert checklist["candidate_manifest"] is None
    assert "activity_start" in checklist["coverage"]["discovered"]
    assert "activity_end" in checklist["coverage"]["missing"]


def test_progress_controller_normalizes_one_carousel_walk_to_current_cycle(
    tmp_path: Path,
) -> None:
    store = SQLiteDailyChecklistStore(tmp_path / "daily.db")
    login = _item("登录奖励", "uncertain")
    login = DailyChecklistItem(
        login.item_id, login.title, login.status, "每天登录领取丰厚奖励"
    )
    wish = _item("心愿征程", "uncertain")
    wish = DailyChecklistItem(
        wish.item_id, wish.title, wish.status, "每日招募可获额外心愿积分"
    )
    snapshots = {
        "left-frame": DailyChecklistSnapshot(
            "left-frame", "uncertain", False, (login,),
            "2026-08-23T00:00:01+00:00",
        ),
        "middle-frame": DailyChecklistSnapshot(
            "middle-frame", "current-daily-cycle", False,
            (wish,),
            "2026-08-23T00:00:02+00:00",
        ),
        "right-frame": DailyChecklistSnapshot(
            "right-frame", "uncertain", False, (),
            "2026-08-23T00:00:03+00:00",
        ),
    }
    controller = StzbDailyProgressController(
        store,
        inspect_daily_checklist=lambda goal, observations: (
            snapshots[observations[0].evidence_id],
        ),
        is_execution_stage=lambda value: "执行" in value,
    )
    goal_id = "goal-cycle"
    task_id = "task-cycle"
    goal = "帮我把率土之滨今天的每日任务做完"
    controller.bind(task_id, goal_id, goal)
    subgoals = (
        Subgoal(0, "精彩活动轮播停留在最左侧边界", "completed"),
        Subgoal(1, "精彩活动轮播滑动到有重叠的中间视口", "completed"),
        Subgoal(2, "精彩活动轮播停留在最右侧边界", "completed"),
    )
    state = SimpleNamespace(
        task_id=task_id, goal=goal, plan=TaskPlan(1, subgoals),
        active_subgoal_index=2,
    )
    for index, evidence_id in enumerate(snapshots):
        attempt = SimpleNamespace(
            after=Observation(evidence_id, "fresh Android frame 1280x720"),
            verification=SimpleNamespace(
                satisfied=True,
                evidence="活动轮播画面明确显示每天登录或每日招募候选",
            ),
            subgoal_index=index,
            decision=SimpleNamespace(intent=None),
        )
        controller.after_attempt(state, attempt)

    checklist = store.state(goal_id)
    assert checklist["candidate_manifest"] is not None
    assert checklist["candidate_manifest"]["date_label"] == "current-daily-cycle"
    assert {item["title"] for item in checklist["candidate_manifest"]["items"]} == {
        "登录奖励", "心愿征程",
    }


def test_activity_candidate_filter_requires_positive_cycle_identity() -> None:
    assert _is_explicit_activity_daily_item(DailyChecklistItem(
        "spurious", "首充豪礼 送五星【太史慈】", "uncertain",
        "卡片可见，但未显示每日/每天/今日周期文案。",
    )) is False
    assert _is_explicit_activity_daily_item(DailyChecklistItem(
        "tomorrow", "学习第三战法输出翻倍 次日登录立得", "uncertain",
        "次日登录可领取奖励，未出现每日身份。",
    )) is False
    assert _is_explicit_activity_daily_item(DailyChecklistItem(
        "login", "登录奖励", "incomplete", "每天登录领取丰厚奖励，今日未领取。",
    )) is True
    assert _is_explicit_activity_daily_item(DailyChecklistItem(
        "wish", "心愿征程", "incomplete", "每日招募可获得额外心愿积分，当前0/4。",
    )) is True
    canonical = _canonical_activity_item(DailyChecklistItem(
        "verbose", "登录奖励：每天登录领取丰厚奖励", "incomplete",
        "每天登录领取丰厚奖励。",
    ))
    assert canonical.title == "登录奖励"
    assert canonical.item_id == _item("登录奖励", "incomplete").item_id


def test_checklist_rejects_item_set_drift_and_completion_gate_stays_partial(
    tmp_path: Path,
) -> None:
    store = SQLiteDailyChecklistStore(tmp_path / "daily.db")
    store.record("goal-1", _snapshot("frame-1", _item("签到", "completed")))
    store.record(
        "goal-1",
        _snapshot("frame-2", _item("签到", "completed"), _item("演武", "completed")),
    )
    state = store.state("goal-1")
    assert state["final_verified"] is False
    assert state["deviation"] is not None

    assessment = _daily_checklist_gate(
        {
            "normalized_intent": {"goal_family": STZB_DAILY_GOAL_FAMILY},
            "success_criteria": [
                {
                    "id": "daily_done",
                    "description": "完成今日每日任务",
                    "source_quote": "每日任务做完",
                    "evidence_requirement": "完整清单复查",
                }
            ],
        },
        GoalCompletionAssessment(
            "verified",
            (CriterionAssessment("daily_done", True, (0,), (1,), "模型声称完成"),),
            ("全部完成",),
            "全部完成",
        ),
        state,
    )
    assert assessment.verdict == "partial"
    assert assessment.criteria[0].satisfied is False
    assert assessment.verified_facts == ()


def test_checklist_missing_frozen_item_is_reported_uncertain(tmp_path: Path) -> None:
    store = SQLiteDailyChecklistStore(tmp_path / "daily.db")
    store.record(
        "goal-1",
        _snapshot("frame-1", _item("签到", "completed"), _item("演武", "incomplete")),
    )
    store.record("goal-1", _snapshot("frame-2", _item("签到", "completed")))
    state = store.state("goal-1")
    assert state["state"] == "FROZEN"
    assert state["final_verified"] is False
    assert state["deviation"] is not None
    assert state["remaining_items"] == [{
        "item_id": _item("演武", "incomplete").item_id,
        "title": "演武",
        "status": "uncertain",
        "evidence": "item missing from the latest complete-view claim",
    }]


def test_structured_qwen_extracts_only_input_bound_checklist_frames(tmp_path: Path) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record(
        "task-1", AndroidScreenshot(b"\x89PNG\r\n\x1a\nchecklist", width=100, height=200)
    )
    calls = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        calls.append(payload)
        arguments = {
            "snapshots": [
                {
                    "image_index": 0,
                    "date_label": "今日",
                    "coverage_complete": True,
                    "surface_id": "single_daily_list",
                    "coverage_start": True,
                    "coverage_end": True,
                    "items": [
                        {"title": "签到", "status": "completed", "evidence": "已领取"},
                        {"title": "演武", "status": "incomplete", "evidence": "0/1"},
                    ],
                }
            ]
        }
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_daily_checklist",
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }}]}}]}

    model = StructuredGoalModel(
        OpenAICompatibleToolRoleModel(
            endpoint="http://127.0.0.1:8080/v1/chat/completions",
            model="qwen3.8-27b",
            evidence=evidence,
            transport=transport,
        )
    )
    snapshots = model.inspect_daily_checklist("率土之滨今日每日任务", (frame,))
    assert snapshots[0].evidence_id == frame.evidence_id
    assert snapshots[0].surface_id == "single_daily_list"
    assert [item.status for item in snapshots[0].items] == ["completed", "incomplete"]
    assert calls[0]["tools"][0]["function"]["name"] == "record_daily_checklist"
    assert calls[0]["tool_choice"] == "required"


def test_stzb_planner_requires_discovery_and_final_reread() -> None:
    prompt = _planner_prompt(
        PlanContext(
            task_id="task-1",
            goal="帮我把率土之滨今天的每日任务做完",
            target_id="adb:emulator-5554",
            input_revision=1,
            owner_inputs=(),
            observation=Observation("frame-1", "fresh Android frame 1080x1920"),
            skill_memory=None,
        )
    )
    assert "complete daily checklist" in prompt
    assert "finally reopen and visibly reread" in prompt
    assert "quick task strip with one tracked objective is navigation only" in prompt
    assert "daily target surface" in prompt
    assert "rename the former daily-affairs page to 事务 or move it to 巡察" in prompt
    assert "Never treat the 事务 label alone as daily identity" in prompt
    assert "may span several explicit daily cards or surfaces" in prompt
    assert "leave the exhausted task surface" in prompt
    assert "visibly labeled activity/daily entry" in prompt
    assert "Do not invent task-specific coordinates" in prompt


def test_stzb_executor_keeps_verified_quick_strip_boundary_in_session() -> None:
    intent = SimpleNamespace(
        name="tap",
        arguments={"x": 45, "y": 40, "target_description": "左上角任务卷轴"},
    )
    attempt = SimpleNamespace(
        sequence=9,
        decision=SimpleNamespace(intent=intent, kind="act"),
        before=Observation("before-9", "fresh Android frame 1280x720"),
        transport=SimpleNamespace(status="accepted"),
        verification=SimpleNamespace(
            satisfied=False,
            progress=True,
            evidence="只打开了左侧单条追踪任务和武将行，不是完整每日清单",
        ),
    )
    context = SimpleNamespace(
        goal="帮我把率土之滨今天的每日任务做完",
        subgoal=SimpleNamespace(description="显示完整每日任务清单及状态"),
        strategy="inspect visible task affordances",
        consecutive_no_progress=0,
        owner_inputs=(),
        skill_memory=None,
        experience_hints=(),
        recent_attempts=(attempt,),
    )
    prompt = _executor_prompt(context)
    assert "target=左上角任务卷轴" in prompt
    assert "verifier=只打开了左侧单条追踪任务和武将行" in prompt
    assert "only a quick task strip, never the complete daily checklist" in prompt
    assert "do not repeat it while the strip is visible" in prompt

    reflection = _reflection_prompt(SimpleNamespace(
        **context.__dict__,
        input_revision=0,
    ))
    assert "tap:左上角任务卷轴" in reflection
    assert "Exhausting the visible tabs of one task surface is not permission" in reflection
    assert "visible Back/Close control" in reflection

    verification = _stzb_daily_verification_instruction(
        context.goal, context.subgoal.description
    )
    assert "可以算 progress" in verification
    assert "当前子目标明确要求完整清单" in verification
    assert "才可 satisfied" in verification


def test_stzb_verdict_guard_rejects_main_quest_page_as_daily_identity() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天的每日任务做完",
        "进入并完整查看今日每日任务清单页面",
        "satisfied",
        (
            "画面顶部有名望/主要事宜/事务标签，右侧列出占领土地、升级仓库等"
            "多条任务及进度，符合完整每日任务清单要求。"
        ),
    )
    assert verdict == "progress"
    assert "no visible daily/today/activity category identity" in evidence

    verified, unchanged = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天的每日任务做完",
        "进入并完整查看今日每日任务清单页面",
        "satisfied",
        "当前页签为“每日任务”，可见多个条目及各自完成状态。",
    )
    assert verified == "satisfied"
    assert unchanged == "当前页签为“每日任务”，可见多个条目及各自完成状态。"

    negative_check, negative_evidence = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天的每日任务做完",
        "确认顶部三个标签均非每日任务，判定当前界面不是今日每日任务页",
        "satisfied",
        "可见标签只有名望、主要事宜、事务，未见每日身份。",
    )
    assert negative_check == "satisfied"
    assert negative_evidence == "可见标签只有名望、主要事宜、事务，未见每日身份。"

    negative_synonym, _ = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天的每日任务做完",
        "确认这是任务总入口而非单一每日清单",
        "satisfied",
        "当前是任务总入口，未执行任何条目。",
    )
    assert negative_synonym == "satisfied"

    migrated, migrated_evidence = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天的每日任务做完",
        "进入并完整查看今日每日任务清单页面",
        "satisfied",
        "当前为任务-事务页面，巡察说明写明每天上午8点增加1次巡察次数。",
    )
    assert migrated == "satisfied"
    assert migrated_evidence == "当前为任务-事务页面，巡察说明写明每天上午8点增加1次巡察次数。"


def test_resettable_stzb_matrix_meets_u5_learning_thresholds() -> None:
    report = run_stzb_daily_fixture_benchmark()
    assert len(report.trials) == 5
    assert {item.popup_present for item in report.trials} == {False, True}
    assert {item.anchor_variant for item in report.trials} == {"baseline", "shifted"}
    assert any(item.wrong_scene_present for item in report.trials)
    assert any(item.no_effect_present for item in report.trials)
    assert report.cold_actions == 44
    assert report.warm_actions == 26
    assert report.action_reduction > 0.40
    assert report.wrong_scene_recovery_rate == 1.0
    assert report.known_wrong_repeat_rate == 0.0
    assert report.stale_policy_rolled_back is True
    assert report.cross_scope_leakage == 0
    assert report.passed is True


def test_checklist_visual_extraction_never_competes_with_active_phone_worker() -> None:
    calls = []

    class ChecklistStore:
        def state(self, goal_id):
            return {"goal_id": goal_id, "state": "NOT_DISCOVERED", "final_verified": False}

    service = GoalService(
        SimpleNamespace(),
        mobile_runtime=None,
        mobile_archive=None,
        configured_serial=None,
        daily_checklist_store=ChecklistStore(),
        inspect_daily_checklist=lambda goal, observations: calls.append((goal, observations)),
    )
    record = GoalRecord(
        "goal-1", "率土每日任务", "RUNNING", "AUTOMATED", None, None,
        "mobile_task_compat", "BOUND", "task-1", "adb:emulator-5554",
        None, None, None, "2026-08-21T00:00:00Z", "2026-08-21T00:00:00Z", None,
    )
    state = service._reconcile_daily_checklist(
        record,
        {"normalized_intent": {"goal_family": STZB_DAILY_GOAL_FAMILY}},
        SimpleNamespace(status="running", attempts=(SimpleNamespace(after=Observation(
            "frame-1", "fresh Android frame 1080x1920"
        )),)),
    )
    assert state["state"] == "NOT_DISCOVERED"
    assert calls == []


def test_uncertain_goal_projection_does_not_retry_checklist_visual_extraction() -> None:
    calls = []
    specification = {
        "normalized_intent": {"goal_family": STZB_DAILY_GOAL_FAMILY},
        "success_criteria": [],
    }

    class ChecklistStore:
        def state(self, goal_id):
            return {
                "goal_id": goal_id,
                "state": "NOT_DISCOVERED",
                "final_verified": False,
            }

    record = GoalRecord(
        "goal-1", "率土每日任务", "UNCERTAIN", "AUTOMATED", None, None,
        "mobile_task_compat", "BOUND", "task-1", "adb:emulator-5554",
        None, None, None, "2026-08-21T00:00:00Z", "2026-08-21T00:00:00Z",
        "2026-08-21T00:01:00Z",
    )
    store = SimpleNamespace(
        sync_mobile_projection=lambda goal_id, source: record,
        completion=lambda goal_id: None,
        specification=lambda goal_id: specification,
    )
    service = GoalService(
        store,
        mobile_runtime=None,
        mobile_archive=SimpleNamespace(inspect=lambda task_id: SimpleNamespace(
            status="uncertain",
            attempts=(SimpleNamespace(after=Observation(
                "frame-1", "fresh Android frame 1280x720"
            )),),
        )),
        configured_serial=None,
        daily_checklist_store=ChecklistStore(),
        inspect_daily_checklist=lambda goal, observations: calls.append(
            (goal, observations)
        ),
    )

    assert service._project(record) == record
    assert calls == []


def test_checklist_candidate_filter_excludes_negative_task_panel_mentions() -> None:
    assert _looks_like_daily_checklist_evidence(
        "左侧已展开任务面板，显示每日任务条目及当前完成状态"
    ) is True
    assert _looks_like_daily_checklist_evidence(
        "左上角任务入口可见，但未弹出任务面板，子目标未达成"
    ) is False
    assert _looks_like_daily_checklist_evidence(
        "桃源合合详情写明每日进入活动和提升繁荣度可获得积分"
    ) is True
