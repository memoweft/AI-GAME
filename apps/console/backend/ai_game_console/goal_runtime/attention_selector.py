from __future__ import annotations

import json
from typing import Any

from ..agent_runtime.domain import AttentionSelectorKind, SliceBudgetDraft
from ..agent_runtime.scheduler import (
    AttentionScheduler,
    AttentionSelectionContext,
    AttentionSelectionDraft,
    TransportFallbackAttentionSelector,
)
from ..mobile_task_adapter import MobileTaskAdapterError, OpenAICompatibleToolRoleModel


class QwenAttentionSelector:
    """Forced-tool Qwen role for the scheduler's already bounded shortlist.

    Eligibility, hard tiers, scoring and the top-100 shortlist remain owned by
    ``AttentionScheduler``.  This adapter cannot create a Goal, wake a Goal or
    select an id that was not supplied by that shortlist.
    """

    SCHEMA_VERSION = "r3.attention-selection.v1"

    def __init__(self, role_model: OpenAICompatibleToolRoleModel) -> None:
        self._role_model = role_model

    def select(self, context: AttentionSelectionContext) -> AttentionSelectionDraft:
        if not context.candidates:
            raise ValueError("Qwen attention selector requires a non-empty shortlist")
        candidate_ids = tuple(item.goal_id for item in context.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Qwen attention shortlist contains duplicate Goal ids")
        details = {item.goal_id: item for item in context.candidate_details}
        if set(details) != set(candidate_ids):
            raise ValueError("Qwen attention candidate details do not match shortlist")

        try:
            decoded = self._role_model.call_tool(
                system=(
                    "你是 AgentSession 的 Attention final selector。必须调用 "
                    "record_attention_selection。候选已经由确定性 eligibility、hard tier、"
                    "公平性和 backoff 围栏筛选；只能从输入 shortlist 精确选择一个 Goal ID。"
                    "不得创建或修改 Goal，不得解除等待，不得执行设备动作，也不得选择输入外 ID。"
                    "reason 要用日常语言解释为何现在选择它。预算只是下一 ActivitySlice 草案："
                    "时间 180000 到 600000 毫秒，动作 1 到 50，checkpoint policy 固定。"
                ),
                prompt=json.dumps(
                    {
                        "schema_version": self.SCHEMA_VERSION,
                        "session": {
                            "id": context.session_id,
                            "original_instruction": context.original_instruction,
                            "authority_revision": context.authority_revision,
                            "trigger_key": context.trigger_key,
                            "trigger_summary": context.trigger_summary,
                            "current_time": context.current_time,
                            "active_goal_id": context.active_goal_id,
                            "current_application_hint": context.current_application_hint,
                        },
                        "shortlist": [
                            {
                                "goal_id": candidate.goal_id,
                                "title": details[candidate.goal_id].title,
                                "original_fragment": details[candidate.goal_id].original_fragment,
                                "application_hint": details[candidate.goal_id].application_hint,
                                "progress_summary": details[candidate.goal_id].progress_summary,
                                "continuation_summary": details[candidate.goal_id].continuation_summary,
                                "hard_tier": candidate.hard_tier,
                                "rank": candidate.rank,
                                "total_score": candidate.total_score,
                                "components": {
                                    "user_priority": candidate.user_priority_component,
                                    "event_urgency": candidate.event_urgency_component,
                                    "waiting_age": candidate.waiting_age_component,
                                    "starvation": candidate.starvation_component,
                                    "continuity": candidate.continuity_component,
                                    "app_switch_cost": candidate.app_switch_cost,
                                    "backoff": candidate.backoff_component,
                                    "recent_failure": candidate.recent_failure_component,
                                },
                            }
                            for candidate in context.candidates
                        ],
                    },
                    ensure_ascii=False,
                ),
                observations=(),
                tool_name="record_attention_selection",
                description="Select one Goal from the scheduler-bounded shortlist",
                parameters=_attention_selection_schema(candidate_ids),
                max_tokens=768,
                reasoning_effort="low",
                reasoning_budget=0,
            )
        except MobileTaskAdapterError as error:
            if error.code == "mobile_role_invalid_response":
                raise ValueError("invalid Qwen attention forced-tool response") from error
            raise
        return _selection_from_decoded(decoded, allowed_goal_ids=frozenset(candidate_ids))


def build_qwen_attention_scheduler(
    role_model: OpenAICompatibleToolRoleModel,
) -> AttentionScheduler:
    """Normal composition: Qwen final choice with transport-only fallback."""

    return AttentionScheduler(
        TransportFallbackAttentionSelector(
            QwenAttentionSelector(role_model),
            transport_errors=(MobileTaskAdapterError,),
        )
    )


def _attention_selection_schema(candidate_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": [QwenAttentionSelector.SCHEMA_VERSION],
            },
            "selected_goal_id": {"type": "string", "enum": list(candidate_ids)},
            "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "slice_time_budget_ms": {
                "type": "integer",
                "minimum": 180_000,
                "maximum": 600_000,
            },
            "slice_action_budget": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
            },
            "checkpoint_policy": {
                "type": "string",
                "enum": ["VERIFIED_CHECKPOINT_ONLY"],
            },
        },
        "required": [
            "schema_version",
            "selected_goal_id",
            "reason",
            "slice_time_budget_ms",
            "slice_action_budget",
            "checkpoint_policy",
        ],
    }


def _selection_from_decoded(
    decoded: dict[str, Any], *, allowed_goal_ids: frozenset[str]
) -> AttentionSelectionDraft:
    required = {
        "schema_version",
        "selected_goal_id",
        "reason",
        "slice_time_budget_ms",
        "slice_action_budget",
        "checkpoint_policy",
    }
    if not isinstance(decoded, dict) or set(decoded) != required:
        raise ValueError("invalid Qwen attention selection fields")
    if decoded.get("schema_version") != QwenAttentionSelector.SCHEMA_VERSION:
        raise ValueError("invalid Qwen attention selection schema version")
    selected_goal_id = decoded.get("selected_goal_id")
    if not isinstance(selected_goal_id, str) or selected_goal_id not in allowed_goal_ids:
        raise ValueError("Qwen attention selection is outside the scheduler shortlist")
    reason = decoded.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1_000:
        raise ValueError("invalid Qwen attention selection reason")
    time_budget = decoded.get("slice_time_budget_ms")
    action_budget = decoded.get("slice_action_budget")
    if isinstance(time_budget, bool) or not isinstance(time_budget, int):
        raise ValueError("invalid Qwen attention time budget")
    if isinstance(action_budget, bool) or not isinstance(action_budget, int):
        raise ValueError("invalid Qwen attention action budget")
    checkpoint_policy = decoded.get("checkpoint_policy")
    if checkpoint_policy != "VERIFIED_CHECKPOINT_ONLY":
        raise ValueError("invalid Qwen attention checkpoint policy")
    budget = SliceBudgetDraft(
        time_budget_ms=time_budget,
        action_budget=action_budget,
        checkpoint_policy=checkpoint_policy,
    )
    return AttentionSelectionDraft(
        selected_goal_id=selected_goal_id,
        reason=reason.strip(),
        selector_kind=AttentionSelectorKind.QWEN,
        slice_budget=budget,
    )
