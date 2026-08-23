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


def test_structured_goal_model_classifies_long_lived_and_answers_language_only(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    calls = []
    system_prompts = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]
        calls.append(tool)
        system_prompts.append(payload["messages"][0]["content"])
        if tool["name"] == "record_goal_specification":
            arguments = {
                "classification": "long_lived_application_goal",
                "outcome": "持续认识适合长期相处的人",
                "success_criteria": [
                    {
                        "id": "continue_until_stop",
                        "description": "持续运行直到用户明确停止",
                        "evidence_requirement": "长期实例与控制证据",
                        "source_quote": "持续认识适合长期相处的人",
                    }
                ],
            }
        else:
            arguments = {"result": "本地语言结果"}
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool["name"],
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }}]}}]}

    model = StructuredGoalModel(OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen3.8-27b",
        evidence=evidence,
        transport=transport,
    ))
    specification = model.specify("持续认识适合长期相处的人")
    result = model.answer_language("把这句话改得更简洁")

    assert specification.normalized_intent["classification"] == (
        "long_lived_application_goal"
    )
    assert calls[0]["parameters"]["properties"]["classification"]["enum"] == [
        "finite_phone_goal",
        "long_lived_local_goal",
        "long_lived_application_goal",
        "language_only_goal",
    ]
    assert "分类只描述目标生命周期和环境" in system_prompts[0]
    assert "不得选择或臆造 owner、profile 或账号门禁" in system_prompts[0]
    assert "soul-reply-v1" not in system_prompts[0]
    assert "external_owner.soul" not in system_prompts[0]
    assert result == "本地语言结果"


def test_mobile_application_readiness_requires_goal_match_and_login_frame(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record(
        "application-cycle-1",
        AndroidScreenshot(PNG, width=100, height=200),
    )
    observed = {}

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        observed.update(payload)
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "record_application_readiness",
                                    "arguments": json.dumps(
                                        {
                                            "ready": True,
                                            "authenticated": True,
                                            "application_matches_goal": True,
                                            "blocking_state": "none",
                                            "evidence": "Soul 登录后的会话界面可交互",
                                        },
                                        ensure_ascii=False,
                                    ),
                                }
                            }
                        ]
                    }
                }
            ]
        }

    model = StructuredGoalModel(
        OpenAICompatibleToolRoleModel(
            endpoint="http://127.0.0.1:8080/v1/chat/completions",
            model="qwen3.8-27b",
            evidence=evidence,
            transport=transport,
        )
    )
    verdict = model.assess_mobile_application_readiness(
        "在 Soul 中持续匹配和聊天",
        frame,
        "cn.soulapp.android",
    )

    assert verdict == {
        "ready": True,
        "authenticated": True,
        "application_matches_goal": True,
        "blocking_state": "none",
        "evidence": "Soul 登录后的会话界面可交互",
    }
    tool = observed["tools"][0]["function"]
    assert tool["name"] == "record_application_readiness"
    assert "application_matches_goal" in tool["parameters"]["required"]
    assert observed["tool_choice"] == "required"
    assert any(
        item["type"] == "image_url"
        for item in observed["messages"][1]["content"]
    )
