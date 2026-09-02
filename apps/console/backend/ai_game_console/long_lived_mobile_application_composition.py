from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from .application_runtime import (
    ApplicationRuntime,
    Decision,
    ExecutionReconciliation,
    ExecutionReceipt,
    ExternalOwnerEvent,
    Intent,
    Observation,
    Outcome,
    Resume,
    RetryableApplicationError,
    RuntimeClosed,
)
from .application_runtime.store import _SQLiteApplicationStore, request_digest
from .kernel_canary import KernelApplicationCycleResult, KernelCanaryCoordinator
from .runtime_adapters.android import AndroidObservationProvider


_INTERNAL_PROFILE_ID = "private-long-lived-mobile-v1"
APPLICATION_DATABASE_FILENAME = "long-lived-mobile-application-runtime.db"
_CYCLE_INTENT = "runtime_kernel.bounded_application_cycle.v1"
_ACTIVATION_WAIT_SECONDS = 1.0
_WAIT_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class LongLivedMobileApplicationComposition:
    runtime: Any
    database_path: Path


@dataclass(frozen=True, slots=True)
class ActivitySliceDispatchRequest:
    """Narrow transport facts for one trigger-owned ActivitySlice.

    The ActivitySlice domain and its state machine remain owned by
    ``agent_runtime.activity_slice``.  This DTO carries only the correlation
    facts the normal mobile composition must lend to an injected adapter.
    """

    idempotency_key: str
    application_instance_id: str
    application_cycle: int
    goal_id: str
    target_id: str
    objective: str
    trigger_id: str
    trigger_kind: str
    initial_application_context: str


@dataclass(frozen=True, slots=True)
class ActivitySliceStepDispatch:
    """Stable identity of one authoritative ActivityStep requesting ACT."""

    step_id: str
    ordinal: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ActivitySliceDispatchResult:
    """ApplicationRuntime-facing settlement of one persisted ActivitySlice."""

    receipt_id: str
    slice_id: str
    status: str
    evidence: str
    accepted: bool
    physical_action_sent: bool
    before_evidence_id: str | None
    after_evidence_id: str | None
    step_ids: tuple[str, ...]
    kernel_task_ids: tuple[str, ...]


class ActivitySliceRunnerPort(Protocol):
    """Adapter implemented around the authoritative ActivitySliceSupervisor.

    ``dispatch_activity_slice`` may call ``execute_step`` several times, but
    only after the corresponding ActivityStep has a durable idempotency key.
    Replays must return the persisted result without invoking the callback.
    """

    def dispatch_activity_slice(
        self,
        request: ActivitySliceDispatchRequest,
        execute_step: Callable[[ActivitySliceStepDispatch], KernelApplicationCycleResult],
    ) -> ActivitySliceDispatchResult: ...

    def inspect_activity_slice(self, receipt_id: str) -> ActivitySliceDispatchResult: ...

    def reconcile_activity_slice(
        self, idempotency_key: str
    ) -> ActivitySliceDispatchResult | None: ...

    def active_activity_step(
        self, idempotency_key: str
    ) -> ActivitySliceStepDispatch | None: ...


@dataclass(frozen=True, slots=True)
class DurableChildAuthorization:
    """Read-only correlation facts for one in-flight Kernel child task.

    This deliberately exposes only the durable identifiers needed by the
    DeviceBody owner resolver.  Callers must still authenticate the Kernel
    Task's accepted-cycle event; this projection is not a general-purpose
    authorization grant for every task sharing a GoalRun.
    """

    instance_id: str
    goal_id: str
    target_id: str
    activated_at: str
    application_cycle: int
    intent_phase: str
    reservation_id: str


@dataclass(frozen=True, slots=True)
class _Binding:
    instance_id: str
    goal_id: str
    target_id: str
    initial_application_context: str
    activated_at: str | None

    @property
    def application_id(self) -> str:
        """Legacy read alias; no R4 code may treat it as a foreground fence."""

        return self.initial_application_context


class _BindingStore:
    """Private correlation ledger for the prepare -> bind -> activate saga."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        with self._connection(write=True) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS long_lived_mobile_bindings(
                  instance_id TEXT PRIMARY KEY,
                  goal_id TEXT NOT NULL UNIQUE,
                  target_id TEXT NOT NULL,
                  application_id TEXT NOT NULL,
                  activated_at TEXT,
                  created_at TEXT NOT NULL
                )
                """
            )

    def put(
        self,
        *,
        instance_id: str,
        goal_id: str,
        target_id: str,
        initial_application_context: str,
    ) -> _Binding:
        now = _now()
        with self._lock, self._connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM long_lived_mobile_bindings WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO long_lived_mobile_bindings VALUES(?,?,?,?,NULL,?)",
                    (instance_id, goal_id, target_id, initial_application_context, now),
                )
            elif (
                str(row["goal_id"]),
                str(row["target_id"]),
                str(row["application_id"]),
            ) != (goal_id, target_id, initial_application_context):
                raise ValueError("long-lived mobile binding correlation conflict")
        return self.get(instance_id)

    def activate(self, instance_id: str) -> _Binding:
        with self._lock, self._connection(write=True) as db:
            changed = db.execute(
                "UPDATE long_lived_mobile_bindings "
                "SET activated_at=COALESCE(activated_at,?) WHERE instance_id=?",
                (_now(), instance_id),
            ).rowcount
            if changed != 1:
                raise KeyError(instance_id)
        return self.get(instance_id)

    def get(self, instance_id: str) -> _Binding:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM long_lived_mobile_bindings WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
        if row is None:
            raise KeyError(instance_id)
        return _Binding(
            str(row["instance_id"]),
            str(row["goal_id"]),
            str(row["target_id"]),
            str(row["application_id"]),
            str(row["activated_at"]) if row["activated_at"] else None,
        )

    def _connection(self, *, write: bool = False) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        if write:
            db.execute("PRAGMA journal_mode=WAL")
        return db


class LongLivedMobileApplicationRuntimeGateway:
    """Private ApplicationRuntime supervisor backed by bounded Kernel cycles.

    The hidden profile is deliberately absent from CapabilityBindingPlan and
    from the public application-profile catalog.  A prepared instance cannot
    dispatch until GoalService has durably bound it and called ``activate``.
    """

    def __init__(
        self,
        database_path: Path | str,
        *,
        kernel: KernelCanaryCoordinator,
        observation_provider: AndroidObservationProvider,
        activity_slice_runner: ActivitySliceRunnerPort | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self._archive = _SQLiteApplicationStore(self.database_path)
        self._bindings = _BindingStore(self.database_path)
        self._kernel = kernel
        self._observation_provider = observation_provider
        self._activity_slice_runner = activity_slice_runner
        self._runtime: ApplicationRuntime | None = None
        self._closed = False
        self._lock = threading.RLock()

    def startup(self) -> None:
        self._runtime_for_new_work()

    def prepare(
        self,
        *,
        client_request_id: str,
        goal_id: str,
        target_id: str,
        initial_application_context: str | None = None,
        application_id: str | None = None,
        initial_input: str,
    ) -> Any:
        # ``application_id`` is retained only for callers replaying an R3
        # request.  The old value describes the first observed application;
        # it is not an authorization to pin later foreground observations.
        if initial_application_context is None:
            initial_application_context = application_id
        elif application_id is not None and application_id != initial_application_context:
            raise ValueError("initial application context conflicts with legacy application_id")
        initial_application_context = str(initial_application_context or "").strip()
        if not initial_application_context:
            raise ValueError("initial application context must not be blank")
        state = self._runtime_for_new_work().start(
            _INTERNAL_PROFILE_ID,
            client_request_id,
            target_id=target_id,
            initial_input=initial_input,
        )
        self._bindings.put(
            instance_id=state.instance_id,
            goal_id=goal_id,
            target_id=target_id,
            initial_application_context=initial_application_context,
        )
        return self.inspect(state.instance_id)

    def activate(self, instance_id: str) -> Any:
        binding = self._bindings.get(instance_id)
        # GoalService calls activate only after the GoalRun binding is durable.
        # Persist that fact before waking the worker so the policy cannot
        # observe an activation command while the binding ledger is stale.
        self._bindings.activate(instance_id)
        state = self._runtime_for_new_work().command(
            instance_id,
            Resume(),
            f"long-lived-mobile:activate:{instance_id}",
        )
        if state.target_id != binding.target_id:
            raise ValueError("long-lived mobile activation target mismatch")
        return self.inspect(instance_id)

    def command(self, instance_id: str, command: Any, client_request_id: str) -> Any:
        return self._runtime_for_new_work().command(
            instance_id, command, client_request_id
        )

    def report_application_outcome(
        self,
        instance_id: str,
        *,
        event_id: str,
        event_type: str,
        evidence_refs: tuple[str, ...],
        attribution_scope: str,
        confidence: float,
    ) -> Any:
        """Attach one attributed application outcome to the bound GoalRun.

        ``ExternalOwnerEvent`` is the existing internal ApplicationRuntime
        envelope.  The generic mobile composition itself supplies the durable
        binding reference; this does not require or select a specialized
        external owner.
        """

        binding = self._bindings.get(instance_id)
        return self._runtime_for_new_work().report_owner_event(
            instance_id,
            ExternalOwnerEvent(
                # ApplicationRuntime fences every inbound event to the same
                # target already frozen on the instance.  Here that is the
                # authorized Android device, not an external-owner account.
                owner_binding_ref=binding.target_id,
                owner_event_id=event_id,
                event_type=event_type,
                evidence_refs=evidence_refs,
                attribution_scope=attribution_scope,
                confidence=confidence,
            ),
        )

    def inspect(self, instance_id: str) -> Any:
        return self._archive.inspect(instance_id)

    def list(self, limit: int = 100) -> list[Any]:
        return self._archive.list(limit)

    def binding(self, instance_id: str) -> _Binding:
        return self._bindings.get(instance_id)

    def durable_child_authorization(
        self, instance_id: str
    ) -> DurableChildAuthorization | None:
        """Return the one dispatching child correlation, if it is unambiguous.

        The DeviceBody bridge calls this during a Kernel dispatch.  The method
        is intentionally read-only and fail-closed: an unactivated binding,
        a terminal instance, or more/less than one unfinished intent has no
        child authorization to lend to a Kernel Task.
        """

        binding = self._bindings.get(instance_id)
        state = self._archive.inspect(instance_id)
        if binding.activated_at is None or state.terminal:
            return None
        unfinished = tuple(
            intent
            for intent in state.intents
            if intent.finalized_at is None
            and intent.phase in {
                "open",
                "reserved",
                "dispatching",
                "dispatched",
                "reconciled",
            }
        )
        if len(unfinished) != 1:
            return None
        intent = unfinished[0]
        if (
            intent.phase != "dispatching"
            or not isinstance(intent.reservation_id, str)
            or not intent.reservation_id.strip()
        ):
            return None
        reservation_id = intent.reservation_id
        if self._activity_slice_runner is not None:
            active_step = self._activity_slice_runner.active_activity_step(
                reservation_id
            )
            if active_step is None:
                return None
            reservation_id = _activity_slice_cycle_key(
                reservation_id, active_step
            )
        return DurableChildAuthorization(
            instance_id=binding.instance_id,
            goal_id=binding.goal_id,
            target_id=binding.target_id,
            activated_at=binding.activated_at,
            application_cycle=int(intent.cycle),
            intent_phase=intent.phase,
            reservation_id=reservation_id,
        )

    def fence_stop_before_start(self, instance_id: str, request_id: str) -> Any:
        """Persist the Goal stop fence before recovery workers can run."""

        state = self.inspect(instance_id)
        if state.status in {"stopped", "completed", "failed"}:
            return state
        digest = request_digest(
            "command",
            {"instance_id": instance_id, "tag": "Stop", "content": None},
        )
        self._archive.accept_command(
            instance_id,
            "Stop",
            None,
            request_id,
            digest,
        )
        self._archive.settle_stop_if_idle(instance_id)
        return self.inspect(instance_id)

    def fence_pause_before_start(self, instance_id: str, request_id: str) -> Any:
        """Persist a non-terminal scheduler fence before recovery workers run.

        ``ApplicationRuntime.recover`` deliberately leaves a paused instance
        out of the planning queue (while still reconciling an already
        dispatched intent).  Writing the real ApplicationRuntime ``Pause``
        command through its archive therefore closes the launcher window in
        which an R2-era, non-selected binding could otherwise re-enter the
        FIFO before the R3 AttentionScheduler has restored its selection.

        The request id and digest use the same durable idempotency contract as
        the live ``command`` API.  A replay observes the original accepted
        command; it does not manufacture a second pause or a projected state.
        """

        state = self.inspect(instance_id)
        if state.status in {"paused", "stopped", "completed", "failed"}:
            return state
        digest = request_digest(
            "command",
            {"instance_id": instance_id, "tag": "Pause", "content": None},
        )
        self._archive.accept_command(
            instance_id,
            "Pause",
            None,
            request_id,
            digest,
        )
        return self.inspect(instance_id)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            runtime = self._runtime
            self._runtime = None
        if runtime is not None:
            runtime.shutdown()

    def _runtime_for_new_work(self) -> ApplicationRuntime:
        with self._lock:
            if self._closed:
                raise RuntimeClosed("long-lived mobile runtime is closed")
            if self._runtime is None:
                self._runtime = ApplicationRuntime(
                    self.database_path,
                    profile=_INTERNAL_PROFILE_ID,
                    memory_scope=_INTERNAL_PROFILE_ID,
                    observation_port=_ObservationPort(
                        self._bindings,
                        self._observation_provider,
                        self._kernel,
                        self._activity_slice_runner,
                    ),
                    policy=_Policy(self._bindings),
                    execution_owner=_ExecutionOwner(
                        self._bindings,
                        self._kernel,
                        self._observation_provider,
                        self._activity_slice_runner,
                    ),
                    verifier=_Verifier(self._kernel, self._activity_slice_runner),
                )
            return self._runtime


class _ObservationPort:
    def __init__(
        self,
        bindings: _BindingStore,
        provider: AndroidObservationProvider,
        kernel: KernelCanaryCoordinator,
        activity_slice_runner: ActivitySliceRunnerPort | None = None,
    ) -> None:
        self._bindings = bindings
        self._provider = provider
        self._kernel = kernel
        self._activity_slice_runner = activity_slice_runner

    def observe(self, instance: Any) -> Observation:
        try:
            binding = self._bindings.get(instance.instance_id)
            state = self._provider.read_device_state(binding.target_id)
        except (KeyError, OSError, RuntimeError, ValueError):
            raise RetryableApplicationError(2.0) from None
        foreground = str(state.foreground_app or "").strip()
        material = "\0".join(
            (
                instance.instance_id,
                binding.target_id,
                binding.initial_application_context,
                foreground,
                str(state.captured_at),
                str(instance.revision),
                str(len(instance.intents)),
            )
        )
        return Observation(
            "android-application-state:v1:"
            + hashlib.sha256(material.encode("utf-8")).hexdigest(),
            (
                "已从授权 Android 目标读取当前前台应用状态。"
            ),
            fresh=True,
            data={
                "target_id": binding.target_id,
                # R4 compatibility projection: this is the app visible when
                # the old long-lived binding was created.  Foreground is a
                # per-observation fact and intentionally may differ.
                "initial_application_context": binding.initial_application_context,
                "foreground_app": foreground or None,
                "connection_state": state.connection_state.value,
                "orientation": state.orientation.value,
                "screen_size": list(state.screen_size),
                "captured_at": state.captured_at,
            },
        )

    def observe_after(
        self, instance: Any, intent: Intent, receipt: ExecutionReceipt
    ) -> Observation:
        del intent
        if receipt.receipt_id:
            if self._activity_slice_runner is not None:
                result = self._activity_slice_runner.inspect_activity_slice(
                    receipt.receipt_id
                )
                if result.after_evidence_id:
                    return Observation(
                        result.after_evidence_id,
                        "ActivitySlice 已结算全部连续 Step，并保留最后一次新鲜动作后证据。",
                        fresh=True,
                        data=_activity_slice_result_data(result),
                    )
                if result.accepted and result.physical_action_sent:
                    raise RuntimeError(
                        "accepted ActivitySlice has no durable after evidence"
                    )
                return self.observe(instance)
            result = self._kernel.inspect_application_cycle(receipt.receipt_id)
            if result.after_evidence_id:
                return Observation(
                    result.after_evidence_id,
                    "RuntimeKernel 已持久化动作后的新鲜目标应用画面。",
                    fresh=True,
                    data=_result_data(result),
                )
            if receipt.accepted:
                raise RuntimeError(
                    "accepted Kernel application cycle has no durable after evidence"
                )
        return self.observe(instance)


class _Policy:
    def __init__(self, bindings: _BindingStore) -> None:
        self._bindings = bindings

    def decide(self, context: Any) -> Decision:
        instance = context.instance
        try:
            binding = self._bindings.get(instance.instance_id)
        except KeyError:
            return Decision(
                wait_seconds=_WAIT_SECONDS,
                detail="等待 GoalRun 持久绑定完成后再激活设备周期。",
            )
        if binding.activated_at is None:
            return Decision(
                wait_seconds=_WAIT_SECONDS,
                detail="长期移动实例已准备，等待 GoalRun 绑定激活事件。",
            )

        activation_sequence = max(
            (
                event.sequence
                for event in instance.events
                if event.created_at < binding.activated_at
            ),
            default=0,
        )
        if not _activation_wait_completed(instance.events, activation_sequence):
            return Decision(
                wait_seconds=_ACTIVATION_WAIT_SECONDS,
                detail="GoalRun 已绑定；先完成一次持久、可中断的事件等待。",
            )
        triggers = _event_triggers(instance, activation_sequence)
        consumed = {
            str(item.intent.arguments.get("trigger_id") or "")
            for item in instance.intents
            if item.intent.name == _CYCLE_INTENT
        }
        trigger = next(
            (item for item in triggers if item[1] not in consumed),
            None,
        )
        # Activation itself is never a business event.  Each persisted normal
        # timer wake, GoalRun input, or authorized application event has a
        # stable trigger identity and can be consumed by at most one intent.
        # Retry/reconciliation timers do not create physical-work triggers.
        if trigger is not None:
            _sequence, trigger_id, trigger_kind, _content = trigger
            candidate = not any(
                item.candidate_notification for item in instance.intents
            )
            return Decision(
                intent=Intent(
                    _CYCLE_INTENT,
                    {
                        "goal_id": binding.goal_id,
                        "target_id": binding.target_id,
                        "initial_application_context": binding.initial_application_context,
                        "trigger_id": trigger_id,
                        "trigger_kind": trigger_kind,
                    },
                ),
                detail="委派一个且仅一个 RuntimeKernel 应用周期。",
                candidate_notification=candidate,
            )
        return Decision(
            wait_seconds=_WAIT_SECONDS,
            detail="本次有界周期已结算；等待目标事件、用户消息或定时唤醒。",
        )


class _ExecutionOwner:
    def __init__(
        self,
        bindings: _BindingStore,
        kernel: KernelCanaryCoordinator,
        observation_provider: AndroidObservationProvider,
        activity_slice_runner: ActivitySliceRunnerPort | None = None,
    ) -> None:
        self._bindings = bindings
        self._kernel = kernel
        self._observation_provider = observation_provider
        self._activity_slice_runner = activity_slice_runner

    def reserve(self, instance: Any, intent: Intent) -> str:
        runtime_intent = _latest_cycle_intent(instance)
        return _cycle_key(instance.instance_id, runtime_intent.intent_id)

    def dispatch(
        self, reservation_id: str, instance: Any, intent: Intent
    ) -> ExecutionReceipt:
        if intent.name != _CYCLE_INTENT:
            raise ValueError("unknown long-lived mobile intent")
        runtime_intent = _latest_cycle_intent(instance)
        if reservation_id != _cycle_key(instance.instance_id, runtime_intent.intent_id):
            raise ValueError("long-lived mobile reservation mismatch")
        binding = self._bindings.get(instance.instance_id)
        if instance.target_id != binding.target_id or binding.activated_at is None:
            raise ValueError("long-lived mobile binding is not active")
        trigger_id = str(intent.arguments.get("trigger_id") or "")
        trigger = next(
            (
                item
                for item in _event_triggers(instance, activation_sequence=0)
                if item[1] == trigger_id
            ),
            None,
        )
        if trigger is None:
            raise ValueError("long-lived mobile trigger is missing")
        cycle_goal = _application_cycle_goal(instance)
        if self._activity_slice_runner is not None:
            slice_result = self._activity_slice_runner.dispatch_activity_slice(
                ActivitySliceDispatchRequest(
                    idempotency_key=reservation_id,
                    application_instance_id=instance.instance_id,
                    application_cycle=runtime_intent.cycle,
                    goal_id=binding.goal_id,
                    target_id=binding.target_id,
                    objective=cycle_goal,
                    trigger_id=trigger_id,
                    trigger_kind=str(intent.arguments.get("trigger_kind") or ""),
                    initial_application_context=binding.initial_application_context,
                ),
                lambda step: self._execute_activity_step(
                    instance=instance,
                    runtime_intent=runtime_intent,
                    binding=binding,
                    reservation_id=reservation_id,
                    cycle_goal=cycle_goal,
                    step=step,
                ),
            )
            return ExecutionReceipt(
                slice_result.receipt_id,
                accepted=slice_result.accepted,
                detail=(
                    f"activity slice {slice_result.slice_id} {slice_result.status}; "
                    f"{len(slice_result.step_ids)} step(s) settled"
                ),
            )

        current_foreground = self._read_foreground(binding.target_id)
        result = self._kernel.execute_application_cycle(
            goal=cycle_goal,
            goal_id=binding.goal_id,
            application_instance_id=instance.instance_id,
            application_cycle=runtime_intent.cycle,
            cycle_key=reservation_id,
            target_id=binding.target_id,
            # The legacy bounded-cycle API still needs this positional
            # compatibility argument.  Supply the foreground just observed
            # for this dispatch, never the old binding's initial context.
            expected_application_id=current_foreground,
        )
        return ExecutionReceipt(
            result.task_id,
            accepted=result.physical_action_sent,
            detail=f"kernel application cycle {result.status}",
        )

    def _execute_activity_step(
        self,
        *,
        instance: Any,
        runtime_intent: Any,
        binding: _Binding,
        reservation_id: str,
        cycle_goal: str,
        step: ActivitySliceStepDispatch,
    ) -> KernelApplicationCycleResult:
        """Dispatch exactly one persisted ActivityStep through RuntimeKernel."""

        current_foreground = self._read_foreground(binding.target_id)
        cycle_key = _activity_slice_cycle_key(reservation_id, step)
        try:
            return self._kernel.execute_application_cycle(
                goal=(
                    f"{cycle_goal}\n\n"
                    f"ActivitySlice Step {step.ordinal}；一次只执行一个可验证动作。"
                ),
                goal_id=binding.goal_id,
                application_instance_id=instance.instance_id,
                # DeviceBody authorization deliberately keeps the outer durable
                # ApplicationRuntime cycle.  Step uniqueness lives in cycle_key.
                application_cycle=runtime_intent.cycle,
                cycle_key=cycle_key,
                target_id=binding.target_id,
                expected_application_id=current_foreground,
                stage_objective=(
                    f"推进当前 Goal 的下一步：{cycle_goal}；只执行一个可验证的原子动作，"
                    "动作后立即重新观察、验证，并交回 ActivitySlice。"
                ),
                completion_criteria=(
                    f"ActivitySlice Step {step.ordinal} 完成一个真实原子动作，"
                    "并形成动作后观察与验证",
                ),
            )
        except Exception:
            # The synchronous Kernel cycle may fail after its durable Task and
            # before any Action exists (for example, a malformed local-model
            # readiness tool call).  Let the Kernel reconcile that exact key
            # and settle the Slice as a no-action yield instead of leaving its
            # Step permanently EXECUTING.  Once any physical effect exists we
            # still fail closed: the original exception must surface so normal
            # restart recovery can preserve the no-replay boundary.
            reconciled = self._kernel.reconcile_application_cycle(cycle_key)
            if reconciled is not None and not reconciled.physical_action_sent:
                return reconciled
            raise

    def _read_foreground(self, target_id: str) -> str:
        try:
            current_foreground = str(
                self._observation_provider.read_device_state(
                    target_id
                ).foreground_app
                or ""
            ).strip()
        except (OSError, RuntimeError, ValueError) as error:
            raise RetryableApplicationError(2.0) from error
        if not current_foreground:
            raise RetryableApplicationError(2.0)
        return current_foreground

    def reconcile(self, instance: Any, runtime_intent: Any) -> ExecutionReconciliation:
        cycle_key = runtime_intent.reservation_id or _cycle_key(
            instance.instance_id, runtime_intent.intent_id
        )
        if self._activity_slice_runner is not None:
            slice_result = self._activity_slice_runner.reconcile_activity_slice(
                cycle_key
            )
            if slice_result is None:
                return ExecutionReconciliation(
                    Outcome(
                        "confirmed_failure",
                        "ActivitySlice ledger proves no dispatch was created",
                        terminal=False,
                    ),
                    f"activity-slice-absent:{runtime_intent.intent_id}",
                    ExecutionReceipt(None, False, "ActivitySlice dispatch did not occur"),
                )
            return ExecutionReconciliation(
                _activity_slice_outcome(slice_result),
                slice_result.after_evidence_id
                or slice_result.before_evidence_id
                or f"activity-slice:{slice_result.slice_id}",
                ExecutionReceipt(
                    slice_result.receipt_id,
                    slice_result.accepted,
                    f"activity slice {slice_result.slice_id} {slice_result.status}",
                ),
            )
        result = self._kernel.reconcile_application_cycle(cycle_key)
        if result is None:
            return ExecutionReconciliation(
                Outcome(
                    "confirmed_failure",
                    "Kernel ledger proves no subordinate cycle was created",
                    terminal=False,
                ),
                f"kernel-cycle-absent:{runtime_intent.intent_id}",
                ExecutionReceipt(None, False, "physical dispatch did not occur"),
            )
        receipt = ExecutionReceipt(
            result.task_id,
            accepted=result.physical_action_sent,
            detail=f"kernel application cycle {result.status}",
        )
        if result.status == "confirmed_success":
            outcome = Outcome(
                "confirmed_success", result.evidence, terminal=False
            )
        elif result.status == "confirmed_failure":
            outcome = Outcome(
                "confirmed_failure", result.evidence, terminal=False
            )
        elif result.status == "uncertain":
            outcome = Outcome("uncertain", result.evidence, terminal=True)
        else:
            outcome = Outcome("unconfirmed", result.evidence, terminal=True)
        return ExecutionReconciliation(
            outcome,
            result.after_evidence_id
            or result.before_evidence_id
            or f"kernel-cycle:{result.task_id}",
            receipt,
        )


class _Verifier:
    def __init__(
        self,
        kernel: KernelCanaryCoordinator,
        activity_slice_runner: ActivitySliceRunnerPort | None = None,
    ) -> None:
        self._kernel = kernel
        self._activity_slice_runner = activity_slice_runner

    def verify(self, context: Any) -> Outcome:
        if context.intent.name != _CYCLE_INTENT or not context.receipt.receipt_id:
            return Outcome(
                "confirmed_failure",
                "long-lived mobile cycle did not return a Kernel task receipt",
                terminal=False,
            )
        if self._activity_slice_runner is not None:
            result = self._activity_slice_runner.inspect_activity_slice(
                context.receipt.receipt_id
            )
            if (
                result.status == "confirmed_success"
                and (
                    not result.physical_action_sent
                    or result.after_evidence_id == context.after.evidence_id
                )
            ):
                return Outcome("confirmed_success", result.evidence, terminal=False)
            return _activity_slice_outcome(result)
        result = self._kernel.inspect_application_cycle(
            context.receipt.receipt_id
        )
        if (
            result.status == "confirmed_success"
            and result.physical_action_sent
            and result.after_evidence_id == context.after.evidence_id
        ):
            return Outcome(
                "confirmed_success",
                result.evidence,
                terminal=False,
            )
        if result.status == "confirmed_failure":
            return Outcome(
                "confirmed_failure",
                result.evidence,
                terminal=False,
            )
        if result.status == "uncertain":
            return Outcome("uncertain", result.evidence, terminal=True)
        return Outcome("unconfirmed", result.evidence, terminal=True)


def compose_long_lived_mobile_application_runtime(
    data_dir: Path,
    *,
    kernel: KernelCanaryCoordinator,
    observation_provider: AndroidObservationProvider,
    activity_slice_runner: ActivitySliceRunnerPort | None = None,
) -> LongLivedMobileApplicationComposition:
    database = data_dir / APPLICATION_DATABASE_FILENAME
    return LongLivedMobileApplicationComposition(
        LongLivedMobileApplicationRuntimeGateway(
            database,
            kernel=kernel,
            observation_provider=observation_provider,
            activity_slice_runner=activity_slice_runner,
        ),
        database,
    )


def _latest_cycle_intent(instance: Any) -> Any:
    matches = [
        item for item in instance.intents if item.intent.name == _CYCLE_INTENT
    ]
    if not matches:
        raise ValueError("long-lived mobile runtime intent is missing")
    return max(matches, key=lambda item: (item.cycle, item.created_at))


def _event_triggers(
    instance: Any, activation_sequence: int
) -> list[tuple[int, str, str, str | None]]:
    """Return durable, uniquely consumable physical-cycle triggers.

    ApplicationRuntime intentionally uses one generic ``wait_elapsed`` event
    for normal waits, dependency retries, and reconciliation retries.  Only a
    wake paired with the ordinary ``wait_scheduled`` event is a new work
    trigger; retry wakes merely resume the already-pending trigger.
    """

    events = sorted(instance.events, key=lambda item: item.sequence)
    triggers: list[tuple[int, str, str, str | None]] = []
    pending_wait: tuple[str, float | None] | None = None
    command_by_revision: dict[int, Any] = {}
    application_event_types = {
        "inbound_event",
        "delayed_positive",
        "delayed_negative",
        "no_response",
        "user_approval",
        "user_rejection",
    }
    for event in events:
        if event.event_type in {
            "wait_scheduled",
            "runtime_retry_scheduled",
            "reconciliation_wait_scheduled",
        }:
            pending_wait = (
                event.event_type,
                (
                    float(event.data.get("wait_seconds"))
                    if event.data.get("wait_seconds") is not None
                    else None
                ),
            )
            continue
        if event.event_type == "command_accepted":
            pending_wait = None
            if event.data.get("tag") == "Input":
                command_by_revision[int(event.data.get("revision") or 0)] = event
            continue
        if event.event_type == "wait_elapsed":
            if (
                pending_wait is not None
                and pending_wait[0] == "wait_scheduled"
                and event.sequence > activation_sequence
            ):
                trigger_kind = (
                    "activation_timer"
                    if pending_wait[1] == _ACTIVATION_WAIT_SECONDS
                    else "timer"
                )
                triggers.append(
                    (
                        event.sequence,
                        f"timer:{event.sequence}",
                        trigger_kind,
                        None,
                    )
                )
            pending_wait = None
            continue
        if (
            event.event_type in application_event_types
            and event.sequence > activation_sequence
        ):
            triggers.append(
                (
                    event.sequence,
                    f"application-event:{event.sequence}",
                    "application_event",
                    None,
                )
            )

    for index, content in enumerate(instance.inputs, start=1):
        event = command_by_revision.get(index)
        if event is None:
            continue
        triggers.append(
            (
                event.sequence,
                f"goal-input:{index}",
                "goal_input",
                content,
            )
        )
    triggers.sort(key=lambda item: (item[0], item[1]))
    # A Goal/application event that arrived while the mandatory activation
    # wait was in progress is the real trigger; the short wait only opened the
    # lifecycle gate and must not create a second physical cycle.
    early_non_timer = {
        item[1]
        for item in triggers
        if item[2] in {"goal_input", "application_event"}
    }
    if early_non_timer:
        first_non_timer_sequence = min(
            item[0]
            for item in triggers
            if item[2] in {"goal_input", "application_event"}
        )
        triggers = [
            item
            for item in triggers
            if not (
                item[2] == "activation_timer"
                and first_non_timer_sequence < item[0]
            )
        ]
    return triggers


def _activation_wait_completed(events: Any, activation_sequence: int) -> bool:
    pending = False
    for event in sorted(events, key=lambda item: item.sequence):
        if event.sequence <= activation_sequence:
            continue
        if event.event_type == "wait_scheduled":
            pending = (
                float(event.data.get("wait_seconds") or -1)
                == _ACTIVATION_WAIT_SECONDS
            )
            continue
        if event.event_type in {
            "runtime_retry_scheduled",
            "reconciliation_wait_scheduled",
            "command_accepted",
        }:
            pending = False
            continue
        if event.event_type == "wait_elapsed":
            if pending:
                return True
            pending = False
    return False


def _cycle_key(instance_id: str, intent_id: str) -> str:
    digest = hashlib.sha256(
        f"{instance_id}\0{intent_id}".encode("utf-8")
    ).hexdigest()
    return f"long-lived-mobile-cycle:{digest}"


def _activity_slice_cycle_key(
    reservation_id: str, step: ActivitySliceStepDispatch
) -> str:
    digest = hashlib.sha256(
        (
            f"{reservation_id}\0{step.step_id}\0{step.ordinal}\0"
            f"{step.idempotency_key}"
        ).encode("utf-8")
    ).hexdigest()
    return f"{reservation_id}:activity-step:{step.ordinal}:{digest}"


def _application_cycle_goal(instance: Any) -> str:
    cycle_goal = str(instance.initial_input or "")
    if instance.inputs:
        input_history = "\n".join(
            f"{index}. {content}"
            for index, content in enumerate(instance.inputs, start=1)
        )
        cycle_goal = (
            f"{cycle_goal}\n\n"
            "同一 GoalRun 当前已接收的授权输入历史（按顺序）：\n"
            f"{input_history}"
        )
    return cycle_goal


def _activity_slice_outcome(result: ActivitySliceDispatchResult) -> Outcome:
    if result.status == "confirmed_success":
        return Outcome("confirmed_success", result.evidence, terminal=False)
    if result.status == "confirmed_failure":
        return Outcome("confirmed_failure", result.evidence, terminal=False)
    if result.status == "uncertain":
        return Outcome("uncertain", result.evidence, terminal=True)
    return Outcome("unconfirmed", result.evidence, terminal=True)


def _activity_slice_result_data(
    result: ActivitySliceDispatchResult,
) -> dict[str, Any]:
    return {
        "activity_slice_id": result.slice_id,
        "activity_slice_status": result.status,
        "activity_step_ids": list(result.step_ids),
        "kernel_task_ids": list(result.kernel_task_ids),
        "physical_action_sent": result.physical_action_sent,
    }


def _result_data(result: KernelApplicationCycleResult) -> dict[str, Any]:
    return {
        "kernel_task_id": result.task_id,
        "cycle_key": result.cycle_key,
        "target_id": result.target_id,
        "application_id": result.application_id,
        "application_ready": result.application_ready,
        "before_observation_id": result.before_observation_id,
        "action_id": result.action_id,
        "execution_id": result.execution_id,
        "after_observation_id": result.after_observation_id,
        "verification_id": result.verification_id,
        "verdict": result.verdict,
        "physical_action_sent": result.physical_action_sent,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()
