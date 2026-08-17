from __future__ import annotations

import base64
import json
import math
import re
import socket
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from .adb_executor import AdbGuiExecutor
from .android_automation import parse_gui_owl_tool_call
from .device_lease import DeviceLease, DeviceLeaseHandle, TargetBusyError
from .domain import TargetKind
from .execution import AndroidScreenshot, GuiAction
from .gui_owl_client import (
    GuiOwlClientError,
    GuiOwlTransport,
    OpenAICompatibleGuiOwlClient,
    _SYSTEM_PROMPT,
    _completion_content,
    _loopback_chat_completions_endpoint,
)
from .mobile_agent import (
    ActionDecision,
    DecisionContext,
    Observation,
    PhysicalIntent,
    PlanContext,
    PlanDraft,
    ReflectionContext,
    ReflectionDecision,
    TaskSession,
    TransportReceipt,
    Verification,
    VerificationContext,
)
from .repository import SQLiteRepository


_EVIDENCE_ID = re.compile(r"^[0-9a-f]{32}$")
_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_FRAME_DIMENSIONS = re.compile(r"\bfresh Android frame (\d+)x(\d+)\b")
_ALLOWED_KEYCODES = {
    "KEYCODE_BACK",
    "KEYCODE_HOME",
    "KEYCODE_APP_SWITCH",
    "KEYCODE_ENTER",
}


@dataclass(frozen=True, slots=True)
class _VisibleEvidenceFacts:
    facts: tuple[str, ...]
    goal_obstructed: bool


class MobileTaskAdapterError(RuntimeError):
    """Stable, sanitized production-adapter failure."""

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


@dataclass(slots=True)
class LocalMobileEvidenceStore:
    """Persist raw local frames behind opaque IDs; task state stores only references."""

    root: Path
    max_frames: int = 256
    max_total_bytes: int = 1024 * 1024 * 1024
    max_age_seconds: float = 7 * 24 * 60 * 60
    now: Callable[[], float] = field(default=time.time, repr=False)

    MAX_IMAGE_BYTES = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        if self.max_frames < 1 or self.max_total_bytes < 1 or self.max_age_seconds < 0:
            raise ValueError("evidence retention bounds are invalid")
        self.root.mkdir(parents=True, exist_ok=True)

    def record(self, task_id: str, screenshot: AndroidScreenshot) -> Observation:
        del task_id  # The opaque identifier is globally unique; no user text enters paths.
        if (
            not screenshot.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            or len(screenshot.png_bytes) > self.MAX_IMAGE_BYTES
            or screenshot.width < 1
            or screenshot.height < 1
        ):
            raise MobileTaskAdapterError(
                "mobile_evidence_invalid",
                "设备返回的画面证据无效。",
            )
        evidence_id = uuid.uuid4().hex
        image_path = self.root / f"{evidence_id}.png"
        metadata_path = self.root / f"{evidence_id}.json"
        try:
            image_path.write_bytes(screenshot.png_bytes)
            metadata_path.write_text(
                json.dumps(
                    {"width": screenshot.width, "height": screenshot.height},
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError:
            image_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            raise MobileTaskAdapterError(
                "mobile_evidence_unavailable",
                "无法保存本轮设备画面证据。",
            ) from None
        self._prune(evidence_id)
        return Observation(
            evidence_id=evidence_id,
            summary=f"fresh Android frame {screenshot.width}x{screenshot.height}",
        )

    def load(self, evidence_id: str) -> AndroidScreenshot:
        if not isinstance(evidence_id, str) or not _EVIDENCE_ID.fullmatch(evidence_id):
            raise MobileTaskAdapterError(
                "mobile_evidence_not_found",
                "找不到本轮设备画面证据。",
            )
        image_path = self.root / f"{evidence_id}.png"
        metadata_path = self.root / f"{evidence_id}.json"
        try:
            image = image_path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            width = metadata["width"]
            height = metadata["height"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise MobileTaskAdapterError(
                "mobile_evidence_not_found",
                "找不到本轮设备画面证据。",
            ) from None
        if (
            not image.startswith(b"\x89PNG\r\n\x1a\n")
            or len(image) > self.MAX_IMAGE_BYTES
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width < 1
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height < 1
        ):
            raise MobileTaskAdapterError(
                "mobile_evidence_invalid",
                "本轮设备画面证据已损坏。",
            )
        return AndroidScreenshot(image, width=width, height=height)

    def _prune(self, retained_evidence_id: str) -> None:
        """Best-effort bounded retention; cleanup never exposes evidence paths."""
        self._remove_orphans()
        now = self.now()
        pairs = self._evidence_pairs()
        for pair in pairs:
            if pair[0] == retained_evidence_id or now - pair[3] <= self.max_age_seconds:
                continue
            self._remove_pair(pair)

        pairs = self._evidence_pairs()
        self._trim_pairs(pairs, retained_evidence_id, self.max_frames, by_bytes=False)
        pairs = self._evidence_pairs()
        self._trim_pairs(
            pairs, retained_evidence_id, self.max_total_bytes, by_bytes=True
        )

    def _remove_orphans(self) -> None:
        grouped = self._evidence_files()
        for files in grouped.values():
            if set(files) == {".png", ".json"}:
                continue
            for path in files.values():
                self._safe_unlink(path)

    def _evidence_pairs(self) -> list[tuple[str, Path, Path, float, int]]:
        pairs: list[tuple[str, Path, Path, float, int]] = []
        for evidence_id, files in self._evidence_files().items():
            png_path = files.get(".png")
            metadata_path = files.get(".json")
            if png_path is None or metadata_path is None:
                continue
            try:
                png_stat = png_path.stat()
                metadata_stat = metadata_path.stat()
            except OSError:
                continue
            pairs.append(
                (
                    evidence_id,
                    png_path,
                    metadata_path,
                    max(png_stat.st_mtime, metadata_stat.st_mtime),
                    png_stat.st_size + metadata_stat.st_size,
                )
            )
        return sorted(pairs, key=lambda pair: (pair[3], pair[0]))

    def _evidence_files(self) -> dict[str, dict[str, Path]]:
        grouped: dict[str, dict[str, Path]] = {}
        try:
            paths = tuple(self.root.iterdir())
        except OSError:
            return grouped
        for path in paths:
            if path.suffix not in {".png", ".json"} or not _EVIDENCE_ID.fullmatch(path.stem):
                continue
            try:
                if path.is_symlink() or not path.is_file() or path.parent.resolve() != self.root:
                    continue
            except OSError:
                continue
            grouped.setdefault(path.stem, {})[path.suffix] = path
        return grouped

    def _trim_pairs(
        self,
        pairs: list[tuple[str, Path, Path, float, int]],
        retained_evidence_id: str,
        limit: int,
        *,
        by_bytes: bool,
    ) -> None:
        remaining_count = len(pairs)
        remaining_bytes = sum(pair[4] for pair in pairs)
        for pair in pairs:
            if pair[0] == retained_evidence_id:
                continue
            if (remaining_bytes if by_bytes else remaining_count) <= limit:
                break
            self._remove_pair(pair)
            remaining_count -= 1
            remaining_bytes -= pair[4]

    def _remove_pair(self, pair: tuple[str, Path, Path, float, int]) -> None:
        self._safe_unlink(pair[1])
        self._safe_unlink(pair[2])

    def _safe_unlink(self, path: Path) -> None:
        try:
            if path.is_symlink() or path.parent.resolve() != self.root:
                return
            path.unlink(missing_ok=True)
        except OSError:
            return


@dataclass(slots=True)
class MobileTaskAndroidDriver:
    """Open one target-bound Android task session and hold its lease until close."""

    repository: SQLiteRepository | Any
    executor: AdbGuiExecutor | Any
    evidence: LocalMobileEvidenceStore
    device_lease: DeviceLease | None = None
    waiter: Any = field(default=time.sleep, repr=False)
    settle_seconds: float = 1.0

    def open(self, task_id: str, target_id: str | None) -> TaskSession:
        selected_executor = self.executor
        serial = getattr(selected_executor, "serial", None)
        canonical_target_id: str
        if target_id is not None:
            target = self.repository.get_target(target_id)
            if target is None:
                raise MobileTaskAdapterError(
                    "mobile_task_target_not_found",
                    "找不到所选 Android 目标。",
                )
            if target.kind is not TargetKind.ANDROID:
                raise MobileTaskAdapterError(
                    "mobile_task_target_kind_mismatch",
                    "所选目标不是 Android 设备。",
                )
            if target.status != "ready":
                raise MobileTaskAdapterError(
                    "mobile_task_target_not_ready",
                    "所选 Android 目标当前未就绪。",
                )
            target_serial = (target.external_id or "").strip()
            if not target_serial:
                raise MobileTaskAdapterError(
                    "mobile_task_target_serial_changed",
                    "所选 Android 目标的连接地址已经变化。",
                )
            if not isinstance(serial, str) or serial.strip() != target_serial:
                bind = getattr(selected_executor, "for_serial", None)
                if not callable(bind):
                    raise MobileTaskAdapterError(
                        "mobile_task_target_serial_changed",
                        "所选 Android 目标的连接地址已经变化。",
                    )
                try:
                    selected_executor = bind(target_serial)
                except (TypeError, ValueError):
                    raise MobileTaskAdapterError(
                        "mobile_task_target_serial_changed",
                        "所选 Android 目标的连接地址已经变化。",
                    ) from None
            serial = target_serial
            canonical_target_id = target_id
        else:
            if not isinstance(serial, str) or not serial.strip():
                raise MobileTaskAdapterError(
                    "executor_not_configured",
                    "请选择一个当前可用的 Android 设备。",
                )
            serial = serial.strip()
            canonical_target_id = f"adb:{serial}"

        lease_handle: DeviceLeaseHandle | None = None
        if self.device_lease is not None:
            lease_handle = self.device_lease.acquire(serial)
            if lease_handle is None:
                raise TargetBusyError(serial)
        return _MobileTaskAndroidSession(
            task_id=task_id,
            target_id=canonical_target_id,
            executor=selected_executor,
            evidence=self.evidence,
            lease_handle=lease_handle,
            waiter=self.waiter,
            settle_seconds=self.settle_seconds,
        )


@dataclass(slots=True)
class _MobileTaskAndroidSession:
    task_id: str
    target_id: str
    executor: Any
    evidence: LocalMobileEvidenceStore
    lease_handle: DeviceLeaseHandle | None
    waiter: Any = field(repr=False)
    settle_seconds: float = 1.0
    _closed: bool = field(default=False, init=False, repr=False)

    def observe(self) -> Observation:
        self._require_open()
        return self.evidence.record(self.task_id, self.executor.capture_screenshot())

    def execute(self, intent: PhysicalIntent) -> TransportReceipt:
        self._require_open()
        if intent.name == "wait":
            try:
                seconds = _number_argument(
                    intent.arguments, "seconds", minimum=0, maximum=10
                )
            except MobileTaskAdapterError as exc:
                return TransportReceipt("rejected", detail=exc.code)
            self.waiter(float(seconds))
            return TransportReceipt("accepted", detail="requested wait completed")
        try:
            action = _gui_action(self.target_id, intent)
        except MobileTaskAdapterError as exc:
            return TransportReceipt("rejected", detail=exc.code)
        try:
            result = self.executor.execute(action)
        except Exception as exc:
            code = _exception_code(exc)
            if code.endswith("uncertain") or code.endswith("timeout"):
                return TransportReceipt("uncertain", detail=code)
            if code.startswith("executor_"):
                return TransportReceipt("rejected", detail=code)
            raise
        if not result.accepted:
            return TransportReceipt("rejected", detail="executor_action_rejected")
        self.waiter(self.settle_seconds)
        return TransportReceipt("accepted", detail="executor accepted one atomic input")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.lease_handle is not None:
            self.lease_handle.release()

    def _require_open(self) -> None:
        if self._closed:
            raise MobileTaskAdapterError(
                "mobile_task_session_closed",
                "本轮设备任务会话已经结束。",
            )


def _gui_action(target_id: str, intent: PhysicalIntent) -> GuiAction:
    arguments = intent.arguments
    if intent.name == "tap":
        return GuiAction(
            target_id=target_id,
            action="tap",
            x=int(_number_argument(arguments, "x", minimum=0, maximum=100_000)),
            y=int(_number_argument(arguments, "y", minimum=0, maximum=100_000)),
        )
    if intent.name == "long_press":
        return GuiAction(
            target_id=target_id,
            action="long_press",
            x=int(_number_argument(arguments, "x", minimum=0, maximum=100_000)),
            y=int(_number_argument(arguments, "y", minimum=0, maximum=100_000)),
            duration_ms=int(
                _number_argument(arguments, "duration_ms", minimum=100, maximum=5_000)
            ),
        )
    if intent.name == "swipe":
        return GuiAction(
            target_id=target_id,
            action="swipe",
            x=int(_number_argument(arguments, "x", minimum=0, maximum=100_000)),
            y=int(_number_argument(arguments, "y", minimum=0, maximum=100_000)),
            end_x=int(_number_argument(arguments, "end_x", minimum=0, maximum=100_000)),
            end_y=int(_number_argument(arguments, "end_y", minimum=0, maximum=100_000)),
            duration_ms=int(
                _number_argument(arguments, "duration_ms", minimum=100, maximum=5_000)
            ),
        )
    if intent.name == "text":
        text = arguments.get("text")
        if not isinstance(text, str) or not text or len(text) > 200:
            raise MobileTaskAdapterError(
                "mobile_intent_invalid",
                "本地模型返回的文字输入动作无效。",
            )
        return GuiAction(target_id=target_id, action="text", text=text)
    if intent.name == "keyevent":
        keycode = arguments.get("keycode")
        if not isinstance(keycode, str) or keycode not in _ALLOWED_KEYCODES:
            raise MobileTaskAdapterError(
                "mobile_intent_invalid",
                "本地模型返回的系统按键动作无效。",
            )
        return GuiAction(target_id=target_id, action="keyevent", keycode=keycode)
    raise MobileTaskAdapterError(
        "mobile_intent_invalid",
        "本地模型返回了不支持的设备动作。",
    )


def _number_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MobileTaskAdapterError(
            "mobile_intent_invalid",
            "本地模型返回的设备动作参数无效。",
        )
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise MobileTaskAdapterError(
            "mobile_intent_invalid",
            "本地模型返回的设备动作参数越界。",
        )
    return number


_PLANNER_SYSTEM = """ROLE: Planner
You are the planning role of a long-horizon Android agent. Use the current
screenshot, owner goal, current updates, and verified Skill Memory. Return only
one JSON object: {"subgoals":["...", "..."]}. Produce 1..16 observable,
ordered UI subgoals. Each subgoal must be verifiable from a later screenshot.
Use the fewest necessary, non-overlapping subgoals. A simple current-screen confirmation
or current-state check should normally be one subgoal; do not split normal HUD elements
into separate checks. Preserve multiple subgoals only when the owner goal genuinely
requires distinct UI states or transitions.
Do not output coordinates or actions in this role. Do not add meta subgoals such
as finish/end/stop the task; the final subgoal must itself describe the owner's
observable requested result."""

_EXECUTOR_SYSTEM = "ROLE: Executor\n" + _SYSTEM_PROMPT

_BEFORE_EVIDENCE_SUMMARY_SYSTEM = """ROLE: Before Evidence Summarizer
Analyze only the BEFORE screenshot for facts visibly relevant to the current
subgoal and attempted action, as refined by Owner updates. Owner updates define
the requested observable result but never substitute for visual evidence. Return only one JSON object:
{"visible_facts":["short visible fact"],"uncertain":false}.
Do not infer hidden state, compare to an AFTER screenshot, or claim success.
Set uncertain=true when the BEFORE evidence cannot be summarized reliably."""

_AFTER_EVIDENCE_SUMMARY_SYSTEM = """ROLE: After Evidence Summarizer
Analyze only the AFTER screenshot for facts visibly relevant to the current
subgoal and attempted action, as refined by Owner updates. Owner updates define
the requested observable result but never substitute for visual evidence. Return only one JSON object:
{"visible_facts":["short visible fact"],"goal_obstructed":false,"uncertain":false}.
Explicitly include any visible dialog, modal, tutorial, overlay, gate, or other
UI layer that covers or blocks the requested result in visible_facts. Set
goal_obstructed=true exactly when visible UI prevents the requested observable
result from being reached, otherwise false. Do not infer hidden state, compare
to a BEFORE screenshot, or claim success. Set uncertain=true when the AFTER
evidence cannot be summarized reliably."""

_VERIFIER_SYSTEM = """ROLE: Verifier
Compare only the supplied BEFORE and AFTER visible-facts summaries to verify
exactly the current subgoal as refined by Owner updates. Owner updates define
the requested observable result but never substitute for visual evidence. You
receive no screenshots; do not invent visual
facts beyond those summaries. Transport acceptance is not success. Return only
one JSON object:
{"satisfied":false,"progress":true,"uncertain":false,"evidence":"short visible fact"}.
Set satisfied=true only when the AFTER facts visibly prove the subgoal.
Treat a start/login/continue gateway as an intermediate screen, not proof that
the requested post-launch in-app or in-game state has been reached.
Set uncertain=true when the visual evidence cannot support a reliable result."""

_REFLECTION_SYSTEM = """ROLE: Reflection
The Android agent has made three consecutive attempts without verified
progress. Diagnose the bounded attempt summaries and latest screenshot, then
change strategy. Return only one JSON object:
{"strategy":"new strategy","terminate":false,"reason":"short reason","replacement_subgoals":null}.
replacement_subgoals may be a JSON array of observable subgoals. Never return
the unchanged strategy unless terminate=true."""


@dataclass(slots=True)
class OpenAICompatibleMobileRoleModel:
    """Use one local GUI-Owl endpoint sequentially for all four task roles."""

    endpoint: str
    model: str
    evidence: LocalMobileEvidenceStore
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    transport: GuiOwlTransport | None = field(default=None, repr=False)

    def plan(self, context: PlanContext) -> PlanDraft:
        prompt = _planner_prompt(context)
        for repair_index in range(3):
            payload = self._complete(
                _PLANNER_SYSTEM,
                _repair_prompt(prompt, repair_index, '{"subgoals":["..."]}'),
                (context.observation,),
                max_tokens=640,
            )
            try:
                decoded = _json_object(payload)
                subgoals = decoded.get("subgoals")
                if (
                    not isinstance(subgoals, list)
                    or any(
                        not isinstance(item, str)
                        or not item.strip()
                        for item in subgoals
                    )
                ):
                    raise _invalid_role_response()
                observable_subgoals = tuple(
                    item.strip()
                    for item in subgoals
                    if not _is_meta_finish_subgoal(item)
                )
                if not 1 <= len(observable_subgoals) <= 16:
                    raise _invalid_role_response()
                return PlanDraft(observable_subgoals)
            except MobileTaskAdapterError as exc:
                if exc.code != "mobile_role_invalid_response":
                    raise
        raise _invalid_role_response()

    def decide(self, context: DecisionContext) -> ActionDecision:
        screenshot = self.evidence.load(context.observation.evidence_id)
        prompt = _executor_prompt(context)
        for repair_index in range(3):
            response = self._complete(
                _EXECUTOR_SYSTEM,
                _repair_prompt(
                    prompt,
                    repair_index,
                    '<tool_call>{"name":"mobile_use","arguments":{"action":"..."}}</tool_call>',
                ),
                (context.observation,),
                max_tokens=384,
            )
            try:
                parsed = parse_gui_owl_tool_call(
                    response,
                    target_id=context.target_id or f"mobile-task:{context.task_id}",
                    screenshot=screenshot,
                )
                reason = _action_reason(response)
                if parsed.kind == "terminate":
                    return ActionDecision(
                        "finish"
                        if parsed.termination_status == "success"
                        else "terminate",
                        reason=reason,
                    )
                if parsed.kind == "interact":
                    return ActionDecision(
                        "terminate", reason="executor requested unsupported interact"
                    )
                if parsed.kind == "wait":
                    return ActionDecision(
                        "act",
                        PhysicalIntent("wait", {"seconds": parsed.wait_seconds or 0}),
                        reason,
                    )
                if parsed.action is None:
                    raise _invalid_role_response()
                return ActionDecision("act", _physical_intent(parsed.action), reason)
            except (ValueError, MobileTaskAdapterError):
                continue
        raise _invalid_role_response()

    def verify(self, context: VerificationContext) -> Verification:
        before_facts = self._summarize_before_evidence(context)
        after_evidence = self._summarize_after_evidence(context)
        before_frame = self.evidence.load(context.before.evidence_id)
        after_frame = self.evidence.load(context.after.evidence_id)
        frames_byte_identical = (
            before_frame.width == after_frame.width
            and before_frame.height == after_frame.height
            and before_frame.png_bytes == after_frame.png_bytes
        )
        prompt = _verifier_prompt(
            context,
            before_facts,
            after_evidence,
            frames_byte_identical=frames_byte_identical,
        )
        expected = (
            '{"satisfied":false,"progress":false,"uncertain":false,'
            '"evidence":"..."}'
        )
        for repair_index in range(3):
            payload = self._complete(
                _VERIFIER_SYSTEM,
                _repair_prompt(prompt, repair_index, expected),
                (),
                max_tokens=320,
            )
            try:
                decoded = _json_object(payload)
                satisfied = decoded.get("satisfied")
                progress = decoded.get("progress")
                uncertain = decoded.get("uncertain")
                evidence = decoded.get("evidence")
                if (
                    not isinstance(satisfied, bool)
                    or not isinstance(progress, bool)
                    or not isinstance(uncertain, bool)
                    or not isinstance(evidence, str)
                ):
                    raise _invalid_role_response()
                if frames_byte_identical and progress and not satisfied:
                    progress = False
                    evidence = "no material visual change from BEFORE evidence"
                if after_evidence.goal_obstructed and satisfied:
                    satisfied = False
                    evidence = "visible AFTER evidence still obstructs the requested result"
                return Verification(
                    satisfied,
                    progress,
                    uncertain=uncertain,
                    evidence=evidence.strip()[:8_000],
                )
            except (ValueError, MobileTaskAdapterError) as exc:
                if isinstance(exc, MobileTaskAdapterError) and exc.code != "mobile_role_invalid_response":
                    raise
        raise _invalid_role_response()

    def _summarize_after_evidence(
        self, context: VerificationContext
    ) -> _VisibleEvidenceFacts:
        prompt = _after_evidence_summary_prompt(context)
        expected = (
            '{"visible_facts":["..."],"goal_obstructed":false,'
            '"uncertain":false}'
        )
        for repair_index in range(3):
            payload = self._complete(
                _AFTER_EVIDENCE_SUMMARY_SYSTEM,
                _repair_prompt(prompt, repair_index, expected),
                (context.after,),
                max_tokens=256,
            )
            try:
                decoded = _json_object(payload)
                facts = decoded.get("visible_facts")
                goal_obstructed = decoded.get("goal_obstructed")
                uncertain = decoded.get("uncertain")
                if uncertain is True:
                    raise MobileTaskAdapterError(
                        "mobile_role_uncertain",
                        "后置画面摘要不确定，无法安全验证。",
                    )
                if (
                    not isinstance(uncertain, bool)
                    or not isinstance(goal_obstructed, bool)
                    or not isinstance(facts, list)
                    or len(facts) > 16
                    or any(
                        not isinstance(fact, str)
                        or not fact.strip()
                        or len(fact.strip()) > 500
                        for fact in facts
                    )
                ):
                    raise _invalid_role_response()
                return _VisibleEvidenceFacts(
                    tuple(fact.strip() for fact in facts), goal_obstructed
                )
            except ValueError:
                continue
            except MobileTaskAdapterError as exc:
                if exc.code == "mobile_role_invalid_response":
                    continue
                raise
        raise _invalid_role_response()

    def _summarize_before_evidence(
        self, context: VerificationContext
    ) -> tuple[str, ...]:
        prompt = _before_evidence_summary_prompt(context)
        expected = '{"visible_facts":["..."],"uncertain":false}'
        for repair_index in range(3):
            payload = self._complete(
                _BEFORE_EVIDENCE_SUMMARY_SYSTEM,
                _repair_prompt(prompt, repair_index, expected),
                (context.before,),
                max_tokens=256,
            )
            try:
                decoded = _json_object(payload)
                facts = decoded.get("visible_facts")
                uncertain = decoded.get("uncertain")
                if uncertain is True:
                    raise MobileTaskAdapterError(
                        "mobile_role_uncertain",
                        "前置画面摘要不确定，无法安全验证。",
                    )
                if (
                    not isinstance(uncertain, bool)
                    or not isinstance(facts, list)
                    or len(facts) > 16
                    or any(
                        not isinstance(fact, str)
                        or not fact.strip()
                        or len(fact.strip()) > 500
                        for fact in facts
                    )
                ):
                    raise _invalid_role_response()
                return tuple(fact.strip() for fact in facts)
            except ValueError:
                continue
            except MobileTaskAdapterError as exc:
                if exc.code == "mobile_role_invalid_response":
                    continue
                raise
        raise _invalid_role_response()

    def reflect(self, context: ReflectionContext) -> ReflectionDecision:
        observation = _latest_attempt_observation(context)
        prompt = _reflection_prompt(context)
        expected = (
            '{"strategy":"...","terminate":false,"reason":"...",'
            '"replacement_subgoals":null}'
        )
        for repair_index in range(3):
            payload = self._complete(
                _REFLECTION_SYSTEM,
                _repair_prompt(prompt, repair_index, expected),
                (observation,),
                max_tokens=512,
            )
            try:
                decoded = _json_object(payload)
                strategy = decoded.get("strategy")
                terminate = decoded.get("terminate")
                reason = decoded.get("reason")
                replacement = decoded.get("replacement_subgoals")
                if (
                    not isinstance(strategy, str)
                    or not strategy.strip()
                    or not isinstance(terminate, bool)
                    or not isinstance(reason, str)
                ):
                    raise _invalid_role_response()
                replacement_tuple: tuple[str, ...] | None = None
                if replacement is not None:
                    if (
                        not isinstance(replacement, list)
                        or not replacement
                        or any(
                            not isinstance(item, str) or not item.strip()
                            for item in replacement
                        )
                    ):
                        raise _invalid_role_response()
                    replacement_tuple = tuple(item.strip() for item in replacement)
                return ReflectionDecision(
                    strategy.strip(),
                    terminate=terminate,
                    reason=reason.strip(),
                    replacement_subgoals=replacement_tuple,
                )
            except (ValueError, MobileTaskAdapterError) as exc:
                if isinstance(exc, MobileTaskAdapterError) and exc.code != "mobile_role_invalid_response":
                    raise
        raise _invalid_role_response()

    def _complete(
        self,
        system_prompt: str,
        user_prompt: str,
        observations: tuple[Observation, ...],
        *,
        max_tokens: int,
    ) -> str:
        try:
            endpoint = _loopback_chat_completions_endpoint(self.endpoint)
            if not self.model.strip():
                raise GuiOwlClientError(
                    "gui_model_not_configured", "本地 GUI 模型名称未配置。"
                )
            content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
            for observation in observations:
                screenshot = self.evidence.load(observation.evidence_id)
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(screenshot.png_bytes).decode("ascii")
                        },
                    }
                )
            request_payload: dict[str, Any] = {
                "model": self.model.strip(),
                "messages": [
                    {
                        "role": "system",
                        "content": [{"type": "text", "text": system_prompt}],
                    },
                    {"role": "user", "content": content},
                ],
                "stream": False,
                "temperature": 0.0,
                "max_tokens": max_tokens,
            }
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            decoded = (
                self.transport(endpoint, request_payload, headers, self.timeout_seconds)
                if self.transport is not None
                else OpenAICompatibleGuiOwlClient._request(
                    endpoint, request_payload, headers, self.timeout_seconds
                )
            )
            return _completion_content(decoded)
        except MobileTaskAdapterError:
            raise
        except GuiOwlClientError as exc:
            raise MobileTaskAdapterError(exc.code, str(exc)) from None
        except HTTPError as exc:
            raise MobileTaskAdapterError(
                "gui_model_http_error",
                f"本地 GUI 模型返回 HTTP {exc.code}。",
            ) from None
        except (URLError, socket.timeout, TimeoutError, OSError):
            raise MobileTaskAdapterError(
                "gui_model_unavailable",
                "本地 GUI 模型暂时不可用。",
            ) from None
        except Exception:
            raise MobileTaskAdapterError(
                "gui_model_request_failed",
                "本地 GUI 模型请求失败。",
            ) from None


def _physical_intent(action: GuiAction) -> PhysicalIntent:
    if action.action == "tap":
        return PhysicalIntent("tap", {"x": action.x, "y": action.y})
    if action.action == "long_press":
        return PhysicalIntent(
            "long_press",
            {"x": action.x, "y": action.y, "duration_ms": action.duration_ms},
        )
    if action.action == "swipe":
        return PhysicalIntent(
            "swipe",
            {
                "x": action.x,
                "y": action.y,
                "end_x": action.end_x,
                "end_y": action.end_y,
                "duration_ms": action.duration_ms,
            },
        )
    if action.action == "text":
        return PhysicalIntent("text", {"text": action.text})
    if action.action == "keyevent":
        return PhysicalIntent("keyevent", {"keycode": action.keycode})
    raise _invalid_role_response()


def _planner_prompt(context: PlanContext) -> str:
    return (
        f"Owner goal: {context.goal}\n"
        f"Owner updates: {_owner_updates(context.owner_inputs)}\n"
        f"Current observation: {context.observation.summary}\n"
        f"Verified Skill Memory: {_skill_memory(context.skill_memory)}"
    )


def _is_meta_finish_subgoal(description: str) -> bool:
    normalized = re.sub(r"[\s。.!！?？]+", "", description).casefold()
    return normalized in {
        "结束任务",
        "完成任务",
        "停止任务",
        "终止任务",
        "finishtask",
        "endtask",
        "stoptask",
    }


def _executor_prompt(context: DecisionContext) -> str:
    recent = []
    for attempt in context.recent_attempts[-8:]:
        intent = attempt.decision.intent
        action = _action_fingerprint(intent, attempt.before)
        transport = attempt.transport.status if attempt.transport is not None else "not_sent"
        verification = attempt.verification
        result = (
            "satisfied"
            if verification is not None and verification.satisfied
            else "progress"
            if verification is not None and verification.progress
            else "no_progress"
        )
        recent.append(f"attempt {attempt.sequence}: {action}; {transport}; {result}")
    history = "\n".join(recent) if recent else "None"
    return (
        "Generate exactly one atomic next move from the current screenshot.\n\n"
        f"Overall goal: {context.goal}\n"
        f"Current subgoal: {context.subgoal.description}\n"
        f"Current strategy: {context.strategy}\n"
        f"Consecutive no-progress attempts: {context.consecutive_no_progress}\n"
        f"Owner updates: {_owner_updates(context.owner_inputs)}\n"
        f"Verified Skill Memory: {_skill_memory(context.skill_memory)}\n\n"
        f"Recent attempts (text only):\n{history}"
        "\nDo not blindly repeat a recent non-idempotent action fingerprint "
        "unless the current screenshot provides new visible justification."
    )


def _action_fingerprint(intent: PhysicalIntent | None, observation: Observation) -> str:
    if intent is None:
        return "no_physical_intent"
    if intent.name in {"tap", "long_press"}:
        return f"{intent.name}@{_screen_region(intent.arguments, observation)}"
    if intent.name == "swipe":
        return f"swipe:{_swipe_direction(intent.arguments)}"
    if intent.name == "text":
        return "text(redacted)"
    if intent.name == "keyevent":
        keycode = intent.arguments.get("keycode")
        return f"keyevent:{keycode}" if keycode in _ALLOWED_KEYCODES else "keyevent"
    if intent.name == "wait":
        return "wait"
    return intent.name


def _screen_region(arguments: Mapping[str, Any], observation: Observation) -> str:
    x = _finite_number(arguments.get("x"))
    y = _finite_number(arguments.get("y"))
    dimensions = _FRAME_DIMENSIONS.search(observation.summary)
    if x is None or y is None or dimensions is None:
        return "unknown-region"
    width = int(dimensions.group(1))
    height = int(dimensions.group(2))
    if width < 1 or height < 1:
        return "unknown-region"
    column = min(3, max(0, int(x * 4 / width)))
    row = min(3, max(0, int(y * 4 / height)))
    return f"r{row}c{column}"


def _swipe_direction(arguments: Mapping[str, Any]) -> str:
    start_x = _finite_number(arguments.get("x"))
    start_y = _finite_number(arguments.get("y"))
    end_x = _finite_number(arguments.get("end_x"))
    end_y = _finite_number(arguments.get("end_y"))
    if None in {start_x, start_y, end_x, end_y}:
        return "unknown"
    delta_x = end_x - start_x  # type: ignore[operator]
    delta_y = end_y - start_y  # type: ignore[operator]
    if abs(delta_x) >= abs(delta_y):
        return "right" if delta_x > 0 else "left" if delta_x < 0 else "stationary"
    return "down" if delta_y > 0 else "up" if delta_y < 0 else "stationary"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _before_evidence_summary_prompt(context: VerificationContext) -> str:
    intent = context.decision.intent
    action = intent.name if intent is not None else context.decision.kind
    return (
        f"Overall goal: {context.goal}\n"
        f"Current subgoal: {context.subgoal.description}\n"
        f"Attempted action type: {action}\n"
        f"Owner updates: {_owner_updates(context.owner_inputs)}\n"
        "Summarize only visible BEFORE facts relevant to a later verification."
    )


def _after_evidence_summary_prompt(context: VerificationContext) -> str:
    intent = context.decision.intent
    action = intent.name if intent is not None else context.decision.kind
    return (
        f"Overall goal: {context.goal}\n"
        f"Current subgoal: {context.subgoal.description}\n"
        f"Attempted action type: {action}\n"
        f"Owner updates: {_owner_updates(context.owner_inputs)}\n"
        "Summarize only visible AFTER facts relevant to verification, including "
        "any dialog, modal, tutorial, overlay, or gate that obstructs the "
        "requested observable result."
    )


def _verifier_prompt(
    context: VerificationContext,
    before_facts: tuple[str, ...],
    after_evidence: _VisibleEvidenceFacts,
    *,
    frames_byte_identical: bool,
) -> str:
    intent = context.decision.intent
    action = intent.name if intent is not None else context.decision.kind
    summary = json.dumps(list(before_facts), ensure_ascii=False, separators=(",", ":"))
    after_summary = json.dumps(
        list(after_evidence.facts), ensure_ascii=False, separators=(",", ":")
    )
    return (
        f"Overall goal: {context.goal}\n"
        f"Current subgoal: {context.subgoal.description}\n"
        f"Attempted action type: {action}\n"
        f"Owner updates: {_owner_updates(context.owner_inputs)}\n"
        f"Transport status: {context.transport.status}\n"
        f"BEFORE visible facts summary: {summary}\n"
        f"AFTER visible facts summary: {after_summary}\n"
        f"AFTER goal obstructed: {str(after_evidence.goal_obstructed).lower()}\n"
        f"Exact BEFORE/AFTER frame match: {str(frames_byte_identical).lower()}\n"
        "Judge only the supplied visible-facts summaries; no screenshot is available. "
        "Set progress=true only for a material visible change beyond the BEFORE facts. "
        "If the AFTER repeats only BEFORE facts, set progress=false. "
        "A start/login/continue gateway is intermediate and does not prove that "
        "a requested post-launch in-app or in-game state has been reached."
    )


def _reflection_prompt(context: ReflectionContext) -> str:
    summaries = []
    for attempt in context.recent_attempts[-3:]:
        intent = attempt.decision.intent
        action = intent.name if intent is not None else attempt.decision.kind
        evidence = attempt.verification.evidence if attempt.verification is not None else ""
        summaries.append(f"attempt {attempt.sequence}: {action}; {evidence[:240]}")
    return (
        f"Overall goal: {context.goal}\n"
        f"Current subgoal: {context.subgoal.description}\n"
        f"Previous strategy: {context.strategy}\n"
        f"Consecutive no progress: {context.consecutive_no_progress}\n"
        f"Owner updates: {_owner_updates(context.owner_inputs)}\n"
        f"Verified Skill Memory: {_skill_memory(context.skill_memory)}\n"
        "Recent attempts:\n" + "\n".join(summaries)
    )


def _owner_updates(inputs: tuple[Any, ...]) -> str:
    values = [item.content for item in inputs[-8:]]
    return " | ".join(values) if values else "None"


def _repair_prompt(base: str, repair_index: int, expected_shape: str) -> str:
    if repair_index == 0:
        return base
    return (
        f"{base}\n\n"
        "Your previous response did not match the required machine-readable format. "
        "Do not explain or use Markdown. Return exactly one result shaped like: "
        f"{expected_shape}"
    )


def _skill_memory(memory: Any) -> str:
    if memory is None:
        return "None"
    procedure = " -> ".join(memory.procedure)
    return f"v{memory.version}; procedure={procedure}; strategy={memory.strategy}"


def _latest_attempt_observation(context: ReflectionContext) -> Observation:
    if not context.recent_attempts:
        raise MobileTaskAdapterError(
            "mobile_reflection_context_invalid",
            "反思角色缺少最近画面证据。",
        )
    latest = context.recent_attempts[-1]
    return latest.after or latest.before


def _json_object(content: str) -> dict[str, Any]:
    candidate = content.strip()
    fenced = _JSON_FENCE.fullmatch(candidate)
    if fenced is not None:
        candidate = fenced.group(1).strip()
    elif not candidate.startswith("{") or not candidate.endswith("}"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise _invalid_role_response()
        candidate = candidate[start : end + 1]
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError:
        raise _invalid_role_response() from None
    if not isinstance(decoded, dict):
        raise _invalid_role_response()
    return decoded


def _action_reason(response: str) -> str:
    prefix = response.split("<tool_call>", 1)[0].strip()
    if prefix.lower().startswith("action:"):
        prefix = prefix.split(":", 1)[1].strip()
    return prefix[:500]


def _exception_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code[:100]
    if exc.args and isinstance(exc.args[0], str):
        candidate = exc.args[0]
        if candidate.startswith(("executor_", "target_busy")):
            return candidate[:100]
    return "mobile_transport_failed"


def _invalid_role_response() -> MobileTaskAdapterError:
    return MobileTaskAdapterError(
        "mobile_role_invalid_response",
        "本地 GUI 模型返回的角色结果格式无效。",
    )
