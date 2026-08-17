from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ...runtime_kernel.event import EventActor, RuntimeEvent, RuntimeEventDraft
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


_SCHEMA_REVISION = 2

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
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for statement in _MIGRATION_1_TO_2.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT INTO runtime_schema(revision, applied_at) VALUES (2, ?)",
                        (datetime.now(timezone.utc).isoformat(),),
                    )
                    connection.execute("PRAGMA user_version = 2")
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


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)
