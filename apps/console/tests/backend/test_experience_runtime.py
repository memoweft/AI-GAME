from __future__ import annotations

from pathlib import Path
import time
from types import SimpleNamespace
import binascii
import struct
import zlib

import pytest

from ai_game_console.experience_runtime import ExperienceService, SQLiteExperienceStore
from ai_game_console.experience_runtime.benchmark import (
    BenchmarkSuiteResult, TrialMetrics, compare_trials,
)
from ai_game_console.experience_runtime.domain import ScopeKey
from ai_game_console.mobile_agent.domain import (
    ActionDecision, ActionAttempt, Observation, PhysicalIntent,
    PlanDraft, TransportReceipt, Verification,
)
from ai_game_console.mobile_agent import MobileTaskRuntime
from ai_game_console.execution import GuiAction


SCOPE = ScopeKey(
    user_scope="local-user", account_scope="fixture-account",
    application_id="fixture.app", goal_family="fixture/daily",
    ui_version="1.0",
)


def _attempt(
    sequence: int, *, before_ref: str, before_summary: str,
    after_ref: str, after_summary: str, evidence: str,
    satisfied: bool, progress: bool, action: str = "tap",
    target_description: str | None = None,
) -> ActionAttempt:
    intent = (
        PhysicalIntent("keyevent", {"keycode": 4}) if action == "back"
        else PhysicalIntent("tap", {
            "x": 80, "y": 240,
            **({"target_description": target_description} if target_description else {}),
        })
    )
    return ActionAttempt(
        attempt_id=f"{before_ref}-attempt-{sequence}", sequence=sequence,
        plan_revision=1, subgoal_index=0, input_revision=0,
        decision=ActionDecision("act", intent),
        before=Observation(before_ref, before_summary),
        transport=TransportReceipt("accepted", receipt_id=f"receipt-{sequence}"),
        after=Observation(after_ref, after_summary),
        verification=Verification(satisfied, progress, evidence=evidence),
        created_at="2026-08-21T00:00:00+00:00",
        finalized_at="2026-08-21T00:00:01+00:00",
    )


def _service(tmp_path: Path) -> ExperienceService:
    return ExperienceService(SQLiteExperienceStore(tmp_path / "experience.db"))


def _png(width: int, height: int, *, invert: bool = False) -> bytes:
    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(
            ">I", binascii.crc32(name + data) & 0xFFFFFFFF
        )
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            value = int(255 * x / max(1, width - 1))
            if invert:
                value = 255 - value
            rows.extend((value, (value + y * 7) % 256, 255 - value))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )


def test_append_only_episode_transition_signal_and_restart(tmp_path: Path) -> None:
    service = _service(tmp_path)
    episode = service.begin_mobile_episode(
        goal_run_id="goal-1", source_task_id="task-1", goal_spec_revision=2,
        frozen_criteria_ids=("open_tasks",), scope=SCOPE,
    )
    assert episode is not None
    wrong = service.record_mobile_attempt(
        source_task_id="task-1", objective="open tasks",
        attempt=_attempt(
            1, before_ref="main-1", before_summary="main city 720x1280",
            after_ref="detail-1", after_summary="detail 720x1280",
            evidence="wrong page: character detail", satisfied=False, progress=True,
        ),
    )
    recovery = service.record_mobile_attempt(
        source_task_id="task-1", objective="open tasks",
        attempt=_attempt(
            2, before_ref="detail-1", before_summary="detail 720x1280",
            after_ref="main-1", after_summary="main city 720x1280",
            evidence="returned to main city", satisfied=True, progress=True,
            action="back",
        ),
    )
    assert wrong is not None and wrong.failure_class == "wrong_scene"
    assert recovery is not None and recovery.immediate_outcome == "recovered"
    assert recovery.recovery_of_transition_id == wrong.transition_id

    reopened = SQLiteExperienceStore(tmp_path / "experience.db")
    assert reopened.counts() == {
        "episodes": 1, "scenes": 2, "transitions": 2, "outcomes": 2,
        "candidates": 2, "policies": 0, "retrievals": 0, "usage": 0,
    }


def test_uncertain_never_becomes_candidate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.begin_mobile_episode(
        goal_run_id="goal-u", source_task_id="task-u", goal_spec_revision=1,
        frozen_criteria_ids=("criterion",), scope=SCOPE,
    )
    attempt = _attempt(
        1, before_ref="a", before_summary="loading 720x1280",
        after_ref="b", after_summary="obscured 720x1280",
        evidence="observation obscured", satisfied=False, progress=False,
    )
    attempt = ActionAttempt(
        attempt.attempt_id, attempt.sequence, attempt.plan_revision,
        attempt.subgoal_index, attempt.input_revision, attempt.decision,
        attempt.before, attempt.transport, attempt.after,
        Verification(False, False, uncertain=True, evidence="obscured"),
        attempt.created_at, attempt.finalized_at,
    )
    transition = service.record_mobile_attempt(
        source_task_id="task-u", objective="open tasks", attempt=attempt,
    )
    assert transition is not None and transition.immediate_outcome == "uncertain"
    assert service.store.counts()["candidates"] == 0


def test_goal_verified_promotion_retrieval_use_scope_and_rollback(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.begin_mobile_episode(
        goal_run_id="goal-cold", source_task_id="task-cold", goal_spec_revision=1,
        frozen_criteria_ids=("open_tasks",), scope=SCOPE,
    )
    cold = service.record_mobile_attempt(
        source_task_id="task-cold", objective="open tasks",
        attempt=_attempt(
            1, before_ref="main-cold", before_summary="main city 720x1280",
            after_ref="tasks-cold", after_summary="task list 720x1280",
            evidence="task list visible", satisfied=True, progress=True,
        ),
    )
    assert cold is not None
    policy = service.confirm_goal_completion(
        "task-cold", goal_id="goal-cold", completion_revision=1
    )
    assert policy is not None and policy.revision == 1

    service.begin_mobile_episode(
        goal_run_id="goal-warm", source_task_id="task-warm", goal_spec_revision=1,
        frozen_criteria_ids=("open_tasks",), scope=SCOPE,
    )
    packet = service.retrieve(
        source_task_id="task-warm", objective="open tasks",
        observation=Observation("main-warm", "main city 720x1280"),
    )
    assert len(packet.items) == 1
    warm = service.record_mobile_attempt(
        source_task_id="task-warm", objective="open tasks", retrieval=packet,
        attempt=_attempt(
            1, before_ref="main-warm", before_summary="main city 720x1280",
            after_ref="tasks-warm", after_summary="task list 720x1280",
            evidence="task list visible", satisfied=True, progress=True,
        ),
    )
    assert warm is not None
    assert service.store.counts()["usage"] == 1

    other_scope = ScopeKey(
        user_scope="other-user", account_scope="fixture-account",
        application_id="fixture.app", goal_family="fixture/daily", ui_version="1.0",
    )
    service.begin_mobile_episode(
        goal_run_id="goal-other", source_task_id="task-other", goal_spec_revision=1,
        frozen_criteria_ids=("open_tasks",), scope=other_scope,
    )
    assert service.retrieve(
        source_task_id="task-other", objective="open tasks",
        observation=Observation("main-other", "main city 720x1280"),
    ).empty

    service.confirm_goal_completion("task-warm", goal_id="goal-warm", completion_revision=1)
    rollback = service.rollback(SCOPE, to_revision=1)
    assert rollback.revision == 2 and rollback.rollback_of_revision == 1


def test_candidate_reject_deprecate_and_legacy_validation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.begin_mobile_episode(
        goal_run_id="goal-1", source_task_id="task-1", goal_spec_revision=1,
        frozen_criteria_ids=("c",), scope=SCOPE,
    )
    transition = service.record_mobile_attempt(
        source_task_id="task-1", objective="open tasks",
        attempt=_attempt(
            1, before_ref="a", before_summary="main city 720x1280",
            after_ref="b", after_summary="same 720x1280",
            evidence="no effect", satisfied=False, progress=False,
        ),
    )
    assert transition is not None
    candidate = service.store.candidates_for_transition(transition.transition_id)[0]
    with pytest.raises(ValueError, match="verified goal coverage"):
        service.promote_candidate(candidate.candidate_id)
    assert service.reject_candidate(candidate.candidate_id).status == "rejected"
    with pytest.raises(ValueError):
        service.store.set_candidate_status(candidate.candidate_id, "promoted")

    assert service.inspect_legacy_hint(
        source_kind="skill_memory", source_id="legacy-1", source_version=1,
        provenance_valid=True, goal_coverage_valid=False,
        detail="goal completion mapping absent",
    ) == "untrusted"
    assert service.inspect_legacy_hint(
        source_kind="game_learning", source_id="transition-1", source_version=1,
        provenance_valid=False, goal_coverage_valid=True,
        detail="missing source observation",
    ) == "rejected"
    assert service.inspect_mobile_skill_memory(
        memory=SimpleNamespace(
            skill_id="settings", source_task_id="task-verified",
            version=1, evidence=("attempt 2 verified",),
        ),
        source_task=SimpleNamespace(task_id="task-verified"),
        goal_completion={
            "verdict": "verified", "source_task_id": "task-verified", "revision": 1,
        },
    ) == "eligible"
    assert service.inspect_mobile_skill_memory(
        memory=SimpleNamespace(
            skill_id="legacy", source_task_id="task-old", version=1,
            evidence=("planner claimed done",),
        ),
        source_task=SimpleNamespace(task_id="task-old"),
        goal_completion=None,
    ) == "untrusted"


def test_controlled_cold_warm_fixture_meets_action_threshold() -> None:
    result = compare_trials(
        TrialMetrics(True, actions=5, wrong_scenes=1, no_progress=1, recoveries=1),
        TrialMetrics(True, actions=3, wrong_scenes=0, no_progress=0, recoveries=0),
        attributable_retrievals=3, attributable_uses=2,
    )
    assert result.action_reduction == pytest.approx(0.4)
    assert result.meets_initial_threshold


def test_three_scene_cold_warm_suite_meets_all_u4_metrics() -> None:
    scenarios = (
        compare_trials(
            TrialMetrics(True, 5, 1, 1, 1), TrialMetrics(True, 3, 0, 0, 0),
            attributable_retrievals=3, attributable_uses=2,
        ),
        compare_trials(
            TrialMetrics(True, 6, 1, 1, 1), TrialMetrics(True, 3, 0, 0, 1),
            attributable_retrievals=3, attributable_uses=2,
        ),
        compare_trials(
            TrialMetrics(True, 5, 1, 1, 1), TrialMetrics(True, 3, 0, 0, 0),
            attributable_retrievals=3, attributable_uses=2,
        ),
    )
    suite = BenchmarkSuiteResult(
        scenarios=scenarios, false_completions=0,
        promoted_with_complete_provenance=6, promoted_total=6,
        repeated_known_wrong_actions=0, known_wrong_opportunities=3,
        recovered_known_wrong_scenes=3, known_wrong_scenes=3,
    )
    assert suite.action_reduction == pytest.approx(0.4375)
    assert suite.meets_u4_thresholds


def test_perceptual_scene_identity_reuses_semantics_across_fresh_evidence_ids(
    tmp_path: Path,
) -> None:
    settings_png = _png(18, 16)
    battery_png = _png(18, 16, invert=True)
    payloads = {
        "main-1": battery_png,
        "settings-after": settings_png,
        "settings-before-new-id": settings_png,
        "battery-after": battery_png,
        "settings-warm": settings_png,
    }
    service = ExperienceService(
        SQLiteExperienceStore(tmp_path / "experience.db"),
        observation_payload=payloads.__getitem__,
    )
    service.begin_mobile_episode(
        goal_run_id="goal-cold", source_task_id="task-cold", goal_spec_revision=1,
        frozen_criteria_ids=("settings", "battery"), scope=SCOPE,
    )
    service.record_mobile_attempt(
        source_task_id="task-cold", objective="open settings",
        attempt=_attempt(
            1, before_ref="main-1", before_summary="fresh Android frame 18x16",
            after_ref="settings-after", after_summary="fresh Android frame 18x16",
            evidence="settings main page visible", satisfied=True, progress=True,
        ),
    )
    service.record_mobile_attempt(
        source_task_id="task-cold", objective="open battery",
        attempt=_attempt(
            2, before_ref="settings-before-new-id",
            before_summary="fresh Android frame 18x16",
            after_ref="battery-after", after_summary="fresh Android frame 18x16",
            evidence="battery page visible", satisfied=True, progress=True,
        ),
    )
    service.confirm_goal_completion("task-cold", goal_id="goal-cold", completion_revision=1)

    service.begin_mobile_episode(
        goal_run_id="goal-warm", source_task_id="task-warm", goal_spec_revision=1,
        frozen_criteria_ids=("settings", "battery"), scope=SCOPE,
    )
    packet = service.retrieve(
        source_task_id="task-warm", objective="open current battery page",
        observation=Observation("settings-warm", "fresh Android frame 18x16"),
    )
    assert len(packet.items) == 1
    assert packet.items[0].expected_next_scene == "battery page visible"


class _Session:
    def __init__(self) -> None:
        self.observations = iter((
            Observation("main", "main city 720x1280"),
            Observation("main", "main city 720x1280"),
            Observation("tasks", "task list 720x1280"),
        ))
    def observe(self):
        return next(self.observations)
    def execute(self, intent):
        del intent
        return TransportReceipt("accepted", receipt_id="adb-1")
    def close(self):
        return None


class _Driver:
    def open(self, task_id, target_id):
        del task_id, target_id
        return _Session()


class _Model:
    def __init__(self) -> None:
        self.hints = []
    def plan(self, context):
        del context
        return PlanDraft(("open tasks",))
    def decide(self, context):
        self.hints.append(context.experience_hints)
        return ActionDecision("act", PhysicalIntent("tap", {"x": 80, "y": 240}))
    def verify(self, context):
        del context
        return Verification(True, True, evidence="task list visible")
    def reflect(self, context):
        raise AssertionError(context)


def _wait(runtime: MobileTaskRuntime, task_id: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = runtime.inspect(task_id)
        if state.terminal:
            return state
        time.sleep(0.01)
    raise AssertionError("runtime did not settle")


def test_mobile_runtime_records_and_consumes_attributed_experience(tmp_path: Path) -> None:
    service = _service(tmp_path)
    model = _Model()
    runtime = MobileTaskRuntime(
        tmp_path / "mobile.db", driver=_Driver(), model=model,
        scope_resolver=lambda goal, target: "fixture/daily",
        experience=service,
    )
    try:
        cold = runtime.start(
            "open tasks", "cold", execution_origin="v2_goal_compat",
            promote_success_memory=False, goal_id="goal-cold",
            goal_spec_revision=1, frozen_criteria_ids=("open_tasks",),
        )
        assert _wait(runtime, cold.task_id).status == "completed"
        assert model.hints == [()]
        runtime.promote_goal_verified_experience(
            cold.task_id, goal_id="goal-cold", completion_revision=1
        )

        warm = runtime.start(
            "open tasks", "warm", execution_origin="v2_goal_compat",
            promote_success_memory=False, goal_id="goal-warm",
            goal_spec_revision=1, frozen_criteria_ids=("open_tasks",),
        )
        assert _wait(runtime, warm.task_id).status == "completed"
        service.sync_mobile_task(runtime.inspect(warm.task_id))
        assert len(model.hints[-1]) == 1
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and service.store.counts()["usage"] < 1:
            time.sleep(0.01)
        counts = service.store.counts()
        assert counts["transitions"] == 2
        assert counts["retrievals"] == 2
        assert counts["usage"] == 1
    finally:
        runtime.shutdown()


def test_selected_game_learning_fact_import_requires_goal_mapping(tmp_path: Path) -> None:
    service = _service(tmp_path)
    transition = SimpleNamespace(
        transition_id="legacy-transition-1", sequence=1,
        proposal=SimpleNamespace(
            action=GuiAction(target_id="adb:fixture", action="tap", x=10, y=20)
        ),
        before=SimpleNamespace(sha256="a" * 64),
        after=SimpleNamespace(sha256="b" * 64),
        transport=SimpleNamespace(status="accepted"),
        outcome=SimpleNamespace(
            confirmed=True, task_succeeded=True, reward=1.0,
            detail="task list visible",
        ),
    )
    episode = service.import_game_learning_episode(
        job=SimpleNamespace(job_id="job-1", instruction="open tasks"),
        transitions=(transition,), goal_run_id="goal-import",
        goal_spec_revision=1, frozen_criteria_ids=("open_tasks",),
        scope=SCOPE,
        scene_labels={"a" * 64: "main city 720x1280", "b" * 64: "task list 720x1280"},
        goal_verified=True,
    )
    assert episode.terminal_outcome == "verified"
    assert service.store.counts()["transitions"] == 1
    assert service.store.counts()["policies"] == 1


def test_same_scene_wrong_action_is_suppressed_and_ui_drift_degrades(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.begin_mobile_episode(
        goal_run_id="goal-cold", source_task_id="task-cold", goal_spec_revision=1,
        frozen_criteria_ids=("open_tasks",), scope=SCOPE,
    )
    service.record_mobile_attempt(
        source_task_id="task-cold", objective="open tasks",
        attempt=_attempt(
            1, before_ref="main", before_summary="main city 720x1280",
            after_ref="detail", after_summary="character detail 720x1280",
            evidence="wrong page: character detail", satisfied=False, progress=True,
            target_description="character portrait",
        ),
    )
    service.record_mobile_attempt(
        source_task_id="task-cold", objective="open tasks",
        attempt=_attempt(
            2, before_ref="detail", before_summary="character detail 720x1280",
            after_ref="main", after_summary="main city 720x1280",
            evidence="returned to main city", satisfied=True, progress=True, action="back",
        ),
    )
    service.record_mobile_attempt(
        source_task_id="task-cold", objective="open tasks",
        attempt=_attempt(
            3, before_ref="main", before_summary="main city 720x1280",
            after_ref="tasks", after_summary="task list 720x1280",
            evidence="task list visible", satisfied=True, progress=True,
            target_description="visible task anchor",
        ),
    )
    service.confirm_goal_completion("task-cold", goal_id="goal-cold", completion_revision=1)

    service.begin_mobile_episode(
        goal_run_id="goal-warm", source_task_id="task-warm", goal_spec_revision=1,
        frozen_criteria_ids=("open_tasks",), scope=SCOPE,
    )
    packet = service.retrieve(
        source_task_id="task-warm", objective="open tasks",
        observation=Observation("main-warm", "main city 720x1280"),
    )
    assert {item.kind for item in packet.items} == {"negative", "positive"}
    negative = next(item for item in packet.items if item.kind == "negative")
    positive = next(item for item in packet.items if item.kind == "positive")
    assert "character portrait" in negative.semantic_action
    assert "task anchor" in positive.semantic_action
    service.record_mobile_attempt(
        source_task_id="task-warm", objective="open tasks", retrieval=packet,
        attempt=_attempt(
            1, before_ref="main-warm", before_summary="main city 720x1280",
            after_ref="tasks-warm", after_summary="task list 720x1280",
            evidence="task list visible", satisfied=True, progress=True,
            target_description="task anchor",
        ),
    )
    assert service.store.counts()["usage"] == 2  # positive use + negative avoidance

    drifted_scope = ScopeKey(
        user_scope=SCOPE.user_scope, account_scope=SCOPE.account_scope,
        application_id=SCOPE.application_id, goal_family=SCOPE.goal_family,
        ui_version="2.0",
    )
    service.begin_mobile_episode(
        goal_run_id="goal-drift", source_task_id="task-drift", goal_spec_revision=1,
        frozen_criteria_ids=("open_tasks",), scope=drifted_scope,
    )
    assert service.retrieve(
        source_task_id="task-drift", objective="open tasks",
        observation=Observation("main-drift", "main city 720x1280"),
    ).empty

    service.deprecate_candidate(positive.candidate_id)
    service.begin_mobile_episode(
        goal_run_id="goal-deprecated", source_task_id="task-deprecated",
        goal_spec_revision=1, frozen_criteria_ids=("open_tasks",), scope=SCOPE,
    )
    remaining = service.retrieve(
        source_task_id="task-deprecated", objective="open tasks",
        observation=Observation("main-deprecated", "main city 720x1280"),
    )
    assert all(item.candidate_id != positive.candidate_id for item in remaining.items)
