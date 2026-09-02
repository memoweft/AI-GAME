"""Small, pure GoalGraph helpers shared by planner and durable store clients."""

from __future__ import annotations

from dataclasses import dataclass

from .domain import (
    GoalCoverageDraft,
    GoalCriterionDraft,
    GoalEdgeDraft,
    GoalGraphRevision,
    GoalGraphRevisionDraft,
    GoalNodeDraft,
)


@dataclass(frozen=True, slots=True)
class GoalGraphPlan:
    """A complete, side-effect-free revision prepared by a Session planner."""

    revision: GoalGraphRevisionDraft
    nodes: tuple[GoalNodeDraft, ...]
    edges: tuple[GoalEdgeDraft, ...] = ()
    criteria: tuple[GoalCriterionDraft, ...] = ()
    coverage: tuple[GoalCoverageDraft, ...] = ()

    def __post_init__(self) -> None:
        ids = [node.existing_goal_id if node.operation != "CREATE" else node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("a GoalGraph plan may mention each GoalNode only once")


@dataclass(frozen=True, slots=True)
class GoalGraphSnapshot:
    revision: GoalGraphRevision
    nodes: tuple[GoalNodeDraft, ...]
    edges: tuple[GoalEdgeDraft, ...]
    criteria: tuple[GoalCriterionDraft, ...]
    coverage: tuple[GoalCoverageDraft, ...]

