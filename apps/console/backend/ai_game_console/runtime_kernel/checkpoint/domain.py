from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ..fact import Fact
from ..task import TaskStatus


@dataclass(frozen=True, slots=True)
class CheckpointDraft:
    id: str
    task_id: str
    goal: str
    status_at_checkpoint: TaskStatus
    current_stage_id: str | None
    completed_stage_summaries: tuple[Mapping[str, Any], ...]
    verified_facts: tuple[Fact, ...]
    device_summary: Mapping[str, Any]
    last_meaningful_progress: Mapping[str, Any] | None
    failure_summary: Mapping[str, Any] | None
    resume_reason: str
    required_fresh_observation: bool
    unresolved_action_ref: str | None
    created_at: str

    def __post_init__(self) -> None:
        _required(self.id, "checkpoint.id")
        _required(self.task_id, "checkpoint.task_id")
        _required(self.goal, "checkpoint.goal")
        for summary in self.completed_stage_summaries:
            if not isinstance(summary, Mapping):
                raise ValueError("checkpoint completed stage summary must be a mapping")
        for fact in self.verified_facts:
            if fact.task_id != self.task_id or fact.status.value != "VERIFIED":
                raise ValueError("checkpoint can include only verified Facts from its Task")
        if not isinstance(self.device_summary, Mapping):
            raise ValueError("checkpoint.device_summary must be a mapping")
        if self.last_meaningful_progress is not None and not isinstance(
            self.last_meaningful_progress, Mapping
        ):
            raise ValueError("checkpoint.last_meaningful_progress must be a mapping")
        if self.failure_summary is not None and not isinstance(self.failure_summary, Mapping):
            raise ValueError("checkpoint.failure_summary must be a mapping")
        _required(self.resume_reason, "checkpoint.resume_reason")
        if self.unresolved_action_ref is not None:
            _required(self.unresolved_action_ref, "checkpoint.unresolved_action_ref")
            if not self.required_fresh_observation:
                raise ValueError(
                    "unresolved Action requires a fresh Observation before recovery"
                )
        _utc_timestamp(self.created_at, "checkpoint.created_at")

    def materialize(self, *, through_sequence: int) -> Checkpoint:
        return Checkpoint(
            id=self.id,
            task_id=self.task_id,
            through_sequence=through_sequence,
            goal=self.goal,
            status_at_checkpoint=self.status_at_checkpoint,
            current_stage_id=self.current_stage_id,
            completed_stage_summaries=self.completed_stage_summaries,
            verified_facts=self.verified_facts,
            device_summary=self.device_summary,
            last_meaningful_progress=self.last_meaningful_progress,
            failure_summary=self.failure_summary,
            resume_reason=self.resume_reason,
            required_fresh_observation=self.required_fresh_observation,
            unresolved_action_ref=self.unresolved_action_ref,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class Checkpoint:
    id: str
    task_id: str
    through_sequence: int
    goal: str
    status_at_checkpoint: TaskStatus
    current_stage_id: str | None
    completed_stage_summaries: tuple[Mapping[str, Any], ...]
    verified_facts: tuple[Fact, ...]
    device_summary: Mapping[str, Any]
    last_meaningful_progress: Mapping[str, Any] | None
    failure_summary: Mapping[str, Any] | None
    resume_reason: str
    required_fresh_observation: bool
    unresolved_action_ref: str | None
    created_at: str

    def __post_init__(self) -> None:
        if self.through_sequence < 1:
            raise ValueError("checkpoint.through_sequence must be positive")
        CheckpointDraft(
            id=self.id,
            task_id=self.task_id,
            goal=self.goal,
            status_at_checkpoint=self.status_at_checkpoint,
            current_stage_id=self.current_stage_id,
            completed_stage_summaries=self.completed_stage_summaries,
            verified_facts=self.verified_facts,
            device_summary=self.device_summary,
            last_meaningful_progress=self.last_meaningful_progress,
            failure_summary=self.failure_summary,
            resume_reason=self.resume_reason,
            required_fresh_observation=self.required_fresh_observation,
            unresolved_action_ref=self.unresolved_action_ref,
            created_at=self.created_at,
        )


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def _utc_timestamp(value: str, name: str) -> datetime:
    _required(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
    return parsed
