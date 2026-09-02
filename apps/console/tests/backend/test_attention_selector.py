from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ai_game_console.agent_runtime.agenda import AgendaContext, AgendaGoalSnapshot
from ai_game_console.agent_runtime.domain import AttentionSelectorKind, GoalNodeStatus
from ai_game_console.agent_runtime.scheduler import (
    AttentionScheduler,
    TransportFallbackAttentionSelector,
)
from ai_game_console.goal_runtime.attention_selector import (
    QwenAttentionSelector,
    build_qwen_attention_scheduler,
)
from ai_game_console.mobile_task_adapter import MobileTaskAdapterError


NOW = datetime(2026, 8, 24, 7, 0, tzinfo=UTC)


class _RoleModel:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def call_tool(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _decoded(goal_id: str = "A", **changes):
    result = {
        "schema_version": QwenAttentionSelector.SCHEMA_VERSION,
        "selected_goal_id": goal_id,
        "reason": "新指令与当前进度都支持先处理这个目标",
        "slice_time_budget_ms": 360_000,
        "slice_action_budget": 15,
        "checkpoint_policy": "VERIFIED_CHECKPOINT_ONLY",
    }
    result.update(changes)
    return result


def _goal(goal_id: str, priority: int) -> AgendaGoalSnapshot:
    return AgendaGoalSnapshot(
        goal_id=goal_id,
        status=GoalNodeStatus.READY,
        created_at=NOW - timedelta(minutes=5),
        title=f"目标 {goal_id}",
        original_fragment=f"完成 {goal_id}",
        progress_summary=f"{goal_id} 尚未开始",
        application_hint=f"app.{goal_id.lower()}",
        explicit_priority=priority,
    )


def _plan(role: _RoleModel):
    scheduler = AttentionScheduler(QwenAttentionSelector(role))
    return scheduler.request_attention(
        session_id="session-1",
        trigger_key="event:event-1",
        original_instruction="先完成 A 或 B",
        authority_revision=3,
        goals=(_goal("A", 100), _goal("B", 50)),
        context=AgendaContext(
            active_goal_id="B",
        ),
        now=NOW,
        trigger_summary="收到新的 Session 事件",
    )


def test_qwen_attention_selector_uses_forced_tool_and_only_scheduler_shortlist():
    role = _RoleModel(_decoded("B"))
    plan = _plan(role)

    assert plan.selected_goal_id == "B"
    assert plan.selector_kind is AttentionSelectorKind.QWEN
    assert plan.slice_budget.time_budget_ms == 360_000
    assert plan.slice_budget.action_budget == 15
    assert plan.model_adjustment == -50

    call = role.calls[0]
    assert call["tool_name"] == "record_attention_selection"
    assert call["observations"] == ()
    assert call["reasoning_effort"] == "low"
    assert call["reasoning_budget"] == 0
    assert call["parameters"]["additionalProperties"] is False
    assert call["parameters"]["properties"]["selected_goal_id"]["enum"] == ["A", "B"]
    prompt = json.loads(call["prompt"])
    assert set(prompt) == {"schema_version", "session", "shortlist"}
    assert [item["goal_id"] for item in prompt["shortlist"]] == ["A", "B"]
    assert prompt["session"]["trigger_summary"] == "收到新的 Session 事件"
    assert "device" not in prompt and "account" not in prompt


@pytest.mark.parametrize(
    "response, error",
    [
        (_decoded("outside"), "outside the scheduler shortlist"),
        (_decoded(slice_time_budget_ms=179_999), "time_budget_ms"),
        (_decoded(slice_time_budget_ms=True), "time budget"),
        (_decoded(slice_action_budget=51), "action_budget"),
        (_decoded(slice_action_budget=True), "action budget"),
        (_decoded(checkpoint_policy="ANY_ACTION"), "checkpoint policy"),
        (_decoded(schema_version="r3.invalid"), "schema version"),
        ({**_decoded(), "unexpected": "field"}, "fields"),
    ],
)
def test_qwen_attention_selector_rejects_invalid_schema_candidate_or_budget(
    response, error
):
    with pytest.raises(ValueError, match=error):
        _plan(_RoleModel(response))


def test_invalid_forced_tool_response_is_schema_failure_not_transport_fallback():
    invalid_response = MobileTaskAdapterError(
        "mobile_role_invalid_response", "bad forced tool call"
    )
    selector = TransportFallbackAttentionSelector(
        QwenAttentionSelector(_RoleModel(error=invalid_response)),
        transport_errors=(MobileTaskAdapterError,),
    )
    with pytest.raises(ValueError, match="forced-tool response"):
        _plan_with_selector(selector)


def test_actual_model_transport_error_uses_deterministic_fallback():
    unavailable = MobileTaskAdapterError("mobile_role_unavailable", "offline")
    plan = build_qwen_attention_scheduler(
        _RoleModel(error=unavailable)
    ).request_attention(
        session_id="session-1",
        trigger_key="event:event-1",
        original_instruction="先完成 A 或 B",
        authority_revision=3,
        goals=(_goal("A", 100), _goal("B", 50)),
        context=AgendaContext(),
        now=NOW,
    )
    assert plan.selected_goal_id == "A"
    assert plan.selector_kind is AttentionSelectorKind.DETERMINISTIC


def _plan_with_selector(selector):
    return AttentionScheduler(selector).request_attention(
        session_id="session-1",
        trigger_key="event:event-1",
        original_instruction="先完成 A 或 B",
        authority_revision=3,
        goals=(_goal("A", 100), _goal("B", 50)),
        context=AgendaContext(),
        now=NOW,
    )
