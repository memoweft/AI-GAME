"""Original-intent coverage checks for a prepared GoalGraph revision."""

from __future__ import annotations

import re

from .domain import GoalCoverageDraft, GoalCoverageKind, GoalNodeDraft


def uncovered_fragments(
    fragments: tuple[str, ...] | list[str], coverage: tuple[GoalCoverageDraft, ...] | list[GoalCoverageDraft]
) -> tuple[str, ...]:
    """Return submitted intent fragments not mapped to a Goal or session policy."""
    covered = tuple(item.original_fragment.strip() for item in coverage)
    missing: list[str] = []
    for fragment in fragments:
        source = _meaningful(fragment)
        if not source:
            continue
        mask = [False] * len(source)
        for piece in covered:
            needle = _meaningful(piece)
            if not needle:
                continue
            start = 0
            while True:
                index = source.find(needle, start)
                if index < 0:
                    break
                for position in range(index, index + len(needle)):
                    mask[position] = True
                start = index + 1
        if not all(mask):
            missing.append(fragment)
    return tuple(missing)


def _meaningful(value: str) -> str:
    """Ignore clause punctuation while retaining every semantic character."""

    return re.sub(r"[\s，,、：:！？!?（）()\[\]【】\"'“”‘’]+", "", value.strip())


def validate_complete_coverage(
    fragments: tuple[str, ...] | list[str],
    coverage: tuple[GoalCoverageDraft, ...] | list[GoalCoverageDraft],
    nodes: tuple[GoalNodeDraft, ...] | list[GoalNodeDraft],
) -> None:
    """Reject plans that silently drop an original clause or point outside it."""
    missing = uncovered_fragments(fragments, coverage)
    if missing:
        raise ValueError("GoalGraph coverage is missing original fragments: " + ", ".join(missing))
    node_ids = {node.id for node in nodes} | {node.existing_goal_id for node in nodes if node.existing_goal_id}
    for item in coverage:
        if item.coverage_kind is GoalCoverageKind.GOAL and item.target_goal_id not in node_ids:
            raise ValueError("Goal coverage points to a GoalNode absent from this revision")
