from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_game_console.agent_runtime.domain import RunnerDispatchRequest, TaskDispatchConflict
from ai_game_console.agent_runtime.service import CanonicalTaskService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.android_ui_runtime.domain import (
    AndroidUiAction,
    AndroidUiActionIntent,
    CanonicalSnapshot,
    CriteriaRevision,
    Criterion,
    CriterionVerdict,
    ObservationEnvelope,
    OwnerBinding,
    RoleDecision,
    UiNode,
    criteria_digest,
    evidence_marker,
    opaque_node_id,
)
from ai_game_console.android_ui_runtime.role import PrimitiveVerification
from ai_game_console.android_ui_runtime.runner import (
    AndroidUiAgentV1Handler,
    DispatchReceipt,
)
from ai_game_console.android_ui_runtime.semantic_verification import (
    EvidenceBoundSemanticVerifier,
)
from ai_game_console.android_ui_runtime.store import SQLiteAndroidUiStepStore
from ai_game_console.emulator_runtime.general_production import (
    ProductionAndroidDispatchPort,
    ProductionGenericEvidenceLookup,
    ProductionKernelPhysicalDispatcher,
    ProductionAndroidUiError,
    _CausalityLedger,
    _command_id,
    _step_id,
)
from ai_game_console.emulator_runtime.general_kernel import (
    GenericKernelCommandPort,
    GenericProfileBinding,
)
from ai_game_console.emulator_runtime.general_store import (
    GenericCommandClaim,
    GenericCommandConflict,
    SQLiteGenericCommandStore,
    command_payload_digest,
)
from ai_game_console.runtime_kernel import ActionStatus, ActionType, RecordNotFound


PRINCIPAL = "principal-settlement"
CONTROLLER = "controller-settlement"
PROFILE = "profile-settlement"
DEVICE = "emulator:profile-settlement"
BOOT = "boot-settlement"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _criteria() -> CriteriaRevision:
    marker = evidence_marker("page_title", "notification settings")
    values = (Criterion("criterion-1", "enter notification settings", (marker,)),)
    return CriteriaRevision(1, values, criteria_digest(values))


def _observation(
    snapshot: CanonicalSnapshot,
    token: str,
    *,
    command_id: str | None = None,
) -> ObservationEnvelope:
    marker = evidence_marker("page_title", "notification settings")
    return ObservationEnvelope(
        task_id=snapshot.task_id,
        profile_id=PROFILE,
        profile_generation=1,
        boot_id=BOOT,
        canonical_device_id=DEVICE,
        screenshot_ref=_digest("screenshot:" + token),
        screenshot_digest=_digest("screenshot:" + token),
        ui_tree_ref=_digest("tree:" + token),
        ui_tree_digest=_digest("tree:" + token),
        device_state_ref=_digest("state:" + token),
        device_state_digest=_digest("state:" + token),
        observed_at="2030-01-01T00:00:02+00:00",
        freshness_token=token,
        ui_summary="safe test screen",
        ui_nodes=(
            UiNode(
                opaque_node_id("notification-title"),
                "Notification settings",
                semantic_kind="page_title",
                semantic_marker=marker,
            ),
        ),
        causality_command_id=command_id,
    )


class _Canonical:
    def __init__(
        self,
        runtime_store: SQLiteAgentRuntimeStore,
        snapshot: CanonicalSnapshot,
    ) -> None:
        self.runtime_store = runtime_store
        self.snapshot = snapshot

    def inspect(self, task_id: str) -> CanonicalSnapshot:
        assert task_id == self.snapshot.task_id
        return self.snapshot


class _Profiles:
    def resolve_ready(self, **kwargs: object) -> object:
        assert kwargs == {
            "principal_id": PRINCIPAL,
            "controller_id": CONTROLLER,
            "profile_id": PROFILE,
            "expected_generation": 1,
        }
        return SimpleNamespace(
            owner_principal_id=PRINCIPAL,
            owner_controller_id=CONTROLLER,
            profile_id=PROFILE,
            profile_generation=1,
            boot_id=BOOT,
            canonical_device_id=DEVICE,
        )


class _Kernel:
    def __init__(self, snapshot: CanonicalSnapshot, *, accepted: bool) -> None:
        self.snapshot = snapshot
        self.accepted = accepted
        self.error = None if accepted else SimpleNamespace(code="executor_unicode_text_cli_unavailable", retryable=False)
        self.actions: dict[str, SimpleNamespace] = {}
        self.executions: dict[str, SimpleNamespace] = {}
        self.verifications: list[dict[str, object]] = []

    def latest_observation(self, task_id: str) -> object:
        assert task_id == self.snapshot.task_id
        return SimpleNamespace(
            id="before-observation",
            device_state=SimpleNamespace(screen_size=(1080, 2400)),
        )

    def load_task(self, task_id: str) -> object:
        assert task_id == self.snapshot.task_id
        return SimpleNamespace(last_observation_id="before-observation")

    def load_action(self, task_id: str, action_id: str) -> object:
        assert task_id == self.snapshot.task_id
        try:
            return self.actions[action_id]
        except KeyError as error:
            raise RecordNotFound(action_id) from error

    def load_action_by_id(self, action_id: str) -> object:
        try:
            return self.actions[action_id]
        except KeyError as error:
            raise RecordNotFound(action_id) from error

    def propose_action(self, **kwargs: object) -> object:
        action = SimpleNamespace(
            id=str(kwargs["action_id"]),
            task_id=str(kwargs["task_id"]),
            stage_id=str(kwargs["stage_id"]),
            based_on_observation_id=str(kwargs["based_on_observation_id"]),
            type=kwargs["action_type"],
            params=dict(kwargs["params"]),
            proposed_by_call_id=str(kwargs["proposed_by_call_id"]),
            status=ActionStatus.PROPOSED,
        )
        self.actions[action.id] = action
        return action

    def execute_action(self, *, task_id: str, action_id: str) -> object:
        action = self.load_action(task_id, action_id)
        if action.status is not ActionStatus.PROPOSED:
            raise ValueError("only a PROPOSED Action can be dispatched")
        action.status = ActionStatus.EXECUTED if self.accepted else ActionStatus.FAILED
        execution = SimpleNamespace(
            action_id=action_id,
            accepted=self.accepted,
            error=self.error,
            finished_at="2030-01-01T00:00:01+00:00",
        )
        self.executions[action_id] = execution
        return execution

    def load_action_execution(self, action_id: str) -> object:
        try:
            return self.executions[action_id]
        except KeyError as error:
            raise RecordNotFound(action_id) from error

    def load_verification(self, action_id: str) -> object:
        raise RecordNotFound(action_id)

    def load_observation(self, observation_id: str) -> object:
        if observation_id != "after-observation":
            raise RecordNotFound(observation_id)
        return SimpleNamespace(
            id=observation_id,
            task_id=self.snapshot.task_id,
            device_id=self.snapshot.canonical_device_id,
            capture_started_at="2030-01-01T00:00:02+00:00",
            captured_at="2030-01-01T00:00:02+00:00",
            screenshot=SimpleNamespace(
                artifact=SimpleNamespace(reference="artifact:screenshot-after")
            ),
            ui_tree=SimpleNamespace(artifact=None),
        )

    def verify_action(self, **kwargs: object) -> None:
        self.verifications.append(dict(kwargs))


class _Observations:
    def __init__(
        self,
        snapshot: CanonicalSnapshot,
        causality: _CausalityLedger,
    ) -> None:
        self.snapshot = snapshot
        self.causality = causality
        self.calls = 0

    def observe(self, snapshot: CanonicalSnapshot) -> ObservationEnvelope:
        assert snapshot == self.snapshot
        self.calls += 1
        if self.calls == 1:
            return _observation(snapshot, "before-observation")
        assert self.calls == 2
        command_id = self.causality.command_for(
            snapshot.task_id,
            "2030-01-01T00:00:02+00:00",
        )
        return _observation(
            snapshot,
            "after-observation",
            command_id=command_id,
        )


class _Planner:
    def plan(self, context: object) -> dict[str, str]:
        del context
        return {"plan": "one bounded action"}


class _Actor:
    def decide(self, context: object, plan: object) -> RoleDecision:
        del context, plan
        return RoleDecision("action", AndroidUiAction("back"))


class _Primitive:
    def verify(self, context: object) -> PrimitiveVerification:
        del context
        return PrimitiveVerification(progress=True, reason="fresh after captured")


class _UnknownSemantic:
    def __init__(self) -> None:
        self.calls = 0

    def verify_goal(self, context: object, **kwargs: object) -> tuple[CriterionVerdict, ...]:
        del context, kwargs
        self.calls += 1
        return (CriterionVerdict("criterion-1", "unknown"),)


def _effect(database_path: Path, command_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT state,outcome FROM task_runner_effects WHERE command_id=?",
            (command_id,),
        ).fetchone()
    assert row is not None
    return str(row[0]), (str(row[1]) if row[1] is not None else None)


def _fixture(tmp_path: Path, *, accepted: bool) -> SimpleNamespace:
    agent_path = tmp_path / "agent-runtime.db"
    runtime_store = SQLiteAgentRuntimeStore(agent_path)
    service = CanonicalTaskService(
        runtime_store,
        principal_id=PRINCIPAL,
        controller_id=CONTROLLER,
    )
    task = service.create_task(
        "Enter notification settings",
        "create-settlement-test",
        origin={"runner_kind": "android_ui_agent", "runner_version": "1"},
    )
    task_id = str(task["task_id"])
    binding = service.upsert_subtask(
        task_id,
        kind="android_ui_agent",
        object_ref=PROFILE,
        conversation_ref=None,
        status="scheduled",
        current_stage="observe",
    )["subtask"]
    service.transition_task(
        task_id,
        status="running",
        reason_code="runner_ready",
        summary="Runner ready.",
        recoverable=True,
        idempotency_key="runner-ready",
        expected_current_status="scheduled",
    )
    snapshot = CanonicalSnapshot(
        task_id=task_id,
        owner=OwnerBinding(PRINCIPAL, CONTROLLER),
        revision=1,
        status="running",
        profile_id=PROFILE,
        profile_generation=1,
        boot_id=BOOT,
        canonical_device_id=DEVICE,
        runner_kind="android_ui_agent",
        runner_version=1,
        runner_binding_id=str(binding["id"]),
    )
    causality = _CausalityLedger()
    kernel = _Kernel(snapshot, accepted=accepted)
    claims = SQLiteGenericCommandStore(tmp_path / "generic-claims.db")
    canonical = _Canonical(runtime_store, snapshot)
    physical = ProductionKernelPhysicalDispatcher(
        kernel=kernel,  # type: ignore[arg-type]
        causality=causality,
        claims=claims,
        canonical_store=runtime_store,
    )
    dispatch = ProductionAndroidDispatchPort(
        canonical=canonical,
        kernel=kernel,  # type: ignore[arg-type]
        profiles=_Profiles(),
        claims=claims,
        physical=physical,
        evidence_lookup=ProductionGenericEvidenceLookup(kernel),  # type: ignore[arg-type]
    )
    semantic = _UnknownSemantic()
    handler = AndroidUiAgentV1Handler(
        canonical=canonical,
        observations=_Observations(snapshot, causality),
        planner=_Planner(),
        actor=_Actor(),
        action_verifier=_Primitive(),
        semantic_verifier=EvidenceBoundSemanticVerifier(semantic),
        store=SQLiteAndroidUiStepStore(tmp_path / "android-ui-steps.db"),
        dispatch=dispatch,
    )
    action = AndroidUiAction("back")
    intent = AndroidUiActionIntent.create(
        task_id=task_id,
        criteria_revision=1,
        step_index=0,
        action=action,
    )
    return SimpleNamespace(
        agent_path=agent_path,
        service=service,
        runtime_store=runtime_store,
        snapshot=snapshot,
        causality=causality,
        kernel=kernel,
        claims=claims,
        physical=physical,
        dispatch=dispatch,
        handler=handler,
        semantic=semantic,
        intent=intent,
        command_id=_command_id(snapshot, intent),
    )


def _runner_request(
    fixture: SimpleNamespace,
    *,
    command_id: str | None = None,
    step_id: str | None = None,
) -> RunnerDispatchRequest:
    resolved_command = command_id or fixture.command_id
    return RunnerDispatchRequest(
        step_id=step_id or _step_id(fixture.snapshot.task_id, fixture.intent),
        action_id=resolved_command,
        command_id=resolved_command,
        runner_kind="android_ui_agent",
        runner_version="1",
        subtask_id=str(fixture.snapshot.runner_binding_id),
        profile_id=PROFILE,
        profile_generation=1,
        device_boot_id=BOOT,
        canonical_device_id=DEVICE,
        command_type="back",
        payload_digest=command_payload_digest("back", {}),
        expected_revision=1,
    )


def _install_kernel_action(
    fixture: SimpleNamespace, request: RunnerDispatchRequest,
) -> None:
    fixture.kernel.actions[request.action_id] = SimpleNamespace(
        id=request.action_id,
        task_id=fixture.snapshot.task_id,
        stage_id="android-ui-stage-test",
        based_on_observation_id="before-observation",
        type=ActionType.BACK,
        params={},
        proposed_by_call_id="test-intent",
        status=ActionStatus.PROPOSED,
    )


def _claim_for_request(
    fixture: SimpleNamespace,
    request: RunnerDispatchRequest,
    *,
    dispatch_id: str,
) -> GenericCommandClaim:
    claim, created = fixture.claims.claim(
        dispatch_id=dispatch_id,
        task_id=fixture.snapshot.task_id,
        step_id=request.step_id,
        subtask_id=request.subtask_id,
        action_id=request.action_id,
        command_id=request.command_id,
        canonical_device_id=request.canonical_device_id,
        owner_principal_id=PRINCIPAL,
        controller_id=CONTROLLER,
        profile_id=request.profile_id,
        profile_generation=request.profile_generation,
        device_boot_id=request.device_boot_id,
        command_type=request.command_type,
        payload_digest=request.payload_digest,
    )
    assert created is True
    return fixture.claims.begin_dispatch(claim.command_id)


def _binding() -> GenericProfileBinding:
    return GenericProfileBinding(
        owner_principal_id=PRINCIPAL,
        controller_id=CONTROLLER,
        profile_id=PROFILE,
        profile_generation=1,
        device_boot_id=BOOT,
        canonical_device_id=DEVICE,
    )


def test_accepted_fresh_after_settles_observed_without_recoverable_unknown(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)

    result = fixture.handler.one_step(
        task_id=fixture.snapshot.task_id,
        goal="enter notification settings",
        criteria=_criteria(),
    )

    assert result.outcome == "continue"
    assert result.outcome != "recoverable_unknown"
    assert result.dispatch_command_id == fixture.command_id
    assert _effect(fixture.agent_path, fixture.command_id) == ("SETTLED", "observed")
    assert fixture.semantic.calls == 1
    assert len(fixture.kernel.verifications) == 1


def test_transport_rejected_is_not_observed_and_has_no_causal_after(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, accepted=False)

    result = fixture.handler.one_step(
        task_id=fixture.snapshot.task_id,
        goal="enter notification settings",
        criteria=_criteria(),
    )

    assert result.outcome == "replan"
    assert result.semantic_satisfied is False
    assert result.dispatch_command_id == fixture.command_id
    assert _effect(fixture.agent_path, fixture.command_id) == (
        "SETTLED",
        "not_observed",
    )
    assert fixture.causality.command_for(
        fixture.snapshot.task_id,
        "2026-08-31T01:00:02+00:00",
    ) is None
    assert fixture.semantic.calls == 0
    assert fixture.kernel.verifications == []
    feedback = fixture.handler.store.recent_action_feedback(fixture.snapshot.task_id, 1)
    assert feedback[-1]["outcome"] == "rejected"
    assert feedback[-1]["reason_code"] == "executor_unicode_text_cli_unavailable"

    forged_after = _observation(
        fixture.snapshot,
        "after-observation",
        command_id=fixture.command_id,
    )
    with pytest.raises(GenericCommandConflict, match="accepted transport outcome"):
        fixture.dispatch.settle_after(
            snapshot=fixture.snapshot,
            intent=fixture.intent,
            receipt=DispatchReceipt(fixture.command_id, fixture.intent.action_intent_id),
            before=_observation(fixture.snapshot, "before-observation"),
            after=forged_after,
            primitive=PrimitiveVerification(progress=True),
        )
    assert _effect(fixture.agent_path, fixture.command_id) == (
        "SETTLED",
        "not_observed",
    )


def test_rejected_redacted_input_recovers_without_replaying_or_faking_after(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, accepted=False)
    text = "通知 設定"
    class TextActor:
        def decide(self, context, plan):
            return RoleDecision("action", AndroidUiAction("input_text", {"text": text}))
    fixture.handler.actor = TextActor()
    original_complete = fixture.handler.store.complete_rejected
    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after durable rejection")
    fixture.handler.store.complete_rejected = crash
    with pytest.raises(RuntimeError, match="simulated crash"):
        fixture.handler.one_step(task_id=fixture.snapshot.task_id, goal="enter notification settings", criteria=_criteria())
    fixture.handler.store.complete_rejected = original_complete
    fixture.handler.store = SQLiteAndroidUiStepStore(tmp_path / "android-ui-steps.db")
    fixture.handler.observations = _Observations(fixture.snapshot, fixture.causality)
    result = fixture.handler.one_step(task_id=fixture.snapshot.task_id, goal="enter notification settings", criteria=_criteria())
    assert result.outcome == "replan" and not result.semantic_satisfied
    assert len(fixture.kernel.actions) == 1
    assert fixture.semantic.calls == 0 and fixture.kernel.verifications == []
    feedback = fixture.handler.store.recent_action_feedback(fixture.snapshot.task_id, 1)
    assert feedback[-1]["action"]["action"] == "input_text"
    assert text not in str(feedback)
    next_step = fixture.handler.store.reserve_step(task_id=fixture.snapshot.task_id, revision=1, before=_observation(fixture.snapshot, "new"), snapshot=fixture.snapshot)
    assert next_step.step_index == 1


def test_uncertain_transport_is_not_closed_as_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, accepted=False)
    fixture.kernel.error = SimpleNamespace(code="executor_unicode_text_uncertain", retryable=True)
    result = fixture.handler.one_step(task_id=fixture.snapshot.task_id, goal="enter notification settings", criteria=_criteria())
    assert result.outcome == "after_evidence_unavailable"
    assert fixture.claims.get(fixture.command_id).state == "OUTCOME_UNKNOWN"
    assert fixture.dispatch.rejection_reason(fixture.snapshot, fixture.intent.action_intent_id) is None
    assert fixture.handler.store.recent_action_feedback(fixture.snapshot.task_id, 1) == ()


def test_caller_forged_after_reference_cannot_release_canonical_effect(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)
    receipt = fixture.dispatch.dispatch(fixture.snapshot, fixture.intent)
    forged_after = _observation(
        fixture.snapshot,
        "caller-forged-observation",
        command_id=fixture.command_id,
    )

    with pytest.raises(GenericCommandConflict, match="authoritative"):
        fixture.dispatch.settle_after(
            snapshot=fixture.snapshot,
            intent=fixture.intent,
            receipt=receipt,
            before=_observation(fixture.snapshot, "before-observation"),
            after=forged_after,
            primitive=PrimitiveVerification(progress=True, reason="caller supplied"),
        )

    assert _effect(fixture.agent_path, fixture.command_id) == ("ACTIVE", None)


def test_production_physical_dispatcher_rejects_direct_legacy_kernel_bypass(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)
    fake_claim = GenericCommandClaim(
        claim_id="claim-forged",
        dispatch_id="dispatch-forged",
        task_id=fixture.snapshot.task_id,
        step_id="step-forged",
        subtask_id=str(fixture.snapshot.runner_binding_id),
        action_id="command-forged",
        command_id="command-forged",
        canonical_device_id=DEVICE,
        owner_principal_id=PRINCIPAL,
        controller_id=CONTROLLER,
        profile_id=PROFILE,
        profile_generation=1,
        device_boot_id=BOOT,
        command_type="back",
        payload_digest=_digest("forged"),
        state="DISPATCHING",
        outcome=None,
        claimed_at="2030-01-01T00:00:00+00:00",
        dispatch_started_at="2030-01-01T00:00:01+00:00",
        settled_at=None,
    )

    with pytest.raises(ProductionAndroidUiError, match="physical_claim_missing"):
        fixture.physical.dispatch(
            canonical_device_id=DEVICE,
            command_type="back",
            payload={},
            command_id=fake_claim.command_id,
            binding=GenericProfileBinding(
                owner_principal_id=PRINCIPAL,
                controller_id=CONTROLLER,
                profile_id=PROFILE,
                profile_generation=1,
                device_boot_id=BOOT,
                canonical_device_id=DEVICE,
            ),
            claim=fake_claim,
        )

    assert fixture.kernel.executions == {}


def test_persisted_claim_without_canonical_dispatch_or_effect_has_zero_physical(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)
    request = _runner_request(fixture, command_id="claim-only-command")
    _install_kernel_action(fixture, request)
    claim = _claim_for_request(
        fixture, request, dispatch_id="claim-only-dispatch",
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="physical_canonical_effect_missing",
    ):
        fixture.physical.dispatch(
            canonical_device_id=DEVICE,
            command_type="back",
            payload={},
            command_id=request.command_id,
            binding=_binding(),
            claim=claim,
        )

    assert fixture.kernel.executions == {}
    assert fixture.runtime_store.runner_dispatches(fixture.snapshot.task_id) == []


@pytest.mark.parametrize("canonical_state", ["other_dispatch", "settled_effect"])
def test_claim_for_other_or_settled_canonical_effect_has_zero_physical(
    tmp_path: Path, canonical_state: str,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)
    request = _runner_request(fixture)
    committed = fixture.service.commit_runner_dispatch(
        fixture.snapshot.task_id,
        request=request,
    )
    if canonical_state == "settled_effect":
        fixture.service.settle_runner_effect(
            fixture.snapshot.task_id,
            command_id=request.command_id,
            outcome="preflight_rejected",
        )
        claim_dispatch_id = committed.dispatch_id
    else:
        claim_dispatch_id = "different-dispatch"
    _install_kernel_action(fixture, request)
    claim = _claim_for_request(
        fixture, request, dispatch_id=claim_dispatch_id,
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="physical_canonical_effect_missing",
    ):
        fixture.physical.dispatch(
            canonical_device_id=DEVICE,
            command_type="back",
            payload={},
            command_id=request.command_id,
            binding=_binding(),
            claim=claim,
        )

    assert fixture.kernel.executions == {}


def test_exact_active_canonical_effect_and_claim_dispatch_at_most_once(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)
    request = _runner_request(fixture)
    committed = fixture.service.commit_runner_dispatch(
        fixture.snapshot.task_id,
        request=request,
    )
    _install_kernel_action(fixture, request)
    claim = _claim_for_request(
        fixture, request, dispatch_id=committed.dispatch_id,
    )

    result = fixture.physical.dispatch(
        canonical_device_id=DEVICE,
        command_type="back",
        payload={},
        command_id=request.command_id,
        binding=_binding(),
        claim=claim,
    )
    assert result.accepted is True
    with pytest.raises(ValueError, match="PROPOSED"):
        fixture.physical.dispatch(
            canonical_device_id=DEVICE,
            command_type="back",
            payload={},
            command_id=request.command_id,
            binding=_binding(),
            claim=claim,
        )
    assert len(fixture.kernel.executions) == 1


@pytest.mark.parametrize(
    "drift",
    [
        "task",
        "owner_principal",
        "controller",
        "step",
        "subtask",
        "action",
        "runner_kind",
        "runner_version",
        "profile",
        "profile_generation",
        "boot",
        "device",
        "command",
        "command_type",
        "payload_digest",
        "current_binding",
    ],
)
def test_any_claim_or_canonical_dispatch_drift_has_zero_physical(
    tmp_path: Path, drift: str,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)
    request = _runner_request(fixture)
    committed = fixture.service.commit_runner_dispatch(
        fixture.snapshot.task_id,
        request=request,
    )
    _install_kernel_action(fixture, request)

    claim_values: dict[str, object] = {
        "dispatch_id": committed.dispatch_id,
        "task_id": fixture.snapshot.task_id,
        "step_id": request.step_id,
        "subtask_id": request.subtask_id,
        "action_id": request.action_id,
        "command_id": request.command_id,
        "canonical_device_id": request.canonical_device_id,
        "owner_principal_id": PRINCIPAL,
        "controller_id": CONTROLLER,
        "profile_id": request.profile_id,
        "profile_generation": request.profile_generation,
        "device_boot_id": request.device_boot_id,
        "command_type": request.command_type,
        "payload_digest": request.payload_digest,
    }
    claim_drift = {
        "task": ("task_id", "different-task"),
        "owner_principal": ("owner_principal_id", "different-principal"),
        "controller": ("controller_id", "different-controller"),
        "step": ("step_id", "different-step"),
        "subtask": ("subtask_id", "different-subtask"),
        "action": ("action_id", "different-action"),
        "profile": ("profile_id", "different-profile"),
        "profile_generation": ("profile_generation", 2),
        "boot": ("device_boot_id", "different-boot"),
        "device": ("canonical_device_id", "emulator:different-profile"),
        "command": ("command_id", "different-command"),
        "command_type": ("command_type", "home"),
        "payload_digest": ("payload_digest", _digest("different-payload")),
    }
    if drift in claim_drift:
        field, value = claim_drift[drift]
        claim_values[field] = value
        if drift == "command_type":
            claim_values["payload_digest"] = command_payload_digest("home", {})
    elif drift in {"runner_kind", "runner_version"}:
        column = drift
        value = "different-runner" if drift == "runner_kind" else "2"
        with sqlite3.connect(fixture.agent_path) as connection:
            connection.execute(
                f"UPDATE task_runner_dispatches SET {column}=? WHERE dispatch_id=?",
                (value, committed.dispatch_id),
            )
    elif drift == "current_binding":
        with sqlite3.connect(fixture.agent_path) as connection:
            connection.execute(
                "UPDATE agent_sessions SET task_current_subtask_id=NULL WHERE session_id=?",
                (fixture.snapshot.task_id,),
            )
    else:  # pragma: no cover - the parametrization is intentionally exhaustive
        raise AssertionError(f"unsupported drift: {drift}")

    raw_claim, created = fixture.claims.claim(**claim_values)
    assert created is True
    claim = fixture.claims.begin_dispatch(raw_claim.command_id)
    binding = replace(
        _binding(),
        owner_principal_id=claim.owner_principal_id,
        controller_id=claim.controller_id,
        profile_id=claim.profile_id,
        profile_generation=claim.profile_generation,
        device_boot_id=claim.device_boot_id,
        canonical_device_id=claim.canonical_device_id,
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="physical_canonical_effect_(missing|mismatch)",
    ):
        fixture.physical.dispatch(
            canonical_device_id=claim.canonical_device_id,
            command_type=claim.command_type,
            payload={},
            command_id=claim.command_id,
            binding=binding,
            claim=claim,
        )

    assert fixture.kernel.executions == {}


def test_exact_canonical_claim_rejects_kernel_action_from_another_task(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)
    request = _runner_request(fixture)
    committed = fixture.service.commit_runner_dispatch(
        fixture.snapshot.task_id,
        request=request,
    )
    fixture.kernel.actions[request.command_id] = SimpleNamespace(
        id=request.command_id,
        task_id="different-kernel-task",
        stage_id="android-ui-stage-test",
        based_on_observation_id="before-observation",
        type=ActionType.BACK,
        params={},
        proposed_by_call_id="test-intent",
        status=ActionStatus.PROPOSED,
    )
    claim = _claim_for_request(
        fixture, request, dispatch_id=committed.dispatch_id,
    )

    with pytest.raises(
        ProductionAndroidUiError,
        match="physical_action_crosswire",
    ):
        fixture.physical.dispatch(
            canonical_device_id=DEVICE,
            command_type="back",
            payload={},
            command_id=request.command_id,
            binding=_binding(),
            claim=claim,
        )

    assert fixture.kernel.executions == {}


def test_canonical_commit_without_claim_recovers_exact_command_at_most_once(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, accepted=True)
    request = _runner_request(fixture)
    committed = fixture.service.commit_runner_dispatch(
        fixture.snapshot.task_id,
        request=request,
    )
    _install_kernel_action(fixture, request)
    port = GenericKernelCommandPort(
        tasks=fixture.service,
        claims=fixture.claims,
        dispatcher=fixture.physical,
        profiles=_Profiles(),
    )

    first = port.dispatch_once(
        task_id=fixture.snapshot.task_id,
        request=request,
        payload={},
    )
    second = port.dispatch_once(
        task_id=fixture.snapshot.task_id,
        request=request,
        payload={},
    )

    assert first.commit == committed
    assert first.physically_dispatched is True
    assert second.physically_dispatched is False
    assert len(fixture.kernel.executions) == 1


def test_pause_before_commit_has_zero_physical_and_commit_before_pause_can_settle_one(
    tmp_path: Path,
) -> None:
    pause_first = _fixture(tmp_path / "pause-first", accepted=True)
    first_request = _runner_request(pause_first)
    pause_first.service.control_task(
        pause_first.snapshot.task_id,
        action="pause",
        idempotency_key="pause-first",
        expected_revision=1,
        requested_by={"source": "test"},
    )
    with pytest.raises(TaskDispatchConflict):
        GenericKernelCommandPort(
            tasks=pause_first.service,
            claims=pause_first.claims,
            dispatcher=pause_first.physical,
            profiles=_Profiles(),
        ).dispatch_once(
            task_id=pause_first.snapshot.task_id,
            request=first_request,
            payload={},
        )
    assert pause_first.kernel.executions == {}

    commit_first = _fixture(tmp_path / "commit-first", accepted=True)
    committed_request = _runner_request(commit_first)
    committed = commit_first.service.commit_runner_dispatch(
        commit_first.snapshot.task_id,
        request=committed_request,
    )
    commit_first.service.control_task(
        commit_first.snapshot.task_id,
        action="pause",
        idempotency_key="pause-after-commit",
        expected_revision=1,
        requested_by={"source": "test"},
    )
    _install_kernel_action(commit_first, committed_request)
    claim = _claim_for_request(
        commit_first,
        committed_request,
        dispatch_id=committed.dispatch_id,
    )
    commit_first.physical.dispatch(
        canonical_device_id=DEVICE,
        command_type="back",
        payload={},
        command_id=committed_request.command_id,
        binding=_binding(),
        claim=claim,
    )
    commit_first.claims.settle(
        committed_request.command_id,
        outcome="accepted",
    )
    commit_first.service.settle_runner_effect(
        commit_first.snapshot.task_id,
        command_id=committed_request.command_id,
        outcome="observed",
    )
    assert len(commit_first.kernel.executions) == 1
    assert _effect(
        commit_first.agent_path,
        committed_request.command_id,
    ) == ("SETTLED", "observed")
    with pytest.raises(TaskDispatchConflict):
        commit_first.service.commit_runner_dispatch(
            commit_first.snapshot.task_id,
            request=_runner_request(
                commit_first,
                command_id="next-command",
                step_id="next-step",
            ),
        )
    assert len(commit_first.kernel.executions) == 1
