from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from ai_game_console.execution import GuiAction
from ai_game_console.game_learning import (
    ActionProposal,
    GameLearner,
    GameLearningError,
    GameProfile,
    InMemoryArtifactStore,
    Observation,
    OutcomeVerification,
    SQLiteLearningStore,
    TransportReceipt,
)


class ScriptedVerifier:
    def __init__(self, outcomes: list[OutcomeVerification]) -> None:
        self.outcomes = iter(outcomes)

    def verify(self, *, before, action, transport, after):
        assert transport.status != "uncertain"
        return next(self.outcomes)


class ScriptedSession:
    def __init__(self, proposals, receipts, outcomes) -> None:
        self.proposals = iter(proposals)
        self.receipts = iter(receipts)
        self.verifier = ScriptedVerifier(outcomes)
        self.execute_calls: list[GuiAction] = []
        self.observation_index = 0
        self.closed = False

    def observe(self):
        self.observation_index += 1
        return Observation(
            payload=f"frame-{self.observation_index}".encode(),
            summary=f"frame {self.observation_index}",
        )

    def propose_action(self, *, instruction, observation, policy_memory):
        assert instruction
        assert observation.summary
        return next(self.proposals)

    def execute(self, action):
        self.execute_calls.append(action)
        return next(self.receipts)

    def close(self):
        self.closed = True


class OneSessionFactory:
    def __init__(self, session: ScriptedSession) -> None:
        self.session = session
        self.open_calls = 0

    def open(self, *, profile, target_id, is_cancelled):
        self.open_calls += 1
        assert profile.profile_id == "test-profile"
        assert target_id == "device-1"
        return self.session


class BlockingProposalSession(ScriptedSession):
    def __init__(self) -> None:
        super().__init__([], [], [])
        self.started = threading.Event()
        self.release = threading.Event()

    def propose_action(self, *, instruction, observation, policy_memory):
        self.started.set()
        assert self.release.wait(1)
        raise RuntimeError("cancelled proposal")


class FailingAfterObservationSession(ScriptedSession):
    def observe(self):
        self.observation_index += 1
        if self.observation_index == 2:
            raise RuntimeError("post observation failed")
        return Observation(payload=b"before", summary="before")


class StableAdapterError(RuntimeError):
    code = "target_not_ready"
    public_message = "目标尚未就绪。"


class FailingEnvironmentFactory:
    def open(self, *, profile, target_id, is_cancelled):
        raise StableAdapterError("private adapter detail must not escape")


def profile() -> GameProfile:
    return GameProfile(
        profile_id="test-profile",
        name="Test profile",
        allowed_actions=("tap",),
        max_actions=4,
        max_duration_seconds=5,
        default_target_id="device-1",
    )


def learner_at(tmp_path: Path, session: ScriptedSession) -> GameLearner:
    return GameLearner(
        store=SQLiteLearningStore(tmp_path / "learning.db"),
        artifacts=InMemoryArtifactStore(),
        environment_factory=OneSessionFactory(session),
        profiles=[profile()],
    )


def wait_terminal(learner: GameLearner, job_id: str):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = learner.inspect(job_id)
        if job.status in {
            "learned",
            "not_learned",
            "failed",
            "stopped",
            "stopped_uncertain",
        }:
            return job
        time.sleep(0.01)
    raise AssertionError("learning job did not finish")


def test_confirmed_success_promotes_only_positive_physical_trajectory(
    tmp_path: Path,
) -> None:
    first = GuiAction(target_id="device-1", action="tap", x=10, y=20)
    second = GuiAction(target_id="device-1", action="tap", x=30, y=40)
    session = ScriptedSession(
        [
            ActionProposal("execute", first),
            ActionProposal("execute", second),
        ],
        [TransportReceipt("accepted"), TransportReceipt("accepted")],
        [
            OutcomeVerification(True, False, 0.1, "confirmed_progress"),
            OutcomeVerification(True, True, 1.0, "confirmed_success"),
        ],
    )
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        accepted = learner.learn("perform safe task", "request-1", profile_id="test-profile")
        finished = wait_terminal(learner, accepted.job_id)
        repeated = learner.learn("perform safe task", "request-1", profile_id="test-profile")
        policy = learner.store.get_policy("test-profile")
        transitions = learner.store.list_transitions(accepted.job_id)
    finally:
        learner.shutdown()

    assert finished.status == "learned"
    assert finished.outcome == "confirmed_success"
    assert finished.policy_state == "promoted"
    assert finished.policy_version == 1
    assert repeated.job_id == accepted.job_id
    assert [item.action for item in policy.trajectory] == [first, second]
    assert all(item.outcome.confirmed for item in transitions)  # type: ignore[union-attr]
    assert session.execute_calls == [first, second]


@pytest.mark.parametrize("transport_status", ["not_sent", "rejected"])
def test_unaccepted_transport_can_never_be_promoted(
    tmp_path: Path,
    transport_status: str,
) -> None:
    action = GuiAction(target_id="device-1", action="tap", x=10, y=20)
    session = ScriptedSession(
        [ActionProposal("execute", action)],
        [TransportReceipt(transport_status)],  # type: ignore[arg-type]
        [OutcomeVerification(True, True, 1.0, "confirmed_success")],
    )
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        accepted = learner.learn(
            "perform safe task",
            f"request-{transport_status}",
            profile_id="test-profile",
        )
        finished = wait_terminal(learner, accepted.job_id)
        policy = learner.store.get_policy("test-profile")
    finally:
        learner.shutdown()

    assert finished.status == "not_learned"
    assert finished.outcome == "confirmed_success"
    assert finished.policy_state == "unchanged"
    assert finished.error_code == "no_positive_transition"
    assert policy.version == 0
    assert policy.trajectory == ()


def test_model_terminate_without_verified_success_is_not_learned(tmp_path: Path) -> None:
    session = ScriptedSession(
        [ActionProposal("terminate")],
        [],
        [OutcomeVerification(False, False, 0.0, "unconfirmed")],
    )
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        job = learner.learn("perform safe task", "request-terminate", profile_id="test-profile")
        finished = wait_terminal(learner, job.job_id)
        policy = learner.store.get_policy("test-profile")
    finally:
        learner.shutdown()

    assert finished.status == "not_learned"
    assert finished.error_code == "termination_unconfirmed"
    assert policy.version == 0
    assert session.execute_calls == []


def test_confirmed_success_without_positive_physical_transition_stays_not_learned(
    tmp_path: Path,
) -> None:
    session = ScriptedSession(
        [ActionProposal("terminate")],
        [],
        [OutcomeVerification(True, True, 1.0, "confirmed_success")],
    )
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        job = learner.learn("perform safe task", "request-confirmed", profile_id="test-profile")
        finished = wait_terminal(learner, job.job_id)
    finally:
        learner.shutdown()

    assert finished.status == "not_learned"
    assert finished.outcome == "confirmed_success"
    assert finished.policy_state == "unchanged"
    assert finished.total_reward == 1.0
    assert finished.verified_successes == 1


def test_confirmed_failure_terminates_without_another_action(tmp_path: Path) -> None:
    first = GuiAction(target_id="device-1", action="tap", x=10, y=20)
    second = GuiAction(target_id="device-1", action="tap", x=30, y=40)
    session = ScriptedSession(
        [ActionProposal("execute", first), ActionProposal("execute", second)],
        [TransportReceipt("accepted"), TransportReceipt("accepted")],
        [OutcomeVerification(True, False, -1.0, "unsafe:scope_violation")],
    )
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        job = learner.learn("perform safe task", "request-unsafe", profile_id="test-profile")
        finished = wait_terminal(learner, job.job_id)
    finally:
        learner.shutdown()

    assert finished.status == "failed"
    assert finished.outcome == "confirmed_failure"
    assert finished.error_code == "unsafe_scene"
    assert session.execute_calls == [first]


def test_cancellation_during_policy_proposal_finishes_stopped(tmp_path: Path) -> None:
    session = BlockingProposalSession()
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        job = learner.learn("perform safe task", "request-cancel", profile_id="test-profile")
        assert session.started.wait(1)
        learner.stop(job.job_id)
        session.release.set()
        finished = wait_terminal(learner, job.job_id)
    finally:
        learner.shutdown()

    assert finished.status == "stopped"
    assert finished.cancel_requested is True
    assert session.execute_calls == []


def test_post_observation_failure_without_physical_transport_is_failed(
    tmp_path: Path,
) -> None:
    session = FailingAfterObservationSession(
        [ActionProposal("terminate")],
        [],
        [],
    )
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        job = learner.learn("perform safe task", "request-post-fail", profile_id="test-profile")
        finished = wait_terminal(learner, job.job_id)
    finally:
        learner.shutdown()

    assert finished.status == "failed"
    assert finished.error_code == "post_action_observation_failed"


def test_preflight_preserves_stable_adapter_code_and_public_message(tmp_path: Path) -> None:
    learner = GameLearner(
        store=SQLiteLearningStore(tmp_path / "learning.db"),
        artifacts=InMemoryArtifactStore(),
        environment_factory=FailingEnvironmentFactory(),
        profiles=[profile()],
    )
    learner.start()
    try:
        job = learner.learn("perform safe task", "request-preflight", profile_id="test-profile")
        finished = wait_terminal(learner, job.job_id)
    finally:
        learner.shutdown()

    assert finished.status == "failed"
    assert finished.error_code == "target_not_ready"
    assert finished.detail == "目标尚未就绪。"


def test_uncertain_transport_stops_and_is_never_replayed(tmp_path: Path) -> None:
    action = GuiAction(target_id="device-1", action="tap", x=10, y=20)
    session = ScriptedSession(
        [ActionProposal("execute", action)],
        [TransportReceipt("uncertain", "timeout")],
        [],
    )
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        job = learner.learn("perform safe task", "request-uncertain", profile_id="test-profile")
        finished = wait_terminal(learner, job.job_id)
    finally:
        learner.shutdown()

    assert finished.status == "stopped_uncertain"
    assert finished.error_code == "transport_uncertain"
    assert session.execute_calls == [action]
    assert learner.store.get_policy("test-profile").version == 0


def test_request_id_conflict_and_profile_action_scope(tmp_path: Path) -> None:
    forbidden = GuiAction(target_id="device-1", action="swipe", x=1, y=1, end_x=2, end_y=2)
    session = ScriptedSession([ActionProposal("execute", forbidden)], [], [])
    learner = learner_at(tmp_path, session)
    learner.start()
    try:
        job = learner.learn("safe task", "request-scope", profile_id="test-profile")
        with pytest.raises(GameLearningError) as conflict:
            learner.learn("different task", "request-scope", profile_id="test-profile")
        finished = wait_terminal(learner, job.job_id)
    finally:
        learner.shutdown()

    assert conflict.value.code == "learning_request_id_conflict"
    assert finished.status == "failed"
    assert finished.error_code == "profile_action_not_allowed"
    assert session.execute_calls == []
