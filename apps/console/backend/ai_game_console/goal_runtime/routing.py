from __future__ import annotations

from dataclasses import dataclass
from typing import Any


FINITE_PHONE = "finite_phone"
LONG_LIVED_MOBILE_APPLICATION = "long_lived_mobile_application"
LOCAL_MANAGED_APPLICATION = "local_managed_application"
LANGUAGE_ONLY = "language_only"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route_kind: str
    binding_kind: str
    capability_ids: tuple[str, ...]
    owner_kind: str | None
    profile_id: str | None
    classification: str
    rationale: str


def decide_route(
    specification: dict[str, Any], *, finite_binding_kind: str
) -> RouteDecision | None:
    """Convert a frozen model classification into a capability-only plan.

    This function is pure: it does not inspect a device, start a worker, call
    an owner, or mutate runtime state.
    """

    intent = specification.get("normalized_intent") or {}
    classification = str(intent.get("classification") or "").strip().casefold()
    if classification in {"finite_phone_goal", "finite_phone", "pending_u3"}:
        return RouteDecision(
            route_kind=FINITE_PHONE,
            binding_kind=finite_binding_kind,
            capability_ids=(
                "android.observe",
                "android.action",
                "local.goal_verification",
                "experience.record",
            ),
            owner_kind=finite_binding_kind,
            profile_id=None,
            classification=classification,
            rationale="bounded phone operation requires the canonical finite-device owner",
        )
    if classification in {
        "long_lived_local_goal",
        "local_managed_long_lived_goal",
    }:
        return RouteDecision(
            route_kind=LOCAL_MANAGED_APPLICATION,
            binding_kind="application_runtime",
            capability_ids=(
                "long_lived.wait",
                "local.managed_notification",
                "experience.record",
            ),
            owner_kind="local_runtime",
            profile_id="local-managed-v1",
            classification=classification,
            rationale=(
                "a managed local continuous goal waits for the same GoalRun's "
                "authorized events without requiring a third-party owner"
            ),
        )
    if classification in {
        "long_lived_application_goal",
        "long_lived_goal",
        "waiting_driven_application_goal",
    }:
        return RouteDecision(
            route_kind=LONG_LIVED_MOBILE_APPLICATION,
            binding_kind="long_lived_mobile_composition",
            capability_ids=(
                "long_lived.wait",
                "android.observe",
                "android.action",
                "local.goal_verification",
                "experience.record",
            ),
            owner_kind=None,
            profile_id=None,
            classification=classification,
            rationale=(
                "a long-lived mobile goal requires an ApplicationRuntime supervisor "
                "composed with bounded RuntimeKernel device cycles"
            ),
        )
    if classification in {"language_only_goal", "language_only"}:
        return RouteDecision(
            route_kind=LANGUAGE_ONLY,
            binding_kind="local_language",
            capability_ids=("local.text_reasoning", "local.language_result"),
            owner_kind="local_language",
            profile_id=None,
            classification=classification,
            rationale="the requested result needs no device or external owner",
        )
    return None
