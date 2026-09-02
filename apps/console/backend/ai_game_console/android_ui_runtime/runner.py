"""One durable Android-UI step; K4 composes it into the sole EventPump."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .domain import AndroidUiAction, AndroidUiActionIntent, CanonicalSnapshot, CriteriaRevision, GoalVerificationRecord, ObservationEnvelope, RoleDecision, verification_reference
from .role import ActionVerificationContext, ActionVerifierRole, ActorRole, PlannerRole, ReflectionContext, ReflectionRole, PlanningContext
from .sanitizer import sanitize_task_goal, sanitize_text
from .semantic_verification import EvidenceBoundSemanticVerifier
from .store import SQLiteAndroidUiStepStore

_OPAQUE = re.compile(r"(?:[a-f0-9]{32,128}|[A-Za-z0-9][A-Za-z0-9_-]{0,255})")
_SCENE_MARKER = re.compile(r"scene:[a-f0-9]{32}")
_EXPERIENCE_KINDS = {"progress", "positive", "negative", "recovery"}
_ACTION_KINDS = {
    "tap", "long_press", "swipe", "input_text", "back", "home",
    "recents", "open_app", "wait",
}


class CanonicalReader(Protocol):
    def inspect(self, task_id: str) -> CanonicalSnapshot: ...


class ObservationProvider(Protocol):
    def observe(self, snapshot: CanonicalSnapshot) -> ObservationEnvelope: ...


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    """K1's durable command claim, not a claim of semantic success."""
    command_id: str
    action_intent_id: str

    def __post_init__(self) -> None:
        if not _OPAQUE.fullmatch(self.command_id) or not _OPAQUE.fullmatch(self.action_intent_id):
            raise ValueError("dispatch receipt must contain opaque identities")


class DispatchPort(Protocol):
    def dispatch(self, snapshot: CanonicalSnapshot, intent: AndroidUiActionIntent) -> DispatchReceipt: ...
    def reconcile(self, snapshot: CanonicalSnapshot, intent: AndroidUiActionIntent) -> DispatchReceipt: ...


@dataclass(frozen=True, slots=True)
class ExperienceHint:
    """Sanitized planner context plus opaque durable retrieval attribution.

    This value deliberately carries no coordinates, typed text, raw UI text,
    artifact reference, or executable callback.  It can inform a fresh plan,
    but it cannot replay an action or bypass dispatch/verification.
    """

    candidate_id: str
    retrieval_id: str
    kind: str
    action_kind: str
    semantic_anchor: str
    expected_scene_marker: str
    confidence: float
    support_count: int
    failure_count: int
    provenance_count: int

    def __post_init__(self) -> None:
        cleaned = sanitize_text(self.semantic_anchor, maximum=240)
        if (
            not _OPAQUE.fullmatch(self.candidate_id)
            or not _OPAQUE.fullmatch(self.retrieval_id)
            or self.kind not in _EXPERIENCE_KINDS
            or self.action_kind not in _ACTION_KINDS
            or cleaned.do_not_learn
            or not cleaned.text
            or cleaned.text != self.semantic_anchor
            or not _SCENE_MARKER.fullmatch(self.expected_scene_marker)
            or not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not 0.0 <= float(self.confidence) <= 1.0
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in (self.support_count, self.failure_count, self.provenance_count)
            )
        ):
            raise ValueError("Android experience hint is not a safe bounded projection")

    def planner_projection(self) -> Mapping[str, Any]:
        return {
            # Opaque ID only.  The actor may explicitly select this hint but
            # never receives a coordinate, typed text, or executable action.
            "hint_id": self.candidate_id,
            "kind": self.kind,
            "action_kind": self.action_kind,
            "semantic_anchor": self.semantic_anchor,
            "expected_scene_marker": self.expected_scene_marker,
            "confidence": float(self.confidence),
            "support_count": self.support_count,
            "failure_count": self.failure_count,
            "provenance_count": self.provenance_count,
        }

    def durable_attribution(self, *, selected: bool = False) -> Mapping[str, str | bool]:
        return {
            "candidate_id": self.candidate_id,
            "provenance": self.retrieval_id,
            "selected": selected,
        }


class ExperiencePort(Protocol):
    """Narrow, non-authoritative K3 seam; every failure must degrade cold."""

    def retrieve(
        self, *, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
        observation: ObservationEnvelope, step_id: str, checkpoint_index: int,
    ) -> tuple[ExperienceHint, ...]: ...

    def record_step(
        self, *, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
        step_id: str, checkpoint_index: int, action: AndroidUiAction,
        before: ObservationEnvelope, after: ObservationEnvelope,
        verification: GoalVerificationRecord, primitive_outcome: str,
        hints: tuple[ExperienceHint, ...], selected_hint: ExperienceHint | None = None,
        selected_candidate_id: str | None = None,
        selected_retrieval_id: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OneStepResult:
    outcome: str
    step_index: int | None = None
    semantic_satisfied: bool = False
    verification_ref: str | None = None
    dispatch_command_id: str | None = None
    primitive_outcome: str | None = None
    wait_seconds: float | None = None


class AndroidUiHandlerRegistry:
    """Exact version registry; unknown runners are deliberately inert."""
    def __init__(self) -> None:
        self._handlers: dict[tuple[str, int], AndroidUiAgentV1Handler] = {}

    def register(self, handler: "AndroidUiAgentV1Handler") -> None:
        key = (handler.runner_kind, handler.runner_version)
        if key in self._handlers and self._handlers[key] is not handler:
            raise ValueError("runner handler already registered")
        self._handlers[key] = handler

    def resolve(self, runner_kind: str, runner_version: int) -> "AndroidUiAgentV1Handler | None":
        return self._handlers.get((runner_kind, runner_version))


@dataclass(slots=True)
class AndroidUiAgentV1Handler:
    canonical: CanonicalReader
    observations: ObservationProvider
    planner: PlannerRole
    actor: ActorRole
    action_verifier: ActionVerifierRole
    semantic_verifier: EvidenceBoundSemanticVerifier
    store: SQLiteAndroidUiStepStore
    dispatch: DispatchPort
    reflection: ReflectionRole | None = None
    experience: ExperiencePort | None = None
    runner_kind: str = "android_ui_agent"
    runner_version: int = 1

    def one_step(self, *, task_id: str, goal: str, criteria: CriteriaRevision) -> OneStepResult:
        first = self.canonical.inspect(task_id)
        if not self._canonical_is_exact(first, first) or criteria.revision != first.revision:
            return OneStepResult("control_stopped")
        self.store.freeze_criteria(task_id, criteria)
        replay = self.store.trusted_terminal_for(
            first,
            criteria,
            runner_kind=self.runner_kind,
            runner_version=self.runner_version,
            model_version=self.semantic_verifier.model_version,
            prompt_version=self.semantic_verifier.prompt_version,
        )
        if replay is not None:
            return OneStepResult("terminal_replayed", replay.step_index, replay.overall == "satisfied", replay.verification_ref)
        try:
            next_waypoint = self.store.next_unmet_waypoint(first, criteria)
        except (OSError, ValueError):
            return OneStepResult("waypoint_integrity_blocked")
        planning_criteria = (
            criteria.waypoint_criteria(next_waypoint)
            if next_waypoint is not None else criteria
        )
        before = self.observations.observe(first)
        if not self._canonical_is_exact(first, self.canonical.inspect(task_id)) or not self._observation_matches(first, before):
            return OneStepResult("decision_invalidated")
        reservation = self.store.reserve_step(
            task_id=task_id,
            revision=first.revision,
            before=before,
            snapshot=first,
        )
        step_index = reservation.step_index
        step_id = self._experience_step_id(task_id, first.revision, step_index)
        experience_hints: tuple[ExperienceHint, ...] = ()
        goal_safe = sanitize_task_goal(goal)
        if reservation.has_after:
            recovery = self.store.pending_after_recovery(reservation)
            rehydrate = getattr(self.observations, "rehydrate", None)
            if recovery is None or not callable(rehydrate):
                return OneStepResult("after_pending_recovery", step_index)
            recovered_before = rehydrate(first, recovery["before"])
            recovered_after = rehydrate(first, recovery["after"])
            if recovered_before is None or recovered_after is None:
                return OneStepResult("after_pending_recovery", step_index)
            command_id = str(recovery["command_id"])
            primitive_outcome = str(recovery["primitive_outcome"])
            semantic = self.semantic_verifier.verify(
                snapshot=first,
                runner_kind=self.runner_kind,
                runner_version=self.runner_version,
                goal=goal_safe,
                criteria=planning_criteria,
                before=recovered_before,
                after=recovered_after,
                latest_step_index=step_index,
                causal_command_id=command_id,
                primitive_outcome=primitive_outcome,
            )
            if not self._canonical_is_exact(
                first, self.canonical.inspect(task_id),
            ):
                return OneStepResult(
                    "semantic_invalidated", step_index,
                    dispatch_command_id=command_id,
                )
            try:
                selected = self.store.selected_hint_attribution(reservation)
            except (OSError, ValueError):
                # A malformed/replayed retrieval row is never relaxed into a
                # different hint. K2 may still settle the already-observed
                # physical step, but K3 receives no attribution at all.
                selected = None
            if next_waypoint is not None:
                self._record_experience(
                    first,
                    planning_criteria,
                    step_id=step_id,
                    checkpoint_index=step_index + 1,
                    action=reservation.action,
                    before=recovered_before,
                    after=recovered_after,
                    verification=semantic,
                    primitive_outcome=primitive_outcome,
                    hints=(),
                    selected_hint=None,
                    selected_candidate_id=(selected[0] if selected is not None else None),
                    selected_retrieval_id=(selected[1] if selected is not None else None),
                )
                if semantic.overall == "satisfied":
                    self.store.record_waypoint_verification(
                        parent=criteria,
                        waypoint_ordinal=next_waypoint,
                        record=semantic,
                    )
                else:
                    self.store.complete_waypoint_attempt(reservation, semantic)
                return OneStepResult(
                    "continue", step_index, False,
                    verification_reference(semantic), command_id, primitive_outcome,
                )
            self._record_experience(
                first,
                criteria,
                step_id=step_id,
                checkpoint_index=step_index + 1,
                action=reservation.action,
                before=recovered_before,
                after=recovered_after,
                verification=semantic,
                primitive_outcome=primitive_outcome,
                hints=(),
                selected_hint=None,
                selected_candidate_id=(selected[0] if selected is not None else None),
                selected_retrieval_id=(selected[1] if selected is not None else None),
            )
            self.store.record_verification(semantic)
            if semantic.overall == "satisfied":
                return self._terminal_result(
                    semantic, step_index, command_id, primitive_outcome,
                )
            return OneStepResult(
                "recoverable_unknown" if primitive_outcome == "uncertain" else "continue",
                step_index,
                False,
                verification_reference(semantic),
                command_id,
                primitive_outcome,
            )

        if reservation.action_intent_id is not None:
            if reservation.action is None:
                return OneStepResult("unresolved_effect", step_index)
            intent = self._intent_from_reservation(reservation)
            if intent is None:
                return OneStepResult("unresolved_effect", step_index)
            try:
                receipt = self.dispatch.reconcile(first, intent)
            except (AttributeError, NotImplementedError):
                return OneStepResult("unresolved_effect", step_index)
            if not self._receipt_matches(receipt, intent) or (reservation.claim_id is not None and reservation.claim_id != receipt.command_id):
                return OneStepResult("unresolved_effect", step_index)
            reservation = self.store.record_claim(reservation, claim_id=receipt.command_id, action_intent_id=intent.action_intent_id)
            decision = RoleDecision("action", intent.action, "reconciled durable action")
            # The pre-effect UI artifact is intentionally not reloaded from
            # SQLite.  Do not bind a new before observation to an old action:
            # K4 must provide the exact artifact-backed recovery path.  This
            # wake has reconciled the one K1 command and has issued no action.
            return OneStepResult("reconciled_effect_pending_evidence", step_index, dispatch_command_id=receipt.command_id)
        elif reservation.decision_kind is not None:
            if not self.store.before_matches(reservation, before):
                if reservation.decision_kind != "terminal_candidate":
                    # An effect-bearing planner decision is only valid for the
                    # exact UI state it saw. A restart may reconcile a
                    # committed command, but may never dispatch against a
                    # replacement before-observation.
                    return OneStepResult("decision_pending_recovery", step_index)
                if self.store.supersede_terminal_candidate(reservation, before):
                    # The old decision remains a completed zero-effect
                    # checkpoint. A later wake may plan only from this newer,
                    # causally restored observation.
                    return OneStepResult("replan", step_index)
                durable_before = self.store.pending_terminal_recovery(reservation)
                rehydrate = getattr(self.observations, "rehydrate", None)
                if durable_before is None or not callable(rehydrate):
                    return OneStepResult("decision_pending_recovery", step_index)
                recovered_before = rehydrate(first, durable_before)
                if (
                    recovered_before is None
                    or not self._observation_matches(first, recovered_before)
                    or not self.store.before_matches(reservation, recovered_before)
                ):
                    return OneStepResult("decision_pending_recovery", step_index)
                before = recovered_before
            decision = self._decision_from_reservation(reservation)
            if decision is None:
                return OneStepResult("unresolved_effect", step_index)
            receipt = None
        else:
            experience_hints = self._retrieve_experience(
                first, planning_criteria, before, step_id=step_id,
                checkpoint_index=step_index + 1,
            )
            decision = self._decide(
                first, task_id, goal_safe, planning_criteria, before,
                experience_hints=experience_hints,
            )
            if decision is None:
                return OneStepResult("decision_invalidated", step_index)
            selected_hint = next(
                (item for item in experience_hints if item.candidate_id == decision.selected_hint_id),
                None,
            )
            if decision.selected_hint_id is not None and selected_hint is None:
                return OneStepResult("decision_invalidated", step_index)
            if decision.kind == "terminal_candidate" and next_waypoint is not None:
                decision = RoleDecision(
                    "replan",
                    reason="ordered waypoint remains unproved",
                )
            reservation = self.store.record_decision(
                reservation,
                decision,
                retrieval_attribution=tuple(
                    item.durable_attribution(selected=item is selected_hint)
                    for item in experience_hints
                ),
            )
            receipt = None

        if not self._canonical_is_exact(first, self.canonical.inspect(task_id)):
            return OneStepResult("decision_invalidated", step_index)
        if decision.kind == "replan":
            self.store.complete_replan(reservation)
            return OneStepResult("replan", step_index)
        if decision.kind == "action" and decision.action is not None and decision.action.kind == "wait":
            seconds = float(decision.action.arguments["seconds"])
            self.store.complete_wait(reservation)
            return OneStepResult("wait", step_index, wait_seconds=seconds)
        if decision.kind == "terminal_candidate" and next_waypoint is not None:
            # A current final page never substitutes for an unfinished route.
            # Keep the zero-effect checkpoint auditable, then request a plan
            # for the same next waypoint on the next fresh observation.
            return OneStepResult("waypoint_pending", step_index)
        if decision.kind == "terminal_candidate":
            if not self.store.terminal_candidate_is_causally_grounded(
                reservation, before,
            ):
                return OneStepResult("terminal_requires_after_evidence", step_index)
            semantic = self.semantic_verifier.verify(snapshot=first, runner_kind=self.runner_kind, runner_version=self.runner_version, goal=goal_safe, criteria=criteria, before=before, after=before, latest_step_index=step_index, already_satisfied=True)
            if not self._canonical_is_exact(first, self.canonical.inspect(task_id)):
                return OneStepResult("semantic_invalidated", step_index)
            self.store.record_verification(semantic)
            return self._terminal_result(semantic, step_index)

        intent = AndroidUiActionIntent.create(task_id=task_id, criteria_revision=first.revision, step_index=step_index, action=decision.action)
        if receipt is None:
            self.store.record_intent(reservation, intent)
            if not self._canonical_is_exact(first, self.canonical.inspect(task_id)):
                return OneStepResult("decision_invalidated", step_index)
            receipt = self.dispatch.dispatch(first, intent)
            if not self._receipt_matches(receipt, intent):
                return OneStepResult("unresolved_effect", step_index)
            reservation = self.store.record_claim(reservation, claim_id=receipt.command_id, action_intent_id=intent.action_intent_id)

        current = self.canonical.inspect(task_id)
        if not self._canonical_is_exact(first, current):
            return OneStepResult("control_after_dispatch", step_index, dispatch_command_id=receipt.command_id)
        after = self.observations.observe(current)
        if not self._observation_matches(current, after) or after.freshness_token == before.freshness_token or after.causality_command_id != receipt.command_id:
            return OneStepResult("after_evidence_unavailable", step_index, dispatch_command_id=receipt.command_id)
        self.store.record_after(reservation, after)
        primitive = self.action_verifier.verify(ActionVerificationContext(goal_safe, before, after, decision.action))
        settle_after = getattr(self.dispatch, "settle_after", None)
        if callable(settle_after):
            try:
                settle_after(
                    snapshot=current,
                    intent=intent,
                    receipt=receipt,
                    before=before,
                    after=after,
                    primitive=primitive,
                )
            except Exception:
                self.store.record_primitive_outcome(
                    reservation,
                    command_id=receipt.command_id,
                    outcome="uncertain",
                )
                return OneStepResult(
                    outcome="recoverable_unknown",
                    step_index=step_index,
                    semantic_satisfied=False,
                    verification_ref=None,
                    dispatch_command_id=receipt.command_id,
                    primitive_outcome="uncertain",
                )
        primitive_outcome = (
            "uncertain" if primitive.uncertain
            else "progress" if primitive.progress
            else "no_progress"
        )
        try:
            self.store.record_primitive_outcome(
                reservation,
                command_id=receipt.command_id,
                outcome=primitive_outcome,
            )
        except ValueError:
            self.store.record_primitive_outcome(
                reservation,
                command_id=receipt.command_id,
                outcome="uncertain",
            )
            return OneStepResult(
                outcome="recoverable_unknown",
                step_index=step_index,
                semantic_satisfied=False,
                dispatch_command_id=receipt.command_id,
                primitive_outcome="uncertain",
            )
        current = self.canonical.inspect(task_id)
        if not self._canonical_is_exact(first, current):
            return OneStepResult("control_after_dispatch", step_index, dispatch_command_id=receipt.command_id)
        semantic = self.semantic_verifier.verify(
            snapshot=current,
            runner_kind=self.runner_kind,
            runner_version=self.runner_version,
            goal=goal_safe,
            criteria=planning_criteria,
            before=before,
            after=after,
            latest_step_index=step_index,
            causal_command_id=receipt.command_id,
            primitive_outcome=primitive_outcome,
        )
        if not self._canonical_is_exact(first, self.canonical.inspect(task_id)):
            return OneStepResult("semantic_invalidated", step_index, dispatch_command_id=receipt.command_id)
        if next_waypoint is not None:
            selected_hint = next(
                (item for item in experience_hints if item.candidate_id == decision.selected_hint_id),
                None,
            )
            self._record_experience(
                first,
                planning_criteria,
                step_id=step_id,
                checkpoint_index=step_index + 1,
                action=decision.action,
                before=before,
                after=after,
                verification=semantic,
                primitive_outcome=primitive_outcome,
                hints=experience_hints,
                selected_hint=selected_hint,
            )
            if semantic.overall == "satisfied":
                self.store.record_waypoint_verification(
                    parent=criteria,
                    waypoint_ordinal=next_waypoint,
                    record=semantic,
                )
            else:
                self.store.complete_waypoint_attempt(reservation, semantic)
            return OneStepResult(
                "continue", step_index, False, verification_reference(semantic),
                receipt.command_id, primitive_outcome,
            )
        self._record_experience(
            first,
            criteria,
            step_id=step_id,
            checkpoint_index=step_index + 1,
            action=decision.action,
            before=before,
            after=after,
            verification=semantic,
            primitive_outcome=primitive_outcome,
            hints=experience_hints,
            selected_hint=(
                next(
                    (item for item in experience_hints if item.candidate_id == decision.selected_hint_id),
                    None,
                )
                if decision is not None else None
            ),
        )
        self.store.record_verification(semantic)
        if semantic.overall == "satisfied":
            return self._terminal_result(
                semantic, step_index, receipt.command_id, primitive_outcome,
            )
        if primitive.uncertain:
            return OneStepResult(
                outcome="recoverable_unknown",
                step_index=step_index,
                semantic_satisfied=False,
                verification_ref=verification_reference(semantic),
                dispatch_command_id=receipt.command_id,
                primitive_outcome="uncertain",
            )
        if not primitive.progress and self.reflection is not None:
            self.reflection.reflect(ReflectionContext(goal_safe, criteria, after, primitive.reason))
            if not self._canonical_is_exact(first, self.canonical.inspect(task_id)):
                return OneStepResult("reflection_invalidated", step_index, dispatch_command_id=receipt.command_id)
        return OneStepResult("continue", step_index, False, verification_reference(semantic), receipt.command_id, "progress" if primitive.progress else "no_progress")

    def trusted_terminal(
        self, *, task_id: str, criteria: CriteriaRevision,
    ) -> object | None:
        """Return only the store's exact verifier-issued terminal fact.

        K4 uses this second read after ``one_step``.  The scheduler therefore
        never promotes a Task from a model result or an in-memory boolean.
        """

        snapshot = self.canonical.inspect(task_id)
        if not self._canonical_is_exact(snapshot, snapshot):
            return None
        return self.store.trusted_terminal_for(
            snapshot,
            criteria,
            runner_kind=self.runner_kind,
            runner_version=self.runner_version,
            model_version=self.semantic_verifier.model_version,
            prompt_version=self.semantic_verifier.prompt_version,
        )

    def _decide(
        self, snapshot: CanonicalSnapshot, task_id: str, goal: str,
        criteria: CriteriaRevision, before: ObservationEnvelope, *,
        experience_hints: tuple[ExperienceHint, ...] = (),
    ) -> RoleDecision | None:
        context = PlanningContext(
            task_id, goal, snapshot.revision, criteria, before,
            tuple(item.planner_projection() for item in experience_hints),
        )
        plan = self.planner.plan(context)
        if not self._canonical_is_exact(snapshot, self.canonical.inspect(snapshot.task_id)):
            return None
        decision = self.actor.decide(context, plan)
        return decision if self._canonical_is_exact(snapshot, self.canonical.inspect(snapshot.task_id)) else None

    def _retrieve_experience(
        self, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
        observation: ObservationEnvelope, *, step_id: str,
        checkpoint_index: int,
    ) -> tuple[ExperienceHint, ...]:
        if self.experience is None:
            return ()
        try:
            values = self.experience.retrieve(
                snapshot=snapshot,
                criteria=criteria,
                observation=observation,
                step_id=step_id,
                checkpoint_index=checkpoint_index,
            )
            if not isinstance(values, tuple):
                return ()
            # A malformed sidecar value can never become planner input or make
            # the Task fail.  The exact K3 adapter returns this typed shape.
            return tuple(item for item in values[:8] if isinstance(item, ExperienceHint))
        except Exception:
            return ()

    def _record_experience(
        self, snapshot: CanonicalSnapshot, criteria: CriteriaRevision, *,
        step_id: str, checkpoint_index: int, action: AndroidUiAction | None,
        before: ObservationEnvelope, after: ObservationEnvelope,
        verification: GoalVerificationRecord, primitive_outcome: str,
        hints: tuple[ExperienceHint, ...], selected_hint: ExperienceHint | None,
        selected_candidate_id: str | None = None,
        selected_retrieval_id: str | None = None,
    ) -> None:
        if self.experience is None or action is None or action.kind == "wait":
            return
        try:
            payload: dict[str, object] = {
                "snapshot": snapshot,
                "criteria": criteria,
                "step_id": step_id,
                "checkpoint_index": checkpoint_index,
                "action": action,
                "before": before,
                "after": after,
                "verification": verification,
                "primitive_outcome": primitive_outcome,
                "hints": hints,
                "selected_hint": selected_hint,
            }
            # Preserve the narrow pre-K3F port contract for normal execution.
            # Only recovery needs the durable opaque identifiers because it
            # deliberately cannot recreate the original ExperienceHint.
            if selected_candidate_id is not None and selected_retrieval_id is not None:
                payload["selected_candidate_id"] = selected_candidate_id
                payload["selected_retrieval_id"] = selected_retrieval_id
            self.experience.record_step(**payload)
        except Exception:
            # Experience is an advisory sidecar.  K2 evidence and canonical
            # Task progress remain valid even when K3 is absent or unavailable.
            return

    def _canonical_is_exact(self, expected: CanonicalSnapshot, current: CanonicalSnapshot) -> bool:
        return (
            current.task_id == expected.task_id and current.owner == expected.owner and current.revision == expected.revision
            and current.status == expected.status and current.allowed_controls == expected.allowed_controls
            and current.profile_id == expected.profile_id and current.profile_generation == expected.profile_generation
            and current.boot_id == expected.boot_id and current.canonical_device_id == expected.canonical_device_id
            and current.runner_kind == self.runner_kind and current.runner_version == self.runner_version
            and current.runner_binding_id == expected.runner_binding_id and current.runner_binding_id is not None and current.runnable
        )

    @staticmethod
    def _observation_matches(snapshot: CanonicalSnapshot, observation: ObservationEnvelope) -> bool:
        return (observation.task_id == snapshot.task_id and observation.profile_id == snapshot.profile_id and observation.profile_generation == snapshot.profile_generation and observation.boot_id == snapshot.boot_id and observation.canonical_device_id == snapshot.canonical_device_id)

    @staticmethod
    def _receipt_matches(receipt: object, intent: AndroidUiActionIntent) -> bool:
        return isinstance(receipt, DispatchReceipt) and receipt.action_intent_id == intent.action_intent_id

    @staticmethod
    def _intent_from_reservation(reservation: object) -> AndroidUiActionIntent | None:
        action = getattr(reservation, "action", None)
        if action is None:
            return None
        intent = AndroidUiActionIntent.create(task_id=getattr(reservation, "task_id"), criteria_revision=getattr(reservation, "revision"), step_index=getattr(reservation, "step_index"), action=action)
        return intent if intent.action_intent_id == getattr(reservation, "action_intent_id") else None

    @staticmethod
    def _decision_from_reservation(reservation: object) -> RoleDecision | None:
        kind = getattr(reservation, "decision_kind", None)
        action = getattr(reservation, "action", None)
        selected_hint_id = getattr(reservation, "selected_hint_id", None)
        if kind == "action":
            return RoleDecision("action", action, selected_hint_id=selected_hint_id) if action is not None else None
        if kind in {"replan", "terminal_candidate"}:
            return RoleDecision(kind)
        return None

    @staticmethod
    def _terminal_result(record: GoalVerificationRecord, step_index: int, command_id: str | None = None, primitive_outcome: str | None = None) -> OneStepResult:
        return OneStepResult("semantic_satisfied" if record.overall == "satisfied" else "terminal_unverified", step_index, record.overall == "satisfied", verification_reference(record), command_id, primitive_outcome)

    @staticmethod
    def _experience_step_id(task_id: str, revision: int, step_index: int) -> str:
        # K3 domain-separates and hashes this value before persistence.  It is
        # stable across a decision retry without becoming an action identity.
        return f"android-ui-step:{task_id}:{revision}:{step_index}"
