from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .domain import (
    ActionTransition,
    AndroidExperienceCandidate,
    AndroidExperienceProjection,
    AndroidPlannerHint,
    AuthenticatedAndroidScope,
    TrustedVerificationAttestation,
    TrustedVerificationQuery,
    ExperienceCandidate,
    ExperienceEpisode,
    ExperiencePacket,
    ExperienceProjection,
    OutcomeSignal,
    PolicyRevision,
    RetrievedExperience,
    SceneState,
    ScopeKey,
    ScopedExperienceHint,
)
from .store import SQLiteExperienceStore
from ..visual_similarity import png_dhash
from ..android_ui_runtime.domain import (
    GoalVerificationRecord,
    ObservationEnvelope,
    opaque_digest,
    owner_scope_digest,
)
from ..android_ui_runtime.sanitizer import sanitize_text
from ..android_ui_runtime.store import (
    CheckpointBaselineAttestationPort,
    CheckpointBaselineQuery,
    TrustedCheckpointBaselineAttestation,
)


class TrustedVerificationAttestationPort(Protocol):
    """K2-owned immutable-checkpoint lookup used by K3 learning only."""

    def attest(self, query: TrustedVerificationQuery) -> TrustedVerificationAttestation | None: ...


_OPAQUE_DIGEST = re.compile(r"[a-f0-9]{64}")
_FABRICATED_SUCCESS_TASK_IDS = frozenset({
    "0b027ab0-b1c7-4288-be1d-d450a743b915",
    "5b673e56-c3af-4282-984b-4429c612f4d4",
})


class ExperienceService:
    """Canonical adapter over append-only evidence and reversible policy heads."""

    def __init__(
        self, store: SQLiteExperienceStore, *, enabled: bool = True,
        observation_payload: Callable[[str], bytes] | None = None,
        trusted_verification_port: TrustedVerificationAttestationPort | None = None,
        checkpoint_baseline_port: CheckpointBaselineAttestationPort | None = None,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.observation_payload = observation_payload
        # K3 never blesses a record merely because a caller can construct its
        # dataclass.  Composition supplies K2's typed immutable-checkpoint
        # attestation; omitted means cold/no learning.
        self.trusted_verification_port = trusted_verification_port
        # K2 owns the durable step/checkpoint baseline. K3 never accepts a
        # checkpoint integer from its caller.
        self.checkpoint_baseline_port = checkpoint_baseline_port

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
        task_scope: str = "generic-task",
        subtask_scope: str = "generic-subtask",
        device_profile_id: str | None = None,
        object_ref: str = "none",
        conversation_ref: str = "none",
        account_scope: str = "local-default",
    ) -> ExperienceEpisode | None:
        normalized = (skill_scope_id or "auto:generic/unknown/v1").removeprefix("auto:")
        application_id = normalized.split("/", 1)[0] if "/" in normalized else "generic"
        return self.begin_mobile_episode(
            goal_run_id=goal_run_id, source_task_id=source_task_id,
            goal_spec_revision=goal_spec_revision,
            frozen_criteria_ids=frozen_criteria_ids,
            scope=ScopeKey(
                user_scope="local-user",
                account_scope=account_scope,
                application_id=application_id,
                goal_family=normalized,
                ui_version="unknown",
                device_class=(target_id or "android").split(":", 1)[0],
                # The task is admitted before its first screenshot. Preserve
                # that uncertainty instead of recording a false portrait fact;
                # the scene compatibility key still binds the observed frame
                # dimensions before any candidate can be retrieved.
                orientation="unknown",
                task_scope=task_scope,
                subtask_scope=subtask_scope,
                # Existing compatibility callers do not own a DeviceProfile.
                # They retain the migrated legacy bucket; new long-task code
                # must pass its stable profile id and therefore gets strict
                # cross-profile isolation.
                device_profile_id=(device_profile_id or "legacy-profile"),
                object_ref=object_ref,
                conversation_ref=conversation_ref,
            ),
        )

    def begin_application_episode(
        self,
        *,
        goal_run_id: str,
        source_instance_id: str,
        goal_spec_revision: int,
        frozen_criteria_ids: tuple[str, ...],
        scope: ScopeKey,
    ) -> ExperienceEpisode | None:
        """Open the same canonical ledger for a long-lived capability binding."""

        return self.begin_mobile_episode(
            goal_run_id=goal_run_id,
            source_task_id=source_instance_id,
            goal_spec_revision=goal_spec_revision,
            frozen_criteria_ids=frozen_criteria_ids,
            scope=scope,
        )

    def record_delayed_outcome(
        self,
        *,
        source_instance_id: str,
        source_event_key: str,
        kind: str,
        source: str,
        attribution_scope: str,
        evidence_refs: tuple[str, ...],
        confidence: float = 1.0,
    ) -> OutcomeSignal | None:
        """Record an idempotent delayed/user outcome without inventing action reward.

        ``attribution_scope`` is an opaque person/conversation reference. It is
        retained with the signal provenance so a later policy adapter cannot
        treat one conversation's outcome as cross-person generic experience.
        """

        if not self.enabled:
            return None
        allowed = {
            "delayed_positive",
            "delayed_negative",
            "no_response",
            "user_approval",
            "user_rejection",
        }
        if kind not in allowed:
            raise ValueError("unsupported delayed outcome kind")
        if not source_event_key.strip() or not source.strip() or not attribution_scope.strip():
            raise ValueError("delayed outcome requires source and attribution scope")
        if not evidence_refs or any(not item.strip() for item in evidence_refs):
            raise ValueError("delayed outcome requires admissible evidence references")
        # [constraint-source: ARCH_INVARIANT; ref: experience store confidence CHECK]
        if isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
            raise ValueError("delayed outcome confidence must be between zero and one")
        try:
            episode = self.store.episode_for_task(source_instance_id)
        except KeyError:
            # [constraint-source: ARCH_INVARIANT; ref: attributable experience scope]
            return None
        signal_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"ai-game:delayed:{source_instance_id}:{source_event_key}",
            )
        )
        scoped_evidence = (
            f"attribution:{attribution_scope}",
            *tuple(evidence_refs),
        )
        return self.store.put_signal(OutcomeSignal(
            signal_id=signal_id,
            episode_id=episode.episode_id,
            transition_id=None,
            kind=kind,
            source=source,
            evidence_refs=scoped_evidence,
            confidence=float(confidence),
            reward_vector_json=_reward(kind),
            created_at=_utc_now(),
        ))

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

    def retrieve_scoped_hints(
        self, *, source_task_id: str, objective: str, observation: Any
    ) -> tuple[ScopedExperienceHint, ...]:
        """Return bounded, scope-matched hints; this method never dispatches.

        ``retrieve`` performs the exact ScopeKey lookup and creates the usage
        attribution record.  This projection intentionally drops raw scene,
        action arguments, evidence references, and any conversation/object
        reference before the planner receives the result.
        """

        packet = self.retrieve(
            source_task_id=source_task_id,
            objective=objective,
            observation=observation,
        )
        hints: list[ScopedExperienceHint] = []
        for item in packet.items[:8]:
            candidate = self.store.candidate(item.candidate_id)
            hints.append(ScopedExperienceHint(
                experience_id=candidate.candidate_id,
                kind=candidate.kind,
                action_kind=_action_kind(candidate.semantic_action),
                confidence=candidate.confidence,
                support_count=candidate.support_count,
                failure_count=candidate.failure_count,
                verified_provenance_count=len(candidate.provenance_transition_ids),
            ))
        return tuple(hints)

    def project_experience(self, candidate_id: str) -> ExperienceProjection:
        """Return a bounded projection safe for v2 host/UI consumers.

        It excludes DSH conversation text, opaque object/conversation values,
        evidence references, UI-tree/screenshot payloads, local paths, action
        arguments, and policy internals.
        """

        candidate = self.store.candidate(candidate_id)
        scope = candidate.scope
        return ExperienceProjection(
            schema_version=2,
            experience_id=candidate.candidate_id,
            kind=candidate.kind,
            application_id=scope.application_id,
            goal_family=scope.goal_family,
            ui_version=scope.ui_version,
            device_class=scope.device_class,
            orientation=scope.orientation,
            device_profile_id=scope.device_profile_id,
            task_scoped=scope.task_scope != "generic-task",
            subtask_scoped=scope.subtask_scope != "generic-subtask",
            object_scoped=scope.object_ref != "none",
            conversation_scoped=scope.conversation_ref != "none",
            status=candidate.status,
            confidence=candidate.confidence,
            support_count=candidate.support_count,
            failure_count=candidate.failure_count,
            verified_provenance_count=len(candidate.provenance_transition_ids),
        )

    # --- v4 authenticated Android UI experience ---------------------------------

    def begin_authenticated_android_episode(self, *, scope: AuthenticatedAndroidScope) -> str | None:
        """Start the v4 task-local ledger from composition-authenticated scope.

        This is intentionally separate from compatibility ``ScopeKey`` paths:
        no local-user/default/unknown row can enter this API or be promoted.
        """
        if not self.enabled:
            return None
        try:
            return self.store.put_android_episode(
                episode_id=_stable_id(
                    "episode", scope.task_id, scope.attestation_digest,
                ), scope=scope, created_at=_utc_now(),
            )
        except (OSError, sqlite3.Error, ValueError):
            return None

    def record_android_ui_progress(
        self, *, scope: AuthenticatedAndroidScope, step_id: str,
        checkpoint: CheckpointBaselineQuery, action_kind: str,
        semantic_anchor: str,
        kind: str = "progress",
    ) -> AndroidExperienceCandidate | None:
        """Record a sanitized, task-local UI edge; it never becomes reusable here."""
        if not self.enabled:
            return None
        attestation = self._trusted_checkpoint(scope, checkpoint)
        if attestation is None:
            return None
        observation = checkpoint.observation
        try:
            if kind not in {"progress", "negative", "recovery"}:
                return None
            cleaned_anchor = sanitize_text(semantic_anchor, maximum=240)
            if cleaned_anchor.do_not_learn:
                return None
            if action_kind not in {"tap", "long_press", "swipe", "input_text", "back", "home", "recents", "open_app", "wait"}:
                return None
            scene_marker = self._fresh_scene_marker(observation)
            step_provenance = _opaque_provenance("step", step_id)
        except Exception:
            return None
        episode_id = self.begin_authenticated_android_episode(scope=scope)
        if episode_id is None:
            return None
        candidate_id = _stable_id(
            "local-candidate", scope.attestation_digest, step_provenance,
            str(attestation.checkpoint_index), attestation.observation_digest,
            action_kind, kind, scene_marker,
        )
        candidate = AndroidExperienceCandidate(
            candidate_id=candidate_id, source_episode_id=episode_id, scope=scope,
            kind=kind, action_kind=action_kind, semantic_anchor=cleaned_anchor.text,
            expected_scene_marker=scene_marker,
            source_checkpoint_index=attestation.checkpoint_index,
            source_observation_digest=attestation.observation_digest,
            support_count=1 if kind in {"progress", "recovery"} else 0,
            failure_count=1 if kind == "negative" else 0,
            confidence=_android_confidence(1 if kind in {"progress", "recovery"} else 0,
                                           1 if kind == "negative" else 0),
            provenance_ids=(
                step_provenance,
                _opaque_provenance("observation", attestation.observation_digest),
            ),
            status="task_local", created_at=_utc_now(), updated_at=_utc_now(),
        )
        try:
            return self.store.put_android_candidate(candidate, reusable=False)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def derive_verified_android_candidate(
        self, *, scope: AuthenticatedAndroidScope, source_candidate_id: str,
        verification: GoalVerificationRecord, checkpoint: CheckpointBaselineQuery,
        verification_record_id: str | None = None,
    ) -> AndroidExperienceCandidate | None:
        """Promote one local edge only after a current K2 semantic success record."""
        if not self.enabled:
            return None
        baseline = self._trusted_checkpoint(scope, checkpoint)
        if baseline is None:
            return None
        attestation = self._verified_terminal_record(
            scope, verification, verification_record_id=verification_record_id, require_satisfied=True,
        )
        if attestation is None:
            return None
        if (
            not scope.cross_task_eligible
            or scope.task_id in _FABRICATED_SUCCESS_TASK_IDS
            or checkpoint.observation != verification.after
            or baseline.checkpoint_index <= 0
        ):
            return None
        try:
            candidate = self.store.android_candidate(source_candidate_id)
        except (OSError, sqlite3.Error, KeyError):
            return None
        if candidate.scope != scope or candidate.status != "task_local" or candidate.kind == "negative":
            return None
        if baseline.checkpoint_index <= candidate.source_checkpoint_index:
            return None
        # A progress edge is useful only after the enclosing goal is verified;
        # then it becomes a reusable positive edge, not a claim that the step
        # alone completed the original goal.
        promoted_kind = "positive" if candidate.kind == "progress" else candidate.kind
        reusable_scope = scope.reusable_scope()
        verification_digest = self._verification_digest(verification)
        promoted = AndroidExperienceCandidate(
            candidate_id=_stable_id(
                "reusable-candidate", source_candidate_id, verification_digest,
                str(baseline.checkpoint_index), baseline.observation_digest,
                baseline.causal_command_id or "no-command",
            ), source_episode_id=candidate.source_episode_id,
            scope=reusable_scope, kind=promoted_kind, action_kind=candidate.action_kind,
            semantic_anchor=candidate.semantic_anchor,
            expected_scene_marker=candidate.expected_scene_marker,
            source_checkpoint_index=candidate.source_checkpoint_index,
            source_observation_digest=candidate.source_observation_digest,
            support_count=max(1, candidate.support_count), failure_count=candidate.failure_count,
            confidence=_android_confidence(max(1, candidate.support_count), candidate.failure_count),
            provenance_ids=(
                *candidate.provenance_ids,
                f"verification:{verification_digest}",
            ),
            status="active", created_at=_utc_now(), updated_at=_utc_now(),
        )
        try:
            return self.store.promote_android_candidate(
                source_candidate_id=source_candidate_id, scope=scope,
                terminal_verification=self._verification_provenance(
                    verification, attestation, baseline,
                ),
                promoted=promoted,
            )
        except (OSError, sqlite3.Error, ValueError, KeyError):
            return None

    def trusted_android_terminal_attestation(
        self, *, scope: AuthenticatedAndroidScope, record_id: str,
        revision: int, step_index: int,
    ) -> TrustedVerificationAttestation | None:
        """Read exactly one K2-owned terminal proof for K3-only recovery."""

        try:
            if (
                self.trusted_verification_port is None
                or not _opaque_record_id(record_id)
                or revision != scope.criteria_revision
                or step_index < 0
            ):
                return None
            query = TrustedVerificationQuery(
                scope=scope,
                scope_digest=scope.attestation_digest,
                record_id=record_id,
                task_id=scope.task_id,
                revision=revision,
                step_index=step_index,
            )
            attestation = self.trusted_verification_port.attest(query)
            if (
                not isinstance(attestation, TrustedVerificationAttestation)
                or attestation.record_id != query.record_id
                or attestation.task_id != query.task_id
                or attestation.scope_digest != query.scope_digest
                or attestation.revision != query.revision
                or attestation.step_index != query.step_index
                or attestation.already_satisfied
                or not isinstance(attestation.immutable_record, Mapping)
            ):
                return None
            return TrustedVerificationAttestation(
                record_id=str(attestation.record_id),
                task_id=str(attestation.task_id),
                scope_digest=str(attestation.scope_digest),
                revision=int(attestation.revision),
                step_index=int(attestation.step_index),
                checkpoint_index=int(attestation.checkpoint_index),
                already_satisfied=bool(attestation.already_satisfied),
                immutable_record=json.loads(_canonical_json(attestation.immutable_record)),
            )
        except Exception:
            return None

    def derive_verified_android_candidate_from_attestation(
        self, *, scope: AuthenticatedAndroidScope, source_candidate_id: str,
        checkpoint: CheckpointBaselineQuery,
        attestation: TrustedVerificationAttestation,
    ) -> AndroidExperienceCandidate | None:
        """Promote a local edge from an attested immutable K2 terminal row.

        This recovery-only path never re-runs the semantic model and cannot
        mint a K2 result: the K2 attestor must first return the same scoped,
        immutable terminal record that it owns.
        """

        if not self.enabled:
            return None
        baseline = self._trusted_checkpoint(scope, checkpoint)
        record = attestation.immutable_record
        after = record.get("after") if isinstance(record, Mapping) else None
        observation = checkpoint.observation
        if (
            baseline is None
            or attestation.task_id != scope.task_id
            or attestation.scope_digest != scope.attestation_digest
            or attestation.revision != scope.criteria_revision
            or attestation.step_index + 1 != attestation.checkpoint_index
            or not isinstance(after, Mapping)
            or record.get("overall") != "satisfied"
            or record.get("already_satisfied") is not False
            or record.get("criteria_digest") != scope.criteria_digest
            or record.get("runner_kind") != scope.runner_kind
            or record.get("runner_version") != scope.runner_version
            or record.get("owner_scope_digest") != scope.owner_scope_digest
            or after.get("screenshot_digest") != observation.screenshot_digest
            or after.get("ui_tree_digest") != observation.ui_tree_digest
            or after.get("device_state_digest") != observation.device_state_digest
            or after.get("freshness_digest")
            != opaque_digest("observation-freshness", observation.freshness_token)
            or after.get("causality_command_id") != observation.causality_command_id
            or baseline.checkpoint_index != attestation.checkpoint_index
        ):
            return None
        if not scope.cross_task_eligible or scope.task_id in _FABRICATED_SUCCESS_TASK_IDS:
            return None
        try:
            candidate = self.store.android_candidate(source_candidate_id)
        except (OSError, sqlite3.Error, KeyError):
            return None
        if (
            candidate.scope != scope
            or candidate.status != "task_local"
            or candidate.kind == "negative"
            or baseline.checkpoint_index <= candidate.source_checkpoint_index
        ):
            return None
        verification_digest = hashlib.sha256(
            _canonical_json(record).encode("utf-8")
        ).hexdigest()
        promoted = AndroidExperienceCandidate(
            candidate_id=_stable_id(
                "reusable-candidate", source_candidate_id, verification_digest,
                str(baseline.checkpoint_index), baseline.observation_digest,
                baseline.causal_command_id or "no-command",
            ), source_episode_id=candidate.source_episode_id,
            scope=scope.reusable_scope(), kind="positive",
            action_kind=candidate.action_kind, semantic_anchor=candidate.semantic_anchor,
            expected_scene_marker=candidate.expected_scene_marker,
            source_checkpoint_index=candidate.source_checkpoint_index,
            source_observation_digest=candidate.source_observation_digest,
            support_count=max(1, candidate.support_count),
            failure_count=candidate.failure_count,
            confidence=_android_confidence(max(1, candidate.support_count), candidate.failure_count),
            provenance_ids=(*candidate.provenance_ids, f"verification:{verification_digest}"),
            status="active", created_at=_utc_now(), updated_at=_utc_now(),
        )
        provenance = {
            "digest": verification_digest,
            "revision": attestation.revision,
            "criteria_digest": scope.criteria_digest,
            "after_screenshot_digest": observation.screenshot_digest,
            "after_tree_digest": observation.ui_tree_digest or "",
            "after_device_state_digest": observation.device_state_digest,
            "checkpoint_index": baseline.checkpoint_index,
            "checkpoint_observation_digest": baseline.observation_digest,
            "causal_command_digest": hashlib.sha256(
                (baseline.causal_command_id or "").encode("utf-8")
            ).hexdigest(),
            "record_digest": hashlib.sha256(attestation.record_id.encode("utf-8")).hexdigest(),
        }
        try:
            return self.store.promote_android_candidate(
                source_candidate_id=source_candidate_id,
                scope=scope,
                terminal_verification=provenance,
                promoted=promoted,
            )
        except (OSError, sqlite3.Error, ValueError, KeyError):
            return None

    def retrieve_planner_hints(
        self, *, scope: AuthenticatedAndroidScope,
        checkpoint: CheckpointBaselineQuery, step_id: str,
    ) -> tuple[AndroidPlannerHint, ...]:
        """Return only persisted, sanitized hints after a fresh observation.

        Retrieval is attribution only.  It cannot create an action, claim a
        command, or bypass the K2 semantic verifier.
        """
        if not self.enabled:
            return ()
        attestation = self._trusted_checkpoint(scope, checkpoint)
        if attestation is None:
            return ()
        observation = checkpoint.observation
        try:
            self.store.android_episode_scope(scope.task_id, expected_scope=scope)
        except (KeyError, OSError, sqlite3.Error, ValueError):
            return ()
        scene = self._fresh_scene_marker(observation)
        try:
            matches = [
                candidate for candidate, _reusable in self.store.android_candidates_for_scope(scope)
                if candidate.expected_scene_marker == scene
            ][:8]
        except (OSError, sqlite3.Error):
            return ()
        try:
            step_provenance = _opaque_provenance("step", step_id)
            retrieval_id = _stable_id(
                "retrieval", scope.attestation_digest, step_provenance,
                str(attestation.checkpoint_index), attestation.observation_digest,
            )
            candidate_ids = self.store.record_android_retrieval(
                retrieval_id=retrieval_id, task_id=scope.task_id,
                step_id=step_provenance, scope=scope,
                observation_token=_opaque_provenance("freshness", observation.freshness_token),
                observation_digest=attestation.observation_digest,
                checkpoint_index=attestation.checkpoint_index,
                candidate_ids=tuple(item.candidate_id for item in matches), created_at=_utc_now(),
            )
        except (OSError, sqlite3.Error, ValueError):
            return ()
        match_by_id = {item.candidate_id: item for item in matches}
        canonical_matches = tuple(
            match_by_id[candidate_id]
            for candidate_id in candidate_ids
            if candidate_id in match_by_id
        )
        return tuple(AndroidPlannerHint(
            candidate_id=item.candidate_id, kind=item.kind, action_kind=item.action_kind,
            semantic_anchor=item.semantic_anchor, expected_scene_marker=item.expected_scene_marker,
            confidence=item.confidence, support_count=item.support_count,
            failure_count=item.failure_count, provenance_count=len(item.provenance_ids),
            retrieval_id=retrieval_id,
            scope=item.scope, provenance_ids=item.provenance_ids,
            retrieval_step_id=step_provenance,
            retrieval_checkpoint_index=attestation.checkpoint_index,
            retrieval_observation_digest=attestation.observation_digest,
        ) for item in canonical_matches)

    def record_android_hint_outcome(
        self, *, scope: AuthenticatedAndroidScope, retrieval_id: str,
        candidate_id: str, step_id: str, result: str,
        checkpoint: CheckpointBaselineQuery,
        verification: GoalVerificationRecord | None,
        verification_record_id: str | None = None,
    ) -> bool:
        """Durably attribute a certain support/contradiction after restart.

        Unknown outcomes deliberately have no confidence effect.  Two certain
        contradictions deprecate the candidate through a new policy revision;
        history is append-only and never silently deleted.
        """
        if not self.enabled:
            return False
        if result not in {"support", "contradiction", "unknown"}:
            return False
        baseline = self._trusted_checkpoint(scope, checkpoint)
        if baseline is None:
            return False
        if result == "unknown":
            digest = None
            verification_token = None
            verification_observation_digest = None
            verification_checkpoint_index = baseline.checkpoint_index
        else:
            if verification is None or verification_record_id is None:
                return False
            if checkpoint.observation != verification.after:
                return False
            attestation = self._verified_terminal_record(
                scope, verification, verification_record_id=verification_record_id, require_satisfied=False,
            )
            if attestation is None:
                return False
            if result == "support" and verification.overall != "satisfied":
                return False
            if result == "contradiction" and verification.overall != "unsatisfied":
                return False
            digest = self._verification_digest(verification)
            verification_token = _opaque_provenance(
                "freshness", checkpoint.observation.freshness_token,
            )
            verification_observation_digest = baseline.observation_digest
            verification_checkpoint_index = baseline.checkpoint_index
        try:
            return self.store.record_android_usage(
                usage_id=_stable_id(
                    "usage", retrieval_id, candidate_id,
                    _opaque_provenance("step", step_id), result,
                    digest or baseline.observation_digest,
                ), retrieval_id=retrieval_id,
                candidate_id=candidate_id, task_id=scope.task_id,
                step_id=_opaque_provenance("step", step_id), scope=scope,
                result=result, verification_digest=digest,
                verification_token=verification_token,
                verification_observation_digest=verification_observation_digest,
                verification_checkpoint_index=verification_checkpoint_index,
                created_at=_utc_now(),
            )
        except (OSError, sqlite3.Error, ValueError, KeyError):
            return False

    def project_android_experience(
        self, candidate_id: str, *, requester_scope: AuthenticatedAndroidScope,
    ) -> AndroidExperienceProjection | None:
        """Return a coarse projection without owner/device/version/content facts."""
        try:
            candidate = self.store.android_candidate(candidate_id)
        except (OSError, sqlite3.Error):
            return None
        if candidate.scope.owner_scope_digest != requester_scope.owner_scope_digest:
            raise KeyError(candidate_id)
        if candidate.status == "task_local":
            if candidate.scope != requester_scope:
                raise KeyError(candidate_id)
        elif (
            candidate.kind == "negative" or not requester_scope.cross_task_eligible
            or candidate.scope != requester_scope.reusable_scope()
        ):
            raise KeyError(candidate_id)
        return AndroidExperienceProjection(
            schema_version=4, experience_id=candidate.candidate_id, kind=candidate.kind,
            runner_kind=candidate.scope.runner_kind, status=candidate.status,
            confidence=candidate.confidence, support_count=candidate.support_count,
            failure_count=candidate.failure_count,
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
        verification_source = str(_value(verification, "source", "device_verifier"))
        verified_by_device = verification_source in {
            "device_verifier", "kernel_verifier", "verified_event",
        }
        uncertain = bool(_value(verification, "uncertain", False)) or not verified_by_device
        satisfied = bool(_value(verification, "satisfied", False)) and verified_by_device
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
            source=("mobile_task_immediate_verifier" if verified_by_device
                    else "untrusted_model_assessment"),
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
            if not _admissible_verified_signal(signal):
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
            return integrity, png_dhash(payload)
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
                or not _admissible_verified_signal(signal)
                or signal.kind not in {"no_progress", "wrong_scene"}
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

    @staticmethod
    def _fresh_scene_marker(observation: ObservationEnvelope) -> str:
        """Text-free structural scene identity; screenshots remain K2 evidence."""
        payload = json.dumps({
            "schema": "android-ui-structure-v2",
            "tree": observation.ui_tree_digest or "no-tree",
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return "scene:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def derive_structural_android_candidate(
        self, *, source: AndroidExperienceCandidate, ui_tree_digest: str,
    ) -> AndroidExperienceCandidate | None:
        """Idempotently derive a v2 marker candidate without mutating legacy proof."""
        if (
            not self.enabled or source.status != "active"
            or source.kind != "positive" or not _OPAQUE_DIGEST.fullmatch(ui_tree_digest)
        ):
            return None
        marker = "scene:" + hashlib.sha256(json.dumps({
            "schema": "android-ui-structure-v2", "tree": ui_tree_digest,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
        migrated = AndroidExperienceCandidate(
            candidate_id=_stable_id("structural-marker-v2", source.candidate_id, marker),
            source_episode_id=source.source_episode_id, scope=source.scope,
            kind=source.kind, action_kind=source.action_kind,
            semantic_anchor=source.semantic_anchor, expected_scene_marker=marker,
            source_checkpoint_index=source.source_checkpoint_index,
            # This new row deliberately carries no copied screenshot-derived
            # observation digest.  Its source pointer is stable and opaque,
            # while the legacy row retains the original K2 provenance.
            source_observation_digest=hashlib.sha256(
                ("structural-source-v2\x00" + source.candidate_id + "\x00" + ui_tree_digest)
                .encode("utf-8")
            ).hexdigest(),
            support_count=source.support_count, failure_count=source.failure_count,
            confidence=source.confidence,
            # Keep provenance within the store's opaque observation namespace;
            # it fingerprints the v2 structure marker, never UI text, bounds,
            # screenshot pixels, or a screenshot digest.
            provenance_ids=(*source.provenance_ids, _opaque_provenance("observation", marker)),
            status="active", created_at=_utc_now(), updated_at=_utc_now(),
        )
        try:
            return self.store.put_android_candidate(migrated, reusable=True)
        except (OSError, sqlite3.Error, ValueError):
            return None

    @staticmethod
    def _fresh_observation(scope: AuthenticatedAndroidScope, observation: ObservationEnvelope) -> None:
        if observation.task_id != scope.task_id:
            raise ValueError("fresh observation belongs to another task")
        if observation.profile_id != scope.profile_id or observation.profile_generation != scope.profile_generation:
            raise ValueError("fresh observation profile binding mismatch")
        if not observation.freshness_token or not observation.screenshot_digest or not observation.device_state_digest:
            raise ValueError("fresh observation is incomplete")

    def _trusted_checkpoint(
        self,
        scope: AuthenticatedAndroidScope,
        query: CheckpointBaselineQuery,
    ) -> TrustedCheckpointBaselineAttestation | None:
        """Resolve and canonicalize K2's sealed baseline inside one cold boundary."""

        try:
            port = self.checkpoint_baseline_port
            if port is None or not isinstance(query, CheckpointBaselineQuery):
                return None
            snapshot = query.snapshot
            criteria = query.criteria
            observation = query.observation
            self._fresh_observation(scope, observation)
            if (
                snapshot.task_id != scope.task_id
                or owner_scope_digest(snapshot.owner) != scope.owner_scope_digest
                or snapshot.profile_id != scope.profile_id
                or snapshot.profile_generation != scope.profile_generation
                or snapshot.runner_kind != scope.runner_kind
                or snapshot.runner_version != scope.runner_version
                or snapshot.runner_binding_id != scope.subtask_id
                or snapshot.revision != scope.criteria_revision
                or criteria.revision != scope.criteria_revision
                or criteria.digest != scope.criteria_digest
                or query.runner_kind != scope.runner_kind
                or query.runner_version != scope.runner_version
                or observation.boot_id != snapshot.boot_id
                or observation.canonical_device_id != snapshot.canonical_device_id
            ):
                return None
            attestation = port.attest(query)
            if (
                not isinstance(attestation, TrustedCheckpointBaselineAttestation)
                or port.validates_for(query, attestation) is not True
                or attestation.task_id != scope.task_id
                or attestation.owner_scope_digest != scope.owner_scope_digest
                or attestation.runner_kind != scope.runner_kind
                or attestation.runner_version != scope.runner_version
                or attestation.runner_binding_digest
                != opaque_digest("runner-binding", scope.subtask_id)
                or attestation.revision != scope.criteria_revision
                or attestation.criteria_digest != scope.criteria_digest
                or not isinstance(attestation.checkpoint_index, int)
                or isinstance(attestation.checkpoint_index, bool)
                or attestation.checkpoint_index < 1
                or not _OPAQUE_DIGEST.fullmatch(attestation.observation_digest)
                or attestation.causal_command_id
                != observation.causality_command_id
            ):
                return None
            # Rebuild the immutable value so no malformed proxy/subclass can
            # execute code after the port call or mutate what K3 consumes.
            return TrustedCheckpointBaselineAttestation(
                task_id=str(attestation.task_id),
                owner_scope_digest=str(attestation.owner_scope_digest),
                runner_kind=str(attestation.runner_kind),
                runner_version=int(attestation.runner_version),
                runner_binding_digest=str(attestation.runner_binding_digest),
                revision=int(attestation.revision),
                criteria_digest=str(attestation.criteria_digest),
                checkpoint_index=int(attestation.checkpoint_index),
                observation_digest=str(attestation.observation_digest),
                causal_command_id=(
                    str(attestation.causal_command_id)
                    if attestation.causal_command_id is not None else None
                ),
                _seal=attestation._seal,
            )
        except Exception:
            return None

    def _verified_terminal_record(
        self, scope: AuthenticatedAndroidScope, verification: GoalVerificationRecord,
        *, verification_record_id: str | None, require_satisfied: bool,
    ) -> TrustedVerificationAttestation | None:
        if verification.task_id != scope.task_id:
            return None
        expected_owner = owner_scope_digest(verification.owner)
        if expected_owner != scope.owner_scope_digest:
            return None
        if verification.runner_kind != scope.runner_kind or verification.runner_version != scope.runner_version:
            return None
        # The old Settings foreground verifier proves only package foreground.
        # Reusable Android experience requires the K2 semantic-record protocol.
        if verification.prompt_version != "android-ui-semantic-v1":
            return None
        if (
            verification.revision != scope.criteria_revision
            or verification.criteria_digest != scope.criteria_digest
            or verification.latest_step_index < 0
        ):
            return None
        try:
            self._fresh_observation(scope, verification.after)
        except ValueError:
            return None
        if not verification.verdicts or any(item.state == "unknown" for item in verification.verdicts):
            return None
        if require_satisfied and (
            verification.overall != "satisfied"
            or any(item.state != "satisfied" or not item.anchors for item in verification.verdicts)
        ):
            return None
        if verification.overall not in {"satisfied", "unsatisfied"}:
            return None
        if verification.already_satisfied:
            return None
        return self._record_is_trusted(scope, verification, verification_record_id)

    def _record_is_trusted(
        self, scope: AuthenticatedAndroidScope, verification: GoalVerificationRecord,
        verification_record_id: str | None,
    ) -> TrustedVerificationAttestation | None:
        try:
            if self.trusted_verification_port is None or not _opaque_record_id(verification_record_id):
                return None
            query = TrustedVerificationQuery(
                scope=scope, scope_digest=scope.attestation_digest,
                record_id=verification_record_id, task_id=verification.task_id,
                revision=verification.revision, step_index=verification.latest_step_index,
            )
            attestation = self.trusted_verification_port.attest(query)
            if not isinstance(attestation, TrustedVerificationAttestation):
                return None
            if (
                attestation.record_id != query.record_id
                or attestation.task_id != query.task_id
                or attestation.scope_digest != query.scope_digest
                or attestation.revision != query.revision
                or attestation.step_index != query.step_index
                or not isinstance(attestation.checkpoint_index, int)
                or isinstance(attestation.checkpoint_index, bool)
                or attestation.checkpoint_index < 1
                or attestation.already_satisfied
                or not isinstance(attestation.immutable_record, Mapping)
            ):
                return None
            expected = verification.private_durable_record()
            if _canonical_json(attestation.immutable_record) != _canonical_json(expected):
                return None
            return TrustedVerificationAttestation(
                record_id=str(attestation.record_id),
                task_id=str(attestation.task_id),
                scope_digest=str(attestation.scope_digest),
                revision=int(attestation.revision),
                step_index=int(attestation.step_index),
                checkpoint_index=int(attestation.checkpoint_index),
                already_satisfied=bool(attestation.already_satisfied),
                immutable_record=json.loads(_canonical_json(attestation.immutable_record)),
            )
        except Exception:
            return None

    @staticmethod
    def _verification_digest(verification: GoalVerificationRecord) -> str:
        payload = {
            "task": verification.task_id,
            "runner": (verification.runner_kind, verification.runner_version),
            "revision": verification.revision,
            "criteria": verification.criteria_digest,
            "step": verification.latest_step_index,
            "overall": verification.overall,
            "after": (
                verification.after.screenshot_digest,
                verification.after.ui_tree_digest,
                verification.after.device_state_digest,
                verification.after.freshness_token,
            ),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @classmethod
    def _verification_provenance(
        cls, verification: GoalVerificationRecord,
        attestation: TrustedVerificationAttestation,
        baseline: TrustedCheckpointBaselineAttestation,
    ) -> dict[str, str | int]:
        return {
            "digest": cls._verification_digest(verification),
            "revision": verification.revision,
            "criteria_digest": verification.criteria_digest,
            "after_screenshot_digest": verification.after.screenshot_digest,
            "after_tree_digest": verification.after.ui_tree_digest or "",
            "after_device_state_digest": verification.after.device_state_digest,
            "checkpoint_index": baseline.checkpoint_index,
            "checkpoint_observation_digest": baseline.observation_digest,
            "causal_command_digest": (
                hashlib.sha256(baseline.causal_command_id.encode("utf-8")).hexdigest()
                if baseline.causal_command_id is not None else ""
            ),
            "record_digest": hashlib.sha256(attestation.record_id.encode("utf-8")).hexdigest(),
        }


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _opaque_provenance(namespace: str, raw: str) -> str:
    """Persist a domain-separated opaque reference, never the caller token."""
    if not isinstance(raw, str) or not raw or len(raw) > 4_096:
        raise ValueError("Android experience provenance must be bounded")
    digest = hashlib.sha256((namespace + "\x00" + raw).encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def _stable_id(namespace: str, *parts: str) -> str:
    if (
        not isinstance(namespace, str)
        or not namespace
        or not parts
        or any(not isinstance(part, str) or len(part) > 4_096 for part in parts)
    ):
        raise ValueError("stable experience identity requires bounded string parts")
    digest = hashlib.sha256(
        "\x00".join((namespace, *parts)).encode("utf-8"),
    ).hexdigest()
    return f"{namespace}_{digest}"


def _opaque_record_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{16,160}", value))


def _canonical_json(value: Mapping[str, Any] | dict[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


def _action_kind(semantic_action: str) -> str:
    return semantic_action.split(":", 1)[0].split("|region=", 1)[0][:64]


def _admissible_verified_signal(signal: OutcomeSignal | None) -> bool:
    """Require a verifier-originated, certain signal with evidence references."""

    return bool(
        signal is not None
        and signal.source == "mobile_task_immediate_verifier"
        and signal.kind != "uncertain"
        and signal.confidence >= 1.0
        and signal.evidence_refs
        and all(item.strip() for item in signal.evidence_refs)
    )


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


def _android_confidence(support: int, failures: int) -> float:
    return round((support + 1) / (support + failures + 2), 6)


def _reward(outcome: str) -> str:
    vectors = {
        "immediate_success": {"correctness": 1, "progress": 1, "recovery": 0},
        "recovered": {"correctness": 1, "progress": 1, "recovery": 1},
        "wrong_scene": {"correctness": -1, "progress": -1, "recovery": 0},
        "no_progress": {"correctness": -1, "progress": 0, "recovery": 0},
        "uncertain": {"correctness": 0, "progress": 0, "recovery": 0},
        "delayed_positive": {"external": 1, "user": 0},
        "delayed_negative": {"external": -1, "user": 0},
        "no_response": {"external": -1, "user": 0},
        "user_approval": {"external": 0, "user": 1},
        "user_rejection": {"external": 0, "user": -1},
    }
    return json.dumps(vectors.get(outcome, {"correctness": 0, "progress": 0, "recovery": 0}),
                      sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
