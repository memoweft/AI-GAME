from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai_game_console.android_chat_adapter import (
    ChatAndroidAutomationRun,
    RepositoryAndroidAutomationFactory,
)
from ai_game_console.chat import AutomationInstructionSnapshot, AutomationStepUpdate
from ai_game_console.device_lease import DeviceExecutionLease
from ai_game_console.domain import Target, TargetKind
from ai_game_console.execution import ActionTransportResult, AndroidScreenshot, GuiAction


def frame() -> AndroidScreenshot:
    return AndroidScreenshot(png_bytes=b"\x89PNG\r\n\x1a\nframe", width=1000, height=1000)


@dataclass
class FakeRepository:
    target: Target | None

    def get_target(self, target_id: str) -> Target | None:
        return self.target if self.target and self.target.id == target_id else None


class FakeExecutor:
    serial = "127.0.0.1:16384"

    def __init__(self, frames: list[AndroidScreenshot]) -> None:
        self.frames = iter(frames)
        self.actions: list[GuiAction] = []

    def capture_screenshot(self) -> AndroidScreenshot:
        return next(self.frames)

    def execute(self, action: GuiAction) -> ActionTransportResult:
        self.actions.append(action)
        return ActionTransportResult(accepted=True, detail="accepted")


class FakeModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.goals: list[str] = []

    def propose_action(self, screenshot, *, goal, action_history):
        self.goals.append(goal)
        return next(self.responses)


@dataclass
class ExplodingLoop:
    code: str

    def run(self):
        raise RuntimeError(self.code)


def tool(arguments: str) -> str:
    return f'<tool_call>{{"name":"mobile_use","arguments":{arguments}}}</tool_call>'


def android_target(*, external_id: str = "127.0.0.1:16384") -> Target:
    return Target(
        id="adb-target",
        name="MuMu",
        kind=TargetKind.ANDROID,
        status="ready",
        source="adb",
        external_id=external_id,
    )


def test_factory_runs_bounded_loop_and_maps_truthful_timeline() -> None:
    executor = FakeExecutor([frame(), frame(), frame()])
    model = FakeModel(
        [
            tool('{"action":"click","coordinate":[500,500]}'),
            tool('{"action":"terminate","status":"success"}'),
        ]
    )
    updates: list[AutomationStepUpdate] = []
    factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target()),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        model=model,
        max_steps=4,
    )

    run = factory.create(
        target_id="adb-target",
        goal={"goal": "Open a report", "exact_text": "hello world"},
        on_step=updates.append,
        is_cancelled=lambda: False,
    )
    result = run.run()

    assert result.status == "completed"
    assert executor.actions[0].action == "tap"
    assert model.goals[0].endswith("hello world")
    assert [item.state for item in updates] == [
        "observed",
        "proposed",
        "transported",
        "observed_after",
        "observed",
        "proposed",
        "terminated",
    ]
    assert "不代表界面目标已经完成" in updates[2].summary


def test_factory_combines_ordered_instruction_updates_with_latest_priority() -> None:
    executor = FakeExecutor([frame()])
    model = FakeModel([tool('{"action":"terminate","status":"success"}')])
    factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target()),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        model=model,
    )

    result = factory.create(
        target_id="adb-target",
        goal={"goal": "Open a report", "exact_text": "original text"},
        on_step=lambda update: None,
        is_cancelled=lambda: False,
        instruction_source=lambda: AutomationInstructionSnapshot(
            revision=3,
            updates=("First choose the summary tab", "Then choose the detail tab"),
        ),
    ).run()

    assert result.status == "completed"
    dynamic_goal = model.goals[0]
    assert dynamic_goal.startswith("Open a report")
    assert "use this exact text without rewriting it: original text" in dynamic_goal
    assert dynamic_goal.index("First choose the summary tab") < dynamic_goal.index(
        "Then choose the detail tab"
    )
    assert (
        "later updates override conflicting earlier updates and conflicting base instructions"
        in dynamic_goal
    )


def test_factory_maps_instruction_update_redirect_to_humane_timeline() -> None:
    executor = FakeExecutor([frame(), frame()])
    current = AutomationInstructionSnapshot(revision=1, updates=())

    class UpdatingModel(FakeModel):
        def propose_action(self, screenshot, *, goal, action_history):
            nonlocal current
            response = super().propose_action(
                screenshot,
                goal=goal,
                action_history=action_history,
            )
            if len(self.goals) == 1:
                current = AutomationInstructionSnapshot(
                    revision=2,
                    updates=("Use the new route",),
                )
            return response

    model = UpdatingModel(
        [
            tool('{"action":"click","coordinate":[500,500]}'),
            tool('{"action":"terminate","status":"success"}'),
        ]
    )
    updates: list[AutomationStepUpdate] = []
    factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target()),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        model=model,
    )

    result = factory.create(
        target_id="adb-target",
        goal={"goal": "Use the old route"},
        on_step=updates.append,
        is_cancelled=lambda: False,
        instruction_source=lambda: current,
    ).run()

    assert result.status == "completed"
    assert executor.actions == []
    instruction_update = next(
        update for update in updates if update.action_type == "instruction_update"
    )
    assert instruction_update.state == "redirected"
    assert "新的用户指令" in instruction_update.summary
    assert "重新规划" in instruction_update.summary
    assert "Use the new route" in model.goals[1]


def test_factory_continues_sensitive_action_text_without_awaiting_user() -> None:
    executor = FakeExecutor([frame(), frame(), frame()])
    model = FakeModel(
        [
            "Action: pause for payment\n"
            + tool('{"action":"click","coordinate":[500,500]}'),
            tool('{"action":"terminate","status":"success"}'),
        ]
    )
    factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target()),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        model=model,
    )

    result = factory.create(
        target_id="adb-target",
        goal={"goal": "Continue"},
        on_step=lambda update: None,
        is_cancelled=lambda: False,
    ).run()

    assert result.status == "completed"
    assert result.error_code is None
    assert len(executor.actions) == 1


def test_factory_default_has_no_production_step_limit() -> None:
    factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target()),  # type: ignore[arg-type]
        executor=FakeExecutor([]),  # type: ignore[arg-type]
        model=FakeModel([]),
    )

    run = factory.create(
        target_id="adb-target",
        goal={"goal": "Continue"},
        on_step=lambda update: None,
        is_cancelled=lambda: False,
    )

    assert run.loop.max_steps is None
    assert run.loop.goal_source is None


def test_factory_normalizes_explicit_bare_coordinate_without_replanning() -> None:
    executor = FakeExecutor([frame(), frame()])
    model = FakeModel(
        [
            '<tool_call>{"name":"mobile_use","arguments":{"action":"click","coordinate":503, 446]}}</tool_call>',
            tool('{"action":"terminate","status":"success"}'),
        ]
    )
    updates: list[AutomationStepUpdate] = []
    factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target()),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        model=model,
    )

    result = factory.create(
        target_id="adb-target",
        goal={"goal": "Continue"},
        on_step=updates.append,
        is_cancelled=lambda: False,
    ).run()

    assert result.status == "completed"
    assert len(executor.actions) == 1
    assert not any(update.state == "redirected" for update in updates)


def test_factory_rejects_stale_dynamic_serial_and_invalid_goal() -> None:
    factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target(external_id="127.0.0.1:7555")),  # type: ignore[arg-type]
        executor=FakeExecutor([]),  # type: ignore[arg-type]
        model=FakeModel([]),
    )
    with pytest.raises(RuntimeError, match="chat_target_serial_changed"):
        factory.create(
            target_id="adb-target",
            goal={"goal": "Continue"},
            on_step=lambda update: None,
            is_cancelled=lambda: False,
        )

    valid_serial_factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target()),  # type: ignore[arg-type]
        executor=FakeExecutor([]),  # type: ignore[arg-type]
        model=FakeModel([]),
    )
    with pytest.raises(RuntimeError, match="execution_goal_invalid"):
        valid_serial_factory.create(
            target_id="adb-target",
            goal={"goal": ""},
            on_step=lambda update: None,
            is_cancelled=lambda: False,
        )


def test_chat_run_returns_target_busy_before_any_device_or_model_call_and_releases() -> None:
    executor = FakeExecutor([frame()])
    model = FakeModel([tool('{"action":"terminate","status":"success"}')])
    lease = DeviceExecutionLease()
    factory = RepositoryAndroidAutomationFactory(
        repository=FakeRepository(android_target()),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        model=model,
        device_lease=lease,
    )
    run = factory.create(
        target_id="adb-target",
        goal={"goal": "Continue"},
        on_step=lambda update: None,
        is_cancelled=lambda: False,
    )

    owner = lease.require(executor.serial)
    busy = run.run()

    assert busy.status == "failed"
    assert busy.error_code == "target_busy"
    assert executor.actions == []
    assert model.goals == []

    owner.release()
    completed = run.run()
    assert completed.status == "completed"
    assert lease.is_held(executor.serial) is False


@pytest.mark.parametrize(
    "code",
    [
        "executor_unicode_text_unavailable",
        "executor_unicode_text_target_not_found",
        "executor_unicode_text_discovery_invalid",
        "executor_unicode_text_rejected",
    ],
)
def test_chat_run_reports_unicode_input_not_sent(code: str) -> None:
    result = ChatAndroidAutomationRun(
        loop=ExplodingLoop(code),  # type: ignore[arg-type]
        target_key="127.0.0.1:16384",
    ).run()

    assert result.status == "failed"
    assert result.error_code == code
    assert result.detail == "无法使用设备的中文输入通道；文本确定未输入，未继续发送后续动作。"
    assert "暂停" not in result.detail
    assert "权限" not in result.detail


def test_chat_run_reports_uncertain_unicode_input_without_retry_claim() -> None:
    result = ChatAndroidAutomationRun(
        loop=ExplodingLoop("executor_unicode_text_uncertain"),  # type: ignore[arg-type]
        target_key="127.0.0.1:16384",
    ).run()

    assert result.status == "failed"
    assert result.error_code == "executor_unicode_text_uncertain"
    assert result.detail == "中文输入结果不确定，为避免重复输入已停止；未继续发送后续动作。"
    assert "暂停" not in result.detail
    assert "权限" not in result.detail
