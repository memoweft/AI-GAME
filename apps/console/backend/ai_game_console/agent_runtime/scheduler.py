from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from datetime import timedelta
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Protocol

from .agenda import (
    AgendaContext,
    AgendaEvaluation,
    AgendaGoalSnapshot,
    evaluate_agenda,
)
from .domain import (
    AttentionDecisionDraft,
    AttentionDecisionOutcome,
    AttentionSelectorKind,
    GoalEligibilityDraft,
    SliceBudgetDraft,
)


MODEL_SCORE_BAND = 100
"""Qwen may choose only within 100 points of the deterministic tier leader."""


class AttentionEvaluationScope(str, Enum):
    """R7 forbids a continuation stack from replacing agenda evaluation."""

    ALL_ELIGIBLE_GOALS = "ALL_ELIGIBLE_GOALS"


@dataclass(frozen=True, slots=True)
class AttentionCandidateDetail:
    goal_id: str
    title: str | None
    original_fragment: str | None
    application_hint: str | None
    progress_summary: str | None
    continuation_summary: str | None


@dataclass(frozen=True, slots=True)
class AttentionSelectionContext:
    session_id: str
    trigger_key: str
    original_instruction: str
    authority_revision: int
    active_goal_id: str | None
    current_application_hint: str | None
    current_time: str
    trigger_summary: str | None
    candidates: tuple[GoalEligibilityDraft, ...]
    candidate_details: tuple[AttentionCandidateDetail, ...]


@dataclass(frozen=True, slots=True)
class AttentionSelectionDraft:
    selected_goal_id: str
    reason: str
    selector_kind: AttentionSelectorKind
    slice_budget: SliceBudgetDraft = SliceBudgetDraft()

    def __post_init__(self) -> None:
        if not self.selected_goal_id.strip():
            raise ValueError("selected_goal_id must not be blank")
        if not self.reason.strip():
            raise ValueError("attention selection reason must not be blank")


class AttentionSelector(Protocol):
    """Bounded final-selection port; eligibility never belongs to the model."""

    def select(self, context: AttentionSelectionContext) -> AttentionSelectionDraft:
        ...


class DeterministicAttentionSelector:
    def select(self, context: AttentionSelectionContext) -> AttentionSelectionDraft:
        if not context.candidates:
            raise ValueError("deterministic selector requires at least one candidate")
        selected = context.candidates[0]
        return AttentionSelectionDraft(
            selected_goal_id=selected.goal_id,
            reason=(
                f"deterministic rank 1: tier {selected.hard_tier}, "
                f"score {selected.total_score}"
            ),
            selector_kind=AttentionSelectorKind.DETERMINISTIC,
        )


class TransportFallbackAttentionSelector:
    """Fallback only on explicitly declared model transport exceptions.

    Invalid JSON/schema, an empty reason, an out-of-shortlist Goal and an
    invalid budget intentionally escape.  Treating those as transport failure
    would let a broken model contract silently change scheduling semantics.
    """

    def __init__(
        self,
        primary: AttentionSelector,
        *,
        transport_errors: tuple[type[BaseException], ...],
        fallback: AttentionSelector | None = None,
    ) -> None:
        if not transport_errors:
            raise ValueError("transport_errors must not be empty")
        self._primary = primary
        self._transport_errors = transport_errors
        self._fallback = fallback or DeterministicAttentionSelector()

    def select(self, context: AttentionSelectionContext) -> AttentionSelectionDraft:
        try:
            return self._primary.select(context)
        except self._transport_errors:
            return self._fallback.select(context)


@dataclass(frozen=True, slots=True)
class AttentionDecisionPlan:
    session_id: str
    trigger_key: str
    evaluation: AgendaEvaluation
    selected: GoalEligibilityDraft | None
    selector_kind: AttentionSelectorKind
    reason: str
    model_adjustment: int
    slice_budget: SliceBudgetDraft
    trigger_event_id: str | None = None
    prior_goal_id: str | None = None
    evaluation_scope: AttentionEvaluationScope = (
        AttentionEvaluationScope.ALL_ELIGIBLE_GOALS
    )

    @property
    def selected_goal_id(self) -> str | None:
        return self.selected.goal_id if self.selected is not None else None

    @property
    def shortlist_goal_ids(self) -> tuple[str, ...]:
        if not self.evaluation.candidates:
            return ()
        tier = self.evaluation.candidates[0].hard_tier
        top_score = self.evaluation.candidates[0].total_score
        return tuple(
            item.goal_id
            for item in self.evaluation.candidates
            if item.hard_tier == tier
            and item.total_score >= top_score - MODEL_SCORE_BAND
        )

    @property
    def evaluated_goal_ids(self) -> tuple[str, ...]:
        """Every Goal considered, including waiting and terminal Goals."""

        return tuple(item.goal_id for item in self.evaluation.all_goals)

    @property
    def requires_new_decision(self) -> bool:
        """An event route is a scheduling request, never a direct switch."""

        return self.trigger_event_id is not None

    @property
    def is_preemption_candidate(self) -> bool:
        """Whether the not-yet-committed plan proposes leaving prior work."""

        return (
            self.prior_goal_id is not None
            and self.selected_goal_id is not None
            and self.selected_goal_id != self.prior_goal_id
        )

    @property
    def forces_prior_goal_return(self) -> bool:
        """R7 Continuations are candidates, never a LIFO return stack."""

        return False

    def to_decision_draft(
        self,
        *,
        authority_revision: int,
        graph_revision: int,
        event_cursor: int,
        agenda_revision: int | None = None,
        trigger_event_id: str | None = None,
        preemption_checkpoint_ref: str | None = None,
    ) -> AttentionDecisionDraft:
        if (
            trigger_event_id is not None
            and self.trigger_event_id is not None
            and trigger_event_id != self.trigger_event_id
        ):
            raise ValueError("trigger_event_id conflicts with the scheduling plan")
        effective_trigger_event_id = trigger_event_id or self.trigger_event_id
        selected = self.selected
        return AttentionDecisionDraft(
            trigger_key=self.trigger_key,
            authority_revision=authority_revision,
            graph_revision=graph_revision,
            event_cursor=event_cursor,
            agenda_revision=agenda_revision,
            outcome=(
                AttentionDecisionOutcome.SELECTED
                if selected is not None
                else AttentionDecisionOutcome.NO_ELIGIBLE
            ),
            selected_goal_id=self.selected_goal_id,
            selector_kind=self.selector_kind,
            reason=self.reason,
            trigger_event_id=effective_trigger_event_id,
            selected_hard_tier=(selected.hard_tier if selected else None),
            user_priority_component=(
                selected.user_priority_component if selected else 0
            ),
            event_urgency_component=(
                selected.event_urgency_component if selected else 0
            ),
            waiting_age_component=(
                selected.waiting_age_component if selected else 0
            ),
            starvation_component=(selected.starvation_component if selected else 0),
            continuity_component=(selected.continuity_component if selected else 0),
            app_switch_cost=(selected.app_switch_cost if selected else 0),
            backoff_component=(selected.backoff_component if selected else 0),
            recent_failure_component=(
                selected.recent_failure_component if selected else 0
            ),
            base_score=(selected.total_score if selected else 0),
            model_adjustment=self.model_adjustment,
            slice_budget=self.slice_budget,
            preemption_checkpoint_ref=preemption_checkpoint_ref,
        )


class AttentionScheduler:
    """Pure R3 scheduler facade used before any Goal execution dispatch."""

    def __init__(self, selector: AttentionSelector | None = None) -> None:
        self._selector = selector or DeterministicAttentionSelector()

    def request_attention(
        self,
        *,
        session_id: str,
        trigger_key: str,
        original_instruction: str,
        authority_revision: int,
        goals: Iterable[AgendaGoalSnapshot],
        context: AgendaContext,
        now: datetime,
        trigger_summary: str | None = None,
        trigger_event_id: str | None = None,
        prior_goal_id: str | None = None,
    ) -> AttentionDecisionPlan:
        if not session_id.strip():
            raise ValueError("session_id must not be blank")
        if not trigger_key.strip():
            raise ValueError("trigger_key must not be blank")
        if authority_revision < 1:
            raise ValueError("authority_revision must be positive")
        if trigger_event_id is not None and not trigger_event_id.strip():
            raise ValueError("trigger_event_id must not be blank")
        if prior_goal_id is not None and not prior_goal_id.strip():
            raise ValueError("prior_goal_id must not be blank")
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("now must be in UTC")

        goal_snapshots = tuple(goals)
        goal_ids = tuple(item.goal_id for item in goal_snapshots)
        if len(goal_ids) != len(set(goal_ids)):
            raise ValueError("agenda contains duplicate Goal ids")
        if prior_goal_id is not None and prior_goal_id not in set(goal_ids):
            raise ValueError("prior Goal must remain in the full agenda evaluation")
        evaluation = evaluate_agenda(goal_snapshots, context=context, now=now)
        if not evaluation.candidates:
            return AttentionDecisionPlan(
                session_id=session_id,
                trigger_key=trigger_key,
                evaluation=evaluation,
                selected=None,
                selector_kind=AttentionSelectorKind.DETERMINISTIC,
                reason="no eligible Goal; all goals are waiting, blocked, or terminal",
                model_adjustment=0,
                slice_budget=SliceBudgetDraft(),
                trigger_event_id=trigger_event_id,
                prior_goal_id=prior_goal_id,
            )

        shortlist = _shortlist(evaluation.candidates)
        snapshots_by_id = {item.goal_id: item for item in goal_snapshots}
        selection_context = AttentionSelectionContext(
            session_id=session_id,
            trigger_key=trigger_key,
            original_instruction=original_instruction,
            authority_revision=authority_revision,
            active_goal_id=context.active_goal_id,
            current_application_hint=context.current_application_hint,
            current_time=now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            trigger_summary=trigger_summary,
            candidates=shortlist,
            candidate_details=tuple(
                AttentionCandidateDetail(
                    goal_id=item.goal_id,
                    title=snapshots_by_id[item.goal_id].title,
                    original_fragment=snapshots_by_id[item.goal_id].original_fragment,
                    application_hint=snapshots_by_id[item.goal_id].application_hint,
                    progress_summary=snapshots_by_id[item.goal_id].progress_summary,
                    continuation_summary=snapshots_by_id[item.goal_id].continuation_summary,
                )
                for item in shortlist
            ),
        )
        selection = self._selector.select(selection_context)
        selected = next(
            (item for item in shortlist if item.goal_id == selection.selected_goal_id),
            None,
        )
        if selected is None:
            allowed = ", ".join(item.goal_id for item in shortlist)
            raise ValueError(
                f"attention selector chose Goal outside bounded shortlist: "
                f"{selection.selected_goal_id}; allowed: {allowed}"
            )
        top_score = shortlist[0].total_score
        return AttentionDecisionPlan(
            session_id=session_id,
            trigger_key=trigger_key,
            evaluation=evaluation,
            selected=selected,
            selector_kind=selection.selector_kind,
            reason=selection.reason,
            model_adjustment=selected.total_score - top_score,
            slice_budget=selection.slice_budget,
            trigger_event_id=trigger_event_id,
            prior_goal_id=prior_goal_id,
        )

    def request_event_attention(
        self,
        *,
        session_id: str,
        trigger_event_id: str,
        original_instruction: str,
        authority_revision: int,
        goals: Iterable[AgendaGoalSnapshot],
        context: AgendaContext,
        now: datetime,
        prior_goal_id: str | None = None,
        trigger_summary: str | None = None,
    ) -> AttentionDecisionPlan:
        """Re-evaluate the complete agenda for one newly routed event.

        Callers must not invoke this method for duplicate-inert routing
        results.  The event identity creates a stable decision trigger; the
        optional prior Goal is retained only as auditable context and receives
        no LIFO promotion.
        """

        if not trigger_event_id.strip():
            raise ValueError("trigger_event_id must not be blank")
        return self.request_attention(
            session_id=session_id,
            trigger_key=f"event:{trigger_event_id}",
            original_instruction=original_instruction,
            authority_revision=authority_revision,
            goals=goals,
            context=context,
            now=now,
            trigger_summary=trigger_summary,
            trigger_event_id=trigger_event_id,
            prior_goal_id=prior_goal_id,
        )

    def recover(
        self,
        *,
        session_id: str,
        original_instruction: str,
        authority_revision: int,
        graph_revision: int,
        event_cursor: int,
        agenda_revision: int,
        goals: Iterable[AgendaGoalSnapshot],
        context: AgendaContext,
        now: datetime,
    ) -> AttentionDecisionPlan:
        """Build the stable scheduler-recovery trigger used by service startup."""

        trigger_key = (
            f"recovery:{session_id}:{authority_revision}:"
            f"{graph_revision}:{event_cursor}:{agenda_revision}"
        )
        return self.request_attention(
            session_id=session_id,
            trigger_key=trigger_key,
            original_instruction=original_instruction,
            authority_revision=authority_revision,
            goals=goals,
            context=context,
            now=now,
        )


def _shortlist(
    candidates: tuple[GoalEligibilityDraft, ...],
) -> tuple[GoalEligibilityDraft, ...]:
    top = candidates[0]
    return tuple(
        item
        for item in candidates
        if item.hard_tier == top.hard_tier
        and item.total_score >= top.total_score - MODEL_SCORE_BAND
    )


# The classes below deliberately sit beside the pure Agenda scheduler rather
# than replacing it.  AgentRuntime remains the canonical Task/event authority;
# this small SQLite ledger owns only scheduler coordination facts (lease and
# delivery attempts).  It lets two local launcher instances converge without
# inventing a second Task state machine.


class OverlapPolicy(str, Enum):
    """The only supported recurring-wake overlap policies.

    ``ALLOW_ALL`` is intentionally absent.  Concurrent buffered device work
    would defeat the single-writer and verified-action boundaries.
    """

    SKIP = "skip"
    BUFFER_ONE = "buffer_one"


@dataclass(frozen=True, slots=True)
class SchedulerLease:
    owner_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WakeDispatch:
    wake_id: str
    task_id: str
    task_revision: int
    due_at: str
    state: str


class SQLiteSchedulerCoordination:
    """Durable local lease and bounded wake-delivery coordination.

    This table is deliberately projection-free: it cannot change a Task,
    event cursor, revision, or control state.  All of those remain calls to
    Package B's owner-scoped canonical service.
    """

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._initialize()

    def acquire_lease(
        self, *, owner_id: str, now: datetime, ttl: timedelta
    ) -> bool:
        if not owner_id.strip():
            raise ValueError("scheduler owner_id must not be blank")
        if ttl <= timedelta(0):
            raise ValueError("scheduler lease ttl must be positive")
        now = _utc(now)
        expires = now + ttl
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT owner_id, expires_at FROM long_task_scheduler_lease WHERE lease_name='default'"
            ).fetchone()
            if row is not None:
                held_until = _parse_utc(str(row["expires_at"]))
                if row["owner_id"] != owner_id and held_until > now:
                    return False
            connection.execute(
                "INSERT INTO long_task_scheduler_lease(lease_name, owner_id, expires_at) VALUES ('default', ?, ?) "
                "ON CONFLICT(lease_name) DO UPDATE SET owner_id=excluded.owner_id, expires_at=excluded.expires_at",
                (owner_id, _format_utc(expires)),
            )
            return True

    def release_lease(self, *, owner_id: str) -> None:
        with self._connection(write=True) as connection:
            connection.execute(
                "DELETE FROM long_task_scheduler_lease WHERE lease_name='default' AND owner_id=?",
                (owner_id,),
            )

    def claim_wake(
        self, wake: WakeDispatch, *, overlap_policy: OverlapPolicy
    ) -> str:
        """Return ``dispatch``, ``buffered`` or ``skipped`` exactly once."""

        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT state FROM long_task_scheduler_wakes WHERE wake_id=?",
                (wake.wake_id,),
            ).fetchone()
            if existing is not None:
                return "duplicate"
            active = connection.execute(
                "SELECT wake_id FROM long_task_scheduler_wakes WHERE task_id=? AND state='active'",
                (wake.task_id,),
            ).fetchone()
            if active is None:
                state = "active"
                outcome = "dispatch"
            elif overlap_policy is OverlapPolicy.BUFFER_ONE:
                buffered = connection.execute(
                    "SELECT wake_id FROM long_task_scheduler_wakes WHERE task_id=? AND state='buffered'",
                    (wake.task_id,),
                ).fetchone()
                state = "buffered" if buffered is None else "skipped"
                outcome = "buffered" if buffered is None else "skipped"
            else:
                state = "skipped"
                outcome = "skipped"
            connection.execute(
                "INSERT INTO long_task_scheduler_wakes(wake_id, task_id, task_revision, due_at, state) VALUES (?, ?, ?, ?, ?)",
                (wake.wake_id, wake.task_id, wake.task_revision, wake.due_at, state),
            )
            return outcome

    def active_wake(self, *, task_id: str) -> WakeDispatch | None:
        """Return the exact active wake for one Task, if any."""

        with self._connection(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM long_task_scheduler_wakes "
                "WHERE task_id=? AND state='active' ORDER BY due_at, wake_id LIMIT 1",
                (task_id,),
            ).fetchone()
            return _wake(row, state="active") if row is not None else None

    def resume_active_wake(self, wake: WakeDispatch) -> bool:
        """Resume only an exact durable active wake after a process restart."""

        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM long_task_scheduler_wakes WHERE wake_id=? AND state='active'",
                (wake.wake_id,),
            ).fetchone()
            return bool(
                row is not None
                and str(row["task_id"]) == wake.task_id
                and int(row["task_revision"]) == wake.task_revision
                and str(row["due_at"]) == wake.due_at
            )

    def complete_wake(self, *, wake_id: str) -> WakeDispatch | None:
        """Settle one active run and promote at most one buffered wake."""

        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM long_task_scheduler_wakes WHERE wake_id=?", (wake_id,)
            ).fetchone()
            if row is None:
                return None
            if row["state"] == "active":
                connection.execute(
                    "UPDATE long_task_scheduler_wakes SET state='completed' WHERE wake_id=?",
                    (wake_id,),
                )
                buffered = connection.execute(
                    "SELECT * FROM long_task_scheduler_wakes WHERE task_id=? AND state='buffered' ORDER BY due_at, wake_id LIMIT 1",
                    (row["task_id"],),
                ).fetchone()
                if buffered is not None:
                    connection.execute(
                        "UPDATE long_task_scheduler_wakes SET state='active' WHERE wake_id=?",
                        (buffered["wake_id"],),
                    )
                    return _wake(buffered, state="active")
            return None

    def reclaim_unprojected_wake(self, wake: WakeDispatch) -> bool:
        """Reclaim a crash window before canonical Task projection commits.

        A scheduler records its active wake before it writes the canonical
        ``waiting_time -> running`` transition.  If it dies in that tiny
        window, the Task is still ``waiting_time`` on restart and therefore
        proves no runner was handed the wake.  Reusing the same stable wake
        identity is safe; an already projected/running Task never calls this.
        """

        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT task_id, state FROM long_task_scheduler_wakes WHERE wake_id=?",
                (wake.wake_id,),
            ).fetchone()
            return bool(
                row is not None
                and row["task_id"] == wake.task_id
                and row["state"] == "active"
            )

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS long_task_scheduler_lease (lease_name TEXT PRIMARY KEY, owner_id TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS long_task_scheduler_wakes (wake_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_revision INTEGER NOT NULL, due_at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','buffered','skipped','completed')))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_long_task_scheduler_wakes_task_state ON long_task_scheduler_wakes(task_id, state, due_at)"
            )

    def _connection(self, *, write: bool) -> Any:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute("BEGIN IMMEDIATE")
        return _SchedulerConnection(connection)


class _SchedulerConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        if kind is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()


class LongTaskScheduler:
    """Lease-owned, restart-safe delivery of canonical Task time wakes.

    ``task_service`` is intentionally structural: Package B's
    ``CanonicalTaskService`` provides ``list_tasks``, ``inspect_task``,
    ``transition_task`` and ``apply_revision_boundary``.  The scheduler never
    reaches into AgentRuntime tables or manufactures an event history.
    """

    def __init__(
        self,
        task_service: Any,
        coordination: SQLiteSchedulerCoordination,
        *,
        owner_id: str,
        dispatch: Callable[[dict[str, Any], WakeDispatch], None],
        fresh_replan_gate: Callable[[str, int], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
        overlap_policy: OverlapPolicy | str = OverlapPolicy.SKIP,
    ) -> None:
        self.task_service = task_service
        self.coordination = coordination
        self.owner_id = owner_id
        self.dispatch = dispatch
        self.fresh_replan_gate = fresh_replan_gate or (lambda _task_id, _revision: True)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_ttl = lease_ttl
        try:
            self.overlap_policy = OverlapPolicy(overlap_policy)
        except ValueError as error:
            raise ValueError("overlap_policy must be skip or buffer_one") from error

    def poll_once(self) -> int:
        now = _utc(self.clock())
        if not self.coordination.acquire_lease(
            owner_id=self.owner_id, now=now, ttl=self.lease_ttl
        ):
            return 0
        delivered = 0
        for task in self.task_service.list_tasks(limit=1000, include_archived=False):
            if not _task_is_due(task, now):
                continue
            wake = _task_wake(task)
            outcome = self.coordination.claim_wake(
                wake, overlap_policy=self.overlap_policy
            )
            if outcome == "duplicate" and self.coordination.reclaim_unprojected_wake(wake):
                outcome = "dispatch"
            if outcome != "dispatch":
                continue
            # This writes a canonical TaskStateChanged event using a stable
            # key.  A process death after it commits cannot create a second
            # wake projection on restart.
            self.task_service.transition_task(
                wake.task_id,
                status="running",
                reason_code="timer_due",
                summary="Timer wake reached its due time; resuming at a fresh planning boundary.",
                recoverable=True,
                idempotency_key=f"scheduler:{wake.wake_id}:running",
            )
            refreshed = self.task_service.inspect_task(wake.task_id)
            revision = int(refreshed["current_revision"])
            if not self.fresh_replan_gate(wake.task_id, revision):
                continue
            self.task_service.apply_revision_boundary(wake.task_id)
            try:
                self.dispatch(refreshed, wake)
            except Exception:
                # A scheduler/runner failure is ordinary operational evidence,
                # not a fabricated terminal Task result.  Do not persist the
                # raw exception: it may contain unsafe transport/device text.
                self.task_service.ordinary_failure(
                    wake.task_id,
                    reason_code="scheduler_dispatch_failed",
                    summary="The scheduled wake could not start; replanning is required.",
                    idempotency_key=f"scheduler:{wake.wake_id}:dispatch-failed",
                    replan=True,
                )
                self.complete(wake.wake_id)
                continue
            delivered += 1
        return delivered

    def complete(self, wake_id: str) -> int:
        """Promote a single buffered wake and dispatch it if its gate passes."""

        promoted = self.coordination.complete_wake(wake_id=wake_id)
        if promoted is None:
            return 0
        task = self.task_service.inspect_task(promoted.task_id)
        revision = int(task["current_revision"])
        if task.get("terminal") or _task_dispatch_blocked(task) or not self.fresh_replan_gate(promoted.task_id, revision):
            return 0
        self.task_service.apply_revision_boundary(promoted.task_id)
        self.dispatch(task, promoted)
        return 1


def stable_wake_id(task_id: str, revision: int, due_at: str) -> str:
    if not task_id.strip() or revision < 1 or not due_at.strip():
        raise ValueError("stable wake identity requires task_id, revision and due_at")
    return f"task:{task_id}:revision:{revision}:due:{due_at}"


def _task_wake(task: dict[str, Any]) -> WakeDispatch:
    due_at = str(task["next_wake_at"])
    task_id = str(task.get("task_id") or task["id"])
    revision = int(task["current_revision"])
    return WakeDispatch(
        wake_id=stable_wake_id(task_id, revision, due_at), task_id=task_id,
        task_revision=revision, due_at=due_at, state="active",
    )


def _task_is_due(task: dict[str, Any], now: datetime) -> bool:
    return (
        task.get("status") == "waiting_time"
        and not bool(task.get("terminal"))
        and not _task_dispatch_blocked(task)
        and task.get("next_wake_at") is not None
        and _parse_utc(str(task["next_wake_at"])) <= now
    )


def _task_dispatch_blocked(task: dict[str, Any]) -> bool:
    return task.get("status") in {"paused", "user_takeover"}


def _wake(row: sqlite3.Row, *, state: str) -> WakeDispatch:
    return WakeDispatch(
        wake_id=str(row["wake_id"]), task_id=str(row["task_id"]),
        task_revision=int(row["task_revision"]), due_at=str(row["due_at"]), state=state,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("scheduler clock must be timezone-aware UTC")
    return value.astimezone(UTC)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("scheduler timestamp must include timezone")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
