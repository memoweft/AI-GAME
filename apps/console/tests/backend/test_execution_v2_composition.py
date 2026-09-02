from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.config import Settings
from ai_game_console.agent_runtime.service import CanonicalTaskService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.discovery import AdbDevice, AdbDiscoveryResult
from ai_game_console.domain import TargetStatus
from ai_game_console.emulator_runtime import (
    EmulatorFingerprint,
    EmulatorProbe,
    EmulatorProfileService,
    EmulatorProfileState,
    EmulatorProfileStore,
)
from ai_game_console.emulator_runtime.task_runner import ResidentV2TaskScheduler
from ai_game_console.execution_contract.service import (
    ExecutionContractError,
    V2ExecutionContractService,
)
from ai_game_console.execution_contract.api import (
    create_execution_router,
    create_execution_v2_router,
    execution_contract_error_handler,
)
from ai_game_console.execution_contract.store import SQLiteExecutionContractStore
from ai_game_console.execution_v2_composition import (
    CanonicalTaskPortAdapter,
    EmulatorProfilePortAdapter,
    VerifiedFrameBinding,
    VerifiedFramePort,
)
from ai_game_console.runtime_adapters.artifacts import FilesystemArtifactStore
from ai_game_console.runtime_kernel.observation import ArtifactRef
from ai_game_console.user_fact_runtime import NeedUserFactStatus


OWNER = {"principal_id": "principal-a", "controller_id": "controller-a"}
OTHER_PRINCIPAL = {"principal_id": "principal-b", "controller_id": "controller-a"}
OTHER_CONTROLLER = {"principal_id": "principal-a", "controller_id": "controller-b"}
NOW = "2026-08-30T00:00:00+00:00"


def _origin(session: str, call: str) -> dict:
    return {
        "dsh_session_id": session, "dsh_turn_id": 1,
        "tool_call_id": call, "root_call_id": call,
    }


def _create(session: str = "dsh-a", call: str = "call-a") -> dict:
    return {
        "origin": _origin(session, call),
        "goal": {"summary": "Open and verify Android Settings"},
        "client_request_id": call, "idempotency_key": call,
        "priority": 60, "authorization_mode": "full-access",
    }


@dataclass
class _Need:
    id: str
    session_id: str
    status: NeedUserFactStatus = NeedUserFactStatus.OPEN
    question: str = "Choose the account"
    why_needed: str = "The task cannot infer it"


class _NeedStore:
    def __init__(self) -> None:
        self.need: _Need | None = None

    def get_need(self, need_id: str) -> _Need:
        if self.need is None or self.need.id != need_id:
            raise KeyError(need_id)
        return self.need

    def list_needs(self, *, session_id: str, status: NeedUserFactStatus):
        return [self.need] if self.need and self.need.session_id == session_id and self.need.status is status else []


class _Questions:
    def __init__(self) -> None:
        self.facts = SimpleNamespace(store=_NeedStore())
        self.answers: list[tuple[str, object]] = []

    def answer(self, *, need_id: str, value: object, **_: object) -> None:
        need = self.facts.store.get_need(need_id)
        need.status = NeedUserFactStatus.ANSWERED
        self.answers.append((need_id, value))


def _composition(tmp_path):
    runtime_store = SQLiteAgentRuntimeStore(tmp_path / "agent.sqlite")
    execution_store = SQLiteExecutionContractStore(tmp_path / "execution.sqlite")
    questions = _Questions()
    tasks = CanonicalTaskPortAdapter(
        runtime_store=runtime_store, execution_store=execution_store,
        fact_questions=questions,
    )
    return (
        V2ExecutionContractService(execution_store, tasks),
        runtime_store, execution_store, questions,
    )


def test_create_app_includes_v2_and_injects_scheduler_into_the_single_event_pump(tmp_path) -> None:
    data = tmp_path / "data"
    settings = Settings(
        project_root=tmp_path,
        data_dir=data,
        database_path=data / "console.sqlite",
        frontend_dist=tmp_path / "missing-dist",
        runtime_mode="legacy",
        harness_api_token="test-harness-token",
    )
    scheduler = SimpleNamespace(poll_once=lambda: 0)
    app = create_app(
        settings=settings,
        adb_discovery=SimpleNamespace(discover=lambda: AdbDiscoveryResult("not_configured", None, "none", (), ())),
        long_task_scheduler=scheduler,
    )
    paths = {
        route.path
        for item in app.routes
        for route in (
            getattr(getattr(item, "original_router", None), "routes", ())
            if not hasattr(item, "path") else (item,)
        )
        if hasattr(route, "path")
    }
    assert "/api/execution/v1/health" in paths
    assert "/api/execution/v2/health" in paths
    assert "/api/execution/v2/device-profiles/discovery" in paths
    assert "/api/execution/v2/tasks/{task_id}/frame" in paths
    assert app.state.agent_runtime_event_pump.long_task_scheduler is scheduler

    auto_settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "auto-data",
        database_path=tmp_path / "auto-data" / "console.sqlite",
        frontend_dist=tmp_path / "missing-dist",
        runtime_mode="legacy",
        harness_api_token="test-harness-token",
    )
    auto = create_app(
        settings=auto_settings,
        adb_discovery=SimpleNamespace(
            discover=lambda: (_ for _ in ()).throw(AssertionError("app construction must not discover ADB")),
        ),
    )
    assert isinstance(
        auto.state.agent_runtime_event_pump.long_task_scheduler,
        ResidentV2TaskScheduler,
    )
    assert auto.state.agent_runtime_event_pump.is_running is False

    disabled_settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "disabled-data",
        database_path=tmp_path / "disabled-data" / "console.sqlite",
        frontend_dist=tmp_path / "missing-dist",
        runtime_mode="legacy",
        harness_api_token="test-harness-token",
    )
    disabled = create_app(
        settings=disabled_settings,
        adb_discovery=SimpleNamespace(
            discover=lambda: AdbDiscoveryResult("not_configured", None, "none", (), ()),
        ),
        long_task_scheduler=None,
    )
    assert disabled.state.agent_runtime_event_pump.long_task_scheduler is None


def test_execution_v1_is_authenticated_read_only_compatibility() -> None:
    app = FastAPI()
    app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)
    app.include_router(
        create_execution_router(
            SimpleNamespace(
                health=lambda: {
                    "status": "ready",
                    "legacy_read_only": True,
                    "capabilities": {"submit": False},
                }
            ),
            token="test-token",
        )
    )
    headers = {
        "X-AI-Game-Client": "weftmate-harness-v1",
        "Authorization": "Bearer test-token",
    }
    request = {
        "identity": _origin("legacy-session", "legacy-call"),
        "goal": {"summary": "historical request"},
        "client_request_id": "legacy-request",
        "idempotency_key": "legacy-request",
        "authorization_mode": "full-access",
    }

    with TestClient(app) as client:
        health = client.get("/api/execution/v1/health", headers=headers)
        assert health.status_code == 200
        assert health.json()["legacy_read_only"] is True
        response = client.post(
            "/api/execution/v1/executions:submit", headers=headers, json=request
        )

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "EXECUTION_V1_READ_ONLY"


def test_canonical_composition_has_one_task_truth_and_complete_owner_pair(tmp_path) -> None:
    service, runtime_store, execution_store, _ = _composition(tmp_path)
    created = service.create_task(_create(), auth_context=OWNER)
    task_id = created["task_id"]

    assert task_id == runtime_store.get_session(task_id).id
    assert len(runtime_store.list_tasks(include_archived=True)) == 1
    assert execution_store.v2_alias_for_task(task_id)["task_id"] == task_id
    assert created["goal"]["summary"] == "Open and verify Android Settings"
    assert created["error"] is None
    assert created["progress"]["kind"] == "unknown"
    assert created["timestamps"]["created_at"]
    assert "principal_id" not in repr(created)
    assert "controller_id" not in repr(created)

    # A second DSH session is provenance only; complete owner pair remains global.
    assert service.get_task(task_id, _origin("dsh-b", "call-b"), auth_context=OWNER)["task_id"] == task_id
    assert service.list_tasks(None, auth_context=OWNER, status=None, cursor=None, limit=20)["items"][0]["task_id"] == task_id
    for foreign in (OTHER_PRINCIPAL, OTHER_CONTROLLER):
        with pytest.raises(ExecutionContractError, match="TASK_NOT_FOUND"):
            service.get_task(task_id, None, auth_context=foreign)
        assert service.list_tasks(None, auth_context=foreign, status=None, cursor=None, limit=20)["items"] == []


def test_composed_submit_fence_reuses_one_canonical_task_across_tool_call_retries(tmp_path) -> None:
    service, runtime_store, execution_store, _ = _composition(tmp_path)
    first_request = _create("dsh-a", "call-first")
    first_request["client_request_id"] = "stable-submit-turn-1"
    first_request["idempotency_key"] = "stable-submit-turn-1"
    first = service.create_task(first_request, auth_context=OWNER)

    retry_request = _create("dsh-a", "call-retry")
    retry_request["client_request_id"] = "stable-submit-turn-1"
    retry_request["idempotency_key"] = "stable-submit-turn-1"
    retry_request["goal"]["summary"] = "Open Android Settings"
    retry = service.create_task(retry_request, auth_context=OWNER)

    assert retry["task_id"] == first["task_id"]
    assert retry["replayed"] is True
    assert len(runtime_store.list_tasks(include_archived=True)) == 1
    attempts = execution_store.v2_attempts_for_submission("stable-submit-turn-1")
    assert [item["tool_call_id"] for item in attempts] == ["call-first", "call-retry"]
    assert {item["task_id"] for item in attempts} == {first["task_id"]}
    assert [item["task_id"] for item in service.list_tasks(
        None, auth_context=OWNER, status=None, cursor=None, limit=20
    )["items"]] == [first["task_id"]]

    new_turn = _create("dsh-a", "call-new-turn")
    new_turn["origin"]["dsh_turn_id"] = 2
    new_turn["client_request_id"] = "stable-submit-turn-2"
    new_turn["idempotency_key"] = "stable-submit-turn-2"
    distinct = service.create_task(new_turn, auth_context=OWNER)
    assert distinct["task_id"] != first["task_id"]
    assert len(runtime_store.list_tasks(include_archived=True)) == 2


def test_general_runner_null_reservation_recovers_first_envelope_then_admits_once(tmp_path) -> None:
    """A response drop after canonical commit must not make a second Task.

    This fixture intentionally has no scheduler, wake or action port.  The
    admission fact is the only K0 output, so the partial saga cannot cause
    Profile I/O, a wake claim, or physical work by itself.
    """
    profile_store = EmulatorProfileStore(tmp_path / "profiles.sqlite")
    profiles = EmulatorProfileService(
        store=profile_store,
        probe=lambda serial: EmulatorProbe(
            serial, EmulatorProfileState.READY, "boot-a",
            EmulatorFingerprint(True, 35, "x86_64", "1080x2400"), (),
        ), clock=lambda: NOW,
    )
    profile = profiles.save_selected(
        **OWNER, candidate=_adb("emulator-5554", "sdk_gphone64_x86_64"),
        display_name="general runner emulator", is_default=True,
    )
    runtime_store = SQLiteAgentRuntimeStore(tmp_path / "agent.sqlite")
    execution_store = SQLiteExecutionContractStore(tmp_path / "execution.sqlite")
    delegate = CanonicalTaskPortAdapter(
        runtime_store=runtime_store, execution_store=execution_store, profiles=profiles,
    )

    class DropAfterCanonicalCreate:
        def __init__(self) -> None:
            self.drop_once = True
            self.create_calls = 0

        def create(self, request: dict) -> dict:
            self.create_calls += 1
            result = delegate.create(request)
            if self.drop_once:
                self.drop_once = False
                raise TimeoutError("response dropped after canonical commit")
            return result

        def __getattr__(self, name: str):
            return getattr(delegate, name)

    tasks = DropAfterCanonicalCreate()
    service = V2ExecutionContractService(execution_store, tasks)
    first = _create("dsh-a", "call-first")
    first.update({
        "client_request_id": "stable-general-submit",
        "idempotency_key": "stable-general-submit",
        "device_profile_id": profile.profile_id,
        "runner_kind": "android_ui_agent",
    })
    with pytest.raises(ExecutionContractError, match="TASK_PORT_UNAVAILABLE"):
        service.create_task(first, auth_context=OWNER)

    [raw] = runtime_store.list_tasks(include_archived=True)
    task_id = raw.id
    pending = execution_store.v2_submission(
        OWNER["principal_id"], OWNER["controller_id"], "stable-general-submit",
    )
    assert pending is not None and pending["task_id"] is None
    assert execution_store.v2_alias_for_task(task_id) is None
    assert service.runner_admission(
        task_id, principal_id=OWNER["principal_id"], controller_id=OWNER["controller_id"],
    ) is None

    retry = _create("dsh-a", "call-retry")
    retry.update({
        "client_request_id": "stable-general-submit",
        "idempotency_key": "stable-general-submit",
        "device_profile_id": profile.profile_id,
        "runner_kind": "android_ui_agent",
    })
    retry["goal"]["summary"] = "Open the Android interface and continue the task"
    replay = service.create_task(retry, auth_context=OWNER)
    assert replay["task_id"] == task_id
    assert replay["replayed"] is True
    assert len(runtime_store.list_tasks(include_archived=True)) == 1
    assert tasks.create_calls == 2
    assert runtime_store.get_session(task_id).original_instruction == "Open and verify Android Settings"
    raw_task = runtime_store.get_task(task_id)
    assert raw_task is not None
    assert raw_task.origin["runner_kind"] == "android_ui_agent"
    assert raw_task.origin["runner_version"] == "1"
    assert [item["kind"] for item in CanonicalTaskService(
        runtime_store, **OWNER
    ).inspect_task(task_id)["subtasks"]] == ["android_ui_agent"]

    admission = service.runner_admission(
        task_id, principal_id=OWNER["principal_id"], controller_id=OWNER["controller_id"],
    )
    assert admission == {
        "task_id": task_id, "principal_id": OWNER["principal_id"],
        "controller_id": OWNER["controller_id"], "submission_key": "stable-general-submit",
        "request_hash": admission["request_hash"], "envelope_hash": admission["envelope_hash"],
        "runner_kind": "android_ui_agent", "runner_version": "1",
        "device_profile_id": profile.profile_id,
    }
    assert service.runner_admission(task_id, **OTHER_PRINCIPAL) is None
    assert service.runner_admission(task_id, **OTHER_CONTROLLER) is None
    encoded = repr(replay) + repr(service.get_task(task_id, None, auth_context=OWNER))
    for internal in ("runner_kind", "runner_version", "canonical_envelope", "principal_id", "controller_id"):
        assert internal not in encoded
    # A persisted runner-marker disagreement is dormant rather than an
    # integrity mutation: later scheduler code receives no admission fact and
    # therefore has no authority to touch Profile/wake/action ports.
    with execution_store.transaction() as connection:
        connection.execute(
            "UPDATE execution_v2_submission_scopes SET runner_version = ? "
            "WHERE task_id = ?",
            ("2", task_id),
        )
    assert service.runner_admission(task_id, **OWNER) is None


def test_general_runner_is_not_admitted_between_canonical_create_and_alias_bind(tmp_path) -> None:
    profile_store = EmulatorProfileStore(tmp_path / "profiles.sqlite")
    profiles = EmulatorProfileService(
        store=profile_store,
        probe=lambda serial: EmulatorProbe(
            serial, EmulatorProfileState.READY, "boot-a",
            EmulatorFingerprint(True, 35, "x86_64", "1080x2400"), (),
        ), clock=lambda: NOW,
    )
    profile = profiles.save_selected(
        **OWNER, candidate=_adb("emulator-5554", "sdk_gphone64_x86_64"),
        display_name="admission test emulator", is_default=True,
    )
    runtime_store = SQLiteAgentRuntimeStore(tmp_path / "agent.sqlite")
    execution_store = SQLiteExecutionContractStore(tmp_path / "execution.sqlite")
    service = V2ExecutionContractService(execution_store, CanonicalTaskPortAdapter(
        runtime_store=runtime_store, execution_store=execution_store, profiles=profiles,
    ))
    request = _create("dsh-a", "alias-gap-first")
    request.update({
        "client_request_id": "alias-gap", "idempotency_key": "alias-gap",
        "runner_kind": "android_ui_agent", "device_profile_id": profile.profile_id,
    })
    real_reserve = execution_store.reserve_v2_alias
    dropped = True

    def drop_before_alias(record: dict):
        nonlocal dropped
        if dropped:
            dropped = False
            raise RuntimeError("response lost before alias write")
        return real_reserve(record)

    execution_store.reserve_v2_alias = drop_before_alias  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before alias"):
        service.create_task(request, auth_context=OWNER)
    [raw] = runtime_store.list_tasks(include_archived=True)
    task_id = raw.id
    bound = execution_store.v2_submission(OWNER["principal_id"], OWNER["controller_id"], "alias-gap")
    assert bound is not None and bound["task_id"] == task_id
    assert execution_store.v2_alias_for_task(task_id) is None
    assert service.runner_admission(task_id, **OWNER) is None

    # No scheduler exists in this fixture: a completed alias is the earliest
    # point at which later packages may even consider Profile/wake/action work.
    execution_store.reserve_v2_alias = real_reserve  # type: ignore[method-assign]
    retry = _create("dsh-a", "alias-gap-retry")
    retry.update({
        "client_request_id": "alias-gap", "idempotency_key": "alias-gap",
        "runner_kind": "android_ui_agent", "device_profile_id": profile.profile_id,
    })
    result = service.create_task(retry, auth_context=OWNER)
    assert result["task_id"] == task_id
    assert service.runner_admission(task_id, **OWNER)["runner_kind"] == "android_ui_agent"
    assert len(runtime_store.list_tasks(include_archived=True)) == 1


def test_runner_admission_rejects_missing_alias_or_inconsistent_immutable_facts(tmp_path) -> None:
    service, runtime_store, execution_store, _ = _composition(tmp_path)
    created = service.create_task(_create(), auth_context=OWNER)
    task_id = created["task_id"]
    # A generic historical V2 Task has no resolved runner and stays inert.
    assert service.runner_admission(task_id, **OWNER) is None
    submission = execution_store.v2_submission(
        OWNER["principal_id"], OWNER["controller_id"], "call-a",
    )
    assert submission is not None
    # No production rows are mutated: this isolated temp SQLite assertion only
    # proves a mismatched persisted alias cannot become scheduler-admissible.
    with execution_store.transaction() as connection:
        connection.execute(
            "UPDATE execution_v2_aliases SET envelope_hash = ? WHERE task_id = ?",
            ("wrong-envelope", task_id),
        )
    assert service.runner_admission(task_id, **OWNER) is None
    assert len(runtime_store.list_tasks(include_archived=True)) == 1


def test_owner_global_reads_do_not_require_a_dsh_origin_query(tmp_path) -> None:
    service, _, _, _ = _composition(tmp_path)
    app = FastAPI()
    app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)
    app.include_router(create_execution_v2_router(
        service, token="test-token", capability_context=OWNER,
    ))
    headers = {
        "X-AI-Game-Client": "weftmate-harness-v1",
        "Authorization": "Bearer test-token",
        "X-AI-Game-Principal-Id": OWNER["principal_id"],
        "X-AI-Game-Controller-Id": OWNER["controller_id"],
    }
    with TestClient(app) as client:
        created = client.post("/api/execution/v2/tasks", headers=headers, json=_create())
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        assert client.get("/api/execution/v2/tasks", headers=headers).status_code == 200
        assert client.get(f"/api/execution/v2/tasks/{task_id}", headers=headers).status_code == 200


def test_token_rotation_keeps_the_authenticated_owner_pair_and_canonical_task(tmp_path) -> None:
    service, runtime_store, _, _ = _composition(tmp_path)

    def app_for(token: str) -> FastAPI:
        app = FastAPI()
        app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)
        app.include_router(create_execution_v2_router(service, token=token))
        return app

    def headers(token: str, owner: dict[str, str]) -> dict[str, str]:
        return {
            "X-AI-Game-Client": "weftmate-harness-v1",
            "Authorization": f"Bearer {token}",
            "X-AI-Game-Principal-Id": owner["principal_id"],
            "X-AI-Game-Controller-Id": owner["controller_id"],
        }

    token_a = "a" * 32
    token_b = "b" * 32
    with TestClient(app_for(token_a)) as client_a:
        created = client_a.post(
            "/api/execution/v2/tasks",
            headers=headers(token_a, OWNER),
            json=_create("dsh-before-rotation", "create-before-rotation"),
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]

    with TestClient(app_for(token_b)) as client_b:
        owner_headers = headers(token_b, OWNER)
        listed = client_b.get("/api/execution/v2/tasks", headers=owner_headers)
        assert listed.status_code == 200
        assert [item["task_id"] for item in listed.json()["items"]] == [task_id]
        assert client_b.get(f"/api/execution/v2/tasks/{task_id}", headers=owner_headers).status_code == 200
        controlled = client_b.post(
            f"/api/execution/v2/tasks/{task_id}/controls",
            headers=owner_headers,
            json={
                "origin": _origin("dsh-after-rotation", "pause-after-rotation"),
                "control_id": "pause-after-rotation",
                "idempotency_key": "pause-after-rotation",
                "expected_revision": 1,
                "action": "pause",
                "authorization_mode": "allowed-once",
            },
        )
        assert controlled.status_code == 202
        assert controlled.json()["status"] == "paused"

        for foreign in (OTHER_PRINCIPAL, OTHER_CONTROLLER):
            foreign_headers = headers(token_b, foreign)
            assert client_b.get("/api/execution/v2/tasks", headers=foreign_headers).json()["items"] == []
            assert client_b.get(
                f"/api/execution/v2/tasks/{task_id}", headers=foreign_headers
            ).status_code == 404

    assert len(runtime_store.list_tasks(include_archived=True)) == 1


def test_restart_revise_control_answer_events_and_archive_use_canonical_history(tmp_path) -> None:
    service, runtime_store, execution_store, questions = _composition(tmp_path)
    task_id = service.create_task(_create(), auth_context=OWNER)["task_id"]
    revision = service.revise(task_id, {
        "origin": _origin("dsh-b", "rev"), "revision_id": "rev-1",
        "idempotency_key": "rev-1", "base_revision": 1, "kind": "revise",
        "instruction": "Use the selected default emulator", "authorization_mode": "allowed-once",
    }, auth_context=OWNER)
    assert revision["current_revision"] == 2
    paused = service.control(task_id, {
        "origin": _origin("dsh-c", "pause"), "control_id": "pause-1",
        "idempotency_key": "pause-1", "expected_revision": 2,
        "action": "pause", "authorization_mode": "allowed-once",
    }, auth_context=OWNER)
    assert paused["status"] == "paused"

    questions.facts.store.need = _Need("need-1", task_id)
    assert service.get_task(task_id, None, auth_context=OWNER)["pending_question"]["question_id"] == "need-1"
    service.answer(task_id, {
        "origin": _origin("dsh-d", "answer"), "question_id": "need-1",
        "idempotency_key": "answer-1", "value": "account-a", "authorization_mode": "allowed-once",
    }, auth_context=OWNER)
    assert questions.answers == [("need-1", "account-a")]
    with pytest.raises(ExecutionContractError, match="TASK_QUESTION_NOT_FOUND"):
        service.answer(task_id, {
            "origin": _origin("dsh-d", "answer-foreign"), "question_id": "need-foreign",
            "idempotency_key": "answer-foreign", "value": "x", "authorization_mode": "allowed-once",
        }, auth_context=OWNER)

    canonical = CanonicalTaskService(runtime_store, **OWNER)
    service.control(task_id, {
        "origin": _origin("dsh-e", "resume"), "control_id": "resume-1",
        "idempotency_key": "resume-1", "expected_revision": 2,
        "action": "resume", "authorization_mode": "allowed-once",
    }, auth_context=OWNER)
    canonical.transition_task(
        task_id, status="succeeded", reason_code="settings_verified",
        summary="Settings foreground verified", recoverable=False,
        idempotency_key="complete-1",
    )
    archived = service.archive(task_id, {
        "origin": _origin("dsh-e", "archive"), "idempotency_key": "archive-1",
        "authorization_mode": "allowed-once",
    }, auth_context=OWNER)
    assert archived["archived"] is True
    events = service.events(task_id, None, auth_context=OWNER, after=0, limit=100)
    assert events["items"]
    assert all(event["task_id"] == task_id for event in events["items"])

    # A process-level service reconstruction reads the same two canonical DBs.
    restarted = V2ExecutionContractService(
        SQLiteExecutionContractStore(tmp_path / "execution.sqlite"),
        CanonicalTaskPortAdapter(
            runtime_store=SQLiteAgentRuntimeStore(tmp_path / "agent.sqlite"),
            execution_store=SQLiteExecutionContractStore(tmp_path / "execution.sqlite"),
            fact_questions=questions,
        ),
    )
    assert restarted.get_task(task_id, None, auth_context=OWNER)["archived"] is True
    assert len(runtime_store.list_tasks(include_archived=True)) == 1


def _adb(serial: str, model: str) -> AdbDevice:
    return AdbDevice(serial, "device", TargetStatus.READY, {"model": model})


def test_discovery_is_emulator_only_opaque_and_save_rediscovers(tmp_path) -> None:
    calls = 0
    devices = [_adb("R58M123", "Pixel_9"), _adb("emulator-5554", "sdk_gphone64_x86_64")]

    def discover():
        nonlocal calls
        calls += 1
        return AdbDiscoveryResult("ready", "C:/internal/adb.exe", "ok", tuple(devices), ())

    probe = lambda serial: EmulatorProbe(
        serial, EmulatorProfileState.READY, "boot-a",
        EmulatorFingerprint(True, 35, "x86_64", "1080x2400", 440),
        ("screen", "open_app"),
    )
    profile_store = EmulatorProfileStore(tmp_path / "profiles.sqlite")
    profiles = EmulatorProfileService(store=profile_store, probe=probe, clock=lambda: NOW)
    port = EmulatorProfilePortAdapter(
        profiles=profiles, store=profile_store, discover=discover, clock=lambda: NOW
    )
    found = port.discover_emulators(OWNER)
    assert len(found["items"]) == 1
    assert "emulator-5554" not in repr(found)
    assert "adb.exe" not in repr(found)
    saved = port.create({**OWNER, "profile": {
        "candidate_id": found["items"][0]["candidate_id"],
        "display_name": "Daily emulator", "is_default": True,
    }})
    assert calls == 2
    assert saved["device_profile_id"].startswith("profile_")
    assert "serial" not in repr(saved).lower()
    renamed = port.update(saved["device_profile_id"], {**OWNER, "profile": {
        "action": "rename", "display_name": "Renamed emulator", "expected_revision": 1,
    }})
    assert renamed["display_name"] == "Renamed emulator"
    assert port.verify(saved["device_profile_id"], OWNER)["state"] == "ready"
    assert port.update(saved["device_profile_id"], {**OWNER, "profile": {
        "action": "set_default",
    }})["is_default"] is True
    assert port.list({**OWNER, "limit": 20})["profiles"][0]["device_profile_id"] == saved["device_profile_id"]
    assert port.list({**OTHER_CONTROLLER, "limit": 20})["profiles"] == []
    assert port.update(saved["device_profile_id"], {**OWNER, "profile": {
        "action": "disable",
    }})["state"] == "disabled"


def test_discovery_rejects_changed_candidate_on_save(tmp_path) -> None:
    devices = [_adb("emulator-5554", "sdk_gphone64_x86_64")]
    profile_store = EmulatorProfileStore(tmp_path / "profiles.sqlite")
    profiles = EmulatorProfileService(
        store=profile_store,
        probe=lambda serial: EmulatorProbe(
            serial, EmulatorProfileState.READY, "boot-a",
            EmulatorFingerprint(True, 35, "x86_64", "1080x2400"), (),
        ),
        clock=lambda: NOW,
    )
    port = EmulatorProfilePortAdapter(
        profiles=profiles, store=profile_store, clock=lambda: NOW,
        discover=lambda: AdbDiscoveryResult("ready", None, "ok", tuple(devices), ()),
    )
    candidate = port.discover_emulators(OWNER)["items"][0]
    devices[:] = [_adb("emulator-5556", "sdk_gphone64_x86_64")]
    with pytest.raises(ExecutionContractError, match="EMULATOR_CANDIDATE_CHANGED"):
        port.create({**OWNER, "profile": {
            "candidate_id": candidate["candidate_id"], "display_name": "changed",
        }})


def test_selected_profile_is_a_canonical_subtask_binding_not_shadow_task_state(tmp_path) -> None:
    profile_store = EmulatorProfileStore(tmp_path / "profiles.sqlite")
    profiles = EmulatorProfileService(
        store=profile_store,
        probe=lambda serial: EmulatorProbe(
            serial, EmulatorProfileState.READY, "boot-a",
            EmulatorFingerprint(True, 35, "x86_64", "1080x2400"), ("screen", "open_app"),
        ), clock=lambda: NOW,
    )
    profile = profiles.save_selected(
        **OWNER, candidate=_adb("emulator-5554", "sdk_gphone64_x86_64"),
        display_name="Default emulator", is_default=True,
    )
    runtime_store = SQLiteAgentRuntimeStore(tmp_path / "agent.sqlite")
    execution_store = SQLiteExecutionContractStore(tmp_path / "execution.sqlite")
    service = V2ExecutionContractService(
        execution_store,
        CanonicalTaskPortAdapter(
            runtime_store=runtime_store, execution_store=execution_store, profiles=profiles,
        ),
    )
    request = _create()
    request["device_profile_id"] = profile.profile_id
    request["runner_kind"] = "android_ui_agent"
    created = service.create_task(request, auth_context=OWNER)
    raw_task = runtime_store.get_task(created["task_id"])
    assert raw_task.origin["runner_kind"] == "android_ui_agent"
    assert raw_task.origin["runner_version"] == "1"
    assert "runner_kind" not in repr(created)
    assert "runner_version" not in repr(created)
    assert created["device"] == {
        "profile_id": profile.profile_id, "display_name": "Default emulator", "state": "ready",
    }
    assert created["subtasks"][0]["object_ref"] == profile.profile_id
    assert len(runtime_store.list_tasks(include_archived=True)) == 1

    generic = _create("dsh-generic", "call-generic")
    generic["device_profile_id"] = profile.profile_id
    generic_created = service.create_task(generic, auth_context=OWNER)
    generic_raw = runtime_store.get_task(generic_created["task_id"])
    assert "runner_kind" not in generic_raw.origin
    assert CanonicalTaskService(
        runtime_store,
        principal_id=OWNER["principal_id"],
        controller_id=OWNER["controller_id"],
    ).inspect_task(generic_created["task_id"])["subtasks"] == []
    assert generic_created["device"] is None


def test_runner_validation_happens_before_submission_reserve_or_canonical_create(tmp_path) -> None:
    service, runtime_store, execution_store, _ = _composition(tmp_path)
    unknown = _create("dsh-unknown", "call-unknown")
    unknown["runner_kind"] = "soul_chat_v1"
    with pytest.raises(ExecutionContractError, match="CAPABILITY_UNAVAILABLE"):
        service.create_task(unknown, auth_context=OWNER)
    assert runtime_store.list_tasks(include_archived=True) == []
    assert execution_store.v2_submission(
        OWNER["principal_id"], OWNER["controller_id"], unknown["idempotency_key"]
    ) is None

    missing_profile = _create("dsh-missing-profile", "call-missing-profile")
    missing_profile["runner_kind"] = "android_ui_agent"
    with pytest.raises(ExecutionContractError, match="DEVICE_PROFILE_REQUIRED"):
        service.create_task(missing_profile, auth_context=OWNER)
    assert runtime_store.list_tasks(include_archived=True) == []
    assert execution_store.v2_submission(
        OWNER["principal_id"], OWNER["controller_id"], missing_profile["idempotency_key"]
    ) is None


def _frame_fixture(tmp_path, task_id: str = "agent-session-1"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    profile_store = EmulatorProfileStore(tmp_path / "profiles.sqlite")
    profiles = EmulatorProfileService(
        store=profile_store,
        probe=lambda serial: EmulatorProbe(
            serial, EmulatorProfileState.READY, "boot-a",
            EmulatorFingerprint(True, 35, "x86_64", "2x2"), ("screen",),
        ), clock=lambda: NOW,
    )
    profile = profiles.save_selected(
        **OWNER, candidate=_adb("emulator-5554", "sdk_gphone64_x86_64"),
        display_name="frame emulator", is_default=True,
    )
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    content = b"\x89PNG\r\n\x1a\nverified-png"
    ref = artifacts.write(artifact_id="task/frame", content_type="image/png", content=content)
    observation = SimpleNamespace(
        id="observation-1", task_id="kernel-task-different",
        device_id=profile.canonical_device_id,
        screenshot=SimpleNamespace(
            artifact=ref, mime_type="image/png", width=2, height=2, captured_at=NOW,
        ),
    )
    binding = VerifiedFrameBinding(task_id, profile.profile_id, observation.id, ref.reference)
    port = VerifiedFramePort(
        resolve_binding=lambda principal, controller, task: binding
        if (principal, controller, task) == (OWNER["principal_id"], OWNER["controller_id"], binding.task_id) else None,
        load_observation=lambda observation_id: observation if observation_id == observation.id else None,
        profiles=profiles, artifacts=artifacts,
    )
    return port, artifacts, observation, binding, content


def test_frame_requires_explicit_binding_and_returns_only_verified_png(tmp_path) -> None:
    port, _, observation, binding, content = _frame_fixture(tmp_path)
    metadata = port.metadata(binding.task_id, OWNER)
    assert metadata["frame_id"] == observation.id
    assert "reference" not in repr(metadata).lower()
    assert "path" not in repr(metadata).lower()
    assert port.content(binding.task_id, observation.id, OWNER)[1] == content
    with pytest.raises(ExecutionContractError, match="FRAME_UNAVAILABLE"):
        port.metadata(binding.task_id, OTHER_CONTROLLER)
    with pytest.raises(ExecutionContractError, match="FRAME_UNAVAILABLE"):
        port.metadata("wrong-task", OWNER)
    with pytest.raises(ExecutionContractError, match="FRAME_UNAVAILABLE"):
        port.content(binding.task_id, "stale-frame", OWNER)

    wrong_profile = VerifiedFrameBinding(binding.task_id, "profile-wrong", observation.id, binding.frame_reference)
    wrong_profile_port = VerifiedFramePort(
        resolve_binding=lambda *_: wrong_profile,
        load_observation=lambda _: observation,
        profiles=port.profiles, artifacts=port.artifacts,
    )
    with pytest.raises(ExecutionContractError, match="FRAME_UNAVAILABLE"):
        wrong_profile_port.metadata(binding.task_id, OWNER)


def test_v2_frame_http_surface_is_owner_gated_and_hardened(tmp_path) -> None:
    service, _, _, _ = _composition(tmp_path)
    task_id = service.create_task(_create(), auth_context=OWNER)["task_id"]
    frames, _, observation, _, content = _frame_fixture(tmp_path / "frame", task_id)
    assert frames.metadata(task_id, OWNER)["task_id"] == task_id
    # FastAPI's TestClient invokes sync handlers on a worker thread while D's
    # focused SQLite store fixture intentionally owns one creating thread.
    # Keep the HTTP-route test on a fake owner/profile read; the prior direct
    # assertion is the real temp-SQLite binding test.
    frames.profiles = SimpleNamespace(require_profile=lambda **_: SimpleNamespace(
        canonical_device_id=observation.device_id
    ))
    service.frames = frames
    assert service.frame_metadata(task_id, auth_context=OWNER)["task_id"] == task_id
    app = FastAPI()
    app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)
    app.include_router(create_execution_v2_router(
        service, token="test-token", capability_context=OWNER,
    ))
    headers = {
        "X-AI-Game-Client": "weftmate-harness-v1",
        "Authorization": "Bearer test-token",
        "X-AI-Game-Principal-Id": OWNER["principal_id"],
        "X-AI-Game-Controller-Id": OWNER["controller_id"],
    }
    with TestClient(app) as client:
        metadata = client.get(f"/api/execution/v2/tasks/{task_id}/frame", headers=headers)
        assert metadata.status_code == 200, metadata.text
        assert "reference" not in metadata.text.lower()
        assert "path" not in metadata.text.lower()
        frame_id = metadata.json()["frame_id"]
        frame = client.get(
            f"/api/execution/v2/tasks/{task_id}/frames/{frame_id}", headers=headers
        )
        assert frame.status_code == 200
        assert frame.content == content
        assert frame.headers["content-type"] == "image/png"
        assert frame.headers["cache-control"] == "no-store"
        assert frame.headers["x-content-type-options"] == "nosniff"


def test_frame_rejects_size_hash_type_missing_and_path_escape(tmp_path) -> None:
    port, artifacts, observation, binding, _ = _frame_fixture(tmp_path)
    path = artifacts.resolve(observation.screenshot.artifact)
    path.write_bytes(b"\x89PNG\r\n\x1a\ncorrupt")
    with pytest.raises(ExecutionContractError, match="FRAME_UNAVAILABLE"):
        port.content(binding.task_id, observation.id, OWNER)
    path.unlink()
    with pytest.raises(ExecutionContractError, match="FRAME_UNAVAILABLE"):
        port.content(binding.task_id, observation.id, OWNER)

    escaped = SimpleNamespace(reference="../escape.png", content_type="image/png", size_bytes=8, sha256="0" * 64)
    observation.screenshot.artifact = escaped
    escaped_binding = VerifiedFrameBinding(binding.task_id, binding.profile_id, observation.id, escaped.reference)
    escaped_port = VerifiedFramePort(
        resolve_binding=lambda *_: escaped_binding,
        load_observation=lambda _: observation,
        profiles=port.profiles, artifacts=artifacts,
    )
    with pytest.raises(ExecutionContractError, match="FRAME_UNAVAILABLE"):
        escaped_port.content(binding.task_id, observation.id, OWNER)

    wrong_type = ArtifactRef("wrong.png", "application/xml", 8, "0" * 64)
    observation.screenshot.artifact = wrong_type
    observation.screenshot.mime_type = "application/xml"
    wrong_binding = VerifiedFrameBinding(binding.task_id, binding.profile_id, observation.id, wrong_type.reference)
    wrong_port = VerifiedFramePort(
        resolve_binding=lambda *_: wrong_binding, load_observation=lambda _: observation,
        profiles=port.profiles, artifacts=artifacts,
    )
    with pytest.raises(ExecutionContractError, match="FRAME_UNAVAILABLE"):
        wrong_port.metadata(binding.task_id, OWNER)
