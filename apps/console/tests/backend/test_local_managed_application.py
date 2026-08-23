from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_game_console.application_runtime import ApplicationRuntime
from ai_game_console.config import Settings
from ai_game_console.goal_runtime import (
    GoalService,
    GoalSpecificationDraft,
    SQLiteGoalStore,
    SuccessCriterion,
)
from ai_game_console.goal_runtime.domain import GoalStateConflict
from ai_game_console.local_managed_application_composition import (
    PROFILE_ID,
    compose_local_managed_application_runtime,
)


GOAL = "持续维护本地候选提醒，直到我明确停止"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "runtime",
        database_path=tmp_path / "runtime" / "console.db",
        frontend_dist=tmp_path / "dist",
    )


def _specification(_: str) -> GoalSpecificationDraft:
    return GoalSpecificationDraft(
        {
            "classification": "long_lived_local_goal",
            "goal_family": "long-lived/local",
        },
        (
            SuccessCriterion(
                "continuous_local_goal",
                "持续等待同一 GoalRun 的授权后续事件并记录一次本地候选里程碑。",
                "持久 binding、用户消息、一次本地 receipt、候选通知、恢复和停止围栏。",
                GOAL,
            ),
        ),
    )


def _service(store: SQLiteGoalStore, runtime: ApplicationRuntime) -> GoalService:
    return GoalService(
        store,
        mobile_runtime=None,
        mobile_archive=None,
        configured_serial=None,
        specify_goal=_specification,
        application_runtime=runtime,
        application_archive=runtime,
    )


def _wait_for(runtime: ApplicationRuntime, instance_id: str, predicate) -> object:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        state = runtime.inspect(instance_id)
        if predicate(state):
            return state
        time.sleep(0.01)
    raise AssertionError(runtime.inspect(instance_id))


def test_local_managed_goal_waits_for_real_goal_message_once_recovers_and_stops(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SQLiteGoalStore(settings.data_dir / "goals.db")
    first = compose_local_managed_application_runtime(settings).runtime
    service = _service(store, first)

    created = service.create(GOAL, "local-create")
    plan = store.binding_plan(created.id)
    assert plan is not None
    assert plan.profile_id == PROFILE_ID
    assert plan.owner_kind == "local_runtime"
    assert plan.owner_binding_ref is None
    assert plan.capability_ids == (
        "long_lived.wait",
        "local.managed_notification",
        "experience.record",
    )
    assert created.bound_task_id is not None
    assert created.target_id == "local-managed-runtime"
    waiting = _wait_for(
        first,
        created.bound_task_id,
        lambda state: state.status == "waiting" and not state.intents,
    )
    assert waiting.wake_at is not None
    goal_events = store.events(created.id, after=0, limit=100)
    frozen = next(item for item in goal_events if item.event_type == "goal_binding_plan_frozen")
    bound = next(item for item in goal_events if item.event_type == "application_instance_bound")
    assert frozen.cursor < bound.cursor

    # This is the actual same-GoalRun user-authorized inbound event.  It is
    # not an owner callback and has no account/device/network dependency.
    service.send_message(created.id, "现在记录一次本地候选里程碑", "local-message")
    after_message = _wait_for(
        first,
        created.bound_task_id,
        lambda state: (
            state.status == "waiting"
            and len(state.intents) == 1
            and len(state.outcomes) == 1
        ),
    )
    assert after_message.intents[0].intent.name == "local.managed_candidate_checkpoint.v1"
    assert after_message.intents[0].candidate_notification is True
    assert after_message.intents[0].receipt is not None
    assert after_message.intents[0].receipt.accepted is True
    assert after_message.outcomes[0].status == "confirmed_success"
    assert [event.event_type for event in after_message.events].count(
        "candidate_notified"
    ) == 1
    projected = service.inspect(created.id)
    assert projected.execution_status == "WAITING_EXTERNAL"
    assert projected.control_state == "AUTOMATED"
    assert len(store.notifications(created.id)) == 1

    # Restart the actual local coordinator around the same durable database.
    first.shutdown()
    second = compose_local_managed_application_runtime(settings).runtime
    restarted = _service(store, second)
    time.sleep(0.2)
    recovered = second.inspect(created.bound_task_id)
    assert recovered.status == "waiting"
    assert len(recovered.intents) == 1
    assert len(recovered.outcomes) == 1
    assert [event.event_type for event in recovered.events].count(
        "candidate_notified"
    ) == 1
    assert len(store.notifications(created.id)) == 1
    assert restarted.inspect(created.id).bound_task_id == created.bound_task_id

    stopped = restarted.control(created.id, "stop", "local-stop")
    assert stopped.execution_status == "CANCELLED"
    receipt_count = len(second.inspect(created.bound_task_id).intents)
    with pytest.raises(GoalStateConflict):
        restarted.send_message(created.id, "停止后不得再执行", "after-stop-message")
    time.sleep(0.25)
    fenced = second.inspect(created.bound_task_id)
    assert fenced.status == "stopped"
    assert len(fenced.intents) == receipt_count
    assert [event.event_type for event in fenced.events].count("candidate_notified") == 1
    second.shutdown()
