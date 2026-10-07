from __future__ import annotations

import json
import sqlite3
import hashlib
from dataclasses import replace

import pytest

import ai_game_console.android_ui_runtime.domain as android_ui_domain
import ai_game_console.android_ui_runtime.semantic_verification as semantic_module
from ai_game_console.android_ui_runtime.domain import (
    Anchor,
    AndroidUiAction, AndroidUiActionIntent, CanonicalSnapshot, CriteriaRevision, Criterion,
    GoalVerificationRecord, GroundedUiNode, ObservationEnvelope, ObservationGroundingQuery,
    OwnerBinding, CriterionVerdict, RoleDecision, TrustedObservationGroundingManifest, UiNode,
    compile_criteria, criteria_digest, evidence_marker, grounding_manifest_digest,
    observation_grounding_query, opaque_digest, opaque_node_id, OrderedWaypoint,
    CRITERIA_IDENTITY_CURRENT, CRITERIA_IDENTITY_LEGACY_HASHED,
    CRITERIA_IDENTITY_LEGACY_RAW,
)
from ai_game_console.android_ui_runtime.semantic_verification import EvidenceBoundSemanticVerifier
from ai_game_console.android_ui_runtime.store import (
    CheckpointBaselineQuery, SQLiteAndroidUiStepStore,
)


def _observation() -> ObservationEnvelope:
    return ObservationEnvelope("task-1", "profile-1", 1, "boot-1", "device-1", "artifact-private", "a" * 64, "tree-private", "b" * 64, "state-private", "c" * 64, "now", "fresh-1", "message: hello private contact Alice")


def _snapshot() -> CanonicalSnapshot:
    return CanonicalSnapshot(
        "task-1", OwnerBinding("principal-private", "controller-private"),
        1, "running", profile_id="profile-1", profile_generation=1,
        boot_id="boot-1", canonical_device_id="device-1",
        runner_kind="android_ui_agent", runner_version=1,
        runner_binding_id="binding-1",
    )


class _UnknownRole:
    def verify_goal(self, *args: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
        return (CriterionVerdict("c1", "unknown"),)


class _SatisfiedRole:
    def __init__(self, marker: str, token: str, node_id: str) -> None:
        self._verdict = CriterionVerdict(
            "c1", "satisfied", (Anchor(
                "ui_node", token, node_id=node_id, semantic_marker=marker,
            ),),
        )

    def verify_goal(self, *args: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
        return (self._verdict,)


class _TrustedGrounding:
    def __init__(self) -> None:
        self._seal = object()
        self.manifest: TrustedObservationGroundingManifest | None = None

    def trust(
        self, observation: ObservationEnvelope, step_index: int, *,
        query: ObservationGroundingQuery | None = None,
        nodes: tuple[GroundedUiNode, ...] | None = None,
    ) -> TrustedObservationGroundingManifest:
        query = query or observation_grounding_query(
            snapshot=_snapshot(), observation=observation,
            runner_kind="android_ui_agent", runner_version=1, step_index=step_index,
        )
        nodes = nodes if nodes is not None else tuple(GroundedUiNode(
            item.node_id, item.semantic_kind, item.semantic_marker,
            item.clickable, item.bounds,
        ) for item in observation.ui_nodes)
        self.manifest = TrustedObservationGroundingManifest(
            query, nodes, grounding_manifest_digest(query, nodes), self._seal,
        )
        return self.manifest

    def resolve(
        self, query: ObservationGroundingQuery,
    ) -> TrustedObservationGroundingManifest | None:
        return self.manifest

    def validates(self, manifest: TrustedObservationGroundingManifest) -> bool:
        return manifest._attestation is self._seal


def _target_observation(
    *, nodes: object | None = None,
) -> tuple[ObservationEnvelope, str, str]:
    marker = evidence_marker("state", "Soul Alice chat completed")
    node_id = opaque_node_id("chat-completed")
    values = nodes if nodes is not None else (
        UiNode(node_id, "chat complete", semantic_kind="state", semantic_marker=marker),
    )
    observation = ObservationEnvelope(
        "task-1", "profile-1", 1, "boot-1", "device-1",
        "shot-target", "d" * 64, "tree-target", "e" * 64,
        "state-target", "f" * 64, "now", "fresh-target", "K2_UI_RAW_SENTINEL",
        values,  # type: ignore[arg-type]
        None,
    )
    return observation, marker, node_id


def _target_criteria(marker: str) -> CriteriaRevision:
    values = (Criterion("c1", "bounded target state", (marker,)),)
    return CriteriaRevision(1, values, criteria_digest(values))


def _reserve_terminal(
    store: SQLiteAndroidUiStepStore, criteria: CriteriaRevision,
    observation: ObservationEnvelope,
) -> None:
    store.freeze_criteria("task-1", criteria)
    reservation = store.reserve_step(
        task_id="task-1", revision=1, before=observation, snapshot=_snapshot(),
    )
    store.record_decision(reservation, RoleDecision("terminal_candidate"))


def _trusted_success(
    grounding: _TrustedGrounding, criteria: CriteriaRevision,
    observation: ObservationEnvelope, marker: str, node_id: str,
) -> GoalVerificationRecord:
    grounding.trust(observation, 0)
    return EvidenceBoundSemanticVerifier(
        _SatisfiedRole(marker, observation.freshness_token, node_id), grounding=grounding,
    ).verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="bounded target state", criteria=criteria,
        before=observation, after=observation, latest_step_index=0,
        already_satisfied=True,
    )


def _verified_already(
    criteria: CriteriaRevision, before: ObservationEnvelope,
) -> GoalVerificationRecord:
    return EvidenceBoundSemanticVerifier(_UnknownRole()).verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="bounded goal", criteria=criteria, before=before, after=before,
        latest_step_index=0, already_satisfied=True,
    )


def test_partly_satisfied_verification_retains_grounding_without_claiming_success(tmp_path) -> None:
    grounding = _TrustedGrounding()
    store = SQLiteAndroidUiStepStore(tmp_path / "partial.db", grounding=grounding)
    observation, marker, node_id = _target_observation()
    criteria_values = (Criterion("c1", "visible target", (marker,)), Criterion("c2", "another target", (evidence_marker("page_title", "another target"),)))
    criteria = CriteriaRevision(1, criteria_values, criteria_digest(criteria_values))
    _reserve_terminal(store, criteria, observation)
    grounding.trust(observation, 0)
    class PartialRole:
        def verify_goal(self, *args, **kwargs):
            return (_SatisfiedRole(marker, observation.freshness_token, node_id)._verdict, CriterionVerdict("c2", "unknown"))
    record = EvidenceBoundSemanticVerifier(PartialRole(), grounding=grounding).verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="two target conditions", criteria=criteria, before=observation, after=observation,
        latest_step_index=0, already_satisfied=True,
    )
    assert record.overall == "unknown"
    store.record_verification(record)
    assert store.safe_goal_verifications("task-1")[0]["overall"] == "unknown"
    assert store.trusted_terminal_for(_snapshot(), criteria, runner_kind="android_ui_agent", runner_version=1, model_version="unknown", prompt_version="android-ui-semantic-v1") is None


def test_retry_without_decision_refreshes_before_but_bound_decision_stays_immutable(tmp_path) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "steps.db")
    first = _observation()
    fresh = replace(first, freshness_token="fresh-2", screenshot_digest="d" * 64)
    reservation = store.reserve_step(task_id="task-1", revision=1, before=first, snapshot=_snapshot())
    retried = store.reserve_step(task_id="task-1", revision=1, before=fresh, snapshot=_snapshot())
    assert retried.step_index == reservation.step_index
    assert store.before_matches(retried, fresh)
    store.record_decision(retried, RoleDecision("action", AndroidUiAction("back")))
    store.reserve_step(task_id="task-1", revision=1, before=first, snapshot=_snapshot())
    assert store.before_matches(retried, fresh)
    assert not store.before_matches(retried, first)


def test_freeze_and_intent_are_immutable_and_input_text_is_redacted(tmp_path: object) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "steps.sqlite")  # type: ignore[operator]
    from ai_game_console.android_ui_runtime.domain import criteria_digest
    values = (Criterion("c1", "open page"),)
    criteria = CriteriaRevision(1, values, criteria_digest(values))
    store.freeze_criteria("task-1", criteria)
    with pytest.raises(ValueError):
        store.freeze_criteria("task-1", CriteriaRevision(1, (Criterion("c1", "different"),), "0" * 64))
    assert store.load_criteria("other-task", 1) is None
    assert store.load_criteria("task-1", 1) == criteria
    intent = AndroidUiActionIntent.create(task_id="task-1", criteria_revision=1, step_index=0, action=AndroidUiAction("input_text", {"text": "password: top-secret 123456"}))
    reservation = store.reserve_step(task_id="task-1", revision=1, before=_observation())
    from ai_game_console.android_ui_runtime.domain import RoleDecision
    reservation = store.record_decision(reservation, RoleDecision("action", intent.action))
    store.record_intent(reservation, intent)
    store.record_intent(reservation, intent)
    event = store.safe_events("task-1")[0]
    assert event["intent"]["text_length"] == len("password: top-secret 123456")
    assert "top-secret" not in json.dumps(event)


def test_new_criteria_rows_redact_descriptions_but_restart_keeps_frozen_identity(
    tmp_path: object,
) -> None:
    database = tmp_path / "criteria-redaction.sqlite"  # type: ignore[operator]
    secret = "Soul Alice private chat navigation"
    waypoint_values = (Criterion("w1c1", secret, (evidence_marker("container", "settings-home"),)),)
    waypoint = OrderedWaypoint(
        "waypoint-1", 1, opaque_digest("goal-clause", secret), waypoint_values,
        criteria_digest(waypoint_values),
    )
    terminal_values = (Criterion("c1", secret, (evidence_marker("page_title", "notification-page"),)),)
    criteria = CriteriaRevision(
        1, terminal_values,
        criteria_digest(terminal_values, waypoints=(waypoint,)),
        waypoints=(waypoint,),
    )
    first = SQLiteAndroidUiStepStore(database)
    first.freeze_criteria("task-1", criteria)

    loaded_here = first.load_criteria("task-1", 1)
    loaded_after_restart = SQLiteAndroidUiStepStore(database).load_criteria("task-1", 1)
    assert loaded_here == criteria == loaded_after_restart
    assert loaded_here is not None
    assert criteria_digest(
        loaded_here.criteria, waypoints=loaded_here.waypoints,
    ) == loaded_here.digest

    with sqlite3.connect(database) as connection:
        persisted = "\n".join(
            str(value)
            for row in connection.execute(
                "SELECT criteria_json, waypoints_json FROM android_ui_criteria"
            )
            for value in row
        )
    assert secret not in persisted
    assert persisted.count("[redacted]") == 2


def test_legacy_criteria_without_description_digest_or_waypoints_stays_readable(
    tmp_path: object,
) -> None:
    database = tmp_path / "legacy-criteria.sqlite"  # type: ignore[operator]
    legacy_payload = [{
        "criterion_id": "c1",
        "description": "legacy visible page",
        "required_evidence_markers": [],
    }]
    legacy_projection = [{
        "id": "c1",
        "description": "legacy visible page",
        "required_evidence_markers": (),
    }]
    legacy_digest = hashlib.sha256(json.dumps(
        legacy_projection, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE android_ui_criteria (
            task_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
            criteria_json TEXT NOT NULL, PRIMARY KEY(task_id, revision))"""
        )
        connection.execute(
            "INSERT INTO android_ui_criteria(task_id,revision,digest,criteria_json) VALUES(?,?,?,?)",
            ("task-1", 1, legacy_digest, json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True)),
        )

    store = SQLiteAndroidUiStepStore(database)
    loaded = store.load_criteria("task-1", 1)
    assert loaded is not None
    assert loaded.digest == legacy_digest
    assert loaded.criteria[0].description == "legacy visible page"
    assert loaded.criteria[0].description_digest is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT waypoints_json FROM android_ui_criteria").fetchone()[0] == "[]"


@pytest.mark.parametrize(
    ("payload_mutation", "digest_mutation"),
    (
        ("description", None),
        ("markers", None),
        (None, "0" * 64),
    ),
)
def test_legacy_criteria_tamper_or_unknown_digest_scheme_fails_closed(
    tmp_path: object, payload_mutation: str | None, digest_mutation: str | None,
) -> None:
    database = tmp_path / f"legacy-tamper-{payload_mutation or 'digest'}.sqlite"  # type: ignore[operator]
    payload = [{
        "criterion_id": "c1",
        "description": "legacy visible page",
        "required_evidence_markers": [],
    }]
    original = [{
        "id": "c1", "description": "legacy visible page",
        "required_evidence_markers": (),
    }]
    digest = hashlib.sha256(json.dumps(
        original, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    if payload_mutation == "description":
        payload[0]["description"] = "tampered visible page"
    elif payload_mutation == "markers":
        payload[0]["required_evidence_markers"] = [
            evidence_marker("page_title", "tampered-page"),
        ]
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE android_ui_criteria (
            task_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
            criteria_json TEXT NOT NULL, PRIMARY KEY(task_id, revision))"""
        )
        connection.execute(
            "INSERT INTO android_ui_criteria(task_id,revision,digest,criteria_json) VALUES(?,?,?,?)",
            ("task-1", 1, digest_mutation or digest, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
    store = SQLiteAndroidUiStepStore(database)
    with pytest.raises(ValueError, match="stored criteria"):
        store.load_criteria("task-1", 1)


def test_legacy_identity_cannot_be_written_as_a_new_revision(tmp_path: object) -> None:
    database = tmp_path / "legacy-boundary.sqlite"  # type: ignore[operator]
    payload = [{
        "criterion_id": "c1", "description": "legacy visible page",
        "required_evidence_markers": [],
    }]
    legacy_digest = hashlib.sha256(json.dumps([{
        "id": "c1", "description": "legacy visible page",
        "required_evidence_markers": (),
    }], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE android_ui_criteria (
            task_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
            criteria_json TEXT NOT NULL, PRIMARY KEY(task_id, revision))"""
        )
        connection.execute(
            "INSERT INTO android_ui_criteria(task_id,revision,digest,criteria_json) VALUES(?,?,?,?)",
            ("task-1", 1, legacy_digest, json.dumps(payload, sort_keys=True)),
        )
    store = SQLiteAndroidUiStepStore(database)
    legacy = store.load_criteria("task-1", 1)
    assert legacy is not None
    with pytest.raises(ValueError, match="legacy criteria identity"):
        store.freeze_criteria("task-1", replace(legacy, revision=2))

    current = CriteriaRevision(
        2, legacy.criteria, criteria_digest(legacy.criteria),
    )
    store.freeze_criteria("task-1", current)
    with sqlite3.connect(database) as connection:
        new_payload = connection.execute(
            "SELECT criteria_json FROM android_ui_criteria WHERE revision=2"
        ).fetchone()[0]
    assert "legacy visible page" not in new_payload
    assert "description_digest" in new_payload


@pytest.mark.parametrize(
    "identity_scheme",
    (
        CRITERIA_IDENTITY_CURRENT,
        CRITERIA_IDENTITY_LEGACY_HASHED,
        CRITERIA_IDENTITY_LEGACY_RAW,
    ),
)
def test_criteria_loader_rejects_unexpected_payload_field_for_every_scheme(
    tmp_path: object, identity_scheme: str,
) -> None:
    database = tmp_path / f"unexpected-{identity_scheme}.sqlite"  # type: ignore[operator]
    criterion = Criterion("c1", "legacy visible page")
    digest = criteria_digest((criterion,), identity_scheme=identity_scheme)
    payload: dict[str, object] = {
        "criterion_id": "c1",
        "description": "[redacted]" if identity_scheme == CRITERIA_IDENTITY_CURRENT else "legacy visible page",
        "required_evidence_markers": [],
        "unexpected_payload_field": "must-not-be-accepted",
    }
    if identity_scheme == CRITERIA_IDENTITY_CURRENT:
        payload["description_digest"] = opaque_digest(
            "criterion-description", "legacy visible page",
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE android_ui_criteria (
            task_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
            criteria_json TEXT NOT NULL, PRIMARY KEY(task_id, revision))"""
        )
        connection.execute(
            "INSERT INTO android_ui_criteria(task_id,revision,digest,criteria_json) VALUES(?,?,?,?)",
            ("task-1", 1, digest, json.dumps([payload], sort_keys=True)),
        )
    with pytest.raises(ValueError, match="stored criteria revision"):
        SQLiteAndroidUiStepStore(database).load_criteria("task-1", 1)


def test_criteria_loader_rejects_duplicate_keys_and_waypoint_unknown_fields(
    tmp_path: object,
) -> None:
    duplicate = tmp_path / "duplicate-criterion.sqlite"  # type: ignore[operator]
    criterion = Criterion("c1", "visible page")
    digest = criteria_digest((criterion,))
    duplicate_json = (
        '[{"criterion_id":"c1","description":"[redacted]",'
        '"description":"[redacted]","description_digest":"'
        + opaque_digest("criterion-description", "visible page")
        + '","required_evidence_markers":[]}]'
    )
    with sqlite3.connect(duplicate) as connection:
        connection.execute(
            """CREATE TABLE android_ui_criteria (
            task_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
            criteria_json TEXT NOT NULL, PRIMARY KEY(task_id, revision))"""
        )
        connection.execute(
            "INSERT INTO android_ui_criteria(task_id,revision,digest,criteria_json) VALUES(?,?,?,?)",
            ("task-1", 1, digest, duplicate_json),
        )
    with pytest.raises(ValueError, match="stored criteria revision"):
        SQLiteAndroidUiStepStore(duplicate).load_criteria("task-1", 1)

    waypoint_db = tmp_path / "unexpected-waypoint.sqlite"  # type: ignore[operator]
    waypoint_values = (Criterion("w1c1", "route page"),)
    waypoint = OrderedWaypoint(
        "waypoint-1", 1, opaque_digest("goal-clause", "route"), waypoint_values,
        criteria_digest(waypoint_values),
    )
    terminal_values = (Criterion("c1", "terminal page"),)
    frozen = CriteriaRevision(
        1, terminal_values, criteria_digest(terminal_values, waypoints=(waypoint,)),
        waypoints=(waypoint,),
    )
    store = SQLiteAndroidUiStepStore(waypoint_db)
    store.freeze_criteria("task-1", frozen)
    with sqlite3.connect(waypoint_db) as connection:
        value = json.loads(connection.execute(
            "SELECT waypoints_json FROM android_ui_criteria"
        ).fetchone()[0])
        value[0]["unexpected_payload_field"] = True
        connection.execute(
            "UPDATE android_ui_criteria SET waypoints_json=?",
            (json.dumps(value, sort_keys=True),),
        )
    with pytest.raises(ValueError, match="stored criteria revision"):
        SQLiteAndroidUiStepStore(waypoint_db).load_criteria("task-1", 1)


@pytest.mark.parametrize(
    ("selected_hint_id", "retrieval"),
    (
        ("a" * 64, ()),
        ("a" * 64, ({"candidate_id": "b" * 64, "provenance": "c" * 64, "selected": True},)),
        # The matching candidate was observed in an older retrieval but was
        # not selected in this durable step, so it is stale for replay.
        ("a" * 64, ({"candidate_id": "a" * 64, "provenance": "c" * 64, "selected": False},)),
    ),
)
def test_selected_hint_recovery_rejects_empty_wrong_or_stale_attribution(
    tmp_path: object, selected_hint_id: str, retrieval: tuple[dict[str, object], ...],
) -> None:
    store = SQLiteAndroidUiStepStore(
        tmp_path / f"retrieval-{selected_hint_id[:2]}-{len(retrieval)}-{str(retrieval[-1] if retrieval else 'empty')[-5:]}.sqlite"  # type: ignore[operator]
    )
    reservation = store.reserve_step(
        task_id="task-1", revision=1, before=_observation(), snapshot=_snapshot(),
    )
    reservation = store.record_decision(
        reservation,
        RoleDecision(
            "action", AndroidUiAction("back"), selected_hint_id=selected_hint_id,
        ),
        retrieval_attribution=retrieval,
    )
    with pytest.raises(ValueError, match="selected hint attribution"):
        store.selected_hint_attribution(reservation)


def test_private_artifact_and_owner_data_never_reach_safe_serialization(tmp_path: object) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "steps.sqlite")  # type: ignore[operator]
    before = _observation()
    criteria = CriteriaRevision(1, (Criterion("c1", "private-safe"),), criteria_digest((Criterion("c1", "private-safe"),)))
    store.freeze_criteria("task-1", criteria)
    reservation = store.reserve_step(task_id="task-1", revision=1, before=before, snapshot=_snapshot())
    store.record_decision(reservation, RoleDecision("terminal_candidate", reason="contact: Alice message: private"))
    record = GoalVerificationRecord("task-1", OwnerBinding("principal-private", "controller-private"), "android_ui_agent", 1, 1, criteria.digest, 0, before, before, "model-v", "prompt-v", (), "unknown", True)
    with pytest.raises(ValueError):
        store.record_verification(record)
    store.record_verification(_verified_already(criteria, before))
    wire = json.dumps({"events": store.safe_events("task-1"), "records": store.safe_goal_verifications("task-1")})
    for forbidden in ("artifact-private", "state-private", "principal-private", "controller-private", "Alice", "private"):
        assert forbidden not in wire


def test_safe_verification_projection_removes_anchors_and_coordinate_bounds(
    tmp_path: object,
) -> None:
    grounding = _TrustedGrounding()
    database = tmp_path / "safe-verification.sqlite"  # type: ignore[operator]
    store = SQLiteAndroidUiStepStore(database, grounding=grounding)
    observation, marker, node_id = _target_observation()
    criteria = _target_criteria(marker)
    _reserve_terminal(store, criteria, observation)
    store.record_verification(_trusted_success(
        grounding, criteria, observation, marker, node_id,
    ))
    safe = store.safe_goal_verifications("task-1")[0]
    assert safe["verdicts"] == [{"criterion_id": "c1", "state": "satisfied"}]
    assert "anchors" not in json.dumps(safe)
    with sqlite3.connect(database) as connection:
        raw = connection.execute(
            "SELECT record_json FROM android_ui_goal_verifications"
        ).fetchone()[0]
    assert "bounds" not in raw
    assert "K2_UI_RAW_SENTINEL" not in raw


def test_raw_sql_never_contains_paths_urls_secrets_chat_or_owner(tmp_path: object) -> None:
    database = tmp_path / "steps.sqlite"  # type: ignore[operator]
    store = SQLiteAndroidUiStepStore(database)
    before = _observation()
    criteria = compile_criteria("在 Soul 与账号 Alice 聊天，完成 C:\\K2_PATH_SENTINEL\\screen.png 并打开 https://K2_URL_SENTINEL.invalid", revision=1)
    store.freeze_criteria("task-1", criteria)
    reservation = store.reserve_step(task_id="task-1", revision=1, before=before, snapshot=_snapshot())
    store.record_decision(reservation, RoleDecision("terminal_candidate", reason="contact: Alice xpath=//K2_XPATH_SENTINEL password: secret", terminal_summary="see https://K2_URL_SENTINEL.invalid"), retrieval_attribution=({"candidate_id": "a" * 64, "provenance": "b" * 64},))
    store.record_verification(_verified_already(criteria, before))
    with sqlite3.connect(database) as connection:
        raw = "\n".join(str(cell) for row in connection.execute("SELECT criteria_json FROM android_ui_criteria UNION ALL SELECT before_json FROM android_ui_steps UNION ALL SELECT after_json FROM android_ui_steps UNION ALL SELECT binding_json FROM android_ui_steps UNION ALL SELECT primitive_json FROM android_ui_steps UNION ALL SELECT retrieval_json FROM android_ui_steps UNION ALL SELECT record_json FROM android_ui_goal_verifications") for cell in row)
    raw_database = database.read_bytes().decode("latin1", errors="ignore")
    for forbidden in ("artifact-private", "tree-private", "state-private", "profile-1", "boot-1", "device-1", "binding-1", "principal-private", "controller-private", "Alice", "password", "secret", "K2_PATH_SENTINEL", "K2_URL_SENTINEL", "K2_XPATH_SENTINEL", "K2_SELECTOR_SENTINEL", "http://", "C:\\\\"):
        assert forbidden not in raw
        assert forbidden not in raw_database


def test_verification_witness_has_no_importable_caller_issuer(tmp_path: object) -> None:
    assert not hasattr(android_ui_domain, "issue_verification_witness")
    assert not hasattr(EvidenceBoundSemanticVerifier, "_issue")
    assert not hasattr(semantic_module, "_MINT_VERIFICATION_RECORD")
    assert not hasattr(semantic_module, "_VERIFIER_SEAL")
    assert not hasattr(semantic_module, "_VerifierWitness")
    store = SQLiteAndroidUiStepStore(tmp_path / "closed.sqlite")  # type: ignore[operator]
    criteria = CriteriaRevision(1, (Criterion("c1", "bounded goal"),), criteria_digest((Criterion("c1", "bounded goal"),)))
    before = _observation()
    store.freeze_criteria("task-1", criteria)
    reservation = store.reserve_step(task_id="task-1", revision=1, before=before, snapshot=_snapshot())
    store.record_decision(reservation, RoleDecision("terminal_candidate"))
    forged = GoalVerificationRecord(
        "task-1", _snapshot().owner, "android_ui_agent", 1, 1,
        criteria.digest, 0, before, before, "model-v", "prompt-v",
        (CriterionVerdict("c1", "satisfied"),), "satisfied", True,
        runner_binding_id="binding-1", primitive_outcome="already_satisfied",
    )
    with pytest.raises(ValueError, match="verifier-issued"):
        store.record_verification(forged)


def test_exact_trusted_grounding_allows_success_and_terminal_replay(tmp_path: object) -> None:
    grounding = _TrustedGrounding()
    database = tmp_path / "grounded.sqlite"  # type: ignore[operator]
    store = SQLiteAndroidUiStepStore(database, grounding=grounding)
    observation, marker, node_id = _target_observation()
    criteria = _target_criteria(marker)
    _reserve_terminal(store, criteria, observation)
    record = _trusted_success(grounding, criteria, observation, marker, node_id)
    store.record_verification(record)
    terminal = store.trusted_terminal_for(
        _snapshot(), criteria, runner_kind="android_ui_agent", runner_version=1,
    )
    assert terminal is not None
    assert terminal.grounding_digest == record.grounding_digest
    raw = database.read_bytes()
    for forbidden in (b"K2_UI_RAW_SENTINEL", b"Soul", b"Alice", b"principal-private"):
        assert forbidden not in raw
    grounding.trust(
        observation, 0,
        nodes=(GroundedUiNode(
            node_id, "state", evidence_marker("state", "changed after record"),
            False, None,
        ),),
    )
    assert store.trusted_terminal_for(
        _snapshot(), criteria, runner_kind="android_ui_agent", runner_version=1,
    ) is None


def test_success_without_store_grounding_authority_cannot_be_recorded_or_replayed(
    tmp_path: object,
) -> None:
    verifier_grounding = _TrustedGrounding()
    store = SQLiteAndroidUiStepStore(tmp_path / "ungrounded.sqlite")  # type: ignore[operator]
    observation, marker, node_id = _target_observation()
    criteria = _target_criteria(marker)
    _reserve_terminal(store, criteria, observation)
    record = _trusted_success(
        verifier_grounding, criteria, observation, marker, node_id,
    )
    with pytest.raises(ValueError, match="trusted artifact grounding"):
        store.record_verification(record)
    assert store.trusted_terminal_for(
        _snapshot(), criteria, runner_kind="android_ui_agent", runner_version=1,
    ) is None


def test_grounding_node_or_container_mutation_after_verifier_fails_closed(
    tmp_path: object,
) -> None:
    source_nodes = [UiNode(
        opaque_node_id("chat-completed"), "chat complete", semantic_kind="state",
        semantic_marker=evidence_marker("state", "Soul Alice chat completed"),
    )]
    with pytest.raises(ValueError, match="immutable tuple"):
        _target_observation(nodes=source_nodes)
    observation, marker, node_id = _target_observation(nodes=tuple(source_nodes))
    assert isinstance(observation.ui_nodes, tuple)
    grounding = _TrustedGrounding()
    store = SQLiteAndroidUiStepStore(
        tmp_path / "mutation.sqlite", grounding=grounding,  # type: ignore[operator]
    )
    criteria = _target_criteria(marker)
    _reserve_terminal(store, criteria, observation)
    record = _trusted_success(grounding, criteria, observation, marker, node_id)

    source_nodes[:] = [UiNode(
        node_id, semantic_kind="state",
        semantic_marker=evidence_marker("state", "replacement state"),
    )]
    assert observation.ui_nodes[0].semantic_marker == marker
    changed_marker = evidence_marker("state", "different state")
    caller_mutated = replace(
        record,
        after=replace(
            observation,
            ui_nodes=(UiNode(
                node_id, semantic_kind="state", semantic_marker=changed_marker,
            ),),
        ),
    )
    with pytest.raises(ValueError, match="verifier-issued"):
        store.record_verification(caller_mutated)

    grounding.trust(
        observation, 0,
        nodes=(GroundedUiNode(
            node_id, "state", changed_marker, False, None,
        ),),
    )
    with pytest.raises(ValueError, match="trusted artifact grounding"):
        store.record_verification(record)
    assert store.trusted_terminal_for(
        _snapshot(), criteria, runner_kind="android_ui_agent", runner_version=1,
    ) is None


@pytest.mark.parametrize("drift", [
    "task", "owner", "runner", "runner_version", "binding", "revision", "step",
    "after_observation", "artifact_set", "ui_tree_artifact", "freshness",
])
def test_matching_node_digest_with_wrong_grounding_scope_cannot_succeed(
    tmp_path: object, drift: str,
) -> None:
    grounding = _TrustedGrounding()
    store = SQLiteAndroidUiStepStore(
        tmp_path / f"wrong-{drift}.sqlite", grounding=grounding,  # type: ignore[operator]
    )
    observation, marker, node_id = _target_observation()
    criteria = _target_criteria(marker)
    _reserve_terminal(store, criteria, observation)
    record = _trusted_success(grounding, criteria, observation, marker, node_id)
    assert grounding.manifest is not None
    query = grounding.manifest.query
    changes = {
        "task": {"task_id": "other-task"},
        "owner": {"owner_scope_digest": "0" * 64},
        "runner": {"runner_kind": "other_runner"},
        "runner_version": {"runner_version": 2},
        "binding": {"runner_binding_digest": "1" * 64},
        "revision": {"revision": 2},
        "step": {"step_index": 1},
        "after_observation": {"after_observation_digest": "2" * 64},
        "artifact_set": {"artifact_digest": "3" * 64},
        "ui_tree_artifact": {"ui_tree_artifact_digest": "4" * 64},
        "freshness": {"freshness_digest": "5" * 64},
    }[drift]
    wrong_query = replace(query, **changes)
    grounding.trust(observation, 0, query=wrong_query)
    with pytest.raises(ValueError, match="trusted artifact grounding"):
        store.record_verification(record)
    assert store.trusted_terminal_for(
        _snapshot(), criteria, runner_kind="android_ui_agent", runner_version=1,
    ) is None


def test_store_rejects_durable_criteria_tamper_even_for_verifier_record(tmp_path: object) -> None:
    database = tmp_path / "tamper.sqlite"  # type: ignore[operator]
    store = SQLiteAndroidUiStepStore(database)
    criteria = CriteriaRevision(1, (Criterion("c1", "bounded goal"),), criteria_digest((Criterion("c1", "bounded goal"),)))
    before = _observation()
    store.freeze_criteria("task-1", criteria)
    reservation = store.reserve_step(task_id="task-1", revision=1, before=before, snapshot=_snapshot())
    store.record_decision(reservation, RoleDecision("terminal_candidate"))
    record = _verified_already(criteria, before)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE android_ui_criteria SET criteria_json=? WHERE task_id=? AND revision=?",
            ('[{"criterion_id":"c1","description":"tampered","required_evidence_markers":[]}]', "task-1", 1),
        )
    with pytest.raises(ValueError, match="criteri"):
        store.record_verification(record)


def test_store_recomputes_primitive_outcome_and_causal_observation(tmp_path: object) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "primitive.sqlite")  # type: ignore[operator]
    criteria = CriteriaRevision(1, (Criterion("c1", "bounded goal"),), criteria_digest((Criterion("c1", "bounded goal"),)))
    before = _observation()
    after = replace(
        before,
        screenshot_ref="artifact-after", screenshot_digest="d" * 64,
        ui_tree_ref="tree-after", ui_tree_digest="e" * 64,
        device_state_ref="state-after", device_state_digest="f" * 64,
        freshness_token="fresh-2", causality_command_id="command-1",
    )
    store.freeze_criteria("task-1", criteria)
    reservation = store.reserve_step(task_id="task-1", revision=1, before=before, snapshot=_snapshot())
    action = AndroidUiAction("back")
    store.record_decision(reservation, RoleDecision("action", action))
    intent = AndroidUiActionIntent.create(
        task_id="task-1", criteria_revision=1, step_index=0, action=action,
    )
    store.record_intent(reservation, intent)
    reservation = store.record_claim(
        reservation, claim_id="command-1", action_intent_id=intent.action_intent_id,
    )
    store.record_after(reservation, after)
    with pytest.raises(ValueError, match="contradicts durable observations"):
        store.record_primitive_outcome(
            reservation, command_id="command-1", outcome="no_progress",
        )
    store.record_primitive_outcome(
        reservation, command_id="command-1", outcome="progress",
    )
    record = EvidenceBoundSemanticVerifier(_UnknownRole()).verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="bounded goal", criteria=criteria, before=before, after=after,
        latest_step_index=0, causal_command_id="command-1",
        primitive_outcome="progress",
    )
    store.record_verification(record)
    assert store.safe_events("task-1")[0]["primitive_outcome"] == "progress"


def test_checkpoint_attestation_derives_baseline_without_caller_integer(tmp_path: object) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "baseline.sqlite")  # type: ignore[operator]
    criteria = CriteriaRevision(1, (Criterion("c1", "bounded goal"),), criteria_digest((Criterion("c1", "bounded goal"),)))
    before = _observation()
    store.freeze_criteria("task-1", criteria)
    store.reserve_step(task_id="task-1", revision=1, before=before, snapshot=_snapshot())
    query = CheckpointBaselineQuery(
        snapshot=_snapshot(), criteria=criteria, observation=before,
        runner_kind="android_ui_agent", runner_version=1,
    )
    port = store.checkpoint_attestation_port()
    attestation = port.attest(query)
    assert attestation is not None and attestation.checkpoint_index == 1
    assert attestation.causal_command_id is None and port.validates(attestation)
    assert "checkpoint_index" not in CheckpointBaselineQuery.__dataclass_fields__
    drifted = replace(query, snapshot=replace(_snapshot(), runner_binding_id="other-binding"))
    assert port.attest(drifted) is None


def test_checkpoint_attestation_validation_is_exact_query_bound_and_read_only(
    tmp_path: object,
) -> None:
    database = tmp_path / "query-bound-baseline.sqlite"  # type: ignore[operator]
    store = SQLiteAndroidUiStepStore(database)
    criteria = CriteriaRevision(
        1,
        (Criterion("c1", "bounded goal"),),
        criteria_digest((Criterion("c1", "bounded goal"),)),
    )
    snapshot = _snapshot()
    initial = _observation()
    observation_a = replace(
        initial,
        screenshot_ref="artifact-a",
        screenshot_digest="d" * 64,
        ui_tree_ref="tree-a",
        ui_tree_digest="e" * 64,
        device_state_ref="state-a",
        device_state_digest="f" * 64,
        freshness_token="fresh-a",
        causality_command_id="shared-command",
    )
    observation_b = replace(
        observation_a,
        screenshot_ref="artifact-b",
        screenshot_digest="1" * 64,
        ui_tree_ref="tree-b",
        ui_tree_digest="2" * 64,
        device_state_ref="state-b",
        device_state_digest="3" * 64,
        freshness_token="fresh-b",
    )
    store.freeze_criteria(snapshot.task_id, criteria)

    def persist_after(before: ObservationEnvelope, after: ObservationEnvelope) -> None:
        reservation = store.reserve_step(
            task_id=snapshot.task_id,
            revision=snapshot.revision,
            before=before,
            snapshot=snapshot,
        )
        action = AndroidUiAction("back")
        reservation = store.record_decision(
            reservation,
            RoleDecision("action", action),
        )
        intent = AndroidUiActionIntent.create(
            task_id=snapshot.task_id,
            criteria_revision=snapshot.revision,
            step_index=reservation.step_index,
            action=action,
        )
        store.record_intent(reservation, intent)
        reservation = store.record_claim(
            reservation,
            claim_id="shared-command",
            action_intent_id=intent.action_intent_id,
        )
        store.record_after(reservation, after)
        store.record_primitive_outcome(
            reservation,
            command_id="shared-command",
            outcome="progress",
        )
        store.record_verification(EvidenceBoundSemanticVerifier(_UnknownRole()).verify(
            snapshot=snapshot,
            runner_kind="android_ui_agent",
            runner_version=1,
            goal="bounded goal",
            criteria=criteria,
            before=before,
            after=after,
            latest_step_index=reservation.step_index,
            causal_command_id="shared-command",
            primitive_outcome="progress",
        ))

    persist_after(initial, observation_a)
    persist_after(observation_a, observation_b)
    query_a = CheckpointBaselineQuery(
        snapshot, criteria, observation_a, "android_ui_agent", 1,
    )
    query_b = replace(query_a, observation=observation_b)
    port = store.checkpoint_attestation_port()
    attestation_a = port.attest(query_a)
    attestation_b = port.attest(query_b)
    assert attestation_a is not None and attestation_b is not None
    assert attestation_a.checkpoint_index == 1
    assert attestation_b.checkpoint_index == 2
    assert attestation_a.causal_command_id == attestation_b.causal_command_id
    assert attestation_a.observation_digest != attestation_b.observation_digest

    with sqlite3.connect(database) as connection:
        before_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "android_ui_criteria",
                "android_ui_steps",
                "android_ui_goal_verifications",
            )
        )
    for _ in range(3):
        assert port.validates_for(query_a, attestation_a)
        assert port.validates_for(query_b, attestation_b)
        assert not port.validates_for(query_a, attestation_b)
        assert not port.validates_for(query_b, attestation_a)
    assert port.validates(attestation_a) and port.validates(attestation_b)
    with sqlite3.connect(database) as connection:
        after_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "android_ui_criteria",
                "android_ui_steps",
                "android_ui_goal_verifications",
            )
        )
    assert before_counts == after_counts == (1, 2, 2)
    raw_database = database.read_bytes()
    for forbidden in (
        b"artifact-private",
        b"tree-private",
        b"state-private",
        b"artifact-a",
        b"artifact-b",
        b"tree-a",
        b"tree-b",
        b"state-a",
        b"state-b",
        b"message: hello private contact Alice",
        b"principal-private",
        b"controller-private",
        b"profile-1",
        b"boot-1",
        b"device-1",
    ):
        assert forbidden not in raw_database


def test_checkpoint_attestation_validation_fails_closed_for_every_binding(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "query-bound-fail-closed.sqlite"  # type: ignore[operator]
    store = SQLiteAndroidUiStepStore(database)
    criteria = CriteriaRevision(
        1,
        (Criterion("c1", "bounded goal"),),
        criteria_digest((Criterion("c1", "bounded goal"),)),
    )
    snapshot = _snapshot()
    observation = _observation()
    store.freeze_criteria(snapshot.task_id, criteria)
    store.reserve_step(
        task_id=snapshot.task_id,
        revision=snapshot.revision,
        before=observation,
        snapshot=snapshot,
    )
    query = CheckpointBaselineQuery(
        snapshot, criteria, observation, "android_ui_agent", 1,
    )
    port = store.checkpoint_attestation_port()
    attestation = port.attest(query)
    assert attestation is not None and port.validates_for(query, attestation)

    other_criterion = Criterion("c2", "different bounded goal")
    other_criteria = CriteriaRevision(
        1, (other_criterion,), criteria_digest((other_criterion,)),
    )
    mismatched_queries = (
        replace(
            query,
            snapshot=replace(snapshot, task_id="foreign-task"),
            observation=replace(observation, task_id="foreign-task"),
        ),
        replace(
            query,
            snapshot=replace(
                snapshot,
                owner=OwnerBinding("foreign-principal", "foreign-controller"),
            ),
        ),
        replace(query, snapshot=replace(snapshot, runner_binding_id="foreign-binding")),
        replace(
            query,
            snapshot=replace(snapshot, revision=2),
            criteria=replace(criteria, revision=2),
        ),
        replace(query, criteria=other_criteria),
        replace(
            query,
            snapshot=replace(snapshot, runner_kind="foreign-runner"),
            runner_kind="foreign-runner",
        ),
        replace(query, observation=replace(observation, screenshot_digest="9" * 64)),
        replace(query, observation=replace(observation, causality_command_id="foreign-command")),
    )
    assert all(not port.validates_for(item, attestation) for item in mismatched_queries)

    tampered_attestations = (
        replace(attestation, task_id="foreign-task"),
        replace(attestation, owner_scope_digest="0" * 64),
        replace(attestation, runner_kind="foreign-runner"),
        replace(attestation, runner_version=2),
        replace(attestation, runner_binding_digest="0" * 64),
        replace(attestation, revision=2),
        replace(attestation, criteria_digest="0" * 64),
        replace(attestation, checkpoint_index=2),
        replace(attestation, checkpoint_index=True),
        replace(attestation, observation_digest="0" * 64),
        replace(attestation, causal_command_id="foreign-command"),
        replace(attestation, _seal=object()),
    )
    assert all(not port.validates_for(query, item) for item in tampered_attestations)
    assert not port.validates_for(object(), attestation)  # type: ignore[arg-type]
    assert not port.validates_for(query, object())  # type: ignore[arg-type]
    assert not port.validates_for(
        replace(
            query,
            snapshot=replace(snapshot, runner_version=True),
            runner_version=True,
        ),
        attestation,
    )

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM android_ui_steps")
    assert not port.validates_for(query, attestation)

    def unavailable() -> sqlite3.Connection:
        raise sqlite3.OperationalError("store unavailable")

    monkeypatch.setattr(store, "_connect", unavailable)
    assert not port.validates_for(query, attestation)
