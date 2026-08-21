from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SignalKind = Literal[
    "immediate_success",
    "immediate_failure",
    "no_progress",
    "wrong_scene",
    "recovered",
    "task_success",
    "task_failure",
    "delayed_positive",
    "delayed_negative",
    "no_response",
    "user_approval",
    "user_rejection",
    "uncertain",
]
CandidateKind = Literal["positive", "negative", "recovery"]
CandidateStatus = Literal["candidate", "promoted", "rejected", "deprecated"]
PolicyStatus = Literal["active", "rolled_back", "superseded"]


@dataclass(frozen=True, slots=True)
class ScopeKey:
    user_scope: str
    account_scope: str
    application_id: str
    goal_family: str
    ui_version: str = "unknown"
    device_class: str = "android"
    orientation: str = "portrait"

    def __post_init__(self) -> None:
        for name in ("user_scope", "account_scope", "application_id", "goal_family"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")


@dataclass(frozen=True, slots=True)
class ExperienceEpisode:
    episode_id: str
    goal_run_id: str
    source_task_id: str
    goal_spec_revision: int
    scope: ScopeKey
    frozen_criteria_ids: tuple[str, ...]
    terminal_outcome: str | None
    action_count: int
    recovery_count: int
    intervention_count: int
    created_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class SceneState:
    scene_id: str
    episode_id: str
    observation_ref: str
    integrity_hash: str
    visual_fingerprint: str
    semantic_label: str
    anchors: tuple[str, ...]
    compatibility_key: str
    confidence: float
    created_at: str


@dataclass(frozen=True, slots=True)
class ActionTransition:
    transition_id: str
    episode_id: str
    source_attempt_id: str
    source_attempt_sequence: int
    objective: str
    before_scene_id: str
    semantic_action: str
    grounded_region: str | None
    coordinates_json: str | None
    expected_outcome: str
    transport_status: str
    after_scene_id: str | None
    immediate_outcome: str
    failure_class: str | None
    recovery_of_transition_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class OutcomeSignal:
    signal_id: str
    episode_id: str
    transition_id: str | None
    kind: SignalKind
    source: str
    evidence_refs: tuple[str, ...]
    confidence: float
    reward_vector_json: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ExperienceCandidate:
    candidate_id: str
    scope: ScopeKey
    kind: CandidateKind
    objective_matcher: str
    scene_matcher: str
    semantic_action: str
    expected_next_scene: str | None
    recovery_action: str | None
    support_count: int
    failure_count: int
    confidence: float
    provenance_transition_ids: tuple[str, ...]
    compatibility_key: str
    status: CandidateStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PolicyRevision:
    policy_id: str
    scope: ScopeKey
    revision: int
    candidate_ids: tuple[str, ...]
    status: PolicyStatus
    created_at: str
    rollback_of_revision: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievedExperience:
    candidate_id: str
    kind: CandidateKind
    semantic_action: str
    expected_next_scene: str | None
    recovery_action: str | None
    confidence: float
    provenance_transition_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExperiencePacket:
    retrieval_id: str | None
    items: tuple[RetrievedExperience, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.items
