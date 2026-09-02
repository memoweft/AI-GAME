from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_game_console.android_ui_runtime.domain import (
    Anchor,
    AndroidUiAction,
    AndroidUiActionIntent,
    CanonicalSnapshot,
    CriteriaRevision,
    Criterion,
    CriterionVerdict,
    GoalVerificationRecord,
    GroundedUiNode,
    ObservationEnvelope,
    OwnerBinding,
    RoleDecision,
    UiNode,
    criteria_digest,
    evidence_marker,
    observation_grounding_query,
    opaque_node_id,
    grounding_manifest_digest,
    TrustedObservationGroundingManifest,
)
from ai_game_console.android_ui_runtime.role import PrimitiveVerification
from ai_game_console.android_ui_runtime.runner import (
    AndroidUiAgentV1Handler,
    DispatchReceipt,
    ExperienceHint,
)
from ai_game_console.android_ui_runtime.semantic_verification import (
    EvidenceBoundSemanticVerifier,
)
from ai_game_console.android_ui_runtime.store import (
    CheckpointBaselineQuery,
    SQLiteAndroidUiStepStore,
)
from ai_game_console.emulator_runtime.general_production import (
    ProductionAndroidExperiencePort,
    ProductionAndroidUiError,
    ProductionCriteriaProvider,
    ProductionAndroidVerificationAttestor,
    ProductionRuntimeObservationGrounding,
    TrustedTreeSemanticRole,
    _authenticated_scope_for_observation,
    _device_state_digest,
    _durable_command_for_observation,
    _goal_clauses,
    _observable_navigation_goal_clauses,
    _ordered_waypoint_clauses,
    _parse_ui_tree,
    _verification_record_id,
    compose_production_android_ui_runner,
)
from ai_game_console.emulator_runtime.domain import EmulatorFingerprint
from ai_game_console.experience_runtime import (
    ExperienceService,
    SQLiteExperienceStore,
    TrustedVerificationQuery,
)
from ai_game_console.runtime_kernel import ActionStatus


class _Grounding:
    def __init__(self) -> None:
        self.seal = object()
        self.nodes = ()

    def bind(self, observation: ObservationEnvelope) -> None:
        self.nodes = tuple(GroundedUiNode(
            item.node_id, item.semantic_kind, item.semantic_marker,
            item.clickable, item.bounds,
        ) for item in observation.ui_nodes)

    def resolve(self, query):
        return TrustedObservationGroundingManifest(
            query, self.nodes, grounding_manifest_digest(query, self.nodes), self.seal,
        )

    def validates(self, manifest):
        return manifest._attestation is self.seal


TASK = "task-experience"
OWNER = OwnerBinding("principal-experience", "controller-experience")
PROFILE = "profile-experience"
DEVICE = "emulator:profile-experience"
BOOT = "boot-experience"
MARKER = evidence_marker("page_title", "notification settings")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _criteria() -> CriteriaRevision:
    values = (Criterion("c1", "enter notification settings", (MARKER,)),)
    return CriteriaRevision(1, values, criteria_digest(values))


def _snapshot() -> CanonicalSnapshot:
    return CanonicalSnapshot(
        TASK,
        OWNER,
        1,
        "running",
        profile_id=PROFILE,
        profile_generation=1,
        boot_id=BOOT,
        canonical_device_id=DEVICE,
        runner_kind="android_ui_agent",
        runner_version=1,
        runner_binding_id="binding-experience",
    )


def _source(
    token: str,
    *,
    app: str = "com.example.target",
    screenshot: str = "same-screen",
    tree: str = "same-tree",
):
    state = SimpleNamespace(
        status=SimpleNamespace(value="AVAILABLE"),
        foreground_app=app,
        screen_size=(1080, 2400),
        orientation="portrait",
        keyboard_state="hidden",
        connection_state="connected",
    )
    return SimpleNamespace(
        id=token,
        task_id=TASK,
        device_id=DEVICE,
        captured_at="2026-08-31T00:00:00+00:00",
        screenshot=SimpleNamespace(
            status=SimpleNamespace(value="AVAILABLE"),
            artifact=SimpleNamespace(
                sha256=_digest(screenshot), content_type="image/png",
            ),
        ),
        ui_tree=SimpleNamespace(
            status=SimpleNamespace(value="AVAILABLE"),
            artifact=SimpleNamespace(
                sha256=_digest(tree), content_type="application/xml",
            ),
        ),
        device_state=state,
        consistency=SimpleNamespace(status=SimpleNamespace(value="consistent")),
    )


def _observation(source, *, command_id: str | None = None) -> ObservationEnvelope:
    return ObservationEnvelope(
        task_id=TASK,
        profile_id=PROFILE,
        profile_generation=1,
        boot_id=BOOT,
        canonical_device_id=DEVICE,
        screenshot_ref=source.screenshot.artifact.sha256,
        screenshot_digest=source.screenshot.artifact.sha256,
        ui_tree_ref=source.ui_tree.artifact.sha256,
        ui_tree_digest=source.ui_tree.artifact.sha256,
        device_state_ref=_device_state_digest(source.device_state),
        device_state_digest=_device_state_digest(source.device_state),
        observed_at="2026-08-31T00:00:00+00:00",
        freshness_token=source.id,
        ui_summary="safe summary",
        ui_nodes=(UiNode(
            opaque_node_id("notification-title"),
            "Notification settings",
            (0.0, 0.0, 1.0, 0.1),
            False,
            "page_title",
            MARKER,
        ),),
        causality_command_id=command_id,
    )


class _Canonical:
    def __init__(self, snapshot: CanonicalSnapshot) -> None:
        self.snapshot = snapshot

    def inspect(self, task_id: str) -> CanonicalSnapshot:
        assert task_id == TASK
        return self.snapshot


class _Observations:
    def __init__(self, *items: ObservationEnvelope) -> None:
        self.items = list(items)

    def observe(self, snapshot: CanonicalSnapshot) -> ObservationEnvelope:
        assert snapshot.task_id == TASK
        return self.items.pop(0)


class _Planner:
    def __init__(self) -> None:
        self.contexts = []

    def plan(self, context):
        self.contexts.append(context)
        return {"plan": "fresh plan"}


class _Actor:
    def __init__(self, decision: RoleDecision) -> None:
        self.decision = decision

    def decide(self, context, plan):
        del context, plan
        return self.decision


class _Primitive:
    def verify(self, context):
        del context
        return PrimitiveVerification(progress=True)


class _UnknownSemantic:
    def verify_goal(self, context, **kwargs):
        del kwargs
        return tuple(
            CriterionVerdict(item.criterion_id, "unknown")
            for item in context.criteria.criteria
        )


class _SatisfiedSemantic:
    def verify_goal(self, context, *, after, **kwargs):
        del kwargs
        node = after.ui_nodes[0]
        return (CriterionVerdict(
            context.criteria.criteria[0].criterion_id,
            "satisfied",
            (Anchor(
                "ui_node",
                after.freshness_token,
                node_id=node.node_id,
                text=node.text,
                bounds=node.bounds,
                semantic_marker=node.semantic_marker,
            ),),
        ),)


class _Dispatch:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(self, snapshot, intent):
        del snapshot
        self.calls += 1
        return DispatchReceipt("command-experience", intent.action_intent_id)

    def reconcile(self, snapshot, intent):
        raise AssertionError("focused test never reconciles")


class _RunnerExperience:
    def __init__(self, store: SQLiteAndroidUiStepStore, *, fail: bool = False) -> None:
        self.store = store
        self.fail = fail
        self.recorded = []
        self.hint = ExperienceHint(
            candidate_id="candidate-experience",
            retrieval_id="retrieval-experience",
            kind="progress",
            action_kind="tap",
            semantic_anchor="frozen page_title criterion",
            expected_scene_marker="scene:" + "a" * 32,
            confidence=0.75,
            support_count=1,
            failure_count=0,
            provenance_count=2,
        )

    def retrieve(self, **kwargs):
        del kwargs
        if self.fail:
            raise OSError("experience unavailable")
        return (self.hint,)

    def record_step(self, **kwargs):
        if self.fail:
            raise OSError("experience unavailable")
        # K3 usage is durably attempted before the K2/waypoint commit.  This
        # closes the crash window where a committed K2 row would otherwise
        # suppress recovery of the selected-hint outcome.
        assert not self.store.safe_goal_verifications(TASK)
        self.recorded.append(kwargs)


def test_runner_uses_action_free_hints_and_atomically_persists_retrieval_attribution(
    tmp_path: Path,
) -> None:
    before = _observation(_source("fresh-before", screenshot="before", tree="before-tree"))
    after = _observation(
        _source("fresh-after", screenshot="after", tree="after-tree"),
        command_id="command-experience",
    )
    store = SQLiteAndroidUiStepStore(tmp_path / "steps.db")
    experience = _RunnerExperience(store)
    planner = _Planner()
    dispatch = _Dispatch()
    handler = AndroidUiAgentV1Handler(
        canonical=_Canonical(_snapshot()),
        observations=_Observations(before, after),
        planner=planner,
        actor=_Actor(RoleDecision(
            "action", AndroidUiAction("tap", {"x": 0.5, "y": 0.5}),
            selected_hint_id="candidate-experience",
        )),
        action_verifier=_Primitive(),
        semantic_verifier=EvidenceBoundSemanticVerifier(_UnknownSemantic()),
        store=store,
        dispatch=dispatch,
        experience=experience,
    )

    result = handler.one_step(
        task_id=TASK, goal="enter notification settings", criteria=_criteria(),
    )

    assert result.outcome == "continue"
    assert dispatch.calls == 1
    assert planner.contexts[0].observation is before
    assert planner.contexts[0].experience_hints == ({
        "hint_id": "candidate-experience",
        "kind": "progress",
        "action_kind": "tap",
        "semantic_anchor": "frozen page_title criterion",
        "expected_scene_marker": "scene:" + "a" * 32,
        "confidence": 0.75,
        "support_count": 1,
        "failure_count": 0,
        "provenance_count": 2,
    },)
    assert planner.contexts[0].experience_hints[0]["hint_id"] == "candidate-experience"
    assert store.safe_events(TASK)[0]["retrieval"] == [{
        "candidate_id": "candidate-experience",
        "provenance": "retrieval-experience",
        "selected": True,
    }]
    assert len(experience.recorded) == 1
    assert experience.recorded[0]["after"] is after
    assert experience.recorded[0]["primitive_outcome"] == "progress"
    assert experience.recorded[0]["selected_hint"].candidate_id == "candidate-experience"


def test_experience_failure_is_cold_and_short_wait_is_never_learned(
    tmp_path: Path,
) -> None:
    before = _observation(_source("cold-before", screenshot="cold-before", tree="cold-before"))
    after = _observation(
        _source("cold-after", screenshot="cold-after", tree="cold-after"),
        command_id="command-experience",
    )
    cold_store = SQLiteAndroidUiStepStore(tmp_path / "cold.db")
    planner = _Planner()
    cold = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _Observations(before, after),
        planner,
        _Actor(RoleDecision("action", AndroidUiAction("back"))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_UnknownSemantic()),
        cold_store,
        _Dispatch(),
        experience=_RunnerExperience(cold_store, fail=True),
    ).one_step(task_id=TASK, goal="continue safely", criteria=_criteria())
    assert cold.outcome == "continue"
    assert planner.contexts[0].experience_hints == ()
    assert cold_store.safe_events(TASK)[0]["retrieval"] == []

    wait_store = SQLiteAndroidUiStepStore(tmp_path / "wait.db")
    wait_experience = _RunnerExperience(wait_store)
    wait = AndroidUiAgentV1Handler(
        _Canonical(_snapshot()),
        _Observations(before),
        _Planner(),
        _Actor(RoleDecision("action", AndroidUiAction("wait", {"seconds": 1.0}))),
        _Primitive(),
        EvidenceBoundSemanticVerifier(_UnknownSemantic()),
        wait_store,
        _Dispatch(),
        experience=wait_experience,
    ).one_step(task_id=TASK, goal="stabilize", criteria=_criteria())
    assert wait.outcome == "wait"
    assert wait_experience.recorded == []
    assert wait_store.safe_events(TASK)[0]["retrieval"]


class _Profiles:
    def __init__(self) -> None:
        self.calls = []
        self.profile = SimpleNamespace(
            owner_principal_id=OWNER.principal_id,
            owner_controller_id=OWNER.controller_id,
            profile_id=PROFILE,
            profile_generation=1,
            boot_id=BOOT,
            canonical_device_id=DEVICE,
            fingerprint=EmulatorFingerprint(True, 35, "x86_64", "1080x2400", 420),
        )

    def require_profile(self, **kwargs):
        self.calls.append(kwargs)
        return self.profile


class _RuntimeMetadata:
    def __init__(self, *, version: str | None, build: str | None) -> None:
        self.version = version
        self.build = build
        self.calls = []

    def read(self, *, profile, app_package):
        self.calls.append((profile.profile_id, app_package))
        if self.version is None or self.build is None:
            return None
        return SimpleNamespace(
            app_package=app_package,
            app_version_digest=self.version,
            android_build_digest=self.build,
        )


def test_production_scope_uses_only_profile_bound_metadata_and_degrades_cold_on_miss() -> None:
    source = _source("trusted-metadata")
    profiles = _Profiles()
    build = _digest("trusted-profile-build")
    profiles.profile.fingerprint = EmulatorFingerprint(
        True, 35, "x86_64", "1080x2400", 420, build,
    )
    metadata = _RuntimeMetadata(version=_digest("settings-version"), build=build)
    scope = _authenticated_scope_for_observation(
        snapshot=_snapshot(), criteria=_criteria(), source=source,
        profiles=profiles, runtime_metadata=metadata,
    )
    assert metadata.calls == [(PROFILE, "com.example.target")]
    assert scope.app_version == _digest("settings-version")
    assert scope.android_build == build
    assert scope.cross_task_eligible

    cold = _authenticated_scope_for_observation(
        snapshot=_snapshot(), criteria=_criteria(), source=source,
        profiles=profiles, runtime_metadata=_RuntimeMetadata(version=None, build=None),
    )
    assert cold.app_version == cold.android_build == "unknown"
    assert not cold.cross_task_eligible


class _Kernel:
    def __init__(self, *sources) -> None:
        self.sources = {item.id: item for item in sources}
        self.latest = sources[-1] if sources else None

    def load_observation(self, token: str):
        return self.sources[token]

    def latest_observation(self, task_id: str):
        assert task_id == TASK
        return self.latest

    def observations(self, task_id: str):
        assert task_id == TASK
        return tuple(self.sources.values())


def _unsatisfied_record(before: ObservationEnvelope, after: ObservationEnvelope):
    return GoalVerificationRecord(
        task_id=TASK,
        owner=OWNER,
        runner_kind="android_ui_agent",
        runner_version=1,
        revision=1,
        criteria_digest=_criteria().digest,
        latest_step_index=0,
        before=before,
        after=after,
        model_version="model",
        prompt_version="android-ui-semantic-v1",
        verdicts=(CriterionVerdict("c1", "unsatisfied"),),
        overall="unsatisfied",
        already_satisfied=False,
    )


def test_k4_production_experience_uses_only_the_k2_checkpoint_port(
    tmp_path: Path,
) -> None:
    first_source = _source("scope-first")
    next_source = _source("scope-next", screenshot="next-screen")
    drifted_source = _source("scope-drift", app="com.example.other")
    kernel = _Kernel(first_source, next_source, drifted_source)
    profiles = _Profiles()
    database = tmp_path / "experience.db"
    checkpoint_store = SQLiteAndroidUiStepStore(tmp_path / "checkpoint-steps.db")
    checkpoint_store.freeze_criteria(TASK, _criteria())
    first = _observation(first_source)
    reservation = checkpoint_store.reserve_step(
        task_id=TASK, revision=1, before=first, snapshot=_snapshot(),
    )
    service = ExperienceService(
        SQLiteExperienceStore(database),
        checkpoint_baseline_port=checkpoint_store.checkpoint_attestation_port(),
    )
    port = ProductionAndroidExperiencePort(service, profiles, kernel)
    kernel.latest = first_source
    checkpoint = CheckpointBaselineQuery(
        snapshot=_snapshot(), criteria=_criteria(), observation=first,
        runner_kind="android_ui_agent", runner_version=1,
    )
    checkpoint_attestation = checkpoint_store.attest(checkpoint)
    assert checkpoint_attestation is not None
    assert checkpoint_store.validates_for(checkpoint, checkpoint_attestation)

    # The deliberately false integer is ignored; the K2-sealed row is the only
    # checkpoint authority used by the production adapter.
    assert port.retrieve(
        snapshot=_snapshot(), criteria=_criteria(), observation=first,
        step_id="step-secret:https://private.invalid/path", checkpoint_index=999,
    ) == ()
    scope = service.store.android_episode_scope(TASK)
    # Recording consumes the immutable K2 checkpoint, so a concurrent newer
    # observation must not erase the completed step's Experience edge.
    kernel.latest = next_source
    port.record_step(
        snapshot=_snapshot(),
        criteria=_criteria(),
        step_id="step-secret:https://private.invalid/path",
        checkpoint_index=999,
        action=AndroidUiAction("input_text", {"text": "password: K3-PRODUCTION-SECRET"}),
        before=first,
        after=first,
        verification=_unsatisfied_record(first, first),
        primitive_outcome="progress",
        hints=(),
    )

    assert scope.owner_scope_digest not in {OWNER.principal_id, OWNER.controller_id}
    assert scope.profile_id == PROFILE and scope.profile_generation == 1
    assert scope.app_package == "com.example.target"
    assert scope.app_version == "unknown"
    assert scope.android_api == "35"
    assert scope.android_build == "unknown"
    assert scope.resolution == "1080x2400" and scope.density == "420"
    assert scope.orientation == "portrait"
    assert scope.observation_schema == "runtime-kernel-observation-v1"
    assert scope.runner_kind == "android_ui_agent" and scope.runner_version == 1
    assert scope.criteria_digest == _criteria().digest
    assert scope.subtask_id == "binding-experience"
    assert not scope.cross_task_eligible
    assert service.store.android_counts()["candidates"] == 1

    # A later observation remains cold until K2 has durably bound it to the
    # action claim and primitive outcome for this exact step.
    next_observation = _observation(next_source, command_id="checkpoint-command")
    assert port.retrieve(
        snapshot=_snapshot(), criteria=_criteria(), observation=next_observation,
        step_id="next-step", checkpoint_index=2,
    ) == ()
    decision = RoleDecision("action", AndroidUiAction("back"))
    reservation = checkpoint_store.record_decision(reservation, decision)
    intent = AndroidUiActionIntent.create(
        task_id=TASK, criteria_revision=1,
        step_index=reservation.step_index, action=decision.action,
    )
    checkpoint_store.record_intent(reservation, intent)
    reservation = checkpoint_store.record_claim(
        reservation,
        claim_id="checkpoint-command",
        action_intent_id=intent.action_intent_id,
    )
    checkpoint_store.record_after(reservation, next_observation)
    checkpoint_store.record_primitive_outcome(
        reservation, command_id="checkpoint-command", outcome="progress",
    )
    port.record_step(
        snapshot=_snapshot(), criteria=_criteria(), step_id="next-step",
        checkpoint_index=-100, action=AndroidUiAction("back"), before=first,
        after=next_observation,
        verification=_unsatisfied_record(first, next_observation),
        primitive_outcome="progress", hints=(),
    )
    assert service.store.android_counts()["candidates"] == 2
    hints = port.retrieve(
        snapshot=_snapshot(), criteria=_criteria(), observation=next_observation,
        step_id="next-step", checkpoint_index=-100,
    )
    # K3 v2 deliberately identifies the same trusted UI-tree structure even
    # when screenshot pixels differ, so both verified local edges are valid
    # advisory candidates at this fresh structural scene.
    assert len(hints) == 2
    kernel.latest = drifted_source
    assert port.retrieve(
        snapshot=_snapshot(), criteria=_criteria(), observation=_observation(drifted_source),
        step_id="drift-step", checkpoint_index=3,
    ) == ()

    raw = database.read_bytes().decode("latin1", errors="ignore")
    for forbidden in (
        "K3-PRODUCTION-SECRET",
        "private.invalid/path",
        OWNER.principal_id,
        OWNER.controller_id,
    ):
        assert forbidden not in raw


class _Artifacts:
    def __init__(self, source) -> None:
        self.source = source
        self.reads: list[str] = []

    def read(self, artifact):
        self.reads.append(artifact.sha256)
        if artifact is self.source.screenshot.artifact:
            return b"trusted-png"
        if artifact is self.source.ui_tree.artifact:
            return (
                b'<hierarchy><node text="Notification settings" '
                b'class="android.widget.TextView" bounds="[0,0][1080,200]"/>'
                b'</hierarchy>'
            )
        raise AssertionError("foreign artifact")


def test_production_grounding_rereads_exact_k2_bound_artifacts_after_newer_wake(
    tmp_path: Path,
) -> None:
    source = _source("grounding-current")
    kernel = _Kernel(source)
    artifacts = _Artifacts(source)
    grounding = ProductionRuntimeObservationGrounding(
        kernel=kernel,
        artifacts=artifacts,
        canonical=_Canonical(_snapshot()),
    )
    step_store = SQLiteAndroidUiStepStore(
        tmp_path / "grounding-steps.db", grounding=grounding,
    )
    grounding.step_store = step_store
    criteria = _criteria()
    observation = _observation(source)
    step_store.freeze_criteria(TASK, criteria)
    reservation = step_store.reserve_step(
        task_id=TASK, revision=1, before=observation, snapshot=_snapshot(),
    )
    query = observation_grounding_query(
        snapshot=_snapshot(), observation=observation,
        runner_kind="android_ui_agent", runner_version=1,
        step_index=reservation.step_index,
    )

    manifest = grounding.resolve(query)
    assert manifest is not None
    assert grounding.validates(manifest)
    assert tuple(item.semantic_marker for item in manifest.nodes) == (MARKER,)
    assert artifacts.reads == [
        source.screenshot.artifact.sha256,
        source.ui_tree.artifact.sha256,
        source.screenshot.artifact.sha256,
        source.ui_tree.artifact.sha256,
    ]

    kernel.latest = _source("grounding-newer")
    recovered = grounding.resolve(query)
    assert recovered is not None
    assert recovered.query == query

    # A wake may make the causal observation non-latest, but immutable digest
    # drift must still prevent the old ledger row from becoming authority.
    source.screenshot.artifact.sha256 = _digest("tampered-screen")
    assert grounding.resolve(query) is None


def test_android_collapsing_toolbar_content_description_is_page_title() -> None:
    xml = (
        '<hierarchy><node content-desc="通知" '
        'class="android.widget.FrameLayout" '
        'resource-id="com.android.settings:id/collapsing_toolbar" '
        'clickable="false" bounds="[0,48][720,406]"/></hierarchy>'
    ).encode("utf-8")

    nodes = _parse_ui_tree(xml, (720, 2400))

    assert len(nodes) == 1
    assert nodes[0].semantic_kind == "page_title"
    assert nodes[0].semantic_marker == evidence_marker("page_title", "通知")


def test_generic_top_frame_content_description_remains_container() -> None:
    xml = (
        '<hierarchy><node content-desc="通知" '
        'class="android.widget.FrameLayout" '
        'resource-id="com.example:id/header_container" '
        'clickable="false" bounds="[0,48][720,406]"/></hierarchy>'
    ).encode("utf-8")

    nodes = _parse_ui_tree(xml, (720, 2400))

    assert len(nodes) == 1
    assert nodes[0].semantic_kind == "container"
    assert nodes[0].semantic_marker == evidence_marker("container", "通知")


def test_durable_causality_restores_only_latest_exact_accepted_command() -> None:
    command = "command-durable"
    action = SimpleNamespace(
        id=command, task_id=TASK, status=ActionStatus.EXECUTED,
        type=SimpleNamespace(value="tap"),
    )
    execution = SimpleNamespace(
        action_id=command, device_id=DEVICE, accepted=True,
        finished_at="2026-08-31T00:00:01+00:00",
    )
    claim = SimpleNamespace(
        task_id=TASK, action_id=command, command_id=command,
        canonical_device_id=DEVICE,
        owner_principal_id=OWNER.principal_id,
        controller_id=OWNER.controller_id,
        profile_id=PROFILE, profile_generation=1, device_boot_id=BOOT,
        command_type="tap", state="SETTLED", outcome="accepted",
        settled_at="2026-08-31T00:00:02+00:00",
    )
    kernel = SimpleNamespace(
        list_actions=lambda task_id: (action,) if task_id == TASK else (),
        load_action_execution=lambda action_id: execution,
    )
    claims = SimpleNamespace(get=lambda command_id: claim)

    assert _durable_command_for_observation(
        snapshot=_snapshot(), captured_at="2026-08-31T00:00:03+00:00",
        kernel=kernel, claims=claims,
    ) == command

    action.status = ActionStatus.VERIFIED
    assert _durable_command_for_observation(
        snapshot=_snapshot(), captured_at="2026-08-31T00:00:03+00:00",
        kernel=kernel, claims=claims,
    ) == command

    claim.profile_generation = 2
    assert _durable_command_for_observation(
        snapshot=_snapshot(), captured_at="2026-08-31T00:00:03+00:00",
        kernel=kernel, claims=claims,
    ) is None


class _CriteriaCaller:
    def __init__(self, criteria) -> None:
        self.criteria = criteria
        self.calls = 0
        self.requests = []

    def call_tool(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        assert kwargs["tool_name"] == "freeze_android_ui_criteria"
        return self.criteria if isinstance(self.criteria, dict) else {"criteria": self.criteria}


def _valid_coverage_response():
    return [
        {
            "description": "The notification settings target page is visible.",
            "source_quote": "进入通知设置",
            "marker_kind": "page_title",
            "semantic_value": "通知设置",
        },
        {
            "description": "The reminder details target page is visible.",
            "source_quote": "查看提醒详情",
            "marker_kind": "page_title",
            "semantic_value": "提醒详情",
        },
    ]


def test_production_criteria_rejects_missing_goal_clause_before_freeze(
    tmp_path: Path,
) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "missing-coverage.db")
    provider = ProductionCriteriaProvider(
        store,
        _CriteriaCaller(_valid_coverage_response()[:1]),
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="android_ui_criteria_coverage_incomplete",
    ):
        provider.criteria_for(
            task_id=TASK,
            goal="进入通知设置并查看提醒详情",
            revision=1,
        )

    assert store.load_criteria(TASK, 1) is None


def test_production_criteria_rejects_complete_quote_mapped_to_generic_settings_marker(
    tmp_path: Path,
) -> None:
    response = _valid_coverage_response()
    response[0] = {
        **response[0],
        # This literal occurs in the source clause, but is too weak to prove
        # the requested notification page rather than the Settings homepage.
        "semantic_value": "设置",
    }
    store = SQLiteAndroidUiStepStore(tmp_path / "wrong-marker.db")
    provider = ProductionCriteriaProvider(store, _CriteriaCaller(response))

    with pytest.raises(
        ProductionAndroidUiError,
        match="android_ui_criteria_coverage_invalid",
    ):
        provider.criteria_for(
            task_id=TASK,
            goal="进入通知设置并查看提醒详情",
            revision=1,
        )

    assert store.load_criteria(TASK, 1) is None


def _wechat_coverage_response(semantic_value: str):
    return [{
        "description": "The WeChat notification settings target page is visible.",
        "source_quote": "打开微信通知设置",
        "marker_kind": "page_title",
        "semantic_value": semantic_value,
    }]


def test_production_criteria_rejects_marker_that_drops_a_goal_entity(
    tmp_path: Path,
) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "missing-entity.db")
    provider = ProductionCriteriaProvider(
        store, _CriteriaCaller(_wechat_coverage_response("通知设置")),
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="android_ui_criteria_coverage_invalid",
    ):
        provider.criteria_for(
            task_id=TASK,
            goal="打开微信通知设置",
            revision=1,
        )

    assert store.load_criteria(TASK, 1) is None


def test_production_criteria_accepts_complete_entity_preserving_target(
    tmp_path: Path,
) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "complete-entity.db")
    criteria = ProductionCriteriaProvider(
        store, _CriteriaCaller(_wechat_coverage_response("微信通知设置")),
    ).criteria_for(
        task_id=TASK,
        goal="打开微信通知设置",
        revision=1,
    )

    assert criteria.criteria[0].required_evidence_markers == (
        evidence_marker("page_title", "微信通知设置"),
    )
    assert store.coverage_is_frozen(TASK, criteria)


def test_production_criteria_accepts_generic_phone_locator_without_dropping_target(
    tmp_path: Path,
) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "generic-phone-locator.db")
    criteria = ProductionCriteriaProvider(
        store,
        _CriteriaCaller([{
            "description": "The notification target page is visible.",
            "source_quote": "进入手机的通知设置",
            "marker_kind": "page_title",
            "semantic_value": "通知",
        }]),
    ).criteria_for(
        task_id=TASK,
        goal="进入手机的通知设置",
        revision=1,
    )

    assert criteria.criteria[0].required_evidence_markers == (
        evidence_marker("page_title", "通知"),
    )
    assert store.coverage_is_frozen(TASK, criteria)


def test_production_criteria_ignores_nonterminal_safety_clauses(
    tmp_path: Path,
) -> None:
    goal = (
        "打开手机系统的通知设置页面（Settings -> Notifications / 通知），"
        "只需导航到该页面并停留，不要修改任何设置项，也不要点击返回键；"
        "到达后验证当前页面确为通知设置页面。"
    )
    caller = _CriteriaCaller([{
        "description": "The notification settings target page is visible.",
        "source_quote": "打开手机系统的通知设置页面（Settings -> Notifications / 通知）",
        "marker_kind": "page_title",
        "semantic_value": "通知",
    }])
    criteria = ProductionCriteriaProvider(
        SQLiteAndroidUiStepStore(tmp_path / "navigation-with-safety-clauses.db"),
        caller,
    ).criteria_for(task_id=TASK, goal=goal, revision=1)

    assert criteria.criteria[0].required_evidence_markers == (
        evidence_marker("page_title", "通知"),
    )
    assert caller.calls == 1
    assert 'Exact clauses: ["打开手机系统的通知设置页面（Settings -> Notifications / 通知）"]' in caller.requests[0]["prompt"]
    assert 'Target cores: ["通知"]' in caller.requests[0]["prompt"]


def test_production_criteria_accepts_constrained_click_to_enter_navigation(
    tmp_path: Path,
) -> None:
    goal = (
        "从当前手机的通知设置页面开始：先通过界面操作（返回键/返回手势）"
        "逐层返回到系统设置主页；确认已回到设置主页后，再次点击进入\"通知\"设置页面。"
        "全程只做导航，不修改任何设置项。"
    )
    caller = _CriteriaCaller([{
        "description": "The notification settings target page is visible.",
        "source_quote": "再次点击进入\"通知\"设置页面",
        "marker_kind": "page_title",
        "semantic_value": "通知",
    }])
    criteria = ProductionCriteriaProvider(
        SQLiteAndroidUiStepStore(tmp_path / "click-enter-navigation.db"),
        caller,
    ).criteria_for(task_id=TASK, goal=goal, revision=1)

    assert criteria.criteria[0].required_evidence_markers == (
        evidence_marker("page_title", "通知"),
    )
    assert 'Target cores: ["通知"]' in caller.requests[0]["prompt"]


def test_production_criteria_freezes_generic_ordered_waypoint_before_terminal(
    tmp_path: Path,
) -> None:
    goal = "先返回到设置主页，再进入通知设置"
    caller = _CriteriaCaller({
        "criteria": [{
            "description": "The notification target page is visible.",
            "source_quote": "再进入通知设置",
            "marker_kind": "page_title",
            "semantic_value": "通知",
        }],
        "waypoints": [{
            "source_quote": "先返回到设置主页",
            "criteria": [{
                "description": "The settings home container is visible.",
                "marker_kind": "container",
                "semantic_value": "设置主页",
            }],
        }],
    })
    store = SQLiteAndroidUiStepStore(tmp_path / "ordered-waypoints.db")
    criteria = ProductionCriteriaProvider(store, caller).criteria_for(
        task_id=TASK, goal=goal, revision=1,
    )

    assert len(criteria.waypoints) == 1
    assert criteria.waypoints[0].ordinal == 1
    assert criteria.waypoints[0].criteria[0].required_evidence_markers == (
        evidence_marker("container", "设置主页"),
    )
    loaded = store.load_criteria(TASK, 1)
    assert loaded == criteria and store.coverage_is_frozen(TASK, criteria)


@pytest.mark.parametrize(
    "goal",
    (
        "进入页面甲，进入页面乙，最后不要执行其他操作",
        "进入页面甲，或者，请进入页面乙，然后告诉我结果",
        "进入页面甲，同时进入页面乙，之后不要修改任何内容",
    ),
)
def test_ordered_waypoint_parser_rejects_lists_alternatives_and_parallel_navigation(
    tmp_path: Path, goal: str,
) -> None:
    clauses = _observable_navigation_goal_clauses(goal)
    assert len(clauses) == 2
    assert _ordered_waypoint_clauses(goal, clauses) == ()
    caller = _CriteriaCaller({
        "criteria": [
            {
                "description": "First requested page is visible.",
                "source_quote": clauses[0].text,
                "marker_kind": "page_title",
                "semantic_value": "页面甲",
            },
            {
                "description": "Second requested page is visible.",
                "source_quote": clauses[1].text,
                "marker_kind": "page_title",
                "semantic_value": "页面乙",
            },
        ],
        "waypoints": [],
    })
    criteria = ProductionCriteriaProvider(
        SQLiteAndroidUiStepStore(tmp_path / f"ambiguous-{hashlib.sha256(goal.encode()).hexdigest()}.db"),
        caller,
    ).criteria_for(task_id=TASK, goal=goal, revision=1)
    assert criteria.waypoints == ()


@pytest.mark.parametrize(
    "goal",
    (
        "先进入页面甲，再进入页面乙",
        "进入页面甲后再进入页面乙",
        "from page alpha then enter page beta",
    ),
)
def test_ordered_waypoint_parser_keeps_direct_adjacent_order_cues(goal: str) -> None:
    clauses = _observable_navigation_goal_clauses(goal)
    assert len(clauses) == 2
    assert _ordered_waypoint_clauses(goal, clauses) == (clauses[0],)


def test_production_criteria_prompt_pins_compound_target_core_verbatim(
    tmp_path: Path,
) -> None:
    caller = _CriteriaCaller([{
        "description": "The requested sound and vibration page is visible.",
        "source_quote": "进入手机的提示音和振动设置",
        "marker_kind": "page_title",
        "semantic_value": "提示音和振动",
    }])
    provider = ProductionCriteriaProvider(
        SQLiteAndroidUiStepStore(tmp_path / "compound-target.db"), caller,
    )

    provider.criteria_for(
        task_id=TASK, goal="进入手机的提示音和振动设置", revision=1,
    )

    assert 'Target cores: ["提示音和振动"]' in caller.requests[0]["prompt"]
    assert "Do not substitute synonyms" in caller.requests[0]["system"]


def test_generic_device_page_title_rejects_extra_generic_ui_suffix(
    tmp_path: Path,
) -> None:
    provider = ProductionCriteriaProvider(
        SQLiteAndroidUiStepStore(tmp_path / "generic-title-suffix.db"),
        _CriteriaCaller([{
            "description": "The requested sound and vibration page is visible.",
            "source_quote": "进入手机的提示音和振动设置",
            "marker_kind": "page_title",
            "semantic_value": "提示音和振动设置",
        }]),
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="android_ui_criteria_coverage_invalid",
    ):
        provider.criteria_for(
            task_id=TASK, goal="进入手机的提示音和振动设置", revision=1,
        )


def test_generic_phone_locator_does_not_make_named_app_optional(
    tmp_path: Path,
) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "generic-phone-named-app.db")
    provider = ProductionCriteriaProvider(
        store,
        _CriteriaCaller([{
            "description": "A generic notification page is visible.",
            "source_quote": "进入手机的微信通知设置",
            "marker_kind": "page_title",
            "semantic_value": "通知",
        }]),
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="android_ui_criteria_coverage_invalid",
    ):
        provider.criteria_for(
            task_id=TASK,
            goal="进入手机的微信通知设置",
            revision=1,
        )

    assert store.load_criteria(TASK, 1) is None


def test_production_criteria_treats_system_settings_route_as_nonterminal_context(
    tmp_path: Path,
) -> None:
    goal = "进入手机的通知设置（打开系统设置 → 通知）"
    store = SQLiteAndroidUiStepStore(tmp_path / "system-settings-route.db")
    criteria = ProductionCriteriaProvider(
        store,
        _CriteriaCaller([{
            "description": "The notification target page is visible.",
            "source_quote": goal,
            "marker_kind": "page_title",
            "semantic_value": "通知",
        }]),
    ).criteria_for(task_id=TASK, goal=goal, revision=1)

    assert criteria.criteria[0].required_evidence_markers == (
        evidence_marker("page_title", "通知"),
    )
    assert store.coverage_is_frozen(TASK, criteria)


def test_system_settings_route_does_not_make_named_app_optional(
    tmp_path: Path,
) -> None:
    goal = "进入手机的微信通知设置（打开系统设置 → 通知）"
    store = SQLiteAndroidUiStepStore(tmp_path / "system-settings-route-named-app.db")
    provider = ProductionCriteriaProvider(
        store,
        _CriteriaCaller([{
            "description": "A generic notification page is visible.",
            "source_quote": goal,
            "marker_kind": "page_title",
            "semantic_value": "通知",
        }]),
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="android_ui_criteria_coverage_invalid",
    ):
        provider.criteria_for(task_id=TASK, goal=goal, revision=1)

    assert store.load_criteria(TASK, 1) is None


@pytest.mark.parametrize(
    ("goal", "semantic_value"),
    (
        ("启用提醒", "提醒"),
        ("关闭飞行模式", "飞行模式"),
        ("设置助手页面", "助手"),
    ),
)
def test_text_only_mutation_or_ambiguous_setting_clause_cannot_freeze(
    tmp_path: Path,
    goal: str,
    semantic_value: str,
) -> None:
    store = SQLiteAndroidUiStepStore(
        tmp_path / f"unsupported-state-{hashlib.sha256(goal.encode()).hexdigest()}.db"
    )
    provider = ProductionCriteriaProvider(
        store,
        _CriteriaCaller([{
            "description": "A model-proposed text-only state marker.",
            "source_quote": goal,
            "marker_kind": "state",
            "semantic_value": semantic_value,
        }]),
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="android_ui_criteria_coverage_invalid",
    ):
        provider.criteria_for(task_id=TASK, goal=goal, revision=1)

    assert store.load_criteria(TASK, 1) is None


@pytest.mark.parametrize(
    "state_attributes",
    (
        'checked="false" selected="false"',
        'checked="true" selected="true"',
    ),
)
def test_text_only_switch_row_is_unknown_for_either_boolean_polarity(
    tmp_path: Path,
    state_attributes: str,
) -> None:
    state_marker = evidence_marker("state", "飞行模式")
    criteria_values = (Criterion(
        "c1", "typed airplane mode state is proven", (state_marker,),
    ),)
    criteria = CriteriaRevision(
        1, criteria_values, criteria_digest(criteria_values),
    )
    xml = (
        '<hierarchy><node text="飞行模式" class="android.widget.Switch" '
        'clickable="false" bounds="[0,900][800,1100]" '
        f'{state_attributes} /></hierarchy>'
    ).encode("utf-8")
    observation = replace(
        _observation(_source("opposite-switch-state")),
        ui_nodes=_parse_ui_tree(xml, (1080, 2400)),
    )
    assert observation.ui_nodes[0].semantic_marker == state_marker
    store = SQLiteAndroidUiStepStore(tmp_path / "opposite-switch-state.db")
    handler = AndroidUiAgentV1Handler(
        canonical=_Canonical(_snapshot()),
        observations=_Observations(observation),
        planner=_Planner(),
        actor=_Actor(RoleDecision("terminal_candidate")),
        action_verifier=_Primitive(),
        semantic_verifier=EvidenceBoundSemanticVerifier(
            TrustedTreeSemanticRole(),
        ),
        store=store,
        dispatch=_Dispatch(),
    )

    result = handler.one_step(
        task_id=TASK,
        goal="飞行模式状态已正确设置",
        criteria=criteria,
    )

    assert result.outcome == "terminal_unverified"
    assert store.safe_goal_verifications(TASK)[0]["overall"] == "unknown"
    assert handler.trusted_terminal(task_id=TASK, criteria=criteria) is None


def test_generic_settings_page_cannot_terminal_for_entity_scoped_goal(
    tmp_path: Path,
) -> None:
    store = SQLiteAndroidUiStepStore(tmp_path / "generic-current-page.db")
    criteria = ProductionCriteriaProvider(
        store, _CriteriaCaller(_wechat_coverage_response("微信通知设置")),
    ).criteria_for(
        task_id=TASK,
        goal="打开微信通知设置",
        revision=1,
    )
    source = _source("generic-settings-current")
    generic_marker = evidence_marker("page_title", "通知设置")
    observation = replace(
        _observation(source),
        ui_nodes=(UiNode(
            opaque_node_id("generic-notification-settings-title"),
            "通知设置",
            (0.0, 0.0, 1.0, 0.1),
            False,
            "page_title",
            generic_marker,
        ),),
    )
    handler = AndroidUiAgentV1Handler(
        canonical=_Canonical(_snapshot()),
        observations=_Observations(observation),
        planner=_Planner(),
        actor=_Actor(RoleDecision("terminal_candidate")),
        action_verifier=_Primitive(),
        semantic_verifier=EvidenceBoundSemanticVerifier(
            TrustedTreeSemanticRole(),
        ),
        store=store,
        dispatch=_Dispatch(),
    )

    result = handler.one_step(
        task_id=TASK,
        goal="打开微信通知设置",
        criteria=criteria,
    )

    assert result.outcome == "terminal_unverified"
    assert not result.semantic_satisfied
    assert store.safe_goal_verifications(TASK)[0]["overall"] == "unknown"
    assert handler.trusted_terminal(task_id=TASK, criteria=criteria) is None


def test_production_criteria_durably_binds_goal_coverage_and_rejects_goal_drift(
    tmp_path: Path,
) -> None:
    database = tmp_path / "covered-criteria.db"
    store = SQLiteAndroidUiStepStore(database)
    caller = _CriteriaCaller(_valid_coverage_response())
    provider = ProductionCriteriaProvider(store, caller)

    frozen = provider.criteria_for(
        task_id=TASK,
        goal="进入通知设置并查看提醒详情",
        revision=1,
    )

    assert frozen.goal_digest and frozen.coverage_digest
    assert store.coverage_is_frozen(TASK, frozen)
    assert store.load_criteria(TASK, 1) == frozen
    assert caller.calls == 1
    # A restart reuses the exact durable proof without another model call.
    assert provider.criteria_for(
        task_id=TASK,
        goal="进入通知设置并查看提醒详情",
        revision=1,
    ) == frozen
    assert caller.calls == 1
    with pytest.raises(
        ProductionAndroidUiError,
        match="android_ui_criteria_coverage_invalid",
    ):
        provider.criteria_for(
            task_id=TASK,
            goal="进入通知设置",
            revision=1,
        )

    with store._connect() as connection:  # private ledger assertion
        coverage_json = str(connection.execute(
            "SELECT coverage_json FROM android_ui_criteria WHERE task_id=? AND revision=?",
            (TASK, 1),
        ).fetchone()[0])
    assert "进入通知设置" not in coverage_json
    assert "查看提醒详情" not in coverage_json


class _AllSatisfiedSemantic:
    def verify_goal(self, context, *, after, **kwargs):
        del kwargs
        verdicts = []
        for criterion in context.criteria.criteria:
            marker = criterion.required_evidence_markers[0]
            node = next(item for item in after.ui_nodes if item.semantic_marker == marker)
            verdicts.append(CriterionVerdict(
                criterion.criterion_id,
                "satisfied",
                (Anchor(
                    "ui_node",
                    after.freshness_token,
                    node_id=node.node_id,
                    text=node.text,
                    bounds=node.bounds,
                    semantic_marker=marker,
                ),),
            ))
        return tuple(verdicts)


def _with_criteria_nodes(
    observation: ObservationEnvelope, criteria: CriteriaRevision,
) -> ObservationEnvelope:
    return replace(observation, ui_nodes=tuple(
        UiNode(
            opaque_node_id(f"covered-node-{index}"),
            f"safe marker {index}",
            (0.0, index * 0.1, 1.0, min(1.0, (index + 1) * 0.1)),
            False,
            marker.split(":", 1)[0],
            marker,
        )
        for index, criterion in enumerate(criteria.criteria)
        for marker in criterion.required_evidence_markers
    ))


def test_goal_and_coverage_digests_bind_the_immutable_semantic_terminal(
    tmp_path: Path,
) -> None:
    grounding = _Grounding()
    store = SQLiteAndroidUiStepStore(
        tmp_path / "covered-terminal.db", grounding=grounding,
    )
    criteria = ProductionCriteriaProvider(
        store, _CriteriaCaller(_valid_coverage_response()),
    ).criteria_for(
        task_id=TASK,
        goal="进入通知设置并查看提醒详情",
        revision=1,
    )
    before = _with_criteria_nodes(
        _observation(_source("covered-before", screenshot="covered-before", tree="covered-before")),
        criteria,
    )
    after = _with_criteria_nodes(
        _observation(
            _source("covered-after", screenshot="covered-after", tree="covered-after"),
            command_id="covered-command",
        ),
        criteria,
    )
    grounding.bind(after)
    reservation = store.reserve_step(task_id=TASK, revision=1, before=before, snapshot=_snapshot())
    decision = RoleDecision("action", AndroidUiAction("back"))
    reservation = store.record_decision(reservation, decision)
    intent = AndroidUiActionIntent.create(
        task_id=TASK,
        criteria_revision=1,
        step_index=reservation.step_index,
        action=decision.action,
    )
    store.record_intent(reservation, intent)
    reservation = store.record_claim(
        reservation, claim_id="covered-command", action_intent_id=intent.action_intent_id,
    )
    store.record_after(reservation, after)
    store.record_primitive_outcome(
        reservation, command_id="covered-command", outcome="progress",
    )
    verification = EvidenceBoundSemanticVerifier(
        _AllSatisfiedSemantic(), grounding=grounding,
    ).verify(
        snapshot=_snapshot(),
        runner_kind="android_ui_agent",
        runner_version=1,
        goal="进入通知设置并查看提醒详情",
        criteria=criteria,
        before=before,
        after=after,
        latest_step_index=reservation.step_index,
        causal_command_id="covered-command",
        primitive_outcome="progress",
    )
    store.record_verification(verification)

    assert verification.goal_digest == criteria.goal_digest
    assert verification.coverage_digest == criteria.coverage_digest
    immutable = store.immutable_goal_verification(
        task_id=TASK, revision=1, step_index=reservation.step_index,
    )
    assert immutable is not None
    assert immutable["goal_digest"] == criteria.goal_digest
    assert immutable["coverage_digest"] == criteria.coverage_digest
    trusted = store.trusted_terminal_for(
        _snapshot(), criteria,
        runner_kind="android_ui_agent", runner_version=1,
    )
    assert trusted is not None
    assert trusted.goal_digest == criteria.goal_digest
    assert trusted.coverage_digest == criteria.coverage_digest
    assert store.trusted_terminal_for(
        _snapshot(), replace(criteria, coverage_digest="0" * 64),
        runner_kind="android_ui_agent", runner_version=1,
    ) is None


def test_production_attestor_replays_the_exact_k2_terminal_record(
    tmp_path: Path,
) -> None:
    before_source = _source("attest-before", screenshot="attest-before", tree="attest-before")
    after_source = _source("attest-after", screenshot="attest-after", tree="attest-after")
    before = _observation(before_source)
    after = _observation(after_source, command_id="command-attested")
    snapshot = _snapshot()
    criteria = _criteria()
    grounding = _Grounding()
    step_store = SQLiteAndroidUiStepStore(
        tmp_path / "attestation-steps.db", grounding=grounding,
    )
    grounding.bind(after)
    step_store.freeze_criteria(TASK, criteria)
    reservation = step_store.reserve_step(
        task_id=TASK, revision=1, before=before, snapshot=snapshot,
    )
    decision = RoleDecision("action", AndroidUiAction("back"))
    reservation = step_store.record_decision(reservation, decision)
    intent = AndroidUiActionIntent.create(
        task_id=TASK,
        criteria_revision=1,
        step_index=reservation.step_index,
        action=decision.action,
    )
    step_store.record_intent(reservation, intent)
    reservation = step_store.record_claim(
        reservation, claim_id="command-attested", action_intent_id=intent.action_intent_id,
    )
    step_store.record_after(reservation, after)
    step_store.record_primitive_outcome(
        reservation, command_id="command-attested", outcome="progress",
    )
    verification = EvidenceBoundSemanticVerifier(
        _SatisfiedSemantic(), grounding=grounding,
    ).verify(
        snapshot=snapshot,
        runner_kind="android_ui_agent",
        runner_version=1,
        goal="enter notification settings",
        criteria=criteria,
        before=before,
        after=after,
        latest_step_index=reservation.step_index,
        causal_command_id="command-attested",
        primitive_outcome="progress",
    )
    step_store.record_verification(verification)
    kernel = _Kernel(before_source, after_source)
    profiles = _Profiles()
    attestor = ProductionAndroidVerificationAttestor(
        step_store=step_store,
        canonical=_Canonical(snapshot),
        profiles=profiles,
        kernel=kernel,
    )
    scope = _authenticated_scope_for_observation(
        snapshot=snapshot,
        criteria=criteria,
        source=after_source,
        profiles=profiles,
    )
    immutable = step_store.immutable_goal_verification(
        task_id=TASK, revision=1, step_index=reservation.step_index,
    )
    assert immutable is not None
    record_id = _verification_record_id(scope, immutable)
    query = TrustedVerificationQuery(
        scope=scope,
        scope_digest=scope.attestation_digest,
        record_id=record_id,
        task_id=TASK,
        revision=1,
        step_index=reservation.step_index,
    )

    # The semantic-record attestor is K2-owned and read-only.  K3 still needs
    # the separate sealed CheckpointBaselineAttestationPort before it can turn
    # this proof into a reusable edge, but this exact terminal row itself is
    # now replayable after a process restart.
    attested = attestor.attest(query)
    assert attested is not None
    assert attested.record_id == record_id
    assert attested.immutable_record == immutable
    assert attestor.attest(replace(query, record_id="record_" + "0" * 64)) is None
    assert attestor.attest(replace(query, scope_digest="0" * 64)) is None


def test_composition_installs_a_narrow_attested_facade_without_runtime_io(
    tmp_path: Path,
) -> None:
    (tmp_path / "runtime").mkdir()
    shared = ExperienceService(SQLiteExperienceStore(tmp_path / "experience.db"))
    metadata = object()
    composition = compose_production_android_ui_runner(
        data_dir=tmp_path,
        kernel=object(),
        artifacts=object(),
        runtime_store=object(),
        profiles=object(),
        role_model=SimpleNamespace(model="fake-model"),
        evidence=object(),
        experience_service=shared,
        runtime_metadata=metadata,
    )

    assert composition.experience is composition.runner.handler.experience
    assert composition.verification_attestor is not None
    assert composition.verification_attestor.runtime_metadata is metadata
    assert composition.experience is not None
    assert composition.experience.service.store is shared.store
    assert (
        composition.experience.service.trusted_verification_port
        is composition.verification_attestor
    )
    assert (
        composition.experience.service.checkpoint_baseline_port
        is composition.step_store
    )
    assert composition.grounding.step_store is composition.step_store
    assert composition.runner.handler.semantic_verifier.grounding is composition.grounding
