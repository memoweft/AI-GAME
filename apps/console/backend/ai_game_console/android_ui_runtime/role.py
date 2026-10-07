"""Pure role protocols and a bounded tool-role adapter.

The adapter accepts opaque model artifacts through ``ArtifactLoader`` only. It
does not log or persist those payloads and deliberately has no device import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .domain import (
    AndroidUiAction, ArtifactLoader, CriteriaRevision, CriterionVerdict,
    ObservationEnvelope, RoleDecision,
)
from .sanitizer import sanitize_summary


@dataclass(frozen=True, slots=True)
class PlanningContext:
    task_id: str
    goal: str
    revision: int
    criteria: CriteriaRevision
    observation: ObservationEnvelope
    experience_hints: tuple[Mapping[str, Any], ...] = ()
    recent_actions: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ActionVerificationContext:
    goal: str
    observation_before: ObservationEnvelope
    observation_after: ObservationEnvelope
    action: AndroidUiAction


@dataclass(frozen=True, slots=True)
class PrimitiveVerification:
    progress: bool
    uncertain: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReflectionContext:
    goal: str
    criteria: CriteriaRevision
    observation: ObservationEnvelope
    reason: str


class PlannerRole(Protocol):
    def plan(self, context: PlanningContext) -> Mapping[str, Any]: ...


class ActorRole(Protocol):
    def decide(self, context: PlanningContext, plan: Mapping[str, Any]) -> RoleDecision: ...


class ActionVerifierRole(Protocol):
    def verify(self, context: ActionVerificationContext) -> PrimitiveVerification: ...


class ReflectionRole(Protocol):
    def reflect(self, context: ReflectionContext) -> Mapping[str, Any]: ...


class FinalSemanticVerifierRole(Protocol):
    def verify_goal(
        self, context: PlanningContext, *, before: ObservationEnvelope | None,
        after: ObservationEnvelope, latest_step_index: int, already_satisfied: bool,
    ) -> tuple[CriterionVerdict, ...]: ...


class ToolCaller(Protocol):
    def call_tool(
        self, *, system: str, prompt: str, observations: tuple[Mapping[str, Any], ...],
        tool_name: str, description: str, parameters: Mapping[str, Any], max_tokens: int,
    ) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class BoundedToolRoleAdapter:
    """Minimal adapter modelled after existing forced-tool use, not its runtime."""
    caller: ToolCaller
    artifacts: ArtifactLoader

    def plan(self, context: PlanningContext) -> Mapping[str, Any]:
        reply = self._call(
            context.observation,
            system="You are a bounded Android UI planner. Preserve every frozen criterion.",
            prompt={"goal": context.goal, "criteria": [c.description for c in context.criteria.criteria], "ui_summary": context.observation.ui_summary, "hints": list(context.experience_hints), "recent_actions": list(context.recent_actions)},
            tool_name="record_android_plan",
            parameters={"type": "object", "additionalProperties": False, "properties": {"plan": {"type": "string", "maxLength": 1000}}, "required": ["plan"]},
        )
        return {"plan": sanitize_summary(reply.get("plan"), maximum=1_000)}

    def decide(self, context: PlanningContext, plan: Mapping[str, Any]) -> RoleDecision:
        reply = self._call(
            context.observation,
            system=(
                "Propose exactly one bounded Android primitive or a terminal candidate. "
                "A terminal candidate is never success. selected_hint_id may name only an "
                "exact supplied hint_id; when hints is empty it must be null."
            ),
            prompt={"goal": context.goal, "criteria": [c.description for c in context.criteria.criteria], "plan": plan.get("plan", ""), "ui_summary": context.observation.ui_summary, "hints": list(context.experience_hints), "recent_actions": list(context.recent_actions)},
            tool_name="android_ui_step",
            parameters={
                "type": "object", "additionalProperties": False,
                "properties": {"kind": {"type": "string", "enum": ["action", "terminal_candidate", "replan"]}, "action": {"type": ["string", "null"], "enum": ["tap", "long_press", "swipe", "input_text", "back", "home", "recents", "open_app", "wait", None]}, "arguments": {
                    "type": "object", "additionalProperties": False,
                    "description": "tap/long_press: x,y or only node_id. swipe: x,y,end_x,end_y. All coordinates are normalized fractions from 0 to 1, never pixels. input_text: text. open_app: package. wait: seconds. back/home/recents and non-action decisions: empty object.",
                    "properties": {
                        "x": {"type": "number", "minimum": 0, "maximum": 1, "description": "Start horizontal position as fraction of screenshot width."},
                        "y": {"type": "number", "minimum": 0, "maximum": 1, "description": "Start vertical position as fraction of screenshot height."},
                        "end_x": {"type": "number", "minimum": 0, "maximum": 1, "description": "Swipe end horizontal fraction."},
                        "end_y": {"type": "number", "minimum": 0, "maximum": 1, "description": "Swipe end vertical fraction."},
                        "node_id": {"type": "string", "description": "Exact clickable node identifier from this observation; use alone for tap/long_press."},
                        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "package": {"type": "string", "description": "Exact Android application package name."},
                        "duration_ms": {"type": "integer", "minimum": 1, "maximum": 10000},
                        "seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 10},
                    },
                }, "reason": {"type": "string", "maxLength": 600}, "selected_hint_id": {"type": ["string", "null"], "maxLength": 256}},
                "required": ["kind", "action", "arguments", "reason", "selected_hint_id"],
            },
        )
        kind = reply.get("kind")
        if kind not in {"action", "terminal_candidate", "replan"}:
            raise ValueError("invalid Android UI role decision")
        reason = sanitize_summary(reply.get("reason"), maximum=600)
        selected_hint_id = reply.get("selected_hint_id")
        available_hint_ids = {
            item.get("hint_id") for item in context.experience_hints
            if isinstance(item, Mapping) and isinstance(item.get("hint_id"), str)
        }
        if selected_hint_id is not None and (
            not isinstance(selected_hint_id, str)
            or selected_hint_id not in available_hint_ids
        ):
            # A hallucinated selection has no execution authority.  When no
            # retrieval was available at all, clear only this advisory field
            # and retain the independently bounded action; a nonempty set
            # still fails closed so K3 usage can never be misattributed.
            if not available_hint_ids:
                selected_hint_id = None
            else:
                raise ValueError("selected experience hint is not available for this fresh observation")
        if kind != "action":
            if selected_hint_id is not None:
                raise ValueError("terminal decision cannot select an experience hint")
            return RoleDecision(kind, reason=reason, terminal_summary=reason)
        action = reply.get("action")
        args = reply.get("arguments")
        if action not in {
            "tap", "long_press", "swipe", "input_text",
            "back", "home", "recents", "open_app", "wait",
        } or not isinstance(args, Mapping):
            raise ValueError("Android UI action decision is incomplete")
        bounded_args = dict(args)
        if action in {"tap", "long_press"} and "node_id" in bounded_args:
            # Prefer the model's opaque node selection over model-authored
            # coordinates. Resolve it only against the exact observation that
            # produced this decision; the runner re-checks canonical freshness
            # again before committing and dispatching the action intent.
            if set(bounded_args) != {"node_id"}:
                raise ValueError("node action arguments are ambiguous")
            node_id = bounded_args["node_id"]
            matches = tuple(
                node for node in context.observation.ui_nodes
                if node.node_id == node_id
            )
            if (
                len(matches) != 1
                or not matches[0].clickable
                or matches[0].bounds is None
            ):
                raise ValueError("node action is not grounded in a clickable bound")
            left, top, right, bottom = matches[0].bounds
            bounded_args = {
                "x": (left + right) / 2,
                "y": (top + bottom) / 2,
            }
        return RoleDecision("action", AndroidUiAction(action, bounded_args), reason, selected_hint_id=selected_hint_id)

    def _call(self, observation: ObservationEnvelope, *, system: str, prompt: Mapping[str, Any], tool_name: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        artifact = self.artifacts.load_for_model(observation)
        response = self.caller.call_tool(
            system=system, prompt=str(dict(prompt)), observations=(artifact,), tool_name=tool_name,
            description="Record one bounded Android UI role result", parameters=parameters, max_tokens=1_024,
        )
        if not isinstance(response, Mapping):
            raise ValueError("tool role response must be a mapping")
        return response
