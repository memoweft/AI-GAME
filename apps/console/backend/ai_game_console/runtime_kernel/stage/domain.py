from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable


class InvalidStageTransition(ValueError):
    """Raised when a Stage lifecycle transition violates the frozen model."""


class StageStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


_ALLOWED_STAGE_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset({StageStatus.ACTIVE, StageStatus.SUPERSEDED}),
    StageStatus.ACTIVE: frozenset(
        {StageStatus.COMPLETED, StageStatus.FAILED, StageStatus.SUPERSEDED}
    ),
    StageStatus.COMPLETED: frozenset(),
    StageStatus.FAILED: frozenset(),
    StageStatus.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Stage:
    id: str
    task_id: str
    ordinal: int
    objective: str
    completion_criteria: tuple[str, ...]
    status: StageStatus
    planner_call_id: str | None
    progress_summary: str | None
    started_at: str | None
    completed_at: str | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.id, "stage.id")
        _required(self.task_id, "stage.task_id")
        if self.ordinal < 1:
            raise ValueError("stage.ordinal must be positive")
        _required(self.objective, "stage.objective")
        if not self.completion_criteria:
            raise ValueError("stage.completion_criteria must not be empty")
        for criterion in self.completion_criteria:
            _required(criterion, "stage.completion_criteria item")
        for evidence_ref in self.evidence_refs:
            _required(evidence_ref, "stage.evidence_refs item")
        if self.started_at is not None:
            _utc_timestamp(self.started_at, "stage.started_at")
        if self.completed_at is not None:
            _utc_timestamp(self.completed_at, "stage.completed_at")
        if self.status is StageStatus.PENDING:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("PENDING Stage cannot have lifecycle timestamps")
        elif self.status is StageStatus.ACTIVE:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("ACTIVE Stage requires started_at only")
        else:
            if self.status is StageStatus.COMPLETED and not self.evidence_refs:
                raise ValueError("COMPLETED Stage requires evidence references")
            if self.completed_at is None:
                raise ValueError("terminal Stage status requires completed_at")

    @classmethod
    def create(
        cls,
        *,
        stage_id: str,
        task_id: str,
        ordinal: int,
        objective: str,
        completion_criteria: Iterable[str],
        planner_call_id: str | None = None,
    ) -> Stage:
        return cls(
            id=stage_id,
            task_id=task_id,
            ordinal=ordinal,
            objective=objective,
            completion_criteria=tuple(completion_criteria),
            status=StageStatus.PENDING,
            planner_call_id=planner_call_id,
            progress_summary=None,
            started_at=None,
            completed_at=None,
            evidence_refs=(),
        )

    def activate(self, *, at: str) -> Stage:
        self._require_transition(StageStatus.ACTIVE)
        _utc_timestamp(at, "Stage activation timestamp")
        return replace(self, status=StageStatus.ACTIVE, started_at=at)

    def complete(
        self,
        *,
        at: str,
        evidence_refs: Iterable[str],
        progress_summary: str | None = None,
    ) -> Stage:
        self._require_transition(StageStatus.COMPLETED)
        _utc_timestamp(at, "Stage completion timestamp")
        refs = tuple(evidence_refs)
        if not refs:
            raise ValueError("Stage completion requires evidence references")
        for ref in refs:
            _required(ref, "Stage completion evidence reference")
        if progress_summary is not None:
            _required(progress_summary, "Stage progress_summary")
        return replace(
            self,
            status=StageStatus.COMPLETED,
            progress_summary=progress_summary,
            completed_at=at,
            evidence_refs=refs,
        )

    def _require_transition(self, status: StageStatus) -> None:
        if status not in _ALLOWED_STAGE_TRANSITIONS[self.status]:
            raise InvalidStageTransition(
                f"Stage cannot transition from {self.status.value} to {status.value}"
            )


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

