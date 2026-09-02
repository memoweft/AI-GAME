from __future__ import annotations

import json
import hashlib
import sqlite3
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_game_console.execution_contract.api import (
    create_execution_v2_router,
    execution_contract_error_handler,
)
from ai_game_console.execution_contract.service import (
    ExecutionContractError,
    V2ExecutionContractService,
)
from ai_game_console.execution_contract.store import SQLiteExecutionContractStore
from ai_game_console.execution_contract.v2_models import ControlRequest, CreateTaskRequest


def identity(session: str = "dsh-session-a", call: str = "call-1") -> dict[str, str | int]:
    return {
        "dsh_session_id": session,
        "dsh_turn_id": 1,
        "tool_call_id": call,
        "root_call_id": "root-1",
    }


def auth(principal: str = "capability-a", controller: str = "weftmate-local-a") -> dict[str, str]:
    return {"principal_id": principal, "controller_id": controller}


def create_request(session: str = "dsh-session-a", call: str = "call-1") -> dict:
    return {
        "origin": identity(session, call),
        "goal": {
            "summary": "Check a bounded emulator task",
            "task_kind": "bounded",
            "stop_condition": {"kind": "criteria", "value": "verified"},
            "schedule": None,
        },
        "client_request_id": "request-1",
        "idempotency_key": "submit-1",
        "priority": 50,
        "authorization_mode": "full-access",
    }


class FakeTaskPort:
    """In-memory canonical-port fixture; it is the sole fake Task authority."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.create_requests: list[dict] = []
        self.create_calls = 0
        self.revision_calls = 0
        self.control_calls = 0
        self.answer_calls = 0
        self.events_by_task: dict[str, list[dict]] = {}

    def create(self, request: dict) -> dict:
        self.create_calls += 1
        self.create_requests.append(deepcopy(request))
        task_id = f"agent-session-{self.create_calls}"
        self.tasks.setdefault(task_id, {
            "schema_version": 2,
            "task_id": task_id,
            "status": "scheduled",
            "current_revision": 0,
            "origin": request["origin"],
            "goal": request["goal"],
            "reason": {"code": "accepted", "summary": "Task accepted."},
            "next_wake_at": None,
            "event_cursor": 1,
            "pending_question": None,
            "result": None,
            "error": None,
            "device": None,
            "timestamps": {
                "created_at": "2026-08-30T00:00:00Z",
                "updated_at": "2026-08-30T00:00:00Z",
                "terminal_at": None,
            },
            # Simulate a port that retains internal owner context.  The v2
            # adapter must remove it from all public projections.
            "principal_id": request["principal_id"],
            "controller_id": request["controller_id"],
        })
        return deepcopy(self.tasks[task_id])

    def get(self, task_id: str) -> dict | None:
        task = self.tasks.get(task_id)
        return deepcopy(task) if task else None

    def list(self, request: dict) -> dict:
        allowed = set(request["task_ids"])
        status = request.get("status")
        return {
            "items": [
                deepcopy(task) for task_id, task in self.tasks.items()
                if task_id in allowed and (status is None or task["status"] == status)
            ],
            "next_cursor": None,
        }

    def events(self, task_id: str, *, after: int, limit: int) -> dict:
        return {
            "items": [
                deepcopy(event) for event in self.events_by_task.get(task_id, [])
                if event["cursor"] > after
            ][:limit],
            "next_cursor": max((event["cursor"] for event in self.events_by_task.get(task_id, [])), default=after),
        }

    def revise(self, task_id: str, request: dict) -> dict:
        self.revision_calls += 1
        task = self.tasks[task_id]
        if request["base_revision"] != task["current_revision"]:
            raise ExecutionContractError("TASK_REVISION_CONFLICT", "The requested revision is stale.", 409)
        task["current_revision"] += 1
        task["status"] = "replanning"
        return deepcopy(task)

    def control(self, task_id: str, request: dict) -> dict:
        self.control_calls += 1
        task = self.tasks[task_id]
        expected = request.get("expected_revision")
        if expected is not None and expected != task["current_revision"]:
            raise ExecutionContractError("TASK_REVISION_CONFLICT", "The requested revision is stale.", 409)
        task["status"] = {"pause": "paused", "resume": "running", "cancel": "cancelled", "takeover": "user_takeover", "release_takeover": "replanning"}[request["action"]]
        return deepcopy(task)

    def answer(self, task_id: str, request: dict) -> dict:
        self.answer_calls += 1
        task = self.tasks[task_id]
        task["status"] = "replanning"
        return deepcopy(task)

    def archive(self, task_id: str, request: dict) -> dict:
        task = self.tasks[task_id]
        task["archived"] = True
        return deepcopy(task)


class FakeDeviceProfilePort:
    def __init__(self, profiles: list[dict], next_cursor: str | None = None) -> None:
        self.profiles = deepcopy(profiles)
        self.next_cursor = next_cursor
        self.list_requests: list[dict] = []

    def list(self, request: dict) -> dict:
        self.list_requests.append(deepcopy(request))
        return {"profiles": deepcopy(self.profiles), "next_cursor": self.next_cursor}


@pytest.fixture
def fixture(tmp_path):
    tasks = FakeTaskPort()
    service = V2ExecutionContractService(SQLiteExecutionContractStore(tmp_path / "v2.sqlite"), tasks)
    return service, tasks


def test_v2_device_profile_list_uses_public_items_contract_and_redacts_raw_material(tmp_path):
    tasks = FakeTaskPort()
    profiles = FakeDeviceProfilePort([{
        "device_profile_id": "profile-mumu-default",
        "display_name": "V2241A",
        "is_default": True,
        "status": "ready",
        "serial": "raw-device-serial",
        "adb_path": "C:/private/platform-tools/adb.exe",
        "properties": {"ro.product.model": "private-raw-model"},
        "credential": "private-profile-credential",
    }], next_cursor="profile-page-2")
    service = V2ExecutionContractService(
        SQLiteExecutionContractStore(tmp_path / "v2.sqlite"),
        tasks,
        device_profiles=profiles,
    )

    page = service.list_device_profiles(
        None, auth_context=auth(), cursor="profile-page-1", limit=20
    )

    assert set(page) == {"items", "next_cursor"}
    assert page["next_cursor"] == "profile-page-2"
    assert page["items"][0]["device_profile_id"] == "profile-mumu-default"
    assert page["items"][0]["display_name"] == "V2241A"
    assert profiles.list_requests == [{
        "principal_id": "capability-a",
        "controller_id": "weftmate-local-a",
        "cursor": "profile-page-1",
        "limit": 20,
    }]
    encoded = json.dumps(page, ensure_ascii=False)
    for raw_value in (
        "raw-device-serial",
        "C:/private/platform-tools/adb.exe",
        "private-raw-model",
        "private-profile-credential",
    ):
        assert raw_value not in encoded


def test_v2_device_profile_list_returns_an_empty_public_page(tmp_path):
    service = V2ExecutionContractService(
        SQLiteExecutionContractStore(tmp_path / "v2.sqlite"),
        FakeTaskPort(),
        device_profiles=FakeDeviceProfilePort([]),
    )

    assert service.list_device_profiles(
        None, auth_context=auth(), cursor=None, limit=20
    ) == {"items": [], "next_cursor": None}


def test_v2_submission_scope_is_additive_and_owner_composite(tmp_path):
    path = tmp_path / "legacy-v2.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE execution_v2_submissions ("
            "submission_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO execution_v2_submissions VALUES (?, ?)",
            ("legacy-global-preview", "legacy-hash"),
        )

    store = SQLiteExecutionContractStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT request_hash FROM execution_v2_submissions WHERE submission_key = ?",
            ("legacy-global-preview",),
        ).fetchone() == ("legacy-hash",)
        primary_key = {
            row[1]: row[5]
            for row in connection.execute(
                "PRAGMA table_info(execution_v2_submission_scopes)"
            )
        }
    assert {key: primary_key[key] for key in (
        "principal_id", "controller_id", "submission_key", "request_hash", "task_id",
        "dsh_session_id", "dsh_turn_id", "created_at", "updated_at",
    )} == {
        "principal_id": 1,
        "controller_id": 2,
        "submission_key": 3,
        "request_hash": 0,
        "task_id": 0,
        "dsh_session_id": 0,
        "dsh_turn_id": 0,
        "created_at": 0,
        "updated_at": 0,
    }
    assert {
        "canonical_envelope_json", "envelope_hash", "runner_kind", "runner_version",
        "device_profile_id", "authorization_mode", "canonical_client_request_id",
        "canonical_idempotency_key", "canonical_origin_json", "canonical_origin_hash",
    }.issubset(primary_key)

    base = {
        "submission_key": "same-opaque-key",
        "request_hash": "request-hash",
        "dsh_session_id": "dsh-session-a",
        "dsh_turn_id": "1",
        "created_at": "2026-08-30T00:00:00Z",
        "updated_at": "2026-08-30T00:00:00Z",
    }
    _, first_created = store.reserve_v2_submission({
        **base, "principal_id": "principal-a", "controller_id": "controller-a",
    })
    _, other_principal_created = store.reserve_v2_submission({
        **base, "principal_id": "principal-b", "controller_id": "controller-a",
    })
    _, other_controller_created = store.reserve_v2_submission({
        **base, "principal_id": "principal-a", "controller_id": "controller-b",
    })
    assert (first_created, other_principal_created, other_controller_created) == (
        True, True, True,
    )


def test_v2_create_model_accepts_the_general_runner_but_not_a_client_version() -> None:
    request = create_request()
    request["runner_kind"] = "android_ui_agent"
    request["device_profile_id"] = "profile-a"
    assert CreateTaskRequest.model_validate(request).runner_kind == "android_ui_agent"
    with pytest.raises(ValidationError):
        CreateTaskRequest.model_validate({**request, "runner_version": "2"})


@pytest.mark.parametrize("priority", [True, False, "50", 50.0])
def test_v2_create_model_requires_an_exact_integer_priority(priority: object) -> None:
    request = create_request()
    request["priority"] = priority
    with pytest.raises(ValidationError):
        CreateTaskRequest.model_validate(request)


def test_v2_submission_fence_rejects_runner_profile_and_authorization_conflicts(fixture) -> None:
    service, tasks = fixture
    first = create_request("dsh-session-a", "first")
    first.update({
        "client_request_id": "same-submit", "idempotency_key": "same-submit",
        "runner_kind": "android_ui_agent", "device_profile_id": "profile-a",
    })
    service.create_task(first, auth_context=auth())
    for changed in (
        {"device_profile_id": "profile-b"},
        {"authorization_mode": "allowed-once"},
    ):
        retry = create_request("dsh-session-a", f"retry-{len(changed)}")
        retry.update({
            "client_request_id": "same-submit", "idempotency_key": "same-submit",
            "runner_kind": "android_ui_agent", "device_profile_id": "profile-a",
        })
        retry.update(changed)
        with pytest.raises(ExecutionContractError, match="EXECUTION_IDEMPOTENCY_CONFLICT"):
            service.create_task(retry, auth_context=auth())
    unsupported = create_request("dsh-session-a", "unsupported-runner")
    unsupported.update({
        "client_request_id": "same-submit", "idempotency_key": "same-submit",
        "runner_kind": "emulator_settings_v1", "device_profile_id": "profile-a",
    })
    with pytest.raises(ExecutionContractError, match="CAPABILITY_UNAVAILABLE"):
        service.create_task(unsupported, auth_context=auth())
    assert tasks.create_calls == 1


def test_v2_first_envelope_uses_the_shared_sanitizer_for_goal_and_nested_payloads(fixture) -> None:
    service, tasks = fixture
    request = create_request("dsh-session-a", "secret-goal")
    sentinels = (
        "K0-password-DO-NOT-PERSIST", "K0-otp-DO-NOT-PERSIST",
        "K0-nested-password-DO-NOT-PERSIST", "K0-nested-token-DO-NOT-PERSIST",
        "K0-key correct horse battery staple",
        "K0-multitoken correct horse battery staple",
        "correct horse battery staple",
    )
    request["goal"] = {
        "summary": (
            "Open Android Settings, then use password: K0-password-DO-NOT-PERSIST "
            "and OTP: K0-otp-DO-NOT-PERSIST; then return to the Settings homepage. "
            "password: K0-multitoken correct horse battery staple"
        ),
        "task_kind": "bounded",
        "stop_condition": {
            "password": "K0-nested-password-DO-NOT-PERSIST",
            "password: K0-key correct horse battery staple": "ignored",
        },
        "schedule": {"token": "K0-nested-token-DO-NOT-PERSIST"},
    }
    created = service.create_task(request, auth_context=auth())
    submission = service.store.v2_submission("capability-a", "weftmate-local-a", "submit-1")
    assert submission is not None
    persisted = json.dumps(submission, ensure_ascii=False)
    canonical = json.dumps(tasks.create_requests[0], ensure_ascii=False)
    public = json.dumps({
        "create": created,
        "get": service.get_task(created["task_id"], None, auth_context=auth()),
        "list": service.list_tasks(None, auth_context=auth(), status=None, cursor=None, limit=20),
        "events": service.events(created["task_id"], None, auth_context=auth(), after=0, limit=20),
    }, ensure_ascii=False)
    for sentinel in sentinels:
        assert sentinel not in persisted
        assert sentinel not in canonical
        assert sentinel not in public
    assert "password: K0-key" not in persisted
    assert "password: K0-key" not in canonical
    assert "password: K0-key" not in public


@pytest.mark.parametrize("tamper", [
    "envelope_owner", "device_profile_id", "runner_kind", "runner_version",
    "authorization_mode", "canonical_client_request_id", "canonical_idempotency_key",
    "canonical_origin_json", "dsh_session_id", "dsh_turn_id", "request_hash", "envelope_hash",
    "task_kind", "stop_condition", "schedule", "priority",
])
def test_v2_null_replay_validates_each_persisted_envelope_fact_before_create(fixture, tamper: str) -> None:
    service, tasks = fixture
    original_create = tasks.create
    dropped = True

    def drop_after_canonical_create(request: dict) -> dict:
        nonlocal dropped
        result = original_create(request)
        if dropped:
            dropped = False
            raise TimeoutError("response dropped")
        return result

    tasks.create = drop_after_canonical_create  # type: ignore[method-assign]
    request = create_request("dsh-session-a", "null-replay-first")
    request.update({
        "client_request_id": "null-replay", "idempotency_key": "null-replay",
        "runner_kind": "android_ui_agent", "device_profile_id": "profile-a",
    })
    with pytest.raises(ExecutionContractError, match="TASK_PORT_UNAVAILABLE"):
        service.create_task(request, auth_context=auth())
    tasks.create_calls = 0
    submission = service.store.v2_submission("capability-a", "weftmate-local-a", "null-replay")
    assert submission is not None and submission["task_id"] is None

    with service.store.transaction() as connection:
        if tamper in {"envelope_owner", "task_kind", "stop_condition", "schedule", "priority"}:
            envelope = json.loads(str(submission["canonical_envelope_json"]))
            if tamper == "envelope_owner":
                envelope["principal_id"] = "principal-tampered"
            elif tamper == "task_kind":
                envelope["goal"]["task_kind"] = "unbounded"
            elif tamper == "stop_condition":
                envelope["goal"]["stop_condition"] = []
            elif tamper == "schedule":
                envelope["goal"]["schedule"] = "every night"
            else:
                envelope["priority"] = True
            encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "UPDATE execution_v2_submission_scopes SET canonical_envelope_json = ?, envelope_hash = ?, request_hash = ? "
                "WHERE principal_id = ? AND controller_id = ? AND submission_key = ?",
                (
                    encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                    service._submission_hash_from_envelope(envelope),
                    "capability-a", "weftmate-local-a", "null-replay",
                ),
            )
        else:
            values = {
                "device_profile_id": "profile-tampered",
                "runner_kind": "emulator_settings_v1",
                "runner_version": "2",
                "authorization_mode": "allowed-once",
                "canonical_client_request_id": "client-tampered",
                "canonical_idempotency_key": "idempotency-tampered",
                "canonical_origin_json": "{}",
                "dsh_session_id": "dsh-session-tampered",
                "dsh_turn_id": "99",
                "request_hash": "tampered-request-hash",
                "envelope_hash": "tampered-envelope-hash",
            }
            connection.execute(
                f"UPDATE execution_v2_submission_scopes SET {tamper} = ? "
                "WHERE principal_id = ? AND controller_id = ? AND submission_key = ?",
                (values[tamper], "capability-a", "weftmate-local-a", "null-replay"),
            )

    with pytest.raises(ExecutionContractError):
        service.create_task(request, auth_context=auth())
    assert tasks.create_calls == 0
    assert service.store.v2_submission("capability-a", "weftmate-local-a", "null-replay")["task_id"] is None
    assert service.store.v2_alias_for_task("agent-session-1") is None
    assert service.runner_admission("agent-session-1", **auth()) is None


@pytest.mark.parametrize("bad_attempt", ["task", "owner", "hash", "runner"])
def test_v2_runner_admission_scans_every_same_scope_attempt(fixture, bad_attempt: str) -> None:
    service, _ = fixture
    request = create_request("dsh-session-a", "admission-first")
    request.update({
        "client_request_id": "admission-key", "idempotency_key": "admission-key",
        "runner_kind": "android_ui_agent", "device_profile_id": "profile-a",
    })
    created = service.create_task(request, auth_context=auth())
    task_id = created["task_id"]
    submission = service.store.v2_submission("capability-a", "weftmate-local-a", "admission-key")
    assert submission is not None
    assert service.runner_admission(task_id, **auth())["runner_kind"] == "android_ui_agent"
    record = service._v2_alias_record(
        execution_id=f"exec-bad-{bad_attempt}", identity_key=f"identity-bad-{bad_attempt}",
        payload_hash="payload-bad", task_id=task_id if bad_attempt != "task" else "other-task",
        submission_key="admission-key", origin=identity("dsh-session-a", f"bad-{bad_attempt}"),
        principal_id="capability-other" if bad_attempt == "owner" else "capability-a",
        controller_id="weftmate-local-a", envelope_hash=(
            "wrong-envelope" if bad_attempt == "hash" else str(submission["envelope_hash"])
        ),
        runner_kind=("emulator_settings_v1" if bad_attempt == "runner" else "android_ui_agent"),
        runner_version="1", now="2026-08-30T00:00:00Z",
    )
    service.store.reserve_v2_alias(record)
    assert service.runner_admission(task_id, **auth()) is None


def test_v2_runner_admission_keeps_same_key_other_owner_other_task_isolated(fixture) -> None:
    service, _ = fixture
    request = create_request("dsh-session-a", "isolation-first")
    request.update({
        "client_request_id": "isolation-key", "idempotency_key": "isolation-key",
        "runner_kind": "android_ui_agent", "device_profile_id": "profile-a",
    })
    created = service.create_task(request, auth_context=auth())
    task_id = created["task_id"]
    submission = service.store.v2_submission("capability-a", "weftmate-local-a", "isolation-key")
    assert submission is not None
    service.store.reserve_v2_alias(service._v2_alias_record(
        execution_id="exec-other-owner", identity_key="identity-other-owner", payload_hash="payload-other",
        task_id="different-owner-task", submission_key="isolation-key",
        origin=identity("dsh-owner-other", "call-owner-other"),
        principal_id="capability-other", controller_id="weftmate-local-a",
        envelope_hash="other-owner-envelope", runner_kind="android_ui_agent", runner_version="1",
        now="2026-08-30T00:00:00Z",
    ))
    assert service.runner_admission(task_id, **auth())["task_id"] == task_id


def test_v2_maps_one_execution_alias_to_the_canonical_agent_session_and_replays(fixture):
    service, tasks = fixture

    first = service.create_task(create_request(), auth_context=auth())
    second = service.create_task(create_request(), auth_context=auth())

    assert first["task_id"] == "agent-session-1"
    assert second["execution_id"] == first["execution_id"]
    assert second["replayed"] is True
    assert tasks.create_calls == 1
    assert tasks.create_requests[0]["principal_id"] == "capability-a"
    assert tasks.create_requests[0]["controller_id"] == "weftmate-local-a"
    assert "principal_id" not in first
    assert "controller_id" not in first
    assert service.store.get(first["execution_id"]) is None
    assert service.store.v2_alias_for_task("agent-session-1")["task_id"] == "agent-session-1"

    conflicting = create_request()
    conflicting["goal"]["summary"] = "A changed goal"
    with pytest.raises(ExecutionContractError, match="EXECUTION_IDEMPOTENCY_CONFLICT"):
        service.create_task(conflicting, auth_context=auth())


def test_v2_submit_retry_fence_reuses_one_task_after_a_committed_response_is_lost(fixture):
    service, tasks = fixture
    first_request = create_request(call="call-first")
    first_request["client_request_id"] = "stable-submit-turn-1"
    first_request["idempotency_key"] = "stable-submit-turn-1"
    first_request["device_profile_id"] = "profile-a"
    first_request["runner_kind"] = "android_ui_agent"

    # Simulate a committed server response that the client never accepted.
    first = service.create_task(first_request, auth_context=auth())

    retry_request = create_request(call="call-retry")
    retry_request["client_request_id"] = "stable-submit-turn-1"
    retry_request["idempotency_key"] = "stable-submit-turn-1"
    retry_request["device_profile_id"] = "profile-a"
    retry_request["runner_kind"] = "android_ui_agent"
    retry_request["goal"]["summary"] = "Check the emulator task (Settings)"
    retry_request["origin"]["root_call_id"] = "root-retry"
    replay = service.create_task(retry_request, auth_context=auth())

    assert replay["task_id"] == first["task_id"]
    assert replay["replayed"] is True
    assert tasks.create_calls == 1
    attempts = service.store.v2_attempts_for_submission("stable-submit-turn-1")
    assert [item["tool_call_id"] for item in attempts] == ["call-first", "call-retry"]
    assert [item["root_call_id"] for item in attempts] == ["root-1", "root-retry"]
    assert {item["task_id"] for item in attempts} == {first["task_id"]}
    listed = service.list_tasks(
        None, auth_context=auth(), status=None, cursor=None, limit=20
    )["items"]
    assert [item["task_id"] for item in listed] == [first["task_id"]]


def test_v2_submit_retry_fence_separates_turns_and_fails_closed_on_conflicts(fixture):
    service, tasks = fixture
    first_request = create_request(call="call-turn-1")
    first_request["client_request_id"] = "stable-submit-turn-1"
    first_request["idempotency_key"] = "stable-submit-turn-1"
    first_request["device_profile_id"] = "profile-a"
    first = service.create_task(first_request, auth_context=auth())

    different_turn = create_request(call="call-turn-2")
    different_turn["origin"]["dsh_turn_id"] = 2
    different_turn["client_request_id"] = "stable-submit-turn-2"
    different_turn["idempotency_key"] = "stable-submit-turn-2"
    different_turn["device_profile_id"] = "profile-a"
    second = service.create_task(different_turn, auth_context=auth())
    assert second["task_id"] != first["task_id"]
    assert tasks.create_calls == 2

    wrong_profile = create_request(call="call-conflict-profile")
    wrong_profile["client_request_id"] = "stable-submit-turn-1"
    wrong_profile["idempotency_key"] = "stable-submit-turn-1"
    wrong_profile["device_profile_id"] = "profile-b"
    with pytest.raises(ExecutionContractError, match="EXECUTION_IDEMPOTENCY_CONFLICT"):
        service.create_task(wrong_profile, auth_context=auth())

    runner_conflict = create_request(call="call-conflict-runner")
    runner_conflict["client_request_id"] = "stable-submit-turn-1"
    runner_conflict["idempotency_key"] = "stable-submit-turn-1"
    runner_conflict["device_profile_id"] = "profile-a"
    runner_conflict["runner_kind"] = "emulator_settings_v1"
    with pytest.raises(ExecutionContractError, match="CAPABILITY_UNAVAILABLE"):
        service.create_task(runner_conflict, auth_context=auth())

    wrong_session = create_request(session="dsh-session-other", call="call-conflict-session")
    wrong_session["client_request_id"] = "stable-submit-turn-1"
    wrong_session["idempotency_key"] = "stable-submit-turn-1"
    wrong_session["device_profile_id"] = "profile-a"
    with pytest.raises(ExecutionContractError, match="EXECUTION_IDEMPOTENCY_CONFLICT"):
        service.create_task(wrong_session, auth_context=auth())

    same_key_other_principal = create_request(call="call-other-principal")
    same_key_other_principal["client_request_id"] = "stable-submit-turn-1"
    same_key_other_principal["idempotency_key"] = "stable-submit-turn-1"
    same_key_other_principal["device_profile_id"] = "profile-a"
    other_principal = service.create_task(
        same_key_other_principal,
        auth_context=auth("capability-other", "weftmate-local-a"),
    )

    same_key_other_controller = create_request(call="call-other-controller")
    same_key_other_controller["client_request_id"] = "stable-submit-turn-1"
    same_key_other_controller["idempotency_key"] = "stable-submit-turn-1"
    same_key_other_controller["device_profile_id"] = "profile-a"
    other_controller = service.create_task(
        same_key_other_controller,
        auth_context=auth("capability-a", "controller-other"),
    )

    assert len({first["task_id"], second["task_id"], other_principal["task_id"], other_controller["task_id"]}) == 4
    assert tasks.create_calls == 4
    assert [item["task_id"] for item in service.list_tasks(
        None,
        auth_context=auth("capability-other", "weftmate-local-a"),
        status=None,
        cursor=None,
        limit=20,
    )["items"]] == [other_principal["task_id"]]
    assert [item["task_id"] for item in service.list_tasks(
        None,
        auth_context=auth("capability-a", "controller-other"),
        status=None,
        cursor=None,
        limit=20,
    )["items"]] == [other_controller["task_id"]]


def test_v2_safe_task_projection_preserves_null_and_emits_only_safe_real_errors(fixture):
    service, tasks = fixture
    created = service.create_task(create_request(), auth_context=auth())
    task_id = created["task_id"]

    assert created["error"] is None
    assert service.get_task(task_id, None, auth_context=auth())["error"] is None
    assert service.list_tasks(
        None, auth_context=auth(), status=None, cursor=None, limit=20
    )["items"][0]["error"] is None

    tasks.tasks[task_id]["status"] = "failed"
    tasks.tasks[task_id]["error"] = {
        "code": "DEVICE/DOWN secret",
        "summary": "C:/private/adb.exe raw serial and credential",
    }
    failed = service.get_task(task_id, None, auth_context=auth())
    assert failed["error"] == {
        "code": "DEVICE_DOWN_secret",
        "summary": "The Task runtime reported an error.",
    }
    encoded = json.dumps(failed, ensure_ascii=False)
    assert "C:/private/adb.exe" not in encoded
    assert "raw serial" not in encoded
    assert "credential" not in encoded


def test_v2_owner_is_capability_principal_not_dsh_session(fixture):
    service, _ = fixture
    created = service.create_task(create_request(), auth_context=auth())

    with pytest.raises(ExecutionContractError, match="TASK_STATUS_UNSUPPORTED"):
        service.list_tasks(identity(), auth_context=auth(), status="future_state", cursor=None, limit=20)
    # A new official DSH session under the same authenticated capability sees
    # and controls the global task; origin session remains provenance only.
    origin_b = identity("dsh-session-b", "call-b")
    assert service.get_task(created["task_id"], origin_b, auth_context=auth())["task_id"] == created["task_id"]
    assert service.list_tasks(origin_b, auth_context=auth(), status=None, cursor=None, limit=20)["items"][0]["task_id"] == created["task_id"]
    with pytest.raises(ExecutionContractError, match="TASK_NOT_FOUND"):
        service.create_task(create_request(), auth_context=auth("capability-b", "weftmate-local-b"))
    with pytest.raises(ExecutionContractError, match="TASK_NOT_FOUND"):
        service.get_task(created["task_id"], origin_b, auth_context=auth("capability-b", "weftmate-local-b"))
    with pytest.raises(ExecutionContractError, match="TASK_NOT_FOUND"):
        service.get_task(created["task_id"], origin_b, auth_context=auth("capability-b", "weftmate-local-a"))
    assert service.list_tasks(origin_b, auth_context=auth("capability-b", "weftmate-local-b"), status=None, cursor=None, limit=20)["items"] == []


def test_v2_revision_control_and_answer_are_idempotent_and_preserve_cas(fixture):
    service, tasks = fixture
    task_id = service.create_task(create_request(), auth_context=auth())["task_id"]

    revision = {
        "origin": identity(), "revision_id": "rev-1", "idempotency_key": "op-rev-1",
        "base_revision": 0, "kind": "revise", "instruction": "Use the safer path",
        "effective_boundary": "after_current_action", "authorization_mode": "full-access",
    }
    assert service.revise(task_id, revision, auth_context=auth())["current_revision"] == 1
    assert service.revise(task_id, revision, auth_context=auth())["current_revision"] == 1
    assert tasks.revision_calls == 1

    stale = dict(revision, revision_id="rev-2", idempotency_key="op-rev-2", base_revision=0)
    with pytest.raises(ExecutionContractError, match="TASK_REVISION_CONFLICT"):
        service.revise(task_id, stale, auth_context=auth())

    control = {
        "origin": identity("dsh-session-b", "call-control"), "control_id": "ctl-1", "idempotency_key": "op-ctl-1",
        "expected_revision": 1, "action": "pause",
        "authorization_mode": "full-access",
    }
    assert service.control(task_id, control, auth_context=auth())["status"] == "paused"
    assert service.control(task_id, control, auth_context=auth())["status"] == "paused"
    assert tasks.control_calls == 1

    answer = {"origin": identity("dsh-session-c", "call-answer"), "question_id": "question-1", "idempotency_key": "op-answer-1", "value": "yes", "authorization_mode": "full-access"}
    assert service.answer(task_id, answer, auth_context=auth())["status"] == "replanning"
    assert service.answer(task_id, answer, auth_context=auth())["status"] == "replanning"
    assert tasks.answer_calls == 1


def test_v2_event_page_is_cursor_bounded_and_redacts_raw_details(fixture):
    service, tasks = fixture
    task_id = service.create_task(create_request(), auth_context=auth())["task_id"]
    tasks.events_by_task[task_id] = [{
        "cursor": 7,
        "type": "task.observed",
        "payload": {
            "token": "must-not-leak",
            "adb_serial": "emulator-5554",
            "error": {"code": "DEVICE_DOWN", "message": "C:/private/path and secret"},
            "large": ["x" * 4096 for _ in range(50)],
        },
    }]

    page = service.events(task_id, identity(), auth_context=auth(), after=0, limit=1)
    encoded = json.dumps(page, ensure_ascii=False).encode("utf-8")
    assert page["next_cursor"] == 7
    assert page["items"][0]["payload"]["truncated"] is True
    assert len(encoded) < 17_000
    assert service.store.v2_projection_checkpoint(task_id) == 7
    with pytest.raises(ExecutionContractError, match="PAGE_LIMIT_INVALID"):
        service.events(task_id, identity(), auth_context=auth(), after=0, limit=201)


def test_v2_router_fails_closed_for_an_unknown_control_enum(fixture):
    service, _ = fixture
    app = FastAPI()
    app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)
    app.include_router(create_execution_v2_router(service, token="test-token"))
    headers = {
        "X-AI-Game-Client": "weftmate-harness-v1",
        "Authorization": "Bearer test-token",
        "X-AI-Game-Principal-Id": "installed-principal-a",
        "X-AI-Game-Controller-Id": "weftmate-controller-a",
    }
    with TestClient(app) as client:
        response = client.post("/api/execution/v2/tasks", headers=headers, json=create_request())
        assert response.status_code == 202
        rejected = client.post(
            "/api/execution/v2/tasks/agent-session-1/controls",
            headers=headers,
            json={
                "origin": identity(), "control_id": "ctl-bad", "idempotency_key": "op-bad",
                "action": "replace_with_success", "authorization_mode": "full-access",
            },
        )
    assert rejected.status_code == 422
    assert "replace_with_success" in rejected.text
    with pytest.raises(ValidationError):
        ControlRequest.model_validate({
            "origin": identity(), "control_id": "ctl-bad", "idempotency_key": "op-bad",
            "action": "replace_with_success", "authorization_mode": "full-access",
        })


def test_v2_router_binds_owner_headers_without_exposing_them(fixture):
    service, tasks = fixture
    app = FastAPI()
    app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)
    app.include_router(create_execution_v2_router(service, token="test-token"))
    headers_a = {
        "X-AI-Game-Client": "weftmate-harness-v1",
        "Authorization": "Bearer test-token",
        "X-AI-Game-Principal-Id": "installed-principal-a",
        "X-AI-Game-Controller-Id": "weftmate-controller-a",
    }
    headers_b = dict(headers_a, **{"X-AI-Game-Controller-Id": "weftmate-controller-b"})
    origin_b = identity("dsh-session-b", "call-b")
    query_b = {
        "dsh_session_id": origin_b["dsh_session_id"],
        "dsh_turn_id": str(origin_b["dsh_turn_id"]),
        "tool_call_id": origin_b["tool_call_id"],
        "root_call_id": origin_b["root_call_id"],
    }
    with TestClient(app) as client:
        created = client.post("/api/execution/v2/tasks", headers=headers_a, json=create_request())
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        assert "principal_id" not in created.text
        assert "installed-principal-a" not in created.text
        assert "controller_id" not in created.text
        assert "weftmate-controller-a" not in created.text
        assert tasks.create_requests[-1]["principal_id"] == "installed-principal-a"
        assert tasks.create_requests[-1]["controller_id"] == "weftmate-controller-a"

        visible = client.get(f"/api/execution/v2/tasks/{task_id}", headers=headers_a, params=query_b)
        assert visible.status_code == 200
        assert visible.json()["task_id"] == task_id

        hidden = client.get(f"/api/execution/v2/tasks/{task_id}", headers=headers_b, params=query_b)
        assert hidden.status_code == 404
        assert "weftmate-controller-b" not in hidden.text

        created_b = client.post(
            "/api/execution/v2/tasks",
            headers=headers_b,
            json=create_request("dsh-session-controller-b", "call-controller-b"),
        )
        assert created_b.status_code == 202
        task_id_b = created_b.json()["task_id"]
        assert task_id_b != task_id
        assert tasks.create_requests[-1]["controller_id"] == "weftmate-controller-b"
        assert tasks.create_requests[-1]["principal_id"] == tasks.create_requests[0]["principal_id"]
        assert client.get(f"/api/execution/v2/tasks/{task_id_b}", headers=headers_b, params=query_b).status_code == 200
        assert client.get(f"/api/execution/v2/tasks/{task_id_b}", headers=headers_a, params=query_b).status_code == 404

        body_owner_attempt = client.post(
            "/api/execution/v2/tasks",
            headers=headers_a,
            json={
                **create_request("dsh-session-body-owner", "call-body-owner"),
                "principal_id": "body-principal",
                "controller_id": "body-controller",
            },
        )
        assert body_owner_attempt.status_code == 422
        assert tasks.create_calls == 2

        missing = client.post("/api/execution/v2/tasks", headers={
            "X-AI-Game-Client": "weftmate-harness-v1", "Authorization": "Bearer test-token",
        }, json=create_request("dsh-session-missing", "call-missing"))
        assert missing.status_code == 403

        invalid = client.post("/api/execution/v2/tasks", headers={
            **headers_a, "X-AI-Game-Controller-Id": "invalid controller!",
        }, json=create_request("dsh-session-invalid", "call-invalid"))
        assert invalid.status_code == 403
        assert "invalid controller!" not in invalid.text

        invalid_principal = client.post("/api/execution/v2/tasks", headers={
            **headers_a, "X-AI-Game-Principal-Id": "invalid principal!",
        }, json=create_request("dsh-session-invalid-principal", "call-invalid-principal"))
        assert invalid_principal.status_code == 403
        assert "invalid principal!" not in invalid_principal.text


def test_v2_router_rejects_explicit_capability_context_controller_conflict(fixture):
    service, _ = fixture
    app = FastAPI()
    app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)
    app.include_router(create_execution_v2_router(
        service,
        token="test-token",
        capability_context={"principal_id": "installed-capability-a", "controller_id": "expected-controller"},
    ))
    with TestClient(app) as client:
        rejected = client.post("/api/execution/v2/tasks", headers={
            "X-AI-Game-Client": "weftmate-harness-v1",
            "Authorization": "Bearer test-token",
            "X-AI-Game-Principal-Id": "installed-capability-a",
            "X-AI-Game-Controller-Id": "different-controller",
        }, json=create_request())
    assert rejected.status_code == 403
    assert "different-controller" not in rejected.text
