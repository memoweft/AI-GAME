from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import uuid
from pathlib import Path

from .domain import (
    ActionDecision,
    DecisionContext,
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
    ) -> MobileTaskState:
        goal = _required_text(goal, "goal", 16_000)
        request_id = _required_text(client_request_id, "client_request_id", 512)
        target_id = _optional_text(target_id, "target_id", 1_000)
        skill_id = _optional_text(skill_id, "skill_id", 1_000)
        digest = _digest(
            "start",
            {"goal": goal, "target_id": target_id, "skill_id": skill_id},
        )
        with self._admission_lock:
            self._ensure_mutable()
            existing = self._store.existing_request(request_id, digest)
            if existing is not None:
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
                    client_request_id=request_id,
                    request_digest=digest,
                )
                if not created:
                    self._slots.release()
                    return state
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

    def list(self, limit: int = 100) -> list[MobileTaskState]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return self._store.list(limit)

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
        verification_owner_inputs = _applied_inputs_through_revision(
            self._store.inspect(task_id), state.input_revision
        )
        if self._shutdown_requested.is_set() and decision.kind != "act":
            self._store.release_for_shutdown(task_id, worker_token=worker_token)
            return
        if decision.kind == "terminate":
            self._store.finish_attempt(
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
                self._store.finish_attempt(
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
            self._store.finish_attempt(
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
        self._store.finish_attempt(
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
                self._store.finish_attempt(
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
            self._store.finish_attempt(
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
            self._store.finish_attempt(
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
            self._store.finish_attempt(
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
            self._store.finish_attempt(
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
        self._store.finish_attempt(
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
