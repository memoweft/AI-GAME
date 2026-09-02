"""Independent SQLite ledger for R6 ActivitySlice facts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .activity_slice import (
    ActivitySlice,
    ActivitySliceConflict,
    ActivitySliceStatus,
    ActivityStep,
    ActivityStepPhase,
    ActivityStepStatus,
    BoundarySnapshot,
    ContinuationDirective,
    SliceBudget,
    SliceYieldReason,
    StepExecutionResult,
    StepOutcome,
    StepProgress,
    WakePlan,
    WakePlanKind,
    WakePlanStatus,
    deterministic_id,
    phase_precedes,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_slices (
    slice_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    start_fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    attention_decision_id TEXT NOT NULL,
    attention_decision_revision INTEGER NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    time_budget_seconds REAL NOT NULL,
    action_limit INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    action_count INTEGER NOT NULL DEFAULT 0,
    next_step_ordinal INTEGER NOT NULL DEFAULT 1,
    last_step_id TEXT,
    observed_directive_revision INTEGER NOT NULL,
    observed_event_cursor INTEGER NOT NULL,
    yield_reason TEXT,
    continuation_key TEXT,
    continuation_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_slices_recovery
ON activity_slices(status, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_running_activity_slice_per_session
ON activity_slices(session_id) WHERE status='RUNNING';

CREATE TABLE IF NOT EXISTS activity_steps (
    step_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    slice_id TEXT NOT NULL REFERENCES activity_slices(slice_id),
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    boundary_json TEXT NOT NULL,
    before_observation_ref TEXT NOT NULL,
    outcome TEXT,
    physical_action_ref TEXT,
    action_receipt_ref TEXT,
    after_observation_ref TEXT,
    verification_ref TEXT,
    verified INTEGER,
    decision_json TEXT,
    result_facts_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT,
    UNIQUE(slice_id, ordinal),
    UNIQUE(slice_id, before_observation_ref)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_steps_action_once
ON activity_steps(physical_action_ref) WHERE physical_action_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS wake_plans (
    wake_plan_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    slice_id TEXT NOT NULL REFERENCES activity_slices(slice_id),
    step_id TEXT NOT NULL REFERENCES activity_steps(step_id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    matcher_json TEXT,
    due_at TEXT,
    user_fact_key TEXT,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wake_plans_slice
ON wake_plans(slice_id, created_at DESC);
"""


class ActivitySliceStore:
    """Small R6 store whose rows point at, but never replace, other ledgers."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.initialize()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def create_slice(
        self,
        *,
        session_id: str,
        goal_id: str,
        attention_decision_id: str,
        attention_decision_revision: int,
        objective: str,
        budget: SliceBudget,
        directive_revision: int,
        event_cursor: int,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[ActivitySlice, bool]:
        now_text = _iso(now)
        fingerprint = _json(
            {
                "session_id": session_id,
                "goal_id": goal_id,
                "attention_decision_id": attention_decision_id,
                "attention_decision_revision": attention_decision_revision,
                "objective": objective,
                "budget": {
                    "time_budget_seconds": budget.time_budget_seconds,
                    "action_limit": budget.action_limit,
                },
                "directive_revision": directive_revision,
                "event_cursor": event_cursor,
            }
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM activity_slices WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["start_fingerprint"]) != fingerprint:
                    raise ActivitySliceConflict(
                        "slice idempotency_key was reused for different start facts"
                    )
                return _slice(existing), False
            slice_id = deterministic_id("activity-slice", idempotency_key)
            deadline_at = _iso(now + timedelta(seconds=budget.time_budget_seconds))
            connection.execute(
                "INSERT INTO activity_slices("
                "slice_id,idempotency_key,start_fingerprint,session_id,goal_id,"
                "attention_decision_id,attention_decision_revision,objective,status,"
                "time_budget_seconds,action_limit,started_at,deadline_at,action_count,"
                "next_step_ordinal,observed_directive_revision,observed_event_cursor,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?,?)",
                (
                    slice_id,
                    idempotency_key,
                    fingerprint,
                    session_id,
                    goal_id,
                    attention_decision_id,
                    attention_decision_revision,
                    objective,
                    ActivitySliceStatus.RUNNING.value,
                    budget.time_budget_seconds,
                    budget.action_limit,
                    now_text,
                    deadline_at,
                    directive_revision,
                    event_cursor,
                    now_text,
                    now_text,
                ),
            )
            row = connection.execute(
                "SELECT * FROM activity_slices WHERE slice_id=?", (slice_id,)
            ).fetchone()
            return _slice(row), True

    def get_slice(self, slice_id: str) -> ActivitySlice:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM activity_slices WHERE slice_id=?", (slice_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown ActivitySlice {slice_id}")
            return _slice(row)

    def running_slices(self) -> tuple[ActivitySlice, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM activity_slices WHERE status=? ORDER BY created_at,slice_id",
                (ActivitySliceStatus.RUNNING.value,),
            ).fetchall()
            return tuple(_slice(row) for row in rows)

    def list_steps(self, slice_id: str) -> tuple[ActivityStep, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM activity_steps WHERE slice_id=? ORDER BY ordinal",
                (slice_id,),
            ).fetchall()
            return tuple(_step(row) for row in rows)

    def pending_step(self, slice_id: str) -> ActivityStep | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM activity_steps WHERE slice_id=? AND status<>? "
                "ORDER BY ordinal DESC LIMIT 1",
                (slice_id, ActivityStepStatus.SETTLED.value),
            ).fetchone()
            return _step(row) if row is not None else None

    def begin_step(
        self, slice_id: str, boundary: BoundarySnapshot, *, now: datetime
    ) -> ActivityStep:
        now_text = _iso(now)
        with self._connect() as connection:
            slice_row = self._slice_row(connection, slice_id)
            activity_slice = _slice(slice_row)
            if activity_slice.status is not ActivitySliceStatus.RUNNING:
                raise ActivitySliceConflict("cannot begin a step for a non-running slice")
            existing_pending = connection.execute(
                "SELECT * FROM activity_steps WHERE slice_id=? AND status<>? LIMIT 1",
                (slice_id, ActivityStepStatus.SETTLED.value),
            ).fetchone()
            if existing_pending is not None:
                return _step(existing_pending)
            reused = connection.execute(
                "SELECT step_id FROM activity_steps WHERE slice_id=? AND "
                "(before_observation_ref=? OR after_observation_ref=?) LIMIT 1",
                (
                    slice_id,
                    boundary.fresh_observation_ref,
                    boundary.fresh_observation_ref,
                ),
            ).fetchone()
            if reused is not None:
                raise ActivitySliceConflict(
                    "a later ActivityStep cannot reuse an earlier observation"
                )
            ordinal = activity_slice.next_step_ordinal
            key = f"{activity_slice.idempotency_key}:step:{ordinal}"
            step_id = deterministic_id("activity-step", key)
            connection.execute(
                "INSERT INTO activity_steps("
                "step_id,idempotency_key,slice_id,ordinal,status,phase,boundary_json,"
                "before_observation_ref,result_facts_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    step_id,
                    key,
                    slice_id,
                    ordinal,
                    ActivityStepStatus.PREPARED.value,
                    ActivityStepPhase.OBSERVATION_CAPTURED.value,
                    _json(_boundary_payload(boundary)),
                    boundary.fresh_observation_ref,
                    "{}",
                    now_text,
                    now_text,
                ),
            )
            connection.execute(
                "UPDATE activity_slices SET observed_directive_revision=?,"
                "observed_event_cursor=?,updated_at=? WHERE slice_id=?",
                (
                    boundary.directive_revision,
                    boundary.event_cursor,
                    now_text,
                    slice_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM activity_steps WHERE step_id=?", (step_id,)
            ).fetchone()
            return _step(row)

    def record_step_progress(
        self, step_id: str, progress: StepProgress, *, now: datetime
    ) -> ActivityStep:
        now_text = _iso(now)
        with self._connect() as connection:
            row = self._step_row(connection, step_id)
            step = _step(row)
            if step.status is ActivityStepStatus.SETTLED:
                return step
            if phase_precedes(progress.phase, step.phase):
                self._assert_progress_compatible(step, progress)
                return step
            self._assert_progress_compatible(step, progress)
            physical_action_ref = progress.physical_action_ref or step.physical_action_ref
            action_receipt_ref = progress.action_receipt_ref or step.action_receipt_ref
            after_observation_ref = (
                progress.after_observation_ref or step.after_observation_ref
            )
            verification_ref = progress.verification_ref or step.verification_ref
            self._assert_observation_fresh(
                connection,
                step,
                after_observation_ref,
            )
            merged_facts = dict(step.result_facts)
            merged_facts.update(dict(progress.facts))
            try:
                connection.execute(
                    "UPDATE activity_steps SET status=?,phase=?,physical_action_ref=?,"
                    "action_receipt_ref=?,after_observation_ref=?,verification_ref=?,"
                    "result_facts_json=?,updated_at=? WHERE step_id=?",
                    (
                        ActivityStepStatus.EXECUTING.value,
                        progress.phase.value,
                        physical_action_ref,
                        action_receipt_ref,
                        after_observation_ref,
                        verification_ref,
                        _json(merged_facts),
                        now_text,
                        step_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ActivitySliceConflict(
                    "one Kernel physical action cannot belong to multiple ActivitySteps"
                ) from error
            updated = self._step_row(connection, step_id)
            return _step(updated)

    def settle_step(
        self, step_id: str, result: StepExecutionResult, *, now: datetime
    ) -> ActivityStep:
        now_text = _iso(now)
        result_payload = _result_payload(result)
        result_json = _json(result_payload)
        with self._connect() as connection:
            row = self._step_row(connection, step_id)
            step = _step(row)
            if step.status is ActivityStepStatus.SETTLED:
                if str(row["result_json"]) != result_json:
                    raise ActivitySliceConflict(
                        "settled step cannot be replayed with a different result"
                    )
                return step
            final_progress = StepProgress(
                phase=(
                    ActivityStepPhase.VERIFICATION_RECORDED
                    if result.verification_ref
                    else step.phase
                ),
                physical_action_ref=result.physical_action_ref,
                action_receipt_ref=result.action_receipt_ref,
                after_observation_ref=result.after_observation_ref,
                verification_ref=result.verification_ref,
            )
            self._assert_progress_compatible(step, final_progress)
            self._assert_observation_fresh(
                connection, step, result.after_observation_ref
            )
            facts = dict(step.result_facts)
            facts.update(dict(result.facts))
            if result.progress_fact is not None:
                facts["progress_fact"] = dict(result.progress_fact)
            if result.completion_proposal is not None:
                facts["completion_proposal"] = dict(result.completion_proposal)
                facts["completion_verified"] = result.completion_verified
            try:
                connection.execute(
                    "UPDATE activity_steps SET status=?,phase=?,outcome=?,"
                    "physical_action_ref=?,action_receipt_ref=?,after_observation_ref=?,"
                    "verification_ref=?,verified=?,decision_json=?,result_facts_json=?,"
                    "result_json=?,updated_at=?,settled_at=? WHERE step_id=?",
                    (
                        ActivityStepStatus.SETTLED.value,
                        ActivityStepPhase.SETTLED.value,
                        result.outcome.value,
                        result.physical_action_ref or step.physical_action_ref,
                        result.action_receipt_ref or step.action_receipt_ref,
                        result.after_observation_ref or step.after_observation_ref,
                        result.verification_ref or step.verification_ref,
                        None if result.verified is None else int(result.verified),
                        _json(dict(result.decision)),
                        _json(facts),
                        result_json,
                        now_text,
                        now_text,
                        step_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ActivitySliceConflict(
                    "one Kernel physical action cannot belong to multiple ActivitySteps"
                ) from error
            action_delta = 1 if result.physical_action_ref is not None else 0
            connection.execute(
                "UPDATE activity_slices SET action_count=action_count+?,"
                "next_step_ordinal=?,last_step_id=?,updated_at=? WHERE slice_id=?",
                (action_delta, step.ordinal + 1, step.id, now_text, step.slice_id),
            )
            return _step(self._step_row(connection, step_id))

    def create_wake_plan(
        self,
        step_id: str,
        *,
        kind: WakePlanKind,
        matcher: Mapping[str, Any] | None,
        due_at: str | None,
        user_fact_key: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[WakePlan, bool]:
        now_text = _iso(now)
        fingerprint = _json(
            {
                "step_id": step_id,
                "kind": kind.value,
                "matcher": dict(matcher) if matcher is not None else None,
                "due_at": due_at,
                "user_fact_key": user_fact_key,
            }
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM wake_plans WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["fingerprint"]) != fingerprint:
                    raise ActivitySliceConflict(
                        "wake idempotency_key was reused for different facts"
                    )
                return _wake(existing), False
            step = _step(self._step_row(connection, step_id))
            wake_id = deterministic_id("wake-plan", idempotency_key)
            connection.execute(
                "INSERT INTO wake_plans(wake_plan_id,idempotency_key,slice_id,step_id,"
                "kind,status,matcher_json,due_at,user_fact_key,fingerprint,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wake_id,
                    idempotency_key,
                    step.slice_id,
                    step.id,
                    kind.value,
                    WakePlanStatus.PENDING.value,
                    _json(dict(matcher)) if matcher is not None else None,
                    due_at,
                    user_fact_key,
                    fingerprint,
                    now_text,
                ),
            )
            row = connection.execute(
                "SELECT * FROM wake_plans WHERE wake_plan_id=?", (wake_id,)
            ).fetchone()
            wake = _wake(row)
            # Dataclass validation keeps malformed due/user-fact plans out.
            return wake, True

    def get_wake_plan_for_slice(self, slice_id: str) -> WakePlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM wake_plans WHERE slice_id=? ORDER BY created_at DESC LIMIT 1",
                (slice_id,),
            ).fetchone()
            return _wake(row) if row is not None else None

    def yield_slice(
        self,
        slice_id: str,
        *,
        status: ActivitySliceStatus,
        reason: SliceYieldReason,
        continuation: ContinuationDirective,
        boundary: BoundarySnapshot | None,
        now: datetime,
    ) -> ActivitySlice:
        if status not in {ActivitySliceStatus.WAITING, ActivitySliceStatus.YIELDED}:
            raise ValueError("yield status must be WAITING or YIELDED")
        now_text = _iso(now)
        continuation_json = _json(_continuation_payload(continuation))
        with self._connect() as connection:
            row = self._slice_row(connection, slice_id)
            current = _slice(row)
            if current.terminal:
                if (
                    current.status is status
                    and current.yield_reason is reason
                    and current.continuation_key == continuation.idempotency_key
                    and str(row["continuation_json"]) == continuation_json
                ):
                    return current
                raise ActivitySliceConflict(
                    "terminal slice cannot yield with different facts"
                )
            directive_revision = (
                boundary.directive_revision
                if boundary is not None
                else current.observed_directive_revision
            )
            event_cursor = (
                boundary.event_cursor
                if boundary is not None
                else current.observed_event_cursor
            )
            connection.execute(
                "UPDATE activity_slices SET status=?,yield_reason=?,continuation_key=?,"
                "continuation_json=?,observed_directive_revision=?,"
                "observed_event_cursor=?,updated_at=? WHERE slice_id=?",
                (
                    status.value,
                    reason.value,
                    continuation.idempotency_key,
                    continuation_json,
                    directive_revision,
                    event_cursor,
                    now_text,
                    slice_id,
                ),
            )
            return _slice(self._slice_row(connection, slice_id))

    def complete_slice(self, slice_id: str, *, now: datetime) -> ActivitySlice:
        with self._connect() as connection:
            current = _slice(self._slice_row(connection, slice_id))
            if current.status is ActivitySliceStatus.COMPLETED:
                return current
            if current.terminal:
                raise ActivitySliceConflict("terminal slice cannot be completed")
            connection.execute(
                "UPDATE activity_slices SET status=?,updated_at=? WHERE slice_id=?",
                (ActivitySliceStatus.COMPLETED.value, _iso(now), slice_id),
            )
            return _slice(self._slice_row(connection, slice_id))

    def continuation(self, slice_id: str) -> ContinuationDirective | None:
        with self._connect() as connection:
            row = self._slice_row(connection, slice_id)
            value = row["continuation_json"]
            if value is None:
                return None
            return _continuation(json.loads(str(value)))

    def _assert_progress_compatible(
        self, step: ActivityStep, progress: StepProgress
    ) -> None:
        for current, incoming, label in (
            (step.physical_action_ref, progress.physical_action_ref, "physical_action_ref"),
            (step.action_receipt_ref, progress.action_receipt_ref, "action_receipt_ref"),
            (step.after_observation_ref, progress.after_observation_ref, "after_observation_ref"),
            (step.verification_ref, progress.verification_ref, "verification_ref"),
        ):
            if current is not None and incoming is not None and current != incoming:
                raise ActivitySliceConflict(
                    f"step replay changed durable {label}"
                )

    def _assert_observation_fresh(
        self,
        connection: sqlite3.Connection,
        step: ActivityStep,
        after_observation_ref: str | None,
    ) -> None:
        if after_observation_ref is None:
            return
        if after_observation_ref == step.before_observation_ref:
            raise ActivitySliceConflict("after-observation must be fresh")
        reused = connection.execute(
            "SELECT step_id FROM activity_steps WHERE slice_id=? AND step_id<>? AND "
            "(before_observation_ref=? OR after_observation_ref=?) LIMIT 1",
            (
                step.slice_id,
                step.id,
                after_observation_ref,
                after_observation_ref,
            ),
        ).fetchone()
        if reused is not None:
            raise ActivitySliceConflict(
                "an ActivityStep cannot reuse another step's observation"
            )

    @staticmethod
    def _slice_row(connection: sqlite3.Connection, slice_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM activity_slices WHERE slice_id=?", (slice_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown ActivitySlice {slice_id}")
        return row

    @staticmethod
    def _step_row(connection: sqlite3.Connection, step_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM activity_steps WHERE step_id=?", (step_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown ActivityStep {step_id}")
        return row

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


# Compatibility with the repository's store naming convention.
SQLiteActivitySliceStore = ActivitySliceStore


def _slice(row: sqlite3.Row) -> ActivitySlice:
    return ActivitySlice(
        id=str(row["slice_id"]),
        idempotency_key=str(row["idempotency_key"]),
        session_id=str(row["session_id"]),
        goal_id=str(row["goal_id"]),
        attention_decision_id=str(row["attention_decision_id"]),
        attention_decision_revision=int(row["attention_decision_revision"]),
        objective=str(row["objective"]),
        status=ActivitySliceStatus(str(row["status"])),
        budget=SliceBudget(
            time_budget_seconds=float(row["time_budget_seconds"]),
            action_limit=int(row["action_limit"]),
        ),
        started_at=str(row["started_at"]),
        deadline_at=str(row["deadline_at"]),
        action_count=int(row["action_count"]),
        next_step_ordinal=int(row["next_step_ordinal"]),
        last_step_id=(str(row["last_step_id"]) if row["last_step_id"] else None),
        observed_directive_revision=int(row["observed_directive_revision"]),
        observed_event_cursor=int(row["observed_event_cursor"]),
        yield_reason=(
            SliceYieldReason(str(row["yield_reason"])) if row["yield_reason"] else None
        ),
        continuation_key=(
            str(row["continuation_key"]) if row["continuation_key"] else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _step(row: sqlite3.Row) -> ActivityStep:
    return ActivityStep(
        id=str(row["step_id"]),
        idempotency_key=str(row["idempotency_key"]),
        slice_id=str(row["slice_id"]),
        ordinal=int(row["ordinal"]),
        status=ActivityStepStatus(str(row["status"])),
        phase=ActivityStepPhase(str(row["phase"])),
        boundary=_boundary(json.loads(str(row["boundary_json"]))),
        outcome=StepOutcome(str(row["outcome"])) if row["outcome"] else None,
        physical_action_ref=(
            str(row["physical_action_ref"]) if row["physical_action_ref"] else None
        ),
        action_receipt_ref=(
            str(row["action_receipt_ref"]) if row["action_receipt_ref"] else None
        ),
        after_observation_ref=(
            str(row["after_observation_ref"]) if row["after_observation_ref"] else None
        ),
        verification_ref=(
            str(row["verification_ref"]) if row["verification_ref"] else None
        ),
        verified=(bool(row["verified"]) if row["verified"] is not None else None),
        decision=(
            json.loads(str(row["decision_json"])) if row["decision_json"] else None
        ),
        result_facts=json.loads(str(row["result_facts_json"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        settled_at=(str(row["settled_at"]) if row["settled_at"] else None),
    )


def _wake(row: sqlite3.Row) -> WakePlan:
    return WakePlan(
        id=str(row["wake_plan_id"]),
        idempotency_key=str(row["idempotency_key"]),
        slice_id=str(row["slice_id"]),
        step_id=str(row["step_id"]),
        kind=WakePlanKind(str(row["kind"])),
        status=WakePlanStatus(str(row["status"])),
        matcher=(
            json.loads(str(row["matcher_json"])) if row["matcher_json"] else None
        ),
        due_at=str(row["due_at"]) if row["due_at"] else None,
        user_fact_key=str(row["user_fact_key"]) if row["user_fact_key"] else None,
        created_at=str(row["created_at"]),
    )


def _boundary_payload(boundary: BoundarySnapshot) -> dict[str, Any]:
    return {
        "captured_at": boundary.captured_at,
        "fresh_observation_ref": boundary.fresh_observation_ref,
        "directive_revision": boundary.directive_revision,
        "event_cursor": boundary.event_cursor,
        "human_active": boundary.human_active,
        "control_mode": boundary.control_mode,
        "stop_requested": boundary.stop_requested,
        "checkpoint_requested": boundary.checkpoint_requested,
        "directive_requires_yield": boundary.directive_requires_yield,
        "event_requires_yield": boundary.event_requires_yield,
        "facts": dict(boundary.facts),
    }


def _boundary(payload: Mapping[str, Any]) -> BoundarySnapshot:
    return BoundarySnapshot(
        captured_at=str(payload["captured_at"]),
        fresh_observation_ref=str(payload["fresh_observation_ref"]),
        directive_revision=int(payload["directive_revision"]),
        event_cursor=int(payload["event_cursor"]),
        human_active=bool(payload.get("human_active", False)),
        control_mode=str(payload.get("control_mode", "AGENT_ACTIVE")),
        stop_requested=bool(payload.get("stop_requested", False)),
        checkpoint_requested=bool(payload.get("checkpoint_requested", False)),
        directive_requires_yield=bool(payload.get("directive_requires_yield", False)),
        event_requires_yield=bool(payload.get("event_requires_yield", False)),
        facts=dict(payload.get("facts") or {}),
    )


def _result_payload(result: StepExecutionResult) -> dict[str, Any]:
    return {
        "outcome": result.outcome.value,
        "decision": dict(result.decision),
        "physical_action_ref": result.physical_action_ref,
        "action_receipt_ref": result.action_receipt_ref,
        "after_observation_ref": result.after_observation_ref,
        "verification_ref": result.verification_ref,
        "verified": result.verified,
        "wake_matcher": (
            dict(result.wake_matcher) if result.wake_matcher is not None else None
        ),
        "due_at": result.due_at,
        "user_fact_key": result.user_fact_key,
        "progress_fact": (
            dict(result.progress_fact) if result.progress_fact is not None else None
        ),
        "completion_proposal": (
            dict(result.completion_proposal)
            if result.completion_proposal is not None
            else None
        ),
        "completion_verified": result.completion_verified,
        "facts": dict(result.facts),
    }


def _continuation_payload(value: ContinuationDirective) -> dict[str, Any]:
    return {
        "idempotency_key": value.idempotency_key,
        "slice_id": value.slice_id,
        "session_id": value.session_id,
        "goal_id": value.goal_id,
        "reason": value.reason.value,
        "last_settled_step_id": value.last_settled_step_id,
        "resume_step_ordinal": value.resume_step_ordinal,
        "observed_directive_revision": value.observed_directive_revision,
        "observed_event_cursor": value.observed_event_cursor,
        "remaining_time_seconds": value.remaining_time_seconds,
        "remaining_actions": value.remaining_actions,
        "checkpoint_ref": value.checkpoint_ref,
        "wake_plan_id": value.wake_plan_id,
        "created_at": value.created_at,
    }


def _continuation(payload: Mapping[str, Any]) -> ContinuationDirective:
    return ContinuationDirective(
        idempotency_key=str(payload["idempotency_key"]),
        slice_id=str(payload["slice_id"]),
        session_id=str(payload["session_id"]),
        goal_id=str(payload["goal_id"]),
        reason=SliceYieldReason(str(payload["reason"])),
        last_settled_step_id=(
            str(payload["last_settled_step_id"])
            if payload.get("last_settled_step_id")
            else None
        ),
        resume_step_ordinal=int(payload["resume_step_ordinal"]),
        observed_directive_revision=int(payload["observed_directive_revision"]),
        observed_event_cursor=int(payload["observed_event_cursor"]),
        remaining_time_seconds=float(payload["remaining_time_seconds"]),
        remaining_actions=int(payload["remaining_actions"]),
        checkpoint_ref=str(payload["checkpoint_ref"]),
        wake_plan_id=(
            str(payload["wake_plan_id"]) if payload.get("wake_plan_id") else None
        ),
        created_at=str(payload["created_at"]),
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ActivitySlice store requires timezone-aware datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["ActivitySliceStore", "SQLiteActivitySliceStore"]
