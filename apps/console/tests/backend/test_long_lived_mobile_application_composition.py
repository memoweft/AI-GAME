from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_game_console.application_runtime import Input, Stop
from ai_game_console.experience_runtime import ExperienceService, SQLiteExperienceStore
from ai_game_console.goal_runtime import GoalService, create_goal_router, goal_error_handler
from ai_game_console.goal_runtime.domain import (
    GoalError,
    GoalSpecificationDraft,
    SuccessCriterion,
)
from ai_game_console.goal_runtime.preflight import PreflightResult
from ai_game_console.goal_runtime.store import SQLiteGoalStore
from ai_game_console.kernel_canary import KernelApplicationCycleResult
from ai_game_console.long_lived_mobile_application_composition import (
    LongLivedMobileApplicationRuntimeGateway,
)


GOAL = "在当前已登录的 Soul 中持续匹配和聊天，直到我明确停止。"
TARGET_ID = "adb:device-1"
APPLICATION_ID = "cn.soulapp.android"


class _Value:
    def __init__(self, value: str) -> None:
        self.value = value


class _ObservationProvider:
    def __init__(self) -> None:
        self.calls = 0

    def read_device_state(self, target_id: str):
        assert target_id == TARGET_ID
        self.calls += 1
        return SimpleNamespace(
            foreground_app=APPLICATION_ID,
            connection_state=_Value("connected"),
            orientation=_Value("portrait"),
            screen_size=(1280, 720),
            captured_at=f"2026-08-23T00:00:{self.calls:02d}+00:00",
        )


class _Kernel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.by_task: dict[str, KernelApplicationCycleResult] = {}
        self.by_cycle: dict[str, KernelApplicationCycleResult] = {}

    def execute_application_cycle(self, **arguments):
        call = dict(arguments)
        self.calls.append(call)
        index = len(self.calls)
        result = KernelApplicationCycleResult(
            task_id=f"kernel-task-{index}",
            cycle_key=str(arguments["cycle_key"]),
            target_id=TARGET_ID,
            status="confirmed_success",
            application_id=APPLICATION_ID,
            application_ready=True,
            before_observation_id=f"before-observation-{index}",
            before_evidence_id=f"before-evidence-{index}",
            action_id=f"action-{index}",
            execution_id=f"execution-{index}",
            after_observation_id=f"after-observation-{index}",
            after_evidence_id=f"after-evidence-{index}",
            verification_id=f"verification-{index}",
            verdict="SUCCESS",
            evidence=f"verified cycle {index}",
            physical_action_sent=True,
        )
        self.by_task[result.task_id] = result
        self.by_cycle[result.cycle_key] = result
        return result

    def inspect_application_cycle(self, task_id: str):
        return self.by_task[task_id]

    def reconcile_application_cycle(self, cycle_key: str):
        return self.by_cycle.get(cycle_key)


def _wait_for(predicate, *, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def test_long_lived_mobile_gateway_waits_then_consumes_each_trigger_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "application.db"
    kernel = _Kernel()
    provider = _ObservationProvider()
    runtime = LongLivedMobileApplicationRuntimeGateway(
        database,
        kernel=kernel,
        observation_provider=provider,
    )
    runtime.startup()
    try:
        prepared = runtime.prepare(
            client_request_id="prepare-1",
            goal_id="goal-1",
            target_id=TARGET_ID,
            application_id=APPLICATION_ID,
            initial_input=GOAL,
        )
        _wait_for(lambda: runtime.inspect(prepared.instance_id).status == "waiting")
        assert kernel.calls == []

        runtime.activate(prepared.instance_id)
        first = _wait_for(
            lambda: (
                runtime.inspect(prepared.instance_id)
                if len(kernel.calls) == 1
                and runtime.inspect(prepared.instance_id).status == "waiting"
                else None
            ),
            timeout=6.0,
        )
        assert len(first.intents) == 1
        assert len(first.outcomes) == 1
        assert first.outcomes[0].status == "confirmed_success"
        assert sum(
            event.event_type == "candidate_notified" for event in first.events
        ) == 1

        runtime.command(
            prepared.instance_id,
            Input("继续，但保持同一原始目标和一次一动作边界。"),
            "goal-input-1",
        )
        second = _wait_for(
            lambda: (
                runtime.inspect(prepared.instance_id)
                if len(kernel.calls) == 2
                and runtime.inspect(prepared.instance_id).status == "waiting"
                else None
            )
        )
        time.sleep(0.15)
        assert len(kernel.calls) == 2
        assert len(second.intents) == 2
        assert len({str(call["cycle_key"]) for call in kernel.calls}) == 2
        assert "当前已接收的授权输入历史" in str(kernel.calls[1]["goal"])
        assert "继续，但保持同一原始目标和一次一动作边界。" in str(
            kernel.calls[1]["goal"]
        )
        assert sum(
            event.event_type == "candidate_notified" for event in second.events
        ) == 1

        outcome = runtime.report_application_outcome(
            prepared.instance_id,
            event_id="no-response-1",
            event_type="no_response",
            evidence_refs=("fresh-no-response-frame-1",),
            attribution_scope="goal-run:goal-1/application:cn.soulapp.android",
            confidence=0.9,
        )
        assert sum(event.event_type == "no_response" for event in outcome.events) == 1
        third = _wait_for(
            lambda: (
                runtime.inspect(prepared.instance_id)
                if len(kernel.calls) == 3
                and runtime.inspect(prepared.instance_id).status == "waiting"
                else None
            )
        )
        replayed = runtime.report_application_outcome(
            prepared.instance_id,
            event_id="no-response-1",
            event_type="no_response",
            evidence_refs=("fresh-no-response-frame-1",),
            attribution_scope="goal-run:goal-1/application:cn.soulapp.android",
            confidence=0.9,
        )
        time.sleep(0.15)
        assert len(kernel.calls) == 3
        assert len(replayed.events) == len(third.events)

        runtime.shutdown()
        recovered = LongLivedMobileApplicationRuntimeGateway(
            database,
            kernel=kernel,
            observation_provider=provider,
        )
        recovered.startup()
        try:
            same = recovered.activate(prepared.instance_id)
            assert same.instance_id == prepared.instance_id
            time.sleep(0.15)
            assert len(kernel.calls) == 3

            recovered.command(prepared.instance_id, Stop(), "stop-1")
            stopped = _wait_for(
                lambda: (
                    recovered.inspect(prepared.instance_id)
                    if recovered.inspect(prepared.instance_id).status == "stopped"
                    else None
                )
            )
            assert stopped.wake_at is None
            with pytest.raises(RuntimeError):
                recovered.command(
                    prepared.instance_id,
                    Input("停止后不得进入新周期"),
                    "post-stop-input",
                )
            time.sleep(0.15)
            assert len(kernel.calls) == 3
        finally:
            recovered.shutdown()
    finally:
        runtime.shutdown()


class _PreparedRuntime:
    def __init__(self, store: SQLiteGoalStore) -> None:
        self.store = store
        self.instance_id = "application-instance-1"
        self.events: list[object] = []
        self.outcomes_by_event_id: dict[str, object] = {}
        self.prepare_calls = 0
        self.activate_calls = 0
        self.status = "waiting"
        self.defer_stop = False

    def prepare(self, **arguments):
        self.prepare_calls += 1
        plan = self.store.binding_plan(str(arguments["goal_id"]))
        assert plan is not None
        assert plan.binding_kind == "long_lived_mobile_composition"
        assert self.store.inspect(str(arguments["goal_id"])).bound_task_id is None
        return self.inspect(self.instance_id)

    def activate(self, instance_id: str):
        self.activate_calls += 1
        record = self.store.list(1)[0]
        assert record.bound_task_id == instance_id
        return self.inspect(instance_id)

    def inspect(self, instance_id: str):
        assert instance_id == self.instance_id
        return SimpleNamespace(
            instance_id=instance_id,
            status=self.status,
            control_state="AUTOMATED",
            detail="waiting",
            error_code=None,
            events=tuple(self.events),
        )

    def command(self, instance_id: str, command, request_id: str):
        del request_id
        if command.tag == "Stop":
            self.status = "stopping" if self.defer_stop else "stopped"
        return self.inspect(instance_id)

    def report_application_outcome(self, instance_id: str, **arguments):
        assert instance_id == self.instance_id
        if self.status == "stopped":
            raise RuntimeError("stopped instance cannot accept outcomes")
        event_id = str(arguments["event_id"])
        existing = self.outcomes_by_event_id.get(event_id)
        if existing is not None:
            return self.inspect(instance_id)
        event = SimpleNamespace(
            sequence=len(self.events) + 1,
            event_type=str(arguments["event_type"]),
            data={
                "evidence_refs": list(arguments["evidence_refs"]),
                "attribution_scope": str(arguments["attribution_scope"]),
                "confidence": float(arguments["confidence"]),
            },
            created_at="2026-08-24T00:00:00+00:00",
        )
        self.outcomes_by_event_id[event_id] = event
        self.events.append(event)
        return self.inspect(instance_id)


def _specification(_: str) -> GoalSpecificationDraft:
    return GoalSpecificationDraft(
        {
            "classification": "long_lived_application_goal",
            "goal_family": "long-lived/mobile",
            "outcome": GOAL,
        },
        (
            SuccessCriterion(
                "continuous_mobile_progress",
                "持续推进匹配和聊天，直到用户停止。",
                "真实移动应用周期和显式停止证据。",
                GOAL,
            ),
        ),
    )


def test_goal_service_binds_composite_only_after_plan_and_prepare(
    tmp_path: Path,
) -> None:
    store = SQLiteGoalStore(tmp_path / "goals.db")
    runtime = _PreparedRuntime(store)
    service = GoalService(
        store,
        mobile_runtime=None,
        mobile_archive=None,
        kernel_runtime=object(),
        kernel_binding_kind="runtime_kernel",
        configured_serial="device-1",
        preflight=lambda: PreflightResult(
            "READY",
            (
                {
                    "capability": "android.discovery",
                    "state": "READY",
                    "detail": "one authorized target",
                },
            ),
            selected_target_id=TARGET_ID,
            selected_serial="device-1",
        ),
        specify_goal=_specification,
        long_lived_mobile_runtime=runtime,
        long_lived_mobile_archive=runtime,
        discover_mobile_application=lambda target_id: (
            APPLICATION_ID if target_id == TARGET_ID else ""
        ),
    )

    created = service.create(GOAL, "goal-create-1")
    assert created.binding_kind == "long_lived_mobile_composition"
    assert created.bound_task_id == runtime.instance_id
    assert created.target_id == TARGET_ID
    assert created.execution_status == "WAITING_EXTERNAL"
    assert runtime.prepare_calls == 1
    assert runtime.activate_calls == 1
    environment = store.environment(created.id)
    assert environment["selected_application_id"] == APPLICATION_ID

    events = store.events(created.id, after=0, limit=100)
    frozen = next(
        event.cursor
        for event in events
        if event.event_type == "goal_binding_plan_frozen"
    )
    bound = next(
        event.cursor
        for event in events
        if event.event_type == "application_instance_bound"
    )
    assert frozen < bound


def test_goal_api_records_one_attributed_application_outcome_and_stop_fences_it(
    tmp_path: Path,
) -> None:
    store = SQLiteGoalStore(tmp_path / "goals.db")
    runtime = _PreparedRuntime(store)
    experience = ExperienceService(
        SQLiteExperienceStore(tmp_path / "experience.db"), enabled=True
    )
    service = GoalService(
        store,
        mobile_runtime=None,
        mobile_archive=None,
        kernel_runtime=object(),
        kernel_binding_kind="runtime_kernel",
        configured_serial="device-1",
        preflight=lambda: PreflightResult(
            "READY",
            (
                {
                    "capability": "android.discovery",
                    "state": "READY",
                    "detail": "one authorized target",
                },
            ),
            selected_target_id=TARGET_ID,
            selected_serial="device-1",
        ),
        specify_goal=_specification,
        long_lived_mobile_runtime=runtime,
        long_lived_mobile_archive=runtime,
        discover_mobile_application=lambda _: APPLICATION_ID,
        experience=experience,
    )
    created = service.create(GOAL, "goal-create-outcome")
    plan = store.binding_plan(created.id)
    assert plan is not None
    assert plan.owner_kind is None
    assert plan.owner_binding_ref is None

    app = FastAPI()
    app.add_exception_handler(GoalError, goal_error_handler)
    app.include_router(create_goal_router(service))
    client = TestClient(app)
    request = {
        "event_id": "real-no-response-1",
        "event_type": "no_response",
        "evidence_refs": ["fresh-soul-frame-1"],
        "attribution_scope": f"goal-run:{created.id}/application:{APPLICATION_ID}",
        "confidence": 0.9,
    }

    response = client.post(
        f"/api/v2/goals/{created.id}/application-outcomes", json=request
    )
    assert response.status_code == 202
    assert response.json()["execution_status"] == "WAITING_EXTERNAL"
    episode = experience.store.episode_for_task(created.bound_task_id)
    signals = experience.store.signals_for_episode(episode.episode_id)
    assert len(signals) == 1
    assert signals[0].kind == "no_response"
    assert signals[0].transition_id is None
    assert signals[0].evidence_refs == (
        f"attribution:{request['attribution_scope']}",
        "fresh-soul-frame-1",
    )

    replay = client.post(
        f"/api/v2/goals/{created.id}/application-outcomes", json=request
    )
    assert replay.status_code == 202
    assert len(experience.store.signals_for_episode(episode.episode_id)) == 1

    runtime.defer_stop = True
    stopped = client.post(
        f"/api/v2/goals/{created.id}/controls",
        json={"action": "stop", "idempotency_key": "stop-after-outcome"},
    )
    assert stopped.status_code == 202
    assert stopped.json()["execution_status"] == "RUNNING"
    assert stopped.json()["control_state"] == "STOP_REQUESTED"
    event_count = len(runtime.events)
    rejected = client.post(
        f"/api/v2/goals/{created.id}/application-outcomes",
        json={**request, "event_id": "after-stop-outcome"},
    )
    assert rejected.status_code == 409
    assert len(runtime.events) == event_count
    rejected_message = client.post(
        f"/api/v2/goals/{created.id}/messages",
        json={
            "content": "停止请求之后不得接收的新消息",
            "idempotency_key": "message-after-stop-request",
        },
    )
    assert rejected_message.status_code == 409


def test_restart_applies_durable_goal_stop_before_application_worker_start(
    tmp_path: Path,
) -> None:
    application_database = tmp_path / "application.db"
    goal_store = SQLiteGoalStore(tmp_path / "goals.db")
    kernel = _Kernel()
    provider = _ObservationProvider()
    runtime = LongLivedMobileApplicationRuntimeGateway(
        application_database,
        kernel=kernel,
        observation_provider=provider,
    )
    runtime.startup()
    record, _ = goal_store.create(goal=GOAL, idempotency_key="stop-crash-goal")
    goal_store.record_binding_plan(
        record.id,
        route_kind="long_lived_mobile_application",
        binding_kind="long_lived_mobile_composition",
        capability_ids=("long_lived.wait", "android.observe", "android.action"),
        owner_kind=None,
        profile_id=None,
        classification="long_lived_application_goal",
        rationale="test composite",
    )
    prepared = runtime.prepare(
        client_request_id=f"goal:{record.id}:prepare",
        goal_id=record.id,
        target_id=TARGET_ID,
        application_id=APPLICATION_ID,
        initial_input=GOAL,
    )
    goal_store.bind_task(
        record.id,
        prepared.instance_id,
        TARGET_ID,
        binding_kind="long_lived_mobile_composition",
    )
    runtime.activate(prepared.instance_id)
    goal_store.record_control(
        record.id,
        action="stop",
        idempotency_key="stop-before-downstream-command",
    )
    assert goal_store.inspect(record.id).control_state == "STOP_REQUESTED"
    runtime.shutdown()

    recovered_runtime = LongLivedMobileApplicationRuntimeGateway(
        application_database,
        kernel=kernel,
        observation_provider=provider,
    )
    service = GoalService(
        goal_store,
        mobile_runtime=None,
        mobile_archive=None,
        configured_serial="device-1",
        long_lived_mobile_runtime=recovered_runtime,
        long_lived_mobile_archive=recovered_runtime,
    )
    service.fence_stopped_long_lived_mobile_bindings_before_start()
    assert recovered_runtime.inspect(prepared.instance_id).status == "stopped"

    recovered_runtime.startup()
    try:
        service.recover_long_lived_mobile_bindings()
        projected = service.inspect(record.id)
        assert projected.execution_status == "CANCELLED"
        assert projected.terminal_at is not None
        terminal_at = projected.terminal_at
        assert service.inspect(record.id).terminal_at == terminal_at
        time.sleep(1.2)
        assert kernel.calls == []
    finally:
        recovered_runtime.shutdown()
