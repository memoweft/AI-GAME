from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from ai_game_console.api import create_app
from ai_game_console.android_chat_adapter import RepositoryAndroidAutomationFactory
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.game_learning.android_adapter import StzbAndroidEnvironmentFactory

from conftest import WRITE_HEADERS, build_settings


class FakeGameLearner:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.learn_calls: list[dict[str, object]] = []
        self.jobs: dict[str, dict[str, object]] = {}

    def start(self) -> None:
        self.started += 1

    def shutdown(self) -> None:
        self.stopped += 1

    def list_profiles(self):
        return [
            {
                "profile_id": "stzb-tutorial-v1",
                "name": "率土之滨低频教程与菜单导航",
                "revision": 1,
                "allowed_actions": ("tap", "keyevent", "swipe"),
                "max_actions": 25,
                "max_duration_seconds": 180.0,
                "default_target_id": None,
            }
        ]

    def learn(
        self,
        instruction: str,
        *,
        client_request_id: str,
        profile_id: str,
        target_id: str | None,
    ):
        call = {
            "instruction": instruction,
            "client_request_id": client_request_id,
            "profile_id": profile_id,
            "target_id": target_id,
        }
        self.learn_calls.append(call)
        job = {
            "job_id": f"job-{len(self.learn_calls)}",
            "profile_id": profile_id,
            "profile_revision": 1,
            "target_id": target_id,
            "client_request_id": client_request_id,
            "status": "queued",
            "outcome": "unknown",
            "policy_state": "unchanged",
            "cancel_requested": False,
            "transition_count": 0,
            "policy_version": 0,
            "policy_memory_count": 0,
            "total_reward": 0.0,
            "verified_successes": 0,
            "detail": None,
            "error_code": None,
            "created_at": "2026-08-09T12:00:00.000Z",
            "started_at": None,
            "finished_at": None,
            "updated_at": "2026-08-09T12:00:00.000Z",
        }
        self.jobs[str(job["job_id"])] = job
        return job

    def list_jobs(self, limit: int = 100):
        return list(self.jobs.values())[:limit]

    def inspect(self, job_id: str):
        return self.jobs[job_id]

    def stop(self, job_id: str):
        job = self.jobs[job_id]
        job.update({"status": "stopping", "cancel_requested": True})
        return job


def build_learning_app(tmp_path: Path, learner: FakeGameLearner | None = None):
    settings = build_settings(tmp_path)
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        game_learner=learner,
    )
    return app


def test_game_learning_routes_and_lifecycle(tmp_path: Path) -> None:
    learner = FakeGameLearner()
    app = build_learning_app(tmp_path, learner)

    with TestClient(app) as client:
        assert learner.started == 1

        profiles = client.get("/api/v1/learning/profiles")
        assert profiles.status_code == 200
        assert profiles.json() == {
            "items": [
                {
                    "id": "stzb-tutorial-v1",
                    "name": "率土之滨低频教程与菜单导航",
                    "game": "率土之滨",
                    "scope_summary": "固定测试环境中的低频教程与只读菜单导航。",
                    "safety_summary": (
                        "登录、协议、实名、验证码、支付、购买、招募、领取、聊天、联盟、"
                        "匹配、真人交互和账号设置均不在允许范围内。"
                    ),
                    "budget_summary": (
                        "每个 LearningEpisode 最多 25 个 Transition，最长 180 秒。"
                    ),
                    "revision": 1,
                    "allowed_actions": ["tap", "keyevent", "swipe"],
                    "max_transitions": 25,
                    "max_duration_seconds": 180.0,
                    "default_target_id": None,
                }
            ],
            "count": 1,
        }

        accepted = client.post(
            "/api/v1/learning/jobs",
            headers=WRITE_HEADERS,
            json={
                "instruction": "  学会从固定主城页进入征兵页  ",
                "client_request_id": "learning-request-1",
            },
        )
        assert accepted.status_code == 202
        assert accepted.json()["phase"] == "accepted"
        assert accepted.json()["result"] == "pending"
        assert accepted.json()["client_request_id"] == "learning-request-1"
        assert accepted.json()["max_transitions"] == 25
        assert "instruction" not in accepted.json()
        assert learner.learn_calls == [
            {
                "instruction": "学会从固定主城页进入征兵页",
                "client_request_id": "learning-request-1",
                "profile_id": "stzb-tutorial-v1",
                "target_id": None,
            }
        ]

        jobs = client.get("/api/v1/learning/jobs")
        assert jobs.status_code == 200
        assert jobs.json()["count"] == 1
        job_id = accepted.json()["id"]

        inspected = client.get(f"/api/v1/learning/jobs/{job_id}")
        assert inspected.status_code == 200
        assert inspected.json()["id"] == job_id

        stopping = client.post(
            f"/api/v1/learning/jobs/{job_id}/stop",
            headers=WRITE_HEADERS,
        )
        assert stopping.status_code == 202
        assert stopping.json()["phase"] == "stopping"
        assert stopping.json()["cancel_requested"] is True

    assert learner.stopped == 1


def test_job_projection_prefers_independent_core_outcome_and_policy_fields(
    tmp_path: Path,
) -> None:
    learner = FakeGameLearner()
    with TestClient(build_learning_app(tmp_path, learner)) as client:
        accepted = client.post(
            "/api/v1/learning/jobs",
            headers=WRITE_HEADERS,
            json={
                "instruction": "打开任务列表",
                "client_request_id": "independent-truth-1",
            },
        ).json()
        stored = learner.jobs[accepted["id"]]
        stored.update(
            {
                "status": "not_learned",
                "profile_revision": 7,
                "outcome": "confirmed_success",
                "policy_state": "unchanged",
                "total_reward": 1.25,
                "verified_successes": 1,
                "policy_version": 4,
                "policy_memory_count": 3,
                "updated_at": "2026-08-09T12:01:00.000Z",
            }
        )

        projected = client.get(f"/api/v1/learning/jobs/{accepted['id']}")

    assert projected.status_code == 200
    payload = projected.json()
    assert payload["result"] == "not_learned"
    assert payload["outcome"] == "confirmed_success"
    assert payload["policy_state"] == "unchanged"
    assert payload["profile_revision"] == 7
    assert payload["total_reward"] == 1.25
    assert payload["verified_successes"] == 1
    assert payload["policy_memory_revision"] == 4
    assert payload["policy_memory_count"] == 3
    assert payload["updated_at"] == "2026-08-09T12:01:00.000Z"


def test_game_learning_create_forwards_optional_profile_and_target(tmp_path: Path) -> None:
    learner = FakeGameLearner()
    with TestClient(build_learning_app(tmp_path, learner)) as client:
        accepted = client.post(
            "/api/v1/learning/jobs",
            headers=WRITE_HEADERS,
            json={
                "instruction": "打开任务列表",
                "client_request_id": "learning-request-explicit",
                "profile_id": "stzb-tutorial-v1",
                "target_id": "android:emulator-1",
            },
        )

    assert accepted.status_code == 202
    assert learner.learn_calls == [
        {
            "instruction": "打开任务列表",
            "client_request_id": "learning-request-explicit",
            "profile_id": "stzb-tutorial-v1",
            "target_id": "android:emulator-1",
        }
    ]
    assert accepted.json()["target_id"] == "android:emulator-1"


def test_learning_writes_require_console_header(tmp_path: Path) -> None:
    learner = FakeGameLearner()
    with TestClient(build_learning_app(tmp_path, learner)) as client:
        create = client.post(
            "/api/v1/learning/jobs",
            json={
                "instruction": "学习教程导航",
                "client_request_id": "learning-request-2",
            },
        )
        assert create.status_code == 403
        assert learner.learn_calls == []


def test_learning_validation_redacts_instruction(tmp_path: Path) -> None:
    learner = FakeGameLearner()
    secret_instruction = "不要回显这条学习指令"
    with TestClient(build_learning_app(tmp_path, learner)) as client:
        response = client.post(
            "/api/v1/learning/jobs",
            headers=WRITE_HEADERS,
            json={
                "instruction": secret_instruction,
                "client_request_id": "contains spaces",
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_game_learning_request",
            "message": "游戏学习请求无效。",
        }
    }
    assert secret_instruction not in response.text
    assert learner.learn_calls == []


def test_default_composition_uses_isolated_store_and_fail_closed_environment(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )
    learner = app.state.game_learner

    assert learner.store.database_path == settings.data_dir / "learning.db"
    assert learner.artifacts.root == (
        settings.project_root / "runtime" / "sessions" / "game-learning"
    ).resolve()

    with TestClient(app) as client:
        profiles = client.get("/api/v1/learning/profiles")
        assert profiles.status_code == 200
        assert profiles.json()["items"][0]["id"] == "stzb-tutorial-v1"

        rejected_instruction = "这条指令不应出现在错误响应中"
        rejected = client.post(
            "/api/v1/learning/jobs",
            headers=WRITE_HEADERS,
            json={
                "instruction": rejected_instruction,
                "client_request_id": "default-composition-missing-profile",
                "profile_id": "missing-profile",
            },
        )
        assert rejected.status_code == 404
        assert rejected.json()["error"]["code"] == "game_profile_not_found"
        assert rejected_instruction not in rejected.text

        accepted = client.post(
            "/api/v1/learning/jobs",
            headers=WRITE_HEADERS,
            json={
                "instruction": "打开任务列表",
                "client_request_id": "default-composition-1",
            },
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["id"]

        deadline = time.monotonic() + 2
        terminal = accepted.json()
        while time.monotonic() < deadline and terminal["phase"] != "terminal":
            terminal = client.get(f"/api/v1/learning/jobs/{job_id}").json()
            time.sleep(0.005)

        assert terminal["phase"] == "terminal"
        assert terminal["result"] == "failed"
        assert terminal["error_code"] == "learning_environment_not_configured"

    assert (settings.data_dir / "learning.db").is_file()


def test_production_composition_shares_one_device_lease_between_chat_and_learning(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        gui_executor_enabled=True,
        adb_path=str(tmp_path / "adb.exe"),
        adb_serial="emulator-5554",
        local_chat_endpoint="http://127.0.0.1:9999/v1",
        local_chat_model="gui-owl",
    )

    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )
    lease = app.state.device_execution_lease
    chat_factory = app.state.chat_coordinator.automation_factory
    learning_factory = app.state.game_learner.environment_factory

    assert isinstance(chat_factory, RepositoryAndroidAutomationFactory)
    assert isinstance(learning_factory, StzbAndroidEnvironmentFactory)
    assert chat_factory.device_lease is lease
    assert learning_factory.device_lease is lease
