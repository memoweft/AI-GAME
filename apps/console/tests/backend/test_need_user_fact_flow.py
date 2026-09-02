from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_game_console.agent_runtime.fact_questions import FactQuestionCoordinator
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.user_fact_runtime import SQLiteUserFactStore, UserFactService
from ai_game_console.user_fact_runtime.api import create_user_fact_router

from test_agent_session_store import FakeGoalService


def _runtime(tmp_path):
    database = tmp_path / "agent-runtime.db"
    sessions = AgentSessionService(SQLiteAgentRuntimeStore(database), FakeGoalService())
    facts = UserFactService(SQLiteUserFactStore(database))
    return sessions, facts, FactQuestionCoordinator(facts, sessions)


def _two_goal_session(sessions: AgentSessionService):
    created = sessions.create("处理招聘问题", "create-r8")
    updated = sessions.send_message(
        created["id"], "同时整理一个普通目标", "add-r8-other", "add"
    )
    return updated["id"], [item["id"] for item in updated["goal_nodes"]]


def _request(coordinator, session_id: str, goal_id: str, *, key="employment.expected_salary"):
    return coordinator.request_fact(
        session_id=session_id,
        goal_id=goal_id,
        user_scope="local-owner",
        fact_key=key,
        question="HR 问期望薪资，我不知道，你期望多少？",
        why_needed="需要回答当前招聘问题",
        answer_schema={"type": "string", "minLength": 1},
        applicability={"domain": "employment"},
        resume_stage="reply-to-hr",
        dispatch=False,
    )


def test_unknown_fact_creates_one_need_and_waits_only_origin_goal(tmp_path):
    sessions, facts, coordinator = _runtime(tmp_path)
    session_id, goals = _two_goal_session(sessions)

    _, need, created = _request(coordinator, session_id, goals[0])
    _, replay, replay_created = _request(coordinator, session_id, goals[0])

    statuses = {
        item.id: item.status.value for item in sessions.store.goal_nodes(session_id)
    }
    assert created is True and replay_created is False
    assert replay.id == need.id
    assert statuses[goals[0]] == "WAITING_USER_FACT"
    assert statuses[goals[1]] != "WAITING_USER_FACT"
    assert len(facts.store.list_needs(session_id=session_id)) == 1


def test_answer_for_one_goal_does_not_wake_another(tmp_path):
    sessions, facts, coordinator = _runtime(tmp_path)
    session_id, goals = _two_goal_session(sessions)
    _, first, _ = _request(coordinator, session_id, goals[0])
    _, second, _ = _request(coordinator, session_id, goals[1])

    applied, revision, created = coordinator.answer(
        need_id=first.id,
        value="税前 20k-25k",
        idempotency_key="answer-r8-1",
        dispatch=False,
    )

    statuses = {
        item.id: item.status.value for item in sessions.store.goal_nodes(session_id)
    }
    assert created is True
    assert applied.status.value == "APPLIED"
    assert revision.fact_key == "employment.expected_salary"
    assert statuses[goals[0]] == "READY"
    assert statuses[goals[1]] == "WAITING_USER_FACT"
    assert facts.store.get_need(second.id).status.value == "OPEN"


def test_answer_is_idempotent_and_new_session_retrieves_fact(tmp_path):
    sessions, facts, coordinator = _runtime(tmp_path)
    session_id, goals = _two_goal_session(sessions)
    _, need, _ = _request(coordinator, session_id, goals[0])

    first = coordinator.answer(
        need_id=need.id,
        value="税前 20k-25k",
        idempotency_key="answer-r8-1",
        dispatch=False,
    )
    replay = coordinator.answer(
        need_id=need.id,
        value="税前 20k-25k",
        idempotency_key="answer-r8-1",
        dispatch=False,
    )
    resolved = facts.resolve(
        user_scope="local-owner",
        fact_key="employment.expected_salary",
        applicability={"domain": "employment"},
    )

    assert first[1].id == replay[1].id
    assert replay[2] is False
    assert resolved.status.value == "KNOWN"
    assert resolved.revision.id == first[1].id


def test_fact_need_api_lists_and_answers_exact_need(tmp_path):
    sessions, _, coordinator = _runtime(tmp_path)
    session_id, goals = _two_goal_session(sessions)
    _, need, _ = _request(coordinator, session_id, goals[0])
    app = FastAPI()
    app.include_router(create_user_fact_router(coordinator))

    with TestClient(app) as client:
        listed = client.get(f"/api/v3/sessions/{session_id}/fact-needs")
        answered = client.post(
            f"/api/v3/fact-needs/{need.id}/answers",
            json={"value": "税前 20k-25k", "idempotency_key": "answer-api-1"},
        )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["why_needed"] == "需要回答当前招聘问题"
    assert answered.status_code == 200
    assert answered.json()["need"]["status"] == "APPLIED"
    assert answered.json()["need"]["resume_stage_id"] == "reply-to-hr"
