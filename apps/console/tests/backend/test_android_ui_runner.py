from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from ai_game_console.android_ui_runtime.domain import (
    Anchor, AndroidUiAction, CanonicalSnapshot, CriteriaRevision, Criterion,
    CriterionVerdict, GroundedUiNode, ObservationEnvelope, ObservationGroundingQuery,
    OrderedWaypoint, OwnerBinding, RoleDecision, TrustedObservationGroundingManifest, UiNode,
    criteria_digest, evidence_marker, grounding_manifest_digest, observation_grounding_query, opaque_digest, opaque_node_id,
    CRITERIA_IDENTITY_LEGACY_HASHED, CRITERIA_IDENTITY_LEGACY_RAW,
)
from ai_game_console.android_ui_runtime.role import PrimitiveVerification
from ai_game_console.android_ui_runtime.runner import AndroidUiAgentV1Handler, AndroidUiHandlerRegistry, DispatchReceipt, ExperienceHint
from ai_game_console.android_ui_runtime.semantic_verification import EvidenceBoundSemanticVerifier
from ai_game_console.android_ui_runtime.store import SQLiteAndroidUiStepStore


def _obs(token: str, *, target: bool = False, command_id: str | None = None) -> ObservationEnvelope:
    kind = "page_title" if target else "container"
    value = "notification-page" if target else "settings-home"
    nodes = (UiNode(opaque_node_id(value), "Notification settings" if target else "settings_homepage_container", semantic_kind=kind, semantic_marker=evidence_marker(kind, "enter notification settings" if target else value)),)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return ObservationEnvelope("task-1", "profile-1", 1, "boot-1", "device-1", "shot-" + token, digest, "tree-" + token, hashlib.sha256((token + "-tree").encode()).hexdigest(), "state-" + token, hashlib.sha256((token + "-state").encode()).hexdigest(), "now", token, "settings", nodes, command_id)


class _Canonical:
    def __init__(self, snapshot: CanonicalSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def inspect(self, task_id: str) -> CanonicalSnapshot:
        self.calls += 1
        return self.snapshot


class _Observations:
    def __init__(self, *items: ObservationEnvelope) -> None:
        self.items = list(items)

    def observe(self, snapshot: CanonicalSnapshot) -> ObservationEnvelope:
        return self.items.pop(0)


class _Planner:
    def plan(self, context: object) -> dict[str, str]: return {"plan": "one step"}


class _Actor:
    def __init__(self, decision: RoleDecision) -> None: self.decision = decision
    def decide(self, context: object, plan: object) -> RoleDecision: return self.decision


class _Primitive:
    def verify(self, context: object) -> PrimitiveVerification: return PrimitiveVerification(True)


class _SemanticRole:
    def __init__(self, state: str, token: str, node: str) -> None: self.state, self.token, self.node = state, token, node
    def verify_goal(self, context: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
        anchors = (Anchor("ui_node", self.token, node_id=opaque_node_id(self.node), semantic_marker=evidence_marker("page_title", "enter notification settings")),) if self.state == "satisfied" else ()
        return (CriterionVerdict("c1", self.state, anchors),)


class _Dispatch:
    def __init__(self) -> None: self.calls = 0; self.reconciles = 0
    def dispatch(self, snapshot: object, intent: object) -> DispatchReceipt:
        self.calls += 1
        return DispatchReceipt("command-1", intent.action_intent_id)  # type: ignore[union-attr]
    def reconcile(self, snapshot: object, intent: object) -> DispatchReceipt:
        self.reconciles += 1
        return DispatchReceipt("command-1", intent.action_intent_id)  # type: ignore[union-attr]


def _snapshot(status: str = "running", revision: int = 1) -> CanonicalSnapshot:
    return CanonicalSnapshot("task-1", OwnerBinding("p", "c"), revision, status, profile_id="profile-1", profile_generation=1, boot_id="boot-1", canonical_device_id="device-1", runner_kind="android_ui_agent", runner_version=1, runner_binding_id="binding-1")


def _criteria() -> CriteriaRevision:
    from ai_game_console.android_ui_runtime.domain import criteria_digest
    values = (Criterion("c1", "enter notification settings", (evidence_marker("page_title", "enter notification settings"),)),)
    return CriteriaRevision(1, values, criteria_digest(values))


class _TrustedGrounding:
    def __init__(self, observation: ObservationEnvelope, step_index: int = 0) -> None:
        self._seal = object()
        query = observation_grounding_query(
            snapshot=_snapshot(), observation=observation,
            runner_kind="android_ui_agent", runner_version=1, step_index=step_index,
        )
        nodes = tuple(GroundedUiNode(
            item.node_id, item.semantic_kind, item.semantic_marker,
            item.clickable, item.bounds,
        ) for item in observation.ui_nodes)
        self._manifest = TrustedObservationGroundingManifest(
            query, nodes, grounding_manifest_digest(query, nodes), self._seal,
        )

    def resolve(
        self, query: ObservationGroundingQuery,
    ) -> TrustedObservationGroundingManifest | None:
        return self._manifest if self._manifest.query == query else None

    def validates(self, manifest: TrustedObservationGroundingManifest) -> bool:
        return manifest._attestation is self._seal


def test_one_step_action_then_fresh_semantic_success(tmp_path: object) -> None:
    dispatch = _Dispatch()
    after = _obs("after", target=True, command_id="command-1")
    grounding = _TrustedGrounding(after)
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite", grounding=grounding)
    handler = AndroidUiAgentV1Handler(_Canonical(_snapshot()), _Observations(_obs("before"), after), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("tap", {"x": .5, "y": .5}))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("satisfied", "after", "notification-page"), grounding=grounding), store, dispatch)
    result = handler.one_step(task_id="task-1", goal="enter notification", criteria=_criteria())
    assert result.semantic_satisfied and dispatch.calls == 1
    trusted = store.trusted_terminal_for(
        _snapshot(), _criteria(), runner_kind="android_ui_agent", runner_version=1,
        model_version="unknown", prompt_version="android-ui-semantic-v1",
    )
    assert trusted is not None
    assert trusted.task_id == "task-1" and trusted.revision == 1 and trusted.step_index == 0
    assert trusted.owner_scope_digest and trusted.runner_binding_digest
    assert trusted.before_observation_digest and trusted.after_observation_digest
    assert trusted.before_observation_digest != trusted.after_observation_digest
    assert trusted.artifact_digest and trusted.causal_command_id == "command-1"
    assert trusted.primitive_outcome == "progress"
    assert trusted.model_version_digest and trusted.prompt_version_digest


def test_same_settings_home_cannot_become_success_and_only_one_action_is_dispatched(tmp_path: object) -> None:
    dispatch = _Dispatch()
    handler = AndroidUiAgentV1Handler(_Canonical(_snapshot()), _Observations(_obs("before"), _obs("after", command_id="command-1")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("open_app", {"package": "com.android.settings"}))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unsatisfied", "after", "home")), SQLiteAndroidUiStepStore(tmp_path / "step.sqlite"), dispatch)
    result = handler.one_step(task_id="task-1", goal="enter notification", criteria=_criteria())
    assert result.outcome == "continue" and not result.semantic_satisfied and dispatch.calls == 1


def test_pause_before_dispatch_invalidates_decision_with_zero_action(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    dispatch = _Dispatch()
    class _ChangingActor(_Actor):
        def decide(self, context: object, plan: object) -> RoleDecision:
            canonical.snapshot = _snapshot("paused")
            return self.decision
    handler = AndroidUiAgentV1Handler(canonical, _Observations(_obs("before")), _Planner(), _ChangingActor(RoleDecision("action", AndroidUiAction("back"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "before", "home")), SQLiteAndroidUiStepStore(tmp_path / "step.sqlite"), dispatch)
    result = handler.one_step(task_id="task-1", goal="x", criteria=_criteria())
    assert result.outcome == "decision_invalidated" and dispatch.calls == 0


def test_terminal_candidate_only_uses_fresh_initial_evidence(tmp_path: object) -> None:
    dispatch = _Dispatch()
    before = _obs("before", target=True)
    grounding = _TrustedGrounding(before)
    handler = AndroidUiAgentV1Handler(_Canonical(_snapshot()), _Observations(before), _Planner(), _Actor(RoleDecision("terminal_candidate", reason="done?")), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("satisfied", "before", "notification-page"), grounding=grounding), SQLiteAndroidUiStepStore(tmp_path / "step.sqlite", grounding=grounding), dispatch)
    result = handler.one_step(task_id="task-1", goal="enter notification", criteria=_criteria())
    assert result.semantic_satisfied and dispatch.calls == 0


@pytest.mark.parametrize(
    "identity_scheme",
    (CRITERIA_IDENTITY_LEGACY_HASHED, CRITERIA_IDENTITY_LEGACY_RAW),
)
def test_legacy_criteria_identity_survives_restart_freeze_verifier_and_handler(
    tmp_path: object, identity_scheme: str,
) -> None:
    """Old rows remain read-only, but retain their exact digest contract."""

    database = tmp_path / f"legacy-{identity_scheme}.sqlite"  # type: ignore[operator]
    values = _criteria().criteria
    digest = criteria_digest(values, identity_scheme=identity_scheme)
    payload = json.dumps([{
        "criterion_id": item.criterion_id,
        "description": item.description,
        "required_evidence_markers": item.required_evidence_markers,
    } for item in values], ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE android_ui_criteria (
            task_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
            criteria_json TEXT NOT NULL, PRIMARY KEY(task_id, revision))"""
        )
        connection.execute(
            "INSERT INTO android_ui_criteria(task_id,revision,digest,criteria_json) VALUES(?,?,?,?)",
            ("task-1", 1, digest, payload),
        )

    loaded = None
    for _ in range(3):
        store = SQLiteAndroidUiStepStore(database)
        loaded = store.load_criteria("task-1", 1)
        assert loaded is not None and loaded.identity_scheme == identity_scheme
        assert store.freeze_criteria("task-1", loaded) == loaded
    assert loaded is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT criteria_json FROM android_ui_criteria"
        ).fetchone()[0] == payload

    before = _obs("legacy-target", target=True)
    grounding = _TrustedGrounding(before)
    verifier = EvidenceBoundSemanticVerifier(
        _SemanticRole("satisfied", "legacy-target", "notification-page"),
        grounding=grounding,
    )
    record = verifier.verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="enter notification settings", criteria=loaded, before=before,
        after=before, latest_step_index=0, already_satisfied=True,
    )
    assert record.criteria_digest == digest
    handler = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(before), _Planner(),
        _Actor(RoleDecision("terminal_candidate")), _Primitive(), verifier,
        SQLiteAndroidUiStepStore(database, grounding=grounding), _Dispatch(),
    )
    result = handler.one_step(
        task_id="task-1", goal="enter notification settings", criteria=loaded,
    )
    assert result.outcome == "semantic_satisfied" and result.semantic_satisfied


def test_terminal_candidate_after_effect_requires_exact_causal_predecessor(
    tmp_path: object,
) -> None:
    dispatch = _Dispatch()
    stable = _obs("stable", target=True)
    grounding = _TrustedGrounding(stable, step_index=1)
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite", grounding=grounding)
    first = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _Observations(_obs("before"), _obs("after", command_id="command-1")),
        _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("back"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")),
        store,
        dispatch,
    )
    assert first.one_step(
        task_id="task-1", goal="enter notification", criteria=_criteria(),
    ).outcome == "continue"

    second = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(stable), _Planner(),
        _Actor(RoleDecision("terminal_candidate")), _Primitive(),
        EvidenceBoundSemanticVerifier(
            _SemanticRole("satisfied", "stable", "notification-page"),
            grounding=grounding,
        ),
        store,
        dispatch,
    )
    result = second.one_step(
        task_id="task-1", goal="enter notification", criteria=_criteria(),
    )

    assert result.outcome == "terminal_requires_after_evidence"
    assert dispatch.calls == 1


def test_causal_terminal_decision_is_rehydrated_without_replaying_action(
    tmp_path: object,
) -> None:
    dispatch = _Dispatch()
    stable = _obs("stable", target=True, command_id="command-1")
    grounding = _TrustedGrounding(stable, step_index=1)
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite", grounding=grounding)
    first = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _Observations(_obs("before"), _obs("after", command_id="command-1")),
        _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("back"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")),
        store,
        dispatch,
    )
    assert first.one_step(
        task_id="task-1", goal="enter notification", criteria=_criteria(),
    ).outcome == "continue"
    reservation = store.reserve_step(
        task_id="task-1", revision=1, before=stable, snapshot=_snapshot(),
    )
    store.record_decision(reservation, RoleDecision("terminal_candidate"))

    class _RecoveringTerminalObservations(_Observations):
        def rehydrate(
            self, snapshot: CanonicalSnapshot, durable: dict[str, object],
        ) -> ObservationEnvelope | None:
            del snapshot, durable
            return stable

    retry_dispatch = _Dispatch()
    retry = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _RecoveringTerminalObservations(_obs("fresh-recovery")),
        _Planner(), _Actor(RoleDecision("action", AndroidUiAction("home"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(
            _SemanticRole("satisfied", "stable", "notification-page"),
            grounding=grounding,
        ),
        store,
        retry_dispatch,
    )

    result = retry.one_step(
        task_id="task-1", goal="enter notification", criteria=_criteria(),
    )

    assert result.semantic_satisfied
    assert result.outcome == "semantic_satisfied"
    assert retry_dispatch.calls == retry_dispatch.reconciles == 0
    assert len(store.safe_goal_verifications("task-1")) == 2


def test_stale_terminal_is_superseded_only_by_newer_causal_observation(
    tmp_path: object,
) -> None:
    dispatch = _Dispatch()
    final_observation = _obs("final", target=True, command_id="command-1")
    grounding = _TrustedGrounding(final_observation, step_index=2)
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite", grounding=grounding)
    first = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _Observations(_obs("before"), _obs("after", command_id="command-1")),
        _Planner(), _Actor(RoleDecision("action", AndroidUiAction("back"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")),
        store, dispatch,
    )
    assert first.one_step(
        task_id="task-1", goal="enter notification", criteria=_criteria(),
    ).outcome == "continue"
    stale = _obs("stale", target=True)
    reservation = store.reserve_step(
        task_id="task-1", revision=1, before=stale, snapshot=_snapshot(),
    )
    store.record_decision(reservation, RoleDecision("terminal_candidate"))

    retry_dispatch = _Dispatch()
    retry = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _Observations(_obs("newer", target=True, command_id="command-1")),
        _Planner(), _Actor(RoleDecision("action", AndroidUiAction("home"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "newer", "home")),
        store, retry_dispatch,
    )
    assert retry.one_step(
        task_id="task-1", goal="enter notification", criteria=_criteria(),
    ).outcome == "replan"
    assert retry_dispatch.calls == retry_dispatch.reconciles == 0

    terminal = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(final_observation), _Planner(),
        _Actor(RoleDecision("terminal_candidate")), _Primitive(),
        EvidenceBoundSemanticVerifier(
            _SemanticRole("satisfied", "final", "notification-page"),
            grounding=grounding,
        ),
        store, retry_dispatch,
    )
    result = terminal.one_step(
        task_id="task-1", goal="enter notification", criteria=_criteria(),
    )

    assert result.semantic_satisfied
    assert retry_dispatch.calls == retry_dispatch.reconciles == 0


def test_handler_registry_is_exact_and_unknown_version_is_inert(tmp_path: object) -> None:
    handler = AndroidUiAgentV1Handler(_Canonical(_snapshot()), _Observations(_obs("before")), _Planner(), _Actor(RoleDecision("terminal_candidate")), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "before", "home")), SQLiteAndroidUiStepStore(tmp_path / "step.sqlite"), _Dispatch())
    registry = AndroidUiHandlerRegistry()
    registry.register(handler)
    assert registry.resolve("android_ui_agent", 1) is handler
    assert registry.resolve("android_ui_agent", 2) is None


class _RouteGrounding:
    def __init__(self, *indexed: tuple[int, ObservationEnvelope]) -> None:
        self._seal = object()
        self._values = {}
        for index, observation in indexed:
            query = observation_grounding_query(
                snapshot=_snapshot(), observation=observation,
                runner_kind="android_ui_agent", runner_version=1,
                step_index=index,
            )
            nodes = tuple(GroundedUiNode(
                item.node_id, item.semantic_kind, item.semantic_marker,
                item.clickable, item.bounds,
            ) for item in observation.ui_nodes)
            self._values[query] = TrustedObservationGroundingManifest(
                query, nodes, grounding_manifest_digest(query, nodes), self._seal,
            )

    def resolve(self, query: ObservationGroundingQuery) -> TrustedObservationGroundingManifest | None:
        return self._values.get(query)

    def validates(self, manifest: TrustedObservationGroundingManifest) -> bool:
        return manifest._attestation is self._seal


class _MarkerSemantic:
    def verify_goal(self, context: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
        criteria = context.criteria  # type: ignore[attr-defined]
        observation = kwargs["after"]
        values = []
        for criterion in criteria.criteria:
            node = next((item for item in observation.ui_nodes if item.semantic_marker in criterion.required_evidence_markers), None)
            values.append(CriterionVerdict(
                criterion.criterion_id,
                "satisfied" if node is not None else "unknown",
                (
                    Anchor("ui_node", observation.freshness_token, node_id=node.node_id, semantic_marker=node.semantic_marker),
                ) if node is not None else (),
            ))
        return tuple(values)


class _WaypointExperience:
    def __init__(self) -> None:
        self.hint = ExperienceHint(
            "hint-waypoint", "retrieval-waypoint", "progress", "back",
            "frozen container criterion", "scene:" + "a" * 32,
            0.8, 1, 0, 1,
        )
        self.retrievals = 0
        self.records = []

    def retrieve(self, **kwargs: object) -> tuple[ExperienceHint, ...]:
        self.retrievals += 1
        return (self.hint,)

    def record_step(self, **kwargs: object) -> None:
        self.records.append(kwargs)


def _ordered_route_criteria() -> CriteriaRevision:
    waypoint_values = (
        Criterion("w1c1", "return to settings home", (
            evidence_marker("container", "settings-home"),
        )),
    )
    waypoint = OrderedWaypoint(
        "waypoint-1", 1, opaque_digest("goal-clause", "return-home"),
        waypoint_values, criteria_digest(waypoint_values),
    )
    terminal_values = (
        Criterion("c1", "enter notification settings", (
            evidence_marker("page_title", "enter notification settings"),
        )),
    )
    return CriteriaRevision(
        1, terminal_values,
        criteria_digest(terminal_values, waypoints=(waypoint,)),
        waypoints=(waypoint,),
    )


def test_ordered_waypoint_requires_real_route_before_terminal_and_replays_proof(
    tmp_path: object,
) -> None:
    criteria = _ordered_route_criteria()
    initial_terminal = _obs("initial-terminal", target=True)
    home_after = _obs("home-after", command_id="command-1")
    terminal_after = _obs("terminal-after", target=True, command_id="command-1")
    grounding = _RouteGrounding((1, home_after), (2, terminal_after))
    store = SQLiteAndroidUiStepStore(tmp_path / "ordered-route.sqlite", grounding=grounding)
    dispatch = _Dispatch()
    experience = _WaypointExperience()

    blocked = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(initial_terminal), _Planner(),
        _Actor(RoleDecision("terminal_candidate", reason="already there")), _Primitive(),
        EvidenceBoundSemanticVerifier(_MarkerSemantic(), grounding=grounding),
        store, dispatch, experience=experience,
    ).one_step(task_id="task-1", goal="first home, then notifications", criteria=criteria)
    assert blocked.outcome == "replan" and dispatch.calls == 0
    assert store.trusted_terminal_for(
        _snapshot(), criteria, runner_kind="android_ui_agent", runner_version=1,
        model_version="unknown", prompt_version="android-ui-semantic-v1",
    ) is None

    first = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(_obs("route-before", target=True), home_after),
        _Planner(), _Actor(RoleDecision("action", AndroidUiAction("back"), selected_hint_id="hint-waypoint")),
        _Primitive(), EvidenceBoundSemanticVerifier(_MarkerSemantic(), grounding=grounding),
        store, dispatch, experience=experience,
    ).one_step(task_id="task-1", goal="first home, then notifications", criteria=criteria)
    assert first.outcome == "continue" and dispatch.calls == 1
    assert len(experience.records) == 1
    assert experience.records[0]["selected_hint"].candidate_id == "hint-waypoint"
    assert store.next_unmet_waypoint(_snapshot(), criteria) is None

    final = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(_obs("home-before"), terminal_after),
        _Planner(), _Actor(RoleDecision("action", AndroidUiAction("tap", {"x": .5, "y": .5}))),
        _Primitive(), EvidenceBoundSemanticVerifier(_MarkerSemantic(), grounding=grounding),
        store, dispatch, experience=experience,
    ).one_step(task_id="task-1", goal="first home, then notifications", criteria=criteria)
    assert final.semantic_satisfied and dispatch.calls == 2
    assert store.trusted_terminal_for(
        _snapshot(), criteria, runner_kind="android_ui_agent", runner_version=1,
        model_version="unknown", prompt_version="android-ui-semantic-v1",
    ) is not None

    # A fresh handler only reads the committed waypoint proof; it does not
    # reissue the already-settled physical back action after a restart.
    replay = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(_obs("unused")), _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(),
        EvidenceBoundSemanticVerifier(_MarkerSemantic(), grounding=grounding),
        store, dispatch,
    ).one_step(task_id="task-1", goal="first home, then notifications", criteria=criteria)
    assert replay.outcome == "terminal_replayed" and dispatch.calls == 2


def test_ordered_waypoint_rejects_wrong_order_wrong_hint_and_binding_drift(
    tmp_path: object,
) -> None:
    criteria = _ordered_route_criteria()
    # Reaching the eventual target first is only an unsatisfied attempt at A;
    # it cannot turn the final title into route history.
    target_after = _obs("wrong-order-after", target=True, command_id="command-1")
    wrong_order_store = SQLiteAndroidUiStepStore(tmp_path / "wrong-order.sqlite")
    wrong_order_dispatch = _Dispatch()
    wrong_order = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(_obs("wrong-order-before"), target_after),
        _Planner(), _Actor(RoleDecision("action", AndroidUiAction("back"))), _Primitive(),
        EvidenceBoundSemanticVerifier(_MarkerSemantic()), wrong_order_store,
        wrong_order_dispatch,
    ).one_step(task_id="task-1", goal="first home, then notifications", criteria=criteria)
    assert wrong_order.outcome == "continue" and wrong_order_dispatch.calls == 1
    assert wrong_order_store.next_unmet_waypoint(_snapshot(), criteria) == 1

    # A hallucinated hint never reaches K1 or K3 attribution.
    experience = _WaypointExperience()
    wrong_hint_dispatch = _Dispatch()
    wrong_hint = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(_obs("wrong-hint-before")), _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("back"), selected_hint_id="not-in-retrieval")),
        _Primitive(), EvidenceBoundSemanticVerifier(_MarkerSemantic()),
        SQLiteAndroidUiStepStore(tmp_path / "wrong-hint.sqlite"), wrong_hint_dispatch,
        experience=experience,
    ).one_step(task_id="task-1", goal="first home, then notifications", criteria=criteria)
    assert wrong_hint.outcome == "decision_invalidated"
    assert wrong_hint_dispatch.calls == 0 and experience.records == []

    # A committed A proof is tied to the exact Profile binding.  A different
    # profile cannot inherit it and plan B.
    home_after = _obs("drift-home-after", command_id="command-1")
    grounding = _RouteGrounding((0, home_after))
    proof_store = SQLiteAndroidUiStepStore(tmp_path / "binding-drift.sqlite", grounding=grounding)
    proof_dispatch = _Dispatch()
    proof = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(_obs("drift-before", target=True), home_after),
        _Planner(), _Actor(RoleDecision("action", AndroidUiAction("back"))), _Primitive(),
        EvidenceBoundSemanticVerifier(_MarkerSemantic(), grounding=grounding), proof_store,
        proof_dispatch,
    ).one_step(task_id="task-1", goal="first home, then notifications", criteria=criteria)
    assert proof.outcome == "continue" and proof_store.next_unmet_waypoint(_snapshot(), criteria) is None
    with pytest.raises(ValueError, match="ordered waypoint proof is invalid"):
        proof_store.next_unmet_waypoint(
            replace(_snapshot(), profile_id="profile-drift"), criteria,
        )


def test_waypoint_recovery_replays_durable_selected_hint_before_committing_proof(
    tmp_path: object,
) -> None:
    """A process loss after ``after`` cannot erase one selected K3 outcome."""

    criteria = _ordered_route_criteria()
    before = _obs("crash-before", target=True)
    after = _obs("crash-after", command_id="command-1")
    grounding = _RouteGrounding((0, after))
    store = SQLiteAndroidUiStepStore(tmp_path / "waypoint-crash.sqlite", grounding=grounding)
    dispatch = _Dispatch()
    experience = _WaypointExperience()

    class _CrashAfterDurableAfter(EvidenceBoundSemanticVerifier):
        def verify(self, **kwargs: object):  # type: ignore[override]
            raise RuntimeError("simulated process loss after durable after")

    first = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(before, after), _Planner(),
        _Actor(RoleDecision(
            "action", AndroidUiAction("back"), selected_hint_id="hint-waypoint",
        )),
        _Primitive(), _CrashAfterDurableAfter(_MarkerSemantic(), grounding=grounding),
        store, dispatch, experience=experience,
    )
    with pytest.raises(RuntimeError, match="process loss"):
        first.one_step(
            task_id="task-1", goal="first home, then notifications", criteria=criteria,
        )
    assert dispatch.calls == 1
    assert store.safe_events("task-1")[0]["retrieval"] == [{
        "candidate_id": "hint-waypoint", "provenance": "retrieval-waypoint",
        "selected": True,
    }]
    assert experience.records == []

    class _RecoveringObservations(_Observations):
        def rehydrate(
            self, snapshot: CanonicalSnapshot, durable: dict[str, object],
        ) -> ObservationEnvelope | None:
            del snapshot
            return after if durable.get("causality_command_id") else before

    retry_dispatch = _Dispatch()
    recovered = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _RecoveringObservations(_obs("fresh-retry")),
        _Planner(), _Actor(RoleDecision("action", AndroidUiAction("home"))),
        _Primitive(), EvidenceBoundSemanticVerifier(_MarkerSemantic(), grounding=grounding),
        store, retry_dispatch, experience=experience,
    ).one_step(
        task_id="task-1", goal="first home, then notifications", criteria=criteria,
    )

    assert recovered.outcome == "continue"
    assert retry_dispatch.calls == retry_dispatch.reconciles == 0
    assert dispatch.calls == 1
    assert store.next_unmet_waypoint(_snapshot(), criteria) is None
    assert len(experience.records) == 1
    assert experience.records[0]["selected_hint"] is None
    assert experience.records[0]["selected_candidate_id"] == "hint-waypoint"
    assert experience.records[0]["selected_retrieval_id"] == "retrieval-waypoint"
    with sqlite3.connect(tmp_path / "waypoint-crash.sqlite") as connection:  # type: ignore[operator]
        proof_json = connection.execute(
            "SELECT record_json FROM android_ui_waypoint_proofs"
        ).fetchone()[0]
    assert "bounds" not in proof_json
    assert "return to settings home" not in proof_json


def test_incomplete_intent_restarts_same_step_and_action_identity(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite")
    first_dispatch = _Dispatch()
    class _Drop(_Dispatch):
        def dispatch(self, snapshot: object, intent: object) -> DispatchReceipt:
            self.calls += 1
            self.intent = intent
            raise RuntimeError("response dropped after durable intent")
    dropped = _Drop()
    first = AndroidUiAgentV1Handler(canonical, _Observations(_obs("before")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("back"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, dropped)
    try:
        first.one_step(task_id="task-1", goal="x", criteria=_criteria())
    except RuntimeError:
        pass
    retry = _Dispatch()
    second = AndroidUiAgentV1Handler(canonical, _Observations(_obs("restart-before"), _obs("after", command_id="command-1")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, retry)
    result = second.one_step(task_id="task-1", goal="x", criteria=_criteria())
    assert result.outcome == "reconciled_effect_pending_evidence"
    assert result.step_index == 0 and retry.calls == 0 and retry.reconciles == 1
    assert dropped.intent.action_intent_id == store.safe_events("task-1")[0]["action_intent_id"]


def test_pause_during_semantic_call_cannot_report_success(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    class _PausingSemantic(_SemanticRole):
        def verify_goal(self, context: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
            canonical.snapshot = _snapshot("paused")
            return super().verify_goal(context, **kwargs)
    dispatch = _Dispatch()
    handler = AndroidUiAgentV1Handler(canonical, _Observations(_obs("before"), _obs("after", target=True, command_id="command-1")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("tap", {"x": .5, "y": .5}))), _Primitive(), EvidenceBoundSemanticVerifier(_PausingSemantic("satisfied", "after", "notification-page")), SQLiteAndroidUiStepStore(tmp_path / "step.sqlite"), dispatch)
    result = handler.one_step(task_id="task-1", goal="x", criteria=_criteria())
    assert result.outcome == "semantic_invalidated" and not result.semantic_satisfied and dispatch.calls == 1


def test_handler_restart_after_intent_commit_response_drop_reuses_null_guarded_step(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    class _DropAfterIntent(SQLiteAndroidUiStepStore):
        dropped = False
        def record_intent(self, reservation: object, intent: object) -> object:
            result = super().record_intent(reservation, intent)  # type: ignore[arg-type]
            if not self.dropped:
                self.dropped = True
                raise RuntimeError("response drop after intent write")
            return result
    store = _DropAfterIntent(tmp_path / "step.sqlite")
    first = AndroidUiAgentV1Handler(canonical, _Observations(_obs("before")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("back"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, _Dispatch())
    try:
        first.one_step(task_id="task-1", goal="x", criteria=_criteria())
    except RuntimeError:
        pass
    dispatch = _Dispatch()
    second = AndroidUiAgentV1Handler(canonical, _Observations(_obs("retry-before"), _obs("after", command_id="command-1")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, dispatch)
    result = second.one_step(task_id="task-1", goal="x", criteria=_criteria())
    assert result.outcome == "reconciled_effect_pending_evidence" and result.step_index == 0
    assert dispatch.calls == 0 and dispatch.reconciles == 1 and len(store.safe_events("task-1")) == 1


def test_replan_never_calls_semantic_verifier(tmp_path: object) -> None:
    class _NoSemantic:
        def verify_goal(self, *args: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
            raise AssertionError("replan must not use final semantic verification")
    handler = AndroidUiAgentV1Handler(_Canonical(_snapshot()), _Observations(_obs("before")), _Planner(), _Actor(RoleDecision("replan", reason="observe again")), _Primitive(), EvidenceBoundSemanticVerifier(_NoSemantic()), SQLiteAndroidUiStepStore(tmp_path / "step.sqlite"), _Dispatch())
    assert handler.one_step(task_id="task-1", goal="x", criteria=_criteria()).outcome == "replan"


def test_short_wait_is_a_completed_zero_effect_step_without_dispatch_or_after_observation(tmp_path: object) -> None:
    dispatch = _Dispatch()
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite")
    observations = _Observations(_obs("before"))
    handler = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), observations, _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("wait", {"seconds": 1.5}))),
        _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "before", "home")),
        store, dispatch,
    )

    result = handler.one_step(task_id="task-1", goal="x", criteria=_criteria())

    assert result.outcome == "wait" and result.wait_seconds == 1.5
    assert dispatch.calls == dispatch.reconciles == 0
    assert observations.items == []
    event = store.safe_events("task-1")[0]
    assert event["action_intent_id"] is None
    assert store.has_effect_bearing_step("task-1") is False


def test_effect_requires_distinct_after_and_k1_command_causality(tmp_path: object) -> None:
    dispatch = _Dispatch()
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite")
    handler = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()), _Observations(_obs("before"), _obs("before", target=True)), _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("tap", {"x": .5, "y": .5}))), _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("satisfied", "before", "notification-page")), store, dispatch,
    )
    result = handler.one_step(task_id="task-1", goal="open notification", criteria=_criteria())
    assert result.outcome == "after_evidence_unavailable" and dispatch.calls == 1
    assert store.safe_goal_verifications("task-1") == ()


def test_terminal_response_drop_replays_durable_verification_without_planner_or_dispatch(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    before = _obs("before", target=True)
    grounding = _TrustedGrounding(before)
    class _DropAfterTerminal(SQLiteAndroidUiStepStore):
        dropped = False
        def record_verification(self, record: object) -> None:
            super().record_verification(record)  # type: ignore[arg-type]
            if not self.dropped:
                self.dropped = True
                raise RuntimeError("response drop after durable terminal")
    store = _DropAfterTerminal(tmp_path / "step.sqlite", grounding=grounding)
    first = AndroidUiAgentV1Handler(
        canonical, _Observations(before), _Planner(), _Actor(RoleDecision("terminal_candidate")),
        _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("satisfied", "before", "notification-page"), grounding=grounding), store, _Dispatch(),
    )
    with pytest.raises(RuntimeError):
        first.one_step(task_id="task-1", goal="enter notification", criteria=_criteria())
    assert store.trusted_terminal_for(
        _snapshot(), _criteria(), runner_kind="android_ui_agent", runner_version=1,
        model_version="different-model", prompt_version="android-ui-semantic-v1",
    ) is None
    assert store.trusted_terminal_for(
        CanonicalSnapshot(
            "task-1", OwnerBinding("p", "c"), 1, "running",
            profile_id="profile-1", profile_generation=1, boot_id="boot-1",
            canonical_device_id="device-1", runner_kind="android_ui_agent",
            runner_version=1, runner_binding_id="different-binding",
        ),
        _criteria(), runner_kind="android_ui_agent", runner_version=1,
        model_version="unknown", prompt_version="android-ui-semantic-v1",
    ) is None
    class _NoPlanner:
        def plan(self, context: object) -> object: raise AssertionError("terminal replay must precede planning")
    replay_dispatch = _Dispatch()
    second = AndroidUiAgentV1Handler(
        canonical, _Observations(), _NoPlanner(), _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "before", "home")), store, replay_dispatch,
    )
    result = second.one_step(task_id="task-1", goal="enter notification", criteria=_criteria())
    assert result.outcome == "terminal_replayed" and result.semantic_satisfied
    assert result.verification_ref and replay_dispatch.calls == replay_dispatch.reconciles == 0


def test_unknown_step_record_is_a_checkpoint_not_a_terminal_replay(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite")
    first = AndroidUiAgentV1Handler(
        canonical, _Observations(_obs("before-0"), _obs("after-0", command_id="command-1")), _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("back"))), _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after-0", "home")), store, _Dispatch(),
    )
    first_result = first.one_step(task_id="task-1", goal="enter notification", criteria=_criteria())
    assert first_result.outcome == "continue" and first_result.verification_ref
    second = AndroidUiAgentV1Handler(
        canonical, _Observations(_obs("before-1"), _obs("after-1", command_id="command-1")), _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after-1", "home")), store, _Dispatch(),
    )
    assert second.one_step(task_id="task-1", goal="enter notification", criteria=_criteria()).step_index == 1


def test_changed_profile_binding_during_semantic_verification_never_persists_terminal(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    class _CrosswiredSemantic(_SemanticRole):
        def verify_goal(self, context: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
            canonical.snapshot = CanonicalSnapshot("task-1", OwnerBinding("p", "c"), 1, "running", profile_id="other-profile", profile_generation=2, boot_id="other-boot", canonical_device_id="other-device", runner_kind="android_ui_agent", runner_version=1, runner_binding_id="other-binding")
            return super().verify_goal(context, **kwargs)
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite")
    result = AndroidUiAgentV1Handler(
        canonical, _Observations(_obs("before"), _obs("after", target=True, command_id="command-1")), _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("tap", {"x": .5, "y": .5}))), _Primitive(),
        EvidenceBoundSemanticVerifier(_CrosswiredSemantic("satisfied", "after", "notification-page")), store, _Dispatch(),
    ).one_step(task_id="task-1", goal="enter notification", criteria=_criteria())
    assert result.outcome == "semantic_invalidated" and store.safe_goal_verifications("task-1") == ()


def test_input_text_recovery_never_reconstructs_or_changes_typed_payload(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite")
    class _Drop(_Dispatch):
        def dispatch(self, snapshot: object, intent: object) -> DispatchReceipt:
            self.calls += 1
            raise RuntimeError("drop")
    first = AndroidUiAgentV1Handler(canonical, _Observations(_obs("before")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("input_text", {"text": "password: K2_INPUT_SECRET"}))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, _Drop())
    with pytest.raises(RuntimeError):
        first.one_step(task_id="task-1", goal="x", criteria=_criteria())
    retry = _Dispatch()
    result = AndroidUiAgentV1Handler(canonical, _Observations(_obs("retry")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, retry).one_step(task_id="task-1", goal="x", criteria=_criteria())
    assert result.outcome == "unresolved_effect" and retry.calls == retry.reconciles == 0


def test_decision_write_response_drop_reuses_exact_decision_without_replanning(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    class _DropAfterDecision(SQLiteAndroidUiStepStore):
        dropped = False
        def record_decision(self, reservation: object, decision: object, **kwargs: object) -> object:
            result = super().record_decision(reservation, decision, **kwargs)  # type: ignore[arg-type]
            if not self.dropped:
                self.dropped = True
                raise RuntimeError("decision response dropped")
            return result
    store = _DropAfterDecision(tmp_path / "step.sqlite")
    first = AndroidUiAgentV1Handler(canonical, _Observations(_obs("before")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("back"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, _Dispatch())
    with pytest.raises(RuntimeError):
        first.one_step(task_id="task-1", goal="x", criteria=_criteria())
    class _NoPlanner:
        def plan(self, context: object) -> object: raise AssertionError("durable decision must replay")
    dispatch = _Dispatch()
    result = AndroidUiAgentV1Handler(canonical, _Observations(_obs("before"), _obs("recovery-after", command_id="command-1")), _NoPlanner(), _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "recovery-after", "home")), store, dispatch).one_step(task_id="task-1", goal="x", criteria=_criteria())
    assert result.step_index == 0 and dispatch.calls == 1


def test_recovered_decision_with_replacement_before_observation_is_inert(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    store = SQLiteAndroidUiStepStore(tmp_path / "step.sqlite")
    reservation = store.reserve_step(task_id="task-1", revision=1, before=_obs("old-before"), snapshot=_snapshot())
    store.record_decision(reservation, RoleDecision("action", AndroidUiAction("back")))
    dispatch = _Dispatch()
    class _NoPlanner:
        def plan(self, context: object) -> object: raise AssertionError("stale decision must not replan or dispatch")
    result = AndroidUiAgentV1Handler(canonical, _Observations(_obs("new-before")), _NoPlanner(), _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "new-before", "home")), store, dispatch).one_step(task_id="task-1", goal="x", criteria=_criteria())
    assert result.outcome == "decision_pending_recovery" and dispatch.calls == dispatch.reconciles == 0


def test_after_write_without_verification_never_dispatches_again(tmp_path: object) -> None:
    canonical = _Canonical(_snapshot())
    class _DropBeforeVerification(SQLiteAndroidUiStepStore):
        dropped = False
        def record_verification(self, record: object) -> None:
            if not self.dropped:
                self.dropped = True
                raise RuntimeError("verification unavailable after durable after")
            super().record_verification(record)  # type: ignore[arg-type]
    store = _DropBeforeVerification(tmp_path / "step.sqlite")
    first_dispatch = _Dispatch()
    first = AndroidUiAgentV1Handler(canonical, _Observations(_obs("before"), _obs("after", command_id="command-1")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("back"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, first_dispatch)
    with pytest.raises(RuntimeError):
        first.one_step(task_id="task-1", goal="x", criteria=_criteria())
    retry_dispatch = _Dispatch()
    result = AndroidUiAgentV1Handler(canonical, _Observations(_obs("fresh-recovery")), _Planner(), _Actor(RoleDecision("action", AndroidUiAction("home"))), _Primitive(), EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")), store, retry_dispatch).one_step(task_id="task-1", goal="x", criteria=_criteria())
    assert result.outcome == "after_pending_recovery" and retry_dispatch.calls == retry_dispatch.reconciles == 0


def test_durable_after_is_rehydrated_and_settled_without_replaying_action(
    tmp_path: object,
) -> None:
    canonical = _Canonical(_snapshot())

    class _DropBeforeVerification(SQLiteAndroidUiStepStore):
        dropped = False

        def record_verification(self, record: object) -> None:
            if not self.dropped:
                self.dropped = True
                raise RuntimeError("verification unavailable after durable after")
            super().record_verification(record)  # type: ignore[arg-type]

    before = _obs("before")
    after = _obs("after", command_id="command-1")
    store = _DropBeforeVerification(tmp_path / "step.sqlite")
    first_dispatch = _Dispatch()
    first = AndroidUiAgentV1Handler(
        canonical,
        _Observations(before, after),
        _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("back"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")),
        store,
        first_dispatch,
    )
    with pytest.raises(RuntimeError):
        first.one_step(task_id="task-1", goal="x", criteria=_criteria())

    class _RecoveringObservations(_Observations):
        def rehydrate(
            self, snapshot: CanonicalSnapshot, durable: dict[str, object],
        ) -> ObservationEnvelope | None:
            del snapshot
            return after if durable.get("causality_command_id") else before

    retry_dispatch = _Dispatch()
    retry = AndroidUiAgentV1Handler(
        canonical,
        _RecoveringObservations(_obs("fresh-recovery")),
        _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("home"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")),
        store,
        retry_dispatch,
    )

    result = retry.one_step(task_id="task-1", goal="x", criteria=_criteria())

    assert result.outcome == "continue"
    assert result.dispatch_command_id == "command-1"
    assert retry_dispatch.calls == retry_dispatch.reconciles == 0
    assert len(store.safe_goal_verifications("task-1")) == 1


def test_recoverable_unknown_keyword_result_never_places_text_in_semantic_flag(tmp_path: object) -> None:
    class _SettleFailure(_Dispatch):
        def settle_after(self, **kwargs: object) -> None:
            raise RuntimeError("fake settlement unavailable")

    dispatch = _SettleFailure()
    result = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _Observations(_obs("before"), _obs("after", command_id="command-1")),
        _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("back"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")),
        SQLiteAndroidUiStepStore(tmp_path / "settle.sqlite"),
        dispatch,
    ).one_step(task_id="task-1", goal="x", criteria=_criteria())

    assert result.outcome == "recoverable_unknown"
    assert result.semantic_satisfied is False
    assert isinstance(result.semantic_satisfied, bool)
    assert result.dispatch_command_id == "command-1"
    assert result.primitive_outcome == "uncertain"


def test_uncertain_primitive_recoverable_result_keeps_semantic_flag_boolean(tmp_path: object) -> None:
    class _UncertainPrimitive:
        def verify(self, context: object) -> PrimitiveVerification:
            return PrimitiveVerification(progress=False, uncertain=True)

    result = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _Observations(_obs("before"), _obs("after", command_id="command-1")),
        _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("back"))),
        _UncertainPrimitive(),
        EvidenceBoundSemanticVerifier(_SemanticRole("unknown", "after", "home")),
        SQLiteAndroidUiStepStore(tmp_path / "uncertain.sqlite"),
        _Dispatch(),
    ).one_step(task_id="task-1", goal="x", criteria=_criteria())

    assert result.outcome == "recoverable_unknown"
    assert result.semantic_satisfied is False
    assert isinstance(result.semantic_satisfied, bool)
