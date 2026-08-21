from __future__ import annotations

import json
from pathlib import Path

from ai_game_console.execution import AndroidScreenshot
from ai_game_console.goal_runtime import StructuredGoalModel
from ai_game_console.mobile_task_adapter import (
    LocalMobileEvidenceStore,
    OpenAICompatibleToolRoleModel,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"goal-qwen-frame"


def test_structured_goal_model_forces_specification_and_completion_tools(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, width=100, height=200))
    calls = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]["name"]
        calls.append(payload)
        arguments = {
            "record_goal_specification": {
                "classification": "finite_phone_goal",
                "outcome": "读取电池并返回桌面",
                "success_criteria": [
                    {
                        "id": "battery_visible",
                        "description": "当前电池信息可见",
                        "evidence_requirement": "电池页新鲜画面",
                        "source_quote": "查看当前可见的电池信息",
                    }
                ],
            },
            "record_goal_completion": {
                "verdict": "verified",
                "criteria": [
                    {
                        "criterion_id": "battery_visible",
                        "satisfied": True,
                        "subgoal_indices": [0],
                        "attempt_sequences": [1],
                        "evidence": "电池页显示 73%",
                    }
                ],
                "verified_facts": ["电量 73%"],
                "result_summary": "当前电量 73%。",
            },
        }[tool]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool, "arguments": json.dumps(arguments, ensure_ascii=False),
        }}]}}]}

    role_model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen3.8-27b",
        evidence=evidence,
        transport=transport,
    )
    model = StructuredGoalModel(role_model)
    specification = model.specify("查看当前可见的电池信息")
    state = {
        "plan": {"subgoals": [{"index": 0, "description": "查看电池"}]},
        "attempts": [{
            "sequence": 1,
            "subgoal_index": 0,
            "after": frame,
            "verification": {
                "satisfied": True,
                "progress": True,
                "uncertain": False,
                "evidence": "电池页显示 73%",
            },
        }],
    }
    assessment = model.verify(
        "查看当前可见的电池信息",
        {
            "revision": 2,
            "original_goal": "查看当前可见的电池信息",
            "normalized_intent": specification.normalized_intent,
            "success_criteria": [{
                "id": "battery_visible",
                "description": "当前电池信息可见",
                "evidence_requirement": "电池页新鲜画面",
                "source_quote": "查看当前可见的电池信息",
            }],
        },
        state,
    )

    assert specification.success_criteria[0].source_quote == "查看当前可见的电池信息"
    assert assessment.verdict == "verified"
    assert assessment.criteria[0].attempt_sequences == (1,)
    assert [call["tools"][0]["function"]["name"] for call in calls] == [
        "record_goal_specification", "record_goal_completion",
    ]
    assert all(call["tool_choice"] == "required" for call in calls)
    completion_content = calls[1]["messages"][1]["content"]
    assert sum(item["type"] == "image_url" for item in completion_content) == 0
    assert frame.evidence_id in completion_content[0]["text"]


def test_stzb_daily_specification_is_frozen_without_model_availability(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    calls = []
    role_model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen3.8-27b",
        evidence=evidence,
        transport=lambda *args: calls.append(args),
    )
    original = "在率土之滨完成今天所有可见的每日任务，并以完整清单复查为准"

    specification = StructuredGoalModel(role_model).specify(original)

    assert specification.normalized_intent["goal_family"] == "stzb/daily/vnext"
    assert tuple(item.criterion_id for item in specification.success_criteria) == (
        "complete_daily_checklist_discovered",
        "all_feasible_daily_items_completed",
        "daily_checklist_independently_reread",
    )
    assert all(
        item.source_quote == original for item in specification.success_criteria
    )
    assert calls == []
