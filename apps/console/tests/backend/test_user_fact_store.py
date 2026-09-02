from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_game_console.user_fact_runtime import (
    FactResolutionStatus,
    NeedUserFactStatus,
    SQLiteUserFactStore,
    UntrustedFactSource,
    UserFactAnswerValidationError,
    UserFactIdempotencyConflict,
    UserFactService,
    UserFactSourceKind,
)


def _runtime(tmp_path: Path) -> tuple[SQLiteUserFactStore, UserFactService]:
    store = SQLiteUserFactStore(tmp_path / "agent-runtime.db")
    return store, UserFactService(store)


def _trusted_revision(
    service: UserFactService,
    *,
    value: object,
    applicability: dict[str, object] | None = None,
    source_ref: str = "import:profile-v1",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
):
    return service.record_trusted_revision(
        user_scope="owner",
        fact_key="employment.expected_salary",
        value=value,
        applicability=applicability or {},
        source_kind=UserFactSourceKind.IMPORTED,
        source_ref=source_ref,
        provenance={"artifact": source_ref},
        valid_from=valid_from,
        valid_until=valid_until,
    )


def test_initialize_is_repeatable_and_coexists_with_agent_runtime_schema(tmp_path: Path):
    database_path = tmp_path / "agent-runtime.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE agent_runtime_schema(singleton INTEGER PRIMARY KEY, revision INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO agent_runtime_schema VALUES (1, 6)")

    SQLiteUserFactStore(database_path).initialize()
    SQLiteUserFactStore(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT revision FROM agent_runtime_schema WHERE singleton=1"
        ).fetchone()[0] == 6
        assert connection.execute(
            "SELECT revision FROM user_fact_runtime_schema WHERE singleton=1"
        ).fetchone()[0] == 1


def test_new_revision_supersedes_same_applicability_without_deleting_history(tmp_path: Path):
    store, service = _runtime(tmp_path)
    first = _trusted_revision(service, value=20_000, applicability={"country": "CN"})
    second = _trusted_revision(
        service,
        value=25_000,
        applicability={"country": "CN"},
        source_ref="import:profile-v2",
    )

    resolution = service.resolve(
        user_scope="owner",
        fact_key="employment.expected_salary",
        applicability={"country": "CN", "app": "recruitment"},
    )

    assert second.fact_id == first.fact_id
    assert (first.revision, second.revision) == (1, 2)
    assert [item.value for item in store.revisions(
        user_scope="owner", fact_key="employment.expected_salary"
    )] == [20_000, 25_000]
    assert resolution.status is FactResolutionStatus.KNOWN
    assert resolution.revision == second


def test_resolution_distinguishes_unknown_stale_conflicting_and_known(tmp_path: Path):
    _, service = _runtime(tmp_path)
    query = {"app": "soul", "conversation_kind": "hr"}

    assert service.resolve(
        user_scope="owner", fact_key="missing", applicability=query
    ).status is FactResolutionStatus.UNKNOWN

    now = datetime.now(UTC)
    _trusted_revision(
        service,
        value=18_000,
        applicability={"app": "expired"},
        valid_from=now - timedelta(days=2),
        valid_until=now - timedelta(days=1),
    )
    assert service.resolve(
        user_scope="owner",
        fact_key="employment.expected_salary",
        applicability={"app": "expired"},
        at=now,
    ).status is FactResolutionStatus.STALE

    _trusted_revision(service, value=20_000, applicability={"app": "soul"})
    _trusted_revision(
        service,
        value=30_000,
        applicability={"conversation_kind": "hr"},
        source_ref="import:other-profile",
    )
    conflict = service.resolve(
        user_scope="owner",
        fact_key="employment.expected_salary",
        applicability=query,
    )
    assert conflict.status is FactResolutionStatus.CONFLICTING
    assert {item.value for item in conflict.candidates} == {20_000, 30_000}

    known = service.resolve(
        user_scope="owner",
        fact_key="employment.expected_salary",
        applicability={"app": "soul"},
    )
    assert known.status is FactResolutionStatus.KNOWN
    assert known.revision is not None and known.revision.value == 20_000


def test_open_need_is_deduplicated_and_persists_resume_and_hint_as_non_identity_data(tmp_path: Path):
    store, service = _runtime(tmp_path)
    arguments = dict(
        session_id="session-1",
        goal_id="goal-recruitment",
        user_scope="owner",
        fact_key="employment.expected_salary",
        question="HR 问期望薪资，我不知道，你期望多少？",
        why_needed="需要回复当前招聘对话",
        answer_schema={"type": "integer", "minimum": 0},
        applicability={"country": "CN"},
        resume_stage="fresh_observe_before_reply",
        conversation_hint="招聘 App 当前会话（仅 hint）",
    )

    first_resolution, first, created = service.resolve_or_create_need(**arguments)
    second_resolution, replay, replay_created = service.resolve_or_create_need(**arguments)

    assert first_resolution.status is FactResolutionStatus.UNKNOWN
    assert second_resolution.status is FactResolutionStatus.UNKNOWN
    assert created is True and replay_created is False
    assert first is not None and replay is not None and replay.id == first.id
    assert replay.resume_stage == "fresh_observe_before_reply"
    assert replay.conversation_hint == "招聘 App 当前会话（仅 hint）"
    assert store.list_facts(user_scope="owner") == []


def test_answer_is_atomic_idempotent_and_creates_one_append_only_revision(tmp_path: Path):
    store, service = _runtime(tmp_path)
    _, need, _ = service.resolve_or_create_need(
        session_id="session-1", goal_id="goal-1", user_scope="owner",
        fact_key="employment.expected_salary", question="期望薪资是多少？",
        why_needed="回复 HR", answer_schema={"type": "integer"},
        applicability={"country": "CN"}, resume_stage="observe_again",
    )
    assert need is not None

    answered, revision, created = service.answer(
        need_id=need.id, value=25_000, idempotency_key="answer-1"
    )
    replay_need, replay_revision, replay_created = service.answer(
        need_id=need.id, value=25_000, idempotency_key="answer-1"
    )

    assert created is True and replay_created is False
    assert answered.status is NeedUserFactStatus.ANSWERED
    assert replay_need.id == answered.id
    assert replay_revision.id == revision.id
    assert revision.source_kind is UserFactSourceKind.USER_ANSWER
    assert revision.source_ref == f"need:{need.id}"
    assert revision.provenance["need_id"] == need.id
    assert len(store.revisions(
        user_scope="owner", fact_key="employment.expected_salary"
    )) == 1

    with pytest.raises(UserFactIdempotencyConflict):
        service.answer(need_id=need.id, value=30_000, idempotency_key="answer-1")


def test_answer_schema_is_validated_before_revision_is_written(tmp_path: Path):
    store, service = _runtime(tmp_path)
    _, need, _ = service.resolve_or_create_need(
        session_id="session-1", goal_id="goal-1", user_scope="owner",
        fact_key="employment.expected_salary", question="期望薪资是多少？",
        why_needed="回复 HR", answer_schema={"type": "integer", "minimum": 1},
        applicability={}, resume_stage="observe_again",
    )
    assert need is not None
    with pytest.raises(UserFactAnswerValidationError):
        service.answer(need_id=need.id, value="两万", idempotency_key="answer-invalid")
    assert store.get_need(need.id).status is NeedUserFactStatus.OPEN
    assert store.revisions(user_scope="owner", fact_key=need.fact_key) == []


def test_answered_need_can_be_applied_once_and_survives_restart(tmp_path: Path):
    store, service = _runtime(tmp_path)
    _, need, _ = service.resolve_or_create_need(
        session_id="session-1", goal_id="goal-1", user_scope="owner",
        fact_key="timezone", question="你的时区？", why_needed="安排提醒",
        answer_schema={"type": "string"}, applicability={}, resume_stage="replan",
    )
    assert need is not None
    service.answer(need_id=need.id, value="Asia/Shanghai", idempotency_key="answer-1")
    applied = store.mark_need_applied(need.id)
    replay = store.mark_need_applied(need.id)

    restarted = SQLiteUserFactStore(tmp_path / "agent-runtime.db")
    restored = restarted.get_need(need.id)
    assert applied.status is replay.status is restored.status is NeedUserFactStatus.APPLIED
    assert restored.applied_at is not None


def test_concurrent_answer_replay_across_store_instances_creates_one_revision(tmp_path: Path):
    database_path = tmp_path / "agent-runtime.db"
    first_service = UserFactService(SQLiteUserFactStore(database_path))
    _, need, _ = first_service.resolve_or_create_need(
        session_id="session-1", goal_id="goal-1", user_scope="owner",
        fact_key="timezone", question="你的时区？", why_needed="安排提醒",
        answer_schema={"type": "string"}, applicability={}, resume_stage="replan",
    )
    assert need is not None

    def answer_once(_: int):
        service = UserFactService(SQLiteUserFactStore(database_path))
        return service.answer(
            need_id=need.id, value="Asia/Shanghai", idempotency_key="answer-1"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        answers = list(executor.map(answer_once, range(2)))

    assert sorted(created for _, _, created in answers) == [False, True]
    revisions = SQLiteUserFactStore(database_path).revisions(
        user_scope="owner", fact_key="timezone"
    )
    assert len(revisions) == 1


def test_later_session_resolves_persisted_applicable_revision(tmp_path: Path):
    database_path = tmp_path / "agent-runtime.db"
    first_service = UserFactService(SQLiteUserFactStore(database_path))
    _trusted_revision(
        first_service, value=25_000, applicability={"country": "CN"}
    )

    restarted_for_new_session = UserFactService(SQLiteUserFactStore(database_path))
    resolution = restarted_for_new_session.resolve(
        user_scope="owner",
        fact_key="employment.expected_salary",
        applicability={"country": "CN", "app": "recruitment"},
    )
    assert resolution.status is FactResolutionStatus.KNOWN
    assert resolution.revision is not None and resolution.revision.value == 25_000


def test_model_or_bare_user_answer_cannot_bypass_trusted_write_boundary(tmp_path: Path):
    store, service = _runtime(tmp_path)
    with pytest.raises(UntrustedFactSource):
        service.record_trusted_revision(
            user_scope="owner", fact_key="employment.expected_salary", value=99_999,
            applicability={}, source_kind=UserFactSourceKind.USER_ANSWER,
            source_ref="model-proposal", provenance={"model": "qwen"},
        )
    with pytest.raises(UntrustedFactSource):
        service.record_trusted_revision(
            user_scope="owner", fact_key="employment.expected_salary", value=99_999,
            applicability={}, source_kind=UserFactSourceKind.OBSERVED,
            source_ref="", provenance={},
        )
    assert store.list_facts(user_scope="owner") == []
