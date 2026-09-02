from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Protocol


InstanceStatus = Literal[
    "queued", "running", "waiting", "paused", "stopping", "stopped", "completed", "failed"
]
OutcomeStatus = Literal[
    "confirmed_success", "confirmed_failure", "unconfirmed", "uncertain"
]


class ApplicationRuntimeError(RuntimeError):
    code = "application_runtime_error"


class RuntimeNotFound(ApplicationRuntimeError):
    code = "application_runtime_not_found"


class IdempotencyConflict(ApplicationRuntimeError):
    code = "application_runtime_request_id_conflict"


class RuntimeClosed(ApplicationRuntimeError):
    code = "application_runtime_closed"


class QueueFull(ApplicationRuntimeError):
    code = "application_runtime_queue_full"


class OwnerEventBindingMismatch(ApplicationRuntimeError):
    code = "application_runtime_owner_event_binding_mismatch"


class OwnerEventFenced(ApplicationRuntimeError):
    code = "application_runtime_owner_event_fenced"


class RetryableApplicationError(ApplicationRuntimeError):
    """A transient pre-intent dependency failure with a bounded retry delay.

    Adapters may raise this only while observing or deciding, before an intent
    reaches the execution owner.  The runtime persists a degraded wait cycle;
    it never uses this exception to replay an unfinished physical action.
    """

    code = "application_runtime_retryable"

    def __init__(self, wait_seconds: float = 1.0) -> None:
        if isinstance(wait_seconds, bool) or not 0.2 <= float(wait_seconds) <= 900:
            raise ValueError("retry wait_seconds must be between 0.2 and 900")
        super().__init__("application dependency is temporarily unavailable")
        self.wait_seconds = float(wait_seconds)


@dataclass(frozen=True, slots=True)
class Observation:
    evidence_id: str
    summary: str = ""
    fresh: bool = True
    data: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("observation evidence_id must not be blank")


@dataclass(frozen=True, slots=True)
class Intent:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)
    hard_risk: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("intent name must not be blank")


@dataclass(frozen=True, slots=True)
class Decision:
    intent: Intent | None = None
    complete: bool = False
    detail: str = ""
    memory_candidate: "MemoryCandidate | None" = None
    candidate_notification: bool = False
    wait_seconds: float | None = None

    def __post_init__(self) -> None:
        modes = int(self.intent is not None) + int(self.complete) + int(self.wait_seconds is not None)
        if modes != 1:
            raise ValueError("decision requires exactly one of intent, complete, or wait_seconds")
        if self.wait_seconds is not None:
            if isinstance(self.wait_seconds, bool) or not 0.2 <= float(self.wait_seconds) <= 900:
                raise ValueError("decision wait_seconds must be between 0.2 and 900")
            if self.memory_candidate is not None:
                raise ValueError("waiting decision cannot include a memory candidate")
        if self.candidate_notification and self.intent is None:
            raise ValueError("candidate notification requires an intent")


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    receipt_id: str | None = None
    accepted: bool = True
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Outcome:
    status: OutcomeStatus
    evidence: str = ""
    hard_risk: bool = False
    terminal: bool = True

    @property
    def confirmed_success(self) -> bool:
        return self.status == "confirmed_success"


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    scope: str
    content: Mapping[str, Any]
    evidence: tuple[str, ...]
    reward_required: bool = False

    def __post_init__(self) -> None:
        if not self.scope.strip():
            raise ValueError("memory candidate scope must not be blank")


@dataclass(frozen=True, slots=True)
class Input:
    content: str
    tag: Literal["Input"] = "Input"


@dataclass(frozen=True, slots=True)
class Pause:
    tag: Literal["Pause"] = "Pause"


@dataclass(frozen=True, slots=True)
class Resume:
    tag: Literal["Resume"] = "Resume"


@dataclass(frozen=True, slots=True)
class Stop:
    tag: Literal["Stop"] = "Stop"


Command = Input | Pause | Resume | Stop


_OWNER_EVENT_TYPES = {
    "candidate_notified",
    "delayed_positive",
    "delayed_negative",
    "no_response",
    "user_approval",
    "user_rejection",
}
_ATTRIBUTED_OWNER_EVENT_TYPES = _OWNER_EVENT_TYPES - {"candidate_notified"}


@dataclass(frozen=True, slots=True)
class ExternalOwnerEvent:
    """One evidence-only event accepted from an already-bound owner.

    This is an internal runtime port, not a Goal API request.  It deliberately
    carries only opaque IDs and evidence references: adapters must keep source
    text, screenshots, drafts, account identities, and raw owner payloads in
    their own protected ledger.
    """

    owner_binding_ref: str
    owner_event_id: str
    event_type: str
    evidence_refs: tuple[str, ...]
    attribution_scope: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner_binding_ref, str) or not self.owner_binding_ref.strip():
            raise ValueError("owner_binding_ref must not be blank")
        if not isinstance(self.owner_event_id, str) or not self.owner_event_id.strip():
            raise ValueError("owner_event_id must not be blank")
        if self.event_type not in _OWNER_EVENT_TYPES:
            raise ValueError("owner event type is not supported")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("owner event requires evidence references")
        if any(not isinstance(item, str) or not item.strip() for item in self.evidence_refs):
            raise ValueError("owner event evidence references must be non-blank strings")
        if self.event_type in _ATTRIBUTED_OWNER_EVENT_TYPES:
            if not isinstance(self.attribution_scope, str) or not self.attribution_scope.strip():
                raise ValueError("attributed owner event requires attribution_scope")
            if (
                isinstance(self.confidence, bool)
                or self.confidence is None
                or not 0.0 <= float(self.confidence) <= 1.0
            ):
                raise ValueError("attributed owner event confidence must be between 0 and 1")
        elif self.confidence is not None:
            raise ValueError("candidate owner event cannot carry confidence")

    def durable_data(self) -> Mapping[str, Any]:
        """Return the allow-listed, content-free projection for the event log."""

        data: dict[str, Any] = {"evidence_refs": list(self.evidence_refs)}
        if self.attribution_scope is not None:
            data["attribution_scope"] = self.attribution_scope
        if self.confidence is not None:
            data["confidence"] = float(self.confidence)
        return data


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    sequence: int
    event_type: str
    data: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class RuntimeIntent:
    intent_id: str
    cycle: int
    revision: int
    intent: Intent
    phase: str
    reservation_id: str | None
    receipt: ExecutionReceipt | None
    created_at: str
    finalized_at: str | None
    candidate_notification: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionReconciliation:
    """Fresh owner-led convergence for an intent found unfinished on restart.

    Reconciliation is inspect-only from the runtime's perspective.  It may
    confirm or quarantine an earlier physical action, but it must never resend
    that action.  ``retry_after_seconds`` means the owner reports that the
    original dispatch is still active: the runtime retains the open intent and
    cycle, persists an interruptible wake, and calls only ``reconcile`` again.
    """

    outcome: Outcome
    evidence_id: str
    receipt: ExecutionReceipt | None = None
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("reconciliation evidence_id must not be blank")
        if self.retry_after_seconds is not None:
            if (
                isinstance(self.retry_after_seconds, bool)
                or not 0.2 <= float(self.retry_after_seconds) <= 900
            ):
                raise ValueError(
                    "reconciliation retry_after_seconds must be between 0.2 and 900"
                )
            if self.outcome.status != "unconfirmed" or self.outcome.terminal:
                raise ValueError(
                    "deferred reconciliation requires a nonterminal unconfirmed outcome"
                )


@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    cycle: int
    status: OutcomeStatus
    evidence: str
    hard_risk: bool
    terminal: bool
    after_evidence_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ApplicationInstance:
    instance_id: str
    profile_id: str
    target_id: str | None
    initial_input: str | None
    status: InstanceStatus
    revision: int
    degraded: bool
    hard_risk: bool
    detail: str | None
    error_code: str | None
    memory_version: int
    inputs: tuple[str, ...]
    intents: tuple[RuntimeIntent, ...]
    outcomes: tuple[RuntimeOutcome, ...]
    events: tuple[RuntimeEvent, ...]
    created_at: str
    updated_at: str
    finished_at: str | None
    wake_at: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"stopped", "completed", "failed"}


@dataclass(frozen=True, slots=True)
class PolicyContext:
    instance: ApplicationInstance
    before: Observation
    active_memory: Mapping[str, Any] | None
    is_cancelled: Callable[[], bool] = field(
        default=lambda: False,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class VerificationContext:
    instance: ApplicationInstance
    intent: Intent
    before: Observation
    after: Observation
    receipt: ExecutionReceipt


class ObservationPort(Protocol):
    def observe(self, instance: ApplicationInstance) -> Observation: ...


class AfterObservationPort(ObservationPort, Protocol):
    def observe_after(
        self,
        instance: ApplicationInstance,
        intent: Intent,
        receipt: ExecutionReceipt,
    ) -> Observation: ...


class Policy(Protocol):
    def decide(self, context: PolicyContext) -> Decision: ...


class ExecutionOwner(Protocol):
    def reserve(self, instance: ApplicationInstance, intent: Intent) -> str: ...

    def dispatch(
        self, reservation_id: str, instance: ApplicationInstance, intent: Intent
    ) -> ExecutionReceipt: ...


class RecoverableExecutionOwner(ExecutionOwner, Protocol):

    def reconcile(
        self, instance: ApplicationInstance, runtime_intent: RuntimeIntent
    ) -> ExecutionReconciliation: ...


class Verifier(Protocol):
    def verify(self, context: VerificationContext) -> Outcome: ...


class MemoryGate(Protocol):
    def promote(
        self, candidate: MemoryCandidate, outcome: Outcome
    ) -> bool | Mapping[str, Any]: ...


class PersistenceProjection(Protocol):
    """Project process-local values into a safe durable/public representation.

    An application adapter can keep screenshots, titles, transcripts, and drafts in an
    in-process vault while persisting only hashes and opaque references.  A
    projection may redact representation, but must not change control facts
    such as intent name, receipt acceptance, outcome status, or risk flags.
    """

    def project_input(self, value: str) -> str: ...

    def project_observation(self, value: Observation) -> Observation: ...

    def project_intent(self, value: Intent) -> Intent: ...

    def project_receipt(self, value: ExecutionReceipt) -> ExecutionReceipt: ...

    def project_outcome(self, value: Outcome) -> Outcome: ...

    def project_memory_candidate(self, value: MemoryCandidate) -> MemoryCandidate: ...

    def project_memory_content(
        self, scope: str, value: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def project_detail(self, value: str) -> str: ...
