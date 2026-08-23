"""Reversible RuntimeKernel coordinator for U6 canary and U7 active modes.

The coordinator reuses the existing sequential Android role model but owns a
distinct RuntimeKernel task lifecycle.  U6 canary events remain readable while
U7 active mode records a production binding and acceptance event.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .device_lease import DeviceExecutionLease, DeviceLeaseHandle
from .execution import AndroidScreenshot
from .mobile_agent import (
    ActionAttempt,
    ActionDecision,
    DecisionContext,
    ExperienceHint,
    InputRevision,
    MobileTaskState,
    Observation as RoleObservation,
    PhysicalIntent,
    PlanContext,
    Reflection,
    ReflectionContext,
    RoleModel,
    Subgoal,
    TaskEvent,
    TaskPlan,
    TransportReceipt,
    Verification as RoleVerification,
    VerificationContext,
)
from .mobile_task_adapter import LocalMobileEvidenceStore
from .runtime_adapters.artifacts import FilesystemArtifactStore
from .runtime_kernel import (
    ActionStatus,
    ActionType,
    ControlCommand,
    RuntimeKernel,
    StageStatus,
    TaskSource,
    TaskStatus,
    VerificationMethod,
    VerificationVerdict,
)


class KernelCanaryError(RuntimeError):
    code = "kernel_canary_error"


class KernelCanaryCoordinator:
    """One serial plan/observe/act/verify worker over RuntimeKernel facts."""

    def __init__(
        self,
        *,
        kernel: RuntimeKernel,
        model: RoleModel,
        artifacts: FilesystemArtifactStore,
        evidence: LocalMobileEvidenceStore,
        device_lease: DeviceExecutionLease,
        settle_seconds: float = 1.0,
        max_attempts: int = 128,
        max_reflections: int = 16,
        experience: Any | None = None,
        binding_kind: str = "runtime_kernel_canary",
    ) -> None:
        self.kernel = kernel
        self.model = model
        self.artifacts = artifacts
        self.evidence = evidence
        self.device_lease = device_lease
        self.settle_seconds = settle_seconds
        self.max_attempts = max_attempts
        self.max_reflections = max_reflections
        self.experience = experience
        self.binding_kind = binding_kind
        self.acceptance_event_type = (
            "KernelTaskAccepted"
            if binding_kind == "runtime_kernel"
            else "KernelCanaryAccepted"
        )
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kernel-canary")
        self._scheduled: set[str] = set()
        self._schedule_lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._closed = False

    def start(
        self,
        goal: str,
        client_request_id: str,
        *,
        target_id: str | None,
        goal_id: str | None = None,
        goal_spec_revision: int | None = None,
        frozen_criteria_ids: tuple[str, ...] = (),
        **_: Any,
    ) -> MobileTaskState:
        device_id = _canonical_device_id(target_id)
        for existing in self.kernel.list_tasks():
            if existing.source.initial_message_id != client_request_id:
                continue
            if existing.goal != goal or existing.device_id != device_id:
                raise KernelCanaryError("kernel canary idempotency conflict")
            self.submit(existing.id)
            return self.inspect(existing.id)
        task = self.kernel.create_task(
            goal=goal,
            source=TaskSource(
                client_id="goal-v2-kernel-canary",
                conversation_id=f"goal:{goal_id or client_request_id}",
                initial_message_id=client_request_id,
            ),
            device_id=device_id,
        )
        self.kernel.record_worker_event(
            task_id=task.id,
            event_type=self.acceptance_event_type,
            payload={"goal_id": goal_id, "binding_kind": self.binding_kind},
        )
        if (
            self.experience is not None
            and goal_id is not None
            and goal_spec_revision is not None
            and frozen_criteria_ids
        ):
            try:
                self.experience.begin_mobile_episode_for_task(
                    goal_run_id=goal_id,
                    source_task_id=task.id,
                    goal_spec_revision=goal_spec_revision,
                    frozen_criteria_ids=frozen_criteria_ids,
                    skill_scope_id=None,
                    target_id=device_id,
                )
            except Exception:
                pass
        self.submit(task.id)
        return self.inspect(task.id)

    def submit(self, task_id: str) -> None:
        with self._schedule_lock:
            if self._closed:
                raise KernelCanaryError("kernel canary coordinator is closed")
            if task_id in self._scheduled:
                return
            if not any(
                event.type in {"KernelCanaryAccepted", "KernelTaskAccepted"}
                for event in self.kernel.events(task_id)
            ):
                self.kernel.record_worker_event(
                    task_id=task_id,
                    event_type=self.acceptance_event_type,
                    payload={"goal_id": None, "binding_kind": self.binding_kind},
                )
            self._scheduled.add(task_id)
            self._pool.submit(self._run_safely, task_id)

    def recover(self) -> None:
        """Restart from durable facts; accepted unverified actions are never replayed."""
        for task in self.kernel.list_tasks():
            if task.terminal:
                continue
            if not any(
                event.type in {"KernelCanaryAccepted", "KernelTaskAccepted"}
                for event in self.kernel.events(task.id)
            ):
                continue
            unresolved = [
                action
                for action in self.kernel.list_actions(task.id)
                if action.status is ActionStatus.EXECUTED
            ]
            if unresolved:
                self.kernel.mark_task_uncertain(
                    task_id=task.id,
                    code="restart_unverified_action",
                    summary="进程重启时发现已下发但未完成验证的动作；未重放。",
                )
            elif task.status is not TaskStatus.PAUSED:
                self.submit(task.id)

    def send(self, task_id: str, content: str, client_request_id: str) -> MobileTaskState:
        task = self.kernel.load_task(task_id)
        self.kernel.record_user_message(
            task_id=task_id,
            message_id=client_request_id,
            conversation_id=task.source.conversation_id,
            text=content,
        )
        return self.inspect(task_id)

    def stop(self, task_id: str, client_request_id: str) -> MobileTaskState:
        del client_request_id
        return self.control(task_id, "stop")

    def control(self, task_id: str, action: str) -> MobileTaskState:
        command = {
            "stop": ControlCommand.CANCEL,
            "pause": ControlCommand.PAUSE,
            "resume": ControlCommand.RESUME,
            "takeover": ControlCommand.TAKEOVER,
        }.get(action)
        if command is None:
            raise KernelCanaryError(f"unsupported kernel canary control: {action}")
        with self._dispatch_lock:
            self.kernel.apply_control(
                task_id=task_id,
                command=command,
                reason="owner control through GoalRun",
            )
        if command is ControlCommand.RESUME:
            self.submit(task_id)
        return self.inspect(task_id)

    def confirm_goal_completion(
        self, task_id: str, goal_id: str, completion_revision: int
    ) -> None:
        state = self.inspect(task_id)
        refs = tuple(
            attempt.after.evidence_id
            for attempt in state.attempts
            if attempt.verification is not None
            and attempt.verification.satisfied
            and attempt.after is not None
        )
        if self.kernel.load_task(task_id).status is not TaskStatus.COMPLETED:
            self.kernel.complete_task(
                task_id=task_id,
                evidence_refs=refs,
                summary=f"GoalRun {goal_id} completion revision {completion_revision} verified",
            )
        if self.experience is not None:
            try:
                self.experience.sync_mobile_task(state)
                self.experience.confirm_goal_completion(
                    task_id,
                    goal_id=goal_id,
                    completion_revision=completion_revision,
                )
            except Exception:
                # Learning remains additive; it cannot rewrite verified Task truth.
                pass

    def inspect(self, task_id: str) -> MobileTaskState:
        task = self.kernel.load_task(task_id)
        events = self.kernel.events(task_id)
        links = {
            str(event.payload.get("kernel_observation_id")): str(
                event.payload.get("evidence_id")
            )
            for event in events
            if event.type == "KernelObservationLinked"
        }
        role_verdicts = {
            str(event.payload.get("action_id")): event.payload
            for event in events
            if event.type == "KernelRoleVerdict"
        }
        stages = self.kernel.list_stages(task_id)
        stages_by_id = {stage.id: stage for stage in stages}
        stage_index = {stage.id: stage.ordinal - 1 for stage in stages}
        subgoals = tuple(
            Subgoal(
                index=stage.ordinal - 1,
                description=stage.objective,
                status=(
                    "completed"
                    if stage.status is StageStatus.COMPLETED
                    else "active"
                    if stage.status is StageStatus.ACTIVE
                    else "pending"
                ),
            )
            for stage in stages
        )
        inputs = _inputs(task.goal, task.created_at, events)
        attempts: list[ActionAttempt] = []
        for sequence, action in enumerate(self.kernel.list_actions(task_id), start=1):
            before = _role_observation(action.based_on_observation_id, links)
            execution = None
            verification = None
            try:
                execution = self.kernel.load_action_execution(action.id)
            except Exception:
                pass
            try:
                verification = self.kernel.load_verification(action.id)
            except Exception:
                pass
            after = (
                _role_observation(verification.after_observation_id, links)
                if verification is not None
                else None
            )
            role_verdict = role_verdicts.get(action.id, {})
            finish_check = (
                action.type is ActionType.SCREENSHOT
                and action.expected_outcome.startswith("finish-check:")
            )
            decision = _decision_from_action(action)
            attempts.append(
                ActionAttempt(
                    attempt_id=action.id,
                    sequence=sequence,
                    plan_revision=1,
                    subgoal_index=stage_index.get(action.stage_id, 0),
                    input_revision=len(inputs),
                    decision=decision,
                    before=before,
                    transport=(
                        TransportReceipt(
                            (
                                "not_sent"
                                if finish_check
                                else "accepted"
                                if execution.accepted
                                else "rejected"
                            ),
                            receipt_id=execution.id,
                            detail=(execution.error.code if execution.error else "kernel dispatch"),
                        )
                        if execution is not None
                        else None
                    ),
                    after=after,
                    verification=(
                        RoleVerification(
                            satisfied=bool(
                                role_verdict.get(
                                    "satisfied",
                                    verification.verdict is VerificationVerdict.SUCCESS
                                    and stages_by_id[action.stage_id].status
                                    is StageStatus.COMPLETED,
                                )
                            ),
                            progress=bool(
                                role_verdict.get(
                                    "progress",
                                    verification.verdict is VerificationVerdict.SUCCESS,
                                )
                            ),
                            uncertain=verification.verdict is VerificationVerdict.UNCERTAIN,
                            evidence=verification.reason,
                        )
                        if verification is not None
                        else None
                    ),
                    created_at=action.proposed_at,
                    finalized_at=(verification.created_at if verification else None),
                )
            )
        reflections = tuple(
            Reflection(
                sequence=index,
                previous_strategy=str(event.payload.get("previous_strategy") or "visual-first"),
                strategy=str(event.payload.get("strategy") or "visual-first"),
                reason=str(event.payload.get("reason") or "bounded recovery"),
                consecutive_no_progress=int(event.payload.get("no_progress_count") or 0),
                created_at=event.created_at,
            )
            for index, event in enumerate(
                (item for item in events if item.type == "KernelStrategyChanged"), start=1
            )
        )
        status = _project_status(task, stages)
        active = next(
            (stage.ordinal - 1 for stage in stages if stage.status is StageStatus.ACTIVE),
            len(stages) if stages else 0,
        )
        return MobileTaskState(
            task_id=task.id,
            goal=task.goal,
            target_id=task.device_id,
            skill_id=None,
            skill_scope_id=None,
            status=status,
            input_revision=len(inputs),
            plan=TaskPlan(1, subgoals) if subgoals else None,
            active_subgoal_index=active,
            strategy=(reflections[-1].strategy if reflections else "visual-first"),
            no_progress_count=task.failure_state.no_progress_count if task.failure_state else 0,
            reflection_count=len(reflections),
            attempt_count=len(attempts),
            cancel_requested=task.status is TaskStatus.CANCELLED,
            verification_satisfied=bool(stages) and all(
                stage.status is StageStatus.COMPLETED for stage in stages
            ),
            detail=task.failure_state.summary if task.failure_state else None,
            error_code=task.failure_state.code if task.failure_state else None,
            skill_memory_version=0,
            inputs=inputs,
            attempts=tuple(attempts),
            reflections=reflections,
            events=tuple(
                TaskEvent(event.sequence, event.type, event.payload, event.created_at)
                for event in events
            ),
            created_at=task.created_at,
            updated_at=task.updated_at,
            finished_at=task.terminal_at,
        )

    def shutdown(self) -> None:
        with self._schedule_lock:
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _run_safely(self, task_id: str) -> None:
        try:
            self._run(task_id)
        except Exception as error:
            try:
                task = self.kernel.load_task(task_id)
                if not task.terminal and task.status is not TaskStatus.PAUSED:
                    self.kernel.fail_task(
                        task_id=task_id,
                        code=str(getattr(error, "code", "kernel_canary_worker_failed")),
                        summary="Kernel canary worker failed before verified completion.",
                    )
            except Exception:
                pass
        finally:
            with self._schedule_lock:
                self._scheduled.discard(task_id)

    def _run(self, task_id: str) -> None:
        task = self.kernel.load_task(task_id)
        if task.terminal or task.status is TaskStatus.PAUSED:
            return
        lease: DeviceLeaseHandle | None = self.device_lease.acquire(
            task.device_id.removeprefix("adb:")
        )
        if lease is None:
            self.kernel.fail_task(
                task_id=task_id,
                code="target_busy",
                summary="目标设备正由另一个运行时持有；canary 未抢占设备。",
            )
            return
        try:
            observation = self._observe(task_id)
            stages = self.kernel.list_stages(task_id)
            if not stages:
                plan = self.model.plan(
                    PlanContext(
                        task_id=task.id,
                        goal=task.goal,
                        target_id=task.device_id,
                        input_revision=1,
                        owner_inputs=_inputs(task.goal, task.created_at, self.kernel.events(task_id)),
                        observation=observation,
                        skill_memory=None,
                    )
                )
                for objective in plan.subgoals:
                    self.kernel.create_stage(
                        task_id=task_id,
                        objective=objective,
                        completion_criteria=(objective,),
                    )
                stages = self.kernel.list_stages(task_id)
            attempt_count = len(self.kernel.list_actions(task_id))
            no_progress = 0
            strategy = "visual-first"
            reflection_count = 0
            while attempt_count < self.max_attempts:
                task = self.kernel.load_task(task_id)
                if task.terminal or task.status is TaskStatus.PAUSED:
                    return
                stage = self.kernel.current_stage(task_id)
                if stage is None:
                    pending = next(
                        (item for item in self.kernel.list_stages(task_id)
                         if item.status is StageStatus.PENDING),
                        None,
                    )
                    if pending is None:
                        self.kernel.record_worker_event(
                            task_id=task_id,
                            event_type="KernelCandidateComplete",
                            payload={"evidence_gate": "goal_completion_verifier"},
                        )
                        return
                    stage = self.kernel.start_stage(task_id=task_id, stage_id=pending.id)
                    observation = self._observe(task_id)
                state = self.inspect(task_id)
                subgoal = state.plan.subgoals[stage.ordinal - 1]
                experience_packet = None
                experience_hints: tuple[ExperienceHint, ...] = ()
                if self.experience is not None:
                    try:
                        experience_packet = self.experience.retrieve(
                            source_task_id=task_id,
                            objective=subgoal.description,
                            observation=observation,
                        )
                        experience_hints = tuple(
                            ExperienceHint(
                                candidate_id=str(item.candidate_id),
                                kind=item.kind,
                                semantic_action=str(item.semantic_action),
                                expected_next_scene=item.expected_next_scene,
                                recovery_action=item.recovery_action,
                                confidence=float(item.confidence),
                                provenance_transition_ids=tuple(
                                    item.provenance_transition_ids
                                ),
                            )
                            for item in getattr(experience_packet, "items", ())
                        )
                        self.kernel.record_worker_event(
                            task_id=task_id,
                            event_type="KernelExperienceRetrieved",
                            payload={
                                "retrieval_id": getattr(
                                    experience_packet, "retrieval_id", None
                                ),
                                "candidate_ids": [
                                    item.candidate_id for item in experience_hints
                                ],
                                "stage_id": stage.id,
                            },
                        )
                    except Exception:
                        experience_packet = None
                        experience_hints = ()
                decision = self.model.decide(
                    DecisionContext(
                        task_id=task_id,
                        goal=task.goal,
                        target_id=task.device_id,
                        plan_revision=1,
                        subgoal=subgoal,
                        input_revision=state.input_revision,
                        owner_inputs=state.inputs,
                        observation=observation,
                        strategy=strategy,
                        consecutive_no_progress=no_progress,
                        recent_attempts=state.attempts[-8:],
                        skill_memory=None,
                        experience_hints=experience_hints,
                    )
                )
                if decision.kind == "terminate":
                    self.kernel.fail_task(
                        task_id=task_id,
                        code="role_terminated",
                        summary=decision.reason or "Role model terminated the canary.",
                    )
                    return
                is_finish = decision.kind == "finish"
                intent = decision.intent or PhysicalIntent("screenshot")
                action_type, params = _kernel_action(intent)
                with self._dispatch_lock:
                    current = self.kernel.load_task(task_id)
                    if current.status is not TaskStatus.RUNNING:
                        return
                    action = self.kernel.propose_action(
                        task_id=task_id,
                        stage_id=stage.id,
                        based_on_observation_id=current.last_observation_id or "",
                        action_type=action_type,
                        params=params,
                        expected_outcome=("finish-check: " if is_finish else "") + stage.objective,
                        proposed_by_call_id=f"kernel-canary:{uuid4().hex}",
                    )
                    if action_type is ActionType.SCREENSHOT:
                        execution = self.kernel.record_action_execution(
                            task_id=task_id,
                            action_id=action.id,
                            accepted=True,
                            adapter_code=0,
                            error=None,
                        )
                    elif action_type is ActionType.WAIT:
                        time.sleep(float(params["seconds"]))
                        execution = self.kernel.record_action_execution(
                            task_id=task_id,
                            action_id=action.id,
                            accepted=True,
                            adapter_code=0,
                            error=None,
                        )
                    else:
                        execution = self.kernel.execute_action(
                            task_id=task_id, action_id=action.id
                        )
                attempt_count += 1
                if not execution.accepted:
                    code = execution.error.code if execution.error else "action_rejected"
                    if code.endswith("timeout"):
                        self.kernel.mark_task_uncertain(
                            task_id=task_id,
                            code=code,
                            summary="动作下发结果不确定；未自动重放。",
                        )
                        return
                    no_progress += 1
                    observation = self._observe(task_id)
                    continue
                if self.settle_seconds > 0 and action_type not in {
                    ActionType.SCREENSHOT,
                    ActionType.WAIT,
                }:
                    time.sleep(self.settle_seconds)
                after = self._observe(task_id)
                receipt = TransportReceipt(
                    "not_sent" if is_finish else "accepted",
                    receipt_id=execution.id,
                    detail="fresh verification follows",
                )
                verdict = self.model.verify(
                    VerificationContext(
                        task_id=task_id,
                        goal=task.goal,
                        subgoal=subgoal,
                        decision=decision,
                        before=observation,
                        transport=receipt,
                        after=after,
                        input_revision=state.input_revision,
                        owner_inputs=state.inputs,
                        plan_revision=1,
                        recent_attempts=state.attempts[-8:],
                    )
                )
                if verdict.uncertain:
                    self.kernel.verify_action(
                        task_id=task_id,
                        action_id=action.id,
                        before_observation_id=self.kernel.load_action(task_id, action.id).based_on_observation_id,
                        after_observation_id=self.kernel.load_task(task_id).last_observation_id or "",
                        verdict=VerificationVerdict.UNCERTAIN,
                        reason=verdict.evidence or "role verification uncertain",
                        evidence_refs=(after.evidence_id,),
                        method=VerificationMethod.ROLE_ASSISTED,
                    )
                    self.kernel.mark_task_uncertain(
                        task_id=task_id,
                        code="verification_uncertain",
                        summary="新观察仍不足以判断动作结果；未自动重放。",
                    )
                    return
                kernel_verdict = (
                    VerificationVerdict.SUCCESS
                    if verdict.satisfied or verdict.progress
                    else VerificationVerdict.FAIL
                )
                self.kernel.verify_action(
                    task_id=task_id,
                    action_id=action.id,
                    before_observation_id=self.kernel.load_action(task_id, action.id).based_on_observation_id,
                    after_observation_id=self.kernel.load_task(task_id).last_observation_id or "",
                    verdict=kernel_verdict,
                    reason=verdict.evidence or "role-assisted fresh observation comparison",
                    evidence_refs=(after.evidence_id,),
                    method=VerificationMethod.ROLE_ASSISTED,
                    complete_stage=verdict.satisfied,
                    progress_summary=verdict.evidence if verdict.satisfied else None,
                )
                self.kernel.record_worker_event(
                    task_id=task_id,
                    event_type="KernelRoleVerdict",
                    payload={
                        "action_id": action.id,
                        "satisfied": verdict.satisfied,
                        "progress": verdict.progress,
                        "uncertain": verdict.uncertain,
                        "evidence": verdict.evidence,
                    },
                )
                if self.experience is not None:
                    try:
                        persisted_state = self.inspect(task_id)
                        persisted_attempt = next(
                            item
                            for item in persisted_state.attempts
                            if item.attempt_id == action.id
                        )
                        self.experience.record_mobile_attempt(
                            source_task_id=task_id,
                            objective=stage.objective,
                            attempt=persisted_attempt,
                            retrieval=experience_packet,
                        )
                    except Exception:
                        pass
                observation = after
                no_progress = 0 if verdict.progress else no_progress + 1
                if no_progress >= 3:
                    if reflection_count >= self.max_reflections:
                        self.kernel.fail_task(
                            task_id=task_id,
                            code="reflection_limit_reached",
                            summary="连续无可验证进展，已触发有界失控保护。",
                        )
                        return
                    reflection = self.model.reflect(
                        ReflectionContext(
                            task_id=task_id,
                            goal=task.goal,
                            subgoal=subgoal,
                            input_revision=state.input_revision,
                            owner_inputs=state.inputs,
                            strategy=strategy,
                            consecutive_no_progress=no_progress,
                            recent_attempts=self.inspect(task_id).attempts[-8:],
                            skill_memory=None,
                        )
                    )
                    reflection_count += 1
                    self.kernel.record_worker_event(
                        task_id=task_id,
                        event_type="KernelStrategyChanged",
                        payload={
                            "previous_strategy": strategy,
                            "strategy": reflection.strategy,
                            "reason": reflection.reason,
                            "no_progress_count": no_progress,
                        },
                    )
                    if reflection.terminate:
                        self.kernel.fail_task(
                            task_id=task_id,
                            code="reflection_terminated",
                            summary=reflection.reason or "Recovery role terminated the canary.",
                        )
                        return
                    strategy = reflection.strategy
                    no_progress = 0
        finally:
            lease.release()

    def _observe(self, task_id: str) -> RoleObservation:
        task = self.kernel.load_task(task_id)
        observation = self.kernel.capture_observation(
            task_id=task_id, device_id=task.device_id
        )
        screenshot = self.artifacts.read(observation.screenshot.artifact)
        role_observation = self.evidence.record(
            task_id,
            AndroidScreenshot(
                screenshot,
                width=observation.screenshot.width,
                height=observation.screenshot.height,
            ),
        )
        self.kernel.record_worker_event(
            task_id=task_id,
            event_type="KernelObservationLinked",
            payload={
                "kernel_observation_id": observation.id,
                "evidence_id": role_observation.evidence_id,
            },
        )
        return role_observation


def _canonical_device_id(target_id: str | None) -> str:
    if not isinstance(target_id, str) or not target_id.strip():
        raise KernelCanaryError("kernel canary requires an explicit Android target")
    value = target_id.strip()
    return value if value.startswith("adb:") else f"adb:{value}"


def _kernel_action(intent: PhysicalIntent) -> tuple[ActionType, dict[str, Any]]:
    args = dict(intent.arguments)
    if intent.name == "tap":
        return ActionType.TAP, {"x": int(args["x"]), "y": int(args["y"])}
    if intent.name == "swipe":
        return ActionType.SWIPE, {
            "start_x": int(args.get("x", args.get("start_x"))),
            "start_y": int(args.get("y", args.get("start_y"))),
            "end_x": int(args["end_x"]),
            "end_y": int(args["end_y"]),
            "duration_ms": int(args.get("duration_ms", 300)),
        }
    if intent.name == "text":
        return ActionType.INPUT_TEXT, {"text": str(args["text"])}
    if intent.name == "wait":
        seconds = max(0.0, min(float(args.get("seconds", 1.0)), 10.0))
        return ActionType.WAIT, {"seconds": seconds}
    if intent.name == "screenshot":
        return ActionType.SCREENSHOT, {}
    if intent.name == "keyevent":
        keycode = str(args.get("keycode", ""))
        if keycode == "KEYCODE_BACK":
            return ActionType.BACK, {}
        if keycode == "KEYCODE_HOME":
            return ActionType.HOME, {}
    raise KernelCanaryError(f"unsupported physical intent: {intent.name}")


def _decision_from_action(action: Any) -> ActionDecision:
    if action.type is ActionType.SCREENSHOT and action.expected_outcome.startswith("finish-check:"):
        return ActionDecision("finish", reason=action.expected_outcome)
    name, arguments = {
        ActionType.TAP: ("tap", action.params),
        ActionType.SWIPE: ("swipe", action.params),
        ActionType.INPUT_TEXT: ("text", action.params),
        ActionType.BACK: ("keyevent", {"keycode": "KEYCODE_BACK"}),
        ActionType.HOME: ("keyevent", {"keycode": "KEYCODE_HOME"}),
        ActionType.WAIT: ("wait", action.params),
        ActionType.SCREENSHOT: ("screenshot", {}),
    }.get(action.type, (action.type.value, action.params))
    return ActionDecision("act", PhysicalIntent(name, arguments), action.expected_outcome)


def _role_observation(kernel_id: str, links: dict[str, str]) -> RoleObservation:
    evidence_id = links.get(kernel_id, kernel_id)
    return RoleObservation(evidence_id, f"Kernel observation {kernel_id}")


def _inputs(goal: str, created_at: str, events: tuple[Any, ...]) -> tuple[InputRevision, ...]:
    values = [InputRevision(1, goal, "applied", "initial", created_at, created_at)]
    for event in events:
        if event.type != "UserMessageReceived":
            continue
        revision = len(values) + 1
        values.append(
            InputRevision(
                revision,
                str(event.payload.get("text") or ""),
                "applied",
                str(event.payload.get("message_id") or f"message-{revision}"),
                event.created_at,
                event.created_at,
            )
        )
    return tuple(values)


def _project_status(task: Any, stages: tuple[Any, ...]) -> str:
    status = task.status
    if status is TaskStatus.CREATED:
        return "queued"
    if status is TaskStatus.PLANNING:
        return (
            "completed"
            if stages and all(stage.status is StageStatus.COMPLETED for stage in stages)
            else "planning"
        )
    if status is TaskStatus.RUNNING:
        return "running"
    if status is TaskStatus.PAUSED:
        return "paused"
    if status is TaskStatus.COMPLETED:
        return "completed"
    if status is TaskStatus.CANCELLED:
        return "stopped"
    if (
        status is TaskStatus.FAILED
        and task.failure_state is not None
        and task.failure_state.last_verdict == "UNCERTAIN"
    ):
        return "uncertain"
    return "failed"
