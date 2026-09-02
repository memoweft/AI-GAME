from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, replace
from datetime import datetime, timezone
from uuid import uuid4

from ..device_lease_manager import DeviceLeaseManager
from .action import Action, ActionExecution, ActionStatus, ActionType, ExecutionError
from .checkpoint import Checkpoint, CheckpointDraft
from .control import ControlCommand, ControlError, ControlResult, InvalidControlTransition
from .event import EventActor, RuntimeEvent, RuntimeEventDraft
from .executor import ActionExecutionResult, ActionExecutorPort
from .fact import Fact, FactScope, FactStatus
from .observation import (
    ArtifactRef,
    BodySnapshotCorrelation,
    ChannelAvailability,
    Observation,
    ScreenshotChannel,
    UiTreeChannel,
)
from .ports import (
    ArtifactStorePort,
    BodyCommandDispatcherPort,
    ObservationProviderPort,
    RecordNotFound,
    RuntimeStoreError,
    RuntimeStorePort,
    StoreConflict,
)
from .stage import Stage, StageStatus
from .task import FailureState, Task, TaskSource, TaskStatus
from .verify import Verification, VerificationMethod, VerificationVerdict


class RuntimeKernel:
    """Minimal application service for the isolated persistent Runtime spine."""

    def __init__(
        self,
        store: RuntimeStorePort,
        *,
        observation_provider: ObservationProviderPort | None = None,
        artifact_store: ArtifactStorePort | None = None,
        action_executor: ActionExecutorPort | None = None,
        body_dispatcher: BodyCommandDispatcherPort | None = None,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
        lease_manager: DeviceLeaseManager | None = None,
    ) -> None:
        self._store = store
        self._observation_provider = observation_provider
        self._artifact_store = artifact_store
        self._action_executor = action_executor
        self._body_dispatcher = body_dispatcher
        self._clock = clock or _utc_now
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._lease_manager = lease_manager
        # Plaintext typed text is intentionally process-local only.  SQLite
        # actions carry an opaque redacted descriptor, so a restart loses this
        # envelope and must fail closed/replan rather than replay credentials.
        self._transient_typed_text: dict[str, str] = {}
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

    def list_tasks_by_conversation(self, conversation_id: str) -> tuple[Task, ...]:
        return self._store.list_tasks_by_conversation(conversation_id)

    def list_tasks(self) -> tuple[Task, ...]:
        return self._store.list_tasks()

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

    def record_worker_event(
        self,
        *,
        task_id: str,
        event_type: str,
        payload: Mapping[str, object],
    ) -> RuntimeEvent:
        """Append a durable coordinator fact without exposing Store internals."""
        self._store.load_task(task_id)
        now = self._clock()
        return self._store.append_event(
            task_id,
            self._event(event_type, now, task_id, payload),
        )

    def complete_task(
        self,
        *,
        task_id: str,
        evidence_refs: Iterable[str],
        summary: str,
    ) -> Task:
        """Commit final Task success only after every Stage has completed."""
        before = self._store.load_task(task_id)
        stages = self._store.list_stages(task_id)
        refs = tuple(str(item) for item in evidence_refs if str(item).strip())
        if before.status is not TaskStatus.PLANNING or before.current_stage_id is not None:
            raise ValueError("Task completion requires an idle PLANNING Task")
        if not stages or any(stage.status is not StageStatus.COMPLETED for stage in stages):
            raise ValueError("Task completion requires every planned Stage to be complete")
        if not refs:
            raise ValueError("Task completion requires final verification evidence")
        now = self._clock()
        after = before.transition_to(TaskStatus.COMPLETED, at=now)
        self._store.mutate_task(
            before_task=before,
            after_task=after,
            event=self._event(
                "TaskCompleted",
                now,
                task_id,
                {"summary": summary, "evidence_refs": list(refs)},
            ),
        )
        return after

    def fail_task(self, *, task_id: str, code: str, summary: str) -> Task:
        return self._terminalize_task(
            task_id=task_id,
            status=TaskStatus.FAILED,
            event_type="TaskFailed",
            code=code,
            summary=summary,
        )

    def mark_task_uncertain(self, *, task_id: str, code: str, summary: str) -> Task:
        return self._terminalize_task(
            task_id=task_id,
            status=TaskStatus.FAILED,
            event_type="TaskUncertain",
            code=code,
            summary=summary,
        )

    def _terminalize_task(
        self,
        *,
        task_id: str,
        status: TaskStatus,
        event_type: str,
        code: str,
        summary: str,
    ) -> Task:
        before = self._store.load_task(task_id)
        if before.terminal:
            return before
        now = self._clock()
        after = before.record_failure(
            self._failure_state(
                before,
                code=code,
                summary=summary,
                action_id=None,
                verdict="UNCERTAIN" if event_type == "TaskUncertain" else "FAIL",
                at=now,
            ),
            at=now,
        ).transition_to(status, at=now)
        self._store.mutate_task(
            before_task=before,
            after_task=after,
            event=self._event(
                event_type,
                now,
                task_id,
                {"code": code, "summary": summary},
            ),
        )
        return after

    def record_user_message(
        self,
        *,
        task_id: str,
        message_id: str,
        conversation_id: str,
        text: str,
    ) -> RuntimeEvent:
        """Persist a user message as a `UserMessageReceived` task event.

        The Kernel only records the message; it does not classify intent.
        Conversation ownership and terminal-Task guards are the Gateway's
        responsibility (contract §7).
        """
        self._store.load_task(task_id)
        now = self._clock()
        return self._store.append_event(
            task_id,
            self._event(
                "UserMessageReceived",
                now,
                task_id,
                {
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "text": text,
                },
                actor=EventActor.USER,
            ),
        )

    def capture_observation(
        self,
        *,
        task_id: str,
        device_id: str,
        observation_id: str | None = None,
        body_snapshot: BodySnapshotCorrelation | None = None,
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
                body_snapshot=body_snapshot,
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
                        "body_snapshot_id": (
                            observation.body_snapshot.snapshot_id
                            if observation.body_snapshot is not None
                            else None
                        ),
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

    def verified_facts(self, task_id: str) -> tuple[Fact, ...]:
        return self._store.list_facts(task_id, verified_only=True)

    def propose_action(
        self,
        *,
        task_id: str,
        stage_id: str,
        based_on_observation_id: str,
        action_type: ActionType,
        params: Mapping[str, object],
        expected_outcome: str,
        proposed_by_call_id: str,
        action_id: str | None = None,
    ) -> Action:
        task = self._store.load_task(task_id)
        stage = self._store.load_stage(task_id, stage_id)
        observation = self._store.load_observation(based_on_observation_id)
        if task.status is not TaskStatus.RUNNING or task.current_stage_id != stage_id:
            raise ValueError("Action requires the Task's active RUNNING Stage")
        if stage.status is not StageStatus.ACTIVE:
            raise ValueError("Action requires an ACTIVE Stage")
        if observation.task_id != task_id or task.last_observation_id != observation.id:
            raise ValueError("Action must be based on the Task's latest Observation")
        now = self._clock()
        action = Action.propose(
            action_id=action_id or self._id_factory(),
            task_id=task_id,
            stage_id=stage_id,
            based_on_observation_id=based_on_observation_id,
            action_type=action_type,
            params=params,
            expected_outcome=expected_outcome,
            proposed_by_call_id=proposed_by_call_id,
            proposed_at=now,
        )
        if action.type in {ActionType.INPUT_TEXT, ActionType.INPUT_TEXT_UNICODE}:
            text = action.params.get("text")
            if not isinstance(text, str):
                raise ValueError("typed text Action requires transient plaintext")
            self._transient_typed_text[action.id] = text
        try:
            self._store.create_action(
                action,
                self._event(
                "ActionProposed",
                now,
                task_id,
                {
                    "action_id": action.id,
                    "stage_id": action.stage_id,
                    "based_on_observation_id": action.based_on_observation_id,
                    "type": action.type.value,
                    "expected_outcome": action.expected_outcome,
                },
                ),
            )
        except Exception:
            self._transient_typed_text.pop(action.id, None)
            raise
        return action

    def load_action(self, task_id: str, action_id: str) -> Action:
        return self._store.load_action(task_id, action_id)

    def load_action_by_id(self, action_id: str) -> Action:
        """Resolve a globally unique Action for Companion retained results."""

        return self._store.load_action_by_id(action_id)

    def load_action_execution(self, action_id: str) -> ActionExecution:
        return self._store.load_action_execution(action_id)

    def load_verification(self, action_id: str) -> Verification:
        return self._store.load_verification(action_id)

    def list_actions(self, task_id: str) -> tuple[Action, ...]:
        return self._store.list_actions(task_id)

    def prepare_action_execution(self, *, task_id: str, action_id: str) -> Action:
        """Return a dispatchable Action only when its decision Observation is current.

        This isolated Kernel has no device executor. A future physical seam must call
        this check immediately before dispatch and then durably report the outcome.
        """
        task = self._store.load_task(task_id)
        action = self._store.load_action(task_id, action_id)
        if action.status is not ActionStatus.PROPOSED:
            raise ValueError("only a PROPOSED Action can be dispatched")
        if task.status is not TaskStatus.RUNNING or task.current_stage_id != action.stage_id:
            raise ValueError("Action no longer belongs to the active RUNNING Stage")
        if task.last_observation_id != action.based_on_observation_id:
            raise ValueError("Action decision is stale; a fresh decision is required")
        checkpoint = self._store.latest_checkpoint(task_id)
        if (
            checkpoint is not None
            and checkpoint.required_fresh_observation
            and checkpoint.unresolved_action_ref == action.id
        ):
            raise ValueError("unresolved Action cannot be replayed; observe and decide again")
        return action

    def record_action_execution(
        self,
        *,
        task_id: str,
        action_id: str | None,
        execution_id: str | None = None,
        accepted: bool,
        adapter_code: int | None,
        error: ExecutionError | None,
        started_at: str | None = None,
        finished_at: str | None = None,
        lease_ref: str | None = None,
        body_command_id: str | None = None,
        body_receipt_id: str | None = None,
    ) -> ActionExecution:
        if body_command_id is not None or body_receipt_id is not None:
            if action_id is None:
                raise ValueError("Body execution requires a Kernel action id")
            execution, _created = self._record_body_execution_once(
                task_id=task_id,
                action_id=action_id,
                execution_id=execution_id,
                device_id=self._store.load_task(task_id).device_id,
                accepted=accepted,
                adapter_code=adapter_code,
                error=error,
                started_at=started_at,
                finished_at=finished_at,
                lease_ref=lease_ref,
                body_command_id=body_command_id,
                body_receipt_id=body_receipt_id,
            )
            return execution
        # This direct recording API is also used by retained adapter replies.
        # It must not become a bypass around the same current-observation and
        # control/recovery fence required by execute_action.
        before_action = self.prepare_action_execution(task_id=task_id, action_id=action_id)
        before_task = self._store.load_task(task_id)
        now = self._clock()
        execution = ActionExecution(
            id=execution_id or self._id_factory(),
            action_id=action_id,
            device_id=before_task.device_id,
            lease_ref=lease_ref,
            accepted=accepted,
            adapter_code=adapter_code,
            error=error,
            started_at=started_at or now,
            finished_at=finished_at or now,
            body_command_id=body_command_id,
            body_receipt_id=body_receipt_id,
        )
        after_action = before_action.record_execution(execution)
        after_task = before_task
        if not accepted:
            after_task = before_task.record_failure(
                self._failure_state(
                    before_task,
                    code=error.code if error else "action_rejected",
                    summary=error.message if error else "Action transport rejected",
                    action_id=action_id,
                    verdict="FAIL",
                    at=execution.finished_at,
                ),
                at=execution.finished_at,
            )
        self._store.record_action_execution(
            before_task=before_task,
            after_task=after_task,
            before_action=before_action,
            after_action=after_action,
            execution=execution,
            event=self._event(
                "ActionExecuted",
                execution.finished_at,
                task_id,
                {
                    "action_id": action_id,
                    "execution_id": execution.id,
                    "accepted": accepted,
                    "adapter_code": adapter_code,
                    "error_code": error.code if error else None,
                    "body_command_id": body_command_id,
                    "body_receipt_id": body_receipt_id,
                },
                actor=EventActor.DEVICE,
            ),
        )
        return execution

    def record_body_execution_once(
        self,
        *,
        kernel_action_id: str,
        body_command_id: str,
        body_receipt_id: str,
        device_id: str,
        accepted: bool,
        adapter_code: int | None = None,
        error: ExecutionError | None = None,
        started_at: str,
        finished_at: str,
    ) -> tuple[ActionExecution, bool]:
        """Merge a synchronous, late, or retained Companion result once.

        This callback intentionally stops at ``ActionExecution``.  It does not
        capture an Observation, write a Verification, complete a Stage/Task,
        or touch Goal authority.  A caller must separately request the normal
        fresh Body snapshot path before any verification.
        """

        action = self._store.load_action_by_id(kernel_action_id)
        return self._record_body_execution_once(
            task_id=action.task_id,
            action_id=action.id,
            execution_id=None,
            device_id=device_id,
            accepted=accepted,
            adapter_code=adapter_code,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            lease_ref=None,
            body_command_id=body_command_id,
            body_receipt_id=body_receipt_id,
        )

    def _record_body_execution_once(
        self,
        *,
        task_id: str,
        action_id: str,
        execution_id: str | None,
        device_id: str,
        accepted: bool,
        adapter_code: int | None,
        error: ExecutionError | None,
        started_at: str | None,
        finished_at: str | None,
        lease_ref: str | None,
        body_command_id: str | None,
        body_receipt_id: str | None,
    ) -> tuple[ActionExecution, bool]:
        if body_command_id is None or body_receipt_id is None:
            raise ValueError("Body execution requires command and receipt identities")
        before_task = self._store.load_task(task_id)
        before_action = self._store.load_action(task_id, action_id)
        if before_task.device_id != device_id:
            raise StoreConflict("Body result device does not match the Kernel Task")

        # Fast idempotent read for ordinary retained-result delivery.  The
        # store repeats the same check under BEGIN IMMEDIATE for sync/late
        # races, so this read is not the concurrency fence.
        try:
            existing = self._store.load_action_execution(action_id)
        except RecordNotFound:
            existing = None
        if existing is not None:
            if (
                existing.body_command_id != body_command_id
                or existing.body_receipt_id != body_receipt_id
                or existing.device_id != device_id
            ):
                raise StoreConflict(
                    "Kernel Action already has a different Body command/receipt identity"
                )
            release_exact = getattr(
                self._store, "release_lease_for_body_execution", None
            )
            if callable(release_exact):
                release_exact(
                    device_id=existing.device_id,
                    task_id=before_task.id,
                    action_id=existing.action_id,
                )
            return existing, False

        # A first durable receipt is a physical-boundary mutation.  It must
        # pass the same fresh observation/control/recovery fence as the
        # synchronous execute path; only an already committed exact receipt
        # above may be reconciled after a pause/takeover/restart.
        before_action = self.prepare_action_execution(task_id=task_id, action_id=action_id)
        before_task = self._store.load_task(task_id)

        now = self._clock()
        execution = ActionExecution(
            id=execution_id or self._id_factory(),
            action_id=action_id,
            device_id=device_id,
            lease_ref=lease_ref,
            accepted=accepted,
            adapter_code=adapter_code,
            error=error,
            started_at=started_at or now,
            finished_at=finished_at or now,
            body_command_id=body_command_id,
            body_receipt_id=body_receipt_id,
        )
        after_action = before_action.record_execution(execution)
        after_task = before_task
        if not accepted and not before_task.terminal:
            after_task = before_task.record_failure(
                self._failure_state(
                    before_task,
                    code=error.code if error else "action_rejected",
                    summary=error.message if error else "Action transport rejected",
                    action_id=action_id,
                    verdict="FAIL",
                    at=execution.finished_at,
                ),
                at=execution.finished_at,
            )
        return self._store.record_body_execution_once(
            before_task=before_task,
            after_task=after_task,
            before_action=before_action,
            after_action=after_action,
            execution=execution,
            event=self._event(
                "ActionExecuted",
                execution.finished_at,
                task_id,
                {
                    "action_id": action_id,
                    "execution_id": execution.id,
                    "accepted": accepted,
                    "adapter_code": adapter_code,
                    "error_code": error.code if error else None,
                    "body_command_id": body_command_id,
                    "body_receipt_id": body_receipt_id,
                },
                actor=EventActor.DEVICE,
            ),
        )

    def capture_fresh_body_observation(
        self, *, task_id: str, kernel_action_id: str
    ) -> Observation:
        """Capture a normal Observation from a fresh post-receipt Body snapshot.

        The Device ``ACTION_RESULT`` payload is never accepted as an
        Observation.  This method always calls the composed
        ``KernelDeviceBodyBridge.capture_fresh_snapshot`` and persists only its
        strict opaque ``BodySnapshotCorrelation`` on the newly captured Kernel
        Observation.
        """

        task = self._store.load_task(task_id)
        action = self._store.load_action(task_id, kernel_action_id)
        execution = self._store.load_action_execution(action.id)
        if execution.body_command_id is None or execution.body_receipt_id is None:
            raise ValueError("ActionExecution is not backed by a Body command/receipt")
        capture = getattr(
            self._body_dispatcher, "capture_snapshot_for_observation", None
        )
        if not callable(capture):
            # Compatibility with older Body bridges is observation-only.  A
            # Body-backed verification still requires the durable verdict gate
            # below, so an UNKNOWN receipt cannot be promoted by a legacy
            # dispatcher that does not implement the R5 reconciliation seam.
            capture = getattr(self._body_dispatcher, "capture_fresh_snapshot", None)
        if not callable(capture):
            raise RuntimeError("fresh DeviceBody snapshot capability is not configured")
        fresh = capture(kernel_action_id=action.id)
        command = getattr(fresh, "command", None)
        receipt = getattr(fresh, "receipt", None)
        if (
            command is None
            or receipt is None
            or getattr(command, "id", None) != execution.body_command_id
            or getattr(command, "kernel_action_id", None) != action.id
            or getattr(receipt, "id", None) != execution.body_receipt_id
            or getattr(receipt, "kernel_action_id", None) != action.id
        ):
            raise StoreConflict(
                "fresh Body snapshot command/receipt does not match ActionExecution"
            )
        correlate = getattr(fresh, "as_kernel_correlation", None)
        if not callable(correlate):
            raise RuntimeError("fresh DeviceBody snapshot has no Kernel correlation")
        correlation = correlate()
        if (
            not isinstance(correlation, BodySnapshotCorrelation)
            or correlation.caused_by_body_command_id != execution.body_command_id
        ):
            raise StoreConflict("fresh Body snapshot correlation is invalid")
        return self.capture_observation(
            task_id=task.id,
            device_id=task.device_id,
            body_snapshot=correlation,
        )

    def verify_persisted_body_action_snapshot(
        self, *, task_id: str, action_id: str, after_observation_id: str
    ) -> object:
        """Return the body verifier result for one persisted after Observation.

        Unlike ``capture_fresh_body_observation`` this performs no physical
        action and creates no Observation.  It is the only public path for an
        operator to turn the opaque persisted Body correlation into a
        deterministic TAP/SWIPE/Unicode match decision.
        """

        task = self._store.load_task(task_id)
        action = self._store.load_action(task_id, action_id)
        execution = self._store.load_action_execution(action.id)
        before_observation = self._store.load_observation(action.based_on_observation_id)
        observation = self._store.load_observation(after_observation_id)
        if (
            observation.task_id != task_id
            or observation.device_id != task.device_id
            or task.last_observation_id != after_observation_id
            or observation.body_snapshot is None
        ):
            raise ValueError("Body verifier requires the Task's correlated after Observation")
        if execution.body_command_id is None or execution.body_receipt_id is None:
            raise ValueError("Body verifier requires a Body-backed ActionExecution")
        correlation = observation.body_snapshot
        if correlation.caused_by_body_command_id != execution.body_command_id:
            raise ValueError("Body verifier correlation does not belong to the ActionExecution")
        verify = getattr(self._body_dispatcher, "verify_persisted_snapshot", None)
        if not callable(verify):
            raise RuntimeError("persisted DeviceBody snapshot verifier is not configured")
        before_correlation = before_observation.body_snapshot
        if action.type is ActionType.SWIPE and (
            before_observation.task_id != task_id
            or before_observation.device_id != task.device_id
            or before_correlation is None
        ):
            raise ValueError("SWIPE verifier requires the Action's correlated before Observation")
        return verify(
            kernel_action_id=action.id,
            snapshot_id=correlation.snapshot_id,
            binding_id=correlation.binding_id,
            device_boot_id=correlation.device_boot_id,
            capture_request_id=correlation.capture_request_id,
            sequence=correlation.sequence,
            caused_by_body_command_id=correlation.caused_by_body_command_id,
            before_snapshot_id=(
                before_correlation.snapshot_id if before_correlation is not None else None
            ),
            before_binding_id=(
                before_correlation.binding_id if before_correlation is not None else None
            ),
            before_device_boot_id=(
                before_correlation.device_boot_id if before_correlation is not None else None
            ),
            before_capture_request_id=(
                before_correlation.capture_request_id
                if before_correlation is not None
                else None
            ),
            before_sequence=(
                before_correlation.sequence if before_correlation is not None else None
            ),
        )

    def execute_action(
        self,
        *,
        task_id: str,
        action_id: str,
    ) -> ActionExecution:
        """执行 Action 的完整栅栏检查 + Lease 获取 + 物理下发
        
        这是 Phase 5 的新方法，整合了：
        1. prepare_action_execution() 栅栏检查
        2. DeviceExecutionLease 获取和管理
        3. 真实 ADB 执行
        4. record_action_execution() 结果持久化
        """
        if self._action_executor is None and self._body_dispatcher is None:
            raise RuntimeError("action executor is not configured")
        
        # 1. 栅栏检查
        action = self.prepare_action_execution(task_id=task_id, action_id=action_id)
        task = self._store.load_task(task_id)
        
        # 2. 获取设备独占 Lease。进程重启可能留下同一 Task/Action 的
        # in-flight Lease；此时必须复用它，让 DeviceBody 以已持久化的
        # command_id 对账。只有这个精确 owner/action 组合可重入，其他
        # Task 或尚未关联 Action 的 Lease 仍由 acquire_lease fail closed。
        lease = self._store.get_lease_for_device(task.device_id)
        if not (
            lease is not None
            and lease.task_id == task_id
            and lease.action_id == action_id
        ):
            lease_id = self._id_factory()
            acquired_at = self._clock()
            import os
            lease = self._store.acquire_lease(
                device_id=task.device_id,
                task_id=task_id,
                holder_process_id=str(os.getpid()),
                ttl_seconds=60,
                lease_id=lease_id,
                acquired_at=acquired_at,
            )
        
        try:
            # 3. 更新 Lease 关联 Action（用于崩溃恢复）
            self._store.update_lease_action(lease.id, action_id)
            
            # 4. A DeviceBody dispatcher owns one command/receipt lifecycle.
            # It is intentionally selected ahead of the legacy executor, so
            # a configured R4 binding cannot bypass the canonical command
            # ledger.  It returns transport facts only; verification remains
            # below this method's caller.
            result: ActionExecutionResult
            if self._body_dispatcher is not None:
                result = self._body_dispatcher.dispatch(
                    action=action, task_id=task_id, device_id=task.device_id
                )
            elif action.type == ActionType.TAP:
                result = self._action_executor.execute_tap(
                    device_id=task.device_id,
                    x=action.params["x"],
                    y=action.params["y"],
                )
            elif action.type == ActionType.LONG_PRESS:
                result = self._action_executor.execute_long_press(
                    device_id=task.device_id,
                    x=action.params["x"],
                    y=action.params["y"],
                    duration_ms=action.params.get("duration_ms", 600),
                )
            elif action.type == ActionType.SWIPE:
                result = self._action_executor.execute_swipe(
                    device_id=task.device_id,
                    start_x=action.params["start_x"],
                    start_y=action.params["start_y"],
                    end_x=action.params["end_x"],
                    end_y=action.params["end_y"],
                    duration_ms=action.params.get("duration_ms", 300),
                )
            elif action.type == ActionType.INPUT_TEXT:
                text = self._transient_typed_text.get(action.id)
                if text is None:
                    raise RuntimeError("typed text plaintext is unavailable after restart; fresh replan required")
                result = self._action_executor.execute_input_text(
                    device_id=task.device_id,
                    text=text,
                )
            elif action.type == ActionType.INPUT_TEXT_UNICODE:
                text = self._transient_typed_text.get(action.id)
                if text is None:
                    raise RuntimeError("typed text plaintext is unavailable after restart; fresh replan required")
                result = self._action_executor.execute_input_text_unicode(
                    device_id=task.device_id,
                    text=text,
                    target_hint=action.params.get("target_hint"),
                )
            elif action.type == ActionType.BACK:
                result = self._action_executor.execute_back(
                    device_id=task.device_id,
                )
            elif action.type == ActionType.HOME:
                result = self._action_executor.execute_home(
                    device_id=task.device_id,
                )
            elif action.type == ActionType.RECENTS:
                result = self._action_executor.execute_recents(device_id=task.device_id)
            elif action.type == ActionType.OPEN_APP:
                result = self._action_executor.execute_open_app(
                    device_id=task.device_id,
                    package=action.params["package"],
                    component=action.params.get("component"),
                )
            elif action.type == ActionType.CAPTURE_SNAPSHOT:
                result = self._action_executor.execute_capture_snapshot(
                    device_id=task.device_id,
                )
            else:
                raise ValueError(f"unsupported action type: {action.type}")
            
            # 5. 清除 Lease 的 Action 关联
            self._store.update_lease_action(lease.id, None)
            
            # 6. 持久化执行结果
            execution = self.record_action_execution(
                task_id=task_id,
                action_id=action_id,
                accepted=result.accepted,
                adapter_code=result.adapter_code,
                error=result.error,
                started_at=result.started_at,
                finished_at=result.finished_at,
                lease_ref=lease.id,
                body_command_id=result.body_command_id,
                body_receipt_id=result.body_receipt_id,
            )
            
            return execution
        
        finally:
            # 7. 释放 Lease（无论成功或失败）
            self._store.release_lease(lease.id)

    def apply_control(
        self,
        *,
        task_id: str,
        command: ControlCommand | str,
        reason: str | None = None,
    ) -> ControlResult:
        """应用用户控制命令: pause / resume / cancel / takeover (契约 §9).

        语义:
        - 命令在返回前已持久化到 Task 事件流 (accepted 语义由 Gateway 负责);
        - ``resume`` 只恢复 Task 状态, 不重新获取 Lease —— Runtime 必须先
          Observe, 下一次 ``execute_action`` 按现有流程自行获取设备 Lease;
        - pause/cancel/takeover 若任务持有 active lease 则释放, 让出设备。
        """
        if isinstance(command, str):
            try:
                command = ControlCommand(command)
            except ValueError as exc:
                raise ControlError(f"unknown control command: {command!r}") from exc

        task = self._store.load_task(task_id)
        now = self._clock()

        if command is ControlCommand.PAUSE:
            if task.status not in {
                TaskStatus.CREATED,
                TaskStatus.PLANNING,
                TaskStatus.RUNNING,
            }:
                raise InvalidControlTransition(
                    f"Task in {task.status.value} cannot be paused"
                )
            after = task.transition_to(TaskStatus.PAUSED, at=now)
            event_type = "TaskPaused"
        elif command is ControlCommand.RESUME:
            if task.status is not TaskStatus.PAUSED:
                raise InvalidControlTransition(
                    f"Task in {task.status.value} cannot be resumed"
                )
            paused_event = next(
                (
                    item
                    for item in reversed(self.events(task_id))
                    if item.type in {"TaskPaused", "UserTakeover"}
                ),
                None,
            )
            previous_status = (
                str(paused_event.payload.get("previous_status"))
                if paused_event is not None
                else TaskStatus.RUNNING.value
            )
            resume_status = (
                TaskStatus(previous_status)
                if previous_status
                in {
                    TaskStatus.CREATED.value,
                    TaskStatus.PLANNING.value,
                    TaskStatus.RUNNING.value,
                }
                else TaskStatus.RUNNING
            )
            # A pre-control observation/proposed Action cannot be used after
            # the user has paused or taken over.  Clearing the durable latest
            # pointer makes both ``prepare_action_execution`` and a later
            # proposal require a newly captured observation before dispatch.
            after = replace(
                task.transition_to(resume_status, at=now),
                last_observation_id=None,
                updated_at=now,
            )
            event_type = "TaskResumed"
        elif command is ControlCommand.CANCEL:
            if task.terminal:
                raise InvalidControlTransition(
                    f"Task in {task.status.value} is already terminal"
                )
            after = task.transition_to(TaskStatus.CANCELLED, at=now)
            event_type = "TaskCancelled"
        elif command is ControlCommand.TAKEOVER:
            if task.status not in {
                TaskStatus.CREATED,
                TaskStatus.PLANNING,
                TaskStatus.RUNNING,
            }:
                raise InvalidControlTransition(
                    f"Task in {task.status.value} cannot be taken over"
                )
            after = task.transition_to(TaskStatus.PAUSED, at=now)
            event_type = "UserTakeover"
        else:
            raise ControlError(f"unknown control command: {command!r}")

        persisted = self._store.mutate_task(
            before_task=task,
            after_task=after,
            event=self._event(
                event_type,
                now,
                task_id,
                {"previous_status": task.status.value, "reason": reason},
                actor=EventActor.GATEWAY,
            ),
        )

        # 控制命令停止设备执行: 释放 Task 当前持有的 Lease (如有)。
        # release_lease 是幂等删除, 与 execute_action 的 finally 释放无冲突。
        released_lease_id: str | None = None
        if command is not ControlCommand.RESUME:
            lease = self._store.get_lease_for_task(task_id)
            if lease is not None:
                self._store.release_lease(lease.id)
                released_lease_id = lease.id

        return ControlResult(
            task=after, event=persisted, released_lease_id=released_lease_id
        )

    def verify_action(
        self,
        *,
        task_id: str,
        action_id: str,
        before_observation_id: str,
        after_observation_id: str,
        verdict: VerificationVerdict,
        reason: str,
        evidence_refs: Iterable[str],
        method: VerificationMethod,
        verification_call_id: str | None = None,
        verified_facts: Iterable[Fact] = (),
        complete_stage: bool = False,
        progress_summary: str | None = None,
        checkpoint_id: str | None = None,
        verification_id: str | None = None,
    ) -> tuple[Verification, Checkpoint | None]:
        before_task = self._store.load_task(task_id)
        before_action = self._store.load_action(task_id, action_id)
        execution = self._store.load_action_execution(action_id)
        before_stage = self._store.load_stage(task_id, before_action.stage_id)
        before_observation = self._store.load_observation(before_observation_id)
        after_observation = self._store.load_observation(after_observation_id)
        if before_action.status is not ActionStatus.EXECUTED:
            raise ValueError("only an accepted Action can be verified")
        if before_action.based_on_observation_id != before_observation_id:
            raise ValueError("Verification before Observation must match the Action decision")
        if (
            before_observation.task_id != task_id
            or after_observation.task_id != task_id
            or before_task.last_observation_id != after_observation_id
        ):
            raise ValueError("Verification requires the Task's fresh current Observation")
        execution_finished = _as_utc(execution.finished_at)
        after_times = (
            after_observation.capture_started_at,
            after_observation.capture_completed_at,
            after_observation.screenshot.captured_at,
            after_observation.ui_tree.captured_at,
        )
        if any(
            value is not None and _as_utc(value) <= execution_finished
            for value in after_times
        ):
            raise ValueError(
                "Verification after Observation channels must be strictly captured after ActionExecution"
            )
        if execution.body_command_id is not None:
            correlation = after_observation.body_snapshot
            if (
                correlation is None
                or correlation.caused_by_body_command_id != execution.body_command_id
            ):
                raise ValueError(
                    "DeviceBody Action verification requires its fresh correlated Snapshot Observation"
                )
            validate_verdict = getattr(
                self._body_dispatcher, "validate_verification_verdict", None
            )
            if not callable(validate_verdict):
                raise RuntimeError(
                    "DeviceBody verification requires a durable receipt verdict gate"
                )
            validate_verdict(
                kernel_action_id=action_id,
                verdict=verdict.value,
            )
        now = self._clock()
        verification = Verification.create(
            verification_id=verification_id or self._id_factory(),
            task_id=task_id,
            stage_id=before_action.stage_id,
            action_id=action_id,
            before_observation_id=before_observation_id,
            after_observation_id=after_observation_id,
            verdict=verdict,
            reason=reason,
            evidence_refs=evidence_refs,
            method=method,
            verification_call_id=verification_call_id,
            created_at=now,
        )
        after_action = before_action.record_verdict(verdict.value, at=now)
        facts = tuple(verified_facts)
        if verdict is not VerificationVerdict.SUCCESS:
            if facts or complete_stage or checkpoint_id is not None:
                raise ValueError("only SUCCESS Verification can commit Facts or Stage progress")
            after_task = before_task.record_failure(
                self._failure_state(
                    before_task,
                    code=(
                        "verification_uncertain"
                        if verdict is VerificationVerdict.UNCERTAIN
                        else "verification_failed"
                    ),
                    summary=reason,
                    action_id=action_id,
                    verdict=verdict.value,
                    at=now,
                ),
                at=now,
            )
            self._store.record_verification(
                before_task=before_task,
                after_task=after_task,
                before_action=before_action,
                after_action=after_action,
                verification=verification,
                event=self._event(
                    "ActionVerified",
                    now,
                    task_id,
                    {
                        "action_id": action_id,
                        "verification_id": verification.id,
                        "verdict": verdict.value,
                        "reason": reason,
                    },
                ),
            )
            return verification, None

        if any(fact.task_id != task_id or fact.status is not FactStatus.VERIFIED for fact in facts):
            raise ValueError("SUCCESS can commit only verified Facts for its Task")
        after_stage: Stage | None = None
        after_task = before_task.record_progress(at=now)
        if complete_stage:
            after_stage = before_stage.complete(
                at=now,
                evidence_refs=verification.evidence_refs,
                progress_summary=progress_summary,
            )
            after_task = after_task.complete_current_stage(before_stage.id, at=now)
            after_task = after_task.record_progress(at=now)

        checkpoint: CheckpointDraft | None = None
        if complete_stage or checkpoint_id is not None:
            checkpoint = self._build_checkpoint_draft(
                task=after_task,
                stages=tuple(
                    after_stage if stage.id == before_stage.id and after_stage else stage
                    for stage in self._store.list_stages(task_id)
                ),
                verified_facts=(
                    self._store.list_facts(task_id, verified_only=True) + facts
                ),
                reason="stage_completed" if complete_stage else "verified_progress",
                checkpoint_id=checkpoint_id or self._id_factory(),
                unresolved_action_ref=None,
            )
            after_task = after_task.record_checkpoint(checkpoint.id, at=now)

        events: list[RuntimeEventDraft] = [
            self._event(
                "ActionVerified",
                now,
                task_id,
                {
                    "action_id": action_id,
                    "verification_id": verification.id,
                    "verdict": verdict.value,
                    "reason": reason,
                },
            )
        ]
        events.extend(
            self._event(
                "FactAdded",
                now,
                task_id,
                {
                    "fact_id": fact.id,
                    "key": fact.key,
                    "status": fact.status.value,
                    "source_refs": list(fact.source_refs),
                },
            )
            for fact in facts
        )
        if after_stage is not None:
            events.append(
                self._event(
                    "StageCompleted",
                    now,
                    task_id,
                    {
                        "stage_id": after_stage.id,
                        "status": after_stage.status.value,
                        "evidence_refs": list(after_stage.evidence_refs),
                    },
                )
            )
        if checkpoint is not None:
            events.append(
                self._event(
                    "CheckpointCreated",
                    now,
                    task_id,
                    {
                        "checkpoint_id": checkpoint.id,
                        "reason": checkpoint.resume_reason,
                        "required_fresh_observation": checkpoint.required_fresh_observation,
                    },
                )
            )
        _, materialized_checkpoint = self._store.commit_successful_verification(
            before_task=before_task,
            after_task=after_task,
            before_stage=before_stage,
            after_stage=after_stage,
            before_action=before_action,
            after_action=after_action,
            verification=verification,
            facts=facts,
            checkpoint=checkpoint,
            events=tuple(events),
        )
        return verification, materialized_checkpoint

    def create_checkpoint(
        self,
        *,
        task_id: str,
        reason: str,
        checkpoint_id: str | None = None,
        unresolved_action_ref: str | None = None,
    ) -> Checkpoint:
        before_task = self._store.load_task(task_id)
        draft = self._build_checkpoint_draft(
            task=before_task,
            stages=self._store.list_stages(task_id),
            verified_facts=self._store.list_facts(task_id, verified_only=True),
            reason=reason,
            checkpoint_id=checkpoint_id or self._id_factory(),
            unresolved_action_ref=unresolved_action_ref,
        )
        after_task = before_task.record_checkpoint(draft.id, at=draft.created_at)
        checkpoint, _ = self._store.create_checkpoint(
            before_task=before_task,
            after_task=after_task,
            checkpoint=draft,
            event=self._event(
                "CheckpointCreated",
                draft.created_at,
                task_id,
                {
                    "checkpoint_id": draft.id,
                    "reason": draft.resume_reason,
                    "required_fresh_observation": draft.required_fresh_observation,
                    "unresolved_action_ref": draft.unresolved_action_ref,
                },
            ),
        )
        return checkpoint

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        return self._store.latest_checkpoint(task_id)

    def active_leased_device_ids(self) -> frozenset[str]:
        """当前未过期 Lease 持有的设备 id 集合（Phase 7 切流接线）。

        供 Gateway 组合传给 ``AdbDeviceRegistry(active_device_ids=...)``：
        Kernel 以未过期 Lease 独占中的设备在 Gateway 设备目录中显示为
        ``IN_USE``，对已租设备的 ``POST /tasks`` 以 ``DEVICE_NOT_AVAILABLE``
        拒绝——补齐切流到 KERNEL_ACTIVE 后设备独占的最后一公里。

        过期判定与 Lease 机制本身一致（``DeviceExecutionLease.is_expired``，
        ``expires_at <= now`` 字符串比较），``now`` 取 Kernel 自身时钟。
        存储不可用时退化为空集合：叠加层故障不得拖垮设备目录。
        """
        try:
            leases = self._store.list_leases()
        except Exception:
            return frozenset()
        now = self._clock()
        return frozenset(
            lease.device_id for lease in leases if not lease.is_expired(now)
        )

    def release_exact_action_lease(self, *, task_id: str, action_id: str) -> bool:
        """Release only the lease durably owned by this exact Task/Action.

        This narrow takeover seam never searches by a device hint and cannot
        release another Task's writer lease.
        """

        lease = self._store.get_lease_for_task(task_id)
        if lease is None:
            return False
        if lease.task_id != task_id or lease.action_id != action_id:
            raise StoreConflict("Task action does not own the current device lease")
        self._store.release_lease(lease.id)
        return True

    def _build_checkpoint_draft(
        self,
        *,
        task: Task,
        stages: tuple[Stage, ...],
        verified_facts: tuple[Fact, ...],
        reason: str,
        checkpoint_id: str,
        unresolved_action_ref: str | None,
    ) -> CheckpointDraft:
        observation = (
            self._store.load_observation(task.last_observation_id)
            if task.last_observation_id is not None
            else None
        )
        completed = tuple(
            {
                "id": stage.id,
                "ordinal": stage.ordinal,
                "objective": stage.objective,
                "progress_summary": stage.progress_summary,
                "completed_at": stage.completed_at,
                "evidence_refs": list(stage.evidence_refs),
            }
            for stage in stages
            if stage.status is StageStatus.COMPLETED
        )
        device_summary: dict[str, object] = {
            "device_id": task.device_id,
            "last_observation_id": task.last_observation_id,
        }
        if observation is not None:
            device_summary.update(
                {
                    "foreground_app": observation.device_state.foreground_app,
                    "connection_state": observation.device_state.connection_state.value,
                    "captured_at": observation.captured_at,
                }
            )
        progress = (
            {"at": task.last_meaningful_progress_at}
            if task.last_meaningful_progress_at is not None
            else None
        )
        return CheckpointDraft(
            id=checkpoint_id,
            task_id=task.id,
            goal=task.goal,
            status_at_checkpoint=task.status,
            current_stage_id=task.current_stage_id,
            completed_stage_summaries=completed,
            verified_facts=verified_facts,
            device_summary=device_summary,
            last_meaningful_progress=progress,
            failure_summary=asdict(task.failure_state) if task.failure_state else None,
            resume_reason=reason,
            required_fresh_observation=unresolved_action_ref is not None,
            unresolved_action_ref=unresolved_action_ref,
            created_at=self._clock(),
        )

    @staticmethod
    def _failure_state(
        task: Task,
        *,
        code: str,
        summary: str,
        action_id: str,
        verdict: str,
        at: str,
    ) -> FailureState:
        previous = task.failure_state
        return FailureState(
            code=code,
            summary=summary,
            retry_count=(previous.retry_count if previous else 0) + 1,
            no_progress_count=(previous.no_progress_count if previous else 0) + 1,
            last_failed_action_id=action_id,
            last_verdict=verdict,
            recoverable=True,
            updated_at=at,
        )

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
        *,
        actor: EventActor = EventActor.RUNTIME,
    ) -> RuntimeEventDraft:
        return RuntimeEventDraft(
            id=self._id_factory(),
            type=event_type,
            actor=actor,
            payload=payload,
            correlation_id=task_id,
            created_at=created_at,
        )
    
    def shutdown(self) -> None:
        """关闭 Kernel 和相关资源"""
        if self._lease_manager:
            self._lease_manager.stop_background_cleanup()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_utc(value: str) -> datetime:
    """Parse ISO facts for causal action/observation ordering."""

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
