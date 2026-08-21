from __future__ import annotations

import hashlib
import json
import re
import struct
import uuid
import zlib
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any

from .domain import (
    ActionTransition,
    ExperienceCandidate,
    ExperienceEpisode,
    ExperiencePacket,
    OutcomeSignal,
    PolicyRevision,
    RetrievedExperience,
    SceneState,
    ScopeKey,
)
from .store import SQLiteExperienceStore


class ExperienceService:
    """Canonical adapter over append-only evidence and reversible policy heads."""

    def __init__(
        self, store: SQLiteExperienceStore, *, enabled: bool = True,
        observation_payload: Callable[[str], bytes] | None = None,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.observation_payload = observation_payload

    def begin_mobile_episode(
        self, *, goal_run_id: str, source_task_id: str, goal_spec_revision: int,
        frozen_criteria_ids: tuple[str, ...], scope: ScopeKey
    ) -> ExperienceEpisode | None:
        if not self.enabled:
            return None
        if goal_spec_revision < 1 or not frozen_criteria_ids:
            raise ValueError("experience episode requires frozen goal criteria")
        return self.store.put_episode(ExperienceEpisode(
            episode_id=str(uuid.uuid4()), goal_run_id=goal_run_id,
            source_task_id=source_task_id, goal_spec_revision=goal_spec_revision,
            scope=scope, frozen_criteria_ids=frozen_criteria_ids,
            terminal_outcome=None, action_count=0, recovery_count=0,
            intervention_count=0, created_at=_utc_now(), finished_at=None,
        ))

    def begin_mobile_episode_for_task(
        self, *, goal_run_id: str, source_task_id: str,
        goal_spec_revision: int, frozen_criteria_ids: tuple[str, ...],
        skill_scope_id: str | None, target_id: str | None,
    ) -> ExperienceEpisode | None:
        normalized = (skill_scope_id or "auto:generic/unknown/v1").removeprefix("auto:")
        application_id = normalized.split("/", 1)[0] if "/" in normalized else "generic"
        return self.begin_mobile_episode(
            goal_run_id=goal_run_id, source_task_id=source_task_id,
            goal_spec_revision=goal_spec_revision,
            frozen_criteria_ids=frozen_criteria_ids,
            scope=ScopeKey(
                user_scope="local-user",
                account_scope="local-default",
                application_id=application_id,
                goal_family=normalized,
                ui_version="unknown",
                device_class=(target_id or "android").split(":", 1)[0],
                # The task is admitted before its first screenshot. Preserve
                # that uncertainty instead of recording a false portrait fact;
                # the scene compatibility key still binds the observed frame
                # dimensions before any candidate can be retrieved.
                orientation="unknown",
            ),
        )

    def retrieve(
        self, *, source_task_id: str, objective: str, observation: Any
    ) -> ExperiencePacket:
        if not self.enabled:
            return ExperiencePacket(None)
        try:
            episode = self.store.episode_for_task(source_task_id)
        except KeyError:
            return ExperiencePacket(None)
        self._reconcile_active_negative_policy(episode.scope)
        # Reconcile candidates created by an older process before querying the
        # active policy. This is idempotent and preserves append-only evidence.
        for candidate in self.store.candidates(episode.scope, status="candidate"):
            self._promote_repeated_local_failure(candidate)
        scene = self._scene(
            episode, observation, semantic_hint=None, confidence=0.55
        )
        candidates = self.store.active_candidates(
            episode.scope, objective=_objective(objective),
            scene_matcher=scene.semantic_label,
            visual_fingerprint=scene.visual_fingerprint,
            compatibility_key=scene.compatibility_key,
        )
        retrieval_id = str(uuid.uuid4())
        self.store.record_retrieval(
            retrieval_id, episode.episode_id, _objective(objective), scene.scene_id,
            tuple(item.candidate_id for item in candidates),
        )
        return ExperiencePacket(
            retrieval_id,
            tuple(RetrievedExperience(
                item.candidate_id, item.kind, item.semantic_action,
                item.expected_next_scene, item.recovery_action, item.confidence,
                item.provenance_transition_ids,
            ) for item in candidates[:8]),
        )

    def record_mobile_attempt(
        self, *, source_task_id: str, objective: str, attempt: Any,
        retrieval: ExperiencePacket | None = None,
        failure_class: str | None = None,
    ) -> ActionTransition | None:
        if not self.enabled:
            return None
        existing = self.store.transition_for_attempt(str(_value(attempt, "attempt_id")))
        if existing is not None:
            if retrieval is not None:
                signal = self.store.latest_signal_for_transition(existing.transition_id)
                self._attribute_usage(retrieval, existing, signal)
            return existing
        try:
            episode = self.store.episode_for_task(source_task_id)
        except KeyError:
            return None
        verification = _value(attempt, "verification")
        if verification is None:
            return None
        before = _value(attempt, "before")
        after = _value(attempt, "after")
        transport = _value(attempt, "transport")
        decision = _value(attempt, "decision")
        semantic_action, region, coordinates = _semantic_action(decision, before)
        before_scene = self._scene(episode, before, semantic_hint=None, confidence=0.55)
        hint = str(_value(verification, "evidence", ""))
        after_scene = (
            self._scene(episode, after, semantic_hint=hint, confidence=0.82)
            if after is not None else None
        )
        uncertain = bool(_value(verification, "uncertain", False))
        satisfied = bool(_value(verification, "satisfied", False))
        progress = bool(_value(verification, "progress", False))
        detected_failure = failure_class or _failure_class(hint, progress, satisfied)
        outcome = "uncertain" if uncertain else (
            "immediate_success" if satisfied else
            "wrong_scene" if detected_failure == "wrong_scene" else
            "immediate_failure" if progress else "no_progress"
        )
        recovery_of = self._recovery_source(
            episode.episode_id, before_scene.scene_id, after_scene.scene_id if after_scene else None,
            satisfied,
        )
        if recovery_of is not None:
            outcome = "recovered"
        transition = self.store.put_transition(ActionTransition(
            transition_id=str(uuid.uuid4()), episode_id=episode.episode_id,
            source_attempt_id=str(_value(attempt, "attempt_id")),
            source_attempt_sequence=int(_value(attempt, "sequence")),
            objective=_objective(objective), before_scene_id=before_scene.scene_id,
            semantic_action=semantic_action, grounded_region=region,
            coordinates_json=coordinates, expected_outcome=_objective(objective),
            transport_status=str(_value(transport, "status", "not_sent")),
            after_scene_id=after_scene.scene_id if after_scene else None,
            immediate_outcome=outcome, failure_class=detected_failure,
            recovery_of_transition_id=recovery_of, created_at=_utc_now(),
        ))
        signal = self.store.put_signal(OutcomeSignal(
            signal_id=str(uuid.uuid4()), episode_id=episode.episode_id,
            transition_id=transition.transition_id, kind=outcome,
            source="mobile_task_immediate_verifier",
            evidence_refs=tuple(ref for ref in (
                str(_value(before, "evidence_id", "")),
                str(_value(after, "evidence_id", "")) if after is not None else "",
            ) if ref),
            confidence=0.0 if uncertain else 1.0,
            reward_vector_json=_reward(outcome), created_at=_utc_now(),
        ))
        if not uncertain:
            candidate = self._candidate_from_transition(
                episode, transition, before_scene, after_scene
            )
            if candidate is not None:
                self._promote_repeated_local_failure(candidate)
        self._attribute_usage(retrieval, transition, signal)
        return transition

    def sync_mobile_task(self, state: Any) -> None:
        source_task_id = str(_value(state, "task_id"))
        plan = _value(state, "plan")
        subgoals = {
            int(_value(item, "index")): str(_value(item, "description"))
            for item in _value(plan, "subgoals", ())
        } if plan is not None else {}
        for attempt in _value(state, "attempts", ()):
            if _value(attempt, "verification") is None:
                continue
            self.record_mobile_attempt(
                source_task_id=source_task_id,
                objective=subgoals.get(int(_value(attempt, "subgoal_index", -1)),
                                       str(_value(state, "goal", "unknown objective"))),
                attempt=attempt,
            )

    def confirm_goal_completion(
        self, source_task_id: str, *, goal_id: str, completion_revision: int
    ) -> PolicyRevision | None:
        if not self.enabled:
            return None
        episode = self.store.episode_for_task(source_task_id)
        if episode.goal_run_id != goal_id:
            raise ValueError("experience goal provenance mismatch")
        if completion_revision < 1:
            raise ValueError("completion revision must be positive")
        self.store.finish_episode(source_task_id, "verified")
        transition_ids = {
            item.transition_id for item in self.store.transitions_for_episode(episode.episode_id)
        }
        candidates: list[ExperienceCandidate] = []
        for transition_id in transition_ids:
            for candidate in self.store.candidates_for_transition(transition_id):
                if candidate.status == "candidate" and candidate.provenance_transition_ids:
                    candidates.append(self.promote_candidate(candidate.candidate_id))
                elif candidate.status == "promoted":
                    candidates.append(candidate)
        unique = {item.candidate_id: item for item in candidates}
        previous = self.store.policies(episode.scope)
        active = next((item for item in reversed(previous) if item.status == "active"), None)
        active_ids = tuple(dict.fromkeys((
            *(active.candidate_ids if active is not None else ()),
            *unique.keys(),
        )))
        if not active_ids:
            return None
        if active is not None and active.candidate_ids == active_ids:
            return active
        policy = PolicyRevision(
            policy_id=str(uuid.uuid4()), scope=episode.scope,
            revision=(previous[-1].revision + 1 if previous else 1),
            candidate_ids=active_ids, status="active", created_at=_utc_now(),
        )
        return self.store.put_policy(policy)

    def reject_candidate(self, candidate_id: str) -> ExperienceCandidate:
        return self.store.set_candidate_status(candidate_id, "rejected")

    def promote_candidate(self, candidate_id: str) -> ExperienceCandidate:
        candidate = self.store.candidate(candidate_id)
        if candidate.status != "candidate" or not candidate.provenance_transition_ids:
            raise ValueError("only a provenance-complete candidate can be promoted")
        for transition_id in candidate.provenance_transition_ids:
            transition = self.store.transition(transition_id)
            episode = self.store.episode(transition.episode_id)
            if episode.terminal_outcome != "verified":
                raise ValueError("candidate provenance lacks verified goal coverage")
            if episode.scope != candidate.scope or not episode.frozen_criteria_ids:
                raise ValueError("candidate provenance scope or goal criteria mismatch")
            signal = self.store.latest_signal_for_transition(transition_id)
            if signal is None or signal.kind == "uncertain" or not signal.evidence_refs:
                raise ValueError("candidate provenance lacks admissible outcome evidence")
        return self.store.set_candidate_status(candidate_id, "promoted")

    def deprecate_candidate(self, candidate_id: str) -> ExperienceCandidate:
        return self.store.set_candidate_status(candidate_id, "deprecated")

    def rollback(self, scope: ScopeKey, *, to_revision: int) -> PolicyRevision:
        policies = self.store.policies(scope)
        target = next((item for item in policies if item.revision == to_revision), None)
        if target is None:
            raise KeyError(to_revision)
        next_revision = policies[-1].revision + 1
        policy = PolicyRevision(
            policy_id=str(uuid.uuid4()), scope=scope, revision=next_revision,
            candidate_ids=target.candidate_ids, status="active",
            created_at=_utc_now(), rollback_of_revision=to_revision,
        )
        return self.store.put_policy(policy)

    def inspect_legacy_hint(
        self, *, source_kind: str, source_id: str, source_version: int,
        provenance_valid: bool, goal_coverage_valid: bool, detail: str
    ) -> str:
        return self.store.record_legacy_import(
            import_id=str(uuid.uuid4()), source_kind=source_kind, source_id=source_id,
            source_version=source_version, provenance_valid=provenance_valid,
            goal_coverage_valid=goal_coverage_valid, detail=detail,
        )

    def inspect_mobile_skill_memory(
        self, *, memory: Any, source_task: Any,
        goal_completion: dict[str, Any] | None,
    ) -> str:
        source_task_id = str(_value(source_task, "task_id", ""))
        memory_task_id = str(_value(memory, "source_task_id", ""))
        evidence = tuple(_value(memory, "evidence", ()))
        provenance_valid = bool(
            source_task_id and memory_task_id == source_task_id
            and int(_value(memory, "version", 0)) > 0 and evidence
        )
        goal_coverage_valid = bool(
            goal_completion
            and goal_completion.get("verdict") == "verified"
            and str(goal_completion.get("source_task_id") or "") == source_task_id
            and int(goal_completion.get("revision") or 0) > 0
        )
        return self.inspect_legacy_hint(
            source_kind="skill_memory",
            source_id=f"{_value(memory, 'skill_id', 'unknown')}:{memory_task_id}",
            source_version=int(_value(memory, "version", 0)),
            provenance_valid=provenance_valid,
            goal_coverage_valid=goal_coverage_valid,
            detail=(
                "legacy SkillMemory source task and verified Goal completion agree"
                if provenance_valid and goal_coverage_valid
                else "legacy SkillMemory remains an untrusted compatibility hint"
            ),
        )

    def import_game_learning_episode(
        self, *, job: Any, transitions: tuple[Any, ...], goal_run_id: str,
        goal_spec_revision: int, frozen_criteria_ids: tuple[str, ...],
        scope: ScopeKey, scene_labels: dict[str, str], goal_verified: bool,
    ) -> ExperienceEpisode:
        """Import only finalized, evidence-referenced GameLearning facts.

        The old database remains authoritative and untouched. Callers must
        supply the GoalRun/specification mapping because legacy jobs do not own
        original-goal coverage.
        """
        job_id = str(_value(job, "job_id"))
        source_task_id = f"game_learning:{job_id}"
        episode = self.begin_mobile_episode(
            goal_run_id=goal_run_id, source_task_id=source_task_id,
            goal_spec_revision=goal_spec_revision,
            frozen_criteria_ids=frozen_criteria_ids, scope=scope,
        )
        if episode is None:
            raise RuntimeError("experience service is disabled")
        for item in transitions:
            proposal = _value(item, "proposal")
            action = _value(proposal, "action")
            before_ref = _value(item, "before")
            after_ref = _value(item, "after")
            transport = _value(item, "transport")
            outcome = _value(item, "outcome")
            if action is None or before_ref is None or transport is None or outcome is None:
                self.inspect_legacy_hint(
                    source_kind="game_learning", source_id=str(_value(item, "transition_id")),
                    source_version=1, provenance_valid=False,
                    goal_coverage_valid=goal_verified,
                    detail="transition is not finalized with before/action/transport/outcome",
                )
                continue
            before_sha = str(_value(before_ref, "sha256"))
            after_sha = str(_value(after_ref, "sha256")) if after_ref is not None else ""
            confirmed = bool(_value(outcome, "confirmed", False))
            reward = float(_value(outcome, "reward", 0.0))
            task_succeeded = bool(_value(outcome, "task_succeeded", False))
            args = {
                key: _value(action, key) for key in
                ("x", "y", "end_x", "end_y", "duration_ms", "keycode", "text")
                if _value(action, key) is not None
            }
            pseudo = {
                "attempt_id": f"game_learning:{_value(item, 'transition_id')}",
                "sequence": int(_value(item, "sequence")),
                "decision": {"kind": "act", "intent": {
                    "name": str(_value(action, "action")), "arguments": args,
                }},
                "before": {"evidence_id": f"sha256:{before_sha}",
                           "summary": scene_labels.get(before_sha, f"artifact {before_sha[:12]}")},
                "transport": {"status": str(_value(transport, "status", "unknown"))},
                "after": ({"evidence_id": f"sha256:{after_sha}",
                           "summary": scene_labels.get(after_sha, f"artifact {after_sha[:12]}")}
                          if after_ref is not None else None),
                "verification": {
                    "satisfied": confirmed and task_succeeded,
                    "progress": confirmed and (reward > 0 or task_succeeded),
                    "uncertain": not confirmed,
                    "evidence": str(_value(outcome, "detail", "legacy verifier")),
                },
            }
            self.record_mobile_attempt(
                source_task_id=source_task_id,
                objective=str(_value(job, "instruction", "legacy learning objective")),
                attempt=pseudo,
                failure_class=("immediate_failure" if confirmed and reward < 0 else None),
            )
            self.inspect_legacy_hint(
                source_kind="game_learning", source_id=str(_value(item, "transition_id")),
                source_version=1, provenance_valid=confirmed,
                goal_coverage_valid=goal_verified,
                detail="selected finalized transition imported additively",
            )
        if goal_verified:
            self.confirm_goal_completion(
                source_task_id, goal_id=goal_run_id, completion_revision=1
            )
        return self.store.episode_for_task(source_task_id)

    def _scene(
        self, episode: ExperienceEpisode, observation: Any, *,
        semantic_hint: str | None, confidence: float
    ) -> SceneState:
        evidence_id = str(_value(observation, "evidence_id"))
        summary = str(_value(observation, "summary", ""))
        integrity_hash, visual_fingerprint = self._visual_identity(evidence_id)
        similar = (
            self.store.similar_scene(episode.scope, visual_fingerprint)
            if semantic_hint is None and _summary_is_generic(summary)
            else None
        )
        label_source = semantic_hint or (
            similar.semantic_label if similar is not None else summary
        )
        label = _semantic_label(label_source)
        compatibility = _compatibility(episode.scope, summary)
        return self.store.put_scene(SceneState(
            scene_id=str(uuid.uuid4()), episode_id=episode.episode_id,
            observation_ref=evidence_id,
            integrity_hash=integrity_hash, visual_fingerprint=visual_fingerprint,
            semantic_label=label,
            anchors=(similar.anchors if similar is not None else _anchors(label_source)),
            compatibility_key=compatibility, confidence=confidence,
            created_at=_utc_now(),
        ))

    def _visual_identity(self, evidence_id: str) -> tuple[str, str]:
        if self.observation_payload is None:
            return hashlib.sha256(evidence_id.encode("utf-8")).hexdigest(), ""
        try:
            payload = self.observation_payload(evidence_id)
            integrity = hashlib.sha256(payload).hexdigest()
            return integrity, _png_dhash(payload)
        except Exception:
            return hashlib.sha256(evidence_id.encode("utf-8")).hexdigest(), ""

    def _attribute_usage(
        self, retrieval: ExperiencePacket | None, transition: ActionTransition,
        signal: OutcomeSignal | None,
    ) -> None:
        if retrieval is None or retrieval.retrieval_id is None:
            return
        for item in retrieval.items:
            used_action = (
                _grounded_action(transition.semantic_action, transition.grounded_region)
                if item.kind == "negative" else transition.semantic_action
            )
            if _candidate_used(item, used_action):
                usage_id = str(uuid.uuid4())
                usage_id = self.store.record_usage(
                    usage_id, retrieval.retrieval_id, item.candidate_id,
                    transition.source_attempt_id,
                    signal.signal_id if signal is not None else None,
                )
                adhered = (
                    not _semantic_action_matches(
                        item.semantic_action, used_action
                    )
                    if item.kind == "negative"
                    else _semantic_action_matches(
                        item.semantic_action, transition.semantic_action
                    )
                    or (
                        item.recovery_action is not None
                        and _semantic_action_matches(
                            item.recovery_action, transition.semantic_action
                        )
                    )
                )
                self.store.record_candidate_trial(
                    trial_id=str(uuid.uuid4()), candidate_id=item.candidate_id,
                    usage_id=usage_id, adhered=adhered,
                    result=_trial_result(
                        item.kind, adhered, signal,
                        expected_transition_match=(
                            bool(item.provenance_transition_ids)
                            and self.store.transition_after_matches(
                                item.provenance_transition_ids[0], transition.transition_id
                            )
                        ),
                    ),
                    signal_id=signal.signal_id if signal is not None else None,
                )

    def _candidate_from_transition(
        self, episode: ExperienceEpisode, transition: ActionTransition,
        before_scene: SceneState, after_scene: SceneState | None,
    ) -> ExperienceCandidate | None:
        if transition.immediate_outcome not in {
            "immediate_success", "no_progress", "wrong_scene", "recovered"
        }:
            return None
        kind = "positive" if transition.immediate_outcome == "immediate_success" else (
            "recovery" if transition.immediate_outcome == "recovered" else "negative"
        )
        semantic_action = (
            _grounded_action(transition.semantic_action, transition.grounded_region)
            if kind == "negative" else transition.semantic_action
        )
        return self.store.upsert_candidate(ExperienceCandidate(
            candidate_id=str(uuid.uuid4()), scope=episode.scope, kind=kind,
            objective_matcher=transition.objective,
            scene_matcher=before_scene.semantic_label,
            semantic_action=semantic_action,
            expected_next_scene=after_scene.semantic_label if after_scene else None,
            recovery_action=transition.semantic_action if kind == "recovery" else None,
            support_count=1, failure_count=0, confidence=2 / 3,
            provenance_transition_ids=(transition.transition_id,),
            compatibility_key=before_scene.compatibility_key,
            status="candidate", created_at=_utc_now(), updated_at=_utc_now(),
        ))

    def _promote_repeated_local_failure(
        self, candidate: ExperienceCandidate
    ) -> PolicyRevision | None:
        """Activate only repeated, certain negative evidence before Goal success.

        Positive action knowledge still requires independent complete-goal
        coverage. This narrow path prevents a chicken-and-egg failure where a
        known same-scene bad action cannot be avoided until the whole task has
        somehow succeeded once.
        """

        if (
            candidate.status != "candidate"
            or candidate.kind != "negative"
            or _action_region(candidate.semantic_action) is None
            or candidate.support_count < 2
            or candidate.failure_count != 0
            or candidate.confidence < 0.75
            or len(candidate.provenance_transition_ids) < 2
        ):
            return None
        for transition_id in candidate.provenance_transition_ids:
            transition = self.store.transition(transition_id)
            episode = self.store.episode(transition.episode_id)
            signal = self.store.latest_signal_for_transition(transition_id)
            if (
                episode.scope != candidate.scope
                or not episode.frozen_criteria_ids
                or transition.immediate_outcome not in {"no_progress", "wrong_scene"}
                or signal is None
                or signal.kind not in {"no_progress", "wrong_scene"}
                or signal.confidence < 1.0
                or not signal.evidence_refs
            ):
                return None
        promoted = self.store.set_candidate_status(candidate.candidate_id, "promoted")
        policies = self.store.policies(candidate.scope)
        active = next((item for item in reversed(policies) if item.status == "active"), None)
        candidate_ids = tuple(dict.fromkeys((
            *(active.candidate_ids if active is not None else ()),
            promoted.candidate_id,
        )))
        if active is not None and active.candidate_ids == candidate_ids:
            return active
        return self.store.put_policy(PolicyRevision(
            policy_id=str(uuid.uuid4()), scope=candidate.scope,
            revision=(policies[-1].revision + 1 if policies else 1),
            candidate_ids=candidate_ids, status="active", created_at=_utc_now(),
        ))

    def _reconcile_active_negative_policy(self, scope: ScopeKey) -> None:
        """Retire unsafe or repeatedly contradicted negative hints.

        Early U5 builds stored only a semantic target, which could turn a bad
        grounding attempt into the over-broad rule "never tap that target".
        Negative hints are safe to activate only when they retain their
        normalized screen region. Repeated attributable failures also retire a
        hint without deleting its append-only evidence.
        """

        policies = self.store.policies(scope)
        active = next((item for item in reversed(policies) if item.status == "active"), None)
        if active is None:
            return
        retained: list[str] = []
        changed = False
        for candidate_id in active.candidate_ids:
            candidate = self.store.candidate(candidate_id)
            unsafe_negative = candidate.kind == "negative" and (
                _action_region(candidate.semantic_action) is None
                or (candidate.failure_count >= 2 and candidate.confidence < 0.5)
            )
            if unsafe_negative:
                if candidate.status == "promoted":
                    self.store.set_candidate_status(candidate_id, "deprecated")
                changed = True
            elif candidate.status == "promoted":
                retained.append(candidate_id)
            else:
                changed = True
        if changed:
            self.store.put_policy(PolicyRevision(
                policy_id=str(uuid.uuid4()), scope=scope,
                revision=policies[-1].revision + 1,
                candidate_ids=tuple(retained), status="active",
                created_at=_utc_now(),
            ))

    def _recovery_source(
        self, episode_id: str, before_scene_id: str, after_scene_id: str | None,
        satisfied: bool,
    ) -> str | None:
        if not satisfied or after_scene_id is None:
            return None
        transitions = self.store.transitions_for_episode(episode_id)
        scenes = {item.scene_id: item for item in self.store.scenes_for_episode(episode_id)}
        after_label = scenes[after_scene_id].semantic_label
        for item in reversed(transitions):
            if item.after_scene_id == before_scene_id and item.failure_class == "wrong_scene":
                if scenes[item.before_scene_id].semantic_label == after_label:
                    return item.transition_id
        return None


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _objective(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())[:500]


def _semantic_label(value: str) -> str:
    compact = re.sub(r"\s+", " ", value.strip().casefold())
    compact = re.sub(r"\b\d{1,4}x\d{1,4}\b", "<frame>", compact)
    compact = re.sub(r"\b\d{1,3}%\b", "<percent>", compact)
    compact = compact.split("。", 1)[0].split(";", 1)[0]
    return compact[:320] or "unknown_scene"


def _summary_is_generic(value: str) -> bool:
    normalized = value.strip().casefold()
    return not normalized or normalized.startswith("fresh android frame")


def _png_dhash(payload: bytes) -> str:
    """Compute a 64-bit dHash for ordinary 8-bit Android PNG frames.

    This intentionally uses only the standard library so experience recording
    does not add a machine-install dependency to the normal launcher.
    """
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    position = 8
    width = height = color_type = bit_depth = 0
    compressed = bytearray()
    while position + 12 <= len(payload):
        length = struct.unpack(">I", payload[position:position + 4])[0]
        chunk_type = payload[position + 4:position + 8]
        data = payload[position + 8:position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
        elif chunk_type == b"IDAT":
            compressed.extend(data)
        elif chunk_type == b"IEND":
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if not width or not height or bit_depth != 8 or channels is None:
        raise ValueError("unsupported PNG format")
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    if len(raw) != (stride + 1) * height:
        raise ValueError("invalid PNG scanline size")
    rows: list[bytearray] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        current = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += stride + 1
        for index in range(stride):
            left = current[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                current[index] = (current[index] + left) & 255
            elif filter_type == 2:
                current[index] = (current[index] + up) & 255
            elif filter_type == 3:
                current[index] = (current[index] + ((left + up) // 2)) & 255
            elif filter_type == 4:
                current[index] = (current[index] + _paeth(left, up, upper_left)) & 255
            elif filter_type != 0:
                raise ValueError("unsupported PNG filter")
        rows.append(current)
        previous = current
    samples: list[int] = []
    for sample_y in range(8):
        row = rows[min(height - 1, int((sample_y + 0.5) * height / 8))]
        for sample_x in range(9):
            pixel = min(width - 1, int((sample_x + 0.5) * width / 9)) * channels
            if color_type in {0, 4}:
                luminance = row[pixel]
            else:
                luminance = (299 * row[pixel] + 587 * row[pixel + 1]
                             + 114 * row[pixel + 2]) // 1000
            samples.append(luminance)
    bits = 0
    for row_index in range(8):
        for column in range(8):
            start = row_index * 9 + column
            bits = (bits << 1) | int(samples[start] > samples[start + 1])
    return f"{bits:016x}"


def _paeth(left: int, up: int, upper_left: int) -> int:
    prediction = left + up - upper_left
    left_distance = abs(prediction - left)
    up_distance = abs(prediction - up)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    return up if up_distance <= upper_left_distance else upper_left


def _anchors(value: str) -> tuple[str, ...]:
    quoted = re.findall(r'["“”「」]([^"“”「」]{1,40})["“”「」]', value)
    words = re.findall(r"[\u4e00-\u9fff]{2,12}|[a-zA-Z][a-zA-Z0-9_-]{2,30}", value)
    return tuple(dict.fromkeys(item.casefold() for item in (*quoted, *words)))[:24]


def _compatibility(scope: ScopeKey, summary: str) -> str:
    dimensions = re.search(r"(\d{2,5})x(\d{2,5})", summary)
    size = dimensions.group(0) if dimensions else "unknown-size"
    return f"{scope.ui_version}|{scope.device_class}|{scope.orientation}|{size}"


def _semantic_action(decision: Any, observation: Any) -> tuple[str, str | None, str | None]:
    kind = str(_value(decision, "kind", "unknown"))
    intent = _value(decision, "intent")
    if intent is None:
        return kind, None, None
    name = str(_value(intent, "name", "unknown"))
    arguments = dict(_value(intent, "arguments", {}))
    coordinates = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    if name in {"tap", "long_press"}:
        target = str(arguments.get("target_description") or "objective_anchor")
        target = re.sub(r"\s+", " ", target.strip().casefold())[:160]
        return f"{name}:ground:{target}", _region(arguments, observation), coordinates
    if name == "keyevent":
        return f"keyevent:{arguments.get('keycode', 'unknown')}", None, coordinates
    if name == "swipe":
        return "swipe:reground_from_current_scene", None, coordinates
    if name == "text":
        return "text:redacted", None, '{"text":"<redacted>"}'
    return name, None, coordinates


def _region(arguments: dict[str, Any], observation: Any) -> str | None:
    summary = str(_value(observation, "summary", ""))
    dimensions = re.search(r"(\d{2,5})x(\d{2,5})", summary)
    try:
        if dimensions is None:
            return None
        width, height = int(dimensions.group(1)), int(dimensions.group(2))
        x, y = float(arguments["x"]), float(arguments["y"])
        return f"r{min(3, max(0, int(y * 4 / height)))}c{min(3, max(0, int(x * 4 / width)))}"
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _failure_class(evidence: str, progress: bool, satisfied: bool) -> str | None:
    text = evidence.casefold()
    if any(item in text for item in ("wrong scene", "wrong page", "错误页面", "误入", "非目标页面")):
        return "wrong_scene"
    if not progress and not satisfied:
        return "no_progress"
    return None


def _candidate_used(item: RetrievedExperience, semantic_action: str) -> bool:
    if item.kind == "negative":
        return True
    return _semantic_action_matches(item.semantic_action, semantic_action) or (
        item.recovery_action is not None
        and _semantic_action_matches(item.recovery_action, semantic_action)
    )


def _grounded_action(semantic_action: str, region: str | None) -> str:
    return (
        f"{semantic_action}|region={region}"
        if region is not None else semantic_action
    )


def _action_region(semantic_action: str) -> str | None:
    marker = "|region="
    return semantic_action.rsplit(marker, 1)[1] if marker in semantic_action else None


def _semantic_action_matches(left: str, right: str) -> bool:
    left_region = _action_region(left)
    right_region = _action_region(right)
    if left_region is not None and left_region != right_region:
        return False
    left = left.split("|region=", 1)[0]
    right = right.split("|region=", 1)[0]
    left_kind = left.split(":", 1)[0]
    right_kind = right.split(":", 1)[0]
    if left_kind != right_kind:
        return False
    if left == right:
        return True
    left_text = "".join(left.casefold().split())
    right_text = "".join(right.casefold().split())
    left_pairs = {left_text[index:index + 2] for index in range(max(1, len(left_text) - 1))}
    right_pairs = {right_text[index:index + 2] for index in range(max(1, len(right_text) - 1))}
    union = left_pairs | right_pairs
    return bool(union) and len(left_pairs & right_pairs) / len(union) >= 0.18


def _trial_result(
    kind: str, adhered: bool, signal: OutcomeSignal | None,
    *, expected_transition_match: bool,
) -> str:
    if signal is None or signal.kind == "uncertain":
        return "uncertain"
    successful = signal.kind in {"immediate_success", "recovered", "task_success"}
    if kind == "negative":
        return "support" if adhered and successful else "failure"
    return "support" if adhered and (successful or expected_transition_match) else "failure"


def _reward(outcome: str) -> str:
    vectors = {
        "immediate_success": {"correctness": 1, "progress": 1, "recovery": 0},
        "recovered": {"correctness": 1, "progress": 1, "recovery": 1},
        "wrong_scene": {"correctness": -1, "progress": -1, "recovery": 0},
        "no_progress": {"correctness": -1, "progress": 0, "recovery": 0},
        "uncertain": {"correctness": 0, "progress": 0, "recovery": 0},
    }
    return json.dumps(vectors.get(outcome, {"correctness": 0, "progress": 0, "recovery": 0}),
                      sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
