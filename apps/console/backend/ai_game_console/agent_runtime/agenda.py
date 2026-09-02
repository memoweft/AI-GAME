from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum, IntEnum
from typing import Any, Iterable

from .domain import (
    GoalEligibilityDraft,
    GoalEligibilityStatus,
    GoalNodeStatus,
    GoalSchedulingClass,
)


HARD_STARVATION_SECONDS = 30 * 60
"""A normal READY Goal crosses the fairness deadline at exactly 30 minutes."""


class AttentionHardTier(IntEnum):
    IDLE_ONLY = 0
    NORMAL = 1
    STARVATION_DEADLINE = 2
    CRITICAL_EVENT = 3
    LATEST_USER_DIRECTIVE = 4


class EventUrgency(IntEnum):
    NONE = 0
    LOW = 100
    NORMAL = 500
    HIGH = 2_000
    CRITICAL = 5_000


@dataclass(frozen=True, slots=True)
class AgendaGoalSnapshot:
    """The durable scheduling facts needed for one pure agenda evaluation.

    This is intentionally smaller than ``GoalNode``.  Store/service code can
    construct it from the current schema without giving the scheduler write
    access or coupling the scoring code to SQLite rows.
    """

    goal_id: str
    status: GoalNodeStatus | str
    created_at: str | datetime
    title: str | None = None
    original_fragment: str | None = None
    progress_summary: str | None = None
    continuation_summary: str | None = None
    explicit_priority: int | None = None
    scheduling_class: GoalSchedulingClass | str = GoalSchedulingClass.NORMAL
    application_hint: str | None = None
    binding_ready: bool = True
    dependencies_satisfied: bool = True
    wake_satisfied: bool = False
    switch_checkpoint_ready: bool = True
    continuation_resumable: bool = False
    next_eligible_at: str | datetime | None = None
    backoff_until: str | datetime | None = None
    wait_started_at: str | datetime | None = None
    last_service_at: str | datetime | None = None
    consecutive_failure_count: int = 0

    @classmethod
    def from_goal_node(
        cls,
        goal: Any,
        *,
        binding_ready: bool | None = None,
        dependencies_satisfied: bool = True,
        wake_satisfied: bool = False,
        switch_checkpoint_ready: bool = True,
        continuation_resumable: bool | None = None,
    ) -> "AgendaGoalSnapshot":
        """Adapt a current or migrated ``GoalNode`` using a stable duck type."""

        continuation_id = getattr(goal, "continuation_id", None)
        return cls(
            goal_id=goal.id,
            status=goal.status,
            created_at=goal.created_at,
            title=getattr(goal, "title", None),
            original_fragment=getattr(goal, "original_fragment", None),
            progress_summary=getattr(goal, "progress_summary", None),
            explicit_priority=getattr(goal, "explicit_priority", None),
            scheduling_class=getattr(
                goal, "scheduling_class", GoalSchedulingClass.NORMAL
            ),
            application_hint=getattr(goal, "application_hint", None),
            binding_ready=(
                getattr(goal, "bound_goal_run_id", None) is not None
                if binding_ready is None
                else binding_ready
            ),
            dependencies_satisfied=dependencies_satisfied,
            wake_satisfied=wake_satisfied,
            switch_checkpoint_ready=switch_checkpoint_ready,
            continuation_resumable=(
                continuation_id is not None
                if continuation_resumable is None
                else continuation_resumable
            ),
            next_eligible_at=getattr(goal, "next_eligible_at", None),
            backoff_until=getattr(goal, "backoff_until", None),
            wait_started_at=getattr(goal, "wait_started_at", None),
            last_service_at=getattr(goal, "last_service_at", None),
            consecutive_failure_count=getattr(
                goal, "consecutive_failure_count", 0
            ),
        )


@dataclass(frozen=True, slots=True)
class AgendaContext:
    active_goal_id: str | None = None
    current_application_hint: str | None = None
    control_allows_scheduling: bool = True
    latest_directive_goal_ids: frozenset[str] = frozenset()
    event_goal_ids: frozenset[str] = frozenset()
    event_urgency: EventUrgency | str | int = EventUrgency.NONE


@dataclass(frozen=True, slots=True)
class ScoreComponents:
    user_priority: int
    event_urgency: int
    waiting_age: int
    starvation: int
    continuity: int
    app_switch_cost: int
    backoff: int
    recent_failure: int

    @property
    def total(self) -> int:
        return (
            self.user_priority
            + self.event_urgency
            + self.waiting_age
            + self.starvation
            + self.continuity
            - self.app_switch_cost
            - self.backoff
            - self.recent_failure
        )


@dataclass(frozen=True, slots=True)
class _AgendaItem:
    eligibility: GoalEligibilityDraft
    last_service_at: str | None
    created_at: str

    @property
    def eligible(self) -> bool:
        return self.eligibility.eligibility is GoalEligibilityStatus.ELIGIBLE


@dataclass(frozen=True, slots=True)
class AgendaEvaluation:
    candidates: tuple[GoalEligibilityDraft, ...]
    all_goals: tuple[GoalEligibilityDraft, ...]

    def for_goal(self, goal_id: str) -> GoalEligibilityDraft:
        return next(item for item in self.all_goals if item.goal_id == goal_id)


def evaluate_agenda(
    goals: Iterable[AgendaGoalSnapshot],
    *,
    context: AgendaContext,
    now: datetime,
) -> AgendaEvaluation:
    """Compute eligibility, integer components, hard tiers and stable ranks."""

    clock = _as_utc_datetime(now, "now")
    preliminary = tuple(_evaluate_goal(goal, context=context, now=clock) for goal in goals)
    normal_ready = any(
        item.eligible
        and item.eligibility.scheduling_class is GoalSchedulingClass.NORMAL
        for item in preliminary
    )
    evaluated = tuple(
        _defer_idle(item, context=context) if normal_ready else item
        for item in preliminary
    )
    ranked = sorted(
        (item for item in evaluated if item.eligible),
        key=_ranking_key,
    )
    candidates = tuple(
        replace(item.eligibility, rank=index) for index, item in enumerate(ranked, 1)
    )
    by_goal = {item.goal_id: item for item in candidates}
    all_goals = tuple(
        by_goal.get(item.eligibility.goal_id, item.eligibility) for item in evaluated
    )
    return AgendaEvaluation(candidates=candidates, all_goals=all_goals)


def _evaluate_goal(
    goal: AgendaGoalSnapshot,
    *,
    context: AgendaContext,
    now: datetime,
) -> _AgendaItem:
    status = _goal_status(goal.status)
    scheduling_class = _scheduling_class(goal.scheduling_class)
    created_at = _as_utc_datetime(goal.created_at, "created_at")
    last_service_at = _optional_datetime(goal.last_service_at, "last_service_at")
    components = _score_components(
        goal,
        context=context,
        now=now,
        created_at=created_at,
        last_service_at=last_service_at,
    )

    def result(
        eligibility: GoalEligibilityStatus,
        reason: str,
        tier: AttentionHardTier | None = None,
    ) -> _AgendaItem:
        return _AgendaItem(
            eligibility=GoalEligibilityDraft(
                goal_id=goal.goal_id,
                eligibility=eligibility,
                reason=reason,
                goal_status=status,
                scheduling_class=scheduling_class,
                next_eligible_at=(
                    _utc_text(_latest_optional_datetime(goal.next_eligible_at, goal.backoff_until))
                    if goal.next_eligible_at is not None or goal.backoff_until is not None
                    else None
                ),
                latest_directive_target=goal.goal_id in context.latest_directive_goal_ids,
                matched_trigger_event=goal.goal_id in context.event_goal_ids,
                hard_tier=int(tier) if tier is not None else 0,
                user_priority_component=components.user_priority,
                event_urgency_component=components.event_urgency,
                waiting_age_component=components.waiting_age,
                starvation_component=components.starvation,
                continuity_component=components.continuity,
                app_switch_cost=components.app_switch_cost,
                backoff_component=components.backoff,
                recent_failure_component=components.recent_failure,
                total_score=components.total,
            ),
            last_service_at=_utc_text(last_service_at) if last_service_at else None,
            created_at=_utc_text(created_at),
        )

    if not context.control_allows_scheduling:
        return result(GoalEligibilityStatus.CONTROL_BLOCKED, "session control blocks scheduling")
    if status.terminal:
        return result(GoalEligibilityStatus.TERMINAL, f"goal is terminal: {status.value}")
    if not goal.binding_ready:
        return result(GoalEligibilityStatus.NOT_BOUND, "goal runtime binding is not ready")
    if not goal.dependencies_satisfied:
        return result(GoalEligibilityStatus.DEPENDENCY_BLOCKED, "goal dependency is not satisfied")

    waiting = status in {
        GoalNodeStatus.WAITING_EVENT,
        GoalNodeStatus.WAITING_TIME,
        GoalNodeStatus.WAITING_USER_FACT,
        GoalNodeStatus.WAITING_DEVICE,
        GoalNodeStatus.WAITING_ACCOUNT,
        GoalNodeStatus.WAITING_IDENTITY,
    }
    if waiting and not goal.wake_satisfied:
        return result(GoalEligibilityStatus.WAITING, f"{status.value} condition is not satisfied")

    not_before = _latest_optional_datetime(
        goal.next_eligible_at,
        goal.backoff_until,
    )
    if not_before is not None and now < not_before:
        return result(
            GoalEligibilityStatus.BACKOFF,
            f"backoff until {_utc_text(not_before)}",
        )
    if status is GoalNodeStatus.ACTIVE and not (
        goal.continuation_resumable or goal.goal_id == context.active_goal_id
    ):
        return result(
            GoalEligibilityStatus.UNCHECKPOINTED,
            "active goal has no resumable continuation",
        )
    if goal.goal_id != context.active_goal_id and not goal.switch_checkpoint_ready:
        return result(
            GoalEligibilityStatus.UNCHECKPOINTED,
            "current action has not reached a switch checkpoint",
        )
    if status not in {GoalNodeStatus.READY, GoalNodeStatus.ACTIVE} and not (
        waiting and goal.wake_satisfied
    ):
        return result(GoalEligibilityStatus.WAITING, f"goal is not runnable: {status.value}")

    tier = _hard_tier(
        goal,
        context=context,
        now=now,
        created_at=created_at,
        last_service_at=last_service_at,
        scheduling_class=scheduling_class,
    )
    return result(GoalEligibilityStatus.ELIGIBLE, "eligible", tier)


def _score_components(
    goal: AgendaGoalSnapshot,
    *,
    context: AgendaContext,
    now: datetime,
    created_at: datetime,
    last_service_at: datetime | None,
) -> ScoreComponents:
    waited_since = _optional_datetime(goal.wait_started_at, "wait_started_at")
    waiting_minutes = _elapsed_minutes(now, waited_since) if waited_since else 0
    starvation_minutes = _elapsed_minutes(now, last_service_at or created_at)
    same_known_application = (
        context.current_application_hint is not None
        and goal.application_hint is not None
        and context.current_application_hint == goal.application_hint
    )
    different_known_application = (
        context.current_application_hint is not None
        and goal.application_hint is not None
        and not same_known_application
    )
    return ScoreComponents(
        user_priority=_clamp(goal.explicit_priority or 0, -1_000, 1_000),
        event_urgency=(
            _event_urgency_value(context.event_urgency)
            if goal.goal_id in context.event_goal_ids
            else 0
        ),
        waiting_age=min(waiting_minutes, 720),
        starvation=min(starvation_minutes, 1_440),
        continuity=(
            250
            if goal.goal_id == context.active_goal_id
            and (
                _goal_status(goal.status) is GoalNodeStatus.ACTIVE
                or goal.continuation_resumable
            )
            else 0
        ),
        app_switch_cost=100 if different_known_application else 0,
        backoff=0,
        recent_failure=min(max(goal.consecutive_failure_count, 0), 5) * 200,
    )


def _hard_tier(
    goal: AgendaGoalSnapshot,
    *,
    context: AgendaContext,
    now: datetime,
    created_at: datetime,
    last_service_at: datetime | None,
    scheduling_class: GoalSchedulingClass,
) -> AttentionHardTier:
    if goal.goal_id in context.latest_directive_goal_ids:
        return AttentionHardTier.LATEST_USER_DIRECTIVE
    if (
        goal.goal_id in context.event_goal_ids
        and _event_urgency_value(context.event_urgency) >= EventUrgency.CRITICAL
    ):
        return AttentionHardTier.CRITICAL_EVENT
    if (
        scheduling_class is GoalSchedulingClass.NORMAL
        and (now - (last_service_at or created_at)).total_seconds()
        >= HARD_STARVATION_SECONDS
    ):
        return AttentionHardTier.STARVATION_DEADLINE
    if scheduling_class is GoalSchedulingClass.NORMAL:
        return AttentionHardTier.NORMAL
    return AttentionHardTier.IDLE_ONLY


def _defer_idle(item: _AgendaItem, *, context: AgendaContext) -> _AgendaItem:
    if (
        item.eligible
        and item.eligibility.scheduling_class is GoalSchedulingClass.IDLE_ONLY
        and item.eligibility.goal_id not in context.latest_directive_goal_ids
        and not (
            item.eligibility.goal_id in context.event_goal_ids
            and _event_urgency_value(context.event_urgency)
            >= EventUrgency.CRITICAL
        )
    ):
        return replace(
            item,
            eligibility=replace(
                item.eligibility,
                eligibility=GoalEligibilityStatus.IDLE_DEFERRED,
                reason="normal eligible goals exist",
                hard_tier=0,
            ),
        )
    return item


def _ranking_key(item: _AgendaItem) -> tuple[Any, ...]:
    earliest = "0001-01-01T00:00:00.000000Z"
    return (
        -item.eligibility.hard_tier,
        -item.eligibility.total_score,
        item.last_service_at or earliest,
        item.created_at,
        item.eligibility.goal_id,
    )


def _goal_status(value: GoalNodeStatus | str) -> GoalNodeStatus:
    if isinstance(value, GoalNodeStatus):
        return value
    raw = value.value if isinstance(value, Enum) else str(value)
    return GoalNodeStatus(raw)


def _scheduling_class(value: GoalSchedulingClass | str) -> GoalSchedulingClass:
    if isinstance(value, GoalSchedulingClass):
        return value
    raw = value.value if isinstance(value, Enum) else str(value)
    return GoalSchedulingClass(raw)


def _event_urgency_value(value: EventUrgency | str | int) -> int:
    if isinstance(value, EventUrgency):
        return int(value)
    if isinstance(value, str):
        return int(EventUrgency[value.upper()])
    integer = int(value)
    if integer not in {int(item) for item in EventUrgency}:
        raise ValueError(f"unsupported event urgency: {integer}")
    return integer


def _latest_optional_datetime(*values: str | datetime | None) -> datetime | None:
    parsed = tuple(
        _as_utc_datetime(value, "eligibility timestamp")
        for value in values
        if value is not None
    )
    return max(parsed) if parsed else None


def _optional_datetime(value: str | datetime | None, label: str) -> datetime | None:
    return None if value is None else _as_utc_datetime(value, label)


def _as_utc_datetime(value: str | datetime, label: str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be in UTC")
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _elapsed_minutes(now: datetime, since: datetime) -> int:
    return max(0, int((now - since).total_seconds() // 60))


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))
