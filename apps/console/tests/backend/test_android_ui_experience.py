from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_game_console.android_ui_runtime.domain import (
    Anchor, AndroidUiAction, AndroidUiActionIntent, CanonicalSnapshot,
    CriteriaRevision, Criterion, CriterionVerdict, GoalVerificationRecord,
    ObservationEnvelope, OwnerBinding, RoleDecision, UiNode,
    criteria_digest, evidence_marker, opaque_digest, opaque_node_id,
    owner_scope_digest,
)
from ai_game_console.android_ui_runtime.store import (
    CheckpointBaselineQuery, SQLiteAndroidUiStepStore,
    TrustedCheckpointBaselineAttestation,
)
from ai_game_console.android_ui_runtime.semantic_verification import (
    EvidenceBoundSemanticVerifier,
)
from ai_game_console.experience_runtime import (
    ExperienceService, SQLiteExperienceStore, authenticated_android_scope,
)
from ai_game_console.experience_runtime.domain import TrustedVerificationAttestation


MARKER = evidence_marker("page_title", "target page")
CRITERIA_ITEMS = (Criterion("criterion-1", "target page is visible", (MARKER,)),)
CRITERIA = CriteriaRevision(1, CRITERIA_ITEMS, criteria_digest(CRITERIA_ITEMS))
FABRICATED_TASKS = (
    "0b027ab0-b1c7-4288-be1d-d450a743b915",
    "5b673e56-c3af-4282-984b-4429c612f4d4",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(*, task="task-a", subtask="binding-a", principal="principal-a",
           controller="controller-a", **changes):
    values = dict(
        principal_id=principal, controller_id=controller, profile_id="profile-a",
        profile_generation=1, app_package="com.example.app", app_version="42",
        android_api="35", android_build="build-a", resolution="1440x2560",
        density="640", orientation="portrait", observation_schema="android-observation-v1",
        runner_kind="android_ui_agent", runner_version=1, criteria_revision=1,
        criteria_digest=CRITERIA.digest, task_id=task, subtask_id=subtask,
        account_scope="account-a", object_ref="object-a", conversation_ref="conversation-a",
    )
    values.update(changes)
    return authenticated_android_scope(**values)


def _snapshot(scope, *, principal="principal-a", controller="controller-a"):
    return CanonicalSnapshot(
        scope.task_id, OwnerBinding(principal, controller), scope.criteria_revision, "running",
        profile_id=scope.profile_id, profile_generation=scope.profile_generation,
        boot_id="boot-a", canonical_device_id="device-a", runner_kind=scope.runner_kind,
        runner_version=scope.runner_version, runner_binding_id=scope.subtask_id,
    )


def _observation(scope=None, *, token="fresh-a", shot="shot-a", command_id=None,
                 ui_text="safe target"):
    scope = scope or _scope()
    return ObservationEnvelope(
        scope.task_id, scope.profile_id, scope.profile_generation, "boot-a", "device-a",
        "opaque-shot-ref", _digest(shot), "opaque-tree-ref", _digest("tree:" + shot),
        "opaque-state-ref", _digest("state:" + shot), "2026-09-02T00:00:00+00:00",
        token, "safe summary", (UiNode(
            opaque_node_id("fixture:target"), ui_text, (0.1, 0.1, 0.9, 0.2), False,
            "page_title", MARKER,
        ),), command_id,
    )


def _query(scope, observation, *, snapshot=None, criteria=CRITERIA):
    return CheckpointBaselineQuery(
        snapshot=snapshot or _snapshot(scope), criteria=criteria, observation=observation,
        runner_kind=scope.runner_kind, runner_version=scope.runner_version,
    )


class _CheckpointPort:
    def __init__(self):
        self.seal = object()
        self.rows = {}
        self.malformed = None
        self.raise_validate = False
        self.validation_result = True
        self.return_none = False
        self.issued = {}

    def attest(self, query):
        if self.return_none:
            return None
        if self.malformed is not None:
            if isinstance(self.malformed, BaseException):
                raise self.malformed
            return self.malformed
        observation = query.observation
        key = (
            query.snapshot.task_id, observation.screenshot_digest,
            observation.ui_tree_digest or "", observation.device_state_digest,
            observation.freshness_token, observation.causality_command_id or "",
        )
        if key not in self.rows:
            self.rows[key] = 1 + sum(item[0] == key[0] for item in self.rows)
        attestation = TrustedCheckpointBaselineAttestation(
            query.snapshot.task_id, owner_scope_digest(query.snapshot.owner), query.runner_kind,
            query.runner_version, opaque_digest("runner-binding", query.snapshot.runner_binding_id),
            query.snapshot.revision, query.criteria.digest, self.rows[key],
            opaque_digest("fake-checkpoint", "|".join(key)), observation.causality_command_id,
            self.seal,
        )
        self.issued[attestation] = query
        return attestation

    def validates(self, attestation):
        if self.raise_validate:
            raise ValueError("malformed typed attestation")
        return isinstance(attestation, TrustedCheckpointBaselineAttestation) and attestation._seal is self.seal

    def validates_for(self, query, attestation):
        if self.raise_validate:
            raise ValueError("malformed typed attestation")
        if self.validation_result is not True:
            return self.validation_result
        return self.validates(attestation) and self.issued.get(attestation) == query


class _LegacyCheckpointPort:
    """Deliberately models a K4 adapter that only implements the retired API."""

    def __init__(self, delegate):
        self.delegate = delegate

    def attest(self, query):
        return self.delegate.attest(query)

    def validates(self, attestation):
        return self.delegate.validates(attestation)


class _StaleCheckpointPort:
    def __init__(self, delegate, stale_attestation):
        self.delegate = delegate
        self.stale_attestation = stale_attestation

    def attest(self, _query):
        return self.stale_attestation

    def validates(self, attestation):
        return self.delegate.validates(attestation)

    def validates_for(self, query, attestation):
        return self.delegate.validates_for(query, attestation)


class _VerificationPort:
    def __init__(self):
        self.rows = {}
        self.malformed = None

    def attest(self, query):
        if self.malformed is not None:
            if isinstance(self.malformed, BaseException):
                raise self.malformed
            return self.malformed
        return self.rows.get(query.record_id)


class _UnknownRole:
    def verify_goal(self, *_args, **_kwargs):
        return (CriterionVerdict("criterion-1", "unknown"),)


def _service(path, *, checkpoints=None, verifications=None):
    checkpoints = checkpoints or _CheckpointPort()
    verifications = verifications or _VerificationPort()
    return ExperienceService(
        SQLiteExperienceStore(path), trusted_verification_port=verifications,
        checkpoint_baseline_port=checkpoints,
    ), checkpoints, verifications


def _verification(scope, *, overall="satisfied", token="terminal-after",
                  shot="terminal-after", already_satisfied=False):
    if already_satisfied:
        after = _observation(scope, token=token, shot=shot)
        before, command_id, primitive = after, None, "already_satisfied"
    else:
        command_id = "command-terminal"
        before = _observation(scope, token="terminal-before", shot="terminal-before")
        after = _observation(scope, token=token, shot=shot, command_id=command_id)
        primitive = "progress" if overall == "satisfied" else "no_progress"
    state = overall if overall in {"satisfied", "unsatisfied"} else "unknown"
    anchors = () if state != "satisfied" else (Anchor(
        "ui_node", after.freshness_token, node_id=after.ui_nodes[0].node_id,
        semantic_marker=MARKER,
    ),)
    return GoalVerificationRecord(
        scope.task_id, OwnerBinding("principal-a", "controller-a"), scope.runner_kind,
        scope.runner_version, scope.criteria_revision, scope.criteria_digest, 1, before, after,
        "model-v1", "android-ui-semantic-v1",
        (CriterionVerdict("criterion-1", state, anchors),), state, already_satisfied,
        runner_binding_id=scope.subtask_id, artifact_digest=_digest("artifact"),
        grounding_digest=_digest("grounding"), causal_command_id=command_id,
        primitive_outcome=primitive,
    )


def _publish(port, scope, record):
    payload = json.dumps(record.private_durable_record(), sort_keys=True, separators=(",", ":"))
    record_id = "record_" + _digest(scope.attestation_digest + "\0" + payload)
    port.rows[record_id] = TrustedVerificationAttestation(
        record_id, scope.task_id, scope.attestation_digest, record.revision,
        record.latest_step_index, 999, record.already_satisfied,
        record.private_durable_record(),
    )
    return record_id


def _local(service, scope, observation=None, *, kind="progress", action="tap"):
    observation = observation or _observation(scope)
    assert service.begin_authenticated_android_episode(scope=scope)
    candidate = service.record_android_ui_progress(
        scope=scope, step_id="stable-step", checkpoint=_query(scope, observation),
        action_kind=action, semantic_anchor="verified target page", kind=kind,
    )
    assert candidate is not None
    return candidate


def _promote(service, verifications, scope):
    local = _local(service, scope)
    record = _verification(scope)
    record_id = _publish(verifications, scope, record)
    promoted = service.derive_verified_android_candidate(
        scope=scope, source_candidate_id=local.candidate_id, verification=record,
        checkpoint=_query(scope, record.after), verification_record_id=record_id,
    )
    assert promoted is not None
    return promoted


def test_k3_api_has_no_caller_checkpoint_or_baseline_integer():
    for name in ("record_android_ui_progress", "retrieve_planner_hints",
                 "derive_verified_android_candidate", "record_android_hint_outcome"):
        parameters = inspect.signature(getattr(ExperienceService, name)).parameters
        assert "checkpoint_index" not in parameters and "baseline" not in parameters
        assert "checkpoint" in parameters


@pytest.mark.parametrize("change", [
    {"principal": "principal-b"}, {"controller": "controller-b"},
    {"profile_id": "profile-b"}, {"profile_generation": 2},
    {"app_package": "com.example.other"}, {"app_version": "43"},
    {"android_api": "36"}, {"android_build": "build-b"},
    {"resolution": "1080x2400"}, {"density": "480"}, {"orientation": "landscape"},
    {"observation_schema": "schema-v2"}, {"runner_version": 2},
    {"criteria_digest": "c" * 64}, {"account_scope": "account-b"},
    {"object_ref": "object-b"}, {"conversation_ref": "conversation-b"},
])
def test_exact_authenticated_scope_mismatch_is_cold(tmp_path, change):
    service, _, verifications = _service(tmp_path / "experience.db")
    promoted = _promote(service, verifications, _scope())
    change = dict(change)
    principal = change.pop("principal", "principal-a")
    controller = change.pop("controller", "controller-a")
    target = _scope(task="task-target", subtask="binding-target",
                    principal=principal, controller=controller, **change)
    assert service.begin_authenticated_android_episode(scope=target)
    observation = _observation(target, token="target-fresh")
    assert service.retrieve_planner_hints(
        scope=target, checkpoint=_query(
            target, observation, snapshot=_snapshot(target, principal=principal, controller=controller),
        ), step_id="target-step",
    ) == ()
    assert service.store.android_candidate(promoted.candidate_id).status == "active"


def test_verified_positive_cross_task_task_local_polarities_and_restart_idempotency(tmp_path):
    database = tmp_path / "experience.db"
    service, checkpoints, verifications = _service(database)
    promoted = _promote(service, verifications, _scope())
    target = _scope(task="task-target", subtask="binding-target")
    locals_ = {
        _local(service, target).candidate_id,
        _local(service, target, kind="negative", action="back").candidate_id,
        _local(service, target, kind="recovery", action="home").candidate_id,
    }
    observation = _observation(target, token="retrieve-target")
    hints = service.retrieve_planner_hints(
        scope=target, checkpoint=_query(target, observation), step_id="retrieve-step",
    )
    assert {item.candidate_id for item in hints} == locals_ | {promoted.candidate_id}
    other = _scope(task="task-other", subtask="binding-other")
    assert service.begin_authenticated_android_episode(scope=other)
    assert [item.candidate_id for item in service.retrieve_planner_hints(
        scope=other, checkpoint=_query(other, _observation(other, token="other")), step_id="other",
    )] == [promoted.candidate_id]
    restarted = ExperienceService(
        SQLiteExperienceStore(database), trusted_verification_port=verifications,
        checkpoint_baseline_port=checkpoints,
    )
    assert restarted.retrieve_planner_hints(
        scope=target, checkpoint=_query(target, observation), step_id="retrieve-step",
    )
    assert restarted.store.android_counts()["retrievals"] == 2


def test_related_criteria_retrieve_a_safe_hint_but_keep_terminal_contracts_distinct(tmp_path):
    service, checkpoints, verifications = _service(tmp_path / "related-criteria.db")
    source = _scope()
    promoted = _promote(service, verifications, source)
    other_criteria = CriteriaRevision(
        1,
        (Criterion("criterion-other", "a different terminal target", (MARKER,)),),
        "d" * 64,
    )
    target = _scope(task="task-related", subtask="binding-related", criteria_digest=other_criteria.digest)
    assert source.criteria_digest != target.criteria_digest
    assert source.reuse_key == target.reuse_key
    assert service.begin_authenticated_android_episode(scope=target)
    observation = _observation(target, token="related-fresh")
    hints = service.retrieve_planner_hints(
        scope=target,
        checkpoint=_query(target, observation, criteria=other_criteria),
        step_id="related-step",
    )
    assert [item.candidate_id for item in hints] == [promoted.candidate_id]
    assert service.store.android_episode_scope(target.task_id).criteria_digest == other_criteria.digest


def test_selected_hint_usage_is_durable_idempotent_and_never_upgrades_unknown_outcome(tmp_path):
    service, checkpoints, verifications = _service(tmp_path / "selected-usage.db")
    source = _scope()
    promoted = _promote(service, verifications, source)
    target = _scope(task="task-selected", subtask="binding-selected")
    assert service.begin_authenticated_android_episode(scope=target)
    observation = _observation(target, token="selected-fresh")
    query = _query(target, observation)
    hint = service.retrieve_planner_hints(scope=target, checkpoint=query, step_id="selected-step")[0]
    assert hint.candidate_id == promoted.candidate_id
    assert service.record_android_hint_outcome(
        scope=target, retrieval_id=hint.retrieval_id, candidate_id=hint.candidate_id,
        step_id="selected-step", result="unknown", checkpoint=query, verification=None,
    )
    # Crash/retry reuses the stable usage identity and cannot double-count.
    assert not service.record_android_hint_outcome(
        scope=target, retrieval_id=hint.retrieval_id, candidate_id=hint.candidate_id,
        step_id="selected-step", result="unknown", checkpoint=query, verification=None,
    )
    assert service.store.android_counts()["retrievals"] == 1
    assert service.store.android_counts()["usage"] == 1
    retained = service.store.android_candidate(promoted.candidate_id)
    assert retained.support_count == promoted.support_count
    assert retained.failure_count == promoted.failure_count
    restarted = ExperienceService(
        SQLiteExperienceStore(tmp_path / "selected-usage.db"),
        trusted_verification_port=verifications, checkpoint_baseline_port=checkpoints,
    )
    assert restarted.store.android_counts()["usage"] == 1


def test_source_baseline_is_k2_owned_and_old_proof_cannot_fake_update(tmp_path):
    service, _, verifications = _service(tmp_path / "experience.db")
    scope = _scope()
    source_observation = _observation(scope)
    local = _local(service, scope, source_observation)
    record = _verification(scope)
    record_id = _publish(verifications, scope, record)
    assert service.derive_verified_android_candidate(
        scope=scope, source_candidate_id=local.candidate_id, verification=record,
        checkpoint=_query(scope, source_observation), verification_record_id=record_id,
    ) is None
    assert service.derive_verified_android_candidate(
        scope=scope, source_candidate_id=local.candidate_id, verification=record,
        checkpoint=_query(scope, record.after), verification_record_id=record_id,
    ) is not None


@pytest.mark.parametrize("malformed", [SimpleNamespace(), SimpleNamespace(checkpoint_index="2"),
                                        ValueError("port failure")])
def test_malformed_typed_attestation_after_port_call_is_cold(tmp_path, malformed):
    checkpoints = _CheckpointPort()
    checkpoints.malformed = malformed
    service, _, _ = _service(tmp_path / "experience.db", checkpoints=checkpoints)
    scope = _scope()
    assert service.begin_authenticated_android_episode(scope=scope)
    query = _query(scope, _observation(scope))
    assert service.record_android_ui_progress(
        scope=scope, step_id="step", checkpoint=query, action_kind="tap",
        semantic_anchor="verified target page",
    ) is None
    assert service.retrieve_planner_hints(scope=scope, checkpoint=query, step_id="step") == ()
    assert service.store.android_counts()["candidates"] == 0
    assert service.store.android_counts()["retrievals"] == 0


def test_foreign_or_validation_throwing_attestation_is_cold(tmp_path):
    checkpoints = _CheckpointPort()
    service, _, _ = _service(tmp_path / "experience.db", checkpoints=checkpoints)
    scope = _scope()
    assert service.begin_authenticated_android_episode(scope=scope)
    query = _query(scope, _observation(scope))
    checkpoints.malformed = replace(checkpoints.attest(query), task_id="foreign")
    assert service.retrieve_planner_hints(scope=scope, checkpoint=query, step_id="x") == ()
    checkpoints.malformed = None
    checkpoints.raise_validate = True
    assert service.retrieve_planner_hints(scope=scope, checkpoint=query, step_id="x") == ()


@pytest.mark.parametrize("validation_result", [False, None, "true", 1])
def test_non_true_query_validation_is_cold(tmp_path, validation_result):
    checkpoints = _CheckpointPort()
    checkpoints.validation_result = validation_result
    service, _, _ = _service(tmp_path / "experience.db", checkpoints=checkpoints)
    scope = _scope()
    assert service.begin_authenticated_android_episode(scope=scope)
    query = _query(scope, _observation(scope))
    assert service.record_android_ui_progress(
        scope=scope, step_id="step", checkpoint=query, action_kind="tap",
        semantic_anchor="verified target page",
    ) is None
    assert service.retrieve_planner_hints(
        scope=scope, checkpoint=query, step_id="step",
    ) == ()
    assert service.store.android_counts() == {
        "episodes": 1, "candidates": 0, "retrievals": 0, "usage": 0, "policies": 0,
    }


def test_missing_query_validator_and_none_attestation_are_cold(tmp_path):
    checkpoints = _CheckpointPort()
    scope = _scope()
    query = _query(scope, _observation(scope))
    for port in (_LegacyCheckpointPort(checkpoints), checkpoints):
        checkpoints.return_none = port is checkpoints
        service, _, _ = _service(
            tmp_path / f"experience-{int(port is checkpoints)}.db",
            checkpoints=port,
        )
        assert service.begin_authenticated_android_episode(scope=scope)
        assert service.record_android_ui_progress(
            scope=scope, step_id="step", checkpoint=query, action_kind="tap",
            semantic_anchor="verified target page",
        ) is None
        assert service.retrieve_planner_hints(
            scope=scope, checkpoint=query, step_id="step",
        ) == ()
        assert service.store.android_counts()["candidates"] == 0
        assert service.store.android_counts()["retrievals"] == 0


def test_already_satisfied_and_fabricated_success_never_promote_action_policy(tmp_path):
    service, _, verifications = _service(tmp_path / "experience.db")
    scope = _scope()
    local = _local(service, scope)
    record = _verification(scope, already_satisfied=True)
    assert service.derive_verified_android_candidate(
        scope=scope, source_candidate_id=local.candidate_id, verification=record,
        checkpoint=_query(scope, record.after), verification_record_id=_publish(verifications, scope, record),
    ) is None
    for task_id in FABRICATED_TASKS:
        scope = _scope(task=task_id, subtask="binding-fabricated")
        local = _local(service, scope)
        record = _verification(scope)
        assert service.derive_verified_android_candidate(
            scope=scope, source_candidate_id=local.candidate_id, verification=record,
            checkpoint=_query(scope, record.after), verification_record_id=_publish(verifications, scope, record),
        ) is None


def test_hint_is_action_free_exact_scoped_and_durably_attributed(tmp_path):
    service, _, verifications = _service(tmp_path / "experience.db")
    promoted = _promote(service, verifications, _scope())
    target = _scope(task="task-target", subtask="binding-target")
    assert service.begin_authenticated_android_episode(scope=target)
    hint = service.retrieve_planner_hints(
        scope=target, checkpoint=_query(target, _observation(target, token="hint")), step_id="hint-step",
    )[0]
    encoded = json.dumps(asdict(hint), sort_keys=True)
    assert hint.candidate_id == promoted.candidate_id
    assert hint.scope == target.reusable_scope()
    assert hint.provenance_ids and hint.retrieval_checkpoint_index >= 1
    assert not hasattr(hint, "dispatch") and not hasattr(hint, "create_action")
    for forbidden in ("opaque-shot-ref", "opaque-tree-ref", "principal-a", "controller-a",
                      "account-a", "object-a", "conversation-a", '"x"', '"y"'):
        assert forbidden not in encoded


def test_same_token_polarity_and_additive_contradiction_deprecation(tmp_path):
    service, _, verifications = _service(tmp_path / "experience.db")
    candidate = _promote(service, verifications, _scope())
    target = _scope(task="task-target", subtask="binding-target")
    assert service.begin_authenticated_android_episode(scope=target)

    def evidence(retrieve_token, after_token, suffix):
        hint = service.retrieve_planner_hints(
            scope=target, checkpoint=_query(target, _observation(target, token=retrieve_token)),
            step_id="step-" + suffix,
        )[0]
        record = _verification(target, overall="unsatisfied", token=after_token, shot="after-" + suffix)
        return hint, record, _publish(verifications, target, record)

    hint, record, record_id = evidence("same", "same", "same")
    assert not service.record_android_hint_outcome(
        scope=target, retrieval_id=hint.retrieval_id, candidate_id=candidate.candidate_id,
        step_id="step-same", result="contradiction", verification=record,
        checkpoint=_query(target, record.after), verification_record_id=record_id,
    )
    for index in (1, 2):
        hint, record, record_id = evidence(f"r{index}", f"a{index}", str(index))
        assert service.record_android_hint_outcome(
            scope=target, retrieval_id=hint.retrieval_id, candidate_id=candidate.candidate_id,
            step_id=f"step-{index}", result="contradiction", verification=record,
            checkpoint=_query(target, record.after), verification_record_id=record_id,
        )
    durable = service.store.android_candidate(candidate.candidate_id)
    assert durable.status == "deprecated" and durable.failure_count == 2
    assert service.store.android_counts()["policies"] == 1


def test_sensitive_sample_is_not_learned_and_raw_sqlite_is_clean(tmp_path):
    database = tmp_path / "experience.db"
    service, _, _ = _service(database)
    scope = _scope()
    assert service.begin_authenticated_android_episode(scope=scope)
    observation = _observation(
        scope, token="https://private.invalid/fresh",
        ui_text="Contact Alice account@example.com selector=//secret",
    )
    assert service.record_android_ui_progress(
        scope=scope, step_id="C:\\private\\secret-step", checkpoint=_query(scope, observation),
        action_kind="input_text", semantic_anchor="password: K3-RAW-TYPED-SECRET",
    ) is None
    raw = database.read_bytes().decode("latin1", errors="ignore")
    for forbidden in ("K3-RAW-TYPED-SECRET", "private.invalid", "Contact Alice",
                      "account@example.com", "//secret", "C:\\private", "opaque-shot-ref"):
        assert forbidden not in raw


def test_unavailable_experience_is_cold(tmp_path, monkeypatch):
    service, _, _ = _service(tmp_path / "experience.db")
    scope = _scope()
    assert service.begin_authenticated_android_episode(scope=scope)
    def unavailable(*_args, **_kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(service.store, "android_candidates_for_scope", unavailable)
    assert service.retrieve_planner_hints(
        scope=scope, checkpoint=_query(scope, _observation(scope)), step_id="cold",
    ) == ()


def test_unbounded_retrieval_step_provenance_is_cold_without_pollution(tmp_path):
    service, _, _ = _service(tmp_path / "experience.db")
    scope = _scope()
    assert service.begin_authenticated_android_episode(scope=scope)
    assert service.retrieve_planner_hints(
        scope=scope,
        checkpoint=_query(scope, _observation(scope)),
        step_id="x" * 4_097,
    ) == ()
    assert service.store.android_counts() == {
        "episodes": 1, "candidates": 0, "retrievals": 0, "usage": 0, "policies": 0,
    }


def test_real_k2_port_derives_checkpoint_from_durable_step(tmp_path):
    scope, observation = _scope(), _observation(_scope())
    snapshot = _snapshot(scope)
    step_store = SQLiteAndroidUiStepStore(tmp_path / "steps.db")
    step_store.freeze_criteria(scope.task_id, CRITERIA)
    step_store.reserve_step(
        task_id=scope.task_id, revision=1, before=observation, snapshot=snapshot,
    )
    service = ExperienceService(
        SQLiteExperienceStore(tmp_path / "experience.db"),
        checkpoint_baseline_port=step_store.checkpoint_attestation_port(),
    )
    candidate = service.record_android_ui_progress(
        scope=scope, step_id="durable", checkpoint=_query(scope, observation, snapshot=snapshot),
        action_kind="tap", semantic_anchor="verified target page",
    )
    assert candidate is not None and candidate.source_checkpoint_index == 1
    assert candidate.source_observation_digest != observation.screenshot_digest


def test_real_k2_stale_a_attestation_is_cold_for_all_k3_paths(tmp_path):
    scope = _scope()
    snapshot = _snapshot(scope)
    initial = _observation(scope, token="fresh-initial", shot="initial")
    observation_a = _observation(
        scope, token="fresh-a", shot="observation-a", command_id="shared-command",
        ui_text="Contact Alice K3-STALE-RAW-SECRET-A",
    )
    observation_b = _observation(
        scope, token="fresh-b", shot="observation-b", command_id="shared-command",
        ui_text="account@example.com K3-STALE-RAW-SECRET-B",
    )
    steps_database = tmp_path / "steps.db"
    experience_database = tmp_path / "experience.db"
    step_store = SQLiteAndroidUiStepStore(steps_database)
    step_store.freeze_criteria(scope.task_id, CRITERIA)

    def persist_after(before, after):
        reservation = step_store.reserve_step(
            task_id=scope.task_id, revision=scope.criteria_revision,
            before=before, snapshot=snapshot,
        )
        action = AndroidUiAction("back")
        reservation = step_store.record_decision(
            reservation, RoleDecision("action", action),
        )
        intent = AndroidUiActionIntent.create(
            task_id=scope.task_id, criteria_revision=scope.criteria_revision,
            step_index=reservation.step_index, action=action,
        )
        step_store.record_intent(reservation, intent)
        reservation = step_store.record_claim(
            reservation, claim_id="shared-command",
            action_intent_id=intent.action_intent_id,
        )
        step_store.record_after(reservation, after)
        step_store.record_primitive_outcome(
            reservation, command_id="shared-command", outcome="progress",
        )
        step_store.record_verification(EvidenceBoundSemanticVerifier(_UnknownRole()).verify(
            snapshot=snapshot, runner_kind=scope.runner_kind,
            runner_version=scope.runner_version, goal="bounded goal",
            criteria=CRITERIA, before=before, after=after,
            latest_step_index=reservation.step_index,
            causal_command_id="shared-command", primitive_outcome="progress",
        ))

    persist_after(initial, observation_a)
    persist_after(observation_a, observation_b)
    query_a = _query(scope, observation_a, snapshot=snapshot)
    query_b = _query(scope, observation_b, snapshot=snapshot)
    real_port = step_store.checkpoint_attestation_port()
    attestation_a = real_port.attest(query_a)
    attestation_b = real_port.attest(query_b)
    assert attestation_a is not None and attestation_a.checkpoint_index == 1
    assert attestation_b is not None and attestation_b.checkpoint_index == 2
    assert real_port.validates_for(query_a, attestation_a)
    assert real_port.validates_for(query_b, attestation_b)

    service = ExperienceService(
        SQLiteExperienceStore(experience_database),
        checkpoint_baseline_port=real_port,
    )
    candidate_a = service.record_android_ui_progress(
        scope=scope, step_id="exact-a", checkpoint=query_a, action_kind="tap",
        semantic_anchor="verified target page",
    )
    candidate_b = service.record_android_ui_progress(
        scope=scope, step_id="exact-b", checkpoint=query_b, action_kind="back",
        semantic_anchor="verified target page",
    )
    assert candidate_a is not None and candidate_a.source_checkpoint_index == 1
    assert candidate_b is not None and candidate_b.source_checkpoint_index == 2
    hints = service.retrieve_planner_hints(
        scope=scope, checkpoint=query_b, step_id="exact-retrieval",
    )
    assert len(hints) == 1 and hints[0].candidate_id == candidate_b.candidate_id

    stale_port = _StaleCheckpointPort(real_port, attestation_a)
    assert stale_port.validates(attestation_a)
    assert not stale_port.validates_for(query_b, attestation_a)
    service.checkpoint_baseline_port = stale_port
    counts_before = service.store.android_counts()
    verification = _verification(scope)
    assert service.record_android_ui_progress(
        scope=scope, step_id="stale-record", checkpoint=query_b, action_kind="tap",
        semantic_anchor="verified target page",
    ) is None
    assert service.retrieve_planner_hints(
        scope=scope, checkpoint=query_b, step_id="stale-retrieval",
    ) == ()
    assert service.derive_verified_android_candidate(
        scope=scope, source_candidate_id=candidate_a.candidate_id,
        verification=verification, checkpoint=query_b,
        verification_record_id="record_" + "0" * 64,
    ) is None
    assert not service.record_android_hint_outcome(
        scope=scope, retrieval_id=hints[0].retrieval_id,
        candidate_id=candidate_b.candidate_id, step_id="stale-outcome",
        result="unknown", checkpoint=query_b, verification=None,
    )
    assert service.store.android_counts() == counts_before
    assert {
        key: counts_before[key]
        for key in ("candidates", "retrievals", "usage", "policies")
    } == {"candidates": 2, "retrievals": 1, "usage": 0, "policies": 0}

    for database in (steps_database, experience_database):
        raw = database.read_bytes()
        for forbidden in (
            b"opaque-shot-ref", b"opaque-tree-ref", b"opaque-state-ref",
            b"principal-a", b"controller-a", b"fresh-a", b"fresh-b",
            b"Contact Alice", b"account@example.com",
            b"K3-STALE-RAW-SECRET-A", b"K3-STALE-RAW-SECRET-B",
        ):
            assert forbidden not in raw


def test_projection_and_promotion_replay_are_idempotent_and_secret_free(tmp_path):
    service, _, verifications = _service(tmp_path / "experience.db")
    scope = _scope()
    local = _local(service, scope)
    record = _verification(scope)
    arguments = dict(
        scope=scope, source_candidate_id=local.candidate_id, verification=record,
        checkpoint=_query(scope, record.after), verification_record_id=_publish(verifications, scope, record),
    )
    first = service.derive_verified_android_candidate(**arguments)
    second = service.derive_verified_android_candidate(**arguments)
    assert first and second and first.candidate_id == second.candidate_id
    projection = asdict(service.project_android_experience(first.candidate_id, requester_scope=scope))
    assert set(projection) == {"schema_version", "experience_id", "kind", "runner_kind",
                               "status", "confidence", "support_count", "failure_count"}
    encoded = json.dumps(projection)
    assert all(value not in encoded for value in ("principal-a", "controller-a", "profile-a", "scene:"))
    assert service.store.android_counts()["candidates"] == 2
