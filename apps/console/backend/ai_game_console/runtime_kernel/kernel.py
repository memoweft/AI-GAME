from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from uuid import uuid4

from .event import EventActor, RuntimeEvent, RuntimeEventDraft
from .observation import (
    ArtifactRef,
    ChannelAvailability,
    Observation,
    ScreenshotChannel,
    UiTreeChannel,
)
from .ports import (
    ArtifactStorePort,
    ObservationProviderPort,
    RuntimeStoreError,
    RuntimeStorePort,
)
from .stage import Stage, StageStatus
from .task import Task, TaskSource, TaskStatus


class RuntimeKernel:
    """Minimal application service for the isolated persistent Runtime spine."""

    def __init__(
        self,
        store: RuntimeStorePort,
        *,
        observation_provider: ObservationProviderPort | None = None,
        artifact_store: ArtifactStorePort | None = None,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._observation_provider = observation_provider
        self._artifact_store = artifact_store
        self._clock = clock or _utc_now
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._store.initialize()

    def close(self) -> None:
        self._store.close()

    def create_task(
        self,
        *,
        goal: str,
        source: TaskSource,
        device_id: str,
        task_id: str | None = None,
    ) -> Task:
        now = self._clock()
        task = Task.create(
            task_id=task_id or self._id_factory(),
            goal=goal,
            source=source,
            device_id=device_id,
            created_at=now,
        )
        self._store.create_task(
            task,
            self._event(
                "TaskCreated",
                now,
                task.id,
                {
                    "goal": task.goal,
                    "source": {
                        "client_id": task.source.client_id,
                        "conversation_id": task.source.conversation_id,
                        "initial_message_id": task.source.initial_message_id,
                    },
                    "device_id": task.device_id,
                    "status": task.status.value,
                },
            ),
        )
        return task

    def load_task(self, task_id: str) -> Task:
        return self._store.load_task(task_id)

    def create_stage(
        self,
        *,
        task_id: str,
        objective: str,
        completion_criteria: Iterable[str],
        planner_call_id: str | None = None,
        stage_id: str | None = None,
    ) -> Stage:
        task = self._store.load_task(task_id)
        if task.status not in {TaskStatus.CREATED, TaskStatus.PLANNING}:
            raise ValueError(
                f"Task in {task.status.value} cannot accept a planned Stage"
            )
        stages = self._store.list_stages(task_id)
        ordinal = max((stage.ordinal for stage in stages), default=0) + 1
        stage = Stage.create(
            stage_id=stage_id or self._id_factory(),
            task_id=task_id,
            ordinal=ordinal,
            objective=objective,
            completion_criteria=completion_criteria,
            planner_call_id=planner_call_id,
        )
        now = self._clock()
        self._store.create_stage(
            stage,
            self._event(
                "StageCreated",
                now,
                task_id,
                {
                    "stage_id": stage.id,
                    "ordinal": stage.ordinal,
                    "status": stage.status.value,
                },
            ),
        )
        return stage

    def load_stage(self, task_id: str, stage_id: str) -> Stage:
        return self._store.load_stage(task_id, stage_id)

    def list_stages(self, task_id: str) -> tuple[Stage, ...]:
        return self._store.list_stages(task_id)

    def current_stage(self, task_id: str) -> Stage | None:
        task = self._store.load_task(task_id)
        if task.current_stage_id is None:
            return None
        return self._store.load_stage(task_id, task.current_stage_id)

    def start_stage(self, *, task_id: str, stage_id: str) -> Stage:
        before_task = self._store.load_task(task_id)
        before_stage = self._store.load_stage(task_id, stage_id)
        if before_stage.task_id != task_id:
            raise ValueError("Stage does not belong to Task")
        now = self._clock()
        after_stage = before_stage.activate(at=now)
        after_task = before_task.start_stage(stage_id, at=now)
        self._store.mutate_task_and_stage(
            before_task=before_task,
            after_task=after_task,
            before_stage=before_stage,
            after_stage=after_stage,
            event=self._event(
                "StageStarted",
                now,
                task_id,
                {
                    "stage_id": stage_id,
                    "ordinal": after_stage.ordinal,
                    "objective": after_stage.objective,
                    "completion_criteria": list(after_stage.completion_criteria),
                    "status": StageStatus.ACTIVE.value,
                },
            ),
        )
        return after_stage

    def complete_stage(
        self,
        *,
        task_id: str,
        stage_id: str,
        evidence_refs: Iterable[str],
        progress_summary: str | None = None,
    ) -> Stage:
        before_task = self._store.load_task(task_id)
        before_stage = self._store.load_stage(task_id, stage_id)
        if before_stage.task_id != task_id:
            raise ValueError("Stage does not belong to Task")
        now = self._clock()
        after_stage = before_stage.complete(
            at=now,
            evidence_refs=evidence_refs,
            progress_summary=progress_summary,
        )
        after_task = before_task.complete_current_stage(stage_id, at=now)
        self._store.mutate_task_and_stage(
            before_task=before_task,
            after_task=after_task,
            before_stage=before_stage,
            after_stage=after_stage,
            event=self._event(
                "StageCompleted",
                now,
                task_id,
                {
                    "stage_id": stage_id,
                    "status": StageStatus.COMPLETED.value,
                    "evidence_refs": list(after_stage.evidence_refs),
                },
            ),
        )
        return after_stage

    def events(
        self, task_id: str, *, after_sequence: int = 0
    ) -> tuple[RuntimeEvent, ...]:
        return self._store.list_events(task_id, after_sequence=after_sequence)

    def capture_observation(
        self,
        *,
        task_id: str,
        device_id: str,
        observation_id: str | None = None,
    ) -> Observation:
        if self._observation_provider is None or self._artifact_store is None:
            raise RuntimeError("observation capability is not configured")
        task = self._store.load_task(task_id)
        if task.device_id != device_id:
            raise ValueError("explicit device_id does not match the Task")

        raw = self._observation_provider.capture(device_id)
        if raw.device_id != device_id:
            raise ValueError("Observation Provider returned a different device_id")
        if raw.screenshot.status is not ChannelAvailability.AVAILABLE:
            raise RuntimeError(
                raw.screenshot.error_code or "observation_screenshot_required"
            )
        if raw.device_state.status is not ChannelAvailability.AVAILABLE:
            raise RuntimeError("observation_device_state_required")

        resolved_id = observation_id or self._id_factory()
        written: list[ArtifactRef] = []
        try:
            screenshot_ref = self._artifact_store.write(
                artifact_id=f"{resolved_id}/screenshot",
                content_type=raw.screenshot.content_type,
                content=raw.screenshot.content or b"",
            )
            written.append(screenshot_ref)
            ui_tree_ref = None
            if raw.ui_tree.status is ChannelAvailability.AVAILABLE:
                ui_tree_ref = self._artifact_store.write(
                    artifact_id=f"{resolved_id}/ui-tree",
                    content_type=raw.ui_tree.content_type,
                    content=raw.ui_tree.content or b"",
                )
                written.append(ui_tree_ref)

            observation = Observation(
                id=resolved_id,
                task_id=task_id,
                device_id=device_id,
                captured_at=raw.capture_completed_at,
                capture_started_at=raw.capture_started_at,
                capture_completed_at=raw.capture_completed_at,
                screenshot=ScreenshotChannel(
                    status=ChannelAvailability.AVAILABLE,
                    artifact=screenshot_ref,
                    width=raw.screenshot.width or 0,
                    height=raw.screenshot.height or 0,
                    mime_type=raw.screenshot.content_type,
                    captured_at=raw.screenshot.captured_at,
                ),
                ui_tree=UiTreeChannel(
                    status=raw.ui_tree.status,
                    artifact=ui_tree_ref,
                    captured_at=raw.ui_tree.captured_at,
                    error_code=raw.ui_tree.error_code,
                ),
                device_state=raw.device_state,
                consistency=raw.consistency,
            )
            after_task = task.record_observation(
                observation.id, at=observation.captured_at
            )
        except Exception:
            self._cleanup_artifacts(written)
            raise

        try:
            self._store.persist_observation(
                before_task=task,
                after_task=after_task,
                observation=observation,
                event=self._event(
                    "ObservationReceived",
                    observation.captured_at,
                    task_id,
                    {
                        "observation_id": observation.id,
                        "device_id": observation.device_id,
                        "channels": {
                            "screenshot": observation.screenshot.status.value,
                            "ui_tree": observation.ui_tree.status.value,
                            "device_state": observation.device_state.status.value,
                        },
                        "capture_started_at": observation.capture_started_at,
                        "capture_completed_at": observation.capture_completed_at,
                        "consistency": observation.consistency.status.value,
                    },
                ),
            )
        except RuntimeStoreError:
            # Port errors certify that the Store transaction did not commit.
            # Unknown exceptions deliberately leave recognizable orphans rather
            # than risk deleting files after an uncertain commit outcome.
            self._cleanup_artifacts(written)
            raise
        return observation

    def load_observation(self, observation_id: str) -> Observation:
        return self._store.load_observation(observation_id)

    def observations(self, task_id: str) -> tuple[Observation, ...]:
        return self._store.list_observations(task_id)

    def latest_observation(self, task_id: str) -> Observation | None:
        return self._store.latest_observation(task_id)

    def _cleanup_artifacts(self, artifacts: list[ArtifactRef]) -> None:
        if self._artifact_store is None:
            return
        for artifact in reversed(artifacts):
            try:
                self._artifact_store.delete(artifact)
            except Exception:
                # Orphans are the allowed cross-store failure mode. They are
                # content-addressable by metadata and never referenced by a
                # committed Observation when cleanup follows a Store failure.
                pass

    def _event(
        self,
        event_type: str,
        created_at: str,
        task_id: str,
        payload: dict[str, object],
    ) -> RuntimeEventDraft:
        return RuntimeEventDraft(
            id=self._id_factory(),
            type=event_type,
            actor=EventActor.RUNTIME,
            payload=payload,
            correlation_id=task_id,
            created_at=created_at,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
