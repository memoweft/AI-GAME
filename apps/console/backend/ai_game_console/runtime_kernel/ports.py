from __future__ import annotations

from typing import Protocol

from .event import RuntimeEvent, RuntimeEventDraft
from .observation import ArtifactRef, Observation, RawObservation
from .stage import Stage
from .task import Task


class RuntimeStoreError(RuntimeError):
    """Base error for the persistent Runtime Store port."""


class RecordNotFound(RuntimeStoreError):
    """Raised when a Task or Stage is absent from the Runtime Store."""


class StoreConflict(RuntimeStoreError):
    """Raised for stale mutations or violated persistent invariants."""


class RuntimeStorePort(Protocol):
    def initialize(self) -> None: ...

    def close(self) -> None: ...

    def create_task(self, task: Task, event: RuntimeEventDraft) -> RuntimeEvent: ...

    def load_task(self, task_id: str) -> Task: ...

    def create_stage(self, stage: Stage, event: RuntimeEventDraft) -> RuntimeEvent: ...

    def load_stage(self, task_id: str, stage_id: str) -> Stage: ...

    def list_stages(self, task_id: str) -> tuple[Stage, ...]: ...

    def mutate_task_and_stage(
        self,
        *,
        before_task: Task,
        after_task: Task,
        before_stage: Stage,
        after_stage: Stage,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent: ...

    def append_event(self, task_id: str, event: RuntimeEventDraft) -> RuntimeEvent: ...

    def list_events(
        self, task_id: str, *, after_sequence: int = 0
    ) -> tuple[RuntimeEvent, ...]: ...

    def persist_observation(
        self,
        *,
        before_task: Task,
        after_task: Task,
        observation: Observation,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent: ...

    def load_observation(self, observation_id: str) -> Observation: ...

    def list_observations(self, task_id: str) -> tuple[Observation, ...]: ...

    def latest_observation(self, task_id: str) -> Observation | None: ...


class ObservationProviderPort(Protocol):
    def capture(self, device_id: str) -> RawObservation: ...


class ArtifactStorePort(Protocol):
    def write(
        self, *, artifact_id: str, content_type: str, content: bytes
    ) -> ArtifactRef: ...

    def delete(self, artifact: ArtifactRef) -> None: ...
