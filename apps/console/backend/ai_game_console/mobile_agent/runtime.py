from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .domain import (
    ActionDecision,
    DecisionContext,
    ExperienceHint,
    InputRevision,
    MobileTaskState,
    Observation,
    PlanContext,
    ReflectionContext,
    RoleModel,
    SkillScopeResolver,
    Subgoal,
    TaskDriver,
    TaskQueueFull,
    TaskRuntimeClosed,
    TaskSession,
    TransportReceipt,
    Verification,
    VerificationContext,
)
from .store import (
    _SQLiteTaskStore,
    _automatic_skill_scope_id,
    _legacy_skill_scope_id,
)


_COORDINATOR_STOP = object()


class MobileTaskRuntime:
    """A deep, durable Module for long-horizon mobile objectives.

    The five task methods are the caller/test Interface. A single coordinator
    serializes model/device use, while ``queue_capacity`` bounds accepted but
    unfinished tasks. Construction initializes storage and resumes only tasks
    whose persisted checkpoint contains no open physical intent. ``shutdown``
    is the explicit process-lifecycle hook and leaves safe work recoverable.
    """

    def __init__(
        self,
        database_path: Path | str,
        *,
        driver: TaskDriver,
        model: RoleModel,
        max_reflections: int = 3,
        max_attempts: int = 64,
        queue_capacity: int = 32,
        scope_resolver: SkillScopeResolver | None = None,
        experience: Any | None = None,
    ) -> None:
        if max_reflections < 1:
            raise ValueError("max_reflections must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self._store = _SQLiteTaskStore(database_path)
        self._driver = driver
        self._model = model
        self._max_reflections = max_reflections
        self._max_attempts = max_attempts
        self._scope_resolver = scope_resolver
        self._experience = experience
        self._attempt_experience: dict[str, Any] = {}
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_capacity)
        self._slots = threading.BoundedSemaphore(queue_capacity)
        self._admission_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_requested = threading.Event()
        self._coordinator = threading.Thread(
            target=self._coordinate,
            name="mobile-task-coordinator",
            daemon=True,
        )
        recovered = self._store.recover_active()
        self._coordinator.start()
        for task_id in recovered:
            if self._slots.acquire(blocking=False):
                self._queue.put_nowait(task_id)
            else:
                self._store.fail_unclaimed(
                    task_id,
                    error_code="recovery_queue_full",
                    detail="恢复任务数量超过本地有界队列容量。",
                )

    def start(
        self,
        goal: str,
        client_request_id: str,
        target_id: str | None = None,
        skill_id: str | None = None,
        execution_origin: str = "direct_v1",
        promote_success_memory: bool = True,
        goal_id: str | None = None,
        goal_spec_revision: int | None = None,
        frozen_criteria_ids: tuple[str, ...] = (),
    ) -> MobileTaskState:
        goal = _required_text(goal, "goal", 16_000)
        request_id = _required_text(client_request_id, "client_request_id", 512)
        target_id = _optional_text(target_id, "target_id", 1_000)
        skill_id = _optional_text(skill_id, "skill_id", 1_000)
        execution_origin = _required_text(execution_origin, "execution_origin", 128)
        goal_id = _optional_text(goal_id, "goal_id", 512)
        if not isinstance(promote_success_memory, bool):
            raise ValueError("promote_success_memory must be a boolean")
        digest_payload: dict[str, object] = {
            "goal": goal,
            "target_id": target_id,
            "skill_id": skill_id,
        }
        # Preserve the exact historical v1 digest so an idempotent retry made
        # after upgrading does not conflict with its pre-U1 request record.
        if execution_origin != "direct_v1" or not promote_success_memory:
            digest_payload.update(
                execution_origin=execution_origin,
                promote_success_memory=promote_success_memory,
            )
        if goal_id is not None:
            digest_payload.update(
                goal_id=goal_id,
                goal_spec_revision=goal_spec_revision,
                frozen_criteria_ids=frozen_criteria_ids,
            )
        digest = _digest("start", digest_payload)
        with self._admission_lock:
            self._ensure_mutable()
            existing = self._store.existing_request(request_id, digest)
            if existing is not None:
                self._begin_experience(
                    existing, goal_id=goal_id,
                    goal_spec_revision=goal_spec_revision,
                    frozen_criteria_ids=frozen_criteria_ids,
                )
                return existing
            if not self._slots.acquire(blocking=False):
                raise TaskQueueFull("mobile task queue is full")
            task_id = str(uuid.uuid4())
            try:
                skill_scope_id = self._resolve_skill_scope(
                    goal=goal,
                    target_id=target_id,
                    skill_id=skill_id,
                )
                state, created = self._store.accept_start(
                    task_id=task_id,
                    goal=goal,
                    target_id=target_id,
                    skill_id=skill_id,
                    skill_scope_id=skill_scope_id,
                    execution_origin=execution_origin,
                    promote_success_memory=promote_success_memory,
                    client_request_id=request_id,
                    request_digest=digest,
                )
                if not created:
                    self._slots.release()
                    return state
                self._begin_experience(
                    state, goal_id=goal_id,
                    goal_spec_revision=goal_spec_revision,
                    frozen_criteria_ids=frozen_criteria_ids,
                )
                self._queue.put_nowait(task_id)
                return state
            except Exception:
                self._slots.release()
                raise

    def send(
        self, task_id: str, content: str, client_request_id: str
    ) -> MobileTaskState:
        task_id = _required_text(task_id, "task_id", 512)
        content = _required_text(content, "content", 10_000)
        request_id = _required_text(client_request_id, "client_request_id", 512)
        with self._dispatch_lock:
            self._ensure_mutable()
            return self._store.accept_input(
                task_id=task_id,
                content=content,
                client_request_id=request_id,
                request_digest=_digest(
                    "send", {"task_id": task_id, "content": content}
                ),
            )

    def stop(self, task_id: str, client_request_id: str) -> MobileTaskState:
        task_id = _required_text(task_id, "task_id", 512)
        request_id = _required_text(client_request_id, "client_request_id", 512)
        with self._dispatch_lock:
            self._ensure_mutable()
            return self._store.accept_stop(
                task_id=task_id,
                client_request_id=request_id,
                request_digest=_digest("stop", {"task_id": task_id}),
            )

    def inspect(self, task_id: str) -> MobileTaskState:
        return self._store.inspect(_required_text(task_id, "task_id", 512))

    def promote_goal_verified_memory(
        self, task_id: str, *, goal_id: str, completion_revision: int
    ) -> int:
        return self._store.promote_goal_verified_memory(
            _required_text(task_id, "task_id", 512),
            goal_id=_required_text(goal_id, "goal_id", 512),
            completion_revision=completion_revision,
        )

    def promote_goal_verified_experience(
        self, task_id: str, *, goal_id: str, completion_revision: int,
        goal_spec_revision: int | None = None,
        frozen_criteria_ids: tuple[str, ...] = (),
    ) -> Any | None:
        if self._experience is None:
            return None
        state = self.inspect(_required_text(task_id, "task_id", 512))
        self._begin_experience(
            state, goal_id=goal_id, goal_spec_revision=goal_spec_revision,
            frozen_criteria_ids=frozen_criteria_ids,
        )
        self._experience.sync_mobile_task(state)
        return self._experience.confirm_goal_completion(
            state.task_id, goal_id=_required_text(goal_id, "goal_id", 512),
            completion_revision=completion_revision,
        )

    def list(self, limit: int = 100) -> list[MobileTaskState]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return self._store.list(limit)

    def active_tasks(self) -> tuple[int, list[str]]:
        """排空门禁：返回 (活动任务数, 活动任务 ID 列表)。"""
        return self._store.active_tasks()

    def shutdown(self, timeout: float | None = 5.0) -> None:
        """Quiesce the coordinator, leaving unexecuted work restart-recoverable."""

        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0
        ):
            raise ValueError("timeout must be a non-negative number or None")
        if threading.current_thread() is self._coordinator:
            raise RuntimeError("coordinator cannot shut itself down")
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        with self._shutdown_lock:
            with self._admission_lock:
                first_request = not self._shutdown_requested.is_set()
                self._shutdown_requested.set()
            if first_request:
                try:
                    self._queue.put_nowait(_COORDINATOR_STOP)
                except queue.Full:
                    pass
            remaining = _remaining(deadline)
            acquired = self._dispatch_lock.acquire(
                timeout=remaining if remaining is not None else -1
            )
            if not acquired:
                raise TimeoutError("mobile task runtime shutdown timed out")
            self._dispatch_lock.release()
            self._coordinator.join(_remaining(deadline))
            if self._coordinator.is_alive():
                raise TimeoutError("mobile task runtime shutdown timed out")

    def _coordinate(self) -> None:
        while True:
            item = self._queue.get()
            if item is _COORDINATOR_STOP:
                self._queue.task_done()
                return
            task_id = str(item)
            if self._shutdown_requested.is_set():
                self._queue.task_done()
                self._slots.release()
                return
            try:
                try:
                    self._run_task(task_id)
                except Exception:
                    # One corrupt/missing queue entry must not kill the sole coordinator.
                    pass
            finally:
                self._queue.task_done()
                self._slots.release()
            if self._shutdown_requested.is_set():
                return

    def _run_task(self, task_id: str) -> None:
        worker_token = str(uuid.uuid4())
        if not self._store.claim(task_id, worker_token):
            return
        if self._shutdown_requested.is_set():
            self._store.release_for_shutdown(task_id, worker_token=worker_token)
            return
        session: TaskSession | None = None
        try:
            state = self._store.inspect(task_id)
            session = self._driver.open(task_id, state.target_id)
            self._work_loop(task_id, worker_token, session)
        except Exception as exc:
            if not (
                self._shutdown_requested.is_set()
                and self._store.release_for_shutdown(
                    task_id, worker_token=worker_token
                )
            ):
                self._store.fail(
                    task_id,
                    worker_token=worker_token,
                    error_code=_error_code(exc, "runtime_failed"),
                    detail=_public_detail(exc, "MobileTask 执行失败。"),
                )
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _work_loop(
        self, task_id: str, worker_token: str, session: TaskSession
    ) -> None:
        while True:
            state = self._store.inspect(task_id)
            if state.terminal:
                return
            if state.cancel_requested:
                self._store.finish_stopped(task_id, worker_token=worker_token)
                return
            if self._shutdown_requested.is_set():
                self._store.release_for_shutdown(task_id, worker_token=worker_token)
                return
            if state.plan is None:
                self._plan(task_id, worker_token, state, session)
                continue
            if state.no_progress_count >= 3:
                if state.reflection_count >= self._max_reflections:
                    self._store.fail(
                        task_id,
                        worker_token=worker_token,
                        error_code="reflection_budget_exhausted",
                        detail="连续无进展，且已达到反思次数上限。",
                    )
                    return
                self._reflect(task_id, worker_token, state)
                continue
            if state.attempt_count >= self._max_attempts:
                self._store.fail(
                    task_id,
                    worker_token=worker_token,
                    error_code="attempt_budget_exhausted",
                    detail="已达到动作尝试上限，任务未被验证完成。",
                )
                return
            self._decide_and_apply(task_id, worker_token, state, session)

    def _plan(
        self,
        task_id: str,
        worker_token: str,
        state: MobileTaskState,
        session: TaskSession,
    ) -> None:
        observation = session.observe()
        if self._shutdown_requested.is_set():
            self._store.release_for_shutdown(task_id, worker_token=worker_token)
            return
        draft = self._model.plan(
            PlanContext(
                task_id=task_id,
                goal=state.goal,
                target_id=state.target_id,
                input_revision=state.input_revision,
                owner_inputs=state.inputs,
                observation=observation,
                skill_memory=self._store.skill_memory(state.skill_scope_id),
            )
        )
        if self._shutdown_requested.is_set():
            self._store.release_for_shutdown(task_id, worker_token=worker_token)
            return
        result = self._store.set_plan_if_current(
            task_id=task_id,
            worker_token=worker_token,
            expected_input_revision=state.input_revision,
            draft=draft,
        )
        if result == "closed":
            self._finish_if_cancelled(task_id, worker_token)

    def _reflect(
        self, task_id: str, worker_token: str, state: MobileTaskState
    ) -> None:
        subgoal = _active_subgoal(state)
        decision = self._model.reflect(
            ReflectionContext(
                task_id=task_id,
                goal=state.goal,
                subgoal=subgoal,
                input_revision=state.input_revision,
                owner_inputs=state.inputs,
                strategy=state.strategy,
                consecutive_no_progress=state.no_progress_count,
                recent_attempts=state.attempts[-3:],
                skill_memory=self._store.skill_memory(state.skill_scope_id),
            )
        )
        if self._shutdown_requested.is_set():
            self._store.release_for_shutdown(task_id, worker_token=worker_token)
            return
        result = self._store.record_reflection_if_current(
            task_id=task_id,
            worker_token=worker_token,
            expected_input_revision=state.input_revision,
            decision=decision,
        )
        if result == "unchanged":
            self._store.fail(
                task_id,
                worker_token=worker_token,
                error_code="reflection_no_strategy_change",
                detail="反思没有改变策略，也没有终止任务。",
            )
        elif result == "closed":
            self._finish_if_cancelled(task_id, worker_token)

    def _decide_and_apply(
        self,
        task_id: str,
        worker_token: str,
        state: MobileTaskState,
        session: TaskSession,
    ) -> None:
        subgoal = _active_subgoal(state)
        before = session.observe()
        if self._shutdown_requested.is_set():
            self._store.release_for_shutdown(task_id, worker_token=worker_token)
            return
        experience_packet = self._retrieve_experience(
            task_id=task_id, objective=subgoal.description, observation=before
        )
        decision = self._model.decide(
            DecisionContext(
                task_id=task_id,
                goal=state.goal,
                target_id=state.target_id,
                plan_revision=state.plan.revision,  # type: ignore[union-attr]
                subgoal=subgoal,
                input_revision=state.input_revision,
                owner_inputs=state.inputs,
                observation=before,
                strategy=state.strategy,
                consecutive_no_progress=state.no_progress_count,
                recent_attempts=state.attempts[-8:],
                skill_memory=self._store.skill_memory(state.skill_scope_id),
                experience_hints=_experience_hints(experience_packet),
            )
        )
        if self._shutdown_requested.is_set():
            self._store.release_for_shutdown(task_id, worker_token=worker_token)
            return
        attempt_id = str(uuid.uuid4())
        result = self._store.begin_attempt(
            attempt_id=attempt_id,
            task_id=task_id,
            worker_token=worker_token,
            expected_input_revision=state.input_revision,
            decision=decision,
            before=before,
        )
        if result == "stale":
            return
        if result == "closed":
            self._finish_if_cancelled(task_id, worker_token)
            return
        if experience_packet is not None:
            self._attempt_experience[attempt_id] = experience_packet
        verification_owner_inputs = _applied_inputs_through_revision(
            self._store.inspect(task_id), state.input_revision
        )
        if self._shutdown_requested.is_set() and decision.kind != "act":
            self._store.release_for_shutdown(task_id, worker_token=worker_token)
            return
        if decision.kind == "terminate":
            self._finish_attempt(
                attempt_id=attempt_id,
                task_id=task_id,
                worker_token=worker_token,
                transport=TransportReceipt("not_sent"),
                after=None,
                verification=Verification(False, False, evidence=decision.reason),
                terminal=(
                    "failed",
                    "model_terminated",
                    decision.reason or "模型终止任务，但任务没有验证完成。",
                ),
            )
            return
        if decision.kind == "finish":
            self._verify_without_transport(
                task_id,
                worker_token,
                attempt_id,
                state,
                subgoal,
                decision,
                before,
                verification_owner_inputs,
                session,
            )
            return
        self._execute_physical(
            task_id,
            worker_token,
            attempt_id,
            state,
            subgoal,
            decision,
            before,
            verification_owner_inputs,
            session,
        )

    def _verify_without_transport(
        self,
        task_id: str,
        worker_token: str,
        attempt_id: str,
        state: MobileTaskState,
        subgoal: Subgoal,
        decision: ActionDecision,
        before: Observation,
        owner_inputs: tuple[InputRevision, ...],
        session: TaskSession,
    ) -> None:
        after: Observation | None = None
        try:
            after = session.observe()
            receipt = TransportReceipt("not_sent", detail="verification-only")
            verification = self._model.verify(
                VerificationContext(
                    task_id=task_id,
                    goal=state.goal,
                    subgoal=subgoal,
                    decision=decision,
                    before=before,
                    transport=receipt,
                    after=after,
                    input_revision=state.input_revision,
                    owner_inputs=owner_inputs,
                )
            )
        except Exception as exc:
            if (
                after is not None
                and _error_code(exc, "completion_verification_failed")
                == "mobile_role_invalid_response"
            ):
                self._finish_attempt(
                    attempt_id=attempt_id,
                    task_id=task_id,
                    worker_token=worker_token,
                    transport=TransportReceipt("not_sent", detail="verification-only"),
                    after=after,
                    verification=Verification(
                        False,
                        False,
                        evidence="local verifier format invalid; retrying with a fresh observation",
                    ),
                )
                return
            self._finish_attempt(
                attempt_id=attempt_id,
                task_id=task_id,
                worker_token=worker_token,
                transport=TransportReceipt("not_sent"),
                after=None,
                verification=Verification(False, False, evidence="verification failed"),
                terminal=(
                    "failed",
                    _error_code(exc, "completion_verification_failed"),
                    _public_detail(exc, "完成验证失败，任务没有完成。"),
                ),
            )
            return
        self._finish_attempt(
            attempt_id=attempt_id,
            task_id=task_id,
            worker_token=worker_token,
            transport=receipt,
            after=after,
            verification=verification,
        )

    def _execute_physical(
        self,
        task_id: str,
        worker_token: str,
        attempt_id: str,
        state: MobileTaskState,
        subgoal: Subgoal,
        decision: ActionDecision,
        before: Observation,
        owner_inputs: tuple[InputRevision, ...],
        session: TaskSession,
    ) -> None:
        with self._dispatch_lock:
            fence = self._store.fence_physical_dispatch(
                attempt_id=attempt_id,
                task_id=task_id,
                worker_token=worker_token,
                expected_input_revision=state.input_revision,
                shutdown_requested=self._shutdown_requested.is_set(),
            )
            if fence != "dispatch":
                return
            try:
                receipt = session.execute(decision.intent)  # type: ignore[arg-type]
            except Exception as exc:
                receipt = TransportReceipt("uncertain", detail="transport raised")
                self._finish_attempt(
                    attempt_id=attempt_id,
                    task_id=task_id,
                    worker_token=worker_token,
                    transport=receipt,
                    after=None,
                    verification=Verification(
                        False, False, uncertain=True, evidence="transport unknown"
                    ),
                    terminal=(
                        "uncertain",
                        _error_code(exc, "transport_uncertain"),
                        "物理意图已经落账，但传输结果未知；任务终止且不会重放。",
                    ),
                )
                return
        if receipt.status == "uncertain":
            self._finish_attempt(
                attempt_id=attempt_id,
                task_id=task_id,
                worker_token=worker_token,
                transport=receipt,
                after=None,
                verification=Verification(
                    False,
                    False,
                    uncertain=True,
                    evidence=receipt.detail or "transport uncertain",
                ),
                terminal=(
                    "uncertain",
                    "transport_uncertain",
                    "传输结果未知；任务终止且不会重放该物理意图。",
                ),
            )
            return
        if receipt.status != "accepted":
            self._finish_attempt(
                attempt_id=attempt_id,
                task_id=task_id,
                worker_token=worker_token,
                transport=receipt,
                after=None,
                verification=Verification(
                    False, False, evidence=receipt.detail or f"transport {receipt.status}"
                ),
            )
            return
        try:
            after = session.observe()
        except Exception as exc:
            self._finish_attempt(
                attempt_id=attempt_id,
                task_id=task_id,
                worker_token=worker_token,
                transport=receipt,
                after=None,
                verification=Verification(
                    False, False, uncertain=True, evidence="fresh observation unavailable"
                ),
                terminal=(
                    "uncertain",
                    _error_code(exc, "post_action_observation_unknown"),
                    "传输已接收，但无法取得新鲜后置观察；未重放物理意图。",
                ),
            )
            return
        try:
            verification = self._model.verify(
                VerificationContext(
                    task_id=task_id,
                    goal=state.goal,
                    subgoal=subgoal,
                    decision=decision,
                    before=before,
                    transport=receipt,
                    after=after,
                    input_revision=state.input_revision,
                    owner_inputs=owner_inputs,
                )
            )
        except Exception as exc:
            self._finish_attempt(
                attempt_id=attempt_id,
                task_id=task_id,
                worker_token=worker_token,
                transport=receipt,
                after=after,
                verification=Verification(
                    False, False, uncertain=True, evidence="verification unavailable"
                ),
                terminal=(
                    "uncertain",
                    _error_code(exc, "verification_unknown_after_transport"),
                    "物理动作已被传输，但验证角色失败；任务终止且不重放。",
                ),
            )
            return
        self._finish_attempt(
            attempt_id=attempt_id,
            task_id=task_id,
            worker_token=worker_token,
            transport=receipt,
            after=after,
            verification=verification,
        )

    def _finish_if_cancelled(self, task_id: str, worker_token: str) -> None:
        state = self._store.inspect(task_id)
        if state.cancel_requested and not state.terminal:
            self._store.finish_stopped(task_id, worker_token=worker_token)

    def _finish_attempt(self, **kwargs: Any) -> MobileTaskState | None:
        state = self._store.finish_attempt(**kwargs)
        attempt_id = str(kwargs["attempt_id"])
        packet = self._attempt_experience.pop(attempt_id, None)
        if state is None or self._experience is None:
            return state
        attempt = next((item for item in state.attempts if item.attempt_id == attempt_id), None)
        if attempt is None or state.plan is None:
            return state
        objective = next(
            (item.description for item in state.plan.subgoals
             if item.index == attempt.subgoal_index),
            state.goal,
        )
        try:
            self._experience.record_mobile_attempt(
                source_task_id=state.task_id, objective=objective,
                attempt=attempt, retrieval=packet,
            )
        except Exception:
            # Learning is additive evidence. A ledger outage cannot rewrite or
            # block the physical owner's already-persisted task truth.
            pass
        return state

    def _begin_experience(
        self, state: MobileTaskState, *, goal_id: str | None,
        goal_spec_revision: int | None, frozen_criteria_ids: tuple[str, ...]
    ) -> None:
        if (
            self._experience is None or goal_id is None
            or goal_spec_revision is None or goal_spec_revision < 1
            or not frozen_criteria_ids
        ):
            return
        try:
            self._experience.begin_mobile_episode_for_task(
                goal_run_id=goal_id, source_task_id=state.task_id,
                goal_spec_revision=goal_spec_revision,
                frozen_criteria_ids=frozen_criteria_ids,
                skill_scope_id=state.skill_scope_id,
                target_id=state.target_id,
            )
        except Exception:
            pass

    def _retrieve_experience(
        self, *, task_id: str, objective: str, observation: Observation
    ) -> Any | None:
        if self._experience is None:
            return None
        try:
            return self._experience.retrieve(
                source_task_id=task_id, objective=objective, observation=observation
            )
        except Exception:
            return None

    def _resolve_skill_scope(
        self,
        *,
        goal: str,
        target_id: str | None,
        skill_id: str | None,
    ) -> str | None:
        if skill_id is not None:
            return _legacy_skill_scope_id(skill_id)
        if self._scope_resolver is None:
            return None
        resolved = _optional_text(
            self._scope_resolver(goal, target_id),
            "scope_resolver result",
            1_000,
        )
        return _automatic_skill_scope_id(resolved) if resolved is not None else None

    def _ensure_mutable(self) -> None:
        if self._shutdown_requested.is_set():
            raise TaskRuntimeClosed("mobile task runtime is closed")


def _active_subgoal(state: MobileTaskState) -> Subgoal:
    if state.plan is None or state.active_subgoal_index >= len(state.plan.subgoals):
        raise RuntimeError("active Subgoal is missing")
    return state.plan.subgoals[state.active_subgoal_index]


def _applied_inputs_through_revision(
    state: MobileTaskState,
    input_revision: int,
) -> tuple[InputRevision, ...]:
    """Keep verification bound to the action's applied input revision."""

    return tuple(
        owner_input
        for owner_input in state.inputs
        if owner_input.lifecycle == "applied"
        and owner_input.revision <= input_revision
    )


def _experience_hints(packet: Any | None) -> tuple[ExperienceHint, ...]:
    if packet is None:
        return ()
    hints: list[ExperienceHint] = []
    for item in getattr(packet, "items", ()):
        hints.append(ExperienceHint(
            candidate_id=str(getattr(item, "candidate_id")),
            kind=getattr(item, "kind"),
            semantic_action=str(getattr(item, "semantic_action")),
            expected_next_scene=getattr(item, "expected_next_scene", None),
            recovery_action=getattr(item, "recovery_action", None),
            confidence=float(getattr(item, "confidence", 0.0)),
            provenance_transition_ids=tuple(
                getattr(item, "provenance_transition_ids", ())
            ),
        ))
    return tuple(hints)


def _required_text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{name} is too long")
    return normalized


def _optional_text(value: str | None, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, name, maximum)


def _digest(operation: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error_code(exc: Exception, fallback: str) -> str:
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, str) and code else fallback


def _public_detail(exc: Exception, fallback: str) -> str:
    detail = getattr(exc, "public_message", None)
    return str(detail) if isinstance(detail, str) and detail else fallback


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())
