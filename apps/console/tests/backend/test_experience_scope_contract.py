from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

import pytest

from ai_game_console.experience_runtime import ExperienceService, SQLiteExperienceStore
from ai_game_console.experience_runtime.domain import ExperienceCandidate, ScopeKey


def _scope(**overrides: str) -> ScopeKey:
    values = {
        "user_scope": "user:fixture",
        "account_scope": "account:primary",
        "application_id": "app:calendar",
        "goal_family": "task:daily-check",
        "ui_version": "ui:42",
        "device_class": "android_emulator",
        "orientation": "portrait",
        "task_scope": "task:daily-check",
        "subtask_scope": "subtask:inbox",
        "device_profile_id": "profile:pixel-a",
        "object_ref": "object:opaque-a",
        "conversation_ref": "conversation:opaque-a",
    }
    values.update(overrides)
    return ScopeKey(**values)


def _attempt(sequence: int, *, satisfied: bool, evidence: str = "verified target") -> dict:
    return {
        "attempt_id": f"attempt:{sequence}",
        "sequence": sequence,
        "decision": {"kind": "act", "intent": {"name": "tap", "arguments": {
            "x": 80, "y": 240, "target_description": "calendar inbox",
        }}},
        "before": {"evidence_id": f"before:{sequence}", "summary": "calendar home 720x1280"},
        "after": {"evidence_id": f"after:{sequence}", "summary": "calendar inbox 720x1280"},
        "transport": {"status": "accepted"},
        "verification": {
            "source": "verified_event",
            "satisfied": satisfied,
            "progress": satisfied,
            "evidence": evidence,
        },
    }


def _promoted_candidate(service: ExperienceService, *, source_task_id: str, scope: ScopeKey):
    service.begin_mobile_episode(
        goal_run_id=f"goal:{source_task_id}", source_task_id=source_task_id,
        goal_spec_revision=1, frozen_criteria_ids=("criterion:inbox",), scope=scope,
    )
    transition = service.record_mobile_attempt(
        source_task_id=source_task_id, objective="check calendar inbox",
        attempt=_attempt(1, satisfied=True),
    )
    assert transition is not None
    policy = service.confirm_goal_completion(
        source_task_id, goal_id=f"goal:{source_task_id}", completion_revision=1,
    )
    assert policy is not None
    return service.store.candidates_for_transition(transition.transition_id)[0]


def test_scope_contract_returns_no_hint_for_each_identity_mismatch(tmp_path: Path) -> None:
    service = ExperienceService(SQLiteExperienceStore(tmp_path / "experience.db"))
    original = _scope()
    _promoted_candidate(service, source_task_id="task:source", scope=original)

    mismatches = (
        {"account_scope": "account:other"},
        {"device_profile_id": "profile:pixel-b"},
        {"object_ref": "object:opaque-b"},
        {"conversation_ref": "conversation:opaque-b"},
        {"ui_version": "ui:43"},
        {"application_id": "app:mail"},
        {"task_scope": "task:other"},
        {"subtask_scope": "subtask:other"},
    )
    for index, changed in enumerate(mismatches, start=1):
        task_id = f"task:mismatch:{index}"
        service.begin_mobile_episode(
            goal_run_id=f"goal:{index}", source_task_id=task_id,
            goal_spec_revision=1, frozen_criteria_ids=("criterion:inbox",),
            scope=_scope(**changed),
        )
        assert service.retrieve_scoped_hints(
            source_task_id=task_id,
            objective="check calendar inbox",
            observation={"evidence_id": f"observe:{index}", "summary": "calendar home 720x1280"},
        ) == ()


def test_model_self_assessment_and_unknown_result_cannot_be_promoted(tmp_path: Path) -> None:
    service = ExperienceService(SQLiteExperienceStore(tmp_path / "experience.db"))
    service.begin_mobile_episode(
        goal_run_id="goal:untrusted", source_task_id="task:untrusted",
        goal_spec_revision=1, frozen_criteria_ids=("criterion",), scope=_scope(),
    )
    attempt = _attempt(1, satisfied=True, evidence="model says it succeeded")
    attempt["verification"]["source"] = "model"
    transition = service.record_mobile_attempt(
        source_task_id="task:untrusted", objective="check calendar inbox", attempt=attempt,
    )
    assert transition is not None and transition.immediate_outcome == "uncertain"
    assert service.store.counts()["candidates"] == 0
    assert service.confirm_goal_completion(
        "task:untrusted", goal_id="goal:untrusted", completion_revision=1,
    ) is None


def test_positive_negative_recovery_policy_is_evidence_backed_and_reversible(tmp_path: Path) -> None:
    service = ExperienceService(SQLiteExperienceStore(tmp_path / "experience.db"))
    scope = _scope()
    service.begin_mobile_episode(
        goal_run_id="goal:recovery", source_task_id="task:recovery",
        goal_spec_revision=1, frozen_criteria_ids=("criterion",), scope=scope,
    )
    wrong = _attempt(1, satisfied=False, evidence="wrong page")
    wrong["verification"]["progress"] = True
    first = service.record_mobile_attempt(
        source_task_id="task:recovery", objective="check calendar inbox", attempt=wrong,
    )
    back = _attempt(2, satisfied=True, evidence="returned to calendar home")
    back["before"] = {"evidence_id": "after:1", "summary": "calendar inbox 720x1280"}
    back["after"] = {"evidence_id": "before:1", "summary": "calendar home 720x1280"}
    back["decision"] = {"kind": "act", "intent": {"name": "keyevent", "arguments": {"keycode": 4}}}
    second = service.record_mobile_attempt(
        source_task_id="task:recovery", objective="check calendar inbox", attempt=back,
    )
    assert first is not None and first.immediate_outcome == "wrong_scene"
    assert second is not None and second.immediate_outcome == "recovered"
    policy = service.confirm_goal_completion(
        "task:recovery", goal_id="goal:recovery", completion_revision=1,
    )
    assert policy is not None
    kinds = {service.store.candidate(candidate_id).kind for candidate_id in policy.candidate_ids}
    assert kinds == {"negative", "recovery"}
    rolled_back = service.rollback(scope, to_revision=policy.revision)
    assert rolled_back.rollback_of_revision == policy.revision


def test_v2_scope_database_migrates_to_strict_v4_keys(tmp_path: Path) -> None:
    database = tmp_path / "experience.db"
    service = ExperienceService(SQLiteExperienceStore(database))
    candidate = _promoted_candidate(service, source_task_id="task:legacy", scope=_scope())
    with sqlite3.connect(database) as connection:
        old_scope = {
            "user_scope": "user:fixture", "account_scope": "account:primary",
            "application_id": "app:calendar", "goal_family": "task:daily-check",
            "ui_version": "ui:42", "device_class": "android_emulator", "orientation": "portrait",
        }
        old_json = json.dumps(old_scope, sort_keys=True, separators=(",", ":"))
        connection.execute("UPDATE experience_schema SET version = 2 WHERE singleton = 1")
        connection.execute("UPDATE experience_episodes SET scope_json = ?", (old_json,))
        connection.execute("UPDATE experience_candidates SET scope_json = ?, scope_key = ?", (old_json, old_json))
        connection.execute("UPDATE experience_policies SET scope_json = ?, scope_key = ?", (old_json, old_json))

    reopened = SQLiteExperienceStore(database)
    restored = reopened.candidate(candidate.candidate_id)
    assert restored.scope.device_profile_id == "legacy-profile"
    assert restored.scope.object_ref == "none"
    assert reopened.policies(restored.scope)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM experience_schema").fetchone()[0] == 6


def test_safe_projection_and_planner_hint_omit_sensitive_experience_content(tmp_path: Path) -> None:
    service = ExperienceService(SQLiteExperienceStore(tmp_path / "experience.db"))
    scope = _scope(
        object_ref="object:secret-contact", conversation_ref="conversation:private-thread",
    )
    candidate = ExperienceCandidate(
        candidate_id="candidate:safe", scope=scope, kind="positive",
        objective_matcher="private user goal", scene_matcher="raw ui tree <node secret=1>",
        semantic_action="tap:C:\\Users\\yun\\secret.png|region=r0c0",
        expected_next_scene="screenshot bytes and api-key=not-for-ui", recovery_action=None,
        support_count=2, failure_count=0, confidence=0.75,
        provenance_transition_ids=("transition:verified",), compatibility_key="compat:fixture",
        status="candidate", created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )
    service.store.upsert_candidate(candidate)
    projection = asdict(service.project_experience(candidate.candidate_id))
    encoded = json.dumps(projection, sort_keys=True)
    for forbidden in (
        "private-thread", "secret-contact", "secret.png", "api-key", "screenshot bytes", "raw ui tree",
    ):
        assert forbidden not in encoded
    assert projection["conversation_scoped"] is True
    assert projection["object_scoped"] is True
    assert projection["task_scoped"] is True
    assert projection["subtask_scoped"] is True
    assert projection["device_profile_id"] == "profile:pixel-a"


def test_scope_values_are_bounded() -> None:
    with pytest.raises(ValueError, match="bounded"):
        _scope(conversation_ref="x" * 513)
