from __future__ import annotations

import pytest

from ai_game_console.android_ui_runtime.domain import (
    AndroidUiAction, CriteriaRevision, Criterion, ObservationEnvelope, UiNode,
    compile_criteria, opaque_node_id,
)
from ai_game_console.android_ui_runtime.role import BoundedToolRoleAdapter, PlanningContext
from ai_game_console.android_ui_runtime.sanitizer import sanitize_task_goal, sanitize_task_payload


def _observation() -> ObservationEnvelope:
    return ObservationEnvelope("task-1", "profile-1", 1, "boot-1", "device-1", "shot-1", "a" * 64, "tree-1", "b" * 64, "state-1", "c" * 64, "now", "fresh-1", "Settings homepage")


class _Artifacts:
    def load_for_model(self, observation: ObservationEnvelope) -> dict[str, object]:
        return {"opaque_screenshot": observation.screenshot_ref, "opaque_tree": observation.ui_tree_ref}


class _Caller:
    def __init__(self, reply: dict[str, object]) -> None:
        self.reply = reply
        self.observations: tuple[dict[str, object], ...] = ()

    def call_tool(self, **kwargs: object) -> dict[str, object]:
        self.observations = kwargs["observations"]  # type: ignore[assignment]
        return self.reply


def _context() -> PlanningContext:
    criteria = CriteriaRevision(1, (Criterion("c1", "enter notification settings"),), "digest")
    return PlanningContext("task-1", "enter notification settings", 1, criteria, _observation())


def test_tool_role_uses_opaque_artifact_and_emits_one_bounded_action() -> None:
    caller = _Caller({"kind": "action", "action": "tap", "arguments": {"x": 0.5, "y": 0.3}, "reason": "Tap notification row"})
    role = BoundedToolRoleAdapter(caller, _Artifacts())
    decision = role.decide(_context(), {"plan": "open notification page"})
    assert decision.action is not None and decision.action.kind == "tap"
    assert caller.observations == ({"opaque_screenshot": "shot-1", "opaque_tree": "tree-1"},)


def test_tool_role_drops_hallucinated_hint_when_retrieval_is_empty() -> None:
    caller = _Caller({
        "kind": "action", "action": "wait", "arguments": {"seconds": 1},
        "reason": "Wait for the page.", "selected_hint_id": "not-a-retrieved-hint",
    })
    decision = BoundedToolRoleAdapter(caller, _Artifacts()).decide(
        _context(), {"plan": "wait"},
    )

    assert decision.action == AndroidUiAction("wait", {"seconds": 1})
    assert decision.selected_hint_id is None


def test_tool_role_resolves_selected_opaque_node_to_trusted_center() -> None:
    node_id = opaque_node_id("notification-row")
    observation = ObservationEnvelope(
        "task-1", "profile-1", 1, "boot-1", "device-1",
        "shot-1", "a" * 64, "tree-1", "b" * 64,
        "state-1", "c" * 64, "now", "fresh-1", "Settings homepage",
        (UiNode(node_id, "通知", (0.1, 0.2, 0.9, 0.4), True, "navigation"),),
    )
    criteria = CriteriaRevision(
        1, (Criterion("c1", "enter notification settings"),), "digest",
    )
    context = PlanningContext(
        "task-1", "enter notification settings", 1, criteria, observation,
    )
    role = BoundedToolRoleAdapter(
        _Caller({
            "kind": "action", "action": "tap",
            "arguments": {"node_id": node_id}, "reason": "Tap notification row",
        }),
        _Artifacts(),
    )

    decision = role.decide(context, {"plan": "open notification page"})

    assert decision.action is not None
    assert decision.action.kind == "tap"
    assert decision.action.arguments["x"] == pytest.approx(0.5)
    assert decision.action.arguments["y"] == pytest.approx(0.3)


@pytest.mark.parametrize(
    "node",
    (
        UiNode(opaque_node_id("not-clickable"), "通知", (0.1, 0.2, 0.9, 0.4), False),
        UiNode(opaque_node_id("missing-bounds"), "通知", None, True),
    ),
)
def test_tool_role_rejects_node_without_clickable_trusted_bounds(node: UiNode) -> None:
    observation = ObservationEnvelope(
        "task-1", "profile-1", 1, "boot-1", "device-1",
        "shot-1", "a" * 64, "tree-1", "b" * 64,
        "state-1", "c" * 64, "now", "fresh-1", "Settings homepage", (node,),
    )
    criteria = CriteriaRevision(
        1, (Criterion("c1", "enter notification settings"),), "digest",
    )
    context = PlanningContext(
        "task-1", "enter notification settings", 1, criteria, observation,
    )
    role = BoundedToolRoleAdapter(
        _Caller({
            "kind": "action", "action": "tap",
            "arguments": {"node_id": node.node_id}, "reason": "Tap notification row",
        }),
        _Artifacts(),
    )

    with pytest.raises(ValueError, match="not grounded"):
        role.decide(context, {"plan": "open notification page"})


def test_tool_role_rejects_unknown_opaque_node() -> None:
    role = BoundedToolRoleAdapter(
        _Caller({
            "kind": "action", "action": "tap",
            "arguments": {"node_id": opaque_node_id("missing")},
            "reason": "Tap notification row",
        }),
        _Artifacts(),
    )

    with pytest.raises(ValueError, match="not grounded"):
        role.decide(_context(), {"plan": "open notification page"})


def test_terminal_candidate_never_claims_success() -> None:
    role = BoundedToolRoleAdapter(_Caller({"kind": "terminal_candidate", "action": None, "arguments": {}, "reason": "Looks done"}), _Artifacts())
    decision = role.decide(_context(), {"plan": "x"})
    assert decision.kind == "terminal_candidate"
    assert decision.action is None


def test_tool_role_accepts_only_a_bounded_non_effect_wait() -> None:
    caller = _Caller({"kind": "action", "action": "wait", "arguments": {"seconds": 1}, "reason": "Wait"})
    role = BoundedToolRoleAdapter(caller, _Artifacts())
    decision = role.decide(_context(), {"plan": "x"})
    assert decision.action == AndroidUiAction("wait", {"seconds": 1})


@pytest.mark.parametrize("action,arguments", [
    ("tap", {"x": 1.1, "y": 0.2}), ("input_text", {"text": ""}),
    ("wait", {"seconds": 11}), ("open_app", {"package": ""}),
])
def test_action_boundaries_reject_invalid_primitives(action: str, arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AndroidUiAction(action, arguments)  # type: ignore[arg-type]


def test_task_goal_sanitizer_keeps_ordinary_goal_but_redacts_explicit_secret() -> None:
    goal = sanitize_task_goal("在 Soul 给 Alice 发消息，然后输入 password: dont-store-this，密码：中文秘密，PIN=2468，auth: abc")
    assert "Soul" in goal and "Alice" in goal and "dont-store-this" not in goal
    assert "中文秘密" not in goal and "2468" not in goal and "abc" not in goal
    payload = sanitize_task_payload({"goal": goal, "nested": ["验证码: 123456"]})
    assert "123456" not in str(payload)


def test_frozen_criteria_keep_normal_social_task_semantics_but_not_secrets() -> None:
    criteria = compile_criteria("在 Soul 和 Alice 匹配后聊天，password: correct horse battery staple", revision=1)
    rendered = " ".join(item.description for item in criteria.criteria)
    assert "Soul" in rendered and "Alice" in rendered and "匹配" in rendered and "聊天" in rendered
    assert "correct horse battery staple" not in rendered


def test_task_payload_redacts_sensitive_mapping_key_even_for_nested_values() -> None:
    payload = sanitize_task_payload({
        "password": "SENTINEL-A", "api_key": {"nested": "SENTINEL-B"},
        "stop_condition": {"nested-auth": ["SENTINEL-C"]},
        "schedule": {"验证码": "SENTINEL-D"},
        "goal": "在 Soul 给 Alice 发普通消息",
    })
    rendered = str(payload)
    for value in ("SENTINEL-A", "SENTINEL-B", "SENTINEL-C", "SENTINEL-D"):
        assert value not in rendered
    assert payload["goal"] == "在 Soul 给 Alice 发普通消息"


def test_task_payload_never_retains_secret_bearing_key_or_multitoken_value() -> None:
    key_sentinel = "K2-key correct horse battery staple"
    value_sentinel = "K2-value correct horse battery staple"
    payload = sanitize_task_payload({
        f"password: {key_sentinel}": "ignored",
        "ordinary": "在 Soul 给 Alice 发消息后，password: " + value_sentinel,
        "next": "保留普通任务语义",
    })
    rendered = str(payload)
    assert key_sentinel not in rendered
    assert value_sentinel not in rendered
    assert "correct horse battery staple" not in rendered
    assert "password" not in rendered.casefold()
    assert list(payload)[:1] == ["[redacted-secret-key-1]"]
    assert payload["next"] == "保留普通任务语义"


@pytest.mark.parametrize("ref", ["C:/private/screenshot.png", "https://host/artifact", "token=secret"])
def test_observation_rejects_nonopaque_artifact_references(ref: str) -> None:
    with pytest.raises(ValueError):
        ObservationEnvelope("task-1", "profile-1", 1, "boot-1", "device-1", ref, "a" * 64, None, None, "state-1", "b" * 64, "now", "fresh-1")
