from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ...runtime_kernel.action import (
    Action,
    ActionExecution,
    ActionStatus,
    ActionType,
    ActionValidationStatus,
    ExecutionError,
)
from ...runtime_kernel.checkpoint import Checkpoint, CheckpointDraft
from ...runtime_kernel.event import EventActor, RuntimeEvent, RuntimeEventDraft
from ...runtime_kernel.fact import Fact, FactScope, FactStatus
from ...runtime_kernel.lease import DeviceExecutionLease
from ...runtime_kernel.lease.errors import LeaseConflict, LeaseExpired, LeaseNotFound
from ...runtime_kernel.observation import (
    ArtifactRef,
    ChannelAvailability,
    ConnectionState,
    ConsistencyStatus,
    DeviceState,
    KeyboardState,
    Observation,
    ObservationConsistency,
    Orientation,
    ScreenshotChannel,
    UiTreeChannel,
)
from ...runtime_kernel.ports import RecordNotFound, StoreConflict
from ...runtime_kernel.stage import Stage, StageStatus
from ...runtime_kernel.task import FailureState, Task, TaskSource, TaskStatus
from ...runtime_kernel.verify import Verification, VerificationMethod, VerificationVerdict


_SCHEMA_REVISION = 4

_PHASE_2_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_schema (
    revision INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_tasks (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'CREATED', 'PLANNING', 'RUNNING', 'WAITING', 'PAUSED',
        'STUCK', 'COMPLETED', 'FAILED', 'CANCELLED'
    )),
    source_client_id TEXT NOT NULL,
    source_conversation_id TEXT NOT NULL,
    source_initial_message_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    current_stage_id TEXT,
    last_observation_id TEXT,
    last_meaningful_progress_at TEXT,
    failure_state_json TEXT,
    latest_checkpoint_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_at TEXT
);

CREATE TABLE IF NOT EXISTS runtime_stages (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    objective TEXT NOT NULL,
    completion_criteria_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'ACTIVE', 'COMPLETED', 'FAILED', 'SUPERSEDED'
    )),
    planner_call_id TEXT,
    progress_summary TEXT,
    started_at TEXT,
    completed_at TEXT,
    evidence_refs_json TEXT NOT NULL,
    UNIQUE (task_id, ordinal),
    UNIQUE (task_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_stages_one_active
ON runtime_stages(task_id) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS runtime_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    type TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (actor IN ('user', 'gateway', 'runtime', 'model', 'device')),
    payload_json TEXT NOT NULL,
    causation_id TEXT,
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    UNIQUE (task_id, sequence)
);

CREATE INDEX IF NOT EXISTS ix_runtime_events_task_sequence
ON runtime_events(task_id, sequence);
"""

_MIGRATION_1_TO_2 = """
CREATE TABLE runtime_observations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    device_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    capture_started_at TEXT NOT NULL,
    capture_completed_at TEXT NOT NULL,
    screenshot_status TEXT NOT NULL CHECK (screenshot_status = 'AVAILABLE'),
    screenshot_artifact_ref TEXT NOT NULL,
    screenshot_content_type TEXT NOT NULL,
    screenshot_size_bytes INTEGER NOT NULL CHECK (screenshot_size_bytes >= 1),
    screenshot_sha256 TEXT NOT NULL,
    screenshot_width INTEGER NOT NULL CHECK (screenshot_width >= 1),
    screenshot_height INTEGER NOT NULL CHECK (screenshot_height >= 1),
    screenshot_captured_at TEXT NOT NULL,
    ui_tree_status TEXT NOT NULL CHECK (
        ui_tree_status IN ('AVAILABLE', 'UNAVAILABLE', 'FAILED')
    ),
    ui_tree_artifact_ref TEXT,
    ui_tree_content_type TEXT,
    ui_tree_size_bytes INTEGER,
    ui_tree_sha256 TEXT,
    ui_tree_captured_at TEXT NOT NULL,
    ui_tree_error_code TEXT,
    device_state_status TEXT NOT NULL CHECK (device_state_status = 'AVAILABLE'),
    foreground_app TEXT,
    screen_width INTEGER NOT NULL CHECK (screen_width >= 1),
    screen_height INTEGER NOT NULL CHECK (screen_height >= 1),
    orientation TEXT NOT NULL CHECK (
        orientation IN ('portrait', 'landscape', 'unknown')
    ),
    keyboard_state TEXT NOT NULL CHECK (
        keyboard_state IN ('shown', 'hidden', 'unknown')
    ),
    connection_state TEXT NOT NULL CHECK (
        connection_state IN ('connected', 'disconnected', 'unauthorized', 'unknown')
    ),
    device_state_captured_at TEXT NOT NULL,
    consistency_status TEXT NOT NULL CHECK (
        consistency_status IN ('consistent', 'degraded')
    ),
    consistency_reason TEXT,
    UNIQUE (task_id, id),
    CHECK (
        (ui_tree_status = 'AVAILABLE'
         AND ui_tree_artifact_ref IS NOT NULL
         AND ui_tree_content_type IS NOT NULL
         AND ui_tree_size_bytes >= 1
         AND ui_tree_sha256 IS NOT NULL
         AND ui_tree_error_code IS NULL)
        OR
        (ui_tree_status != 'AVAILABLE'
         AND ui_tree_artifact_ref IS NULL
         AND ui_tree_content_type IS NULL
         AND ui_tree_size_bytes IS NULL
         AND ui_tree_sha256 IS NULL)
    )
);

CREATE INDEX ix_runtime_observations_task_capture
ON runtime_observations(task_id, captured_at, id);
"""

_MIGRATION_2_TO_3 = """
CREATE TABLE runtime_actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL REFERENCES runtime_stages(id) ON DELETE RESTRICT,
    based_on_observation_id TEXT NOT NULL REFERENCES runtime_observations(id) ON DELETE RESTRICT,
    type TEXT NOT NULL CHECK (type IN (
        'tap', 'long_press', 'swipe', 'input_text', 'back', 'home',
        'open_app', 'wait', 'screenshot'
    )),
    params_json TEXT NOT NULL,
    expected_outcome TEXT NOT NULL,
    proposed_by_call_id TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('accepted', 'rejected')),
    rejection_code TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'PROPOSED', 'EXECUTED', 'VERIFIED', 'FAILED', 'UNCERTAIN'
    )),
    updated_at TEXT NOT NULL,
    CHECK (
        (validation_status = 'accepted' AND rejection_code IS NULL)
        OR
        (validation_status = 'rejected' AND rejection_code IS NOT NULL AND status = 'FAILED')
    )
);
CREATE INDEX ix_runtime_actions_task_created
ON runtime_actions(task_id, proposed_at, id);
CREATE INDEX ix_runtime_actions_stage_created
ON runtime_actions(stage_id, proposed_at, id);

CREATE TABLE runtime_action_executions (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE REFERENCES runtime_actions(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    device_id TEXT NOT NULL,
    lease_ref TEXT,
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    adapter_code INTEGER,
    error_code TEXT,
    error_message TEXT,
    error_retryable INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    CHECK (
        (accepted = 1 AND error_code IS NULL AND error_message IS NULL AND error_retryable IS NULL)
        OR
        (accepted = 0 AND error_code IS NOT NULL AND error_message IS NOT NULL AND error_retryable IN (0, 1))
    )
);
CREATE INDEX ix_runtime_action_executions_task_started
ON runtime_action_executions(task_id, started_at, id);

CREATE TABLE runtime_verifications (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE REFERENCES runtime_actions(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL REFERENCES runtime_stages(id) ON DELETE RESTRICT,
    before_observation_id TEXT NOT NULL REFERENCES runtime_observations(id) ON DELETE RESTRICT,
    after_observation_id TEXT NOT NULL REFERENCES runtime_observations(id) ON DELETE RESTRICT,
    verdict TEXT NOT NULL CHECK (verdict IN ('SUCCESS', 'FAIL', 'UNCERTAIN')),
    reason TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('runtime_rule', 'role_assisted', 'combined')),
    verification_call_id TEXT,
    created_at TEXT NOT NULL,
    CHECK (after_observation_id <> before_observation_id)
);
CREATE INDEX ix_runtime_verifications_task_created
ON runtime_verifications(task_id, created_at, id);

CREATE TABLE runtime_facts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('VERIFIED', 'USER_PROVIDED', 'UNVERIFIED')),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    scope TEXT NOT NULL CHECK (scope IN ('task', 'stage')),
    stage_id TEXT REFERENCES runtime_stages(id) ON DELETE RESTRICT,
    source_refs_json TEXT NOT NULL,
    supersedes_fact_id TEXT REFERENCES runtime_facts(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK ((scope = 'task' AND stage_id IS NULL) OR (scope = 'stage' AND stage_id IS NOT NULL)),
    CHECK (supersedes_fact_id IS NULL OR supersedes_fact_id <> id)
);
CREATE INDEX ix_runtime_facts_task_created
ON runtime_facts(task_id, created_at, id);
CREATE INDEX ix_runtime_facts_superseded
ON runtime_facts(supersedes_fact_id);

CREATE TABLE runtime_checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    through_sequence INTEGER NOT NULL CHECK (through_sequence >= 1),
    goal TEXT NOT NULL,
    status_at_checkpoint TEXT NOT NULL CHECK (status_at_checkpoint IN (
        'CREATED', 'PLANNING', 'RUNNING', 'WAITING', 'PAUSED',
        'STUCK', 'COMPLETED', 'FAILED', 'CANCELLED'
    )),
    current_stage_id TEXT REFERENCES runtime_stages(id) ON DELETE RESTRICT,
    completed_stage_summaries_json TEXT NOT NULL,
    verified_facts_json TEXT NOT NULL,
    device_summary_json TEXT NOT NULL,
    last_meaningful_progress_json TEXT,
    failure_summary_json TEXT,
    resume_reason TEXT NOT NULL,
    required_fresh_observation INTEGER NOT NULL CHECK (required_fresh_observation IN (0, 1)),
    unresolved_action_ref TEXT REFERENCES runtime_actions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (required_fresh_observation = 1 OR unresolved_action_ref IS NULL)
);
CREATE INDEX ix_runtime_checkpoints_task_created
ON runtime_checkpoints(task_id, created_at, id);
"""

_MIGRATION_3_TO_4 = """
CREATE TABLE runtime_device_leases (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES runtime_tasks(id) ON DELETE RESTRICT,
    holder_process_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    action_id TEXT REFERENCES runtime_actions(id) ON DELETE RESTRICT,
    UNIQUE(device_id)
);

CREATE INDEX ix_runtime_device_leases_task
ON runtime_device_leases(task_id);

CREATE INDEX ix_runtime_device_leases_expires
ON runtime_device_leases(expires_at);
"""


class SQLiteRuntimeStore:
    """SQLite adapter implementing the Runtime Store port without live wiring."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not tables:
                connection.executescript(_PHASE_2_SCHEMA)
                connection.execute(
                    "INSERT INTO runtime_schema(revision, applied_at) VALUES (1, ?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            elif "runtime_schema" not in tables:
                raise StoreConflict("existing database has no runtime_schema version fact")

            row = connection.execute(
                "SELECT MAX(revision) FROM runtime_schema"
            ).fetchone()
            revision = int(row[0]) if row and row[0] is not None else 0
            if revision > _SCHEMA_REVISION:
                raise StoreConflict(
                    f"runtime schema revision {revision} is newer than supported"
                )
            if revision < 1:
                raise StoreConflict("runtime schema revision is missing or invalid")
            if revision == 1:
                self._apply_migration(
                    connection,
                    statements=_MIGRATION_1_TO_2,
                    target_revision=2,
                )
                revision = 2
            if revision == 2:
                self._apply_migration(
                    connection,
                    statements=_MIGRATION_2_TO_3,
                    target_revision=3,
                )
                revision = 3
            if revision == 3:
                self._apply_migration(
                    connection,
                    statements=_MIGRATION_3_TO_4,
                    target_revision=4,
                )

    @staticmethod
    def _apply_migration(
        connection: sqlite3.Connection,
        *,
        statements: str,
        target_revision: int,
    ) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "INSERT INTO runtime_schema(revision, applied_at) VALUES (?, ?)",
                (target_revision, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(f"PRAGMA user_version = {target_revision}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def close(self) -> None:
        # Connections are deliberately operation-scoped so restart behavior is real.
        return None

    def create_task(self, task: Task, event: RuntimeEventDraft) -> RuntimeEvent:
        try:
            with self._transaction() as connection:
                self._insert_task(connection, task)
                return self._insert_event(connection, task.id, event)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not create Task {task.id}: {exc}") from exc

    def load_task(self, task_id: str) -> Task:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Task {task_id} was not found")
        return self._task_from_row(row)

    def create_stage(self, stage: Stage, event: RuntimeEventDraft) -> RuntimeEvent:
        try:
            with self._transaction() as connection:
                self._insert_stage(connection, stage)
                return self._insert_event(connection, stage.task_id, event)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not create Stage {stage.id}: {exc}") from exc

    def load_stage(self, task_id: str, stage_id: str) -> Stage:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_stages WHERE task_id = ? AND id = ?",
                (task_id, stage_id),
            ).fetchone()
        if row is None:
            raise RecordNotFound(
                f"Stage {stage_id} for Task {task_id} was not found"
            )
        return self._stage_from_row(row)

    def list_stages(self, task_id: str) -> tuple[Stage, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_stages WHERE task_id = ? ORDER BY ordinal",
                (task_id,),
            ).fetchall()
        return tuple(self._stage_from_row(row) for row in rows)

    def mutate_task_and_stage(
        self,
        *,
        before_task: Task,
        after_task: Task,
        before_stage: Stage,
        after_stage: Stage,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent:
        if before_task.id != after_task.id:
            raise StoreConflict("Task identity cannot change during mutation")
        if before_stage.id != after_stage.id:
            raise StoreConflict("Stage identity cannot change during mutation")
        if {before_stage.task_id, after_stage.task_id} != {before_task.id}:
            raise StoreConflict("Stage must remain owned by the mutated Task")
        try:
            with self._transaction() as connection:
                task_row = connection.execute(
                    "SELECT * FROM runtime_tasks WHERE id = ?", (before_task.id,)
                ).fetchone()
                stage_row = connection.execute(
                    "SELECT * FROM runtime_stages WHERE task_id = ? AND id = ?",
                    (before_task.id, before_stage.id),
                ).fetchone()
                if task_row is None or stage_row is None:
                    raise RecordNotFound("Task/Stage mutation target was not found")
                if self._task_from_row(task_row) != before_task:
                    raise StoreConflict("Task changed since it was loaded")
                if self._stage_from_row(stage_row) != before_stage:
                    raise StoreConflict("Stage changed since it was loaded")
                self._update_task(connection, after_task)
                self._update_stage(connection, after_stage)
                return self._insert_event(connection, after_task.id, event)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not mutate Task/Stage: {exc}") from exc

    def append_event(
        self, task_id: str, event: RuntimeEventDraft
    ) -> RuntimeEvent:
        try:
            with self._transaction() as connection:
                return self._insert_event(connection, task_id, event)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not append RuntimeEvent: {exc}") from exc

    def list_events(
        self, task_id: str, *, after_sequence: int = 0
    ) -> tuple[RuntimeEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_events
                WHERE task_id = ? AND sequence > ?
                ORDER BY sequence
                """,
                (task_id, after_sequence),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def persist_observation(
        self,
        *,
        before_task: Task,
        after_task: Task,
        observation: Observation,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent:
        if before_task.id != after_task.id or observation.task_id != before_task.id:
            raise StoreConflict("Observation must remain owned by the mutated Task")
        if before_task.device_id != observation.device_id:
            raise StoreConflict("Observation device_id does not match its Task")
        if after_task.last_observation_id != observation.id:
            raise StoreConflict("Task must reference the committed Observation")
        try:
            with self._transaction() as connection:
                task_row = connection.execute(
                    "SELECT * FROM runtime_tasks WHERE id = ?", (before_task.id,)
                ).fetchone()
                if task_row is None:
                    raise RecordNotFound(f"Task {before_task.id} was not found")
                if self._task_from_row(task_row) != before_task:
                    raise StoreConflict("Task changed since it was loaded")
                self._insert_observation(connection, observation)
                self._update_task(connection, after_task)
                return self._insert_event(connection, before_task.id, event)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(
                f"could not persist Observation {observation.id}: {exc}"
            ) from exc

    def load_observation(self, observation_id: str) -> Observation:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_observations WHERE id = ?", (observation_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Observation {observation_id} was not found")
        return self._observation_from_row(row)

    def list_observations(self, task_id: str) -> tuple[Observation, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_observations
                WHERE task_id = ?
                ORDER BY captured_at, id
                """,
                (task_id,),
            ).fetchall()
        return tuple(self._observation_from_row(row) for row in rows)

    def latest_observation(self, task_id: str) -> Observation | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT observation.*
                FROM runtime_tasks AS task
                LEFT JOIN runtime_observations AS observation
                  ON observation.id = task.last_observation_id
                 AND observation.task_id = task.id
                WHERE task.id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Task {task_id} was not found")
        if row["id"] is None:
            return None
        return self._observation_from_row(row)

    def create_action(self, action: Action, event: RuntimeEventDraft) -> RuntimeEvent:
        try:
            with self._transaction() as connection:
                task_row = connection.execute(
                    "SELECT id FROM runtime_tasks WHERE id = ?", (action.task_id,)
                ).fetchone()
                stage_row = connection.execute(
                    "SELECT task_id FROM runtime_stages WHERE id = ?", (action.stage_id,)
                ).fetchone()
                observation_row = connection.execute(
                    "SELECT task_id FROM runtime_observations WHERE id = ?",
                    (action.based_on_observation_id,),
                ).fetchone()
                if task_row is None or stage_row is None or observation_row is None:
                    raise RecordNotFound("Action references a missing Task, Stage, or Observation")
                if stage_row["task_id"] != action.task_id or observation_row["task_id"] != action.task_id:
                    raise StoreConflict("Action references facts from another Task")
                self._insert_action(connection, action)
                return self._insert_event(connection, action.task_id, event)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not create Action {action.id}: {exc}") from exc

    def load_action(self, task_id: str, action_id: str) -> Action:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_actions WHERE task_id = ? AND id = ?",
                (task_id, action_id),
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Action {action_id} for Task {task_id} was not found")
        return self._action_from_row(row)

    def list_actions(self, task_id: str) -> tuple[Action, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_actions WHERE task_id = ? ORDER BY proposed_at, id",
                (task_id,),
            ).fetchall()
        return tuple(self._action_from_row(row) for row in rows)

    def record_action_execution(
        self,
        *,
        before_task: Task,
        after_task: Task,
        before_action: Action,
        after_action: Action,
        execution: ActionExecution,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent:
        self._validate_action_task_mutation(
            before_task, after_task, before_action, after_action
        )
        if execution.action_id != before_action.id or execution.device_id != before_task.device_id:
            raise StoreConflict("ActionExecution does not match its Action or Task device")
        try:
            with self._transaction() as connection:
                self._require_current_task_and_action(
                    connection, before_task, before_action
                )
                self._insert_action_execution(connection, before_task.id, execution)
                self._update_action(connection, after_action)
                self._update_task(connection, after_task)
                return self._insert_event(connection, before_task.id, event)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not record ActionExecution: {exc}") from exc

    def load_action_execution(self, action_id: str) -> ActionExecution:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_action_executions WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"ActionExecution for Action {action_id} was not found")
        return self._action_execution_from_row(row)

    def record_verification(
        self,
        *,
        before_task: Task,
        after_task: Task,
        before_action: Action,
        after_action: Action,
        verification: Verification,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent:
        self._validate_action_task_mutation(
            before_task, after_task, before_action, after_action
        )
        if verification.task_id != before_task.id or verification.action_id != before_action.id:
            raise StoreConflict("Verification does not belong to the mutated Task and Action")
        if verification.verdict.value == "SUCCESS":
            raise StoreConflict("SUCCESS Verification must use the atomic commit path")
        try:
            with self._transaction() as connection:
                self._require_current_task_and_action(
                    connection, before_task, before_action
                )
                self._require_verification_references(connection, verification)
                self._insert_verification(connection, verification)
                self._update_action(connection, after_action)
                self._update_task(connection, after_task)
                return self._insert_event(connection, before_task.id, event)
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not record Verification: {exc}") from exc

    def load_verification(self, action_id: str) -> Verification:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_verifications WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Verification for Action {action_id} was not found")
        return self._verification_from_row(row)

    def commit_successful_verification(
        self,
        *,
        before_task: Task,
        after_task: Task,
        before_stage: Stage,
        after_stage: Stage | None,
        before_action: Action,
        after_action: Action,
        verification: Verification,
        facts: tuple[Fact, ...],
        checkpoint: CheckpointDraft | None,
        events: tuple[RuntimeEventDraft, ...],
    ) -> tuple[tuple[RuntimeEvent, ...], Checkpoint | None]:
        self._validate_action_task_mutation(
            before_task, after_task, before_action, after_action
        )
        if verification.verdict.value != "SUCCESS":
            raise StoreConflict("only SUCCESS Verification can use the commit path")
        if verification.task_id != before_task.id or verification.stage_id != before_stage.id:
            raise StoreConflict("Verification must belong to the committed Task and Stage")
        if before_action.stage_id != before_stage.id:
            raise StoreConflict("Action must belong to the committed Stage")
        if after_stage is not None and after_stage.id != before_stage.id:
            raise StoreConflict("Stage identity cannot change during verification commit")
        if any(fact.task_id != before_task.id or fact.status.value != "VERIFIED" for fact in facts):
            raise StoreConflict("SUCCESS commit can add only verified Facts for its Task")
        if checkpoint is not None:
            if checkpoint.task_id != before_task.id or after_task.latest_checkpoint_id != checkpoint.id:
                raise StoreConflict("Task must reference the Checkpoint committed with it")
            if not events or events[-1].type != "CheckpointCreated":
                raise StoreConflict("CheckpointCreated must be the final atomic commit event")
        elif any(event.type == "CheckpointCreated" for event in events):
            raise StoreConflict("CheckpointCreated event requires a Checkpoint")
        try:
            with self._transaction() as connection:
                self._require_current_task_and_action(
                    connection, before_task, before_action
                )
                stage_row = connection.execute(
                    "SELECT * FROM runtime_stages WHERE task_id = ? AND id = ?",
                    (before_task.id, before_stage.id),
                ).fetchone()
                if stage_row is None:
                    raise RecordNotFound("Stage for verification commit was not found")
                if self._stage_from_row(stage_row) != before_stage:
                    raise StoreConflict("Stage changed since it was loaded")
                self._require_verification_references(connection, verification)
                self._insert_verification(connection, verification)
                self._update_action(connection, after_action)
                for fact in facts:
                    self._validate_fact_ownership(connection, fact)
                    self._insert_fact(connection, fact)
                if after_stage is not None:
                    self._update_stage(connection, after_stage)
                self._update_task(connection, after_task)
                committed_events: list[RuntimeEvent] = []
                materialized_checkpoint: Checkpoint | None = None
                for draft in events:
                    if draft.type == "CheckpointCreated":
                        if checkpoint is None:
                            raise StoreConflict("Checkpoint event has no Checkpoint")
                        next_sequence = self._next_event_sequence(connection, before_task.id)
                        materialized_checkpoint = checkpoint.materialize(
                            through_sequence=next_sequence
                        )
                        self._insert_checkpoint(connection, materialized_checkpoint)
                    committed_events.append(
                        self._insert_event(connection, before_task.id, draft)
                    )
                return tuple(committed_events), materialized_checkpoint
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not commit SUCCESS Verification: {exc}") from exc

    def list_facts(self, task_id: str, *, verified_only: bool = False) -> tuple[Fact, ...]:
        query = "SELECT * FROM runtime_facts WHERE task_id = ?"
        values: tuple[object, ...] = (task_id,)
        if verified_only:
            query += " AND status = 'VERIFIED'"
        query += " ORDER BY created_at, id"
        with self._connection() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(self._fact_from_row(row) for row in rows)

    def create_checkpoint(
        self,
        *,
        before_task: Task,
        after_task: Task,
        checkpoint: CheckpointDraft,
        event: RuntimeEventDraft,
    ) -> tuple[Checkpoint, RuntimeEvent]:
        if before_task.id != after_task.id or checkpoint.task_id != before_task.id:
            raise StoreConflict("Checkpoint must remain owned by the mutated Task")
        if after_task.latest_checkpoint_id != checkpoint.id:
            raise StoreConflict("Task must reference the committed Checkpoint")
        if event.type != "CheckpointCreated":
            raise StoreConflict("Checkpoint requires CheckpointCreated event")
        try:
            with self._transaction() as connection:
                task_row = connection.execute(
                    "SELECT * FROM runtime_tasks WHERE id = ?", (before_task.id,)
                ).fetchone()
                if task_row is None:
                    raise RecordNotFound(f"Task {before_task.id} was not found")
                if self._task_from_row(task_row) != before_task:
                    raise StoreConflict("Task changed since it was loaded")
                self._validate_checkpoint_ownership(connection, checkpoint)
                materialized = checkpoint.materialize(
                    through_sequence=self._next_event_sequence(connection, before_task.id)
                )
                self._insert_checkpoint(connection, materialized)
                self._update_task(connection, after_task)
                committed_event = self._insert_event(connection, before_task.id, event)
                if committed_event.sequence != materialized.through_sequence:
                    raise StoreConflict("Checkpoint sequence does not match CheckpointCreated")
                return materialized, committed_event
        except sqlite3.IntegrityError as exc:
            raise StoreConflict(f"could not create Checkpoint: {exc}") from exc

    def load_checkpoint(self, task_id: str, checkpoint_id: str) -> Checkpoint:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_checkpoints WHERE task_id = ? AND id = ?",
                (task_id, checkpoint_id),
            ).fetchone()
        if row is None:
            raise RecordNotFound(
                f"Checkpoint {checkpoint_id} for Task {task_id} was not found"
            )
        return self._checkpoint_from_row(row)

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT checkpoint.*
                FROM runtime_tasks AS task
                LEFT JOIN runtime_checkpoints AS checkpoint
                  ON checkpoint.id = task.latest_checkpoint_id
                 AND checkpoint.task_id = task.id
                WHERE task.id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Task {task_id} was not found")
        if row["id"] is None:
            return None
        return self._checkpoint_from_row(row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_action_task_mutation(
        before_task: Task,
        after_task: Task,
        before_action: Action,
        after_action: Action,
    ) -> None:
        if before_task.id != after_task.id:
            raise StoreConflict("Task identity cannot change during Action mutation")
        if before_action.id != after_action.id:
            raise StoreConflict("Action identity cannot change during mutation")
        if {before_action.task_id, after_action.task_id} != {before_task.id}:
            raise StoreConflict("Action must remain owned by the mutated Task")

    def _require_current_task_and_action(
        self,
        connection: sqlite3.Connection,
        before_task: Task,
        before_action: Action,
    ) -> None:
        task_row = connection.execute(
            "SELECT * FROM runtime_tasks WHERE id = ?", (before_task.id,)
        ).fetchone()
        action_row = connection.execute(
            "SELECT * FROM runtime_actions WHERE task_id = ? AND id = ?",
            (before_task.id, before_action.id),
        ).fetchone()
        if task_row is None or action_row is None:
            raise RecordNotFound("Task/Action mutation target was not found")
        if self._task_from_row(task_row) != before_task:
            raise StoreConflict("Task changed since it was loaded")
        if self._action_from_row(action_row) != before_action:
            raise StoreConflict("Action changed since it was loaded")

    @staticmethod
    def _require_verification_references(
        connection: sqlite3.Connection, verification: Verification
    ) -> None:
        action_row = connection.execute(
            "SELECT task_id, stage_id FROM runtime_actions WHERE id = ?",
            (verification.action_id,),
        ).fetchone()
        before_row = connection.execute(
            "SELECT task_id FROM runtime_observations WHERE id = ?",
            (verification.before_observation_id,),
        ).fetchone()
        after_row = connection.execute(
            "SELECT task_id FROM runtime_observations WHERE id = ?",
            (verification.after_observation_id,),
        ).fetchone()
        if action_row is None or before_row is None or after_row is None:
            raise RecordNotFound("Verification references a missing Action or Observation")
        if (
            action_row["task_id"] != verification.task_id
            or action_row["stage_id"] != verification.stage_id
            or before_row["task_id"] != verification.task_id
            or after_row["task_id"] != verification.task_id
        ):
            raise StoreConflict("Verification references facts from another Task")

    @staticmethod
    def _validate_fact_ownership(connection: sqlite3.Connection, fact: Fact) -> None:
        if fact.stage_id is not None:
            row = connection.execute(
                "SELECT task_id FROM runtime_stages WHERE id = ?", (fact.stage_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound("Fact references a missing Stage")
            if row["task_id"] != fact.task_id:
                raise StoreConflict("Fact Stage belongs to another Task")
        if fact.supersedes_fact_id is not None:
            superseded = connection.execute(
                "SELECT task_id FROM runtime_facts WHERE id = ?",
                (fact.supersedes_fact_id,),
            ).fetchone()
            if superseded is None:
                raise RecordNotFound("Fact supersedes a missing Fact")
            if superseded["task_id"] != fact.task_id:
                raise StoreConflict("Fact cannot supersede a Fact from another Task")

    @staticmethod
    def _validate_checkpoint_ownership(
        connection: sqlite3.Connection, checkpoint: CheckpointDraft
    ) -> None:
        if checkpoint.current_stage_id is not None:
            row = connection.execute(
                "SELECT task_id FROM runtime_stages WHERE id = ?",
                (checkpoint.current_stage_id,),
            ).fetchone()
            if row is None or row["task_id"] != checkpoint.task_id:
                raise StoreConflict("Checkpoint current Stage belongs to another Task")
        if checkpoint.unresolved_action_ref is not None:
            row = connection.execute(
                "SELECT task_id FROM runtime_actions WHERE id = ?",
                (checkpoint.unresolved_action_ref,),
            ).fetchone()
            if row is None or row["task_id"] != checkpoint.task_id:
                raise StoreConflict("Checkpoint unresolved Action belongs to another Task")

    @staticmethod
    def _next_event_sequence(connection: sqlite3.Connection, task_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0])

    def _insert_task(self, connection: sqlite3.Connection, task: Task) -> None:
        connection.execute(
            """
            INSERT INTO runtime_tasks (
                id, schema_version, goal, status, source_client_id,
                source_conversation_id, source_initial_message_id, device_id,
                current_stage_id, last_observation_id,
                last_meaningful_progress_at, failure_state_json,
                latest_checkpoint_id, created_at, updated_at, terminal_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._task_values(task),
        )

    def _update_task(self, connection: sqlite3.Connection, task: Task) -> None:
        values = self._task_values(task)
        connection.execute(
            """
            UPDATE runtime_tasks SET
                schema_version = ?, goal = ?, status = ?, source_client_id = ?,
                source_conversation_id = ?, source_initial_message_id = ?,
                device_id = ?, current_stage_id = ?, last_observation_id = ?,
                last_meaningful_progress_at = ?, failure_state_json = ?,
                latest_checkpoint_id = ?, created_at = ?, updated_at = ?,
                terminal_at = ?
            WHERE id = ?
            """,
            values[1:] + (values[0],),
        )

    def _insert_stage(self, connection: sqlite3.Connection, stage: Stage) -> None:
        connection.execute(
            """
            INSERT INTO runtime_stages (
                id, task_id, ordinal, objective, completion_criteria_json,
                status, planner_call_id, progress_summary, started_at,
                completed_at, evidence_refs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._stage_values(stage),
        )

    def _insert_observation(
        self, connection: sqlite3.Connection, observation: Observation
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_observations (
                id, task_id, device_id, captured_at, capture_started_at,
                capture_completed_at, screenshot_status,
                screenshot_artifact_ref, screenshot_content_type,
                screenshot_size_bytes, screenshot_sha256, screenshot_width,
                screenshot_height, screenshot_captured_at, ui_tree_status,
                ui_tree_artifact_ref, ui_tree_content_type, ui_tree_size_bytes,
                ui_tree_sha256, ui_tree_captured_at, ui_tree_error_code,
                device_state_status, foreground_app, screen_width, screen_height,
                orientation, keyboard_state, connection_state,
                device_state_captured_at, consistency_status, consistency_reason
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            self._observation_values(observation),
        )

    def _insert_action(self, connection: sqlite3.Connection, action: Action) -> None:
        connection.execute(
            """
            INSERT INTO runtime_actions (
                id, task_id, stage_id, based_on_observation_id, type, params_json,
                expected_outcome, proposed_by_call_id, proposed_at, validation_status,
                rejection_code, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._action_values(action),
        )

    def _update_action(self, connection: sqlite3.Connection, action: Action) -> None:
        values = self._action_values(action)
        connection.execute(
            """
            UPDATE runtime_actions SET
                task_id = ?, stage_id = ?, based_on_observation_id = ?, type = ?,
                params_json = ?, expected_outcome = ?, proposed_by_call_id = ?,
                proposed_at = ?, validation_status = ?, rejection_code = ?,
                status = ?, updated_at = ?
            WHERE id = ?
            """,
            values[1:] + (values[0],),
        )

    @staticmethod
    def _insert_action_execution(
        connection: sqlite3.Connection, task_id: str, execution: ActionExecution
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_action_executions (
                id, action_id, task_id, device_id, lease_ref, accepted, adapter_code,
                error_code, error_message, error_retryable, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution.id,
                execution.action_id,
                task_id,
                execution.device_id,
                execution.lease_ref,
                int(execution.accepted),
                execution.adapter_code,
                execution.error.code if execution.error else None,
                execution.error.message if execution.error else None,
                int(execution.error.retryable) if execution.error else None,
                execution.started_at,
                execution.finished_at,
            ),
        )

    @staticmethod
    def _insert_verification(
        connection: sqlite3.Connection, verification: Verification
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_verifications (
                id, action_id, task_id, stage_id, before_observation_id,
                after_observation_id, verdict, reason, evidence_refs_json, method,
                verification_call_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                verification.id,
                verification.action_id,
                verification.task_id,
                verification.stage_id,
                verification.before_observation_id,
                verification.after_observation_id,
                verification.verdict.value,
                verification.reason,
                _json(verification.evidence_refs),
                verification.method.value,
                verification.verification_call_id,
                verification.created_at,
            ),
        )

    @staticmethod
    def _insert_fact(connection: sqlite3.Connection, fact: Fact) -> None:
        connection.execute(
            """
            INSERT INTO runtime_facts (
                id, task_id, key, value_json, status, confidence, scope, stage_id,
                source_refs_json, supersedes_fact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.id,
                fact.task_id,
                fact.key,
                _json(fact.value),
                fact.status.value,
                fact.confidence,
                fact.scope.value,
                fact.stage_id,
                _json(fact.source_refs),
                fact.supersedes_fact_id,
                fact.created_at,
            ),
        )

    @staticmethod
    def _insert_checkpoint(
        connection: sqlite3.Connection, checkpoint: Checkpoint
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_checkpoints (
                id, task_id, through_sequence, goal, status_at_checkpoint,
                current_stage_id, completed_stage_summaries_json, verified_facts_json,
                device_summary_json, last_meaningful_progress_json,
                failure_summary_json, resume_reason, required_fresh_observation,
                unresolved_action_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.id,
                checkpoint.task_id,
                checkpoint.through_sequence,
                checkpoint.goal,
                checkpoint.status_at_checkpoint.value,
                checkpoint.current_stage_id,
                _json(checkpoint.completed_stage_summaries),
                _json([SQLiteRuntimeStore._fact_payload(fact) for fact in checkpoint.verified_facts]),
                _json(checkpoint.device_summary),
                _json(checkpoint.last_meaningful_progress)
                if checkpoint.last_meaningful_progress is not None
                else None,
                _json(checkpoint.failure_summary)
                if checkpoint.failure_summary is not None
                else None,
                checkpoint.resume_reason,
                int(checkpoint.required_fresh_observation),
                checkpoint.unresolved_action_ref,
                checkpoint.created_at,
            ),
        )

    def _update_stage(self, connection: sqlite3.Connection, stage: Stage) -> None:
        values = self._stage_values(stage)
        connection.execute(
            """
            UPDATE runtime_stages SET
                task_id = ?, ordinal = ?, objective = ?,
                completion_criteria_json = ?, status = ?, planner_call_id = ?,
                progress_summary = ?, started_at = ?, completed_at = ?,
                evidence_refs_json = ?
            WHERE id = ?
            """,
            values[1:] + (values[0],),
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        sequence = int(row[0])
        connection.execute(
            """
            INSERT INTO runtime_events (
                id, task_id, sequence, type, actor, payload_json,
                causation_id, correlation_id, created_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                task_id,
                sequence,
                event.type,
                event.actor.value,
                _json(event.payload),
                event.causation_id,
                event.correlation_id,
                event.created_at,
                event.schema_version,
            ),
        )
        return RuntimeEvent(
            id=event.id,
            task_id=task_id,
            sequence=sequence,
            type=event.type,
            actor=event.actor,
            payload=dict(event.payload),
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
            created_at=event.created_at,
            schema_version=event.schema_version,
        )

    @staticmethod
    def _action_values(action: Action) -> tuple[Any, ...]:
        return (
            action.id,
            action.task_id,
            action.stage_id,
            action.based_on_observation_id,
            action.type.value,
            _json(action.params),
            action.expected_outcome,
            action.proposed_by_call_id,
            action.proposed_at,
            action.validation_status.value,
            action.rejection_code,
            action.status.value,
            action.updated_at,
        )

    @staticmethod
    def _fact_payload(fact: Fact) -> dict[str, Any]:
        return {
            "id": fact.id,
            "task_id": fact.task_id,
            "key": fact.key,
            "value": fact.value,
            "status": fact.status.value,
            "confidence": fact.confidence,
            "scope": fact.scope.value,
            "stage_id": fact.stage_id,
            "source_refs": list(fact.source_refs),
            "supersedes_fact_id": fact.supersedes_fact_id,
            "created_at": fact.created_at,
        }

    @staticmethod
    def _fact_from_payload(payload: Mapping[str, Any]) -> Fact:
        return Fact(
            id=payload["id"],
            task_id=payload["task_id"],
            key=payload["key"],
            value=payload["value"],
            status=FactStatus(payload["status"]),
            confidence=payload["confidence"],
            scope=FactScope(payload["scope"]),
            stage_id=payload["stage_id"],
            source_refs=tuple(payload["source_refs"]),
            supersedes_fact_id=payload["supersedes_fact_id"],
            created_at=payload["created_at"],
        )

    @staticmethod
    def _task_values(task: Task) -> tuple[Any, ...]:
        return (
            task.id,
            task.schema_version,
            task.goal,
            task.status.value,
            task.source.client_id,
            task.source.conversation_id,
            task.source.initial_message_id,
            task.device_id,
            task.current_stage_id,
            task.last_observation_id,
            task.last_meaningful_progress_at,
            _json(asdict(task.failure_state)) if task.failure_state else None,
            task.latest_checkpoint_id,
            task.created_at,
            task.updated_at,
            task.terminal_at,
        )

    @staticmethod
    def _stage_values(stage: Stage) -> tuple[Any, ...]:
        return (
            stage.id,
            stage.task_id,
            stage.ordinal,
            stage.objective,
            _json(stage.completion_criteria),
            stage.status.value,
            stage.planner_call_id,
            stage.progress_summary,
            stage.started_at,
            stage.completed_at,
            _json(stage.evidence_refs),
        )

    @staticmethod
    def _observation_values(observation: Observation) -> tuple[Any, ...]:
        ui_artifact = observation.ui_tree.artifact
        return (
            observation.id,
            observation.task_id,
            observation.device_id,
            observation.captured_at,
            observation.capture_started_at,
            observation.capture_completed_at,
            observation.screenshot.status.value,
            observation.screenshot.artifact.reference,
            observation.screenshot.artifact.content_type,
            observation.screenshot.artifact.size_bytes,
            observation.screenshot.artifact.sha256,
            observation.screenshot.width,
            observation.screenshot.height,
            observation.screenshot.captured_at,
            observation.ui_tree.status.value,
            ui_artifact.reference if ui_artifact else None,
            ui_artifact.content_type if ui_artifact else None,
            ui_artifact.size_bytes if ui_artifact else None,
            ui_artifact.sha256 if ui_artifact else None,
            observation.ui_tree.captured_at,
            observation.ui_tree.error_code,
            observation.device_state.status.value,
            observation.device_state.foreground_app,
            observation.device_state.screen_size[0],
            observation.device_state.screen_size[1],
            observation.device_state.orientation.value,
            observation.device_state.keyboard_state.value,
            observation.device_state.connection_state.value,
            observation.device_state.captured_at,
            observation.consistency.status.value,
            observation.consistency.reason,
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        failure_data = _load_json(row["failure_state_json"])
        return Task(
            id=row["id"],
            schema_version=row["schema_version"],
            goal=row["goal"],
            status=TaskStatus(row["status"]),
            source=TaskSource(
                client_id=row["source_client_id"],
                conversation_id=row["source_conversation_id"],
                initial_message_id=row["source_initial_message_id"],
            ),
            device_id=row["device_id"],
            current_stage_id=row["current_stage_id"],
            last_observation_id=row["last_observation_id"],
            last_meaningful_progress_at=row["last_meaningful_progress_at"],
            failure_state=FailureState(**failure_data) if failure_data else None,
            latest_checkpoint_id=row["latest_checkpoint_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            terminal_at=row["terminal_at"],
        )

    @staticmethod
    def _stage_from_row(row: sqlite3.Row) -> Stage:
        return Stage(
            id=row["id"],
            task_id=row["task_id"],
            ordinal=row["ordinal"],
            objective=row["objective"],
            completion_criteria=tuple(_load_json(row["completion_criteria_json"])),
            status=StageStatus(row["status"]),
            planner_call_id=row["planner_call_id"],
            progress_summary=row["progress_summary"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            evidence_refs=tuple(_load_json(row["evidence_refs_json"])),
        )

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> Action:
        return Action(
            id=row["id"],
            task_id=row["task_id"],
            stage_id=row["stage_id"],
            based_on_observation_id=row["based_on_observation_id"],
            type=ActionType(row["type"]),
            params=_load_json(row["params_json"]),
            expected_outcome=row["expected_outcome"],
            proposed_by_call_id=row["proposed_by_call_id"],
            proposed_at=row["proposed_at"],
            validation_status=ActionValidationStatus(row["validation_status"]),
            rejection_code=row["rejection_code"],
            status=ActionStatus(row["status"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _action_execution_from_row(row: sqlite3.Row) -> ActionExecution:
        error = None
        if row["error_code"] is not None:
            error = ExecutionError(
                code=row["error_code"],
                message=row["error_message"],
                retryable=bool(row["error_retryable"]),
            )
        return ActionExecution(
            id=row["id"],
            action_id=row["action_id"],
            device_id=row["device_id"],
            lease_ref=row["lease_ref"],
            accepted=bool(row["accepted"]),
            adapter_code=row["adapter_code"],
            error=error,
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _verification_from_row(row: sqlite3.Row) -> Verification:
        return Verification(
            id=row["id"],
            task_id=row["task_id"],
            stage_id=row["stage_id"],
            action_id=row["action_id"],
            before_observation_id=row["before_observation_id"],
            after_observation_id=row["after_observation_id"],
            verdict=VerificationVerdict(row["verdict"]),
            reason=row["reason"],
            evidence_refs=tuple(_load_json(row["evidence_refs_json"])),
            method=VerificationMethod(row["method"]),
            verification_call_id=row["verification_call_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _fact_from_row(row: sqlite3.Row) -> Fact:
        return Fact(
            id=row["id"],
            task_id=row["task_id"],
            key=row["key"],
            value=_load_json(row["value_json"]),
            status=FactStatus(row["status"]),
            confidence=row["confidence"],
            scope=FactScope(row["scope"]),
            stage_id=row["stage_id"],
            source_refs=tuple(_load_json(row["source_refs_json"])),
            supersedes_fact_id=row["supersedes_fact_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> Checkpoint:
        return Checkpoint(
            id=row["id"],
            task_id=row["task_id"],
            through_sequence=row["through_sequence"],
            goal=row["goal"],
            status_at_checkpoint=TaskStatus(row["status_at_checkpoint"]),
            current_stage_id=row["current_stage_id"],
            completed_stage_summaries=tuple(
                _load_json(row["completed_stage_summaries_json"])
            ),
            verified_facts=tuple(
                SQLiteRuntimeStore._fact_from_payload(payload)
                for payload in _load_json(row["verified_facts_json"])
            ),
            device_summary=_load_json(row["device_summary_json"]),
            last_meaningful_progress=_load_json(
                row["last_meaningful_progress_json"]
            ),
            failure_summary=_load_json(row["failure_summary_json"]),
            resume_reason=row["resume_reason"],
            required_fresh_observation=bool(row["required_fresh_observation"]),
            unresolved_action_ref=row["unresolved_action_ref"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RuntimeEvent:
        return RuntimeEvent(
            id=row["id"],
            task_id=row["task_id"],
            sequence=row["sequence"],
            type=row["type"],
            actor=EventActor(row["actor"]),
            payload=_load_json(row["payload_json"]),
            causation_id=row["causation_id"],
            correlation_id=row["correlation_id"],
            created_at=row["created_at"],
            schema_version=row["schema_version"],
        )

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> Observation:
        screenshot_artifact = ArtifactRef(
            reference=row["screenshot_artifact_ref"],
            content_type=row["screenshot_content_type"],
            size_bytes=row["screenshot_size_bytes"],
            sha256=row["screenshot_sha256"],
        )
        ui_artifact = None
        if row["ui_tree_artifact_ref"] is not None:
            ui_artifact = ArtifactRef(
                reference=row["ui_tree_artifact_ref"],
                content_type=row["ui_tree_content_type"],
                size_bytes=row["ui_tree_size_bytes"],
                sha256=row["ui_tree_sha256"],
            )
        return Observation(
            id=row["id"],
            task_id=row["task_id"],
            device_id=row["device_id"],
            captured_at=row["captured_at"],
            capture_started_at=row["capture_started_at"],
            capture_completed_at=row["capture_completed_at"],
            screenshot=ScreenshotChannel(
                status=ChannelAvailability(row["screenshot_status"]),
                artifact=screenshot_artifact,
                width=row["screenshot_width"],
                height=row["screenshot_height"],
                mime_type=row["screenshot_content_type"],
                captured_at=row["screenshot_captured_at"],
            ),
            ui_tree=UiTreeChannel(
                status=ChannelAvailability(row["ui_tree_status"]),
                artifact=ui_artifact,
                captured_at=row["ui_tree_captured_at"],
                error_code=row["ui_tree_error_code"],
            ),
            device_state=DeviceState(
                status=ChannelAvailability(row["device_state_status"]),
                foreground_app=row["foreground_app"],
                screen_size=(row["screen_width"], row["screen_height"]),
                orientation=Orientation(row["orientation"]),
                keyboard_state=KeyboardState(row["keyboard_state"]),
                connection_state=ConnectionState(row["connection_state"]),
                captured_at=row["device_state_captured_at"],
            ),
            consistency=ObservationConsistency(
                status=ConsistencyStatus(row["consistency_status"]),
                reason=row["consistency_reason"],
            ),
        )

    def acquire_lease(
        self,
        *,
        device_id: str,
        task_id: str,
        holder_process_id: str,
        ttl_seconds: int,
        lease_id: str,
        acquired_at: str,
    ) -> DeviceExecutionLease:
        """获取设备独占权，如果已被占用则抛出 LeaseConflict"""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # 检查设备是否已被占用
                existing = connection.execute(
                    """
                    SELECT id, task_id
                    FROM runtime_device_leases
                    WHERE device_id = ?
                    """,
                    (device_id,),
                ).fetchone()
                
                if existing:
                    raise LeaseConflict(
                        device_id=device_id,
                        existing_task_id=existing["task_id"],
                        existing_lease_id=existing["id"],
                        requested_task_id=task_id,
                    )
                
                # 计算过期时间
                from datetime import timedelta
                acquired_dt = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
                expires_dt = acquired_dt.replace(microsecond=0) + timedelta(seconds=ttl_seconds)
                expires_at = expires_dt.isoformat()
                
                # 插入新 Lease
                connection.execute(
                    """
                    INSERT INTO runtime_device_leases (
                        id, device_id, task_id, holder_process_id,
                        acquired_at, expires_at, last_heartbeat_at, action_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease_id,
                        device_id,
                        task_id,
                        holder_process_id,
                        acquired_at,
                        expires_at,
                        acquired_at,
                        None,
                    ),
                )
                connection.commit()
                
                return DeviceExecutionLease(
                    id=lease_id,
                    device_id=device_id,
                    task_id=task_id,
                    holder_process_id=holder_process_id,
                    acquired_at=acquired_at,
                    expires_at=expires_at,
                    last_heartbeat_at=acquired_at,
                    action_id=None,
                )
            except Exception:
                connection.rollback()
                raise

    def renew_lease(
        self, lease_id: str, new_expires_at: str, new_heartbeat_at: str
    ) -> DeviceExecutionLease:
        """续期 Lease"""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT id, device_id, task_id, holder_process_id,
                           acquired_at, expires_at, last_heartbeat_at, action_id
                    FROM runtime_device_leases
                    WHERE id = ?
                    """,
                    (lease_id,),
                ).fetchone()
                
                if not row:
                    raise LeaseNotFound(lease_id=lease_id)
                
                connection.execute(
                    """
                    UPDATE runtime_device_leases
                    SET expires_at = ?, last_heartbeat_at = ?
                    WHERE id = ?
                    """,
                    (new_expires_at, new_heartbeat_at, lease_id),
                )
                connection.commit()
                
                return DeviceExecutionLease(
                    id=row["id"],
                    device_id=row["device_id"],
                    task_id=row["task_id"],
                    holder_process_id=row["holder_process_id"],
                    acquired_at=row["acquired_at"],
                    expires_at=new_expires_at,
                    last_heartbeat_at=new_heartbeat_at,
                    action_id=row["action_id"],
                )
            except Exception:
                connection.rollback()
                raise

    def release_lease(self, lease_id: str) -> None:
        """释放 Lease"""
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM runtime_device_leases WHERE id = ?",
                (lease_id,),
            )
            connection.commit()

    def get_lease_for_device(self, device_id: str) -> DeviceExecutionLease | None:
        """查询设备当前 Lease"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, device_id, task_id, holder_process_id,
                       acquired_at, expires_at, last_heartbeat_at, action_id
                FROM runtime_device_leases
                WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()
            
            if not row:
                return None
            
            return self._lease_from_row(row)

    def get_lease_for_task(self, task_id: str) -> DeviceExecutionLease | None:
        """查询 Task 当前 Lease"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, device_id, task_id, holder_process_id,
                       acquired_at, expires_at, last_heartbeat_at, action_id
                FROM runtime_device_leases
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            
            if not row:
                return None
            
            return self._lease_from_row(row)

    def list_expired_leases(self, now: str) -> tuple[DeviceExecutionLease, ...]:
        """查询所有已过期的 Lease"""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, device_id, task_id, holder_process_id,
                       acquired_at, expires_at, last_heartbeat_at, action_id
                FROM runtime_device_leases
                WHERE expires_at <= ?
                ORDER BY expires_at
                """,
                (now,),
            ).fetchall()
            
            return tuple(self._lease_from_row(row) for row in rows)

    def update_lease_action(self, lease_id: str, action_id: str | None) -> None:
        """更新 Lease 关联的 Action"""
        with self._connection() as connection:
            connection.execute(
                "UPDATE runtime_device_leases SET action_id = ? WHERE id = ?",
                (action_id, lease_id),
            )
            connection.commit()

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> DeviceExecutionLease:
        return DeviceExecutionLease(
            id=row["id"],
            device_id=row["device_id"],
            task_id=row["task_id"],
            holder_process_id=row["holder_process_id"],
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            action_id=row["action_id"],
        )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)
