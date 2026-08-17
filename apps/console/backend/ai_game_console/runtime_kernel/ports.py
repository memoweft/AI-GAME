from __future__ import annotations

from typing import Protocol

from .action import Action, ActionExecution
from .checkpoint import Checkpoint, CheckpointDraft
from .event import RuntimeEvent, RuntimeEventDraft
from .fact import Fact
from .observation import ArtifactRef, Observation, RawObservation
from .stage import Stage
from .task import Task
from .verify import Verification


class RuntimeStoreError(RuntimeError):
    """Base error for the persistent Runtime Store port."""


class RecordNotFound(RuntimeStoreError):
    """Raised when a Runtime record is absent from the Runtime Store."""


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

    def create_action(self, action: Action, event: RuntimeEventDraft) -> RuntimeEvent: ...

    def load_action(self, task_id: str, action_id: str) -> Action: ...

    def list_actions(self, task_id: str) -> tuple[Action, ...]: ...

    def record_action_execution(
        self,
        *,
        before_task: Task,
        after_task: Task,
        before_action: Action,
        after_action: Action,
        execution: ActionExecution,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent: ...

    def load_action_execution(self, action_id: str) -> ActionExecution: ...

    def record_verification(
        self,
        *,
        before_task: Task,
        after_task: Task,
        before_action: Action,
        after_action: Action,
        verification: Verification,
        event: RuntimeEventDraft,
    ) -> RuntimeEvent: ...

    def load_verification(self, action_id: str) -> Verification: ...

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
    ) -> tuple[tuple[RuntimeEvent, ...], Checkpoint | None]: ...

    def list_facts(self, task_id: str, *, verified_only: bool = False) -> tuple[Fact, ...]: ...

    def create_checkpoint(
        self,
        *,
        before_task: Task,
        after_task: Task,
        checkpoint: CheckpointDraft,
        event: RuntimeEventDraft,
    ) -> tuple[Checkpoint, RuntimeEvent]: ...

    def load_checkpoint(self, task_id: str, checkpoint_id: str) -> Checkpoint: ...

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None: ...


class ObservationProviderPort(Protocol):
    def capture(self, device_id: str) -> RawObservation: ...


class ArtifactStorePort(Protocol):
    def write(
        self, *, artifact_id: str, content_type: str, content: bytes
    ) -> ArtifactRef: ...

    def delete(self, artifact: ArtifactRef) -> None: ...
