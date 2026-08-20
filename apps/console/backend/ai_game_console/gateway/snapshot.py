"""Gateway Task Snapshot projection (frozen contract §6).

``GET /api/v1/tasks/{task_id}`` returns a *Gateway Snapshot*, not a raw
Store row. The projection is read from the Runtime Kernel's public
service surface only:

* ``load_task`` / ``current_stage`` / ``list_stages`` for stage state;
* ``verified_facts`` for verified facts only (no unverified claims);
* ``events`` for ``last_event_sequence``;
* ``task.last_observation_id`` for the latest observation reference.

The Snapshot and the event stream are the two calibration surfaces the
client uses to keep its view of the Task consistent (§17).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..runtime_kernel import Fact, RuntimeKernel, Stage, StageStatus, Task

# The frozen contract reserves a `constraints` array on the Snapshot; the
# kernel has no constraints model yet, so the projection is always empty
# until that domain lands.
_CONSTRAINTS: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class StageProjection:
    id: str
    objective: str
    completion_criteria: tuple[str, ...]

    @classmethod
    def from_stage(cls, stage: Stage) -> StageProjection:
        return cls(
            id=stage.id,
            objective=stage.objective,
            completion_criteria=tuple(stage.completion_criteria),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "completion_criteria": list(self.completion_criteria),
        }


@dataclass(frozen=True, slots=True)
class CompletedStageProjection:
    id: str
    objective: str
    completion_criteria: tuple[str, ...]
    completed_at: str | None

    @classmethod
    def from_stage(cls, stage: Stage) -> CompletedStageProjection:
        return cls(
            id=stage.id,
            objective=stage.objective,
            completion_criteria=tuple(stage.completion_criteria),
            completed_at=stage.completed_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "completion_criteria": list(self.completion_criteria),
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class VerifiedFactProjection:
    id: str
    key: str
    value: Any
    confidence: float | None
    created_at: str

    @classmethod
    def from_fact(cls, fact: Fact) -> VerifiedFactProjection:
        return cls(
            id=fact.id,
            key=fact.key,
            value=fact.value,
            confidence=fact.confidence,
            created_at=fact.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "confidence": self.confidence,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    id: str
    goal: str
    status: str
    device_id: str
    constraints: tuple[dict[str, Any], ...]
    current_stage: StageProjection | None
    completed_stages: tuple[CompletedStageProjection, ...]
    verified_facts: tuple[VerifiedFactProjection, ...]
    last_observation_id: str | None
    last_event_sequence: int
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "device_id": self.device_id,
            "constraints": [c for c in self.constraints],
            "current_stage": self.current_stage.to_dict() if self.current_stage else None,
            "completed_stages": [s.to_dict() for s in self.completed_stages],
            "verified_facts": [f.to_dict() for f in self.verified_facts],
            "last_observation_id": self.last_observation_id,
            "last_event_sequence": self.last_event_sequence,
            "updated_at": self.updated_at,
        }


def build_task_snapshot(kernel: RuntimeKernel, task: Task) -> TaskSnapshot:
    """Project a Kernel Task into the frozen contract §6 Snapshot shape."""
    stages = kernel.list_stages(task.id)
    current_stage_id = task.current_stage_id
    current_stage: StageProjection | None = None
    if current_stage_id is not None:
        current = next(
            (stage for stage in stages if stage.id == current_stage_id), None
        )
        if current is not None and current.status is not StageStatus.COMPLETED:
            current_stage = StageProjection.from_stage(current)
    completed = tuple(
        CompletedStageProjection.from_stage(stage)
        for stage in stages
        if stage.status is StageStatus.COMPLETED
    )
    events = kernel.events(task.id)
    last_event_sequence = events[-1].sequence if events else 0
    return TaskSnapshot(
        id=task.id,
        goal=task.goal,
        status=task.status.value,
        device_id=task.device_id,
        constraints=_CONSTRAINTS,
        current_stage=current_stage,
        completed_stages=completed,
        verified_facts=tuple(
            VerifiedFactProjection.from_fact(fact)
            for fact in kernel.verified_facts(task.id)
        ),
        last_observation_id=task.last_observation_id,
        last_event_sequence=last_event_sequence,
        updated_at=task.updated_at,
    )
