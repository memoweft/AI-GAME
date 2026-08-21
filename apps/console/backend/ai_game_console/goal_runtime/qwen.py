from __future__ import annotations

import json
import re
from typing import Any

from ..mobile_agent.domain import Observation
from ..mobile_task_adapter import OpenAICompatibleToolRoleModel
from .domain import (
    CriterionAssessment,
    GoalCompletionAssessment,
    GoalSpecificationDraft,
    SuccessCriterion,
)


class StructuredGoalModel:
    """Qwen goal-level roles over the same provider-neutral forced-tool transport."""

    def __init__(self, role_model: OpenAICompatibleToolRoleModel) -> None:
        self._role_model = role_model

    def specify(self, original_goal: str) -> GoalSpecificationDraft:
        decoded = self._role_model.call_tool(
            system=(
                "你是独立目标规格层。必须调用 record_goal_specification。原始目标不可修改。"
                "把每个并列、顺序和结果要求分别变成可验证成功标准；不得因为执行困难而省略。"
                "每个标准的 source_quote 必须逐字摘自原始目标，并使用稳定英文小写 id。"
            ),
            prompt=f"原始目标：{original_goal}",
            observations=(),
            tool_name="record_goal_specification",
            description="Freeze the complete owner goal before device execution",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "classification": {"type": "string"},
                    "outcome": {"type": "string"},
                    "success_criteria": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 24,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "evidence_requirement": {"type": "string"},
                                "source_quote": {"type": "string"},
                            },
                            "required": [
                                "id", "description", "evidence_requirement", "source_quote"
                            ],
                        },
                    },
                },
                "required": ["classification", "outcome", "success_criteria"],
            },
            max_tokens=768,
            # This extraction task is schema-constrained and does not benefit
            # from consuming the whole response budget in hidden reasoning.
            # The final verifier uses the same stable non-thinking baseline.
            reasoning_effort="minimal",
            reasoning_budget=0,
        )
        classification = decoded.get("classification")
        outcome = decoded.get("outcome")
        raw_criteria = decoded.get("success_criteria")
        if (
            not isinstance(classification, str)
            or not classification.strip()
            or not isinstance(outcome, str)
            or not outcome.strip()
            or not isinstance(raw_criteria, list)
        ):
            raise ValueError("invalid goal specification response")
        criteria = []
        for item in raw_criteria:
            if not isinstance(item, dict):
                raise ValueError("invalid goal criterion")
            identifier = str(item.get("id") or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", identifier):
                raise ValueError("invalid goal criterion id")
            criteria.append(SuccessCriterion(
                identifier,
                str(item.get("description") or "").strip(),
                str(item.get("evidence_requirement") or "").strip(),
                str(item.get("source_quote") or "").strip(),
            ))
        return GoalSpecificationDraft(
            {"classification": classification.strip(), "outcome": outcome.strip()},
            tuple(criteria),
        )

    def verify(
        self, original_goal: str, specification: dict[str, Any], state: Any
    ) -> GoalCompletionAssessment:
        attempts = tuple(_value(state, "attempts", ()))
        eligible = []
        for attempt in attempts:
            verification = _value(attempt, "verification")
            after = _value(attempt, "after")
            if (
                verification is not None
                and bool(_value(verification, "satisfied", False))
                and not bool(_value(verification, "uncertain", False))
                and after is not None
            ):
                eligible.append({
                    "sequence": int(_value(attempt, "sequence", 0)),
                    "subgoal_index": int(_value(attempt, "subgoal_index", -1)),
                    "verification_evidence": str(_value(verification, "evidence", "")),
                    "after_evidence_id": str(_value(after, "evidence_id", "")),
                })
        if not eligible:
            raise ValueError("goal completion has no eligible verified after evidence")
        plan = _value(state, "plan")
        plan_payload = [
            {
                "index": int(_value(item, "index", -1)),
                "description": str(_value(item, "description", "")),
            }
            for item in _value(plan, "subgoals", ())
        ] if plan is not None else []
        prompt = json.dumps({
            "original_goal": original_goal,
            "frozen_specification": specification,
            "generated_plan": plan_payload,
            "eligible_verified_attempts": eligible,
            "instruction": (
                "逐项判断冻结标准。satisfied 只能引用上面存在的 subgoal index 和 attempt "
                "sequence。每条记录都来自已持久化的新鲜动作后画面验证；最终层不得重新发明画面事实。"
                "计划完成本身不是目标完成。结果摘要只能使用已验证事实。对于告知、报告或"
                "回复用户的标准，本次 GoalRun 最终响应就是交付载体：只要它能完全由某条"
                "eligible_verified_attempt 的事实生成，就引用该事实的阶段和尝试并判定；"
                "不要要求在最终响应之前已经存在另一条用户回复。"
            ),
        }, ensure_ascii=False)
        decoded = self._role_model.call_tool(
            system=(
                "你是独立 Goal Completion Verifier，不是规划器。必须调用 "
                "record_goal_completion。不得相信传输成功或任务 completed 状态；只能使用冻结标准、"
                "已验证尝试事实。任何标准缺证据时 verdict 不能是 verified。"
            ),
            prompt=prompt,
            observations=(),
            tool_name="record_goal_completion",
            description="Assess every frozen criterion against admissible attempt evidence",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "verdict": {"type": "string", "enum": ["verified", "partial", "uncertain"]},
                    "criteria": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "criterion_id": {"type": "string"},
                                "satisfied": {"type": "boolean"},
                                "subgoal_indices": {"type": "array", "items": {"type": "integer"}},
                                "attempt_sequences": {"type": "array", "items": {"type": "integer"}},
                                "evidence": {"type": "string"},
                            },
                            "required": [
                                "criterion_id", "satisfied", "subgoal_indices",
                                "attempt_sequences", "evidence"
                            ],
                        },
                    },
                    "verified_facts": {"type": "array", "items": {"type": "string"}},
                    "result_summary": {"type": "string"},
                },
                "required": ["verdict", "criteria", "verified_facts", "result_summary"],
            },
            max_tokens=2_048,
            reasoning_effort="minimal",
            reasoning_budget=0,
        )
        criteria = tuple(
            CriterionAssessment(
                criterion_id=str(item.get("criterion_id") or ""),
                satisfied=bool(item.get("satisfied")),
                subgoal_indices=tuple(int(value) for value in item.get("subgoal_indices", ())),
                attempt_sequences=tuple(int(value) for value in item.get("attempt_sequences", ())),
                evidence=str(item.get("evidence") or ""),
            )
            for item in decoded.get("criteria", ())
            if isinstance(item, dict)
        )
        facts = decoded.get("verified_facts")
        return GoalCompletionAssessment(
            verdict=str(decoded.get("verdict") or "uncertain"),
            criteria=criteria,
            verified_facts=tuple(str(item).strip() for item in facts)
            if isinstance(facts, list) else (),
            result_summary=str(decoded.get("result_summary") or "").strip(),
        )


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)
