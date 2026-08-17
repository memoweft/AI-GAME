from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_game_console.execution import GuiAction
from ai_game_console.game_learning.domain import (
    ActionProposal,
    ArtifactRef,
    DistilledTransition,
    OutcomeVerification,
    TransportReceipt,
)
from ai_game_console.game_learning.store import (
    LearningIdempotencyConflict,
    LearningStoreError,
    SQLiteLearningStore,
)


def accept(store: SQLiteLearningStore, *, request_id: str = "request-1"):
    return store.accept_job(
        job_id="job-1",
        profile_id="stzb-tutorial-v1",
        profile_revision=1,
        target_id="127.0.0.1:16384",
        instruction="打开任务列表",
        client_request_id=request_id,
        request_digest="digest-1",
    )


def test_store_is_isolated_and_idempotent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="console database"):
        SQLiteLearningStore(tmp_path / "console.db")

    store = SQLiteLearningStore(tmp_path / "learning.db")
    first, created = accept(store)
    repeated, repeated_created = accept(store)

    assert created is True
    assert repeated_created is False
    assert repeated.job_id == first.job_id
    assert repeated.request_digest == "digest-1"
    with pytest.raises(LearningIdempotencyConflict):
        store.accept_job(
            job_id="job-2",
            profile_id="stzb-tutorial-v1",
            profile_revision=1,
            target_id=None,
            instruction="不同请求",
            client_request_id="request-1",
            request_digest="different",
        )


def test_store_rejects_newer_schema_version(tmp_path: Path) -> None:
    database = tmp_path / "future-learning.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 2")
    with pytest.raises(LearningStoreError, match="newer than supported"):
        SQLiteLearningStore(database).initialize()


def test_transition_events_are_append_only_and_policy_promotion_is_atomic(
    tmp_path: Path,
) -> None:
    database = tmp_path / "learning.db"
    store = SQLiteLearningStore(database)
    job, _ = accept(store)
    assert store.claim_job(job.job_id).status == "running"  # type: ignore[union-attr]
    action = GuiAction(target_id="127.0.0.1:16384", action="tap", x=10, y=20)
    before = ArtifactRef("a" * 64, "job-1/before.png", 100, "image/png")
    after = ArtifactRef("b" * 64, "job-1/after.png", 120, "image/png")

    intent = store.append_intent(
        transition_id="transition-1",
        job_id=job.job_id,
        sequence=1,
        proposal=ActionProposal(kind="execute", action=action),
        before=before,
    )
    assert intent.finalized is False
    finalized = store.finalize_transition(
        transition_id="transition-1",
        after=after,
        transport=TransportReceipt("accepted", "adb accepted"),
        outcome=OutcomeVerification(True, True, 1.0, "confirmed_success"),
    )
    assert finalized.finalized is True
    assert finalized.transport.status == "accepted"  # type: ignore[union-attr]
    assert finalized.outcome.task_succeeded is True  # type: ignore[union-attr]

    learned = store.complete_learned(
        job.job_id,
        distilled=(
            DistilledTransition(
                action,
                1.0,
                before.sha256,
                after.sha256,
                "confirmed_success",
            ),
        ),
        detail="promoted",
    )
    policy = store.get_policy(job.profile_id)
    assert learned.status == "learned"
    assert learned.outcome == "confirmed_success"
    assert learned.policy_state == "promoted"
    assert learned.total_reward == 1.0
    assert learned.verified_successes == 1
    assert learned.policy_version == 1
    assert policy.version == 1
    assert policy.trajectory[0].action == action

    with sqlite3.connect(database) as connection:
        phases = connection.execute(
            "SELECT phase FROM learning_transition_events ORDER BY event_id"
        ).fetchall()
        stored = database.read_bytes()
    assert phases == [("intent",), ("finalized",)]
    assert b"\x89PNG" not in stored


def test_store_refuses_promotion_without_accepted_transition_provenance(
    tmp_path: Path,
) -> None:
    store = SQLiteLearningStore(tmp_path / "learning.db")
    job, _ = accept(store)
    store.claim_job(job.job_id)
    action = GuiAction(target_id="127.0.0.1:16384", action="tap", x=10, y=20)
    before = ArtifactRef("a" * 64, "job-1/before.png", 100, "image/png")
    after = ArtifactRef("b" * 64, "job-1/after.png", 120, "image/png")
    store.append_intent(
        transition_id="transition-rejected",
        job_id=job.job_id,
        sequence=1,
        proposal=ActionProposal(kind="execute", action=action),
        before=before,
    )
    store.finalize_transition(
        transition_id="transition-rejected",
        after=after,
        transport=TransportReceipt("rejected"),
        outcome=OutcomeVerification(True, True, 1.0, "confirmed_success"),
    )

    with pytest.raises(LearningStoreError, match="accepted positive Transition provenance"):
        store.complete_learned(
            job.job_id,
            distilled=(
                DistilledTransition(
                    action,
                    1.0,
                    before.sha256,
                    after.sha256,
                    "confirmed_success",
                ),
            ),
            detail="must not promote",
        )

    assert store.get_policy(job.profile_id).version == 0


def test_policy_memory_revisions_are_immutable_rows(tmp_path: Path) -> None:
    database = tmp_path / "learning.db"
    store = SQLiteLearningStore(database)
    action = GuiAction(target_id="device", action="tap", x=1, y=2)
    for index in (1, 2):
        job_id = f"job-{index}"
        job, _ = store.accept_job(
            job_id=job_id,
            profile_id="stzb-tutorial-v1",
            profile_revision=1,
            target_id="device",
            instruction="打开任务列表",
            client_request_id=f"request-{index}",
            request_digest=f"digest-{index}",
        )
        store.claim_job(job_id)
        transition_id = f"transition-{index}"
        store.append_intent(
            transition_id=transition_id,
            job_id=job_id,
            sequence=1,
            proposal=ActionProposal("execute", action),
            before=None,
        )
        store.finalize_transition(
            transition_id=transition_id,
            after=None,
            transport=TransportReceipt("accepted"),
            outcome=OutcomeVerification(True, True, 1.0, "confirmed_success"),
        )
        store.complete_learned(
            job.job_id,
            distilled=(
                DistilledTransition(
                    action,
                    1.0,
                    None,
                    None,
                    "confirmed_success",
                ),
            ),
            detail="promoted",
        )

    with sqlite3.connect(database) as connection:
        revisions = connection.execute(
            """
            SELECT version, trajectory_json FROM learning_policy_memory
            WHERE profile_id = 'stzb-tutorial-v1' ORDER BY version
            """
        ).fetchall()
    assert [item[0] for item in revisions] == [1, 2]
    assert len(revisions[0][1]) < len(revisions[1][1])
    assert store.get_policy("stzb-tutorial-v1").version == 2


def test_restart_terminates_open_intent_as_uncertain_without_replay(tmp_path: Path) -> None:
    store = SQLiteLearningStore(tmp_path / "learning.db")
    job, _ = accept(store)
    store.claim_job(job.job_id)
    action = GuiAction(target_id="127.0.0.1:16384", action="tap", x=10, y=20)
    store.append_intent(
        transition_id="transition-open",
        job_id=job.job_id,
        sequence=1,
        proposal=ActionProposal(kind="execute", action=action),
        before=None,
    )

    restarted = SQLiteLearningStore(store.database_path)
    assert restarted.recover_active_jobs() == 1
    recovered = restarted.get_job(job.job_id)
    assert recovered is not None
    assert recovered.status == "stopped_uncertain"
    assert recovered.error_code == "coordinator_restarted"
    assert len(restarted.list_transitions(job.job_id)) == 1
    assert restarted.list_transitions(job.job_id)[0].finalized is False
