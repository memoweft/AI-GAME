from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from ai_game_console.agent_runtime.domain import (
    RunnerDispatchRequest,
    TaskDispatchConflict,
)
from ai_game_console.agent_runtime.service import CanonicalTaskService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.emulator_runtime.general_kernel import (
    GenericDispatchResult,
    GenericKernelCommandPort,
    GenericReconciliationEvidence,
)
from ai_game_console.emulator_runtime.general_store import (
    GenericCommandConflict,
    SQLiteGenericCommandStore,
    UnresolvedEffect,
    command_payload_digest,
)


class _Dispatcher:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.raises = raises

    def dispatch(self, **kwargs: object) -> GenericDispatchResult:
        self.calls.append(dict(kwargs))
        if self.raises:
            raise TimeoutError("transport response was lost")
        return GenericDispatchResult(accepted=True, outcome="accepted")


class _ReadyProfile:
    owner_principal_id = "principal-a"
    owner_controller_id = "controller-a"
    profile_id = "profile-a"
    profile_generation = 1
    boot_id = "boot-a"
    canonical_device_id = "emulator:profile-a"


class _Profiles:
    def __init__(self, profile: object | None = None) -> None:
        self.profile = profile or _ReadyProfile()
        self.calls: list[dict[str, object]] = []

    def resolve_ready(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        return self.profile


class _EvidenceLookup:
    def __init__(
        self, *, observed_effect: bool = False,
        captured_at: str = "2030-01-01T00:00:00Z",
        persisted_at: str = "2030-01-01T00:00:00Z",
        overrides: dict[str, object] | None = None,
    ) -> None:
        self.observed_effect = observed_effect
        self.captured_at = captured_at
        self.persisted_at = persisted_at
        self.overrides = overrides or {}
        self.calls: list[dict[str, object]] = []

    def resolve_reconciliation(self, **kwargs: object) -> GenericReconciliationEvidence:
        self.calls.append(dict(kwargs))
        claim = kwargs["claim"]
        values: dict[str, object] = dict(
            task_id=claim.task_id, dispatch_id=claim.dispatch_id,
            step_id=claim.step_id, subtask_id=claim.subtask_id,
            action_id=claim.action_id, owner_principal_id=claim.owner_principal_id,
            controller_id=claim.controller_id, profile_id=claim.profile_id,
            profile_generation=claim.profile_generation, device_boot_id=claim.device_boot_id,
            canonical_device_id=claim.canonical_device_id,
            observation_id=str(kwargs["observation_id"]), evidence_refs=("artifact:authoritative-after",),
            command_id=claim.command_id, captured_at=self.captured_at,
            persisted_at=self.persisted_at,
            observed_effect=self.observed_effect,
        )
        values.update(self.overrides)
        return GenericReconciliationEvidence(**values)


def _task(tmp_path: Path) -> tuple[CanonicalTaskService, str, str]:
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    service = CanonicalTaskService(store, principal_id="principal-a", controller_id="controller-a")
    task = service.create_task(
        "Navigate to a target screen",
        f"create-{uuid4()}",
        origin={"runner_kind": "android_ui_agent", "runner_version": "1"},
    )
    task_id = task["task_id"]
    subtask = service.upsert_subtask(
        task_id,
        kind="android_ui_agent",
        object_ref="profile-a",
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
        idempotency_key=f"running-{uuid4()}",
        expected_current_status="scheduled",
    )
    return service, task_id, subtask["id"]


def _request(
    *, step: str = "step-1", action: str = "action-1", command: str = "command-1",
    profile: str = "profile-a", command_type: str = "tap", payload: dict[str, object] | None = None,
    expected_revision: int = 1,
) -> RunnerDispatchRequest:
    body = payload or {"x": 10, "y": 20}
    return RunnerDispatchRequest(
        step_id=step,
        action_id=action,
        command_id=command,
        runner_kind="android_ui_agent",
        runner_version="1",
        subtask_id="subtask-placeholder",
        profile_id=profile,
        profile_generation=1,
        device_boot_id="boot-a",
        canonical_device_id="emulator:profile-a",
        command_type=command_type,
        payload_digest=command_payload_digest(command_type, body),
        expected_revision=expected_revision,
    )


def _gateway(
    tmp_path: Path,
    service: CanonicalTaskService,
    dispatcher: _Dispatcher,
    *,
    evidence_lookup: _EvidenceLookup | None = None,
) -> GenericKernelCommandPort:
    return GenericKernelCommandPort(
        tasks=service,
        claims=SQLiteGenericCommandStore(tmp_path / "generic-claims.db"),
        dispatcher=dispatcher,
        profiles=_Profiles(),
        evidence_lookup=evidence_lookup or _EvidenceLookup(),
    )


def _bound_request(subtask_id: str, **kwargs: object) -> RunnerDispatchRequest:
    request = _request(**kwargs)
    return RunnerDispatchRequest(
        step_id=request.step_id, action_id=request.action_id, command_id=request.command_id,
        runner_kind=request.runner_kind, runner_version=request.runner_version,
        subtask_id=subtask_id, profile_id=request.profile_id,
        profile_generation=request.profile_generation, device_boot_id=request.device_boot_id,
        canonical_device_id=request.canonical_device_id, command_type=request.command_type,
        payload_digest=request.payload_digest, expected_revision=request.expected_revision,
    )


def test_claim_precedes_physical_call_and_replay_never_dispatches_twice(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher()
    gateway = _gateway(tmp_path, service, dispatcher)
    request = _bound_request(subtask_id)

    first = gateway.dispatch_once(task_id=task_id, request=request, payload={"x": 10, "y": 20})
    second = gateway.dispatch_once(task_id=task_id, request=request, payload={"x": 10, "y": 20})

    assert first.physically_dispatched is True
    assert first.claim.state == "SETTLED"
    assert second.physically_dispatched is False
    assert len(dispatcher.calls) == 1
    assert service.store.runner_dispatches(task_id)[0].request == request


@pytest.mark.parametrize("control", ["pause", "takeover", "cancel"])
def test_control_committed_before_dispatch_blocks_physical_call(tmp_path: Path, control: str) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher()
    gateway = _gateway(tmp_path, service, dispatcher)
    service.control_task(
        task_id, action=control, idempotency_key=f"{control}-{uuid4()}", expected_revision=1,
        requested_by={"source": "test"},
    )

    with pytest.raises(TaskDispatchConflict):
        gateway.dispatch_once(
            task_id=task_id, request=_bound_request(subtask_id), payload={"x": 10, "y": 20},
        )
    assert dispatcher.calls == []


def test_dispatch_commit_is_the_action_boundary_but_later_step_obeys_pause(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher()
    gateway = _gateway(tmp_path, service, dispatcher)
    first = _bound_request(subtask_id)
    service.commit_runner_dispatch(task_id, request=first)
    service.control_task(
        task_id, action="pause", idempotency_key=f"pause-{uuid4()}", expected_revision=1,
        requested_by={"source": "test"},
    )

    replay = gateway.dispatch_once(task_id=task_id, request=first, payload={"x": 10, "y": 20})
    assert replay.physically_dispatched is True
    assert len(dispatcher.calls) == 1
    second = _bound_request(subtask_id, step="step-2", action="action-2", command="command-2")
    with pytest.raises(TaskDispatchConflict):
        gateway.dispatch_once(task_id=task_id, request=second, payload={"x": 10, "y": 20})
    assert len(dispatcher.calls) == 1


def test_unknown_effect_blocks_a_new_action_and_typed_text_is_not_persisted(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher(raises=True)
    gateway = _gateway(tmp_path, service, dispatcher)
    secret = "password=unrepeatable-secret"
    first = _bound_request(
        subtask_id, command_type="input_text", payload={"text": secret},
    )
    outcome = gateway.dispatch_once(task_id=task_id, request=first, payload={"text": secret})
    assert outcome.claim.state == "OUTCOME_UNKNOWN"
    second = _bound_request(subtask_id, step="step-2", action="action-2", command="command-2")
    with pytest.raises(TaskDispatchConflict, match="unresolved committed"):
        gateway.dispatch_once(task_id=task_id, request=second, payload={"x": 10, "y": 20})
    assert len(dispatcher.calls) == 1
    assert secret not in (tmp_path / "generic-claims.db").read_bytes().decode("latin-1")


def test_unknown_effect_keeps_device_lane_and_rejects_bare_boolean_reconciliation(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    failing = _Dispatcher(raises=True)
    gateway = _gateway(tmp_path, service, failing)
    first = _bound_request(subtask_id)
    gateway.dispatch_once(task_id=task_id, request=first, payload={"x": 10, "y": 20})

    with pytest.raises(GenericCommandConflict):
        gateway.reconcile(command_id=first.command_id, reliably_happened=True)

    other = SQLiteGenericCommandStore(tmp_path / "generic-claims.db")
    with pytest.raises(GenericCommandConflict, match="writer lane"):
        other.claim(
            dispatch_id="other-dispatch", task_id="other-task", step_id="other-step",
            subtask_id="other-subtask",
            action_id="other-action", command_id="other-command",
            canonical_device_id="emulator:profile-a", command_type="tap",
            payload_digest=command_payload_digest("tap", {"x": 3, "y": 4}),
            owner_principal_id="principal-other", controller_id="controller-other",
            profile_id="profile-a", profile_generation=1, device_boot_id="boot-a",
        )
    # Caller attempts to lie about a different generation and positive effect,
    # but the injected authority returns the claim-bound fresh evidence.
    released = gateway.reconcile(
        command_id=first.command_id,
        evidence=GenericReconciliationEvidence(
            task_id=task_id, dispatch_id="forged-dispatch", step_id="forged-step",
            subtask_id="forged-subtask", action_id="forged-action",
            owner_principal_id="principal-a", controller_id="controller-a",
            profile_id="profile-a", profile_generation=2, device_boot_id="boot-a",
            canonical_device_id="emulator:profile-a", observation_id="obs-after",
            evidence_refs=("forged",), command_id="forged-command",
            captured_at="2000-01-01T00:00:00Z",
            persisted_at="2000-01-01T00:00:00Z", observed_effect=True,
        ),
    )
    assert released.state == "REPLAN_REQUIRED"


def test_same_length_typed_text_has_distinct_opaque_command_digest() -> None:
    first = command_payload_digest("input_text", {"text": "aaaa"})
    second = command_payload_digest("input_text", {"text": "bbbb"})
    assert first != second
    assert "aaaa" not in first
    assert "bbbb" not in second


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_principal_id", "principal-b"),
        ("owner_controller_id", "controller-b"),
        ("profile_id", "profile-b"),
        ("profile_generation", 2),
        ("boot_id", "boot-b"),
        ("canonical_device_id", "emulator:profile-b"),
    ],
)
def test_profile_rebound_before_physical_call_and_mismatch_has_zero_dispatch(
    tmp_path: Path, field: str, value: object,
) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher()
    drifted = _ReadyProfile()
    setattr(drifted, field, value)
    gateway = GenericKernelCommandPort(
        tasks=service, claims=SQLiteGenericCommandStore(tmp_path / "generic-claims.db"),
        dispatcher=dispatcher, profiles=_Profiles(drifted),
    )
    request = _bound_request(subtask_id)
    attempt = gateway.dispatch_once(task_id=task_id, request=request, payload={"x": 10, "y": 20})
    assert attempt.claim.state == "SETTLED"
    assert attempt.claim.outcome == "rejected"
    assert dispatcher.calls == []


def test_pending_accepted_revision_blocks_new_step_but_exact_committed_step_replays(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher()
    gateway = _gateway(tmp_path, service, dispatcher)
    first = _bound_request(subtask_id)
    service.commit_runner_dispatch(task_id, request=first)
    service.revise_task(
        task_id, base_revision=1, kind="revise", instruction="Use the updated target.",
        patch={}, idempotency_key="accepted-revision", requested_by={"source": "test"},
    )
    replay = gateway.dispatch_once(task_id=task_id, request=first, payload={"x": 10, "y": 20})
    assert replay.physically_dispatched is True
    later = _bound_request(subtask_id, step="step-2", action="action-2", command="command-2", expected_revision=2)
    with pytest.raises(TaskDispatchConflict, match="unresolved committed"):
        gateway.dispatch_once(task_id=task_id, request=later, payload={"x": 10, "y": 20})


def test_committed_without_claim_blocks_new_identity_until_exact_command_reconciles(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    first = _bound_request(subtask_id)
    service.commit_runner_dispatch(task_id, request=first)
    later = _bound_request(subtask_id, step="step-2", action="action-2", command="command-2")
    with pytest.raises(TaskDispatchConflict, match="unresolved committed"):
        service.commit_runner_dispatch(task_id, request=later)

    dispatcher = _Dispatcher()
    gateway = _gateway(tmp_path, service, dispatcher)
    recovered = gateway.dispatch_once(task_id=task_id, request=first, payload={"x": 10, "y": 20})
    assert recovered.physically_dispatched is True
    assert len(dispatcher.calls) == 1
    assert gateway.dispatch_once(task_id=task_id, request=first, payload={"x": 10, "y": 20}).physically_dispatched is False
    assert len(dispatcher.calls) == 1


@pytest.mark.parametrize("unfinished_state", ["CLAIMED", "DISPATCHING"])
def test_recovered_claim_without_receipt_becomes_unknown_and_never_replays(
    tmp_path: Path, unfinished_state: str,
) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    request = _bound_request(subtask_id)
    commit = service.commit_runner_dispatch(task_id, request=request)
    claims = SQLiteGenericCommandStore(tmp_path / "generic-claims.db")
    claim, created = claims.claim(
        dispatch_id=commit.dispatch_id,
        task_id=task_id,
        step_id=request.step_id,
        subtask_id=request.subtask_id,
        action_id=request.action_id,
        command_id=request.command_id,
        canonical_device_id=request.canonical_device_id,
        owner_principal_id="principal-a",
        controller_id="controller-a",
        profile_id=request.profile_id,
        profile_generation=request.profile_generation,
        device_boot_id=request.device_boot_id,
        command_type=request.command_type,
        payload_digest=request.payload_digest,
    )
    assert created is True
    if unfinished_state == "DISPATCHING":
        claim = claims.begin_dispatch(request.command_id)
    assert claim.state == unfinished_state

    dispatcher = _Dispatcher()
    recovered = GenericKernelCommandPort(
        tasks=service,
        claims=claims,
        dispatcher=dispatcher,
        profiles=_Profiles(),
        evidence_lookup=_EvidenceLookup(),
    ).dispatch_once(task_id=task_id, request=request, payload={"x": 10, "y": 20})

    assert recovered.physically_dispatched is False
    assert recovered.claim.state == "OUTCOME_UNKNOWN"
    assert dispatcher.calls == []
    with pytest.raises(TaskDispatchConflict, match="unresolved committed"):
        service.commit_runner_dispatch(
            task_id,
            request=_bound_request(
                subtask_id, step="step-2", action="action-2", command="command-2",
            ),
        )


@pytest.mark.parametrize(
    "crash_phase",
    [
        "after_canonical_commit",
        "after_claim",
        "after_physical",
        "after_receipt",
        "after_observation",
        "after_verification",
        "after_projection",
    ],
)
def test_crash_matrix_never_dispatches_one_stable_command_more_than_once(
    tmp_path: Path, crash_phase: str,
) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    request = _bound_request(subtask_id)
    commit = service.commit_runner_dispatch(task_id, request=request)
    claims = SQLiteGenericCommandStore(tmp_path / "generic-claims.db")
    dispatcher = _Dispatcher()

    if crash_phase != "after_canonical_commit":
        claim, created = claims.claim(
            dispatch_id=commit.dispatch_id,
            task_id=task_id,
            step_id=request.step_id,
            subtask_id=request.subtask_id,
            action_id=request.action_id,
            command_id=request.command_id,
            canonical_device_id=request.canonical_device_id,
            owner_principal_id="principal-a",
            controller_id="controller-a",
            profile_id=request.profile_id,
            profile_generation=request.profile_generation,
            device_boot_id=request.device_boot_id,
            command_type=request.command_type,
            payload_digest=request.payload_digest,
        )
        assert created is True
        if crash_phase not in {"after_claim"}:
            claims.begin_dispatch(request.command_id)
            # Model a physical call that happened immediately before the
            # selected crash window.  Recovered code must never append a
            # second call for the same stable command identity.
            dispatcher.calls.append({"command_id": request.command_id})
        if crash_phase in {
            "after_receipt", "after_observation", "after_verification", "after_projection",
        }:
            claims.settle(request.command_id, outcome="accepted")
        if crash_phase in {"after_observation", "after_verification", "after_projection"}:
            service.settle_runner_effect(
                task_id,
                command_id=request.command_id,
                outcome="observed",
            )

    recovered = GenericKernelCommandPort(
        tasks=service,
        claims=claims,
        dispatcher=dispatcher,
        profiles=_Profiles(),
        evidence_lookup=_EvidenceLookup(observed_effect=True),
    ).dispatch_once(task_id=task_id, request=request, payload={"x": 10, "y": 20})
    assert len(dispatcher.calls) <= 1
    assert recovered.commit == commit


@pytest.mark.parametrize(
    ("captured_at", "persisted_at"),
    [
        ("2000-01-01T00:00:00Z", "2030-01-01T00:00:00Z"),
        ("2030-01-01T00:00:00Z", "2000-01-01T00:00:00Z"),
    ],
)
def test_reconciliation_requires_authoritative_capture_and_persistence_after_receipt(
    tmp_path: Path, captured_at: str, persisted_at: str,
) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher(raises=True)
    lookup = _EvidenceLookup(captured_at=captured_at, persisted_at=persisted_at)
    gateway = _gateway(tmp_path, service, dispatcher, evidence_lookup=lookup)
    request = _bound_request(subtask_id)
    gateway.dispatch_once(task_id=task_id, request=request, payload={"x": 10, "y": 20})

    with pytest.raises(GenericCommandConflict, match="captured and persisted"):
        gateway.reconcile(command_id=request.command_id, observation_id="after-observation")
    with pytest.raises(TaskDispatchConflict, match="unresolved committed"):
        service.commit_runner_dispatch(
            task_id,
            request=_bound_request(
                subtask_id, step="step-2", action="action-2", command="command-2",
            ),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"dispatch_id": "foreign-dispatch"},
        {"action_id": "foreign-action"},
        {"subtask_id": "foreign-subtask"},
    ],
)
def test_authoritative_evidence_must_bind_exact_effect_action_and_current_subtask(
    tmp_path: Path, overrides: dict[str, object],
) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher(raises=True)
    lookup = _EvidenceLookup(overrides=overrides)
    gateway = _gateway(tmp_path, service, dispatcher, evidence_lookup=lookup)
    request = _bound_request(subtask_id)
    gateway.dispatch_once(task_id=task_id, request=request, payload={"x": 10, "y": 20})

    with pytest.raises(GenericCommandConflict, match="binding"):
        gateway.reconcile(command_id=request.command_id, observation_id="after-observation")
    assert service.store.runner_dispatches(task_id)[0].request == request


def test_reconciliation_rejects_when_canonical_current_subtask_has_changed(
    tmp_path: Path,
) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher(raises=True)
    gateway = _gateway(tmp_path, service, dispatcher)
    request = _bound_request(subtask_id)
    gateway.dispatch_once(task_id=task_id, request=request, payload={"x": 10, "y": 20})
    service.upsert_subtask(
        task_id,
        kind="other_work",
        object_ref=None,
        conversation_ref=None,
        status="scheduled",
        current_stage="elsewhere",
    )

    with pytest.raises(GenericCommandConflict, match="no longer matches"):
        gateway.reconcile(command_id=request.command_id, observation_id="after-observation")
    with pytest.raises(TaskDispatchConflict, match="unresolved committed"):
        service.commit_runner_dispatch(
            task_id,
            request=_bound_request(
                subtask_id, step="step-2", action="action-2", command="command-2",
            ),
        )


def test_binding_must_be_the_current_subtask_before_dispatch(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    service.upsert_subtask(
        task_id, kind="other_work", object_ref=None, conversation_ref=None,
        status="scheduled", current_stage="elsewhere",
    )
    dispatcher = _Dispatcher()
    gateway = _gateway(tmp_path, service, dispatcher)
    with pytest.raises(TaskDispatchConflict, match="binding"):
        gateway.dispatch_once(
            task_id=task_id, request=_bound_request(subtask_id), payload={"x": 10, "y": 20},
        )
    assert dispatcher.calls == []


def test_reliable_non_effect_requires_explicit_replan_boundary_before_new_step(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    failed_dispatcher = _Dispatcher(raises=True)
    gateway = _gateway(tmp_path, service, failed_dispatcher)
    first = _bound_request(subtask_id)
    gateway.dispatch_once(task_id=task_id, request=first, payload={"x": 10, "y": 20})

    boundary = gateway.reconcile(command_id=first.command_id, evidence=_evidence(task_id, first, observed_effect=False))
    assert boundary.state == "REPLAN_REQUIRED"
    assert len(failed_dispatcher.calls) == 1

    recovered_dispatcher = _Dispatcher()
    recovered = _gateway(tmp_path, service, recovered_dispatcher)
    second = _bound_request(subtask_id, step="step-2", action="action-2", command="command-2")
    attempt = recovered.dispatch_once(task_id=task_id, request=second, payload={"x": 10, "y": 20})
    assert attempt.physically_dispatched is True
    assert len(recovered_dispatcher.calls) == 1


@pytest.mark.parametrize("unsupported", ["wait", "screenshot", "capture_snapshot", "input_text_unicode"])
def test_unsupported_generic_primitives_reject_before_commit_or_physical(tmp_path: Path, unsupported: str) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher()
    gateway = _gateway(tmp_path, service, dispatcher)
    request = _bound_request(subtask_id, command_type=unsupported, payload={})

    with pytest.raises(GenericCommandConflict):
        gateway.dispatch_once(task_id=task_id, request=request, payload={})
    assert dispatcher.calls == []
    assert service.store.runner_dispatches(task_id) == []


def test_profile_crosswire_and_device_writer_conflict_have_zero_physical_dispatch(tmp_path: Path) -> None:
    service, task_id, subtask_id = _task(tmp_path)
    dispatcher = _Dispatcher()
    gateway = _gateway(tmp_path, service, dispatcher)
    wrong_profile = _bound_request(subtask_id, profile="profile-b")
    with pytest.raises(TaskDispatchConflict):
        gateway.dispatch_once(task_id=task_id, request=wrong_profile, payload={"x": 10, "y": 20})

    claims = SQLiteGenericCommandStore(tmp_path / "writer.db")
    claims.claim(
        dispatch_id="dispatch-a", task_id="task-a", step_id="step-a", action_id="action-a",
        subtask_id="subtask-a",
        command_id="command-a", canonical_device_id="emulator:profile-a", command_type="tap",
        payload_digest=command_payload_digest("tap", {"x": 1, "y": 1}),
        owner_principal_id="principal-a", controller_id="controller-a", profile_id="profile-a",
        profile_generation=1, device_boot_id="boot-a",
    )
    with pytest.raises(GenericCommandConflict):
        claims.claim(
            dispatch_id="dispatch-b", task_id="task-b", step_id="step-b", action_id="action-b",
            subtask_id="subtask-b",
            command_id="command-b", canonical_device_id="emulator:profile-a", command_type="tap",
            payload_digest=command_payload_digest("tap", {"x": 2, "y": 2}),
            owner_principal_id="principal-b", controller_id="controller-b", profile_id="profile-b",
            profile_generation=1, device_boot_id="boot-b",
        )
    assert dispatcher.calls == []


def test_concurrent_pause_and_dispatch_are_serialized_at_canonical_transaction(tmp_path: Path) -> None:
    """The two writers may race, but never observe an impossible half-state."""

    service, task_id, subtask_id = _task(tmp_path)
    request = _bound_request(subtask_id)
    gate = Barrier(2)

    def commit() -> str:
        gate.wait()
        try:
            service.commit_runner_dispatch(task_id, request=request)
            return "committed"
        except TaskDispatchConflict:
            return "blocked"

    def pause() -> str:
        gate.wait()
        service.control_task(
            task_id, action="pause", idempotency_key=f"race-pause-{uuid4()}",
            expected_revision=1, requested_by={"source": "race"},
        )
        return "paused"

    with ThreadPoolExecutor(max_workers=2) as executor:
        dispatch_future = executor.submit(commit)
        pause_future = executor.submit(pause)
        results = {dispatch_future.result(), pause_future.result()}

    assert "paused" in results
    assert results.intersection({"committed", "blocked"})
    task = service.inspect_task(task_id)
    if "committed" in results:
        assert len(service.store.runner_dispatches(task_id)) == 1
    else:
        assert service.store.runner_dispatches(task_id) == []
    assert task["status"] == "paused"


def _evidence(task_id: str, request: RunnerDispatchRequest, *, observed_effect: bool) -> GenericReconciliationEvidence:
    return GenericReconciliationEvidence(
        task_id=task_id, dispatch_id="caller-dispatch", step_id="caller-step",
        subtask_id="caller-subtask", action_id="caller-action",
        owner_principal_id="principal-a", controller_id="controller-a",
        profile_id=request.profile_id, profile_generation=request.profile_generation,
        device_boot_id=request.device_boot_id, canonical_device_id=request.canonical_device_id,
        observation_id="observation-after", evidence_refs=("artifact:after",),
        command_id=request.command_id, captured_at="2030-01-01T00:00:00Z",
        persisted_at="2030-01-01T00:00:00Z",
        observed_effect=observed_effect,
    )
