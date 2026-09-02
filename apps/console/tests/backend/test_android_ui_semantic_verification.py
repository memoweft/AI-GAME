from __future__ import annotations

import pytest

from ai_game_console.android_ui_runtime.domain import (
    Anchor, CanonicalSnapshot, CriteriaRevision, Criterion, CriterionVerdict,
    GroundedUiNode, ObservationEnvelope, ObservationGroundingQuery, OwnerBinding,
    TrustedObservationGroundingManifest, UiNode, criteria_digest, evidence_marker,
    grounding_manifest_digest, observation_grounding_query, opaque_node_id,
)
from ai_game_console.android_ui_runtime.semantic_verification import EvidenceBoundSemanticVerifier, SemanticVerificationError


def _snapshot() -> CanonicalSnapshot:
    return CanonicalSnapshot("task-1", OwnerBinding("principal-private", "controller-private"), 1, "running", profile_id="profile-1", profile_generation=1, boot_id="boot-1", canonical_device_id="device-1", runner_kind="android_ui_agent", runner_version=1, runner_binding_id="binding-1")


def _settings_home(token: str = "fresh-1") -> ObservationEnvelope:
    return ObservationEnvelope("task-1", "profile-1", 1, "boot-1", "device-1", "screenshot-ref", "a" * 64, "tree-ref", "b" * 64, "state-ref", "c" * 64, "now", token, "Settings homepage", (UiNode(opaque_node_id("home"), "settings_homepage_container", semantic_kind="container", semantic_marker=evidence_marker("container", "settings home")), UiNode(opaque_node_id("notification"), "通知", (0.1, 0.3, 0.9, 0.4), True, "navigation")), "command-1")


class _Role:
    def __init__(self, verdicts: tuple[CriterionVerdict, ...]) -> None:
        self.verdicts = verdicts

    def verify_goal(self, *args: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
        return self.verdicts


class _TrustedGrounding:
    def __init__(self) -> None:
        self._seal = object()
        self._manifests: dict[ObservationGroundingQuery, TrustedObservationGroundingManifest] = {}

    def trust(
        self, observation: ObservationEnvelope, step_index: int, *,
        snapshot: CanonicalSnapshot | None = None,
    ) -> TrustedObservationGroundingManifest:
        query = observation_grounding_query(
            snapshot=snapshot or _snapshot(), observation=observation,
            runner_kind="android_ui_agent", runner_version=1, step_index=step_index,
        )
        nodes = tuple(GroundedUiNode(
            item.node_id, item.semantic_kind, item.semantic_marker,
            item.clickable, item.bounds,
        ) for item in observation.ui_nodes)
        manifest = TrustedObservationGroundingManifest(
            query, nodes, grounding_manifest_digest(query, nodes), self._seal,
        )
        self._manifests[query] = manifest
        return manifest

    def resolve(
        self, query: ObservationGroundingQuery,
    ) -> TrustedObservationGroundingManifest | None:
        return self._manifests.get(query)

    def validates(self, manifest: TrustedObservationGroundingManifest) -> bool:
        return manifest._attestation is self._seal


def _criteria(text: str) -> CriteriaRevision:
    from ai_game_console.android_ui_runtime.domain import criteria_digest
    criteria = (Criterion("c1", text, (evidence_marker("page_title", text),)),)
    return CriteriaRevision(1, criteria, criteria_digest(criteria))


@pytest.mark.parametrize("goal", ["enter notification settings", "find battery"])
def test_settings_home_and_open_app_cannot_pass_page_goal(goal: str) -> None:
    home = _settings_home()
    verifier = EvidenceBoundSemanticVerifier(_Role((CriterionVerdict("c1", "unsatisfied"),)))
    record = verifier.verify(snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1, goal=goal, criteria=_criteria(goal), before=home, after=home, latest_step_index=1, causal_command_id="command-1", primitive_outcome="no_progress")
    assert record.overall == "unsatisfied"


def test_target_page_needs_matching_ui_anchor_to_succeed() -> None:
    marker = evidence_marker("page_title", "enter notification")
    page = ObservationEnvelope("task-1", "profile-1", 1, "boot-1", "device-1", "shot", "a" * 64, "tree", "b" * 64, "state", "c" * 64, "now", "fresh-target", "Notification settings", (UiNode(opaque_node_id("page"), "Notification settings", semantic_kind="page_title", semantic_marker=marker),), "command-1")
    verdict = CriterionVerdict("c1", "satisfied", (Anchor("ui_node", "fresh-target", node_id=opaque_node_id("page"), text="Notification settings", semantic_marker=marker),))
    grounding = _TrustedGrounding()
    grounding.trust(page, 2)
    record = EvidenceBoundSemanticVerifier(_Role((verdict,)), grounding=grounding).verify(snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1, goal="enter notification", criteria=_criteria("enter notification"), before=_settings_home(), after=page, latest_step_index=2, causal_command_id="command-1", primitive_outcome="progress")
    assert record.overall == "satisfied"
    assert record.grounding_digest is not None


def test_memory_only_nodes_without_trusted_grounding_fail_closed() -> None:
    marker = evidence_marker("page_title", "enter notification")
    page = ObservationEnvelope(
        "task-1", "profile-1", 1, "boot-1", "device-1", "shot", "a" * 64,
        "tree", "b" * 64, "state", "c" * 64, "now", "fresh-target", "target",
        (UiNode(opaque_node_id("page"), semantic_kind="page_title", semantic_marker=marker),),
        "command-1",
    )
    verdict = CriterionVerdict(
        "c1", "satisfied", (Anchor(
            "ui_node", "fresh-target", node_id=opaque_node_id("page"),
            semantic_marker=marker,
        ),),
    )
    record = EvidenceBoundSemanticVerifier(_Role((verdict,))).verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="enter notification", criteria=_criteria("enter notification"),
        before=_settings_home(), after=page, latest_step_index=2,
        causal_command_id="command-1", primitive_outcome="progress",
    )
    assert record.overall == "unknown"
    assert record.grounding_digest is None


def test_production_tree_role_cannot_turn_memory_only_nodes_into_success() -> None:
    from ai_game_console.emulator_runtime.general_production import (
        TrustedTreeSemanticRole,
    )

    marker = evidence_marker("page_title", "enter notification")
    page = ObservationEnvelope(
        "task-1", "profile-1", 1, "boot-1", "device-1", "shot", "a" * 64,
        "tree", "b" * 64, "state", "c" * 64, "now", "fresh-target", "target",
        (UiNode(
            opaque_node_id("page"), "Notification settings",
            semantic_kind="page_title", semantic_marker=marker,
        ),),
        "command-1",
    )
    record = EvidenceBoundSemanticVerifier(TrustedTreeSemanticRole()).verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="enter notification", criteria=_criteria("enter notification"),
        before=_settings_home(), after=page, latest_step_index=2,
        causal_command_id="command-1", primitive_outcome="progress",
    )
    assert record.overall == "unknown"
    assert record.grounding_digest is None


def test_satisfied_without_evidence_or_missing_tree_anchor_is_rejected() -> None:
    home = _settings_home()
    with pytest.raises(SemanticVerificationError):
        EvidenceBoundSemanticVerifier(_Role((CriterionVerdict("c1", "satisfied"),))).verify(snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1, goal="x", criteria=_criteria("x"), before=home, after=home, latest_step_index=1, causal_command_id="command-1", primitive_outcome="no_progress")
    invalid = CriterionVerdict("c1", "satisfied", (Anchor("ui_node", "fresh-1", node_id=opaque_node_id("missing")),))
    with pytest.raises(SemanticVerificationError):
        EvidenceBoundSemanticVerifier(_Role((invalid,))).verify(snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1, goal="x", criteria=_criteria("x"), before=home, after=home, latest_step_index=1, causal_command_id="command-1", primitive_outcome="no_progress")


def test_already_satisfied_requires_fresh_initial_evidence() -> None:
    with pytest.raises(SemanticVerificationError):
        EvidenceBoundSemanticVerifier(_Role((CriterionVerdict("c1", "satisfied", (Anchor("ui_node", "fresh-1", node_id=opaque_node_id("home")),)),))).verify(snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1, goal="x", criteria=_criteria("x"), before=None, after=_settings_home(), latest_step_index=0, already_satisfied=True)


@pytest.mark.parametrize("goal", ["enter notification settings", "find battery"])
def test_clickable_settings_navigation_row_cannot_be_malicious_success(goal: str) -> None:
    home = _settings_home()
    forged = CriterionVerdict("c1", "satisfied", (Anchor("ui_node", "fresh-1", node_id=opaque_node_id("notification"), text="通知"),))
    assert EvidenceBoundSemanticVerifier(_Role((forged,))).verify(snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1, goal=goal, criteria=_criteria(goal), before=home, after=home, latest_step_index=1, causal_command_id="command-1", primitive_outcome="no_progress").overall == "unknown"


def test_missing_ui_tree_forces_unknown_even_if_role_claims_success() -> None:
    observation = ObservationEnvelope("task-1", "profile-1", 1, "boot-1", "device-1", "shot", "a" * 64, None, None, "state", "b" * 64, "now", "fresh", "no tree", (), "command-1")
    record = EvidenceBoundSemanticVerifier(_Role((CriterionVerdict("c1", "satisfied"),))).verify(snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1, goal="x", criteria=_criteria("x"), before=observation, after=observation, latest_step_index=1, causal_command_id="command-1", primitive_outcome="no_progress")
    assert record.overall == "unknown" and record.verdicts[0].state == "unknown"


@pytest.mark.parametrize("kind,value", [("container", "settings homepage"), ("page_title", "Settings"), ("state", "wifi enabled")])
def test_unrelated_container_title_or_state_cannot_impersonate_criterion(kind: str, value: str) -> None:
    marker = evidence_marker(kind, value)  # type: ignore[arg-type]
    node_id = opaque_node_id("home-" + kind)
    observation = ObservationEnvelope("task-1", "profile-1", 1, "boot-1", "device-1", "shot", "a" * 64, "tree", "b" * 64, "state", "c" * 64, "now", "fresh", "settings", (UiNode(node_id, value, semantic_kind=kind, semantic_marker=marker),), "command-1")  # type: ignore[arg-type]
    expected = evidence_marker("page_title", "Notification settings")
    values = (Criterion("c1", "enter notification settings", (expected,)),)
    criteria = CriteriaRevision(1, values, criteria_digest(values))
    malicious = CriterionVerdict("c1", "satisfied", (Anchor("ui_node", "fresh", node_id=node_id, semantic_marker=marker),))
    record = EvidenceBoundSemanticVerifier(_Role((malicious,))).verify(snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1, goal="enter notification", criteria=criteria, before=observation, after=observation, latest_step_index=1, causal_command_id="command-1", primitive_outcome="no_progress")
    assert record.overall == "unknown"


def test_model_asserted_screenshot_region_cannot_prove_target_page() -> None:
    marker = evidence_marker("page_title", "notification settings")
    values = (Criterion("c1", "enter notification settings", (marker,)),)
    criteria = CriteriaRevision(1, values, criteria_digest(values))
    verifier = EvidenceBoundSemanticVerifier(
        _Role((CriterionVerdict(
            "c1",
            "satisfied",
            (Anchor("screenshot_region", "after", bounds=(0.0, 0.0, 1.0, 0.2), semantic_marker=marker),),
        ),))
    )
    record = verifier.verify(
        snapshot=_snapshot(),
        runner_kind="android_ui_agent",
        runner_version=1,
        goal="enter notification settings",
        criteria=criteria,
        before=_settings_home("before"),
        after=_settings_home("after"),
        latest_step_index=1,
        causal_command_id="command-1",
        primitive_outcome="no_progress",
    )
    assert record.overall == "unknown"
    assert record.verdicts[0].state == "unknown"


def test_soul_alice_chat_semantics_pass_only_on_matching_completed_state() -> None:
    marker = evidence_marker("state", "Soul Alice chat completed")
    criteria_values = (
        Criterion("c1", "在 Soul 与 Alice 完成聊天", (marker,)),
    )
    criteria = CriteriaRevision(1, criteria_values, criteria_digest(criteria_values))
    home = _settings_home("before")
    navigation_claim = CriterionVerdict(
        "c1", "satisfied",
        (Anchor(
            "ui_node", "before", node_id=opaque_node_id("notification"),
        ),),
    )
    blocked = EvidenceBoundSemanticVerifier(_Role((navigation_claim,))).verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="在 Soul 与 Alice 完成聊天", criteria=criteria,
        before=home, after=home, latest_step_index=1,
        causal_command_id="command-1", primitive_outcome="no_progress",
    )
    assert blocked.overall != "satisfied"

    completed = ObservationEnvelope(
        "task-1", "profile-1", 1, "boot-1", "device-1",
        "shot-chat", "d" * 64, "tree-chat", "e" * 64,
        "state-chat", "f" * 64, "now", "after-chat", "chat complete",
        (UiNode(
            opaque_node_id("chat-complete"), "chat completed",
            semantic_kind="state", semantic_marker=marker,
        ),),
        "command-1",
    )
    grounded = CriterionVerdict(
        "c1", "satisfied",
        (Anchor(
            "ui_node", "after-chat", node_id=opaque_node_id("chat-complete"),
            semantic_marker=marker,
        ),),
    )
    grounding = _TrustedGrounding()
    grounding.trust(completed, 2)
    record = EvidenceBoundSemanticVerifier(_Role((grounded,)), grounding=grounding).verify(
        snapshot=_snapshot(), runner_kind="android_ui_agent", runner_version=1,
        goal="在 Soul 与 Alice 完成聊天", criteria=criteria,
        before=home, after=completed, latest_step_index=2,
        causal_command_id="command-1", primitive_outcome="progress",
    )
    assert record.overall == "satisfied"
