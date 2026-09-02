from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .application_runtime import (
    ApplicationRuntime,
    Decision,
    ExecutionReconciliation,
    ExecutionReceipt,
    Intent,
    Observation,
    Outcome,
    RuntimeClosed,
)
from .application_runtime.store import _SQLiteApplicationStore, request_digest
from .config import Settings


PROFILE_ID = "local-managed-v1"
APPLICATION_DATABASE_FILENAME = "local-managed-application-runtime.db"
_CHECKPOINT_INTENT = "local.managed_candidate_checkpoint.v1"
_WAIT_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class LocalManagedApplicationComposition:
    """The built-in, account-free continuous GoalRun capability.

    This profile deliberately owns only AI-GAME's own durable checkpoint and
    candidate-notification lifecycle.  It never opens a device, network
    connection, third-party account, or external execution owner.
    """

    runtime: Any


class LocalManagedApplicationRuntimeGateway:
    """Lazy gateway so an unread default app does not claim the runtime lock.

    The normal launcher calls ``startup`` and owns the local coordinator. Unit
    construction and read-only API history stay possible without accidentally
    creating a second coordinator for the same data directory.
    """

    def __init__(self, database_path: Path | str) -> None:
        self._database_path = database_path
        self._archive = _SQLiteApplicationStore(database_path)
        self._runtime: ApplicationRuntime | None = None
        self._closed = False
        self._lock = threading.RLock()

    def startup(self) -> None:
        self._runtime_for_new_work()

    def start(
        self,
        profile_id: str,
        client_request_id: str,
        target_id: str | None = None,
        initial_input: str | None = None,
    ) -> Any:
        if profile_id != PROFILE_ID:
            raise ValueError("profile_id must match local managed runtime profile")
        return self._runtime_for_new_work().start(
            profile_id,
            client_request_id,
            target_id=target_id,
            initial_input=initial_input,
        )

    def command(self, instance_id: str, command: Any, client_request_id: str) -> Any:
        return self._runtime_for_new_work().command(
            instance_id, command, client_request_id
        )

    def inspect(self, instance_id: str) -> Any:
        return self._archive.inspect(instance_id)

    def list(self, limit: int = 100) -> list[Any]:
        return self._archive.list(limit)

    def fence_pause_before_start(self, instance_id: str, request_id: str) -> Any:
        """Persist a real Pause before ApplicationRuntime recovery can queue it."""

        state = self.inspect(instance_id)
        if state.status in {"paused", "stopped", "completed", "failed"}:
            return state
        digest = request_digest(
            "command",
            {"instance_id": instance_id, "tag": "Pause", "content": None},
        )
        self._archive.accept_command(
            instance_id, "Pause", None, request_id, digest
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
                raise RuntimeClosed("local managed application runtime is closed")
            if self._runtime is None:
                self._runtime = ApplicationRuntime(
                    self._database_path,
                    profile=PROFILE_ID,
                    memory_scope=PROFILE_ID,
                    observation_port=_ObservationPort(),
                    policy=_Policy(),
                    execution_owner=_ExecutionOwner(),
                    verifier=_Verifier(),
                )
            return self._runtime


class _ObservationPort:
    def observe(self, instance: Any) -> Observation:
        return Observation(
            _evidence_id(instance, "before"),
            "本地长期 GoalRun 已读取当前持久状态。",
            fresh=True,
            data={"scope": "ai_game_local_managed"},
        )

    def observe_after(
        self, instance: Any, intent: Intent, receipt: ExecutionReceipt
    ) -> Observation:
        del intent, receipt
        return Observation(
            _evidence_id(instance, "after"),
            "本地候选里程碑已写入 AI-GAME 持久账本。",
            fresh=True,
            data={"scope": "ai_game_local_managed"},
        )


class _Policy:
    def decide(self, context: Any) -> Decision:
        instance = context.instance
        if any(
            runtime_intent.intent.name == _CHECKPOINT_INTENT
            for runtime_intent in instance.intents
        ):
            return Decision(
                wait_seconds=_WAIT_SECONDS,
                detail="本地候选里程碑已提交；继续等待下一次授权事件或定时唤醒。",
            )
        if instance.inputs:
            return Decision(
                intent=Intent(
                    _CHECKPOINT_INTENT,
                    {"trigger": "authorized_goal_message"},
                ),
                detail="根据同一 GoalRun 的用户后续消息提交一次本地候选里程碑。",
                candidate_notification=True,
            )
        return Decision(
            wait_seconds=_WAIT_SECONDS,
            detail="等待同一 GoalRun 的用户后续消息或下一次本地定时唤醒。",
        )


class _ExecutionOwner:
    def reserve(self, instance: Any, intent: Intent) -> str:
        return _receipt_id(instance, intent, "reservation")

    def dispatch(
        self, reservation_id: str, instance: Any, intent: Intent
    ) -> ExecutionReceipt:
        if reservation_id != _receipt_id(instance, intent, "reservation"):
            raise ValueError("local managed reservation mismatch")
        return ExecutionReceipt(
            _receipt_id(instance, intent, "receipt"),
            True,
            "AI-GAME local managed candidate milestone committed",
        )

    def reconcile(self, instance: Any, runtime_intent: Any) -> ExecutionReconciliation:
        intent = runtime_intent.intent
        return ExecutionReconciliation(
            Outcome(
                "confirmed_success",
                "local managed checkpoint is durably reconcilable",
                terminal=False,
            ),
            _receipt_id(instance, intent, "reconciliation-evidence"),
            ExecutionReceipt(
                _receipt_id(instance, intent, "receipt"),
                True,
                "AI-GAME local managed candidate milestone reconciled",
            ),
        )


class _Verifier:
    def verify(self, context: Any) -> Outcome:
        if context.intent.name != _CHECKPOINT_INTENT:
            return Outcome(
                "confirmed_failure",
                "local managed runtime rejected an unknown intent",
                terminal=True,
            )
        if not context.receipt.accepted:
            return Outcome(
                "confirmed_failure",
                "local managed checkpoint was not accepted",
                terminal=True,
            )
        return Outcome(
            "confirmed_success",
            "local managed checkpoint receipt and fresh after-observation agree",
            terminal=False,
        )


def compose_local_managed_application_runtime(
    settings: Settings,
) -> LocalManagedApplicationComposition:
    """Build the normal-launcher local profile without contacting anything."""

    database = settings.data_dir / APPLICATION_DATABASE_FILENAME
    return LocalManagedApplicationComposition(
        LocalManagedApplicationRuntimeGateway(database)
    )


def _evidence_id(instance: Any, phase: str) -> str:
    material = ":".join(
        (
            PROFILE_ID,
            str(instance.instance_id),
            phase,
            str(instance.revision),
            str(len(instance.inputs)),
            str(len(instance.intents)),
            str(len(instance.outcomes)),
        )
    )
    return "local-managed-evidence:v1:" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()


def _receipt_id(instance: Any, intent: Intent, kind: str) -> str:
    material = "\0".join((PROFILE_ID, str(instance.instance_id), intent.name, kind))
    return "local-managed:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
