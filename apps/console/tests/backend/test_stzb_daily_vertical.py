from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ai_game_console.goal_families import (
    STZB_DAILY_GOAL_FAMILY,
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
)
from ai_game_console.goal_runtime.stzb_daily_benchmark import (
    run_stzb_daily_fixture_benchmark,
)
from ai_game_console.mobile_agent import Observation, PlanContext
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
    assert "independent full daily-task page" in prompt
    assert "Do not reinterpret main-story, reputation, or generic affairs" in prompt
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
    assert "绝不能算 satisfied" in verification


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
