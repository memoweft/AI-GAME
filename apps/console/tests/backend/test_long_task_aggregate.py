from __future__ import annotations

import sqlite3

import pytest

from ai_game_console.agent_runtime.domain import (
    SessionNotFound,
    TaskControlConflict,
    TaskRecordStatus,
    TaskRevisionConflict,
)
from ai_game_console.agent_runtime.service import CanonicalTaskService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore


def _service(tmp_path) -> CanonicalTaskService:
    return CanonicalTaskService(SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"))


def _requester() -> dict[str, str]:
    return {"kind": "dsh_user", "execution_id": "exec-safe"}


def test_task_is_the_agent_session_identity_and_canonical_event_outbox_is_atomic(tmp_path) -> None:
    service = _service(tmp_path)
    task = service.create_task("打开设置", "create-a")

    assert task["task_id"] == task["id"]
    assert task["status"] == "scheduled"
    events = service.events(task["task_id"])
    assert [event["type"] for event in events] == ["task.created"]

    with sqlite3.connect(tmp_path / "agent-runtime.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_projection_outbox").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0] == 1


def test_revision_cas_is_accepted_then_applied_only_at_action_boundary(tmp_path) -> None:
    service = _service(tmp_path)
    task = service.create_task("持续检查", "create-b")
    task_id = task["task_id"]
    service.transition_task(
        task_id, status="running", reason_code="started", summary="开始执行", recoverable=True,
        idempotency_key="started",
    )

    response = service.revise_task(
        task_id, base_revision=1, kind="revise", instruction="改为每小时检查", patch={},
        idempotency_key="revise-1", requested_by=_requester(),
    )
    assert response["revision"]["status"] == TaskRecordStatus.ACCEPTED.value
    assert response["task"]["current_revision"] == 2
    assert response["revision"]["applied_at"] is None

    applied = service.apply_revision_boundary(task_id)
    assert [item["revision"] for item in applied["applied"]] == [2]
    assert applied["applied"][0]["status"] == TaskRecordStatus.APPLIED.value
    with pytest.raises(TaskRevisionConflict):
        service.revise_task(
            task_id, base_revision=1, kind="add", instruction="过期修订", patch={},
            idempotency_key="stale", requested_by=_requester(),
        )


def test_controls_are_idempotent_restart_safe_and_terminal_tasks_do_not_revive(tmp_path) -> None:
    service = _service(tmp_path)
    task_id = service.create_task("任务", "create-c")["task_id"]
    service.transition_task(task_id, status="running", reason_code="started", summary="开始", recoverable=True, idempotency_key="started")

    first = service.control_task(task_id, action="pause", idempotency_key="pause-1", expected_revision=1, requested_by=_requester())
    replay = service.control_task(task_id, action="pause", idempotency_key="pause-1", expected_revision=1, requested_by=_requester())
    assert first["control"]["id"] == replay["control"]["id"]
    assert replay["task"]["status"] == "paused"
    assert "resume" in replay["task"]["allowed_controls"]

    # A new service proves Task state comes back from SQLite rather than memory.
    after_restart = CanonicalTaskService(SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"))
    assert after_restart.inspect_task(task_id)["status"] == "paused"
    after_restart.control_task(task_id, action="resume", idempotency_key="resume", expected_revision=1, requested_by=_requester())
    after_restart.control_task(task_id, action="takeover", idempotency_key="takeover", expected_revision=1, requested_by=_requester())
    released = after_restart.control_task(task_id, action="release_takeover", idempotency_key="release", expected_revision=1, requested_by=_requester())
    assert released["task"]["status"] == "replanning"
    cancelled = after_restart.control_task(task_id, action="cancel", idempotency_key="cancel", expected_revision=1, requested_by=_requester())
    assert cancelled["task"]["status"] == "cancelled"
    archived = after_restart.control_task(task_id, action="archive", idempotency_key="archive", expected_revision=1, requested_by=_requester())
    assert archived["task"]["archived_at"] is not None
    with pytest.raises(TaskControlConflict):
        after_restart.control_task(task_id, action="resume", idempotency_key="revive", expected_revision=1, requested_by=_requester())


def test_ordinary_failure_is_recovery_not_terminal_and_integrity_fences_are_terminal(tmp_path) -> None:
    service = _service(tmp_path)
    task_id = service.create_task("等待回复", "create-d")["task_id"]
    service.transition_task(task_id, status="running", reason_code="started", summary="开始", recoverable=True, idempotency_key="started")
    recovering = service.ordinary_failure(
        task_id, reason_code="network_transient", summary="网络暂时不可用", idempotency_key="network",
    )
    assert recovering["status"] == "recovering"
    assert recovering["terminal"] is False

    for index, reason_code in enumerate((
        "duplicate_real_side_effect", "identity_crosswire", "credential_exposure", "fabricated_success",
    )):
        isolated = service.create_task(f"完整性-{index}", f"create-integrity-{index}")["task_id"]
        blocked = service.fence_integrity(isolated, reason_code=reason_code, summary="必须停止", idempotency_key=reason_code)
        assert blocked["status"] == "failed"
        assert blocked["integrity_state"] == "blocked"
        assert blocked["integrity_reason_code"] == reason_code


def test_task_events_are_monotonic_projection_rebuilds_and_subtask_scopes_are_isolated(tmp_path) -> None:
    service = _service(tmp_path)
    first_id = service.create_task("对象 A", "create-e1")["task_id"]
    second_id = service.create_task("对象 B", "create-e2")["task_id"]
    one = service.store.upsert_task_subtask(first_id, kind="conversation", object_ref="opaque-object", conversation_ref="opaque-chat", status="running")
    two = service.store.upsert_task_subtask(second_id, kind="conversation", object_ref="opaque-object", conversation_ref="opaque-chat", status="running")
    assert one.id != two.id
    assert one.task_id == first_id and two.task_id == second_id

    service.transition_task(first_id, status="running", reason_code="started", summary="开始", recoverable=True, idempotency_key="started")
    service.ordinary_failure(first_id, reason_code="page_changed", summary="页面变化", idempotency_key="page", replan=True)
    before = service.events(first_id)
    assert [item["cursor"] for item in before] == sorted(item["cursor"] for item in before)
    rebuilt = CanonicalTaskService(SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"))
    assert rebuilt.inspect_task(first_id)["status"] == "replanning"
    assert rebuilt.events(first_id) == before


def test_owner_principal_is_persistent_and_independent_from_dsh_origin_session(tmp_path) -> None:
    database = tmp_path / "agent-runtime.db"
    owner_one = CanonicalTaskService(
        SQLiteAgentRuntimeStore(database), principal_id="capability-principal-a", controller_id="install-a",
    )
    created = owner_one.create_task(
        "跨会话继续", "owner-create",
        origin={"dsh_session_id": "dsh-session-one", "created_turn_id": "turn-one", "created_tool_call_id": "call-one"},
    )
    task_id = created["task_id"]
    persisted = owner_one.store.get_task(task_id)
    assert persisted.owner_principal_id == "capability-principal-a"
    assert persisted.controller_id == "install-a"
    assert persisted.origin["dsh_session_id"] == "dsh-session-one"
    assert "owner_principal_id" not in created and "origin" not in created

    # A later DSH session under the exact same capability owner pair sees and
    # controls the task.  Its DSH session/turn is provenance, not ownership.
    same_principal_new_dsh_session = CanonicalTaskService(
        SQLiteAgentRuntimeStore(database), principal_id="capability-principal-a", controller_id="install-a",
    )
    assert same_principal_new_dsh_session.inspect_task(task_id)["task_id"] == task_id
    same_principal_new_dsh_session.transition_task(
        task_id, status="running", reason_code="continued", summary="另一会话继续", recoverable=True,
        idempotency_key="continue-from-other-dsh-session",
    )

    same_principal_other_controller = CanonicalTaskService(
        SQLiteAgentRuntimeStore(database), principal_id="capability-principal-a", controller_id="install-b",
    )
    assert same_principal_other_controller.list_tasks() == []
    with pytest.raises(SessionNotFound):
        same_principal_other_controller.inspect_task(task_id)
    with pytest.raises(SessionNotFound):
        same_principal_other_controller.control_task(
            task_id, action="pause", idempotency_key="same-principal-foreign-controller",
            expected_revision=1, requested_by=_requester(),
        )

    other_principal = CanonicalTaskService(
        SQLiteAgentRuntimeStore(database), principal_id="capability-principal-b", controller_id="install-b",
    )
    assert other_principal.list_tasks() == []
    with pytest.raises(SessionNotFound):
        other_principal.inspect_task(task_id)
    with pytest.raises(SessionNotFound):
        other_principal.control_task(
            task_id, action="pause", idempotency_key="foreign-control", expected_revision=1, requested_by=_requester(),
        )


@pytest.mark.parametrize(
    ("target", "next_wake_at"),
    [
        ("waiting_time", "2026-08-30T12:00:00+00:00"),
        ("waiting_event", None),
        ("recovering", None),
        ("replanning", None),
        ("paused", None),
        ("user_takeover", None),
        ("needs_user_input", None),
        ("succeeded", None),
        ("failed", None),
        ("cancelled", None),
    ],
)
def test_each_task_lifecycle_status_has_a_safe_transition_from_running(tmp_path, target, next_wake_at) -> None:
    service = _service(tmp_path)
    task_id = service.create_task(f"状态-{target}", f"create-{target}")["task_id"]
    service.transition_task(task_id, status="running", reason_code="started", summary="开始", recoverable=True, idempotency_key="started")
    result = service.transition_task(
        task_id, status=target, reason_code=f"to_{target}", summary=target,
        recoverable=target not in {"succeeded", "failed", "cancelled"}, idempotency_key=f"to-{target}",
        next_wake_at=next_wake_at,
    )
    assert result["status"] == target
