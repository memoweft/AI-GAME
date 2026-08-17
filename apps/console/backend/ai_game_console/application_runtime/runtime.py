from __future__ import annotations

import os
import queue
import threading
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Mapping

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - exercised by non-Windows CI
    import fcntl

from .domain import (
    ApplicationInstance,
    Command,
    Decision,
    ExecutionOwner,
    ExecutionReceipt,
    ExecutionReconciliation,
    Input,
    Intent,
    MemoryCandidate,
    MemoryGate,
    Observation,
    ObservationPort,
    Outcome,
    Pause,
    PersistenceProjection,
    Policy,
    PolicyContext,
    QueueFull,
    Resume,
    RetryableApplicationError,
    RuntimeClosed,
    RuntimeIntent,
    Stop,
    Verifier,
    VerificationContext,
)
from .store import _SQLiteApplicationStore, request_digest


_STOP = object()


class _ProcessDatabaseLock:
    """Process-wide singleton ownership for one runtime database.

    SQLite remains readable through the archive while this sidecar byte-range
    lock prevents a second coordinator from calling ``recover`` against live
    work. Operating-system locks are released automatically after a crash.
    """

    def __init__(self, database_path: Path | str) -> None:
        resolved = Path(database_path).resolve()
        self.path = Path(str(resolved) + ".application-runtime.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        self._handle: BinaryIO | None = handle
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised by non-Windows CI
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self._handle = None
            raise RuntimeError(
                "application runtime database is already active"
            ) from None

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised by non-Windows CI
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __del__(self) -> None:  # pragma: no cover - defensive constructor cleanup
        try:
            self.close()
        except Exception:
            pass


class _IdentityPersistenceProjection:
    def project_input(self, value: str) -> str:
        return value

    def project_observation(self, value: Observation) -> Observation:
        return value

    def project_intent(self, value: Intent) -> Intent:
        return value

    def project_receipt(self, value: ExecutionReceipt) -> ExecutionReceipt:
        return value

    def project_outcome(self, value: Outcome) -> Outcome:
        return value

    def project_memory_candidate(self, value: MemoryCandidate) -> MemoryCandidate:
        return value

    def project_memory_content(
        self, scope: str, value: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return value

    def project_detail(self, value: str) -> str:
        return value


class ApplicationRuntime:
    """A generic, serialized and durable application-cycle coordinator.

    A queue slot belongs to one active instance while it is queued, running,
    or waiting on an interruptible timer.  Physical dispatch is a commit fence:
    Input/Pause/Stop cannot be acknowledged between the final revision check
    and the owner's dispatch result.
    """

    def __init__(
        self,
        database_path: Path | str,
        *,
        profile: str,
        observation_port: ObservationPort,
        policy: Policy,
        execution_owner: ExecutionOwner,
        verifier: Verifier,
        memory_gate: MemoryGate | None = None,
        memory_scope: str | None = None,
        persistence_projection: PersistenceProjection | None = None,
        queue_capacity: int = 32,
    ) -> None:
        self._required(profile, "profile")
        if memory_scope is not None:
            self._required(memory_scope, "memory_scope")
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self._store = _SQLiteApplicationStore(database_path)
        self._process_database_lock = _ProcessDatabaseLock(database_path)
        self._profile = profile
        self._memory_scope = memory_scope or profile
        self._observation_port = observation_port
        self._policy = policy
        self._execution_owner = execution_owner
        self._verifier = verifier
        self._memory_gate = memory_gate
        self._projection = persistence_projection or _IdentityPersistenceProjection()
        self._queue: queue.Queue[object] = queue.Queue(queue_capacity)
        self._slots = threading.BoundedSemaphore(queue_capacity)
        self._slot_lock = threading.Lock()
        self._slot_instances: set[str] = set()
        self._admission_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._timer_lock = threading.Lock()
        self._timers: dict[
            str, tuple[str, str | None, threading.Timer]
        ] = {}
        self._closed = threading.Event()

        recovered = self._store.recover()
        self._thread = threading.Thread(
            target=self._coordinate,
            name="application-runtime-coordinator",
            daemon=True,
        )
        self._thread.start()
        for instance_id, delay in recovered:
            available, _new = self._acquire_slot(instance_id)
            if not available:
                self._store.fail_cycle(
                    instance_id,
                    None,
                    "recovery_admission_full",
                    "recovery",
                )
                continue
            if delay > 0:
                self._schedule_timer(instance_id, delay)
            else:
                state = self._store.inspect(instance_id)
                if state.status == "waiting":
                    self._store.wake_waiting(instance_id, state.wake_at)
                self._queue.put_nowait(instance_id)

    def start(
        self,
        profile_id: str,
        client_request_id: str,
        target_id: str | None = None,
        initial_input: str | None = None,
    ) -> ApplicationInstance:
        if profile_id != self._profile:
            raise ValueError("profile_id must match runtime profile")
        self._required(client_request_id, "client_request_id")
        if initial_input is not None:
            self._required(initial_input, "initial_input")
        digest = request_digest(
            "start",
            {
                "profile_id": profile_id,
                "target_id": target_id,
                "initial_input": initial_input,
            },
        )
        with self._admission_lock:
            self._ensure_open()
            existing = self._store.existing_request(client_request_id, digest)
            if existing is not None:
                return existing
            instance_id = str(uuid.uuid4())
            available, _new = self._acquire_slot(instance_id)
            if not available:
                raise QueueFull("application runtime queue is full")
            created = False
            try:
                durable_input = (
                    self._project_input(initial_input)
                    if initial_input is not None
                    else None
                )
                state, created = self._store.accept_start(
                    instance_id,
                    profile_id,
                    target_id,
                    durable_input,
                    client_request_id,
                    digest,
                )
                if not created:
                    return state
                self._queue.put_nowait(instance_id)
                return state
            finally:
                # A newly admitted item retains its slot until it becomes
                # terminal/paused.  An idempotent replay did not consume one.
                if not created:
                    self._release_slot(instance_id)

    def command(
        self, instance_id: str, command: Command, client_request_id: str
    ) -> ApplicationInstance:
        self._required(instance_id, "instance_id")
        self._required(client_request_id, "client_request_id")
        tag, raw_content = self._command(command)
        digest = request_digest(
            "command",
            {"instance_id": instance_id, "tag": tag, "content": raw_content},
        )
        durable_content = (
            self._project_input(raw_content) if raw_content is not None else None
        )
        resume_slot = False
        timer: threading.Timer | None = None
        continue_reconciliation = False
        with self._dispatch_lock:
            self._ensure_open()
            before = self._store.inspect(instance_id)
            if (
                tag == "Resume"
                and before.status == "paused"
                and before.wake_at is None
            ):
                available, resume_slot = self._acquire_slot(instance_id)
                if not available:
                    raise QueueFull("application runtime queue is full")
            try:
                state, created = self._store.accept_command(
                    instance_id,
                    tag,
                    durable_content,
                    client_request_id,
                    digest,
                )
                if created and tag == "Stop":
                    self._store.settle_stop_if_idle(instance_id)
                    state = self._store.inspect(instance_id)
                if created:
                    timer = self._pop_timer(instance_id)
                    continue_reconciliation = (
                        self._store.unfinished_intent(instance_id) is not None
                    )
            except Exception:
                if resume_slot:
                    self._release_slot(instance_id)
                raise

        if not created:
            if resume_slot:
                self._release_slot(instance_id)
            return state

        if timer is not None:
            timer.cancel()
            if continue_reconciliation or tag in {"Input", "Resume"}:
                self._queue.put_nowait(instance_id)
            else:
                self._release_slot(instance_id)
        elif resume_slot:
            self._queue.put_nowait(instance_id)
        return self._store.inspect(instance_id)

    def inspect(self, instance_id: str) -> ApplicationInstance:
        return self._store.inspect(instance_id)

    def list(self, limit: int = 100) -> list[ApplicationInstance]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return self._store.list(limit)

    def shutdown(self, timeout: float | None = 5.0) -> None:
        self._closed.set()
        with self._timer_lock:
            timers = [entry[2] for entry in self._timers.values()]
            self._timers.clear()
        for timer in timers:
            timer.cancel()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            # The coordinator polls the closed flag and drains already-admitted
            # items without starting new external work.
            pass
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("application runtime shutdown timed out")
        # Timer callbacks and commands share this fence. Once acquired after
        # closing, every callback that could touch SQLite has finished; any
        # callback still waiting will observe its cleared token and return.
        with self._dispatch_lock:
            pass
        self._process_database_lock.close()

    def _coordinate(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._closed.is_set():
                    return
                continue
            if item is _STOP:
                self._queue.task_done()
                return
            instance_id = str(item)
            delay: float | None = None
            try:
                if not self._closed.is_set():
                    delay = self._run(instance_id)
            except Exception:
                # _run persists every expected stage failure.  This final guard
                # is only for an unexpected coordinator bug and must still be
                # visible instead of being silently swallowed.
                try:
                    self._store.fail_cycle(
                        instance_id,
                        None,
                        "coordinator_failed",
                        "coordinator",
                    )
                except Exception:
                    # If even the durable failure write is unavailable, stop
                    # admitting work.  Continuing would recreate the original
                    # silent-running failure mode with no trustworthy ledger.
                    self._closed.set()
            finally:
                self._queue.task_done()

            if self._closed.is_set() or delay is None:
                self._release_slot(instance_id)
            elif delay <= 0:
                self._queue.put_nowait(instance_id)
            else:
                self._schedule_timer(instance_id, delay)

    def _run(self, instance_id: str) -> float | None:
        token = str(uuid.uuid4())
        if not self._store.claim(instance_id, token):
            return None
        cycle: int | None = None
        intent_id: str | None = None
        stage = "claim"
        try:
            unfinished = self._store.unfinished_intent(instance_id)
            if unfinished is not None:
                return self._reconcile(instance_id, unfinished)

            cycle_info = self._store.begin_cycle(instance_id, token)
            if cycle_info is None:
                return self._next_delay(instance_id)
            cycle, revision = cycle_info
            state = self._store.inspect(instance_id)
            if state.status != "running" or state.revision != revision:
                self._store.settle_fence(instance_id, cycle)
                return self._next_delay(instance_id)

            stage = "observation_before"
            instance = self._store.inspect(instance_id)
            before = self._observation_port.observe(instance)
            self._require_observation(before, "observation_port.observe")
            durable_before = self._project_observation(before)
            self._store.record_observation(
                instance_id,
                cycle,
                "before",
                durable_before,
            )
            if not before.fresh:
                outcome = Outcome(
                    "unconfirmed",
                    "before observation was not fresh",
                    terminal=True,
                )
                durable_outcome = self._project_outcome(outcome)
                self._store.finish_cycle(
                    instance_id,
                    cycle,
                    durable_outcome,
                    durable_before.evidence_id,
                    degraded=True,
                    detail=durable_outcome.evidence,
                    status="failed",
                    error_code="before_observation_not_fresh",
                    expected_revision=revision,
                    replan_on_revision_change=True,
                )
                return self._next_delay(instance_id)

            stage = "policy"
            decision = self._policy.decide(
                PolicyContext(
                    instance,
                    before,
                    self._store.active_memory(self._memory_scope),
                    is_cancelled=lambda: self._policy_cancelled(
                        instance_id, revision
                    ),
                )
            )
            if not isinstance(decision, Decision):
                raise TypeError("policy.decide must return Decision")
            if decision.complete:
                self._store.complete_without_action(
                    instance_id, cycle, self._project_detail(decision.detail)
                )
                return None
            if decision.wait_seconds is not None:
                wake_at = self._store.schedule_wait(
                    instance_id, cycle, revision, float(decision.wait_seconds)
                )
                if wake_at is None:
                    self._store.settle_fence(instance_id, cycle)
                    return self._next_delay(instance_id)
                return float(decision.wait_seconds)

            assert decision.intent is not None
            intent_id = str(uuid.uuid4())
            stage = "intent_persist"
            if not self._store.persist_intent(
                intent_id,
                instance_id,
                cycle,
                revision,
                decision.intent,
                self._project_intent(decision.intent),
            ):
                self._store.settle_fence(instance_id, cycle)
                return self._next_delay(instance_id)

            # The lock is the physical-dispatch commit fence.  A command can be
            # accepted before it or after the owner returns, never in between.
            with self._dispatch_lock:
                stage = "reserve_preflight"
                if not self._store.pre_dispatch(instance_id, intent_id, revision):
                    self._store.settle_fence(instance_id, cycle)
                    return self._next_delay(instance_id)
                stage = "owner_reserve"
                reservation_id = self._execution_owner.reserve(
                    self._store.inspect(instance_id), decision.intent
                )
                if not self._store.mark_reserved(
                    instance_id, intent_id, reservation_id
                ):
                    raise RuntimeError("intent reservation persistence failed")
                if not self._store.mark_dispatching(
                    instance_id, intent_id, revision
                ):
                    self._store.settle_fence(instance_id, cycle)
                    return self._next_delay(instance_id)
                stage = "owner_dispatch"
                receipt = self._execution_owner.dispatch(
                    reservation_id,
                    self._store.inspect(instance_id),
                    decision.intent,
                )
                if not isinstance(receipt, ExecutionReceipt):
                    raise TypeError(
                        "execution_owner.dispatch must return ExecutionReceipt"
                    )
                if not self._store.mark_dispatched(
                    instance_id,
                    intent_id,
                    receipt,
                    self._project_receipt(receipt),
                ):
                    raise RuntimeError("dispatch receipt persistence failed")

            stage = "observation_after"
            after_observer = getattr(self._observation_port, "observe_after", None)
            if callable(after_observer):
                after = after_observer(
                    self._store.inspect(instance_id), decision.intent, receipt
                )
            else:
                after = self._observation_port.observe(
                    self._store.inspect(instance_id)
                )
            self._require_observation(after, "observation_port.observe_after")
            durable_after = self._project_observation(after)
            self._store.record_observation(
                instance_id,
                cycle,
                "after",
                durable_after,
            )
            if not after.fresh or after.evidence_id == before.evidence_id:
                self._store.defer_reconciliation(
                    instance_id,
                    "after_observation_not_fresh",
                    "observation_after",
                )
                unfinished = self._store.unfinished_intent(instance_id)
                if unfinished is None:
                    self._store.fail_cycle(
                        instance_id,
                        cycle,
                        "after_observation_not_fresh",
                        "observation_after",
                    )
                    return None
                return self._reconcile(instance_id, unfinished)

            stage = "verification"
            outcome = self._verifier.verify(
                VerificationContext(
                    self._store.inspect(instance_id),
                    decision.intent,
                    before,
                    after,
                    receipt,
                )
            )
            if not isinstance(outcome, Outcome):
                raise TypeError("verifier.verify must return Outcome")
            if not receipt.accepted and outcome.confirmed_success:
                outcome = Outcome(
                    "unconfirmed",
                    "dispatch was not accepted",
                    decision.intent.hard_risk,
                    terminal=True,
                )

            effective_hard = outcome.hard_risk or (
                decision.intent.hard_risk and not outcome.confirmed_success
            )
            if effective_hard != outcome.hard_risk:
                outcome = Outcome(
                    outcome.status,
                    outcome.evidence,
                    effective_hard,
                    outcome.terminal,
                )

            memory_degraded = False
            if outcome.confirmed_success:
                stage = "memory"
                memory_degraded = self._promote(
                    decision.memory_candidate, outcome, instance_id
                )

            durable_outcome = self._project_outcome(outcome)
            hard = outcome.hard_risk
            if outcome.confirmed_success:
                desired_status = "completed" if outcome.terminal else "running"
                error_code = None
            elif outcome.terminal or hard:
                desired_status = "failed"
                error_code = (
                    "hard_risk_outcome" if hard else f"{outcome.status}_outcome"
                )
            else:
                desired_status = "running"
                error_code = None
            resolved_status = self._store.finish_cycle(
                instance_id,
                cycle,
                durable_outcome,
                durable_after.evidence_id,
                degraded=memory_degraded or not outcome.confirmed_success,
                detail=self._project_detail(outcome.evidence),
                status=desired_status,
                error_code=error_code,
                expected_revision=revision,
                replan_on_revision_change=(
                    outcome.confirmed_success
                    or (
                        outcome.status == "confirmed_failure"
                        and not outcome.terminal
                        and not hard
                        and not receipt.accepted
                    )
                ),
                owner_settlement_priority=(desired_status == "failed"),
            )
            return 0.0 if resolved_status in {"running", "queued"} else None
        except RetryableApplicationError as exc:
            return self._persist_retryable_stage(
                instance_id,
                cycle,
                intent_id,
                stage,
                exc.wait_seconds,
            )
        except Exception as exc:
            return self._persist_stage_failure(
                instance_id,
                cycle,
                intent_id,
                stage,
                exc,
            )
        finally:
            self._store.release(instance_id, token)

    def _reconcile(
        self, instance_id: str, runtime_intent: RuntimeIntent
    ) -> float | None:
        reconcile = getattr(self._execution_owner, "reconcile", None)
        try:
            if not callable(reconcile):
                result = ExecutionReconciliation(
                    Outcome(
                        "uncertain",
                        "execution owner reconciliation unavailable",
                        runtime_intent.intent.hard_risk,
                        terminal=True,
                    ),
                    f"recovery-unavailable:{runtime_intent.intent_id}",
                )
            else:
                result = reconcile(self._store.inspect(instance_id), runtime_intent)
            if not isinstance(result, ExecutionReconciliation):
                raise TypeError(
                    "execution_owner.reconcile must return ExecutionReconciliation"
                )
            outcome = result.outcome
            if result.retry_after_seconds is not None:
                wake_at = self._store.schedule_reconciliation_wait(
                    instance_id,
                    runtime_intent.intent_id,
                    float(result.retry_after_seconds),
                )
                if wake_at is None:
                    return self._next_delay(instance_id)
                return float(result.retry_after_seconds)
            receipt = result.receipt
            durable_receipt = (
                self._project_receipt(receipt) if receipt is not None else None
            )
            hard = outcome.hard_risk or runtime_intent.intent.hard_risk
            if hard != outcome.hard_risk:
                outcome = Outcome(
                    outcome.status,
                    outcome.evidence,
                    hard,
                    outcome.terminal,
                )
            durable_outcome = self._project_outcome(outcome)
            self._store.mark_reconciled(
                instance_id, runtime_intent.intent_id, durable_receipt
            )
            definite_not_dispatched = (
                outcome.status == "confirmed_failure"
                and not outcome.terminal
                and not hard
                and receipt is not None
                and not receipt.accepted
            )
            if outcome.confirmed_success:
                status = "completed" if outcome.terminal else "running"
                error_code = None
            elif definite_not_dispatched:
                # The owner proved that the old action never crossed its
                # physical boundary. Close that cycle and let a long-lived
                # instance observe current state before planning anew.
                status = "running"
                error_code = None
            else:
                # An unfinished physical intent is never re-planned.  Even an
                # unconfirmed non-hard result is terminal for that instance.
                status = "failed"
                error_code = (
                    "recovery_hard_risk" if hard else "recovery_no_replay"
                )
            resolved = self._store.finish_cycle(
                instance_id,
                runtime_intent.cycle,
                durable_outcome,
                result.evidence_id,
                degraded=True,
                detail=self._project_detail(outcome.evidence),
                status=status,
                error_code=error_code,
                expected_revision=runtime_intent.revision,
                replan_on_revision_change=(
                    outcome.confirmed_success or definite_not_dispatched
                ),
                owner_settlement_priority=(status == "failed"),
            )
            return 0.0 if resolved in {"running", "queued"} else None
        except Exception:
            # This fallback is already a constant, non-sensitive persistence
            # representation.  Do not send it through the failing projection
            # again and do not repeat owner inspection: reconciliation is an
            # inspect-only convergence boundary, not a retry loop.
            fallback = Outcome(
                "uncertain",
                "execution owner reconciliation failed",
                runtime_intent.intent.hard_risk,
                terminal=True,
            )
            try:
                self._store.mark_reconciled(
                    instance_id, runtime_intent.intent_id, None
                )
                self._store.finish_cycle(
                    instance_id,
                    runtime_intent.cycle,
                    fallback,
                    f"recovery-failed:{runtime_intent.intent_id}",
                    degraded=True,
                    detail=fallback.evidence,
                    status="failed",
                    error_code="reconciliation_failed",
                    expected_revision=runtime_intent.revision,
                    owner_settlement_priority=True,
                )
            except Exception:
                # With no trustworthy durable convergence record, stop the
                # coordinator.  A later process will recover the still-open
                # intent and inspect it again; it will never dispatch it.
                self._closed.set()
            return None

    def _persist_retryable_stage(
        self,
        instance_id: str,
        cycle: int | None,
        intent_id: str | None,
        stage: str,
        wait_seconds: float,
    ) -> float | None:
        unfinished = self._store.unfinished_intent(instance_id)
        if intent_id is not None or unfinished is not None:
            # Once an intent exists, only the execution owner may establish
            # whether its physical action happened. Never turn a generic retry
            # request into a second dispatch.
            if unfinished is not None:
                self._store.defer_reconciliation(
                    instance_id, f"{stage}_retryable", stage
                )
                return self._reconcile(instance_id, unfinished)
            self._store.fail_cycle(
                instance_id, cycle, f"{stage}_retryable", stage
            )
            return None
        if cycle is None:
            self._store.fail_cycle(
                instance_id, None, f"{stage}_retryable", stage
            )
            return None
        wake_at = self._store.schedule_retry(
            instance_id,
            cycle,
            float(wait_seconds),
            error_code=f"{stage}_retryable",
            stage=stage,
        )
        if wake_at is None:
            self._store.settle_fence(instance_id, cycle)
            return self._next_delay(instance_id)
        return float(wait_seconds)

    def _persist_stage_failure(
        self,
        instance_id: str,
        cycle: int | None,
        intent_id: str | None,
        stage: str,
        exc: Exception,
    ) -> float | None:
        del exc  # Never persist or expose exception text from model/device data.
        error_code = f"{stage}_failed"
        unfinished = self._store.unfinished_intent(instance_id)
        if intent_id is not None and unfinished is not None:
            self._store.defer_reconciliation(instance_id, error_code, stage)
            return self._reconcile(instance_id, unfinished)
        self._store.fail_cycle(instance_id, cycle, error_code, stage)
        return None

    def _promote(
        self,
        candidate: MemoryCandidate | None,
        outcome: Outcome,
        instance_id: str,
    ) -> bool:
        if candidate is None:
            return False
        candidate_id = str(uuid.uuid4())
        if candidate.scope != self._memory_scope:
            self._store.record_runtime_event(
                instance_id,
                "memory_scope_rejected",
                {"candidate_id": candidate_id},
            )
            return True
        durable_candidate = self._project_memory_candidate(candidate)
        if candidate.reward_required:
            self._store.promote_memory(
                instance_id,
                candidate_id,
                candidate,
                outcome,
                None,
                durable_candidate,
            )
            return False
        promoted: Mapping[str, Any] | None = None
        try:
            if self._memory_gate is not None:
                decision = self._memory_gate.promote(candidate, outcome)
                if decision is True:
                    promoted = dict(candidate.content)
                elif decision:
                    promoted = dict(decision)
            durable_promoted = (
                self._project_memory_content(candidate.scope, promoted)
                if promoted is not None
                else None
            )
            self._store.promote_memory(
                instance_id,
                candidate_id,
                candidate,
                outcome,
                durable_promoted,
                durable_candidate,
            )
            return False
        except Exception:
            # Delivery truth remains valid even if learning projection/gating
            # fails.  Persist a safe event and finish the action as degraded.
            self._store.record_runtime_event(
                instance_id,
                "memory_gate_failed",
                {"candidate_id": candidate_id},
            )
            return True

    def _schedule_timer(self, instance_id: str, delay: float) -> None:
        timer_token = str(uuid.uuid4())
        with self._dispatch_lock:
            try:
                state = self._store.inspect(instance_id)
            except Exception:
                self._settle_timer_failure(instance_id, "wait_schedule_failed")
                return
            reconciliation_wait = (
                state.status in {"paused", "stopping"}
                and state.wake_at is not None
            )
            if state.status != "waiting" and not reconciliation_wait:
                if state.status in {"queued", "running"}:
                    self._queue.put_nowait(instance_id)
                else:
                    if state.status == "stopping":
                        self._store.settle_stop_if_idle(instance_id)
                    self._release_slot(instance_id)
                return
            timer = threading.Timer(
                max(0.0, float(delay)),
                self._timer_elapsed,
                args=(instance_id, timer_token, state.wake_at),
            )
            timer.daemon = True
            with self._timer_lock:
                if self._closed.is_set():
                    self._release_slot(instance_id)
                    return
                prior = self._timers.get(instance_id)
                if prior is not None:
                    prior[2].cancel()
                self._timers[instance_id] = (
                    timer_token,
                    state.wake_at,
                    timer,
                )
            try:
                timer.start()
            except Exception:
                with self._timer_lock:
                    current = self._timers.get(instance_id)
                    if current is not None and current[0] == timer_token:
                        self._timers.pop(instance_id, None)
                self._settle_timer_failure(instance_id, "wait_schedule_failed")

    def _timer_elapsed(
        self,
        instance_id: str,
        timer_token: str,
        wake_at: str | None,
    ) -> None:
        with self._dispatch_lock:
            with self._timer_lock:
                current = self._timers.get(instance_id)
                if current is None or current[0] != timer_token:
                    return
                self._timers.pop(instance_id, None)
            if self._closed.is_set():
                self._release_slot(instance_id)
                return
            try:
                if self._store.wake_scheduled(instance_id, wake_at):
                    self._queue.put_nowait(instance_id)
                    return
                state = self._store.inspect(instance_id)
                if state.status in {"queued", "running"}:
                    self._queue.put_nowait(instance_id)
                else:
                    self._release_slot(instance_id)
            except Exception:
                self._settle_timer_failure(instance_id, "wait_wakeup_failed")

    def _settle_timer_failure(self, instance_id: str, error_code: str) -> None:
        try:
            self._store.fail_cycle(instance_id, None, error_code, "timer")
        except Exception:
            self._closed.set()
        finally:
            self._release_slot(instance_id)

    def _acquire_slot(self, instance_id: str) -> tuple[bool, bool]:
        """Return ``(available, newly_acquired)`` for one durable instance."""
        with self._slot_lock:
            if instance_id in self._slot_instances:
                return True, False
            if not self._slots.acquire(blocking=False):
                return False, False
            self._slot_instances.add(instance_id)
            return True, True

    def _release_slot(self, instance_id: str) -> None:
        with self._slot_lock:
            if instance_id not in self._slot_instances:
                return
            self._slot_instances.remove(instance_id)
            self._slots.release()

    def _pop_timer(self, instance_id: str) -> threading.Timer | None:
        with self._timer_lock:
            current = self._timers.pop(instance_id, None)
            return current[2] if current is not None else None

    def _next_delay(self, instance_id: str) -> float | None:
        state = self._store.inspect(instance_id)
        return 0.0 if state.status in {"queued", "running"} else None

    def _project_input(self, value: str) -> str:
        projected = self._projection.project_input(value)
        if not isinstance(projected, str) or not projected:
            raise TypeError("persistence projection must return non-blank input")
        return projected

    def _policy_cancelled(self, instance_id: str, expected_revision: int) -> bool:
        if self._closed.is_set():
            return True
        try:
            state = self._store.inspect(instance_id)
        except Exception:
            # A dependency doing cooperative cancellation must never continue
            # expensive generation when the durable control fence is unreadable.
            return True
        return state.status != "running" or state.revision != expected_revision

    def _project_observation(self, value: Observation) -> Observation:
        projected = self._projection.project_observation(value)
        if not isinstance(projected, Observation):
            raise TypeError("persistence projection must return Observation")
        if projected.fresh != value.fresh:
            raise ValueError("persistence projection cannot change observation freshness")
        return projected

    def _project_intent(self, value: Intent) -> Intent:
        projected = self._projection.project_intent(value)
        if not isinstance(projected, Intent):
            raise TypeError("persistence projection must return Intent")
        if (projected.name, projected.hard_risk) != (value.name, value.hard_risk):
            raise ValueError("persistence projection cannot change intent control facts")
        return projected

    def _project_receipt(self, value: ExecutionReceipt) -> ExecutionReceipt:
        projected = self._projection.project_receipt(value)
        if not isinstance(projected, ExecutionReceipt):
            raise TypeError("persistence projection must return ExecutionReceipt")
        if projected.accepted != value.accepted:
            raise ValueError("persistence projection cannot change receipt acceptance")
        return projected

    def _project_outcome(self, value: Outcome) -> Outcome:
        projected = self._projection.project_outcome(value)
        if not isinstance(projected, Outcome):
            raise TypeError("persistence projection must return Outcome")
        if (
            projected.status,
            projected.hard_risk,
            projected.terminal,
        ) != (value.status, value.hard_risk, value.terminal):
            raise ValueError("persistence projection cannot change outcome control facts")
        return projected

    def _project_memory_candidate(
        self, value: MemoryCandidate
    ) -> MemoryCandidate:
        projected = self._projection.project_memory_candidate(value)
        if not isinstance(projected, MemoryCandidate):
            raise TypeError("persistence projection must return MemoryCandidate")
        if (projected.scope, projected.reward_required) != (
            value.scope,
            value.reward_required,
        ):
            raise ValueError("persistence projection cannot change memory control facts")
        return projected

    def _project_memory_content(
        self, scope: str, value: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        projected = self._projection.project_memory_content(scope, value)
        if not isinstance(projected, Mapping):
            raise TypeError("persistence projection must return memory mapping")
        return projected

    def _project_detail(self, value: str) -> str:
        projected = self._projection.project_detail(value)
        if not isinstance(projected, str):
            raise TypeError("persistence projection must return detail string")
        return projected

    @staticmethod
    def _require_observation(value: Any, source: str) -> None:
        if not isinstance(value, Observation):
            raise TypeError(f"{source} must return Observation")

    @staticmethod
    def _required(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-blank")

    @staticmethod
    def _command(command: Command) -> tuple[str, str | None]:
        if isinstance(command, Input):
            ApplicationRuntime._required(command.content, "command.content")
            return "Input", command.content
        if isinstance(command, (Pause, Resume, Stop)):
            return command.tag, None
        raise TypeError("command must be Input, Pause, Resume, or Stop")

    def _ensure_open(self) -> None:
        if self._closed.is_set():
            raise RuntimeClosed("application runtime is shut down")
