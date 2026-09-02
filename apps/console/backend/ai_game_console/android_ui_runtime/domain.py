from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

from .sanitizer import sanitize_summary, sanitize_task_goal

ActionKind = Literal[
    "tap", "long_press", "swipe", "input_text", "back", "home", "recents", "open_app", "wait"
]
CriterionState = Literal["satisfied", "unsatisfied", "unknown"]
DecisionKind = Literal["action", "terminal_candidate", "replan"]
PrimitiveOutcome = Literal["already_satisfied", "progress", "no_progress", "uncertain"]
_OPAQUE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}")
_DIGEST = re.compile(r"[a-f0-9]{32,128}")
_OPAQUE_NODE_ID = re.compile(r"node_[a-f0-9]{32,128}")
_SEMANTIC_MARKER = re.compile(r"(?:page_title|container|state):[a-f0-9]{64}")
CRITERIA_IDENTITY_CURRENT = "description_digest_v1"
CRITERIA_IDENTITY_LEGACY_HASHED = "legacy_description_hash_v1"
CRITERIA_IDENTITY_LEGACY_RAW = "legacy_raw_description_v1"
_CRITERIA_IDENTITY_SCHEMES = frozenset({
    CRITERIA_IDENTITY_CURRENT,
    CRITERIA_IDENTITY_LEGACY_HASHED,
    CRITERIA_IDENTITY_LEGACY_RAW,
})


@dataclass(frozen=True, slots=True)
class OwnerBinding:
    """Private binding; never include this dataclass in a public projection."""
    principal_id: str
    controller_id: str


@dataclass(frozen=True, slots=True)
class CanonicalSnapshot:
    task_id: str
    owner: OwnerBinding
    revision: int
    status: str
    allowed_controls: tuple[str, ...] = ()
    profile_id: str | None = None
    profile_generation: int | None = None
    boot_id: str | None = None
    canonical_device_id: str | None = None
    runner_kind: str | None = None
    runner_version: int | None = None
    runner_binding_id: str | None = None

    @property
    def runnable(self) -> bool:
        return self.status in {"scheduled", "running", "recovering", "replanning", "waiting_time"}


@dataclass(frozen=True, slots=True)
class UiNode:
    node_id: str
    text: str = ""
    bounds: tuple[float, float, float, float] | None = None
    clickable: bool = False
    semantic_kind: Literal["unknown", "navigation", "page_title", "container", "state"] = "unknown"
    semantic_marker: str | None = None

    def __post_init__(self) -> None:
        if not _OPAQUE_NODE_ID.fullmatch(self.node_id):
            raise ValueError("UI node id must be a trusted opaque digest")
        # Nodes live only in the in-memory verifier/model envelope.  Still
        # sanitize them here so a caller cannot accidentally hand private raw
        # tree text to a role or an anchor comparison.
        object.__setattr__(self, "text", sanitize_summary(self.text, maximum=300))
        if self.semantic_marker is not None:
            if not _SEMANTIC_MARKER.fullmatch(self.semantic_marker):
                raise ValueError("UI node semantic marker is invalid")
            if self.semantic_kind == "unknown" or not self.semantic_marker.startswith(self.semantic_kind + ":"):
                raise ValueError("UI node marker must match its semantic kind")
        if self.bounds is not None:
            bounds = tuple(self.bounds)
            if len(bounds) != 4 or any(not 0 <= item <= 1 for item in bounds):
                raise ValueError("UI bounds must be normalized")
            object.__setattr__(self, "bounds", bounds)


@dataclass(frozen=True, slots=True)
class ObservationEnvelope:
    task_id: str
    profile_id: str
    profile_generation: int
    boot_id: str
    canonical_device_id: str
    screenshot_ref: str
    screenshot_digest: str
    ui_tree_ref: str | None
    ui_tree_digest: str | None
    device_state_ref: str
    device_state_digest: str
    observed_at: str
    freshness_token: str
    ui_summary: str = ""
    ui_nodes: tuple[UiNode, ...] = ()
    causality_command_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ui_nodes, tuple):
            raise ValueError("UI nodes must use an immutable tuple")
        nodes = self.ui_nodes
        if any(not isinstance(item, UiNode) for item in nodes):
            raise ValueError("UI nodes must use the immutable UiNode contract")
        required = (
            self.task_id, self.profile_id, self.boot_id, self.canonical_device_id,
            self.screenshot_ref, self.screenshot_digest, self.device_state_ref,
            self.device_state_digest, self.observed_at, self.freshness_token,
        )
        if any(not isinstance(item, str) or not item.strip() for item in required):
            raise ValueError("observation envelope is missing an opaque reference")
        for ref in (self.screenshot_ref, self.ui_tree_ref, self.device_state_ref, self.causality_command_id):
            if ref is not None and not _OPAQUE_REF.fullmatch(ref):
                raise ValueError("artifact reference must be an opaque identifier")
        for digest in (self.screenshot_digest, self.ui_tree_digest, self.device_state_digest):
            if digest is not None and not _DIGEST.fullmatch(digest):
                raise ValueError("artifact digest is invalid")
        if self.profile_generation < 1:
            raise ValueError("profile generation must be positive")
        if (self.ui_tree_ref is None) != (self.ui_tree_digest is None):
            raise ValueError("UI tree ref and digest must be paired")
        if self.ui_tree_ref is None and self.ui_nodes:
            raise ValueError("UI nodes require an available UI tree")
        if len(self.ui_nodes) > 128:
            raise ValueError("too many UI nodes")
        object.__setattr__(self, "ui_summary", sanitize_summary(self.ui_summary, maximum=1_200))

    def private_record(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "profile_id_digest": opaque_digest("profile-id", self.profile_id),
            "profile_generation": self.profile_generation,
            "boot_id_digest": opaque_digest("boot-id", self.boot_id),
            "canonical_device_id_digest": opaque_digest(
                "canonical-device-id", self.canonical_device_id,
            ),
            "screenshot_ref": self.screenshot_ref, "screenshot_digest": self.screenshot_digest,
            "ui_tree_ref": self.ui_tree_ref, "ui_tree_digest": self.ui_tree_digest,
            "device_state_ref": self.device_state_ref, "device_state_digest": self.device_state_digest,
            "observed_at": self.observed_at, "freshness_token": self.freshness_token,
            # Raw UI-tree nodes never enter durable step JSON.  They remain in
            # this in-memory envelope for the immediate model/verifier call;
            # artifacts are reloaded through ArtifactLoader after a restart.
        }


@dataclass(frozen=True, slots=True, eq=False)
class Criterion:
    criterion_id: str
    description: str
    required_evidence_markers: tuple[str, ...] = ()
    description_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.criterion_id or not self.description:
            raise ValueError("criterion is incomplete")
        if any(not _SEMANTIC_MARKER.fullmatch(item) for item in self.required_evidence_markers):
            raise ValueError("criterion evidence marker is invalid")
        if self.description_digest is not None and not _DIGEST.fullmatch(self.description_digest):
            raise ValueError("criterion description digest is invalid")

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Criterion)
            and self.criterion_id == other.criterion_id
            and self.required_evidence_markers == other.required_evidence_markers
            and criterion_description_digest(self)
            == criterion_description_digest(other)
        )

    def __hash__(self) -> int:
        return hash((
            self.criterion_id,
            self.required_evidence_markers,
            criterion_description_digest(self),
        ))


@dataclass(frozen=True, slots=True)
class OrderedWaypoint:
    """One immutable, evidence-bound prerequisite in a frozen route.

    ``source_digest`` binds the waypoint to the exact canonical goal clause
    without persisting that clause's UI text.  The criteria are deliberately
    the same typed marker contract used by K2 terminal verification; a route
    never advances from a planner assertion alone.
    """

    waypoint_id: str
    ordinal: int
    source_digest: str
    criteria: tuple[Criterion, ...]
    digest: str

    def __post_init__(self) -> None:
        if (
            not _OPAQUE_REF.fullmatch(self.waypoint_id)
            or self.ordinal < 1
            or not _DIGEST.fullmatch(self.source_digest)
            or not self.criteria
            or any(not isinstance(item, Criterion) for item in self.criteria)
            or len({item.criterion_id for item in self.criteria}) != len(self.criteria)
            or self.digest != criteria_digest(self.criteria)
        ):
            raise ValueError("ordered waypoint is invalid")


@dataclass(frozen=True, slots=True)
class CriteriaRevision:
    revision: int
    criteria: tuple[Criterion, ...]
    digest: str
    goal_digest: str | None = None
    coverage_digest: str | None = None
    waypoints: tuple[OrderedWaypoint, ...] = ()
    identity_scheme: str = CRITERIA_IDENTITY_CURRENT

    def __post_init__(self) -> None:
        if (
            self.revision < 1
            or not self.criteria
            or not isinstance(self.digest, str)
            or not self.digest
            or self.identity_scheme not in _CRITERIA_IDENTITY_SCHEMES
        ):
            raise ValueError("criteria revision is incomplete")
        if (self.goal_digest is None) != (self.coverage_digest is None):
            raise ValueError("goal and coverage digests must be supplied together")
        if self.goal_digest is not None and (
            not _DIGEST.fullmatch(self.goal_digest)
            or not _DIGEST.fullmatch(str(self.coverage_digest))
        ):
            raise ValueError("goal coverage digests must be opaque")
        waypoints = tuple(self.waypoints)
        if (
            any(not isinstance(item, OrderedWaypoint) for item in waypoints)
            or tuple(item.ordinal for item in waypoints) != tuple(range(1, len(waypoints) + 1))
            or len({item.waypoint_id for item in waypoints}) != len(waypoints)
        ):
            raise ValueError("criteria waypoints must be strictly ordered")
        object.__setattr__(self, "waypoints", waypoints)

    def waypoint_criteria(self, ordinal: int) -> "CriteriaRevision":
        """Return the exact evidence contract for one unfinished waypoint.

        This derived view is never frozen as a replacement Task revision.  Its
        digest is carried by the immutable parent revision and only feeds the
        existing evidence-bound semantic verifier for the current step.
        """

        waypoint = next((item for item in self.waypoints if item.ordinal == ordinal), None)
        if waypoint is None:
            raise ValueError("unknown ordered waypoint")
        return CriteriaRevision(
            self.revision, waypoint.criteria, waypoint.digest,
            identity_scheme=self.identity_scheme,
        )


def compile_criteria(goal: str, *, revision: int, compiler: "CriteriaCompiler | None" = None, marker_factory: "EvidenceMarkerFactory | None" = None) -> CriteriaRevision:
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must not be blank")
    source = compiler.compile(goal.strip()) if compiler is not None else (goal.strip(),)
    normalized = tuple(Criterion(
        f"c{index + 1}", sanitize_task_goal(item, maximum=400),
        tuple(evidence_marker(kind, value) for kind, value in marker_factory.required_markers(goal.strip(), item)) if marker_factory is not None else (),
    ) for index, item in enumerate(source))
    if not normalized or any(not item.description or item.description == "[redacted]" for item in normalized):
        raise ValueError("criteria must be observable and non-sensitive")
    return CriteriaRevision(revision, normalized, criteria_digest(normalized))


def criteria_digest(
    criteria: tuple[Criterion, ...], *, goal_digest: str | None = None,
    coverage_digest: str | None = None,
    waypoints: tuple[OrderedWaypoint, ...] = (),
    identity_scheme: str = CRITERIA_IDENTITY_CURRENT,
) -> str:
    if identity_scheme not in _CRITERIA_IDENTITY_SCHEMES:
        raise ValueError("criteria identity scheme is invalid")
    encoded_criteria = [
        _criterion_digest_projection(item, identity_scheme)
        for item in criteria
    ]
    # Preserve the v1 K2 digest for pure/unit callers.  Production criteria use
    # the typed goal+coverage form below, so a truncated coverage map cannot be
    # substituted while retaining the same frozen CriteriaRevision identity.
    value: Any = encoded_criteria
    if goal_digest is not None or coverage_digest is not None or waypoints:
        if (goal_digest is None) != (coverage_digest is None):
            raise ValueError("criteria goal coverage digests are invalid")
        if goal_digest is not None and (
            not _DIGEST.fullmatch(goal_digest)
            or not _DIGEST.fullmatch(coverage_digest)
        ):
            raise ValueError("criteria goal coverage digests are invalid")
        if goal_digest is None or coverage_digest is None:
            if waypoints:
                value = {
                    "criteria": encoded_criteria,
                    "waypoints": _waypoint_digest_payload(waypoints),
                }
            else:
                raise ValueError("criteria goal coverage digests are invalid")
        else:
            value = {
            "criteria": encoded_criteria,
            "goal_digest": goal_digest,
            "coverage_digest": coverage_digest,
            }
            if waypoints:
                value["waypoints"] = _waypoint_digest_payload(waypoints)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _criterion_digest_projection(
    criterion: Criterion, identity_scheme: str,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": criterion.criterion_id,
        "required_evidence_markers": criterion.required_evidence_markers,
    }
    if identity_scheme == CRITERIA_IDENTITY_LEGACY_RAW:
        # Read-only compatibility for the pre-description-digest payload.
        # New SQLite rows never use this representation.
        value["description"] = criterion.description
    else:
        value["description_digest"] = criterion_description_digest(criterion)
    return value


def _waypoint_digest_payload(
    waypoints: tuple[OrderedWaypoint, ...],
) -> list[dict[str, Any]]:
    if tuple(item.ordinal for item in waypoints) != tuple(range(1, len(waypoints) + 1)):
        raise ValueError("ordered waypoint sequence is invalid")
    return [
        {
            "waypoint_id": item.waypoint_id,
            "ordinal": item.ordinal,
            "source_digest": item.source_digest,
            "criteria_digest": item.digest,
        }
        for item in waypoints
    ]


def criterion_description_digest(criterion: Criterion) -> str:
    return criterion.description_digest or opaque_digest(
        "criterion-description", criterion.description,
    )




def criteria_coverage_digest(
    coverage: tuple[Mapping[str, Any], ...],
) -> str:
    """Hash the bounded, text-free goal-to-marker coverage proof."""

    normalized: list[dict[str, Any]] = []
    for item in coverage:
        value = {
            "criterion_id": item.get("criterion_id"),
            "clause_index": item.get("clause_index"),
            "source_start": item.get("source_start"),
            "source_end": item.get("source_end"),
            "source_digest": item.get("source_digest"),
            "semantic_marker": item.get("semantic_marker"),
        }
        if (
            not isinstance(value["criterion_id"], str)
            or not value["criterion_id"]
            or any(
                isinstance(value[name], bool)
                or not isinstance(value[name], int)
                or value[name] < 0
                for name in ("clause_index", "source_start", "source_end")
            )
            or value["source_end"] <= value["source_start"]
            or not isinstance(value["source_digest"], str)
            or not _DIGEST.fullmatch(value["source_digest"])
            or not isinstance(value["semantic_marker"], str)
            or not _SEMANTIC_MARKER.fullmatch(value["semantic_marker"])
        ):
            raise ValueError("criteria coverage proof is invalid")
        normalized.append(value)
    if not normalized:
        raise ValueError("criteria coverage proof must not be empty")
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CriteriaCompiler(Protocol):
    def compile(self, goal: str) -> tuple[str, ...]: ...


class EvidenceMarkerFactory(Protocol):
    def required_markers(self, goal: str, criterion: str) -> tuple[tuple[Literal["page_title", "container", "state"], str], ...]: ...


def evidence_marker(kind: Literal["page_title", "container", "state"], semantic_value: str) -> str:
    normalized = " ".join(semantic_value.casefold().split())
    if not normalized or len(normalized) > 800:
        raise ValueError("semantic marker value is invalid")
    return f"{kind}:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def opaque_node_id(adapter_node_identity: str) -> str:
    """Convert an adapter's raw resource/XPath identity before UI roles see it."""
    if not isinstance(adapter_node_identity, str) or not adapter_node_identity:
        raise ValueError("node identity is invalid")
    return "node_" + hashlib.sha256(adapter_node_identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AndroidUiAction:
    kind: ActionKind
    arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        args = dict(self.arguments)
        if self.kind in {"tap", "long_press"}:
            _coordinates(args, ("x", "y"))
        elif self.kind == "swipe":
            _coordinates(args, ("x", "y", "end_x", "end_y"))
        elif self.kind == "input_text":
            if not isinstance(args.get("text"), str) or not args["text"] or len(args["text"]) > 4_096:
                raise ValueError("input_text requires bounded text")
        elif self.kind == "open_app":
            package = args.get("package")
            if not isinstance(package, str) or not _ANDROID_PACKAGE.fullmatch(package) or len(package) > 200:
                raise ValueError("open_app requires a package")
        elif self.kind == "wait":
            seconds = args.get("seconds")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= 10:
                raise ValueError("wait must be between 0 and 10 seconds")
        elif args:
            raise ValueError("system action must not carry arguments")


def _coordinates(args: Mapping[str, Any], names: tuple[str, ...]) -> None:
    for name in names:
        value = args.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be a normalized coordinate")


_ANDROID_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")


@dataclass(frozen=True, slots=True)
class AndroidUiActionIntent:
    action_intent_id: str
    task_id: str
    criteria_revision: int
    step_index: int
    action: AndroidUiAction

    @classmethod
    def create(cls, *, task_id: str, criteria_revision: int, step_index: int, action: AndroidUiAction) -> "AndroidUiActionIntent":
        # Never include model prose, process state, time, or wake identity.
        stable = json.dumps({"task_id": task_id, "criteria_revision": criteria_revision, "step_index": step_index, "action": action.kind, "arguments": _stable_action_arguments(action)}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(hashlib.sha256(stable.encode()).hexdigest(), task_id, criteria_revision, step_index, action)


def _stable_action_arguments(action: AndroidUiAction) -> dict[str, Any]:
    from .sanitizer import safe_action_payload
    return safe_action_payload(action.kind, action.arguments)


@dataclass(frozen=True, slots=True)
class RoleDecision:
    kind: DecisionKind
    action: AndroidUiAction | None = None
    reason: str = ""
    terminal_summary: str = ""
    selected_hint_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "action" and self.action is None:
            raise ValueError("action decision requires an action")
        if self.kind != "action" and self.action is not None:
            raise ValueError("only action decision may carry action")
        if self.selected_hint_id is not None and (
            self.kind != "action"
            or not isinstance(self.selected_hint_id, str)
            or not _OPAQUE_REF.fullmatch(self.selected_hint_id)
        ):
            raise ValueError("selected experience hint must be an opaque action selection")


@dataclass(frozen=True, slots=True)
class Anchor:
    kind: Literal["ui_node", "screenshot_region"]
    observation_freshness_token: str
    node_id: str | None = None
    text: str | None = None
    bounds: tuple[float, float, float, float] | None = None
    semantic_marker: str | None = None

    def __post_init__(self) -> None:
        if self.node_id is not None and not _OPAQUE_NODE_ID.fullmatch(self.node_id):
            raise ValueError("anchor node id must be opaque")
        if self.semantic_marker is not None and not _SEMANTIC_MARKER.fullmatch(self.semantic_marker):
            raise ValueError("anchor semantic marker is invalid")
        if self.bounds is not None:
            bounds = tuple(self.bounds)
            if len(bounds) != 4 or any(not 0 <= item <= 1 for item in bounds):
                raise ValueError("anchor bounds must be normalized")
            object.__setattr__(self, "bounds", bounds)


@dataclass(frozen=True, slots=True)
class CriterionVerdict:
    criterion_id: str
    state: CriterionState
    anchors: tuple[Anchor, ...] = ()
    explanation: str = ""

    def __post_init__(self) -> None:
        anchors = tuple(self.anchors)
        if any(not isinstance(item, Anchor) for item in anchors):
            raise ValueError("criterion anchors must use the immutable Anchor contract")
        object.__setattr__(self, "anchors", anchors)


@dataclass(frozen=True, slots=True)
class GoalVerificationRecord:
    task_id: str
    owner: OwnerBinding
    runner_kind: str
    runner_version: int
    revision: int
    criteria_digest: str
    latest_step_index: int
    before: ObservationEnvelope | None
    after: ObservationEnvelope
    model_version: str
    prompt_version: str
    verdicts: tuple[CriterionVerdict, ...]
    overall: CriterionState
    already_satisfied: bool = False
    goal_digest: str | None = None
    coverage_digest: str | None = None
    runner_binding_id: str | None = None
    artifact_digest: str | None = None
    grounding_digest: str | None = None
    causal_command_id: str | None = None
    primitive_outcome: PrimitiveOutcome | None = None
    # The concrete witness type and minting authority live inside the semantic
    # verifier module.  Keeping only an opaque slot here means an ordinary
    # runner/store caller has no public constructor or issue function.
    witness: object | None = field(default=None, repr=False, compare=False)

    def safe_projection(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "runner_kind": self.runner_kind,
            "runner_version": self.runner_version, "revision": self.revision,
            "criteria_digest": self.criteria_digest, "latest_step_index": self.latest_step_index,
            "goal_digest": self.goal_digest, "coverage_digest": self.coverage_digest,
            "overall": self.overall, "already_satisfied": self.already_satisfied,
            "verdicts": [
                {"criterion_id": item.criterion_id, "state": item.state,
                 "anchors": [{"kind": anchor.kind, "freshness": opaque_digest("observation-freshness", anchor.observation_freshness_token), "node_id": anchor.node_id, "semantic_marker": anchor.semantic_marker} for anchor in item.anchors]}
                for item in self.verdicts
            ],
        }

    def private_durable_record(self) -> dict[str, Any]:
        """Audit facts without raw artifact refs, UI text, or owner ids."""
        return {
            **self.safe_projection(),
            "verification_ref": verification_reference(self),
            "owner_scope_digest": owner_scope_digest(self.owner),
            "runner_binding_digest": opaque_digest("runner-binding", self.runner_binding_id),
            "model_version": opaque_digest("model-version", self.model_version),
            "prompt_version": opaque_digest("prompt-version", self.prompt_version),
            "artifact_digest": self.artifact_digest,
            "grounding_digest": self.grounding_digest,
            "causal_command_id": self.causal_command_id,
            "primitive_outcome": self.primitive_outcome,
            "before": _durable_observation(self.before), "after": _durable_observation(self.after),
        }


def _durable_observation(observation: ObservationEnvelope | None) -> dict[str, Any] | None:
    if observation is None:
        return None
    return {
        "profile_id_digest": opaque_digest("profile-id", observation.profile_id),
        "profile_generation": observation.profile_generation,
        "boot_id_digest": opaque_digest("boot-id", observation.boot_id),
        "canonical_device_id_digest": opaque_digest(
            "canonical-device-id", observation.canonical_device_id,
        ),
        "screenshot_digest": observation.screenshot_digest, "ui_tree_digest": observation.ui_tree_digest,
        "device_state_digest": observation.device_state_digest,
        "freshness_digest": opaque_digest("observation-freshness", observation.freshness_token),
        "causality_command_id": observation.causality_command_id,
    }


def owner_scope_digest(owner: OwnerBinding) -> str:
    return _domain_digest("owner-scope", owner.principal_id, owner.controller_id)


def opaque_digest(domain: str, value: object) -> str:
    return _domain_digest(domain, str(value))


def _domain_digest(domain: str, *parts: str) -> str:
    payload = json.dumps([domain, *parts], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verification_reference(record: GoalVerificationRecord) -> str:
    return _domain_digest("verification-reference", _verification_binding_digest(record))


def _verification_binding_digest(record: GoalVerificationRecord) -> str:
    before = _durable_observation(record.before)
    after = _durable_observation(record.after)
    payload = json.dumps({
        "task": record.task_id,
        "owner": owner_scope_digest(record.owner),
        "runner": [record.runner_kind, record.runner_version],
        "runner_binding": opaque_digest("runner-binding", record.runner_binding_id),
        "revision": record.revision,
        "criteria": record.criteria_digest,
        "goal": record.goal_digest,
        "coverage": record.coverage_digest,
        "step": record.latest_step_index,
        "before": before,
        "after": after,
        "before_node_projection": _ui_node_projection_digest(record.before),
        "after_node_projection": _ui_node_projection_digest(record.after),
        "artifact": record.artifact_digest,
        "grounding": record.grounding_digest,
        "causal_command": record.causal_command_id,
        "primitive_outcome": record.primitive_outcome,
        "model_version": opaque_digest("model-version", record.model_version),
        "prompt_version": opaque_digest("prompt-version", record.prompt_version),
        "overall": record.overall,
        "already_satisfied": record.already_satisfied,
        "verdicts": [
            {"id": item.criterion_id, "state": item.state,
             "anchors": [{"kind": anchor.kind, "freshness": opaque_digest("anchor-freshness", anchor.observation_freshness_token), "node": anchor.node_id, "marker": anchor.semantic_marker, "bounds": anchor.bounds} for anchor in item.anchors]}
            for item in record.verdicts
        ],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ui_node_projection_digest(observation: ObservationEnvelope | None) -> str | None:
    """Witness-only text-free binding for the immutable in-memory node container."""

    if observation is None:
        return None
    encoded = json.dumps([
        {
            "node_id": item.node_id,
            "semantic_kind": item.semantic_kind,
            "semantic_marker": item.semantic_marker,
            "clickable": item.clickable,
            "bounds": item.bounds,
        }
        for item in observation.ui_nodes
    ], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return opaque_digest("ui-node-projection", encoded)


def observation_artifact_digest(observation: ObservationEnvelope) -> str:
    """Bind the exact screenshot/UI-tree/device-state artifact set."""

    return _domain_digest(
        "observation-artifacts",
        observation.screenshot_digest,
        observation.ui_tree_digest or "unavailable",
        observation.device_state_digest,
    )


@dataclass(frozen=True, slots=True)
class GroundedUiNode:
    """Text-free node facts issued by the trusted artifact grounding adapter."""

    node_id: str
    semantic_kind: Literal["unknown", "navigation", "page_title", "container", "state"]
    semantic_marker: str | None
    clickable: bool
    bounds: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if not _OPAQUE_NODE_ID.fullmatch(self.node_id):
            raise ValueError("grounded node id must be opaque")
        if self.semantic_marker is not None:
            if not _SEMANTIC_MARKER.fullmatch(self.semantic_marker):
                raise ValueError("grounded semantic marker is invalid")
            if not self.semantic_marker.startswith(self.semantic_kind + ":"):
                raise ValueError("grounded marker must match semantic kind")
        if self.bounds is not None:
            bounds = tuple(self.bounds)
            if len(bounds) != 4 or any(not 0 <= item <= 1 for item in bounds):
                raise ValueError("grounded bounds must be normalized")
            object.__setattr__(self, "bounds", bounds)


@dataclass(frozen=True, slots=True)
class ObservationGroundingQuery:
    """Exact identity/artifact request presented to the trusted grounding port."""

    task_id: str
    owner_scope_digest: str
    runner_kind: str
    runner_version: int
    runner_binding_digest: str
    revision: int
    step_index: int
    after_observation_digest: str
    artifact_digest: str
    ui_tree_artifact_digest: str
    freshness_digest: str

    def __post_init__(self) -> None:
        if (
            not _OPAQUE_REF.fullmatch(self.task_id)
            or not _OPAQUE_REF.fullmatch(self.runner_kind)
            or self.runner_version < 1
            or self.revision < 1
            or self.step_index < 0
        ):
            raise ValueError("grounding query identity is invalid")
        for digest in (
            self.owner_scope_digest,
            self.runner_binding_digest,
            self.after_observation_digest,
            self.artifact_digest,
            self.ui_tree_artifact_digest,
            self.freshness_digest,
        ):
            if not _DIGEST.fullmatch(digest):
                raise ValueError("grounding query digest is invalid")


@dataclass(frozen=True, slots=True)
class TrustedObservationGroundingManifest:
    """Immutable, text-free authority result for one exact observation tree."""

    query: ObservationGroundingQuery
    nodes: tuple[GroundedUiNode, ...]
    grounding_digest: str
    _attestation: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        if any(not isinstance(item, GroundedUiNode) for item in nodes):
            raise ValueError("grounding manifest contains an invalid node")
        if len({item.node_id for item in nodes}) != len(nodes):
            raise ValueError("grounding manifest contains duplicate node ids")
        object.__setattr__(self, "nodes", nodes)
        if self.grounding_digest != grounding_manifest_digest(self.query, nodes):
            raise ValueError("grounding manifest digest is invalid")


class ObservationArtifactGroundingPort(Protocol):
    """K2 read boundary; K4 supplies the RuntimeKernel/artifact adapter later."""

    def resolve(
        self, query: ObservationGroundingQuery,
    ) -> TrustedObservationGroundingManifest | None: ...

    def validates(self, manifest: TrustedObservationGroundingManifest) -> bool: ...


def observation_grounding_query(
    *, snapshot: CanonicalSnapshot, observation: ObservationEnvelope,
    runner_kind: str, runner_version: int, step_index: int,
) -> ObservationGroundingQuery:
    """Build the exact, digest-only grounding lookup identity."""

    if snapshot.runner_binding_id is None or observation.ui_tree_digest is None:
        raise ValueError("grounding query requires runner binding and UI-tree artifact")
    durable = _durable_observation(observation)
    assert durable is not None
    encoded = json.dumps(durable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ObservationGroundingQuery(
        task_id=snapshot.task_id,
        owner_scope_digest=owner_scope_digest(snapshot.owner),
        runner_kind=runner_kind,
        runner_version=runner_version,
        runner_binding_digest=opaque_digest("runner-binding", snapshot.runner_binding_id),
        revision=snapshot.revision,
        step_index=step_index,
        after_observation_digest=opaque_digest("durable-observation", encoded),
        artifact_digest=observation_artifact_digest(observation),
        ui_tree_artifact_digest=observation.ui_tree_digest,
        freshness_digest=opaque_digest("observation-freshness", observation.freshness_token),
    )


def grounding_manifest_digest(
    query: ObservationGroundingQuery, nodes: tuple[GroundedUiNode, ...],
) -> str:
    """Bind scope, runner, revision, step, artifact, freshness, and exact node facts."""

    payload = {
        "query": {
            "task_id": query.task_id,
            "owner_scope_digest": query.owner_scope_digest,
            "runner_kind": query.runner_kind,
            "runner_version": query.runner_version,
            "runner_binding_digest": query.runner_binding_digest,
            "revision": query.revision,
            "step_index": query.step_index,
            "after_observation_digest": query.after_observation_digest,
            "artifact_digest": query.artifact_digest,
            "ui_tree_artifact_digest": query.ui_tree_artifact_digest,
            "freshness_digest": query.freshness_digest,
        },
        "nodes": [
            {
                "node_id": item.node_id,
                "semantic_kind": item.semantic_kind,
                "semantic_marker": item.semantic_marker,
                "clickable": item.clickable,
                "bounds": item.bounds,
            }
            for item in nodes
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return opaque_digest("observation-grounding", encoded)


class ArtifactLoader(Protocol):
    def load_for_model(self, observation: ObservationEnvelope) -> Mapping[str, Any]: ...
