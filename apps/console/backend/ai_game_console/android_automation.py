from __future__ import annotations

import json
import re
import time
from itertools import count
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from .execution import ActionTransportResult, AndroidScreenshot, GuiAction


class AndroidDeviceAdapter(Protocol):
    """Minimal physical-device boundary needed by Android automation."""

    def capture_screenshot(self) -> AndroidScreenshot: ...

    def execute(self, action: GuiAction) -> ActionTransportResult: ...


class GuiOwlMultimodalModel(Protocol):
    """The injected model receives only the current image and safe text context."""

    def propose_action(
        self,
        screenshot: AndroidScreenshot,
        *,
        goal: str,
        action_history: tuple[str, ...],
    ) -> str: ...


class AndroidAutomation(Protocol):
    """Run an Android screenshot-to-action loop until it terminates or is cancelled."""

    def run(self) -> "AndroidAutomationResult": ...


CancellationCheck = Callable[[], bool]
Waiter = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class AndroidAutomationGoalSnapshot:
    """One immutable, revisioned goal presented to the GUI planner."""

    revision: int
    goal: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError("goal snapshot revision must be a non-negative integer")
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("goal snapshot goal must not be blank")
        object.__setattr__(self, "goal", self.goal.strip())


AndroidAutomationGoalSource = Callable[[], AndroidAutomationGoalSnapshot]


@dataclass(frozen=True, slots=True)
class AndroidAutomationDecision:
    kind: Literal["execute", "wait", "terminate", "interact"]
    action: GuiAction | None = None
    wait_seconds: float | None = None
    termination_status: Literal["success", "failure"] | None = None


@dataclass(frozen=True, slots=True)
class AndroidAutomationStep:
    index: int
    action: str | None
    disposition: Literal["executed", "waited", "terminated", "redirected", "paused", "blocked"]


@dataclass(frozen=True, slots=True)
class AndroidAutomationEvent:
    """Small persistence-safe timeline record emitted by the adapter loop."""

    index: int
    phase: Literal[
        "observed",
        "proposed",
        "transported",
        "observed_after",
        "waiting",
        "redirected",
        "paused",
        "terminated",
    ]
    action: str | None
    width: int | None = None
    height: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AndroidAutomationResult:
    status: Literal["terminated", "paused", "cancelled", "max_steps"]
    steps: tuple[AndroidAutomationStep, ...]
    blocker: str | None = None
    termination_status: Literal["success", "failure"] | None = None


StepEventHook = Callable[[AndroidAutomationEvent], None]


class GuiOwlAndroidAutomation:
    """Cancellation-aware GUI-Owl loop over an Android adapter.

    Each iteration gets a fresh PNG screenshot and executes at most one atomic
    device action. Test mode has no production step cap: it continues until the
    model terminates, the caller cancels, or a device/model failure is raised.
    ``interact`` is recorded as a redirection and the next model turn is asked
    to choose an actual action instead of handing control back to a user.
    """

    DEFAULT_MAX_STEPS: int | None = None
    DEFAULT_WAIT_SECONDS = 1.0
    MAX_WAIT_SECONDS = 0.5
    MAX_CONSECUTIVE_WAITS_BEFORE_REPLAN = 3
    MAX_CONSECUTIVE_TAPS_IN_REGION_BEFORE_REPLAN = 3
    SAME_TAP_REGION_RADIUS = 80

    def __init__(
        self,
        *,
        target_id: str,
        goal: str,
        device: AndroidDeviceAdapter,
        model: GuiOwlMultimodalModel,
        max_steps: int | None = DEFAULT_MAX_STEPS,
        is_cancelled: CancellationCheck | None = None,
        waiter: Waiter = time.sleep,
        on_step_event: StepEventHook | None = None,
        goal_source: AndroidAutomationGoalSource | None = None,
    ) -> None:
        if not target_id.strip():
            raise ValueError("target_id must not be blank")
        if not goal.strip():
            raise ValueError("goal must not be blank")
        if max_steps is not None and (
            isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1
        ):
            raise ValueError("max_steps must be a positive integer or None")
        self.target_id = target_id.strip()
        self.goal = goal.strip()
        self.device = device
        self.model = model
        self.max_steps = max_steps
        self.is_cancelled = is_cancelled or (lambda: False)
        self.waiter = waiter
        self.on_step_event = on_step_event
        self.goal_source = goal_source

    def run(self) -> AndroidAutomationResult:
        steps: list[AndroidAutomationStep] = []
        action_history: list[str] = []
        consecutive_waits = 0
        previous_tap_normalized: tuple[int, int] | None = None
        consecutive_taps_in_region = 0
        next_screenshot: AndroidScreenshot | None = None
        for index in count(1):
            if self.max_steps is not None and index > self.max_steps:
                return AndroidAutomationResult(status="max_steps", steps=tuple(steps))
            if self.is_cancelled():
                return AndroidAutomationResult(status="cancelled", steps=tuple(steps))

            # The verified post-action frame is already the freshest available
            # observation. Reuse it for the next decision instead of issuing a
            # second, immediately adjacent ADB screencap.
            screenshot = next_screenshot or self.device.capture_screenshot()
            next_screenshot = None
            self._emit(
                AndroidAutomationEvent(
                    index=index,
                    phase="observed",
                    action=None,
                    width=screenshot.width,
                    height=screenshot.height,
                )
            )
            if self.is_cancelled():
                return AndroidAutomationResult(status="cancelled", steps=tuple(steps))
            planned_goal = self._latest_goal_snapshot()
            if self.is_cancelled():
                return AndroidAutomationResult(status="cancelled", steps=tuple(steps))
            response = self.model.propose_action(
                screenshot,
                goal=planned_goal.goal,
                action_history=tuple(action_history),
            )
            if self.is_cancelled():
                return AndroidAutomationResult(status="cancelled", steps=tuple(steps))
            if self._redirect_for_instruction_update(
                index=index,
                planned_goal=planned_goal,
                steps=steps,
                action_history=action_history,
            ):
                consecutive_waits = 0
                previous_tap_normalized = None
                consecutive_taps_in_region = 0
                continue
            try:
                decision = parse_gui_owl_tool_call(
                    response,
                    target_id=self.target_id,
                    screenshot=screenshot,
                )
            except ValueError:
                # A malformed model tool call is not a device fault. Never
                # send a guessed action: record the invalid response and ask
                # for a correctly formatted choice against a fresh frame.
                self._emit(
                    AndroidAutomationEvent(
                        index=index,
                        phase="redirected",
                        action="invalid_tool_call",
                    )
                )
                steps.append(
                    AndroidAutomationStep(
                        index=index,
                        action="invalid_tool_call",
                        disposition="redirected",
                    )
                )
                action_history.append(_format_repair_instruction(index))
                consecutive_waits = 0
                continue
            action_name = decision.action.action if decision.action is not None else decision.kind
            self._emit(
                AndroidAutomationEvent(
                    index=index,
                    phase="proposed",
                    action=action_name,
                )
            )

            # A model response can be slow. Check again after parsing and
            # immediately before any physical input is transported.
            if self.is_cancelled():
                return AndroidAutomationResult(status="cancelled", steps=tuple(steps))

            if decision.kind == "execute":
                action = decision.action
                if action is None:  # pragma: no cover - parser invariant
                    raise RuntimeError("execution decision without action")
                if self.is_cancelled():
                    return AndroidAutomationResult(status="cancelled", steps=tuple(steps))
                if self._redirect_for_instruction_update(
                    index=index,
                    planned_goal=planned_goal,
                    steps=steps,
                    action_history=action_history,
                ):
                    consecutive_waits = 0
                    previous_tap_normalized = None
                    consecutive_taps_in_region = 0
                    continue
                transport = self.device.execute(action)
                if not transport.accepted:
                    raise RuntimeError("executor_action_rejected")
                self._emit(
                    AndroidAutomationEvent(
                        index=index,
                        phase="transported",
                        action=action.action,
                    )
                )
                steps.append(
                    AndroidAutomationStep(
                        index=index,
                        action=action.action,
                        disposition="executed",
                    )
                )
                if action.action == "tap" and action.x is not None and action.y is not None:
                    normalized_tap = (
                        _pixel_to_normalized(action.x, screenshot.width),
                        _pixel_to_normalized(action.y, screenshot.height),
                    )
                    if (
                        previous_tap_normalized is not None
                        and abs(normalized_tap[0] - previous_tap_normalized[0])
                        <= self.SAME_TAP_REGION_RADIUS
                        and abs(normalized_tap[1] - previous_tap_normalized[1])
                        <= self.SAME_TAP_REGION_RADIUS
                    ):
                        consecutive_taps_in_region += 1
                    else:
                        consecutive_taps_in_region = 1
                    previous_tap_normalized = normalized_tap
                    action_history.append(
                        f"step {index}: transported tap at normalized "
                        f"[{normalized_tap[0]},{normalized_tap[1]}]"
                    )
                    if (
                        consecutive_taps_in_region
                        >= self.MAX_CONSECUTIVE_TAPS_IN_REGION_BEFORE_REPLAN
                    ):
                        action_history.append(
                            _repeated_tap_region_replan_instruction(
                                index,
                                normalized_tap,
                            )
                        )
                        consecutive_taps_in_region = 0
                        previous_tap_normalized = None
                else:
                    action_history.append(f"step {index}: transported {action.action}")
                    consecutive_taps_in_region = 0
                    previous_tap_normalized = None
                consecutive_waits = 0
                # A transport acknowledgement is not UI completion. Always
                # capture a distinct follow-up frame for verification/timeline.
                observed_after = self.device.capture_screenshot()
                self._emit(
                    AndroidAutomationEvent(
                        index=index,
                        phase="observed_after",
                        action=action.action,
                        width=observed_after.width,
                        height=observed_after.height,
                    )
                )
                next_screenshot = observed_after
                continue
            if decision.kind == "wait":
                self._emit(
                    AndroidAutomationEvent(index=index, phase="waiting", action="wait")
                )
                requested_wait = (
                    self.DEFAULT_WAIT_SECONDS
                    if decision.wait_seconds is None
                    else decision.wait_seconds
                )
                self.waiter(min(requested_wait, self.MAX_WAIT_SECONDS))
                steps.append(
                    AndroidAutomationStep(index=index, action="wait", disposition="waited")
                )
                action_history.append(f"step {index}: waited")
                consecutive_waits += 1
                if consecutive_waits >= self.MAX_CONSECUTIVE_WAITS_BEFORE_REPLAN:
                    action_history.append(_physical_action_replan_instruction(index))
                continue
            if decision.kind == "terminate":
                if self._redirect_for_instruction_update(
                    index=index,
                    planned_goal=planned_goal,
                    steps=steps,
                    action_history=action_history,
                ):
                    consecutive_waits = 0
                    previous_tap_normalized = None
                    consecutive_taps_in_region = 0
                    continue
                self._emit(
                    AndroidAutomationEvent(index=index, phase="terminated", action="terminate")
                )
                steps.append(
                    AndroidAutomationStep(
                        index=index,
                        action="terminate",
                        disposition="terminated",
                    )
                )
                return AndroidAutomationResult(
                    status="terminated",
                    steps=tuple(steps),
                    termination_status=decision.termination_status,
                )

            # Legacy GUI-Owl variants can still emit ``interact``. There is no
            # manual handoff in test mode, so persist the redirect and obtain a
            # fresh next decision instead of pausing the task.
            self._emit(
                AndroidAutomationEvent(
                    index=index,
                    phase="redirected",
                    action="interact",
                )
            )
            steps.append(
                AndroidAutomationStep(index=index, action="interact", disposition="redirected")
            )
            action_history.append(
                f"step {index}: model requested interact; no manual handoff is available."
            )
            action_history.append(_physical_action_replan_instruction(index))
            consecutive_waits = 0
            continue

    def _emit(self, event: AndroidAutomationEvent) -> None:
        if self.on_step_event is not None:
            self.on_step_event(event)

    def _latest_goal_snapshot(self) -> AndroidAutomationGoalSnapshot:
        if self.goal_source is None:
            return AndroidAutomationGoalSnapshot(revision=0, goal=self.goal)
        snapshot = self.goal_source()
        if not isinstance(snapshot, AndroidAutomationGoalSnapshot):
            raise TypeError("goal source must return AndroidAutomationGoalSnapshot")
        return snapshot

    def _redirect_for_instruction_update(
        self,
        *,
        index: int,
        planned_goal: AndroidAutomationGoalSnapshot,
        steps: list[AndroidAutomationStep],
        action_history: list[str],
    ) -> bool:
        if self.goal_source is None:
            return False
        latest_goal = self._latest_goal_snapshot()
        if latest_goal.revision == planned_goal.revision:
            return False
        reason = (
            f"instruction revision changed from {planned_goal.revision} "
            f"to {latest_goal.revision}"
        )
        self._emit(
            AndroidAutomationEvent(
                index=index,
                phase="redirected",
                action="instruction_update",
                reason=reason,
            )
        )
        steps.append(
            AndroidAutomationStep(
                index=index,
                action="instruction_update",
                disposition="redirected",
            )
        )
        action_history.append(
            f"step {index}: {reason}; the stale model proposal was discarded before "
            "device dispatch. Replan from a fresh observation using the latest goal."
        )
        return True


def _physical_action_replan_instruction(index: int) -> str:
    return (
        f"step {index}: REPLAN REQUIRED. The next decision must choose one physical "
        "mobile_use action (click, long_press, swipe, type, or system_button) from the "
        "current screenshot; do not choose wait or interact."
    )


def _format_repair_instruction(index: int) -> str:
    return (
        f"step {index}: FORMAT REPAIR REQUIRED; valid JSON only; "
        "coordinate/coordinate2 must be JSON arrays [x,y]."
    )


def _repeated_tap_region_replan_instruction(
    index: int,
    normalized_tap: tuple[int, int],
) -> str:
    return (
        f"step {index}: REPLAN REQUIRED. Repeated taps in the same screen region near "
        f"normalized [{normalized_tap[0]},{normalized_tap[1]}] did not advance the task. "
        "Do not repeat that region or auxiliary Help/Back controls. First scan for a "
        "semi-transparent tutorial hand or pulsing golden ring and click the control at "
        "its fingertip; otherwise choose a different highlighted control that advances "
        "the current instruction."
    )


_TOOL_CALL_PATTERN = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_BARE_COORDINATE_PAIR_PATTERN = re.compile(
    r'(?P<prefix>"coordinate(?:2)?"\s*:\s*)'
    r'\[?\s*(?P<x>-?\d+)\s*,\s*(?P<y>-?\d+)\s*\]?'
    r'(?=\s*[,}])'
)
_SYSTEM_BUTTON_KEYCODES = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "menu": "KEYCODE_APP_SWITCH",
    "recent": "KEYCODE_APP_SWITCH",
    "recents": "KEYCODE_APP_SWITCH",
    "app_switch": "KEYCODE_APP_SWITCH",
    "enter": "KEYCODE_ENTER",
}


def parse_gui_owl_tool_call(
    response: str,
    *,
    target_id: str,
    screenshot: AndroidScreenshot,
) -> AndroidAutomationDecision:
    """Parse one official GUI-Owl ``<tool_call>`` JSON envelope safely."""

    matches = _extract_tool_call_payloads(response)
    if len(matches) != 1:
        raise ValueError("expected exactly one GUI-Owl tool call")
    raw_payload = _BARE_COORDINATE_PAIR_PATTERN.sub(
        lambda match: (
            f'{match.group("prefix")}'
            f'[{match.group("x")},{match.group("y")}]'
        ),
        matches[0],
    ).strip()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        # GUI-Owl 1.5 sometimes emits an otherwise unambiguous coordinate as
        # `"coordinate":503,446`, `503,446]`, or `[503,446`. Normalize only
        # this explicit numeric pair into the documented JSON array form; all
        # bounds/type checks still run below.
        # A second frequent 1.5-8B variant emits one unmatched trailing brace.
        # Accept only the narrow JSONDecoder "Extra data" case where that one
        # final delimiter is the entire suffix after an otherwise valid object.
        if (
            exc.msg == "Extra data"
            and exc.pos == len(raw_payload) - 1
            and raw_payload[-1] in "}]"
        ):
            try:
                payload = json.loads(raw_payload[:-1])
            except json.JSONDecodeError:
                raise ValueError("GUI-Owl tool call is not valid JSON") from exc
        else:
            raise ValueError("GUI-Owl tool call is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("GUI-Owl tool call must be a JSON object")
    if payload.get("name") == "mobile_use":
        arguments = payload.get("arguments")
    elif "name" not in payload and isinstance(payload.get("action"), str):
        # GUI-Owl sometimes places the mobile_use arguments directly inside
        # <tool_call>. The surrounding tag already supplies the tool boundary.
        arguments = payload
    else:
        raise ValueError("GUI-Owl tool call must invoke mobile_use")
    if not isinstance(arguments, dict):
        raise ValueError("GUI-Owl mobile_use arguments must be a JSON object")
    action_name = arguments.get("action")
    if not isinstance(action_name, str):
        raise ValueError("GUI-Owl action must be a string")

    if action_name == "click":
        x, y = _coordinates(arguments.get("coordinate"), screenshot)
        return AndroidAutomationDecision(
            kind="execute",
            action=GuiAction(target_id=target_id, action="tap", x=x, y=y),
        )
    if action_name == "long_press":
        x, y = _coordinates(arguments.get("coordinate"), screenshot)
        return AndroidAutomationDecision(
            kind="execute",
            action=GuiAction(
                target_id=target_id,
                action="long_press",
                x=x,
                y=y,
                duration_ms=_duration_ms(arguments),
            ),
        )
    if action_name in {"scroll", "swipe"}:
        start, end = _swipe_coordinates(
            arguments.get("coordinate"),
            screenshot,
            second_coordinate=arguments.get("coordinate2"),
        )
        return AndroidAutomationDecision(
            kind="execute",
            action=GuiAction(
                target_id=target_id,
                action="swipe",
                x=start[0],
                y=start[1],
                end_x=end[0],
                end_y=end[1],
                duration_ms=_duration_ms(arguments),
            ),
        )
    if action_name == "type":
        text = arguments.get("text")
        if not isinstance(text, str) or not text or len(text) > 200:
            raise ValueError("GUI-Owl type text must be 1..200 characters")
        return AndroidAutomationDecision(
            kind="execute",
            action=GuiAction(target_id=target_id, action="text", text=text),
        )
    if action_name == "system_button":
        button = arguments.get("button")
        nested_arguments = arguments.get("arguments")
        if button is None and isinstance(nested_arguments, dict):
            # GUI-Owl 1.5 sometimes nests the selected system button as an
            # ``action`` value even though the outer action already names the
            # tool operation. Accept only a known button token; no coordinates
            # or arbitrary nested action are inferred.
            button = nested_arguments.get("action")
        if not isinstance(button, str) or button.lower() not in _SYSTEM_BUTTON_KEYCODES:
            raise ValueError("unsupported GUI-Owl system button")
        return AndroidAutomationDecision(
            kind="execute",
            action=GuiAction(
                target_id=target_id,
                action="keyevent",
                keycode=_SYSTEM_BUTTON_KEYCODES[button.lower()],
            ),
        )
    if action_name == "wait":
        return AndroidAutomationDecision(kind="wait", wait_seconds=_wait_seconds(arguments))
    if action_name == "terminate":
        status = arguments.get("status")
        if status not in {"success", "failure"}:
            raise ValueError("GUI-Owl terminate status must be success or failure")
        return AndroidAutomationDecision(
            kind="terminate",
            termination_status=status,
        )
    if action_name == "interact":
        return AndroidAutomationDecision(kind="interact")
    raise ValueError("unsupported GUI-Owl action")


def _extract_tool_call_payloads(response: str) -> list[str]:
    matches = _TOOL_CALL_PATTERN.findall(response)
    if matches:
        return matches

    # GUI-Owl 1.5 can occasionally omit only the opening tag while keeping a
    # complete JSON object and one closing tag. Recover that narrow form; the
    # normal schema/action/bounds validation below still applies in full.
    if response.count("</tool_call>") != 1 or "<tool_call>" in response:
        return []
    before, after = response.split("</tool_call>", 1)
    if after.strip():
        return []
    object_start = before.find("{")
    if object_start < 0:
        return []
    candidate = before[object_start:].strip()
    # Apply the same narrowly scoped coordinate-pair repair used by the main
    # parser before asking JSONDecoder to locate the end of an opening-tagless
    # object. Otherwise a recoverable GUI-Owl 1.5 pair such as
    # ``"coordinate":503,446]`` is rejected here before the main parser gets
    # a chance to normalize it.
    candidate = _BARE_COORDINATE_PAIR_PATTERN.sub(
        lambda match: (
            f'{match.group("prefix")}'
            f'[{match.group("x")},{match.group("y")}]'
        ),
        candidate,
    )
    try:
        _, object_end = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        return []
    if candidate[object_end:].strip():
        return []
    return [candidate[:object_end]]


def _coordinates(value: object, screenshot: AndroidScreenshot) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("GUI-Owl coordinate must be [x, y]")
    return (_normalize_coordinate(value[0], screenshot.width), _normalize_coordinate(value[1], screenshot.height))


def _swipe_coordinates(
    value: object,
    screenshot: AndroidScreenshot,
    *,
    second_coordinate: object | None,
) -> tuple[tuple[int, int], tuple[int, int]]:
    if second_coordinate is not None:
        return _coordinates(value, screenshot), _coordinates(second_coordinate, screenshot)
    if isinstance(value, list) and len(value) == 2 and all(isinstance(item, list) for item in value):
        return _coordinates(value[0], screenshot), _coordinates(value[1], screenshot)
    if isinstance(value, list) and len(value) == 4:
        return (
            _normalize_coordinate(value[0], screenshot.width),
            _normalize_coordinate(value[1], screenshot.height),
        ), (
            _normalize_coordinate(value[2], screenshot.width),
            _normalize_coordinate(value[3], screenshot.height),
        )
    raise ValueError("GUI-Owl swipe coordinate must contain two points")


def _normalize_coordinate(value: object, length: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
        raise ValueError("GUI-Owl coordinates must be normalized integers from 0 to 1000")
    if length < 1:
        raise ValueError("screenshot dimensions must be positive")
    return round(value * (length - 1) / 1000)


def _pixel_to_normalized(value: int, length: int) -> int:
    if length < 1:
        raise ValueError("screenshot dimensions must be positive")
    if length == 1:
        return 0
    return round(value * 1000 / (length - 1))


def _duration_ms(payload: dict[object, object]) -> int:
    if "duration_ms" in payload:
        value = payload["duration_ms"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
            raise ValueError("GUI-Owl gesture duration must be 1..10000 milliseconds")
        return value
    if "time" in payload:
        seconds = payload["time"]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 10:
            raise ValueError("GUI-Owl gesture time must be greater than 0 and at most 10 seconds")
        return round(float(seconds) * 1000)
    value = payload.get("duration", 600)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
        raise ValueError("GUI-Owl gesture duration must be 1..10000 milliseconds")
    return value


def _wait_seconds(payload: dict[object, object]) -> float:
    value = payload.get("time", payload.get("seconds", payload.get("duration", 1.0)))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10:
        raise ValueError("GUI-Owl wait duration must be 0..10 seconds")
    return float(value)
