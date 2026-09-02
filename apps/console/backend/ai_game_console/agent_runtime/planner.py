"""R2 Session-level GoalGraph planning.

The planner deliberately returns a *complete* snapshot.  A caller may use a
model-backed implementation, but must validate coverage before handing the
snapshot to the durable graph store.  The deterministic implementation is a
conservative no-model path: it preserves every directive as a separate goal
unless a small, unambiguous Chinese compound pattern can be safely split.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from .domain import (
    GoalCoverageDraft,
    GoalCoverageKind,
    GoalCriterionDraft,
    GoalEdgeDraft,
    GoalGraphRevisionDraft,
    GoalNodeDraft,
    GoalNodeStatus,
    GoalSchedulingClass,
    GoalNode,
    UserDirective,
)


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """Validated-planner input for one atomic GoalGraph revision commit.

    ``goal_text_by_id`` is intentionally separate from the public node title:
    it is the immutable original input passed to GoalRuntime only after the
    graph transaction commits.
    """

    revision: GoalGraphRevisionDraft
    nodes: tuple[GoalNodeDraft, ...]
    edges: tuple[GoalEdgeDraft, ...]
    criteria: tuple[GoalCriterionDraft, ...]
    coverage: tuple[GoalCoverageDraft, ...]
    goal_text_by_id: dict[str, str]
    operations: tuple[dict[str, str], ...] = ()
    session_interpretation: str | None = None

    def goal_text(self, goal_id: str) -> str:
        text = self.goal_text_by_id.get(goal_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"planner omitted execution text for GoalNode {goal_id}")
        return text.strip()


class SessionPlanner(Protocol):
    """Produce one revision snapshot from the durable directive ledger."""

    def plan(
        self,
        *,
        session_id: str,
        directives: Sequence[UserDirective],
        authority_revision: int,
        graph_revision: int,
        existing_nodes: Sequence[GoalNode] = (),
        applicable_user_facts: Sequence[dict[str, Any]] = (),
    ) -> SessionPlan: ...


class FallbackSessionPlanner:
    """Use a conservative planner only for explicitly recoverable outages.

    Validation and programming errors are deliberately not swallowed. Normal
    composition supplies the concrete local-model transport exception so an
    unavailable Qwen endpoint cannot strand an already durable directive.
    """

    def __init__(
        self,
        primary: SessionPlanner,
        *,
        fallback: SessionPlanner | None = None,
        recoverable_exceptions: tuple[type[BaseException], ...],
    ) -> None:
        self.primary = primary
        self.fallback = fallback or DeterministicSessionPlanner()
        self.recoverable_exceptions = recoverable_exceptions

    def plan(
        self,
        *,
        session_id: str,
        directives: Sequence[UserDirective],
        authority_revision: int,
        graph_revision: int,
        existing_nodes: Sequence[GoalNode] = (),
        applicable_user_facts: Sequence[dict[str, Any]] = (),
    ) -> SessionPlan:
        try:
            return self.primary.plan(
                session_id=session_id, directives=directives,
                authority_revision=authority_revision, graph_revision=graph_revision,
                existing_nodes=existing_nodes,
                applicable_user_facts=applicable_user_facts,
            )
        except self.recoverable_exceptions:
            return self.fallback.plan(
                session_id=session_id, directives=directives,
                authority_revision=authority_revision, graph_revision=graph_revision,
                existing_nodes=existing_nodes,
                applicable_user_facts=applicable_user_facts,
            )


class DeterministicSessionPlanner:
    """Conservative fallback used when a structured Qwen role is unavailable.

    This is not a semantic substitute for Qwen.  It makes no destructive
    revision inference: every directive remains represented, and the handful
    of explicit application conjunctions below are split only when both the
    application and the shared outcome wording are present in source text.
    """

    def plan(
        self,
        *,
        session_id: str,
        directives: Sequence[UserDirective],
        authority_revision: int,
        graph_revision: int,
        existing_nodes: Sequence[GoalNode] = (),
        applicable_user_facts: Sequence[dict[str, Any]] = (),
    ) -> SessionPlan:
        del session_id, applicable_user_facts
        if not directives:
            raise ValueError("Session planning requires at least one directive")
        source = directives[-1]
        nodes: list[GoalNodeDraft] = []
        criteria: list[GoalCriterionDraft] = []
        coverage: list[GoalCoverageDraft] = []
        goal_text_by_id: dict[str, str] = {}
        operations: list[dict[str, str]] = []

        # A later directive starts with a full snapshot of the existing graph.
        # KEEP records are deliberately side-effect-free in the store.
        for current in existing_nodes:
            nodes.append(GoalNodeDraft(
                id=current.id, title=current.title,
                source_directive_id=current.source_directive_id,
                original_fragment=current.original_fragment or current.title,
                status=current.status, goal_family=current.goal_family,
                application_hint=current.application_hint, account_hint=current.account_hint,
                explicit_priority=current.explicit_priority, existing_goal_id=current.id,
                scheduling_class=current.scheduling_class,
                operation="KEEP",
            ))
            goal_text_by_id[current.id] = current.original_fragment or current.title
            operations.append({"operation": "RETAIN", "goal_ref": current.id})
            coverage.append(GoalCoverageDraft(
                source_directive_id=current.source_directive_id,
                original_fragment=current.original_fragment or current.title,
                coverage_kind=GoalCoverageKind.GOAL, target_goal_id=current.id,
            ))

        # Plan every directive not represented by the committed snapshot. This
        # matters when two user messages arrive while one model plan is still
        # running: the winning authority may advance by more than one revision.
        represented_directives = {item.source_directive_id for item in existing_nodes}
        planned_directives = [
            item for item in directives if item.id not in represented_directives
        ]
        for directive in planned_directives:
            if directive.directive_kind.value == "reprioritize" and existing_nodes:
                target = _mentioned_goal(existing_nodes, directive.content)
                if target is not None:
                    priority = 100 if "第一" in directive.content or "优先" in directive.content else target.explicit_priority
                    _replace_existing_snapshot(nodes, target, title=target.title, priority=priority)
                    coverage.append(GoalCoverageDraft(
                        source_directive_id=directive.id,
                        original_fragment=directive.content,
                        coverage_kind=GoalCoverageKind.GOAL,
                        target_goal_id=target.id,
                    ))
                    operations.append({"operation": "REPRIORITIZE", "goal_ref": target.id})
                else:
                    # An ambiguous reference remains an explicit Session policy
                    # instead of inventing a duplicate GoalRun.
                    coverage.append(GoalCoverageDraft(
                        source_directive_id=directive.id,
                        original_fragment=directive.content,
                        coverage_kind=GoalCoverageKind.SESSION_POLICY,
                        session_policy=directive.content,
                    ))
                    operations.append({"operation": "REPRIORITIZE", "goal_ref": "session-policy"})
                continue
            if directive.directive_kind.value == "revise" and existing_nodes:
                revision_match = re.fullmatch(
                    r"把[‘’“”'\"]?(.+?)[‘’“”'\"]?修改为[‘’“”'\"]?(.+?)[‘’“”'\"]?",
                    directive.content.strip().rstrip("。"),
                )
                target = _mentioned_goal(existing_nodes, revision_match.group(1)) if revision_match else None
                if target is not None and revision_match is not None:
                    revised_title = revision_match.group(2).strip().strip("‘’“”'\"")
                    _replace_existing_snapshot(
                        nodes, target, title=revised_title,
                        priority=target.explicit_priority,
                    )
                    coverage.append(GoalCoverageDraft(
                        source_directive_id=directive.id,
                        original_fragment=directive.content,
                        coverage_kind=GoalCoverageKind.GOAL,
                        target_goal_id=target.id,
                    ))
                    operations.append({"operation": "REVISE", "goal_ref": target.id})
                else:
                    coverage.append(GoalCoverageDraft(
                        source_directive_id=directive.id,
                        original_fragment=directive.content,
                        coverage_kind=GoalCoverageKind.SESSION_POLICY,
                        session_policy=directive.content,
                    ))
                    operations.append({"operation": "REVISE", "goal_ref": "session-policy"})
                continue
            for ordinal, (title, fragment) in enumerate(_fallback_goals(directive.content), 1):
                goal_id = f"directive-{directive.revision}-goal-{ordinal}"
                # Stable ids keep planner retries byte-for-byte identical.  A
                # store resolves them into durable GoalNode ids during commit.
                node = GoalNodeDraft(
                    id=goal_id,
                    title=title,
                    source_directive_id=directive.id,
                    original_fragment=fragment,
                    status=GoalNodeStatus.PLANNED,
                    execution_goal=fragment,
                    scheduling_class=_scheduling_class_from_fragment(fragment),
                )
                nodes.append(node)
                goal_text_by_id[goal_id] = fragment
                criteria.append(GoalCriterionDraft(
                    goal_id=goal_id,
                    description=f"完成并验证：{fragment}",
                    evidence_requirement="保留与该目标对应的可复核结果或阻塞事实。",
                    required=True,
                ))
                coverage.append(GoalCoverageDraft(
                    source_directive_id=directive.id,
                    original_fragment=fragment,
                    coverage_kind=GoalCoverageKind.GOAL,
                    target_goal_id=goal_id,
                ))
                operations.append({"operation": "ADD", "goal_ref": goal_id})

        return SessionPlan(
            revision=GoalGraphRevisionDraft(
                authority_revision=authority_revision,
                source_directive_id=source.id,
                reason="deterministic_fallback",
                base_graph_revision=graph_revision,
            ),
            nodes=tuple(nodes),
            edges=(),
            criteria=tuple(criteria),
            coverage=tuple(coverage),
            goal_text_by_id=goal_text_by_id,
            operations=tuple(operations),
            session_interpretation="模型不可用；已保守保留每条原始指令的独立目标。",
        )


def _fallback_goals(content: str) -> list[tuple[str, str]]:
    """Return only source-derived fallback goals; never discard text."""

    text = content.strip()
    output: list[tuple[str, str]] = []
    consumed: set[str] = set()

    # The R2 acceptance phrase is intentionally handled as two independent
    # goals.  The shared chat qualifier remains part of each immutable source
    # fragment, so neither branch loses it before model planning is available.
    if re.search(r"Soul", text, re.IGNORECASE) and "探探" in text and "认识" in text:
        fragment = _sentence_containing(text, "认识")
        output.extend((("Soul 认识人", fragment), ("探探认识人", fragment)))
        consumed.add(fragment)
    for marker, title in (
        ("投简历", "找工作"),
        ("微信", "微信维护"),
        ("游戏", "空闲游戏"),
    ):
        if marker in text:
            fragment = _sentence_containing(text, marker)
            output.append((title, fragment))
            consumed.add(fragment)

    # Preserve every otherwise-unrecognised clause.  A model-backed plan may
    # later refine it, but the fallback must never silently omit user intent.
    for clause in (item.strip() for item in re.split(r"[；;。]", text)):
        if clause and clause not in consumed:
            output.append((clause, clause))
    return output or [(text, text)]


def _sentence_containing(text: str, marker: str) -> str:
    for candidate in re.split(r"[；;。]", text):
        if marker in candidate:
            return candidate.strip()
    return text


def _mentioned_goal(existing_nodes: Sequence[GoalNode], text: str) -> GoalNode | None:
    matches = [item for item in existing_nodes if item.title in text]
    return max(matches, key=lambda item: len(item.title)) if matches else None


def _replace_existing_snapshot(
    nodes: list[GoalNodeDraft],
    current: GoalNode,
    *,
    title: str,
    priority: int | None,
) -> None:
    for index, draft in enumerate(nodes):
        if draft.existing_goal_id != current.id:
            continue
        nodes[index] = GoalNodeDraft(
            id=current.id,
            title=title,
            source_directive_id=current.source_directive_id,
            original_fragment=current.original_fragment or current.title,
            status=current.status,
            goal_family=current.goal_family,
            application_hint=current.application_hint,
            account_hint=current.account_hint,
            explicit_priority=priority,
            scheduling_class=current.scheduling_class,
            existing_goal_id=current.id,
            operation="UPDATE",
        )
        return
    raise ValueError("revised GoalNode is absent from the complete snapshot")


def _scheduling_class_from_fragment(fragment: str) -> GoalSchedulingClass:
    """Mark only an explicit idle-game instruction as idle-only.

    R3 migration deliberately defaults historical goals to NORMAL.  The
    deterministic planner may opt into IDLE_ONLY only when the same source
    fragment clearly contains both the idle condition and game intent.
    """

    compact = re.sub(r"\s+", "", fragment)
    idle = any(token in compact for token in ("空闲", "没别的事", "没有别的事", "没事时"))
    game = any(token in compact for token in ("游戏", "玩一会", "玩会", "打游戏"))
    return (
        GoalSchedulingClass.IDLE_ONLY
        if idle and game
        else GoalSchedulingClass.NORMAL
    )
