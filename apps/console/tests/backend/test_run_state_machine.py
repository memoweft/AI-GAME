from __future__ import annotations

from hashlib import sha256

from fastapi.testclient import TestClient

from conftest import WRITE_HEADERS


def create_run(
    client: TestClient,
    *,
    requires_approval: bool = False,
    exact_text: str | None = None,
):
    return client.post(
        "/api/v1/runs",
        headers=WRITE_HEADERS,
        json={
            "workflow_id": "windows-general",
            "target_id": "windows-local",
            "instruction": "打开记事本并准备输入",
            "exact_text": exact_text,
            "requires_approval": requires_approval,
            "name": "状态机测试",
        },
    )


def act(client: TestClient, run_id: str, action: str):
    return client.post(
        f"/api/v1/runs/{run_id}/actions",
        headers=WRITE_HEADERS,
        json={"action": action},
    )


def test_regular_run_stays_queued_and_exact_text_is_redacted_from_collections(
    client: TestClient,
) -> None:
    secret = "只在详情中出现的精确文字"
    response = create_run(client, exact_text=secret)

    assert response.status_code == 201
    created = response.json()
    assert created["status"] == "queued"
    assert created["blockers"] == [
        {
            "code": "workflow_executor_not_connected",
            "message": "传统任务队列尚未接入；请在“对话执行”中操作设备。",
        }
    ]
    assert "exact_text" not in created
    assert created["has_exact_text"] is True
    assert created["exact_text_length"] == len(secret)
    assert created["exact_text_sha256"] == sha256(secret.encode("utf-8")).hexdigest()
    assert created["workflow_name"] == "通用 Windows 软件"
    assert created["target_name"] == "本机 Windows 桌面"

    run_id = created["id"]
    listed = client.get("/api/v1/runs").json()["items"][0]
    overview = client.get("/api/v1/overview").json()["recent_runs"][0]
    assert "exact_text" not in listed
    assert "exact_text" not in overview
    assert client.get(f"/api/v1/runs/{run_id}").json()["exact_text"] == secret

    events_response = client.get(f"/api/v1/events?run_id={run_id}")
    assert secret not in events_response.text
    assert all("exact_text" not in event["data"] for event in events_response.json()["items"])


def test_overview_active_run_count_keeps_its_unfinished_run_compatibility_meaning(
    client: TestClient,
) -> None:
    queued = create_run(client).json()["id"]
    paused = create_run(client).json()["id"]
    awaiting_approval = create_run(client, requires_approval=True).json()["id"]
    cancelled = create_run(client).json()["id"]

    assert act(client, paused, "pause").status_code == 200
    assert act(client, cancelled, "cancel").status_code == 200

    # The current phase deliberately has no API action that starts a GUI run.
    # Set up the reserved future state directly so the count contract covers it.
    repository = client.app.state.repository
    with repository._connection(write=True) as connection:
        connection.execute("UPDATE runs SET status = 'running' WHERE id = ?", (queued,))

    overview = client.get("/api/v1/overview").json()
    assert overview["summary"]["active_run_count"] == 3
    assert overview["run_status_counts"] == {
        "awaiting_approval": 1,
        "cancelled": 1,
        "paused": 1,
        "running": 1,
    }
    assert awaiting_approval in {run["id"] for run in overview["recent_runs"]}


def test_pause_resume_cancel_transitions_are_strict(client: TestClient) -> None:
    run_id = create_run(client).json()["id"]

    paused = act(client, run_id, "pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    invalid_pause = act(client, run_id, "pause")
    assert invalid_pause.status_code == 409
    assert invalid_pause.json()["error"]["code"] == "invalid_run_transition"

    resumed = act(client, run_id, "resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "queued"
    assert resumed.json()["blockers"][0]["code"] == "workflow_executor_not_connected"

    cancelled = act(client, run_id, "cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["blockers"] == []

    invalid_resume = act(client, run_id, "resume")
    assert invalid_resume.status_code == 409
    assert invalid_resume.json()["error"]["code"] == "invalid_run_transition"


def test_approval_acceptance_and_rejection_drive_run_state(client: TestClient) -> None:
    awaiting = create_run(client, requires_approval=True).json()
    assert awaiting["status"] == "awaiting_approval"
    assert awaiting["blockers"][0]["code"] == "approval_required"

    approval = client.get("/api/v1/approvals").json()["items"][0]
    accepted = client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        headers=WRITE_HEADERS,
        json={"decision": "approved", "note": "本机确认"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["approval"]["status"] == "approved"
    assert accepted.json()["run"]["status"] == "queued"
    assert accepted.json()["run"]["blockers"][0]["code"] == "workflow_executor_not_connected"

    duplicate = client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        headers=WRITE_HEADERS,
        json={"decision": "rejected", "note": None},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "approval_already_decided"

    second_run = create_run(client, requires_approval=True).json()
    pending = next(
        item
        for item in client.get("/api/v1/approvals").json()["items"]
        if item["run_id"] == second_run["id"]
    )
    rejected = client.post(
        f"/api/v1/approvals/{pending['id']}/decision",
        headers=WRITE_HEADERS,
        json={"decision": "rejected", "note": "不执行"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["approval"]["status"] == "rejected"
    assert rejected.json()["run"]["status"] == "cancelled"
    assert rejected.json()["run"]["blockers"][0]["code"] == "approval_rejected"


def test_cancelling_awaiting_run_withdraws_pending_approval(client: TestClient) -> None:
    run = create_run(client, requires_approval=True).json()
    assert act(client, run["id"], "cancel").json()["status"] == "cancelled"

    approval = next(
        item
        for item in client.get("/api/v1/approvals").json()["items"]
        if item["run_id"] == run["id"]
    )
    assert approval["status"] == "withdrawn"


def test_disabled_workflow_missing_records_and_request_validation(client: TestClient) -> None:
    disabled = client.post(
        "/api/v1/runs",
        headers=WRITE_HEADERS,
        json={
            "workflow_id": "soul",
            "target_id": "windows-local",
            "instruction": "测试",
            "exact_text": None,
            "requires_approval": True,
            "name": None,
        },
    )
    assert disabled.status_code == 409
    assert disabled.json()["error"]["code"] == "workflow_disabled"

    missing = client.get("/api/v1/runs/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "run_not_found"

    private_exact_text = "PRIVATE_RUN_VALIDATION_INPUT"
    invalid = client.post(
        "/api/v1/runs",
        headers=WRITE_HEADERS,
        json={
            "workflow_id": "windows-general",
            "target_id": "windows-local",
            "instruction": "打开记事本",
            "exact_text": {"private": private_exact_text},
            "requires_approval": False,
        },
    )
    assert invalid.status_code == 422
    assert invalid.json() == {
        "error": {
            "code": "invalid_run_request",
            "message": "运行请求无效。",
        }
    }
    assert private_exact_text not in invalid.text


def test_invalid_approval_validation_is_redacted(client: TestClient) -> None:
    private_note = "PRIVATE_APPROVAL_VALIDATION_INPUT"
    invalid = client.post(
        "/api/v1/approvals/not-a-real-approval/decision",
        headers=WRITE_HEADERS,
        json={"decision": "approved", "note": {"private": private_note}},
    )

    assert invalid.status_code == 422
    assert invalid.json() == {
        "error": {
            "code": "invalid_approval_request",
            "message": "审批请求无效。",
        }
    }
    assert private_note not in invalid.text


def test_event_limit_and_run_filter(client: TestClient) -> None:
    first = create_run(client).json()["id"]
    second = create_run(client).json()["id"]
    act(client, first, "pause")

    filtered = client.get(f"/api/v1/events?run_id={first}&limit=1").json()
    assert filtered["count"] == 1
    assert filtered["items"][0]["run_id"] == first
    assert filtered["items"][0]["type"] == "run_paused"

    other = client.get(f"/api/v1/events?run_id={second}").json()["items"]
    assert all(item["run_id"] == second for item in other)
