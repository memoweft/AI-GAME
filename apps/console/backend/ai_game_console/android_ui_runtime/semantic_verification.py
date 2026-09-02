from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from .domain import (
    Anchor, CanonicalSnapshot, CriteriaRevision, CriterionVerdict, GoalVerificationRecord,
    ObservationArtifactGroundingPort, ObservationEnvelope, PrimitiveOutcome,
    TrustedObservationGroundingManifest, criteria_digest, observation_artifact_digest,
    observation_grounding_query, verification_reference,
)
from .role import FinalSemanticVerifierRole, PlanningContext
from .sanitizer import sanitize_summary


class SemanticVerificationError(ValueError):
    pass


def _build_witness_authority() -> tuple[
    Callable[[GoalVerificationRecord], GoalVerificationRecord],
    Callable[[GoalVerificationRecord], bool],
]:
    """Create an issuer whose witness class and seal never enter module globals."""

    seal = object()

    @dataclass(frozen=True, slots=True)
    class Witness:
        authority: object
        binding_digest: str

    def issue(record: GoalVerificationRecord) -> GoalVerificationRecord:
        if record.witness is not None:
            raise SemanticVerificationError("verification record is already witnessed")
        return replace(
            record,
            witness=Witness(seal, verification_reference(record)),
        )

    def validate(record: GoalVerificationRecord) -> bool:
        witness = record.witness
        return (
            isinstance(witness, Witness)
            and witness.authority is seal
            and witness.binding_digest == verification_reference(record)
        )

    return issue, validate


_MINT_VERIFICATION_RECORD, _is_verifier_issued = _build_witness_authority()
del _build_witness_authority


@dataclass(slots=True)
class EvidenceBoundSemanticVerifier:
    role: FinalSemanticVerifierRole
    model_version: str = "unknown"
    prompt_version: str = "android-ui-semantic-v1"
    grounding: ObservationArtifactGroundingPort | None = None

    def verify(
        self, *, snapshot: CanonicalSnapshot, runner_kind: str, runner_version: int,
        goal: str, criteria: CriteriaRevision, before: ObservationEnvelope | None,
        after: ObservationEnvelope, latest_step_index: int, already_satisfied: bool = False,
        causal_command_id: str | None = None,
        primitive_outcome: PrimitiveOutcome | None = None,
        __issuer: Callable[[GoalVerificationRecord], GoalVerificationRecord] = _MINT_VERIFICATION_RECORD,
    ) -> GoalVerificationRecord:
        if (
            criteria.revision != snapshot.revision
            or criteria.digest != criteria_digest(
                criteria.criteria,
                goal_digest=criteria.goal_digest,
                coverage_digest=criteria.coverage_digest,
                waypoints=criteria.waypoints,
                identity_scheme=criteria.identity_scheme,
            )
            or snapshot.runner_kind != runner_kind
            or snapshot.runner_version != runner_version
            or snapshot.runner_binding_id is None
        ):
            raise SemanticVerificationError("criteria revision or digest no longer matches canonical Task")
        self._binding(snapshot, after)
        if before is not None:
            self._binding(snapshot, before)
        if already_satisfied:
            if before is None or before != after:
                raise SemanticVerificationError(
                    "already_satisfied requires the exact fresh initial observation",
                )
            if causal_command_id is not None:
                raise SemanticVerificationError(
                    "already_satisfied cannot claim command causality",
                )
            # A later stable observation may still be attributed to the most
            # recent completed command.  The current zero-effect checkpoint
            # does not claim that command; the store independently validates
            # the predecessor before accepting a terminal record.
            primitive_outcome = "already_satisfied"
        else:
            if (
                before is None
                or causal_command_id is None
                or after.causality_command_id != causal_command_id
                or primitive_outcome not in {"progress", "no_progress", "uncertain"}
            ):
                raise SemanticVerificationError(
                    "effect verification requires a causal command and primitive outcome",
                )
        context = PlanningContext(snapshot.task_id, goal, snapshot.revision, criteria, after)
        if after.ui_tree_ref is None:
            # A screenshot alone is not a sufficiently structured source for
            # this v1 final verifier.  Preserve an honest unknown record.
            record = GoalVerificationRecord(
                task_id=snapshot.task_id, owner=snapshot.owner, runner_kind=runner_kind,
                runner_version=runner_version, revision=snapshot.revision, criteria_digest=criteria.digest,
                latest_step_index=latest_step_index, before=before, after=after,
                model_version=self.model_version, prompt_version=self.prompt_version,
                verdicts=tuple(CriterionVerdict(item.criterion_id, "unknown") for item in criteria.criteria),
                overall="unknown", already_satisfied=False,
                goal_digest=criteria.goal_digest,
                coverage_digest=criteria.coverage_digest,
                runner_binding_id=snapshot.runner_binding_id,
                artifact_digest=observation_artifact_digest(after),
                causal_command_id=causal_command_id,
                primitive_outcome=primitive_outcome,
            )
            return __issuer(record)
        verdicts = self.role.verify_goal(context, before=before, after=after, latest_step_index=latest_step_index, already_satisfied=already_satisfied)
        if not isinstance(verdicts, tuple):
            verdicts = tuple(verdicts)
        expected = {criterion.criterion_id for criterion in criteria.criteria}
        received = {item.criterion_id for item in verdicts}
        if received != expected or len(verdicts) != len(expected):
            raise SemanticVerificationError("semantic verifier must decide every frozen criterion exactly once")
        criterion_by_id = {criterion.criterion_id: criterion for criterion in criteria.criteria}
        grounding = None
        if any(item.state == "satisfied" for item in verdicts):
            grounding = self._resolve_grounding(
                snapshot=snapshot,
                observation=after,
                runner_kind=runner_kind,
                runner_version=runner_version,
                step_index=latest_step_index,
            )
        normalized_verdicts: list[CriterionVerdict] = []
        for verdict in verdicts:
            normalized_verdicts.append(self._validate_verdict(
                verdict, criterion_by_id[verdict.criterion_id], after, grounding,
            ))
        verdicts = tuple(normalized_verdicts)
        satisfied = bool(verdicts) and all(item.state == "satisfied" for item in verdicts)
        overall = "satisfied" if satisfied else ("unknown" if any(item.state == "unknown" for item in verdicts) else "unsatisfied")
        record = GoalVerificationRecord(
            task_id=snapshot.task_id, owner=snapshot.owner, runner_kind=runner_kind,
            runner_version=runner_version, revision=snapshot.revision, criteria_digest=criteria.digest,
            latest_step_index=latest_step_index, before=before, after=after,
            model_version=self.model_version, prompt_version=self.prompt_version,
            verdicts=tuple(CriterionVerdict(item.criterion_id, item.state, item.anchors, sanitize_summary(item.explanation, maximum=500)) for item in verdicts),
            overall=overall, already_satisfied=already_satisfied,
            goal_digest=criteria.goal_digest,
            coverage_digest=criteria.coverage_digest,
            runner_binding_id=snapshot.runner_binding_id,
            artifact_digest=observation_artifact_digest(after),
            grounding_digest=(grounding.grounding_digest if grounding is not None else None),
            causal_command_id=causal_command_id,
            primitive_outcome=primitive_outcome,
        )
        # The only minting sites are reached after every verifier binding and
        # criterion check above.  Runner/store callers receive no issuer that
        # accepts a caller-constructed record.
        return __issuer(record)

    @staticmethod
    def _binding(snapshot: CanonicalSnapshot, observation: ObservationEnvelope) -> None:
        if observation.task_id != snapshot.task_id:
            raise SemanticVerificationError("observation belongs to another task")
        if (
            snapshot.profile_id is None or snapshot.profile_generation is None
            or snapshot.boot_id is None or snapshot.canonical_device_id is None
            or observation.profile_id != snapshot.profile_id
            or observation.profile_generation != snapshot.profile_generation
            or observation.boot_id != snapshot.boot_id
            or observation.canonical_device_id != snapshot.canonical_device_id
        ):
            raise SemanticVerificationError("observation device binding does not match canonical Task")

    @staticmethod
    def _validate_verdict(
        verdict: CriterionVerdict, criterion: object, observation: ObservationEnvelope,
        grounding: TrustedObservationGroundingManifest | None,
    ) -> CriterionVerdict:
        required_markers = getattr(criterion, "required_evidence_markers", ())
        if verdict.state == "satisfied" and not required_markers:
            return CriterionVerdict(verdict.criterion_id, "unknown", (), "criterion has no trusted evidence marker")
        if verdict.state == "satisfied" and not verdict.anchors:
            raise SemanticVerificationError("satisfied criterion requires evidence anchors")
        for anchor in verdict.anchors:
            if anchor.observation_freshness_token != observation.freshness_token:
                raise SemanticVerificationError("anchor belongs to another observation")
            if anchor.kind == "ui_node":
                if anchor.node_id is None:
                    raise SemanticVerificationError("UI anchor needs node id")
                node = next((item for item in observation.ui_nodes if item.node_id == anchor.node_id), None)
                if node is None or (anchor.text is not None and anchor.text != node.text):
                    raise SemanticVerificationError("UI anchor is not present in this tree")
                if anchor.bounds is not None and anchor.bounds != node.bounds:
                    raise SemanticVerificationError("UI anchor bounds do not match")
                if verdict.state != "satisfied":
                    continue
                if grounding is None:
                    return CriterionVerdict(
                        verdict.criterion_id, "unknown", (),
                        "trusted observation grounding is unavailable",
                    )
                grounded = next(
                    (item for item in grounding.nodes if item.node_id == anchor.node_id), None,
                )
                if grounded is None:
                    return CriterionVerdict(
                        verdict.criterion_id, "unknown", (),
                        "anchor is absent from trusted observation grounding",
                    )
                if grounded.clickable and grounded.semantic_kind == "navigation":
                    return CriterionVerdict(verdict.criterion_id, "unknown", (), "navigation row cannot prove target page state")
                if grounded.semantic_kind not in {"page_title", "container", "state"}:
                    return CriterionVerdict(verdict.criterion_id, "unknown", (), "anchor is not a page or state marker")
                if (
                    grounded.semantic_marker not in required_markers
                    or anchor.semantic_marker != grounded.semantic_marker
                    or (anchor.bounds is not None and anchor.bounds != grounded.bounds)
                ):
                    return CriterionVerdict(verdict.criterion_id, "unknown", (), "anchor does not match criterion marker")
            elif anchor.kind == "screenshot_region":
                if anchor.bounds is None or len(anchor.bounds) != 4 or any(not 0 <= value <= 1 for value in anchor.bounds):
                    raise SemanticVerificationError("screenshot anchor requires normalized bounds")
                if verdict.state == "satisfied":
                    # The model can inspect a screenshot, but v1 has no
                    # independently grounded pixel-to-semantic-marker ledger.
                    # Accepting a marker echoed by the same model would let an
                    # unchanged Settings-home image impersonate any target
                    # page.  Until a trusted vision grounding adapter exists,
                    # only a current UI-tree node can prove satisfaction.
                    return CriterionVerdict(
                        verdict.criterion_id,
                        "unknown",
                        (),
                        "screenshot-only evidence cannot prove a semantic goal",
                    )
            else:
                raise SemanticVerificationError("unknown evidence anchor")
        return verdict

    def _resolve_grounding(
        self, *, snapshot: CanonicalSnapshot, observation: ObservationEnvelope,
        runner_kind: str, runner_version: int, step_index: int,
    ) -> TrustedObservationGroundingManifest | None:
        if self.grounding is None or observation.ui_tree_digest is None:
            return None
        query = observation_grounding_query(
            snapshot=snapshot,
            observation=observation,
            runner_kind=runner_kind,
            runner_version=runner_version,
            step_index=step_index,
        )
        manifest = self.grounding.resolve(query)
        if (
            manifest is None
            or manifest.query != query
            or not self.grounding.validates(manifest)
        ):
            return None
        return manifest


# The issuer survives only in ``verify``'s evaluated default closure.  Store
# code receives the validator, while ordinary modules cannot import a minting
# function, witness type, or authority seal.
del _MINT_VERIFICATION_RECORD
