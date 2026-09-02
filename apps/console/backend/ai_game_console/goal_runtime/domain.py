from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


class GoalError(RuntimeError):
    code = "goal_error"
    status_code = 409

    def __init__(self, message: str = "goal operation failed") -> None:
        super().__init__(message)

    def as_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": str(self)}}


class GoalNotFound(GoalError):
    code = "goal_not_found"
    status_code = 404


class GoalIdempotencyConflict(GoalError):
    code = "goal_idempotency_conflict"
    status_code = 409


class GoalControlUnsupported(GoalError):
    code = "goal_control_unsupported"
    status_code = 409


class GoalStateConflict(GoalError):
    code = "goal_state_conflict"
    status_code = 409


class GoalOwnerUnavailable(GoalError):
    """A bound execution owner could not prove its current state/control."""

    code = "goal_owner_temporarily_unavailable"
    status_code = 503


@dataclass(frozen=True, slots=True)
class GoalRecord:
    id: str
    original_goal: str
    execution_status: str
    control_state: str
    resume_execution_status: str | None
    active_stage: str | None
    binding_kind: str
    binding_state: str
    bound_task_id: str | None
    target_id: str | None
    waiting_reason: dict[str, Any] | None
    error: dict[str, Any] | None
    result_summary: str | None
    created_at: str
    updated_at: str
    terminal_at: str | None


@dataclass(frozen=True, slots=True)
class CapabilityBindingPlan:
    goal_id: str
    revision: int
    route_kind: str
    binding_kind: str
    capability_ids: tuple[str, ...]
    owner_kind: str | None
    owner_binding_ref: str | None
    profile_id: str | None
    classification: str
    rationale: str
    created_at: str


@dataclass(frozen=True, slots=True)
class GoalNotification:
    notification_id: str
    goal_id: str
    source_event_sequence: int
    kind: str
    summary: str
    evidence_refs: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class StoredEvent:
    cursor: int
    goal_id: str
    event_type: str
    data: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class SuccessCriterion:
    criterion_id: str
    description: str
    evidence_requirement: str
    source_quote: str

    def __post_init__(self) -> None:
        if not self.criterion_id.strip() or not self.description.strip():
            raise ValueError("success criterion id and description must not be blank")
        if not self.evidence_requirement.strip():
            raise ValueError("success criterion evidence requirement must not be blank")
        if not self.source_quote.strip():
            raise ValueError("success criterion source quote must not be blank")


@dataclass(frozen=True, slots=True)
class GoalSpecificationDraft:
    normalized_intent: dict[str, Any]
    success_criteria: tuple[SuccessCriterion, ...]

    def __post_init__(self) -> None:
        if not self.success_criteria:
            raise ValueError("goal specification requires success criteria")
        identifiers = [item.criterion_id for item in self.success_criteria]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("success criterion ids must be unique")


@dataclass(frozen=True, slots=True)
class CriterionAssessment:
    criterion_id: str
    satisfied: bool
    subgoal_indices: tuple[int, ...]
    attempt_sequences: tuple[int, ...]
    evidence: str

    def __post_init__(self) -> None:
        if not self.criterion_id.strip() or not self.evidence.strip():
            raise ValueError("criterion assessment id and evidence must not be blank")
        if self.satisfied and (not self.subgoal_indices or not self.attempt_sequences):
            raise ValueError("satisfied criterion requires plan coverage and attempt evidence")
        if len(set(self.subgoal_indices)) != len(self.subgoal_indices):
            raise ValueError("subgoal coverage references must be unique")
        if len(set(self.attempt_sequences)) != len(self.attempt_sequences):
            raise ValueError("attempt evidence references must be unique")


@dataclass(frozen=True, slots=True)
class GoalCompletionAssessment:
    verdict: Literal["verified", "partial", "uncertain"]
    criteria: tuple[CriterionAssessment, ...]
    verified_facts: tuple[str, ...]
    result_summary: str

    def __post_init__(self) -> None:
        if self.verdict not in {"verified", "partial", "uncertain"}:
            raise ValueError("invalid goal completion verdict")
        if not self.criteria or not self.result_summary.strip():
            raise ValueError("completion assessment requires criteria and a result summary")
        if any(not item.strip() for item in self.verified_facts):
            raise ValueError("verified facts must not contain blank items")
