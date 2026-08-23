from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
class _Binding:
    instance_id: str
    goal_id: str
    target_id: str
    application_id: str
    activated_at: str | None


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
        self, *, instance_id: str, goal_id: str, target_id: str, application_id: str
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
                    (instance_id, goal_id, target_id, application_id, now),
                )
            elif (
                str(row["goal_id"]),
                str(row["target_id"]),
                str(row["application_id"]),
            ) != (goal_id, target_id, application_id):
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
    ) -> None:
        self.database_path = Path(database_path)
        self._archive = _SQLiteApplicationStore(self.database_path)
        self._bindings = _BindingStore(self.database_path)
        self._kernel = kernel
        self._observation_provider = observation_provider
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
        application_id: str,
        initial_input: str,
    ) -> Any:
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
            application_id=application_id,
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
                        self._bindings, self._observation_provider, self._kernel
                    ),
                    policy=_Policy(self._bindings),
                    execution_owner=_ExecutionOwner(self._bindings, self._kernel),
                    verifier=_Verifier(self._kernel),
                )
            return self._runtime


class _ObservationPort:
    def __init__(
        self,
        bindings: _BindingStore,
        provider: AndroidObservationProvider,
        kernel: KernelCanaryCoordinator,
    ) -> None:
        self._bindings = bindings
        self._provider = provider
        self._kernel = kernel

    def observe(self, instance: Any) -> Observation:
        try:
            binding = self._bindings.get(instance.instance_id)
            state = self._provider.read_device_state(binding.target_id)
        except (KeyError, OSError, RuntimeError, ValueError):
            raise RetryableApplicationError(2.0) from None
        foreground = str(state.foreground_app or "").strip()
        if foreground != binding.application_id:
            # [constraint-source: ARCH_INVARIANT; ref: current-target validation]
            # A temporary foreground change is a resumable event gate.  It
            # must neither terminalize the long-lived GoalRun nor authorize an
            # action against a different application.
            raise RetryableApplicationError(2.0)
        material = "\0".join(
            (
                instance.instance_id,
                binding.target_id,
                binding.application_id,
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
                "application_id": binding.application_id,
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
                        "application_id": binding.application_id,
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
        self, bindings: _BindingStore, kernel: KernelCanaryCoordinator
    ) -> None:
        self._bindings = bindings
        self._kernel = kernel

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
        result = self._kernel.execute_application_cycle(
            goal=cycle_goal,
            goal_id=binding.goal_id,
            application_instance_id=instance.instance_id,
            application_cycle=runtime_intent.cycle,
            cycle_key=reservation_id,
            target_id=binding.target_id,
            expected_application_id=binding.application_id,
        )
        return ExecutionReceipt(
            result.task_id,
            accepted=result.physical_action_sent,
            detail=f"kernel application cycle {result.status}",
        )

    def reconcile(self, instance: Any, runtime_intent: Any) -> ExecutionReconciliation:
        cycle_key = runtime_intent.reservation_id or _cycle_key(
            instance.instance_id, runtime_intent.intent_id
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
    def __init__(self, kernel: KernelCanaryCoordinator) -> None:
        self._kernel = kernel

    def verify(self, context: Any) -> Outcome:
        if context.intent.name != _CYCLE_INTENT or not context.receipt.receipt_id:
            return Outcome(
                "confirmed_failure",
                "long-lived mobile cycle did not return a Kernel task receipt",
                terminal=False,
            )
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
) -> LongLivedMobileApplicationComposition:
    database = data_dir / APPLICATION_DATABASE_FILENAME
    return LongLivedMobileApplicationComposition(
        LongLivedMobileApplicationRuntimeGateway(
            database,
            kernel=kernel,
            observation_provider=observation_provider,
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
