from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from ai_game_console.mobile_agent import (
    ActionDecision,
    IdempotencyConflict,
    MobileTaskRuntime,
    Observation,
    PhysicalIntent,
    PlanDraft,
    ReflectionDecision,
    TaskQueueFull,
    TaskRuntimeClosed,
    TransportReceipt,
    Verification,
)


TERMINAL = {"completed", "failed", "stopped", "uncertain"}


def wait_terminal(runtime: MobileTaskRuntime, task_id: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = runtime.inspect(task_id)
        if state.status in TERMINAL:
            return state
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not finish: {runtime.inspect(task_id)}")


def wait_dispatch_mutation_fence(runtime: MobileTaskRuntime) -> None:
    """Wait until the competing mutation owns the pre-dispatch linearization lock."""

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if runtime._dispatch_lock.locked():
            return
        time.sleep(0.001)
    raise AssertionError("competing task mutation did not reach the dispatch fence")


class RecordingDriver:
    def __init__(self, receipts=()) -> None:
        self.receipts = deque(receipts)
        self.intents: list[PhysicalIntent] = []
        self.observation_count = 0
        self.open_calls: list[tuple[str, str | None]] = []
        self.close_count = 0

    def open(self, task_id, target_id):
        self.open_calls.append((task_id, target_id))
        return self

    def observe(self):
        self.observation_count += 1
        return Observation(
            evidence_id=f"frame-{self.observation_count}",
            summary=f"fresh frame {self.observation_count}",
        )

    def execute(self, intent):
        self.intents.append(intent)
        if self.receipts:
            return self.receipts.popleft()
        return TransportReceipt("accepted", receipt_id=f"receipt-{len(self.intents)}")

    def close(self):
        self.close_count += 1


class ScriptedModel:
    def __init__(
        self,
        *,
        plans=(PlanDraft(("完成目标",)),),
        decisions=(),
        verifications=(),
        reflections=(),
    ) -> None:
        self.plans = deque(plans)
        self.decisions = deque(decisions)
        self.verifications = deque(verifications)
        self.reflections = deque(reflections)
        self.plan_contexts = []
        self.decision_contexts = []
        self.verification_contexts = []
        self.reflection_contexts = []

    def plan(self, context):
        self.plan_contexts.append(context)
        return self.plans.popleft()

    def decide(self, context):
        self.decision_contexts.append(context)
        return self.decisions.popleft()

    def verify(self, context):
        self.verification_contexts.append(context)
        return self.verifications.popleft()

    def reflect(self, context):
        self.reflection_contexts.append(context)
        return self.reflections.popleft()


class RetryOnceCompletionVerificationModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__(
            decisions=(
                ActionDecision("finish", reason="screen already satisfies the goal"),
                ActionDecision("finish", reason="screen still satisfies the goal"),
            ),
            verifications=(Verification(True, True, evidence="goal visibly satisfied"),),
        )
        self._failed_once = False

    def verify(self, context):
        self.verification_contexts.append(context)
        if not self._failed_once:
            self._failed_once = True
            error = RuntimeError("private malformed response")
            error.code = "mobile_role_invalid_response"  # type: ignore[attr-defined]
            error.public_message = "本地 GUI 模型返回的角色结果格式无效。"  # type: ignore[attr-defined]
            raise error
        return self.verifications.popleft()


def act(name: str) -> ActionDecision:
    return ActionDecision("act", PhysicalIntent(name, {"source": "test"}), name)


def test_invalid_verifier_format_without_transport_is_retried_without_failing_task(
    tmp_path: Path,
) -> None:
    model = RetryOnceCompletionVerificationModel()
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(tmp_path / "mobile.db", driver=driver, model=model)

    task = runtime.start("确认当前画面已经满足目标", "retry-invalid-finish-verifier")
    finished = wait_terminal(runtime, task.task_id)

    assert finished.status == "completed"
    assert finished.error_code is None
    assert len(finished.attempts) == 2
    assert [attempt.transport.status for attempt in finished.attempts] == [
        "not_sent",
        "not_sent",
    ]
    assert finished.attempts[0].verification.satisfied is False
    assert finished.attempts[0].verification.uncertain is False
    assert finished.attempts[1].verification.satisfied is True
    assert driver.intents == []


def test_verified_subgoals_reflect_after_three_no_progress_and_promote_skill(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        plans=(PlanDraft(("打开入口", "领取奖励")),),
        decisions=(act("old-1"), act("old-2"), act("old-3"), act("new"), act("claim")),
        verifications=(
            Verification(False, False, evidence="same screen 1"),
            Verification(False, False, evidence="same screen 2"),
            Verification(False, False, evidence="same screen 3"),
            Verification(True, True, evidence="entry visible"),
            Verification(True, True, evidence="reward claimed"),
        ),
        reflections=(ReflectionDecision("open the side menu first", reason="old route stalled"),),
    )
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(tmp_path / "mobile.db", driver=driver, model=model)

    accepted = runtime.start(
        "领取今天的活动奖励",
        "start-1",
        target_id="device-1",
        skill_id="daily-reward",
    )
    finished = wait_terminal(runtime, accepted.task_id)
    persisted = MobileTaskRuntime(
        tmp_path / "mobile.db", driver=RecordingDriver(), model=ScriptedModel()
    ).inspect(accepted.task_id)

    assert finished.status == "completed"
    assert finished.verification_satisfied is True
    assert finished.reflection_count == 1
    assert finished.skill_memory_version == 1
    assert finished.plan is not None
    assert [item.status for item in finished.plan.subgoals] == ["completed", "completed"]
    assert len(finished.attempts) == 5
    assert all(item.transport.status == "accepted" for item in finished.attempts)
    assert [item.verification.satisfied for item in finished.attempts] == [
        False,
        False,
        False,
        True,
        True,
    ]
    assert [item.event_type for item in finished.events].count("reflection_recorded") == 1
    assert len(model.reflection_contexts) == 1
    assert model.reflection_contexts[0].consecutive_no_progress == 3
    assert len(model.decision_contexts[3].recent_attempts) == 3
    assert persisted.status == "completed"
    assert persisted.skill_memory_version == 1
    assert driver.close_count == 1


def test_transport_acceptance_does_not_complete_without_verification(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        decisions=(act("tap"), ActionDecision("terminate", reason="cannot verify")),
        verifications=(Verification(False, True, evidence="changed but target absent"),),
    )
    runtime = MobileTaskRuntime(
        tmp_path / "mobile.db", driver=RecordingDriver(), model=model
    )

    task = runtime.start("完成任务", "start-no-proof", skill_id="unverified")
    finished = wait_terminal(runtime, task.task_id)

    assert finished.status == "failed"
    assert finished.verification_satisfied is False
    assert finished.skill_memory_version == 0
    assert finished.attempts[0].transport.status == "accepted"
    assert finished.attempts[0].verification.satisfied is False


def test_uncertain_transport_is_terminal_and_is_not_replayed_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    first_driver = RecordingDriver((TransportReceipt("uncertain", detail="timeout"),))
    first = MobileTaskRuntime(
        database,
        driver=first_driver,
        model=ScriptedModel(decisions=(act("tap-once"),)),
    )

    task = first.start("执行一次", "start-uncertain")
    finished = wait_terminal(first, task.task_id)
    second_driver = RecordingDriver()
    restarted = MobileTaskRuntime(database, driver=second_driver, model=ScriptedModel())
    time.sleep(0.05)

    assert finished.status == "uncertain"
    assert finished.error_code == "transport_uncertain"
    assert len(first_driver.intents) == 1
    assert restarted.inspect(task.task_id).status == "uncertain"
    assert second_driver.intents == []


class BlockingTransportDriver(RecordingDriver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, intent):
        self.intents.append(intent)
        self.entered.set()
        assert self.release.wait(2)
        return TransportReceipt("accepted", receipt_id="late-receipt")


def test_restart_marks_open_physical_intent_uncertain_without_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    blocked_driver = BlockingTransportDriver()
    first = MobileTaskRuntime(
        database,
        driver=blocked_driver,
        model=ScriptedModel(
            decisions=(act("physical-intent"),),
            verifications=(Verification(True, True, evidence="too late"),),
        ),
    )
    task = first.start("执行物理动作", "start-open-intent")
    assert blocked_driver.entered.wait(1)

    recovery_driver = RecordingDriver()
    restarted = MobileTaskRuntime(
        database, driver=recovery_driver, model=ScriptedModel()
    )
    recovered = restarted.inspect(task.task_id)
    blocked_driver.release.set()
    time.sleep(0.05)

    assert recovered.status == "uncertain"
    assert recovered.error_code == "restart_open_intent"
    assert recovery_driver.intents == []
    assert len(blocked_driver.intents) == 1


class RevisionFenceModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__(verifications=(Verification(True, True, evidence="new instruction done"),))
        self.first_decision_started = threading.Event()
        self.release_first_decision = threading.Event()

    def decide(self, context):
        self.decision_contexts.append(context)
        if len(self.decision_contexts) == 1:
            self.first_decision_started.set()
            assert self.release_first_decision.wait(2)
            return act("stale-action")
        assert context.input_revision == 1
        assert [item.content for item in context.owner_inputs] == ["先关闭弹窗再继续"]
        return act("revised-action")


def test_owner_input_revision_fences_stale_model_decision_and_is_idempotent(
    tmp_path: Path,
) -> None:
    model = RevisionFenceModel()
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(tmp_path / "mobile.db", driver=driver, model=model)
    task = runtime.start("继续任务", "start-revision")
    assert model.first_decision_started.wait(1)

    updated = runtime.send(task.task_id, "先关闭弹窗再继续", "input-1")
    repeated = runtime.send(task.task_id, "先关闭弹窗再继续", "input-1")
    with pytest.raises(IdempotencyConflict):
        runtime.send(task.task_id, "改成别的内容", "input-1")
    model.release_first_decision.set()
    finished = wait_terminal(runtime, task.task_id)

    assert updated.input_revision == 1
    assert repeated.input_revision == 1
    assert [item.name for item in driver.intents] == ["revised-action"]
    assert [item.lifecycle for item in finished.inputs] == ["applied"]
    assert "decision_stale" in [item.event_type for item in finished.events]


class BlockingPlanModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def plan(self, context):
        self.plan_contexts.append(context)
        self.entered.set()
        assert self.release.wait(2)
        return PlanDraft(("不会执行",))


def test_start_and_stop_request_ids_are_idempotent_and_conflict_on_new_content(
    tmp_path: Path,
) -> None:
    model = BlockingPlanModel()
    runtime = MobileTaskRuntime(
        tmp_path / "mobile.db", driver=RecordingDriver(), model=model
    )
    first = runtime.start("等待", "same-start")
    repeated = runtime.start("等待", "same-start")
    with pytest.raises(IdempotencyConflict):
        runtime.start("不同目标", "same-start")
    assert model.entered.wait(1)

    stopped = runtime.stop(first.task_id, "same-stop")
    stopped_again = runtime.stop(first.task_id, "same-stop")
    with pytest.raises(IdempotencyConflict):
        runtime.stop("different-task", "same-stop")
    model.release.set()
    terminal = wait_terminal(runtime, first.task_id)

    assert repeated.task_id == first.task_id
    assert stopped.cancel_requested is True
    assert stopped_again.task_id == stopped.task_id
    assert terminal.status == "stopped"


class MemoryAwareModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__()

    def plan(self, context):
        self.plan_contexts.append(context)
        return PlanDraft(("verified checkpoint",))

    def decide(self, context):
        self.decision_contexts.append(context)
        return act(f"attempt-{len(self.decision_contexts)}")

    def verify(self, context):
        return Verification(True, True, evidence=f"evidence-{len(self.decision_contexts)}")


def test_skill_memory_is_versioned_and_visible_only_after_verified_completion(
    tmp_path: Path,
) -> None:
    model = MemoryAwareModel()
    runtime = MobileTaskRuntime(
        tmp_path / "mobile.db", driver=RecordingDriver(), model=model
    )

    first = wait_terminal(
        runtime,
        runtime.start("第一次", "memory-1", skill_id="shared-skill").task_id,
    )
    second = wait_terminal(
        runtime,
        runtime.start("第二次", "memory-2", skill_id="shared-skill").task_id,
    )
    listed = runtime.list(limit=1)

    assert first.skill_memory_version == 1
    assert second.skill_memory_version == 2
    assert model.plan_contexts[0].skill_memory is None
    assert model.plan_contexts[1].skill_memory is not None
    assert model.plan_contexts[1].skill_memory.version == 1
    assert model.plan_contexts[1].skill_memory.procedure == ("verified checkpoint",)
    assert model.plan_contexts[1].skill_memory.evidence == ("evidence-1",)
    assert [item.task_id for item in listed] == [second.task_id]


def test_unchanged_reflection_terminates_instead_of_repeating_forever(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        decisions=(act("same-1"), act("same-2"), act("same-3")),
        verifications=(
            Verification(False, False, evidence="same"),
            Verification(False, False, evidence="same"),
            Verification(False, False, evidence="same"),
        ),
        reflections=(ReflectionDecision("initial", reason="no actual change"),),
    )
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(tmp_path / "mobile.db", driver=driver, model=model)

    finished = wait_terminal(
        runtime, runtime.start("不要无限重复", "reflection-no-change").task_id
    )

    assert finished.status == "failed"
    assert finished.error_code == "reflection_no_strategy_change"
    assert len(driver.intents) == 3


def test_single_coordinator_has_bounded_outstanding_task_queue(tmp_path: Path) -> None:
    model = BlockingPlanModel()
    runtime = MobileTaskRuntime(
        tmp_path / "mobile.db",
        driver=RecordingDriver(),
        model=model,
        queue_capacity=1,
    )
    first = runtime.start("占用唯一执行槽", "queue-first")
    assert model.entered.wait(1)

    repeated = runtime.start("占用唯一执行槽", "queue-first")
    with pytest.raises(TaskQueueFull) as full:
        runtime.start("第二个任务", "queue-second")
    runtime.stop(first.task_id, "queue-stop")
    model.release.set()
    wait_terminal(runtime, first.task_id)

    assert repeated.task_id == first.task_id
    assert full.value.code == "mobile_task_queue_full"


class BlockingDecisionModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def decide(self, context):
        self.decision_contexts.append(context)
        self.entered.set()
        assert self.release.wait(2)
        return act("decision-from-old-process")


def test_restart_resumes_safe_checkpoint_and_fences_old_worker(tmp_path: Path) -> None:
    database = tmp_path / "mobile.db"
    old_model = BlockingDecisionModel()
    old_driver = RecordingDriver()
    old_runtime = MobileTaskRuntime(database, driver=old_driver, model=old_model)
    task = old_runtime.start("从安全检查点恢复", "safe-restart")
    assert old_model.entered.wait(1)

    new_driver = RecordingDriver()
    new_runtime = MobileTaskRuntime(
        database,
        driver=new_driver,
        model=ScriptedModel(
            decisions=(act("decision-from-new-process"),),
            verifications=(Verification(True, True, evidence="verified after restart"),),
        ),
    )
    finished = wait_terminal(new_runtime, task.task_id)
    old_model.release.set()
    time.sleep(0.05)

    assert finished.status == "completed"
    assert old_driver.intents == []
    assert [item.name for item in new_driver.intents] == ["decision-from-new-process"]


def test_reflection_budget_is_hard_bounded(tmp_path: Path) -> None:
    model = ScriptedModel(
        decisions=tuple(act(f"tap-{index}") for index in range(6)),
        verifications=tuple(
            Verification(False, False, evidence=f"no-progress-{index}")
            for index in range(6)
        ),
        reflections=(ReflectionDecision("changed-once", reason="first strategy change"),),
    )
    runtime = MobileTaskRuntime(
        tmp_path / "mobile.db",
        driver=RecordingDriver(),
        model=model,
        max_reflections=1,
    )

    finished = wait_terminal(
        runtime, runtime.start("有限反思", "bounded-reflection").task_id
    )

    assert finished.status == "failed"
    assert finished.error_code == "reflection_budget_exhausted"
    assert finished.reflection_count == 1
    assert finished.attempt_count == 6


def test_physical_intent_repr_hides_typed_arguments() -> None:
    intent = PhysicalIntent("type", {"text": "private-message"})

    assert "private-message" not in repr(intent)


class PersistBarrierArguments(Mapping[str, str]):
    """Pause while begin_attempt serializes the intent inside its transaction."""

    def __init__(self) -> None:
        self._data = {"source": "persist-barrier"}
        self.entered = threading.Event()
        self.release = threading.Event()
        self.armed = False

    def __iter__(self) -> Iterator[str]:
        if self.armed:
            self.entered.set()
            assert self.release.wait(2)
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, key: str) -> str:
        return self._data[key]


class PostPersistBarrierModel(ScriptedModel):
    def __init__(self, *, revised: bool = False) -> None:
        super().__init__(
            verifications=(Verification(True, True, evidence="revised action verified"),)
            if revised
            else ()
        )
        self.arguments = PersistBarrierArguments()
        self.revised = revised

    def decide(self, context):
        self.decision_contexts.append(context)
        if len(self.decision_contexts) == 1:
            intent = PhysicalIntent("stale-after-persist", self.arguments)
            self.arguments.armed = True
            return ActionDecision("act", intent, "must be fenced")
        assert self.revised
        assert context.input_revision == 1
        assert context.owner_inputs[-1].content == "使用新策略"
        return act("revised-after-input")


def test_stop_after_intent_persisted_finalizes_not_sent_without_execute_or_restart_uncertainty(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    model = PostPersistBarrierModel()
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(database, driver=driver, model=model)
    task = runtime.start("停止栅栏", "dispatch-stop-start")
    assert model.arguments.entered.wait(1)

    stop_result: list[object] = []
    stop_thread = threading.Thread(
        target=lambda: stop_result.append(
            runtime.stop(task.task_id, "dispatch-stop-request")
        )
    )
    stop_thread.start()
    wait_dispatch_mutation_fence(runtime)
    model.arguments.release.set()
    stop_thread.join(2)
    finished = wait_terminal(runtime, task.task_id)
    restarted = MobileTaskRuntime(
        database, driver=RecordingDriver(), model=ScriptedModel()
    ).inspect(task.task_id)

    assert len(stop_result) == 1
    assert finished.status == "stopped"
    assert driver.intents == []
    assert finished.attempts[0].transport.status == "not_sent"
    assert finished.attempts[0].verification.uncertain is False
    assert finished.attempts[0].verification.evidence == "cancelled before dispatch"
    assert restarted.status == "stopped"
    assert restarted.error_code is None


def test_input_after_intent_persisted_finalizes_stale_not_sent_and_redecides(
    tmp_path: Path,
) -> None:
    model = PostPersistBarrierModel(revised=True)
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(tmp_path / "mobile.db", driver=driver, model=model)
    task = runtime.start("输入修订栅栏", "dispatch-input-start")
    assert model.arguments.entered.wait(1)

    input_result: list[object] = []
    input_thread = threading.Thread(
        target=lambda: input_result.append(
            runtime.send(task.task_id, "使用新策略", "dispatch-input-request")
        )
    )
    input_thread.start()
    wait_dispatch_mutation_fence(runtime)
    model.arguments.release.set()
    input_thread.join(2)
    finished = wait_terminal(runtime, task.task_id)

    assert len(input_result) == 1
    assert finished.status == "completed"
    assert [item.name for item in driver.intents] == ["revised-after-input"]
    assert [item.transport.status for item in finished.attempts] == [
        "not_sent",
        "accepted",
    ]
    assert finished.attempts[0].verification.evidence == "stale before dispatch"
    assert finished.attempts[0].verification.uncertain is False
    assert "decision_stale_after_intent" in [
        item.event_type for item in finished.events
    ]


class BlockingVerifierModel(ScriptedModel):
    def __init__(self, *, revised: bool = False) -> None:
        super().__init__()
        self.revised = revised
        self.verifier_entered = threading.Event()
        self.release_verifier = threading.Event()
        self.verify_count = 0

    def decide(self, context):
        self.decision_contexts.append(context)
        if len(self.decision_contexts) == 1:
            return act("old-accepted-action")
        assert self.revised
        assert context.input_revision == 1
        assert context.owner_inputs[-1].content == "验证后改用新动作"
        return act("new-revision-action")

    def verify(self, context):
        self.verify_count += 1
        if self.verify_count == 1:
            assert context.input_revision == 0
            assert context.owner_inputs == ()
            self.verifier_entered.set()
            assert self.release_verifier.wait(2)
            return Verification(True, True, evidence="old revision verified")
        assert self.revised
        assert context.input_revision == 1
        assert context.owner_inputs[-1].content == "验证后改用新动作"
        return Verification(True, True, evidence="new revision verified")


def test_stop_while_accepted_action_verifier_blocks_saves_attempt_but_never_completes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    model = BlockingVerifierModel()
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(database, driver=driver, model=model)
    task = runtime.start(
        "验证期间停止",
        "verify-stop-start",
        skill_id="verify-stop-skill",
    )
    assert model.verifier_entered.wait(1)

    stopping = runtime.stop(task.task_id, "verify-stop-request")
    model.release_verifier.set()
    finished = wait_terminal(runtime, task.task_id)
    recovery_driver = RecordingDriver()
    restarted = MobileTaskRuntime(
        database, driver=recovery_driver, model=ScriptedModel()
    ).inspect(task.task_id)

    assert stopping.status == "stopping"
    assert finished.status == "stopped"
    assert finished.verification_satisfied is False
    assert finished.skill_memory_version == 0
    assert len(finished.attempts) == 1
    assert finished.attempts[0].transport.status == "accepted"
    assert finished.attempts[0].verification.satisfied is True
    assert restarted.status == "stopped"
    assert recovery_driver.intents == []


class CaptureMemoryModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__()
        self.planned = threading.Event()

    def plan(self, context):
        self.plan_contexts.append(context)
        self.planned.set()
        return PlanDraft(("inspect memory",))

    def decide(self, context):
        return ActionDecision("terminate", reason="memory captured")


def test_input_while_accepted_action_verifier_blocks_fences_completion_and_old_memory(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    model = BlockingVerifierModel(revised=True)
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(database, driver=driver, model=model)
    task = runtime.start(
        "验证期间改指令",
        "verify-input-start",
        skill_id="revision-aware-skill",
    )
    assert model.verifier_entered.wait(1)

    updated = runtime.send(
        task.task_id,
        "验证后改用新动作",
        "verify-input-request",
    )
    model.release_verifier.set()
    finished = wait_terminal(runtime, task.task_id)

    capture = CaptureMemoryModel()
    followup = MobileTaskRuntime(
        database, driver=RecordingDriver(), model=capture
    )
    followup.start(
        "读取技能记忆",
        "verify-memory-capture",
        skill_id="revision-aware-skill",
    )
    assert capture.planned.wait(1)
    memory = capture.plan_contexts[0].skill_memory

    assert updated.input_revision == 1
    assert finished.status == "completed"
    assert [item.name for item in driver.intents] == [
        "old-accepted-action",
        "new-revision-action",
    ]
    assert len(finished.attempts) == 2
    assert all(item.transport.status == "accepted" for item in finished.attempts)
    assert all(item.verification.satisfied for item in finished.attempts)
    assert finished.inputs[-1].lifecycle == "applied"
    assert "verification_stale_after_transport" in [
        item.event_type for item in finished.events
    ]
    assert memory is not None
    assert memory.version == 1
    assert memory.evidence == ("new revision verified",)


def test_uncertain_verification_after_accepted_action_is_terminal_and_not_replayed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(
        database,
        driver=driver,
        model=ScriptedModel(
            decisions=(act("single-physical-action"), act("must-not-retry")),
            verifications=(
                Verification(
                    False,
                    False,
                    uncertain=True,
                    evidence="screen evidence ambiguous",
                ),
            ),
        ),
    )
    task = runtime.start(
        "未知验证必须终止",
        "uncertain-verification-start",
        skill_id="uncertain-verification-skill",
    )
    finished = wait_terminal(runtime, task.task_id)

    recovery_driver = RecordingDriver()
    restarted = MobileTaskRuntime(
        database, driver=recovery_driver, model=ScriptedModel()
    )
    time.sleep(0.05)

    assert finished.status == "uncertain"
    assert finished.error_code == "verification_uncertain_after_transport"
    assert finished.skill_memory_version == 0
    assert [item.name for item in driver.intents] == ["single-physical-action"]
    assert finished.attempts[0].transport.status == "accepted"
    assert finished.attempts[0].verification.uncertain is True
    assert restarted.inspect(task.task_id).status == "uncertain"
    assert recovery_driver.intents == []


class BlockingUncertainVerifierModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__(decisions=(act("accepted-before-uncertain"),))
        self.entered = threading.Event()
        self.release = threading.Event()

    def verify(self, context):
        self.entered.set()
        assert self.release.wait(2)
        return Verification(
            False,
            False,
            uncertain=True,
            evidence="uncertain beats concurrent stop",
        )


def test_verification_uncertainty_after_transport_has_priority_over_concurrent_stop(
    tmp_path: Path,
) -> None:
    model = BlockingUncertainVerifierModel()
    driver = RecordingDriver()
    runtime = MobileTaskRuntime(tmp_path / "mobile.db", driver=driver, model=model)
    task = runtime.start("未知优先", "uncertain-priority-start")
    assert model.entered.wait(1)

    stopping = runtime.stop(task.task_id, "uncertain-priority-stop")
    model.release.set()
    finished = wait_terminal(runtime, task.task_id)

    assert stopping.status == "stopping"
    assert finished.status == "uncertain"
    assert finished.error_code == "verification_uncertain_after_transport"
    assert len(driver.intents) == 1


def test_automatic_scope_is_persisted_reused_after_restart_and_isolated_from_legacy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    scope_calls: list[tuple[str, str | None]] = []

    def resolve_scope(goal: str, target_id: str | None) -> str:
        scope_calls.append((goal, target_id))
        return "stzb-game/1/claim-daily-reward"

    seed_model = MemoryAwareModel()
    seed_runtime = MobileTaskRuntime(
        database,
        driver=RecordingDriver(),
        model=seed_model,
        scope_resolver=resolve_scope,
    )
    automatic_seed = wait_terminal(
        seed_runtime,
        seed_runtime.start(
            "领取率土今日奖励",
            "auto-scope-seed",
            target_id="tablet-1",
        ).task_id,
    )
    seed_runtime.shutdown(timeout=1)

    reuse_model = MemoryAwareModel()
    restarted = MobileTaskRuntime(
        database,
        driver=RecordingDriver(),
        model=reuse_model,
        scope_resolver=resolve_scope,
    )
    automatic_reuse = wait_terminal(
        restarted,
        restarted.start(
            "再次领取率土今日奖励",
            "auto-scope-reuse",
            target_id="tablet-1",
        ).task_id,
    )
    legacy_seed = wait_terminal(
        restarted,
        restarted.start(
            "显式旧技能第一次",
            "legacy-scope-seed",
            skill_id="stzb-game/1/claim-daily-reward",
        ).task_id,
    )
    legacy_reuse = wait_terminal(
        restarted,
        restarted.start(
            "显式旧技能第二次",
            "legacy-scope-reuse",
            skill_id="stzb-game/1/claim-daily-reward",
        ).task_id,
    )
    restarted.shutdown(timeout=1)

    assert automatic_seed.skill_id is None
    assert automatic_seed.skill_scope_id == "auto:stzb-game/1/claim-daily-reward"
    assert automatic_seed.skill_memory_version == 1
    assert automatic_reuse.skill_id is None
    assert automatic_reuse.skill_scope_id == automatic_seed.skill_scope_id
    assert automatic_reuse.skill_memory_version == 2
    assert legacy_seed.skill_id == "stzb-game/1/claim-daily-reward"
    assert legacy_seed.skill_scope_id == "legacy:stzb-game/1/claim-daily-reward"
    assert legacy_seed.skill_memory_version == 1
    assert legacy_reuse.skill_memory_version == 2
    assert reuse_model.plan_contexts[0].skill_memory is not None
    assert reuse_model.plan_contexts[0].skill_memory.skill_id == automatic_seed.skill_scope_id
    assert reuse_model.plan_contexts[0].skill_memory.version == 1
    assert reuse_model.plan_contexts[1].skill_memory is None
    assert reuse_model.plan_contexts[2].skill_memory is not None
    assert reuse_model.plan_contexts[2].skill_memory.skill_id == legacy_seed.skill_scope_id
    assert scope_calls == [
        ("领取率土今日奖励", "tablet-1"),
        ("再次领取率土今日奖励", "tablet-1"),
    ]


def test_schema_v1_explicit_skill_memory_migrates_into_legacy_scope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    seed_model = MemoryAwareModel()
    seed_runtime = MobileTaskRuntime(
        database, driver=RecordingDriver(), model=seed_model
    )
    seed = wait_terminal(
        seed_runtime,
        seed_runtime.start(
            "旧数据库技能",
            "migration-seed",
            skill_id="old-skill",
        ).task_id,
    )
    prefixed_seed = wait_terminal(
        seed_runtime,
        seed_runtime.start(
            "旧数据库字面前缀技能",
            "migration-prefixed-seed",
            skill_id="legacy:old-skill",
        ).task_id,
    )
    seed_runtime.shutdown(timeout=1)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE mobile_skill_memories SET skill_id = substr(skill_id, 8) "
            "WHERE skill_id LIKE 'legacy:%'"
        )
        connection.execute("ALTER TABLE mobile_tasks DROP COLUMN skill_scope_id")
        connection.execute(
            "UPDATE mobile_agent_schema SET version = 1 WHERE singleton = 1"
        )

    migrated_model = MemoryAwareModel()
    migrated_runtime = MobileTaskRuntime(
        database, driver=RecordingDriver(), model=migrated_model
    )
    migrated_seed = migrated_runtime.inspect(seed.task_id)
    migrated_prefixed_seed = migrated_runtime.inspect(prefixed_seed.task_id)
    followup = wait_terminal(
        migrated_runtime,
        migrated_runtime.start(
            "迁移后复用旧技能",
            "migration-followup",
            skill_id="old-skill",
        ).task_id,
    )
    migrated_runtime.shutdown(timeout=1)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version FROM mobile_agent_schema WHERE singleton = 1"
        ).fetchone()[0]

    assert version == 2
    assert migrated_seed.skill_id == "old-skill"
    assert migrated_seed.skill_scope_id == "legacy:old-skill"
    assert migrated_prefixed_seed.skill_id == "legacy:old-skill"
    assert migrated_prefixed_seed.skill_scope_id == "legacy:legacy:old-skill"
    assert migrated_model.plan_contexts[0].skill_memory is not None
    assert migrated_model.plan_contexts[0].skill_memory.skill_id == "legacy:old-skill"
    assert migrated_model.plan_contexts[0].skill_memory.version == 1
    assert followup.skill_memory_version == 2


def test_shutdown_times_out_deterministically_then_leaves_unexecuted_tasks_recoverable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    blocked_model = BlockingPlanModel()
    old_driver = RecordingDriver()
    runtime = MobileTaskRuntime(
        database,
        driver=old_driver,
        model=blocked_model,
        queue_capacity=2,
    )
    first = runtime.start("关闭时正在规划", "shutdown-running")
    assert blocked_model.entered.wait(1)
    second = runtime.start("关闭时仍在队列", "shutdown-queued")

    with pytest.raises(TimeoutError, match="shutdown timed out"):
        runtime.shutdown(timeout=0.01)
    with pytest.raises(TaskRuntimeClosed):
        runtime.start("关闭后不得接受", "shutdown-rejected")

    blocked_model.release.set()
    runtime.shutdown(timeout=1)

    assert runtime._coordinator.is_alive() is False
    assert runtime.inspect(first.task_id).status == "queued"
    assert runtime.inspect(second.task_id).status == "queued"
    assert old_driver.intents == []

    recovery_driver = RecordingDriver()
    recovered = MobileTaskRuntime(
        database,
        driver=recovery_driver,
        model=MemoryAwareModel(),
    )
    assert wait_terminal(recovered, first.task_id).status == "completed"
    assert wait_terminal(recovered, second.task_id).status == "completed"
    recovered.shutdown(timeout=1)
    assert len(recovery_driver.intents) == 2


def test_shutdown_after_intent_persistence_finalizes_not_sent_and_restart_executes_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mobile.db"
    model = PostPersistBarrierModel()
    old_driver = RecordingDriver()
    runtime = MobileTaskRuntime(database, driver=old_driver, model=model)
    task = runtime.start("关闭前已持久化意图", "shutdown-intent-start")
    assert model.arguments.entered.wait(1)

    with pytest.raises(TimeoutError, match="shutdown timed out"):
        runtime.shutdown(timeout=0.01)
    model.arguments.release.set()
    runtime.shutdown(timeout=1)
    suspended = runtime.inspect(task.task_id)

    assert suspended.status == "queued"
    assert old_driver.intents == []
    assert len(suspended.attempts) == 1
    assert suspended.attempts[0].transport.status == "not_sent"
    assert suspended.attempts[0].verification.uncertain is False
    assert suspended.attempts[0].verification.evidence == "runtime shutdown before dispatch"

    recovery_driver = RecordingDriver()
    recovered = MobileTaskRuntime(
        database,
        driver=recovery_driver,
        model=ScriptedModel(
            decisions=(act("execute-once-after-restart"),),
            verifications=(Verification(True, True, evidence="verified after restart"),),
        ),
    )
    finished = wait_terminal(recovered, task.task_id)
    recovered.shutdown(timeout=1)

    assert finished.status == "completed"
    assert [item.name for item in recovery_driver.intents] == [
        "execute-once-after-restart"
    ]
