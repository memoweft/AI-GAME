from __future__ import annotations

import pytest

from ai_game_console.android_automation import (
    AndroidAutomationEvent,
    AndroidAutomationGoalSnapshot,
    GuiOwlAndroidAutomation,
    parse_gui_owl_tool_call,
)
from ai_game_console.execution import ActionTransportResult, AndroidScreenshot, GuiAction


def screenshot(width: int = 1080, height: int = 1920) -> AndroidScreenshot:
    return AndroidScreenshot(png_bytes=b"fake-png", width=width, height=height)


class FakeDevice:
    def __init__(self, frames: list[AndroidScreenshot]) -> None:
        self.frames = iter(frames)
        self.capture_count = 0
        self.actions: list[GuiAction] = []

    def capture_screenshot(self) -> AndroidScreenshot:
        self.capture_count += 1
        return next(self.frames)

    def execute(self, action: GuiAction) -> ActionTransportResult:
        self.actions.append(action)
        return ActionTransportResult(accepted=True, detail="transport accepted")


class FakeModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.screenshots: list[AndroidScreenshot] = []
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def propose_action(
        self,
        frame: AndroidScreenshot,
        *,
        goal: str,
        action_history: tuple[str, ...],
    ) -> str:
        self.screenshots.append(frame)
        self.calls.append((goal, action_history))
        return next(self.responses)


def tool_call(payload: str) -> str:
    return f'<tool_call>{{"name":"mobile_use","arguments":{payload}}}</tool_call>'


def test_loop_uses_goal_safe_history_fresh_frames_and_post_action_observation() -> None:
    device = FakeDevice([screenshot(), screenshot()])
    model = FakeModel(
        [
            tool_call('{"action":"click","coordinate":[500,1000]}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )

    events: list[AndroidAutomationEvent] = []
    result = GuiOwlAndroidAutomation(
        target_id="adb:127.0.0.1:16384",
        goal="Open the local settings screen",
        device=device,
        model=model,
        max_steps=3,
        on_step_event=events.append,
    ).run()

    assert result.status == "terminated"
    assert result.termination_status == "success"
    # The post-action observation is the next decision frame; do not pay for
    # an immediately duplicated ADB screencap.
    assert device.capture_count == 2
    assert model.screenshots == [screenshot(), screenshot()]
    assert model.calls == [
        ("Open the local settings screen", ()),
        (
            "Open the local settings screen",
            ("step 1: transported tap at normalized [500,1000]",),
        ),
    ]
    assert device.actions == [
        GuiAction(
            target_id="adb:127.0.0.1:16384",
            action="tap",
            x=540,
            y=1919,
        )
    ]
    assert [(step.action, step.disposition) for step in result.steps] == [
        ("tap", "executed"),
        ("terminate", "terminated"),
    ]
    assert [(event.phase, event.action) for event in events] == [
        ("observed", None),
        ("proposed", "tap"),
        ("transported", "tap"),
        ("observed_after", "tap"),
        ("observed", None),
        ("proposed", "terminate"),
        ("terminated", "terminate"),
    ]
    assert all(event.width is None or event.width == 1080 for event in events)
    assert all(event.height is None or event.height == 1920 for event in events)


def test_instruction_update_during_model_proposal_discards_stale_action_and_replans() -> None:
    device = FakeDevice([screenshot(width=1000, height=1000) for _ in range(3)])
    current = AndroidAutomationGoalSnapshot(revision=0, goal="Tap the old control")

    class UpdatingModel(FakeModel):
        def propose_action(self, frame, *, goal, action_history):
            nonlocal current
            response = super().propose_action(
                frame,
                goal=goal,
                action_history=action_history,
            )
            if len(self.calls) == 1:
                current = AndroidAutomationGoalSnapshot(
                    revision=1,
                    goal="Tap the new control",
                )
            return response

    model = UpdatingModel(
        [
            tool_call('{"action":"click","coordinate":[100,100]}'),
            tool_call('{"action":"click","coordinate":[900,900]}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )
    events: list[AndroidAutomationEvent] = []

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Fixed fallback goal",
        goal_source=lambda: current,
        device=device,
        model=model,
        on_step_event=events.append,
    ).run()

    assert result.status == "terminated"
    assert device.actions == [
        GuiAction(target_id="adb:target", action="tap", x=899, y=899)
    ]
    assert [call[0] for call in model.calls] == [
        "Tap the old control",
        "Tap the new control",
        "Tap the new control",
    ]
    assert [(step.action, step.disposition) for step in result.steps] == [
        ("instruction_update", "redirected"),
        ("tap", "executed"),
        ("terminate", "terminated"),
    ]
    assert ("redirected", "instruction_update") in [
        (event.phase, event.action) for event in events
    ]


def test_instruction_update_at_final_execute_checkpoint_discards_stale_action() -> None:
    device = FakeDevice([screenshot(width=1000, height=1000) for _ in range(3)])
    old_goal = AndroidAutomationGoalSnapshot(revision=0, goal="Tap the old control")
    new_goal = AndroidAutomationGoalSnapshot(revision=1, goal="Tap the new control")
    source_calls = 0

    def goal_source() -> AndroidAutomationGoalSnapshot:
        nonlocal source_calls
        source_calls += 1
        return old_goal if source_calls < 3 else new_goal

    model = FakeModel(
        [
            tool_call('{"action":"click","coordinate":[100,100]}'),
            tool_call('{"action":"click","coordinate":[900,900]}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Fixed fallback goal",
        goal_source=goal_source,
        device=device,
        model=model,
    ).run()

    assert result.status == "terminated"
    assert device.actions == [
        GuiAction(target_id="adb:target", action="tap", x=899, y=899)
    ]
    assert [call[0] for call in model.calls] == [
        "Tap the old control",
        "Tap the new control",
        "Tap the new control",
    ]
    assert result.steps[0].action == "instruction_update"
    assert result.steps[0].disposition == "redirected"


def test_instruction_update_at_final_terminate_checkpoint_forces_replan() -> None:
    device = FakeDevice([screenshot(), screenshot()])
    old_goal = AndroidAutomationGoalSnapshot(revision=0, goal="Finish the old task")
    new_goal = AndroidAutomationGoalSnapshot(revision=1, goal="Continue with the new task")
    source_calls = 0

    def goal_source() -> AndroidAutomationGoalSnapshot:
        nonlocal source_calls
        source_calls += 1
        return old_goal if source_calls < 3 else new_goal

    model = FakeModel(
        [
            tool_call('{"action":"terminate","status":"success"}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Fixed fallback goal",
        goal_source=goal_source,
        device=device,
        model=model,
    ).run()

    assert result.status == "terminated"
    assert device.actions == []
    assert [call[0] for call in model.calls] == [
        "Finish the old task",
        "Continue with the new task",
    ]
    assert [(step.action, step.disposition) for step in result.steps] == [
        ("instruction_update", "redirected"),
        ("terminate", "terminated"),
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            '{"action":"long_press","coordinate":[100,200],"time":0.7}',
            GuiAction(
                target_id="adb:target",
                action="long_press",
                x=100,
                y=200,
                duration_ms=700,
            ),
        ),
        (
            '{"action":"swipe","coordinate":[0,1000],"coordinate2":[1000,0],"duration":900}',
            GuiAction(
                target_id="adb:target",
                action="swipe",
                x=0,
                y=999,
                end_x=999,
                end_y=0,
                duration_ms=900,
            ),
        ),
        (
            '{"action":"system_button","button":"recent"}',
            GuiAction(
                target_id="adb:target",
                action="keyevent",
                keycode="KEYCODE_APP_SWITCH",
            ),
        ),
        (
            '{"action":"system_button","arguments":{"action":"Home"}}',
            GuiAction(
                target_id="adb:target",
                action="keyevent",
                keycode="KEYCODE_HOME",
            ),
        ),
        (
            '{"action":"type","text":"safe text"}',
            GuiAction(
                target_id="adb:target",
                action="text",
                text="safe text",
            ),
        ),
    ],
)
def test_parser_maps_official_actions_to_restricted_atomic_actions(payload, expected) -> None:
    decision = parse_gui_owl_tool_call(
        tool_call(payload),
        target_id="adb:target",
        screenshot=screenshot(width=1000, height=1000),
    )

    assert decision.kind == "execute"
    assert decision.action == expected


def test_interact_is_recorded_then_model_is_required_to_continue_automatically() -> None:
    device = FakeDevice([screenshot(), screenshot(), screenshot(), screenshot(), screenshot()])
    model = FakeModel(
        [
            tool_call('{"action":"wait","time":0.25}'),
            tool_call('{"action":"interact","message":"confirm"}'),
            tool_call('{"action":"click","coordinate":[500,500]}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )
    waits: list[float] = []

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Wait for a safe screen",
        device=device,
        model=model,
        waiter=waits.append,
    ).run()

    assert result.status == "terminated"
    assert result.blocker is None
    assert waits == [0.25]
    assert device.actions == [
        GuiAction(target_id="adb:target", action="tap", x=540, y=960)
    ]
    assert [(step.action, step.disposition) for step in result.steps] == [
        ("wait", "waited"),
        ("interact", "redirected"),
        ("tap", "executed"),
        ("terminate", "terminated"),
    ]
    assert any("REPLAN REQUIRED" in item for item in model.calls[2][1])


def test_three_consecutive_waits_force_a_physical_action_replan_in_next_history() -> None:
    device = FakeDevice([screenshot() for _ in range(6)])
    model = FakeModel(
        [
            tool_call('{"action":"wait","time":0}'),
            tool_call('{"action":"wait","time":0}'),
            tool_call('{"action":"wait","time":0}'),
            tool_call('{"action":"click","coordinate":[500,500]}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Progress through the current screen",
        device=device,
        model=model,
        waiter=lambda _: None,
    ).run()

    assert result.status == "terminated"
    assert len(device.actions) == 1
    assert any("REPLAN REQUIRED" in item for item in model.calls[3][1])
    assert any("do not choose wait or interact" in item for item in model.calls[3][1])


def test_repeated_taps_in_same_region_force_a_different_control_replan() -> None:
    device = FakeDevice([screenshot(width=1000, height=1000) for _ in range(5)])
    model = FakeModel(
        [
            tool_call('{"action":"click","coordinate":[900,100]}'),
            tool_call('{"action":"click","coordinate":[910,110]}'),
            tool_call('{"action":"click","coordinate":[905,105]}'),
            tool_call('{"action":"click","coordinate":[500,800]}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Advance the tutorial instead of repeating one control",
        device=device,
        model=model,
    ).run()

    assert result.status == "terminated"
    assert any("normalized [900,100]" in item for item in model.calls[1][1])
    assert any("REPLAN REQUIRED" in item for item in model.calls[3][1])
    assert any("same screen region" in item for item in model.calls[3][1])
    assert any("tutorial hand" in item for item in model.calls[3][1])
    assert any("fingertip" in item for item in model.calls[3][1])


def test_model_wait_is_capped_for_fast_screen_polling() -> None:
    device = FakeDevice([screenshot(), screenshot()])
    model = FakeModel(
        [
            tool_call('{"action":"wait","time":10}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )
    waits: list[float] = []

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Poll the game quickly",
        device=device,
        model=model,
        waiter=waits.append,
    ).run()

    assert result.status == "terminated"
    assert waits == [0.5]


@pytest.mark.parametrize(
    "coordinate",
    [
        "503, 446",
        "503, 446]",
        "[503, 446",
    ],
)
def test_explicit_malformed_coordinate_pair_is_normalized_without_an_extra_model_turn(
    coordinate: str,
) -> None:
    device = FakeDevice([screenshot(), screenshot()])
    model = FakeModel(
        [
            '<tool_call>{"name":"mobile_use","arguments":{"action":"click","coordinate":'
            + coordinate
            + '}}</tool_call>',
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )
    events: list[AndroidAutomationEvent] = []

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Use the explicit model coordinates",
        device=device,
        model=model,
        on_step_event=events.append,
    ).run()

    assert result.status == "terminated"
    assert device.actions == [
        GuiAction(target_id="adb:target", action="tap", x=543, y=856)
    ]
    assert len(model.calls) == 2
    assert not any(event.phase == "redirected" for event in events)


@pytest.mark.parametrize(
    "response",
    [
        '<tool_call>{"action":"click","coordinate":[503,446]}</tool_call>',
        '<tool_call>{"action":"click","coordinate":[503,446]}}</tool_call>',
        'click\n{"name":"mobile_use","arguments":{"action":"click","coordinate":[503,446]}}\n</tool_call>',
        'click\n{"name":"mobile_use","arguments":{"action":"click","coordinate":503,446]}}\n</tool_call>',
    ],
)
def test_direct_mobile_action_payload_is_normalized_without_replanning(
    response: str,
) -> None:
    decision = parse_gui_owl_tool_call(
        response,
        target_id="adb:target",
        screenshot=screenshot(),
    )

    assert decision.kind == "execute"
    assert decision.action == GuiAction(
        target_id="adb:target",
        action="tap",
        x=543,
        y=856,
    )


def test_invalid_tool_call_is_redirected_then_format_repaired_on_a_fresh_frame() -> None:
    device = FakeDevice([screenshot() for _ in range(4)])
    model = FakeModel(
        [
            tool_call('{"action":"click","coordinate":"not-a-point"}'),
            tool_call('{"action":"click","coordinate":[500,500]}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )
    events: list[AndroidAutomationEvent] = []

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Recover from an invalid model action format",
        device=device,
        model=model,
        on_step_event=events.append,
    ).run()

    assert result.status == "terminated"
    assert len(device.actions) == 1
    assert [(step.action, step.disposition) for step in result.steps] == [
        ("invalid_tool_call", "redirected"),
        ("tap", "executed"),
        ("terminate", "terminated"),
    ]
    assert ("redirected", "invalid_tool_call") in [
        (event.phase, event.action) for event in events
    ]
    assert any("FORMAT REPAIR REQUIRED" in item for item in model.calls[1][1])
    assert any("coordinate/coordinate2" in item for item in model.calls[1][1])
    assert any("JSON arrays [x,y]" in item for item in model.calls[1][1])


@pytest.mark.parametrize(
    "action_text",
    [
        "Action: enter the password",
        "Action: wait for OTP verification",
        "Action: confirm 实名认证",
        "Action: confirm payment",
        "Action: solve CAPTCHA",
        "Action: grant permission",
        "Action: accept terms of service",
        "Action: 无法判断当前页面",
    ],
)
def test_sensitive_or_ambiguous_action_text_does_not_pause_execution(action_text: str) -> None:
    device = FakeDevice([screenshot(), screenshot(), screenshot()])
    model = FakeModel(
        [
            action_text + "\n" + tool_call('{"action":"click","coordinate":[1,2]}'),
            tool_call('{"action":"terminate","status":"success"}'),
        ]
    )

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Continue automatically in test mode",
        device=device,
        model=model,
    ).run()

    assert result.status == "terminated"
    assert result.blocker is None
    assert len(device.actions) == 1


def test_cancellation_is_checked_before_capture_and_before_physical_execution() -> None:
    not_started = FakeDevice([screenshot()])
    never_called = FakeModel([tool_call('{"action":"terminate","status":"success"}')])

    before_capture = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Do nothing after cancellation",
        device=not_started,
        model=never_called,
        is_cancelled=lambda: True,
    ).run()
    assert before_capture.status == "cancelled"
    assert not_started.capture_count == 0

    device = FakeDevice([screenshot()])
    model = FakeModel([tool_call('{"action":"click","coordinate":[1,2]}')])
    after_model = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Do nothing after cancellation",
        device=device,
        model=model,
        is_cancelled=lambda: len(model.screenshots) == 1,
    ).run()
    assert after_model.status == "cancelled"
    assert device.actions == []


def test_cancellation_after_observation_does_not_claim_instructions_or_call_model() -> None:
    device = FakeDevice([screenshot()])
    model = FakeModel([tool_call('{"action":"terminate","status":"success"}')])
    cancelled = False
    source_calls = 0

    def on_event(event: AndroidAutomationEvent) -> None:
        nonlocal cancelled
        if event.phase == "observed":
            cancelled = True

    def goal_source() -> AndroidAutomationGoalSnapshot:
        nonlocal source_calls
        source_calls += 1
        return AndroidAutomationGoalSnapshot(revision=1, goal="Do not start")

    result = GuiOwlAndroidAutomation(
        target_id="adb:target",
        goal="Do not start",
        goal_source=goal_source,
        device=device,
        model=model,
        is_cancelled=lambda: cancelled,
        on_step_event=on_event,
    ).run()

    assert result.status == "cancelled"
    assert device.capture_count == 1
    assert source_calls == 0
    assert model.calls == []
    assert device.actions == []


def test_default_loop_executes_more_than_twenty_actions_then_terminates() -> None:
    action_count = 21
    device = FakeDevice([screenshot() for _ in range(action_count + 1)])
    model = FakeModel(
        [tool_call('{"action":"click","coordinate":[1,2]}')] * action_count
        + [tool_call('{"action":"terminate","status":"success"}')]
    )
    result = GuiOwlAndroidAutomation(
        target_id="adb:target", goal="Unbounded test", device=device, model=model
    ).run()

    assert result.status == "terminated"
    assert result.termination_status == "success"
    assert len(device.actions) == action_count
    assert device.capture_count == action_count + 1


def test_parser_rejects_malformed_or_unsafe_tool_calls() -> None:

    with pytest.raises(ValueError, match="exactly one"):
        parse_gui_owl_tool_call(
            "not a tool call",
            target_id="adb:target",
            screenshot=screenshot(),
        )
    with pytest.raises(ValueError, match="normalized"):
        parse_gui_owl_tool_call(
            tool_call('{"action":"click","coordinate":[1001,2]}'),
            target_id="adb:target",
            screenshot=screenshot(),
        )
    with pytest.raises(ValueError, match="unsupported"):
        parse_gui_owl_tool_call(
            tool_call('{"action":"open","text":"Settings"}'),
            target_id="adb:target",
            screenshot=screenshot(),
        )
    with pytest.raises(ValueError, match="mobile_use"):
        parse_gui_owl_tool_call(
            '<tool_call>{"name":"other_tool","arguments":{"action":"terminate"}}</tool_call>',
            target_id="adb:target",
            screenshot=screenshot(),
        )
