from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..execution import GuiAction, GuiActionName


JobStatus = Literal[
    "queued",
    "running",
    "stopping",
    "learned",
    "not_learned",
    "failed",
    "stopped",
    "stopped_uncertain",
]
JobOutcome = Literal["unknown", "confirmed_success", "confirmed_failure", "unconfirmed"]
PolicyState = Literal["unchanged", "candidate", "promoted", "rejected"]
TransportStatus = Literal["not_sent", "accepted", "rejected", "uncertain"]
ProposalKind = Literal["execute", "wait", "terminate"]


@dataclass(frozen=True, slots=True)
class GameProfile:
    """One immutable, positively scoped learning environment revision."""

    profile_id: str
    name: str
    allowed_actions: tuple[GuiActionName, ...]
    max_actions: int = 32
    max_duration_seconds: float = 180.0
    default_target_id: str | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.name.strip():
            raise ValueError("profile id and name must not be blank")
        if not self.allowed_actions or len(set(self.allowed_actions)) != len(
            self.allowed_actions
        ):
            raise ValueError("profile allowed_actions must be non-empty and unique")
        if self.max_actions < 1 or self.max_duration_seconds <= 0 or self.revision < 1:
            raise ValueError("profile budgets and revision must be positive")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    sha256: str
    relative_path: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class Observation:
    """Ephemeral evidence; payload bytes never belong in the learning database."""

    payload: bytes | None = field(default=None, repr=False)
    summary: str = ""
    mime_type: str = "image/png"

    def __post_init__(self) -> None:
        if self.payload is not None and not isinstance(self.payload, bytes):
            raise TypeError("observation payload must be bytes or None")
        if len(self.summary) > 4_000:
            raise ValueError("observation summary is too long")


@dataclass(frozen=True, slots=True)
class ActionProposal:
    kind: ProposalKind
    action: GuiAction | None = None
    wait_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.kind == "execute" and self.action is None:
            raise ValueError("execute proposal requires an action")
        if self.kind != "execute" and self.action is not None:
            raise ValueError("only execute proposals may carry an action")
        if self.kind == "wait" and (
            self.wait_seconds is None or not 0 <= self.wait_seconds <= 5.0
        ):
            raise ValueError("wait proposal requires 0..5 seconds")


@dataclass(frozen=True, slots=True)
class TransportReceipt:
    """Physical transport fact, deliberately separate from outcome truth."""

    status: TransportStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class OutcomeVerification:
    """Evidence-backed outcome fact returned by an OutcomeVerifier."""

    confirmed: bool
    task_succeeded: bool
    reward: float
    detail: str = ""

    def __post_init__(self) -> None:
        if self.task_succeeded and not self.confirmed:
            raise ValueError("task success must be confirmed")
        if self.reward > 0 and not self.confirmed:
            raise ValueError("positive reward must be confirmed")


@dataclass(frozen=True, slots=True)
class DistilledTransition:
    action: GuiAction
    reward: float
    before_sha256: str | None
    after_sha256: str | None
    verifier_detail: str


@dataclass(frozen=True, slots=True)
class PolicyMemory:
    profile_id: str
    version: int
    trajectory: tuple[DistilledTransition, ...] = ()


@dataclass(frozen=True, slots=True)
class Transition:
    transition_id: str
    job_id: str
    sequence: int
    proposal: ActionProposal
    before: ArtifactRef | None
    after: ArtifactRef | None
    transport: TransportReceipt | None
    outcome: OutcomeVerification | None
    created_at: str
    finalized_at: str | None = None

    @property
    def finalized(self) -> bool:
        return self.transport is not None and self.outcome is not None


@dataclass(frozen=True, slots=True)
class LearningJob:
    job_id: str
    profile_id: str
    profile_revision: int
    target_id: str | None
    instruction: str
    client_request_id: str
    request_digest: str
    status: JobStatus
    cancel_requested: bool
    transition_count: int
    policy_version: int
    policy_memory_count: int | None
    outcome: JobOutcome
    policy_state: PolicyState
    total_reward: float | None
    verified_successes: int | None
    detail: str | None
    error_code: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


LearningEpisode = LearningJob


@dataclass(frozen=True, slots=True)
class TrainingJob:
    job_id: str
    profile_id: str
    training_kind: Literal["trajectory_distillation"] = "trajectory_distillation"
    status: Literal["disabled", "completed"] = "disabled"
    policy_version: int = 0


class OutcomeVerifier(Protocol):
    def verify(
        self,
        *,
        before: Observation,
        action: GuiAction | None,
        transport: TransportReceipt,
        after: Observation | None,
    ) -> OutcomeVerification: ...


class Session(Protocol):
    @property
    def verifier(self) -> OutcomeVerifier: ...

    def observe(self) -> Observation: ...

    def propose_action(
        self,
        *,
        instruction: str,
        observation: Observation,
        policy_memory: PolicyMemory,
    ) -> ActionProposal: ...

    def execute(self, action: GuiAction) -> TransportReceipt: ...

    def close(self) -> None: ...


class EnvironmentFactory(Protocol):
    def open(
        self,
        *,
        profile: GameProfile,
        target_id: str | None,
        is_cancelled: Callable[[], bool],
    ) -> Session: ...


class Trainer(Protocol):
    """Disabled external seam in v1; the built-in kind is trajectory distillation."""

    def distill(
        self,
        *,
        profile: GameProfile,
        transitions: Sequence[Transition],
        previous: PolicyMemory,
    ) -> tuple[DistilledTransition, ...]: ...
