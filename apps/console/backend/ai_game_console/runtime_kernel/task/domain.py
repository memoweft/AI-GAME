from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum


class InvalidTaskTransition(ValueError):
    """Raised when a Task lifecycle transition violates the frozen state model."""


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    STUCK = "STUCK"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

_ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED}),
    TaskStatus.PLANNING: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.WAITING,
            TaskStatus.PAUSED,
            TaskStatus.STUCK,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.STUCK: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TaskSource:
    client_id: str
    conversation_id: str
    initial_message_id: str

    def __post_init__(self) -> None:
        _required(self.client_id, "source.client_id")
        _required(self.conversation_id, "source.conversation_id")
        _required(self.initial_message_id, "source.initial_message_id")


@dataclass(frozen=True, slots=True)
class FailureState:
    code: str
    summary: str
    retry_count: int
    no_progress_count: int
    last_failed_action_id: str | None
    last_verdict: str | None
    recoverable: bool
    updated_at: str

    def __post_init__(self) -> None:
        _required(self.code, "failure_state.code")
        _required(self.summary, "failure_state.summary")
        if self.retry_count < 0 or self.no_progress_count < 0:
            raise ValueError("failure counters must not be negative")
        if self.last_verdict not in {None, "FAIL", "UNCERTAIN"}:
            raise ValueError("failure_state.last_verdict must be FAIL, UNCERTAIN, or null")
        _utc_timestamp(self.updated_at, "failure_state.updated_at")


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    schema_version: int
    goal: str
    status: TaskStatus
    source: TaskSource
    device_id: str
    current_stage_id: str | None
    last_observation_id: str | None
    last_meaningful_progress_at: str | None
    failure_state: FailureState | None
    latest_checkpoint_id: str | None
    created_at: str
    updated_at: str
    terminal_at: str | None

    def __post_init__(self) -> None:
        _required(self.id, "task.id")
        if self.schema_version < 1:
            raise ValueError("task.schema_version must be positive")
        _required(self.goal, "task.goal")
        _required(self.device_id, "task.device_id")
        _utc_timestamp(self.created_at, "task.created_at")
        _utc_timestamp(self.updated_at, "task.updated_at")
        if self.last_meaningful_progress_at is not None:
            _utc_timestamp(
                self.last_meaningful_progress_at,
                "task.last_meaningful_progress_at",
            )
        if self.terminal_at is not None:
            _utc_timestamp(self.terminal_at, "task.terminal_at")
        if self.status in TERMINAL_TASK_STATUSES and self.terminal_at is None:
            raise ValueError("terminal Task status requires terminal_at")
        if self.status not in TERMINAL_TASK_STATUSES and self.terminal_at is not None:
            raise ValueError("nonterminal Task must not have terminal_at")
        if self.status is TaskStatus.CREATED and self.current_stage_id is not None:
            raise ValueError("CREATED Task cannot reference an active Stage")

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        goal: str,
        source: TaskSource,
        device_id: str,
        created_at: str,
        schema_version: int = 1,
    ) -> Task:
        return cls(
            id=task_id,
            schema_version=schema_version,
            goal=goal,
            status=TaskStatus.CREATED,
            source=source,
            device_id=device_id,
            current_stage_id=None,
            last_observation_id=None,
            last_meaningful_progress_at=None,
            failure_state=None,
            latest_checkpoint_id=None,
            created_at=created_at,
            updated_at=created_at,
            terminal_at=None,
        )

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    def transition_to(self, status: TaskStatus, *, at: str) -> Task:
        _utc_timestamp(at, "transition timestamp")
        if status not in _ALLOWED_TASK_TRANSITIONS[self.status]:
            raise InvalidTaskTransition(
                f"Task cannot transition from {self.status.value} to {status.value}"
            )
        return replace(
            self,
            status=status,
            updated_at=at,
            terminal_at=at if status in TERMINAL_TASK_STATUSES else None,
        )

    def start_stage(self, stage_id: str, *, at: str) -> Task:
        _required(stage_id, "stage_id")
        if self.current_stage_id is not None:
            raise InvalidTaskTransition("Task already references an active Stage")
        if self.status is TaskStatus.CREATED:
            planning = self.transition_to(TaskStatus.PLANNING, at=at)
        elif self.status is TaskStatus.PLANNING:
            planning = self
        else:
            raise InvalidTaskTransition(
                f"Task in {self.status.value} cannot start a Stage"
            )
        running = planning.transition_to(TaskStatus.RUNNING, at=at)
        return replace(running, current_stage_id=stage_id, updated_at=at)

    def complete_current_stage(self, stage_id: str, *, at: str) -> Task:
        _required(stage_id, "stage_id")
        if self.status is not TaskStatus.RUNNING:
            raise InvalidTaskTransition("only a RUNNING Task can complete its Stage")
        if self.current_stage_id != stage_id:
            raise InvalidTaskTransition("completed Stage is not the Task's active Stage")
        planning = self.transition_to(TaskStatus.PLANNING, at=at)
        return replace(planning, current_stage_id=None, updated_at=at)

    def record_observation(self, observation_id: str, *, at: str) -> Task:
        _required(observation_id, "observation_id")
        _utc_timestamp(at, "observation timestamp")
        return replace(self, last_observation_id=observation_id, updated_at=at)

    def record_progress(self, *, at: str) -> Task:
        _utc_timestamp(at, "progress timestamp")
        return replace(
            self,
            last_meaningful_progress_at=at,
            failure_state=None,
            updated_at=at,
        )

    def record_failure(self, failure_state: FailureState, *, at: str) -> Task:
        if self.terminal:
            raise InvalidTaskTransition("terminal Task cannot record another failure")
        _utc_timestamp(at, "failure timestamp")
        if failure_state.updated_at != at:
            raise ValueError("failure_state.updated_at must match its Task mutation")
        return replace(self, failure_state=failure_state, updated_at=at)

    def record_checkpoint(self, checkpoint_id: str, *, at: str) -> Task:
        _required(checkpoint_id, "checkpoint_id")
        _utc_timestamp(at, "checkpoint timestamp")
        return replace(self, latest_checkpoint_id=checkpoint_id, updated_at=at)


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def _utc_timestamp(value: str, name: str) -> str:
    _required(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
    return value
