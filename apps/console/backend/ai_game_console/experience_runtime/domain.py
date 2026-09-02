from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from ..android_ui_runtime.domain import OwnerBinding, owner_scope_digest


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
AndroidExperienceKind = Literal["progress", "positive", "negative", "recovery"]
AndroidCandidateStatus = Literal["task_local", "active", "deprecated"]


@dataclass(frozen=True, slots=True)
class ScopeKey:
    """The minimum identity boundary for reusable execution experience.

    The identifiers here are deliberately opaque references.  They describe
    applicability; provenance remains on ``ExperienceEpisode`` and its
    transitions.  Callers must not put conversation text, credentials, screen
    contents, or file paths in these fields.
    """

    user_scope: str
    account_scope: str
    application_id: str
    goal_family: str
    ui_version: str = "unknown"
    device_class: str = "android"
    orientation: str = "portrait"
    task_scope: str = "generic-task"
    subtask_scope: str = "generic-subtask"
    device_profile_id: str = "legacy-profile"
    object_ref: str = "none"
    conversation_ref: str = "none"

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be blank")
            if len(value) > 512:
                raise ValueError(f"{name} must be bounded")


@dataclass(frozen=True, slots=True)
class AuthenticatedAndroidScope:
    """Private, exact applicability boundary for Android UI experience.

    The caller supplies the authenticated owner pair; only its domain-separated
    digest is retained.  Object, account and conversation values are likewise
    stored as opaque digests, never as user-facing values.  ``unknown`` app
    versions intentionally remain task-local and can never be promoted.
    """

    owner_scope_digest: str
    profile_id: str
    profile_generation: int
    app_package: str
    app_version: str
    android_api: str
    android_build: str
    resolution: str
    density: str
    orientation: str
    observation_schema: str
    runner_kind: str
    runner_version: int
    criteria_revision: int
    criteria_digest: str
    task_id: str
    subtask_id: str
    account_scope_digest: str = "none"
    object_scope_digest: str = "none"
    conversation_scope_digest: str = "none"
    # A related-task bucket is separate from each Task's frozen criteria.
    reuse_key: str = "task-local"

    def __post_init__(self) -> None:
        if self.profile_generation < 1 or self.runner_version < 1 or self.criteria_revision < 1:
            raise ValueError("profile, runner and criteria versions must be positive")
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, int):
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(f"{name} must be a bounded non-blank string")

    @property
    def cross_task_eligible(self) -> bool:
        # Cross-Task reuse is opt-in only when every environmental fact that
        # can change UI meaning is known.  ``none`` is a valid *optional*
        # account/object/conversation bucket, but it is never valid for a
        # required device/app/criteria dimension.
        required = (
            self.profile_id, self.app_package, self.app_version,
            self.android_api, self.android_build, self.resolution,
            self.density, self.orientation, self.observation_schema,
            self.runner_kind, self.criteria_digest,
        )
        return (
            all(value not in {"unknown", "none", "legacy-profile"} for value in required)
            and self.reuse_key not in {"unknown", "none", "task-local"}
        )

    def reusable_scope(self) -> "AuthenticatedAndroidScope":
        if not self.cross_task_eligible:
            raise ValueError("unknown application version is task-local")
        return AuthenticatedAndroidScope(
            owner_scope_digest=self.owner_scope_digest,
            profile_id=self.profile_id,
            profile_generation=self.profile_generation,
            app_package=self.app_package,
            app_version=self.app_version,
            android_api=self.android_api,
            android_build=self.android_build,
            resolution=self.resolution,
            density=self.density,
            orientation=self.orientation,
            observation_schema=self.observation_schema,
            runner_kind=self.runner_kind,
            runner_version=self.runner_version,
            # A reusable hint has an environmental reuse key, not the source
            # Task's semantic acceptance contract.  Each Task keeps its own
            # immutable criteria_digest on its episode/retrieval records.
            criteria_revision=1,
            criteria_digest="related-task-reuse-v1",
            task_id="cross-task",
            subtask_id="cross-subtask",
            account_scope_digest=self.account_scope_digest,
            object_scope_digest=self.object_scope_digest,
            conversation_scope_digest=self.conversation_scope_digest,
            reuse_key=self.reuse_key,
        )

    @property
    def attestation_digest(self) -> str:
        """Stable full-scope proof used only between K2 and K3 internals."""
        payload = {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(("android-experience-scope\x00" + encoded).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TrustedVerificationQuery:
    """Exact K3 request for an immutable K2 semantic checkpoint."""

    scope: AuthenticatedAndroidScope
    scope_digest: str
    record_id: str
    task_id: str
    revision: int
    step_index: int


@dataclass(frozen=True, slots=True)
class TrustedVerificationAttestation:
    """K2-owned proof; callers cannot manufacture it from a dataclass record."""

    record_id: str
    task_id: str
    scope_digest: str
    revision: int
    step_index: int
    checkpoint_index: int
    already_satisfied: bool
    immutable_record: Mapping[str, Any]


def authenticated_android_scope(
    *, principal_id: str, controller_id: str, profile_id: str,
    profile_generation: int, app_package: str, app_version: str,
    android_api: str, android_build: str, resolution: str, density: str,
    orientation: str, observation_schema: str, runner_kind: str,
    runner_version: int, criteria_revision: int, criteria_digest: str,
    task_id: str, subtask_id: str,
    account_scope: str | None = None, object_ref: str | None = None,
    conversation_ref: str | None = None,
) -> AuthenticatedAndroidScope:
    """Build private scope from a host-authenticated owner pair only.

    This intentionally has no DSH session, tool-call or request-body owner
    argument.  Composition must pass its authenticated principal/controller.
    """
    return AuthenticatedAndroidScope(
        owner_scope_digest=owner_scope_digest(OwnerBinding(principal_id, controller_id)),
        profile_id=profile_id, profile_generation=profile_generation,
        app_package=app_package, app_version=app_version,
        android_api=android_api, android_build=android_build,
        resolution=resolution, density=density, orientation=orientation,
        observation_schema=observation_schema, runner_kind=runner_kind,
        runner_version=runner_version, criteria_revision=criteria_revision,
        criteria_digest=criteria_digest, task_id=task_id, subtask_id=subtask_id,
        account_scope_digest=_opaque_optional("account", account_scope),
        object_scope_digest=_opaque_optional("object", object_ref),
        conversation_scope_digest=_opaque_optional("conversation", conversation_ref),
        reuse_key=_opaque_digest(
            "android-ui-related-reuse-v1",
            owner_scope_digest(OwnerBinding(principal_id, controller_id)),
            profile_id, str(profile_generation), app_package, app_version,
            android_api, android_build, resolution, density, orientation,
            observation_schema, runner_kind, str(runner_version),
        ),
    )


def _opaque_optional(namespace: str, value: str | None) -> str:
    return "none" if value is None else _opaque_digest(namespace, value)


def _opaque_digest(namespace: str, *parts: str) -> str:
    if not parts or any(not isinstance(part, str) or not part.strip() for part in parts):
        raise ValueError(f"{namespace} scope requires non-blank authenticated values")
    payload = "\x00".join((namespace, *parts)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class AndroidExperienceCandidate:
    candidate_id: str
    source_episode_id: str
    scope: AuthenticatedAndroidScope
    kind: AndroidExperienceKind
    action_kind: str
    semantic_anchor: str
    expected_scene_marker: str
    source_checkpoint_index: int
    source_observation_digest: str
    support_count: int
    failure_count: int
    confidence: float
    provenance_ids: tuple[str, ...]
    status: AndroidCandidateStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AndroidPlannerHint:
    """Sanitized, action-free planner context, not a replay instruction."""

    candidate_id: str
    kind: AndroidExperienceKind
    action_kind: str
    semantic_anchor: str
    expected_scene_marker: str
    confidence: float
    support_count: int
    failure_count: int
    provenance_count: int
    retrieval_id: str
    scope: AuthenticatedAndroidScope
    provenance_ids: tuple[str, ...]
    retrieval_step_id: str
    retrieval_checkpoint_index: int
    retrieval_observation_digest: str


@dataclass(frozen=True, slots=True)
class AndroidExperienceProjection:
    """Coarse public projection for a v4 Android experience candidate."""

    schema_version: int
    experience_id: str
    kind: AndroidExperienceKind
    runner_kind: str
    status: AndroidCandidateStatus
    confidence: float
    support_count: int
    failure_count: int


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


@dataclass(frozen=True, slots=True)
class ScopedExperienceHint:
    """A planner-only, action-free description of one applicable experience."""

    experience_id: str
    kind: CandidateKind
    action_kind: str
    confidence: float
    support_count: int
    failure_count: int
    verified_provenance_count: int


@dataclass(frozen=True, slots=True)
class ExperienceProjection:
    """Safe external projection; deliberately excludes content and raw artifacts."""

    schema_version: int
    experience_id: str
    kind: CandidateKind
    application_id: str
    goal_family: str
    ui_version: str
    device_class: str
    orientation: str
    device_profile_id: str
    task_scoped: bool
    subtask_scoped: bool
    object_scoped: bool
    conversation_scoped: bool
    status: CandidateStatus
    confidence: float
    support_count: int
    failure_count: int
    verified_provenance_count: int
