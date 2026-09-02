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
from .goal_families import (
    STZB_DAILY_GOAL_FAMILY,
    is_stzb_discovery_only_goal,
    normalize_goal_family,
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
from .visual_similarity import png_perceptual_distance, png_sampled_change_ratio


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
    if intent.name == "open_app":
        package = arguments.get("package")
        component = arguments.get("component")
        if (
            not isinstance(package, str)
            or not package.strip()
            or (component is not None and (not isinstance(component, str) or not component.strip()))
        ):
            raise MobileTaskAdapterError(
                "mobile_intent_invalid",
                "本地模型返回的应用启动动作无效。",
            )
        return GuiAction(
            target_id=target_id,
            action="open_app",
            package=package.strip(),
            component=component.strip() if isinstance(component, str) else None,
        )
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


@dataclass(slots=True)
class OpenAICompatibleToolRoleModel:
    """Structured commander and visual roles over one local tool-call endpoint."""

    endpoint: str
    model: str
    evidence: LocalMobileEvidenceStore
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 60.0
    transport: GuiOwlTransport | None = field(default=None, repr=False)

    def plan(self, context: PlanContext) -> PlanDraft:
        base_prompt = _planner_prompt(context)
        retry_feedback = ""
        previous_rejected: tuple[str, ...] | None = None
        for semantic_attempt in range(4):
            try:
                decoded = self.call_tool(
                    system=(
                        "你是手机任务指挥层。必须调用 record_plan。规划1到24个可观察阶段，"
                        "完整覆盖原始目标；不输出坐标、点击或设备命令。只规划需要在手机上达到的"
                        "可见状态，不要把向用户汇报或结束任务单列为手机阶段。每个阶段必须能由"
                        "一张当前截图独立验证；不同页签、不同列表边界或不同详情页面必须拆成"
                        "不同阶段，禁止把依次查看多个页面合并为一个阶段。这是线性计划："
                        "每个阶段必须是无条件的单一可观察结果，禁止写若、如果、否则、如无等分支。"
                    ),
                    prompt=base_prompt + retry_feedback,
                    observations=(context.observation,),
                    tool_name="record_plan",
                    description="Record an observable plan that preserves the full owner goal",
                    parameters={
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "subgoals": {
                                "type": "array", "minItems": 1, "maxItems": 24,
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["subgoals"],
                    },
                    max_tokens=1_024,
                    reasoning_effort="low",
                    reasoning_budget=0,
                )
            except MobileTaskAdapterError as error:
                if (
                    error.code == "mobile_role_invalid_response"
                    and normalize_goal_family(context.goal) == STZB_DAILY_GOAL_FAMILY
                ):
                    return PlanDraft(_stzb_daily_plan_scaffold(context.goal))
                raise
            subgoals = decoded.get("subgoals")
            if not isinstance(subgoals, list):
                retry_feedback = _plan_retry_feedback(("invalid_structure",))
                continue
            cleaned = tuple(
                item.strip() for item in subgoals
                if isinstance(item, str) and item.strip() and not _is_meta_finish_subgoal(item)
            )
            if len(cleaned) != len(subgoals) or not 1 <= len(cleaned) <= 24:
                retry_feedback = _plan_retry_feedback(("invalid_structure",))
                continue
            issues = _stzb_daily_plan_issues(context.goal, cleaned)
            if not issues:
                return PlanDraft(cleaned)
            if previous_rejected == cleaned:
                return PlanDraft(_stzb_daily_plan_scaffold(context.goal))
            previous_rejected = cleaned
            retry_feedback = _plan_retry_feedback(issues, cleaned)
        if normalize_goal_family(context.goal) == STZB_DAILY_GOAL_FAMILY:
            return PlanDraft(_stzb_daily_plan_scaffold(context.goal))
        raise _invalid_role_response()

    def decide(self, context: DecisionContext) -> ActionDecision:
        screenshot = self.evidence.load(context.observation.evidence_id)
        decoded = self.call_tool(
            system=(
                "你是手机视觉动作层。必须调用 mobile_use，并且只根据当前截图和当前子目标"
                "给一个动作。若画面已证明子目标完成，terminate success。系统级返回桌面、"
                "返回或最近任务应使用 system_button，不要点击导航栏坐标。对可见目标动作"
                    "填写简短 target_description，描述目标文字或语义，不得填写坐标。点击目标"
                    "应落在当前截图可见的可交互文字或控件主体内，避开装饰边缘；若同一区域"
                    "刚刚无进展，必须依据当前截图重新定位到 visibly different 的可交互子区域。"
                    "若领取后出现全屏‘获得新战法’或类似连续奖励展示，名称/图标随点击变化"
                    "就是结算进展：每页点击一次继续，直到返回原列表；期间不要使用系统返回。"
            ),
            prompt=_executor_prompt(context),
            observations=(context.observation,),
            tool_name="mobile_use",
            description="Propose exactly one validated atomic Android action",
            parameters=_MOBILE_USE_TOOL_PARAMETERS,
            max_tokens=768,
            reasoning_effort="low",
            reasoning_budget=0,
        )
        # Qwen occasionally names the Android atomic gesture ``tap`` even
        # though the shared mobile_use schema inherited GUI-Owl's ``click``
        # spelling. They are exact semantic aliases here; normalize only this
        # one token before the ordinary coordinate/bounds parser runs.
        if decoded.get("action") == "tap":
            decoded = {**decoded, "action": "click"}
        if decoded.get("action") == "open_app":
            package = decoded.get("package")
            component = decoded.get("component")
            if (
                not isinstance(package, str)
                or not package.strip()
                or (component is not None and (not isinstance(component, str) or not component.strip()))
            ):
                raise _invalid_role_response()
            arguments: dict[str, object] = {"package": package.strip()}
            if component is not None:
                arguments["component"] = component.strip()
            return ActionDecision("act", PhysicalIntent("open_app", arguments), "structured app launch")
        envelope = (
            '<tool_call>{"name":"mobile_use","arguments":'
            + json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
            + "}</tool_call>"
        )
        try:
            parsed = parse_gui_owl_tool_call(
                envelope,
                target_id=context.target_id or f"mobile-task:{context.task_id}",
                screenshot=screenshot,
            )
        except ValueError:
            raise _invalid_role_response() from None
        if parsed.kind == "terminate":
            return ActionDecision(
                "finish" if parsed.termination_status == "success" else "terminate",
                reason="structured visual terminal decision",
            )
        if parsed.kind == "wait":
            return ActionDecision(
                "act", PhysicalIntent("wait", {"seconds": parsed.wait_seconds or 0}),
                "structured visual wait",
            )
        if parsed.action is None:
            raise _invalid_role_response()
        target_description = decoded.get("target_description")
        if target_description is not None and (
            not isinstance(target_description, str)
            or not target_description.strip()
            or len(target_description.strip()) > 200
        ):
            raise _invalid_role_response()
        return ActionDecision(
            "act", _physical_intent(
                parsed.action,
                target_description=(target_description.strip() if target_description else None),
            ), "structured visual action"
        )

    def verify(self, context: VerificationContext) -> Verification:
        decoded = self.call_tool(
            system=(
                "你是动作后验证层。必须调用 record_verification。只提供一张动作后画面。"
                "传输成功不等于结果成功；verdict四选一，不得输出互相矛盾的状态。"
                "只有画面不可读、严重遮挡或无法可靠判断时才选择 uncertain。"
                "若画面清晰可读但手势后没有可见变化，应选 no_progress，不得仅因为边界或"
                "子目标尚未被证明就选 uncertain。"
            ),
            prompt=(
                f"整体目标：{context.goal}\n当前子目标：{context.subgoal.description}\n"
                f"动作：{context.decision.intent.name if context.decision.intent else context.decision.kind}\n"
                "根据当前动作后画面选择 satisfied、progress、no_progress 或 uncertain。"
                f"{_stzb_daily_verification_instruction(context.goal, context.subgoal.description)}"
            ),
            observations=(context.after,),
            tool_name="record_verification",
            description="Record an evidence-bounded verdict over before and after frames",
            parameters={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "verdict": {"type": "string", "enum": [
                        "satisfied", "progress", "no_progress", "uncertain",
                    ]},
                    "evidence": {"type": "string"},
                },
                "required": ["verdict", "evidence"],
            },
            max_tokens=768,
            reasoning_effort="low",
            reasoning_budget=0,
        )
        verdict = decoded.get("verdict")
        evidence = decoded.get("evidence")
        if verdict not in {"satisfied", "progress", "no_progress", "uncertain"} or not isinstance(evidence, str):
            raise _invalid_role_response()
        verdict, evidence = _stzb_daily_verdict_guard(
            context.goal, context.subgoal.description, verdict, evidence
        )
        satisfied = verdict == "satisfied"
        progress = verdict in {"satisfied", "progress"}
        uncertain = verdict == "uncertain"
        before = self.evidence.load(context.before.evidence_id)
        after = self.evidence.load(context.after.evidence_id)
        perceptual_distance = (
            png_perceptual_distance(before.png_bytes, after.png_bytes)
            if before.width == after.width and before.height == after.height
            else None
        )
        sampled_change_ratio = (
            png_sampled_change_ratio(before.png_bytes, after.png_bytes)
            if before.width == after.width and before.height == after.height
            else None
        )
        materially_unchanged = (
            before.width == after.width
            and before.height == after.height
            and (
                before.png_bytes == after.png_bytes
                or perceptual_distance is not None
                and perceptual_distance <= 8
                and sampled_change_ratio is not None
                and sampled_change_ratio <= 0.02
            )
        )
        activity_boundary = _stzb_activity_boundary_kind(
            context.goal, context.subgoal.description
        )
        boundary_probe_invalid = False
        if activity_boundary is not None:
            intent = context.decision.intent
            start_x = intent.arguments.get("x") if intent is not None else None
            end_x = intent.arguments.get("end_x") if intent is not None else None
            correct_probe = (
                intent is not None
                and intent.name == "swipe"
                and isinstance(start_x, (int, float))
                and isinstance(end_x, (int, float))
                and (
                    end_x > start_x
                    if activity_boundary == "left"
                    else end_x < start_x
                )
            )
            if not correct_probe:
                satisfied = False
                progress = False
                uncertain = False
                boundary_probe_invalid = True
                evidence = (
                    f"{activity_boundary} activity boundary lacks the required "
                    "directed terminal swipe probe [STZB activity-boundary guard]"
                )
            elif not materially_unchanged:
                # A correctly directed swipe that still changes the frame is
                # traversal progress, not proof that the terminal boundary has
                # been reached. This objective transition outranks a model's
                # mistaken expectation that one old card title must define the
                # boundary. Only an unchanged same-direction probe may close
                # this stage.
                satisfied = False
                progress = True
                uncertain = False
                evidence = (
                    f"directed swipe moved toward the {activity_boundary} activity "
                    "boundary; another terminal probe is required "
                    "[STZB activity-boundary guard]"
                )
        if materially_unchanged:
            middle_viewport_requires_transition = (
                normalize_goal_family(context.goal) == STZB_DAILY_GOAL_FAMILY
                and re.search(
                    r"(?:活动|轮播|卡片)", context.subgoal.description
                ) is not None
                and re.search(
                    r"(?:中间视口|中间画面|中部视口|有重叠|一个可见卡片组)",
                    context.subgoal.description,
                ) is not None
            )
            if satisfied and middle_viewport_requires_transition:
                # The plan contract puts this stage after the left boundary.
                # Therefore an unchanged frame cannot prove that a distinct
                # middle viewport was reached, even if the model describes the
                # same visible cards as an overlapping group.
                satisfied = False
                progress = False
                uncertain = False
                evidence = (
                    "no material visual transition from the prior activity "
                    "boundary [STZB activity-middle guard]"
                )
            else:
                progress = satisfied is True
            if not satisfied:
                if (
                    not middle_viewport_requires_transition
                    and not boundary_probe_invalid
                ):
                    evidence = "no material visual change from BEFORE evidence"
                # A readable, unchanged result after a swipe is a definitive
                # boundary/no-progress observation. Unlike a tap, the gesture
                # has no plausible hidden one-shot side effect that would make
                # a later strategy unsafe; keep it reflectable instead of
                # terminating the task as uncertain.
                if (
                    context.decision.intent is not None
                    and context.decision.intent.name == "swipe"
                ):
                    uncertain = False
        elif uncertain and not re.search(
            r"(?:画面|截图|图像).{0,24}(?:不可读|无法看清|看不清|模糊|"
            r"严重遮挡|缺失|损坏|异常)|(?:黑屏|花屏|截图失败)",
            evidence,
            re.IGNORECASE,
        ):
            # A materially changed and readable AFTER frame makes the physical
            # outcome observable even when it still does not prove the semantic
            # subgoal. Keep that case reflectable as no-progress. True visual
            # ambiguity remains terminal, as does an unchanged tap above.
            uncertain = False
            progress = False
            evidence = (
                evidence.strip()[:7_520]
                + " [clear changed AFTER frame: semantic uncertainty is no-progress]"
            )
        if progress and not satisfied:
            for attempt in context.recent_attempts:
                if (
                    attempt.plan_revision != context.plan_revision
                    or attempt.subgoal_index != context.subgoal.index
                    or attempt.after is None
                ):
                    continue
                try:
                    prior = self.evidence.load(attempt.after.evidence_id)
                except (MobileTaskAdapterError, OSError):
                    continue
                if after.width != prior.width or after.height != prior.height:
                    continue
                repeated_distance = png_perceptual_distance(
                    prior.png_bytes, after.png_bytes,
                )
                if prior.png_bytes == after.png_bytes or repeated_distance <= 8:
                    progress = False
                    evidence = (
                        evidence.strip()[:7_520]
                        + " [scene-loop guard: this unsatisfied subgoal revisited "
                        "a materially unchanged prior scene]"
                    )
                    break
        if context.decision.kind == "finish" and not satisfied:
            # A finish decision sends no device action. Animation or other
            # incidental frame drift cannot create progress when the current
            # screenshot still does not prove the subgoal; count it as
            # no-progress so bounded reflection can repair the plan granularity.
            progress = False
        return Verification(
            bool(satisfied), bool(progress), uncertain=bool(uncertain),
            evidence=evidence.strip()[:8_000],
        )

    def reflect(self, context: ReflectionContext) -> ReflectionDecision:
        observation = _latest_attempt_observation(context)
        base_prompt = _reflection_prompt(context)
        semantic_issues: tuple[str, ...] = ()
        for semantic_attempt in range(4):
            try:
                decoded = self.call_tool(
                    system=(
                        "你是手机任务反思层。必须调用 record_reflection。根据连续无进展记录改变策略，"
                        "不得原样重复失败动作。replacement_subgoals 只能写无条件、可从新画面验证的"
                        "完整结果句；禁止坐标、固定点击脚本、‘若/如果/否则’条件步骤。只插入恢复目标，"
                        "不要重写或删除原计划尚未完成的后续目标，运行时会自动接回它们。"
                    ),
                    prompt=base_prompt + (
                        _reflection_retry_feedback(
                            semantic_issues, context.subgoal.description,
                        )
                        if semantic_attempt else ""
                    ),
                    observations=(observation,),
                    tool_name="record_reflection",
                    description="Record one bounded recovery strategy",
                    parameters={
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "strategy": {"type": "string"},
                            "terminate": {"type": "boolean"},
                            "reason": {"type": "string"},
                            "replacement_subgoals": {
                                "type": ["array", "null"], "items": {"type": "string"},
                            },
                        },
                        "required": ["strategy", "terminate", "reason", "replacement_subgoals"],
                    },
                    max_tokens=512,
                    reasoning_effort="low",
                    reasoning_budget=0,
                )
            except MobileTaskAdapterError as error:
                if error.code != "mobile_role_invalid_response":
                    raise
                break
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
                semantic_issues = ("invalid_structure",)
                continue
            replacement_tuple = None
            if replacement is not None:
                if (
                    not isinstance(replacement, list)
                    or not replacement
                    or any(
                        not isinstance(item, str)
                        or not item.strip()
                        for item in replacement
                    )
                ):
                    semantic_issues = ("invalid_structure",)
                    continue
                replacement_tuple = tuple(item.strip() for item in replacement)
                semantic_issues = _stzb_daily_recovery_issues(
                    context.goal,
                    context.subgoal.description,
                    replacement_tuple,
                )
                if semantic_issues:
                    continue
            return ReflectionDecision(
                strategy.strip(), terminate=terminate, reason=reason.strip(),
                replacement_subgoals=replacement_tuple,
            )
        fallback = _stzb_daily_reflection_fallback(context)
        if fallback is not None:
            return fallback
        raise _invalid_role_response()

    def call_tool(
        self, *, system: str, prompt: str, observations: tuple[Observation, ...],
        tool_name: str, description: str, parameters: dict[str, Any], max_tokens: int,
        reasoning_effort: str = "medium",
        reasoning_budget: int | None = None,
    ) -> dict[str, Any]:
        if not self.model.strip():
            raise MobileTaskAdapterError("mobile_role_not_configured", "本地角色模型名称未配置。")
        no_think = "\n/no_think" if "qwen" in self.model.casefold() else ""
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt + no_think}
        ]
        for observation in observations:
            screenshot = self.evidence.load(observation.evidence_id)
            content.append({
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(screenshot.png_bytes).decode("ascii")},
            })
        if no_think and observations:
            # Qwen's multimodal template evaluates the directive at the end of
            # the user turn. Keeping it only in the leading text block leaves
            # an image after it and may re-enable a long hidden reasoning pass.
            content.append({"type": "text", "text": "/no_think"})
        payload = {
            "model": self.model.strip(), "temperature": 0.0,
            "reasoning_effort": reasoning_effort,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system + no_think},
                {"role": "user", "content": content},
            ],
            "tools": [{"type": "function", "function": {
                "name": tool_name, "description": description, "parameters": parameters,
            }}],
            # Exactly one tool is supplied. The standard string form forces a
            # tool call and is accepted by both OpenAI-compatible servers and
            # the inspected llama.cpp build, which rejects the object form.
            "tool_choice": "required",
        }
        if reasoning_budget is not None:
            payload["reasoning_budget"] = reasoning_budget
            if reasoning_budget == 0:
                payload["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        endpoint = _loopback_chat_completions_endpoint(self.endpoint)
        for repair_index in range(3):
            if repair_index:
                content[0]["text"] = (
                    prompt
                    + "\n\nYour previous response was not one valid forced tool call. "
                    + f"Call {tool_name} exactly once with arguments matching its schema. "
                    + "Do not explain, use Markdown, or return plain text."
                    + no_think
                )
            try:
                response = (
                    self.transport(endpoint, payload, headers, self.timeout_seconds)
                    if self.transport is not None else
                    OpenAICompatibleGuiOwlClient._request(
                        endpoint, payload, headers, self.timeout_seconds
                    )
                )
                choices = response.get("choices")
                if not isinstance(choices, list) or len(choices) != 1:
                    continue
                message = (
                    choices[0].get("message")
                    if isinstance(choices[0], Mapping) else None
                )
                calls = message.get("tool_calls") if isinstance(message, Mapping) else None
                if (
                    not isinstance(calls, list) or len(calls) != 1
                    or not isinstance(calls[0], Mapping)
                ):
                    continue
                function = calls[0].get("function")
                if not isinstance(function, Mapping) or function.get("name") != tool_name:
                    continue
                arguments = function.get("arguments")
                decoded = json.loads(arguments) if isinstance(arguments, str) else arguments
                if isinstance(decoded, dict):
                    return decoded
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            except MobileTaskAdapterError:
                raise
            except GuiOwlClientError as error:
                raise MobileTaskAdapterError(error.code, str(error)) from None
            except (HTTPError, URLError, socket.timeout, TimeoutError, OSError):
                raise MobileTaskAdapterError(
                    "mobile_role_unavailable", "本地角色模型暂时不可用。"
                ) from None
        raise _invalid_role_response()


_MOBILE_USE_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": [
            "click", "long_press", "swipe", "type", "system_button", "open_app", "wait", "terminate",
        ]},
        "coordinate": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
        "coordinate2": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
        "text": {"type": "string"},
        "button": {"type": "string", "enum": ["Back", "Home", "Menu", "Enter"]},
        "package": {"type": "string", "minLength": 1, "maxLength": 255},
        "component": {"type": "string", "minLength": 1, "maxLength": 512},
        "time": {"type": "number"},
        "status": {"type": "string", "enum": ["success", "failure"]},
        "target_description": {"type": "string", "maxLength": 200},
    },
    "required": ["action"],
}


def _physical_intent(
    action: GuiAction, *, target_description: str | None = None
) -> PhysicalIntent:
    target = ({"target_description": target_description} if target_description else {})
    if action.action == "tap":
        return PhysicalIntent("tap", {"x": action.x, "y": action.y, **target})
    if action.action == "long_press":
        return PhysicalIntent(
            "long_press",
            {"x": action.x, "y": action.y, "duration_ms": action.duration_ms, **target},
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
                **target,
            },
        )
    if action.action == "text":
        return PhysicalIntent("text", {"text": action.text})
    if action.action == "keyevent":
        return PhysicalIntent("keyevent", {"keycode": action.keycode})
    raise _invalid_role_response()


def _planner_prompt(context: PlanContext) -> str:
    family_instruction = ""
    if normalize_goal_family(context.goal) == STZB_DAILY_GOAL_FAMILY:
        execution_instruction = (
            "This owner goal is discovery_only: navigate and inspect, but do not execute, "
            "claim, complete, recruit for, or otherwise mutate any discovered daily item. "
            "Finish after the complete visible target set and its coverage boundaries are "
            "recorded. "
            if is_stzb_discovery_only_goal(context.goal)
            else "Then complete every currently incomplete available item and finally "
            "reopen and visibly reread the complete checklist. "
        )
        family_instruction = (
            "\nSTZB daily vNext requirements: first reach and visibly inspect today's "
            "complete daily checklist or bounded multi-surface target set. "
            f"{execution_instruction}"
            "A map-side quick task strip with one tracked objective is navigation only, "
            "not that checklist: plan a separate observable stage for reaching a "
            "daily target surface that shows explicit daily identity and item state. "
            "Current game builds may rename the former daily-affairs page to 事务 or move "
            "it to 巡察. Never treat the 事务 label alone as daily identity, but do inspect "
            "visible 巡察, 每日/每天刷新, 今日, or equivalent current-cycle evidence. "
            "The current-day target set may span several explicit daily cards or surfaces; "
            "preserve each item and the visible start/middle/end coverage views instead of "
            "inventing one legacy page. For execution goals, plan item execution only "
            "after the screenshot "
            "visibly identifies a daily/today/activity category; otherwise plan an "
            "observable fallback: leave the exhausted task surface, inspect the current "
            "main navigation for a visibly labeled activity/daily entry, and only then "
            "use an honest partial stop if no visible route remains. "
            "Treat the task panel as an identity investigation, not as a presumed daily "
            "page: inspect 主要事宜, 事务, and 名望 in separate screenshot-verifiable stages, "
            "and word each stage as confirming whether daily/current-cycle identity is "
            "present so a visibly negative result can close that stage. Never combine "
            "multiple task-panel tabs into one '逐页/逐标签' stage. Likewise, keep carousel "
            "left boundary, one overlapping middle viewport, right boundary, and each planned "
            "card detail as distinct stages. A direct jump from the first to last viewport is "
            "not complete carousel coverage. "
            "对执行型目标，必须严格按以下四段顺序生成计划："
            "(1) 先用独立阶段覆盖轮播边界、卡片详情和任务页签，只确认是否存在明确的"
            "每日身份；当前画面只显示名称时，不得把某张卡片预设为每日来源。"
            "(2) 在任何执行、领取或完成阶段之前，单独加入一条类似‘汇总以上"
            "各独立表面，冻结并记录今天完整目标清单及覆盖边界’的可观察阶段。"
            "(3) 只在该冻结阶段之后规划执行，不得把多项物理操作合并成一个不可单屏"
            "验证的阶段。(4) 最后从新鲜画面分开复读已确认的每日表面；对先前为"
            "否定结果的页面，只能复核其仍无每日身份，不得写成复读其每日条目。"
            "初始规划尚未得到冻结清单，不得猜测第四个及更多未知条目；最多为冻结后"
            "发现的前三个当前可行条目各预留一个执行阶段，把其余阶段留给完整发现和"
            "逐表面最终复读。清单确有更多条目时必须由后续有证据的重规划扩展，不能"
            "用‘其余条目逐一执行’合并占位。"
            "Do not reduce the goal to launching the game, opening one panel, or claiming "
            "one reward. Do not invent task-specific coordinates. If an item needs elapsed "
            "time or an unavailable external condition, preserve it as remaining instead "
            "of claiming all done.\n"
        )
    return (
        f"Owner goal: {context.goal}\n"
        f"Owner updates: {_owner_updates(context.owner_inputs)}\n"
        f"Current observation: {context.observation.summary}\n"
        f"Verified Skill Memory: {_skill_memory(context.skill_memory)}"
        f"{family_instruction}"
    )


def _stzb_daily_plan_is_semantically_valid(
    goal: str, subgoals: tuple[str, ...],
) -> bool:
    return not _stzb_daily_plan_issues(goal, subgoals)


def _stzb_daily_recovery_issues(
    goal: str,
    current_subgoal: str,
    subgoals: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate a recovery replacement and retain the stalled stage's core outcome."""

    if normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY:
        return ()
    issues = [
        issue
        for issue in _stzb_daily_plan_issues(goal, subgoals)
        if not issue.startswith("missing_")
    ]
    if any(_looks_truncated_subgoal(item) for item in subgoals):
        issues.append("truncated_subgoal")
    if any(
        re.search(
            r"(?:^|[，。；;])\s*(?:若|如果|否则|如未|如无|没有则|未显示则)",
            item,
        )
        for item in subgoals
    ):
        issues.append("conditional_stage")

    joined = "\n".join(subgoals)
    if re.search(
        r"(?:每日|每天|今日).{0,20}(?:列表|详情|页面|目标表面|表面)"
        r"|(?:列表|详情|页面|目标表面|表面).{0,20}(?:每日|每天|今日)",
        current_subgoal,
    ) and not re.search(
        r"(?:每日|每天|今日).{0,20}(?:列表|详情|页面|目标表面|表面)"
        r"|(?:列表|详情|页面|目标表面|表面).{0,20}(?:每日|每天|今日)",
        joined,
    ):
        issues.append("missing_current_daily_surface")
    if re.search(
        r"(?:完整.{0,16}(?:清单|目标集|列表|条目|第一屏)|"
        r"第一屏.{0,16}(?:完整|边界)|"
        r"(?:清单|目标集|列表).{0,16}(?:起止|左右|上下|全部).{0,8}边界|"
        r"覆盖.{0,16}(?:起止|左右|上下|全部).{0,8}边界)",
        current_subgoal,
    ) and not re.search(
        r"(?:完整.{0,16}(?:清单|目标集|列表|条目|第一屏)|"
        r"第一屏.{0,16}(?:完整|边界)|"
        r"(?:清单|目标集|列表).{0,16}(?:起止|左右|上下|全部).{0,8}边界|"
        r"覆盖.{0,16}(?:起止|左右|上下|全部).{0,8}边界)",
        joined,
    ):
        issues.append("missing_current_discovery")
    current_boundary = re.search(
        r"(左|右)(?:侧)?边界|最(左|右)", current_subgoal,
    )
    if current_boundary is not None:
        direction = next(
            value for value in current_boundary.groups() if value is not None
        )
        if re.search(
            rf"(?:{direction}(?:侧)?边界|最{direction})", joined,
        ) is None:
            issues.append("missing_current_activity_boundary")
    if re.search(
        r"(?:执行|完成|领取|招募|占领|升级|处理).{0,24}"
        r"(?:条目|任务|事项|奖励|招募|占领|升级|巡察|状态|进度)",
        current_subgoal,
    ) and not re.search(
        r"(?:执行|完成|领取|招募|占领|升级|处理).{0,24}"
        r"(?:条目|任务|事项|奖励|招募|占领|升级|巡察|状态|进度)"
        r"|(?:阻塞|不可完成|条件不足|资源不足|时间未到)",
        joined,
    ):
        issues.append("missing_current_execution")
    if re.search(
        r"(?:重新|再次|最终|独立).{0,24}(?:复读|复查|读取|确认|打开)",
        current_subgoal,
    ) and not re.search(
        r"(?:重新|再次|最终|独立).{0,24}(?:复读|复查|读取|确认|打开)",
        joined,
    ):
        issues.append("missing_current_reread")
    return tuple(dict.fromkeys(issues))


_STZB_REFLECTION_FALLBACK_STRATEGY = "bounded STZB visible-surface recovery"


def _stzb_daily_reflection_fallback(
    context: ReflectionContext,
) -> ReflectionDecision | None:
    """Preserve one read-only stalled outcome when Qwen cannot shape a recovery.

    This is deliberately unavailable for execution stages and may only replace
    one model reflection in a row. It re-grounds through an observable
    navigation result without coordinates, then restores the exact stalled
    outcome so invalid role formatting cannot discard the owner goal.
    """

    current = context.subgoal.description.strip()
    if (
        normalize_goal_family(context.goal) != STZB_DAILY_GOAL_FAMILY
        or _is_stzb_execution_stage(current)
        or context.strategy == _STZB_REFLECTION_FALLBACK_STRATEGY
    ):
        return None
    replacement = tuple(dict.fromkeys((
        "画面已回到可操作的主导航表面，主导航入口清晰可见。",
        current,
    )))
    if _stzb_daily_recovery_issues(context.goal, current, replacement):
        return None
    return ReflectionDecision(
        strategy=_STZB_REFLECTION_FALLBACK_STRATEGY,
        terminate=False,
        reason=(
            "本地角色模型未返回可用恢复格式；仅重建可见导航状态并保留原子目标，"
            "不执行清单条目。"
        ),
        replacement_subgoals=replacement,
    )


def _reflection_retry_feedback(
    issues: tuple[str, ...], current_subgoal: str,
) -> str:
    messages = {
        "invalid_structure": "返回非空 strategy/reason、布尔 terminate 和非空恢复阶段数组。",
        "truncated_subgoal": "每个恢复阶段必须是完整结果句，不得以连接词或未完成短语结尾。",
        "conditional_stage": "不得写若、如果、否则等分支；把当前可见的下一条单一路径写成无条件结果阶段。",
        "aggregated_surface": "不同任务页签或列表边界必须拆成独立的单屏恢复阶段。",
        "presumed_daily_identity": "任务面板、主要事宜、事务或名望只能确认是否具有每日身份，不得预设其就是每日页。",
        "aggregated_execution": "不得把多个执行结果合成一个恢复阶段。",
        "missing_current_daily_surface": "恢复阶段替换了当前阶段，最后必须重新进入并证明当前要求的每日列表或详情表面。",
        "missing_current_discovery": "恢复阶段替换了当前阶段，最后必须重新达到当前要求的完整清单或边界覆盖结果。",
        "missing_current_activity_boundary": "恢复阶段替换了活动边界结果，最后必须重新达到当前要求的同一侧边界。",
        "missing_current_execution": "恢复阶段替换了当前阶段，最后必须重新达到当前执行结果或明确阻塞结果。",
        "missing_current_reread": "恢复阶段替换了当前阶段，最后必须重新达到当前独立最终复读结果。",
    }
    detail = "".join(messages[item] for item in issues if item in messages)
    return (
        "\n上一份恢复方案不满足运行时契约：" + detail
        + f"当前被替换子目标是：{current_subgoal}。"
        + "重新生成至多四个无条件、逐画面可验证的恢复结果；最后一个阶段必须重新达到当前子目标的核心结果，"
        + "如果当前子目标预设了实际不存在的单一每日清单页，不要继续虚构该页面或标题；"
        + "可以改为覆盖多个带明确每日/每天/今日机制的独立表面及其边界，重建同等完整目标集。"
        + "只选择当前画面可见的一条下一路径，不要附加备用分支。"
        + "运行时随后会自动接回原计划尚未尝试的尾部。"
    )


def _stzb_daily_plan_scaffold(goal: str) -> tuple[str, ...]:
    """Return a bounded semantic fallback, never a coordinate/action macro."""

    discovery = (
        "率土之滨可操作画面已稳定显示，当前加载层和遮挡性弹窗已结束或关闭",
        "打开精彩活动面板并切换到活动页签，活动轮播及可见卡片已显示",
        "将精彩活动的活动轮播移动到左侧边界，记录左侧边界画面全部可见卡片及每日、每天或今日身份标识",
        "从活动左侧边界向右移动一个有重叠的可见卡片组，记录中间视口全部可见卡片及每日、每天或今日身份标识",
        "将精彩活动的活动轮播移动到右侧边界，记录右侧边界画面全部可见卡片及每日、每天或今日身份标识",
        "打开轮播中第一个明确带每日、每天或今日文案的活动卡片详情，确认其每日机制和当前条目状态",
        "返回活动轮播并打开第二个明确带每日、每天或今日文案的活动卡片详情，确认其每日机制和当前条目状态",
        "关闭精彩活动面板并回到主界面，主导航入口清晰可见",
        "从主界面打开任务面板，主要事宜、事务和名望页签清晰可见",
        "在任务面板打开主要事宜页签，确认该页签是否具有每日、每天或今日周期身份",
        "在任务面板打开事务页签，确认该页签是否具有每日、每天或今日周期身份",
        "在任务面板打开名望页签，确认该页签是否具有每日、每天或今日周期身份",
        "关闭任务面板回到主导航界面，打开当前可见的巡察入口并确认其今日次数、条目和状态",
        "汇总以上各独立表面，冻结并记录今天完整每日目标清单及覆盖边界",
    )
    if is_stzb_discovery_only_goal(goal):
        return discovery
    return discovery + (
        "执行冻结清单中第一个当前可行条目并使其在单屏可见为已完成、已领取或明确阻塞状态",
        "执行冻结清单中第二个当前可行条目并使其在单屏可见为已完成、已领取或明确阻塞状态",
        "从新鲜画面重新打开第一个已确认含每日身份的每日表面，复读其全部条目最终状态",
        "从新鲜画面重新打开主要事宜页签，确认是否仍无每日身份并记录状态",
        "从新鲜画面重新打开事务页签，确认是否仍无每日身份并记录状态",
        "从新鲜画面重新打开名望页签，确认是否仍无每日身份并记录状态",
        "从新鲜画面重新打开巡察入口，复读其今日次数、全部条目和最终状态",
        "从新鲜画面重新打开精彩活动并移动到左侧边界，复读左侧边界的全部每日候选及最终状态",
        "从活动左侧边界向右移动一个有重叠的可见卡片组，复读中间视口的全部每日候选及最终状态",
        "将精彩活动轮播移动到右侧边界，复读右侧边界的全部每日候选及最终状态",
    )


def _stzb_daily_plan_issues(
    goal: str, subgoals: tuple[str, ...],
) -> tuple[str, ...]:
    if normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY:
        return ()
    issues: list[str] = []
    task_surface = r"(?:任务[\s\"'“”‘’]{0,3}(?:面板|总览|入口|页)|主要事宜|事务|名望)"
    confirmed_task_surfaces: set[str] = set()
    task_surface_indices: list[int] = []
    patrol_indices: list[int] = []
    for item_index, item in enumerate(subgoals):
        if re.search(
            r"(?:^|[，。；;（(])\s*(?:若|如果|否则|如未|如无|如有|如存在|如可见|没有则|未显示则)",
            item,
        ):
            issues.append("conditional_stage")
        if re.search(
            task_surface + r".{0,24}"
            r"(?:逐页|逐标签|全部标签|所有标签|分别查看|全部查看|所有页签)"
            r"|" + task_surface + r".{0,32}(?:逐一|依次|分别)(?:检查|查看)?"
            r".{0,16}(?:每个|多个|所有|各个)?(?:页签|子分类)"
            r"|(?:逐页|逐标签|分别查看|全部查看|所有).{0,24}" + task_surface,
            item,
        ):
            issues.append("aggregated_surface")
        if re.search(
            r"(?:关闭|退出|离开).{0,20}精彩活动面板.{0,40}"
            r"(?:势力发展|充值好礼|活动).{0,8}页签"
            r"|主界面.{0,24}(?:势力发展|充值好礼).{0,8}页签",
            item,
        ):
            issues.append("closed_surface_navigation")
        if (
            re.search(r"(?:主要事宜|事务|名望)", item)
            and re.search(r"(?:点击|进入|打开|切换)", item)
            and (
                re.search(r"(?:主导航|主界面)", item)
                and not re.search(r"任务(?:面板|总览|入口|页)", item)
                or re.search(r"精彩活动(?:面板)?", item)
                and not re.search(
                    r"(?:关闭|退出|离开).{0,24}精彩活动(?:面板)?.{0,48}"
                    r"(?:打开|进入).{0,16}任务(?:面板|总览|入口|页)",
                    item,
                )
            )
        ):
            issues.append("misplaced_task_tab_navigation")
        if (
            re.search(r"(?:每日|每天|今日|当前周期)", item)
            and re.search(
                r"(?:是否|有无|确认.{0,24}(?:是否|身份|标识|属于|存在|含有|有无)|"
                r"判断.{0,16}(?:身份|性质|是否))",
                item,
            )
        ):
            for surface_name in ("主要事宜", "事务", "名望"):
                if surface_name in item:
                    confirmed_task_surfaces.add(surface_name)
                    task_surface_indices.append(item_index)
        if (
            "巡察" in item
            and re.search(r"(?:确认|查看|读取|记录).{0,32}(?:次数|条目|状态|今日|每日)", item)
        ):
            patrol_indices.append(item_index)
        if (
            re.search(task_surface, item)
            and re.search(r"(?:每日|每天|今日|当前周期)", item)
            and not (
                re.search(r"(?:主导航|主界面)", item)
                and re.search(r"可见", item)
                and re.search(r"(?:、|/|或)", item)
                and not re.search(r"(?:点击|进入|打开)", item)
            )
            and not re.search(
                r"(?:任务面板.{0,8}(?:已)?关闭|(?:关闭|退出|离开).{0,16}任务面板)"
                r".{0,80}(?:主导航|独立|不同)",
                item,
            )
            and not re.search(
                r"(?:是否|有无|确认.{0,24}(?:是否|身份|标识|属于|存在|含有|有无)|"
                r"判断.{0,16}(?:身份|性质|是否))",
                item,
            )
            and not (
                confirmed_task_surfaces == {"主要事宜", "事务", "名望"}
                and re.search(
                    r"(?:汇总|冻结|记录).{0,200}(?:完整|全部|所有)"
                    r".{0,24}(?:每日|日常|目标)?(?:清单|目标集|条目)",
                    item,
                )
            )
            and not re.search(
                r"(?:汇总|冻结|记录).{0,200}(?:明确|区分|标注).{0,24}"
                r"哪些.{0,24}(?:每日|日常).{0,8}身份",
                item,
            )
            and not re.search(
                r"(?:重新|再次|最终|从新鲜画面).{0,48}"
                r"(?:打开|进入|复读|复核).{0,64}"
                r"(?:已确认(?:为|具有|含有)?|具有).{0,12}"
                r"(?:每日|每天|今日)",
                item,
            )
        ):
            issues.append("presumed_daily_identity")
        if re.search(
            r"(?:其余|全部|所有).{0,20}(?:逐项|逐一|逐个|依次|分别).{0,16}"
            r"(?:执行|完成|领取|处理)",
            item,
        ):
            issues.append("aggregated_execution")
        if re.search(
            r"冻结清单中第(?:四|五|六|七|八|九|十|[4-9]|[1-9][0-9]+)个"
            r".{0,32}(?:执行|完成|领取|处理)",
            item,
        ):
            issues.append("invented_execution_slot")
        if (
            re.search(r"(?:重新|再次|最终|从新鲜画面).{0,48}(?:复读|复核|打开|进入)", item)
            and re.search(r"(?:先前|此前|之前).{0,24}(?:否定|无每日身份|非每日)", item)
            and sum(name in item for name in ("主要事宜", "事务", "名望")) != 1
        ):
            issues.append("aggregated_negative_reread")

    joined = "\n".join(subgoals)
    discovery_present = re.search(
        r"(?:发现|清单|目标集|列表|任务面板|主要事宜|事务|名望|活动|轮播|"
        r"每日|每天|今日|巡察).{0,32}(?:查看|读取|记录|确认|覆盖|边界|身份|状态)"
        r"|(?:查看|读取|记录|确认|覆盖).{0,32}"
        r"(?:清单|目标集|列表|任务面板|主要事宜|事务|名望|活动|轮播|每日|每天|今日|巡察)",
        joined,
        re.IGNORECASE,
    ) is not None
    if not discovery_present:
        issues.append("missing_discovery")

    if is_stzb_discovery_only_goal(goal):
        final_record_present = re.search(
            r"(?:最终|完整|全部|汇总|冻结).{0,28}(?:记录|复读|复查|确认|清单|目标集|边界)"
            r"|(?:记录|汇总|冻结).{0,28}(?:完整|全部|边界|清单|目标集)",
            joined,
            re.IGNORECASE,
        ) is not None
        if not final_record_present:
            issues.append("missing_discovery_record")
    else:
        if confirmed_task_surfaces != {"主要事宜", "事务", "名望"}:
            issues.append("missing_task_identity_surfaces")
        left_activity_boundary_indices = [
            index for index, item in enumerate(subgoals)
            if re.search(
                r"(?:精彩活动|活动|轮播|卡片).{0,40}(?:左右边界|起止边界|左边界|左侧边界|最左)"
                r"|(?:左右边界|起止边界|左边界|左侧边界|最左).{0,40}(?:精彩活动|活动|轮播|卡片)",
                item,
            )
        ]
        right_activity_boundary_indices = [
            index for index, item in enumerate(subgoals)
            if re.search(
                r"(?:精彩活动|活动|轮播|卡片).{0,40}(?:左右边界|起止边界|右边界|右侧边界|最右)"
                r"|(?:左右边界|起止边界|右边界|右侧边界|最右).{0,40}(?:精彩活动|活动|轮播|卡片)",
                item,
            )
        ]
        if not left_activity_boundary_indices or not right_activity_boundary_indices:
            issues.append("missing_activity_boundary")
        elif set(left_activity_boundary_indices) & set(right_activity_boundary_indices):
            issues.append("aggregated_surface")
        activity_middle_indices = [
            index for index, item in enumerate(subgoals)
            if re.search(r"(?:活动|轮播|卡片)", item)
            and re.search(
                r"(?:中间视口|中间画面|中部视口|有重叠|一个可见卡片组)", item
            )
        ]
        if not activity_middle_indices:
            issues.append("missing_activity_middle")
        activity_detail_indices = [
            index for index, item in enumerate(subgoals)
            if re.search(r"(?:活动|卡片|登录奖励|心愿征程).{0,32}详情|详情.{0,32}(?:活动|卡片)", item)
            and re.search(r"(?:是否|确认|记录|读取).{0,40}(?:每日|每天|今日|身份|条目|状态)", item)
        ]
        if not activity_detail_indices:
            issues.append("missing_activity_detail")
        if not patrol_indices:
            issues.append("missing_patrol_surface")
        complete_discovery_indices = [
            index
            for index, item in enumerate(subgoals)
            if re.search(
                r"(?:(?:汇总|冻结).{0,96}(?:完整|全部|所有|全量|覆盖).{0,40}"
                r"(?:清单|目标集|每日表面|日常表面|候选|条目|边界)|"
                r"(?:覆盖所有|覆盖全部).{0,96}(?:记录|形成|冻结).{0,32}"
                r"(?:完整|全部|全量).{0,24}(?:清单|目标集|候选|条目))",
                item,
                re.IGNORECASE,
            )
        ]
        if not complete_discovery_indices:
            issues.append("missing_complete_discovery")
        if complete_discovery_indices:
            first_manifest_index = min(complete_discovery_indices)
            required_discovery_groups = (
                task_surface_indices,
                left_activity_boundary_indices,
                activity_middle_indices,
                right_activity_boundary_indices,
                activity_detail_indices,
                patrol_indices,
            )
            # Later final rereads legitimately mention the same surfaces.  The
            # freeze is premature only when an entire required discovery group
            # has no observation before the first manifest stage.
            if any(
                group and not any(index < first_manifest_index for index in group)
                for group in required_discovery_groups
            ):
                issues.append("manifest_before_discovery_complete")
        if re.search(
            r"(?:没有|不存在|若无|如果没有).{0,20}单一.{0,12}(?:每日|今日).{0,8}(?:页|清单)"
            r"|所有.{0,20}(?:独立)?表面",
            goal,
        ) and not (
            re.search(
                r"(?:多个|所有|全部|各个|多表面).{0,24}(?:独立)?(?:每日|日常|目标)?表面"
                r"|(?:独立表面).{0,24}(?:全部|所有|边界|覆盖)"
                r"|(?:各|多个|所有|全部)(?:个|独立)?表面(?:中)?(?:所有|全部)?",
                joined,
            )
            and complete_discovery_indices
        ):
            issues.append("missing_multisurface_discovery")
        execution_indices = [
            index
            for index, item in enumerate(subgoals)
            if _is_stzb_execution_stage(item)
        ]
        if not execution_indices:
            issues.append("missing_execution")
        elif complete_discovery_indices:
            first_execution_index = min(execution_indices)
            if (
                first_execution_index is not None
                and first_execution_index < min(complete_discovery_indices)
            ):
                issues.append("execution_before_complete_discovery")
        reread_present = re.search(
            r"(?:重新|再次|最终).{0,32}(?:打开|进入|读取|复读|复查|确认).{0,32}"
            r"(?:清单|目标集|列表|每日|每天|今日|条目|状态|入口)"
            r"|(?:独立复读|独立复查|最终复读|最终复查)",
            joined,
            re.IGNORECASE,
        ) is not None
        if not reread_present:
            issues.append("missing_final_reread")
    return tuple(dict.fromkeys(issues))


def _is_stzb_execution_stage(item: str) -> bool:
    """Distinguish a physical execution goal from discovery of executability."""

    if re.search(r"(?:阻塞|不可完成|条件不足|资源不足|时间未到)", item):
        return True
    return re.search(
        r"(?<!可)(?<!不)(?<!未)(?:执行|领取|处理).{0,32}"
        r"(?:任务|条目|事项|奖励|招募|占领|升级|巡察|操作|反馈|结果)"
        r"|(?:完成|招募|占领|升级).{0,24}"
        r"(?:任务|条目|事项|奖励|武将|土地|建筑|事件|操作|反馈|结果)",
        item,
        re.IGNORECASE,
    ) is not None


def _plan_retry_feedback(
    issues: tuple[str, ...], rejected_subgoals: tuple[str, ...] = (),
) -> str:
    messages = {
        "invalid_structure": "输出必须是1到24个非空阶段，不得加入结束或汇报阶段。",
        "conditional_stage": "线性计划不得写若、如果、否则、如无等分支，也不要用括号内的‘如某卡片’举例；仅把含条件或举例的阶段改写成确定的单一结果。",
        "aggregated_surface": "不同任务页签或列表边界必须拆成不同的单屏阶段；最终复读也不得用‘逐页签’合并它们。",
        "closed_surface_navigation": "势力发展、充值好礼和活动是精彩活动面板内的页签；必须在关闭该面板之前分别查看，不得说在主界面点击它们。",
        "misplaced_task_tab_navigation": "主要事宜、事务和名望是任务面板内的页签，既不是主导航入口也不属于精彩活动面板；先关闭精彩活动回到主界面，再用一个阶段打开任务面板，然后在面板内分别切换三个页签。",
        "presumed_daily_identity": "任务面板、主要事宜、事务或名望只能先确认是否具有每日身份，不得预设其就是每日页；最终复读请写成‘重新打开该页，确认是否仍无每日身份并记录状态’，不要写‘无每日身份或显示当前状态’这种二选一结果。",
        "missing_task_identity_surfaces": "普通完成目标必须保留三个独立阶段，分别写明‘确认主要事宜是否具有每日身份’、‘确认事务是否具有每日身份’和‘确认名望是否具有每日身份’，不得因当前停在活动面板就漏掉它们。",
        "missing_activity_boundary": "普通完成目标必须在关闭精彩活动面板前，用独立阶段确认活动轮播的左右或起止边界。",
        "missing_activity_middle": "普通完成目标必须在活动左右边界之间增加一个有重叠的中间视口阶段，记录中间卡片，禁止从起点直接跳到终点。",
        "missing_activity_detail": "普通完成目标必须打开至少一张活动卡片详情，独立确认其是否含每日、每天或今日机制；单看卡片名称不足以关闭发现。",
        "missing_patrol_surface": "普通完成目标必须在冻结清单前独立打开巡察入口，确认今日次数、条目和状态。",
        "aggregated_execution": "不要把其余或全部条目合成一个逐项执行阶段；每个阶段必须能由一张截图验证。",
        "invented_execution_slot": "初始计划尚未冻结真实清单，最多保留前三个未知条目的独立执行占位；删除第四个及以后猜出的序号执行阶段，把这些位置用于缺失的发现或最终复读。",
        "aggregated_negative_reread": "最终复读不能把多个先前否定的页面合成一个阶段；分别重新打开主要事宜、事务和名望并记录各自是否仍无每日身份。",
        "missing_discovery": "计划缺少发现并覆盖当前每日目标集及可见边界的阶段。",
        "missing_complete_discovery": "必须在执行前新增一个单独阶段，明确写成‘汇总以上各独立表面，冻结并记录今天完整目标清单及覆盖边界’。",
        "manifest_before_discovery_complete": "冻结完整清单阶段必须排在任务三页、活动左右边界、每日候选详情和巡察状态全部独立确认之后。",
        "missing_multisurface_discovery": "目标已说明不存在单一页面时，计划必须在各页独立确认后，用一条‘汇总以上各独立表面’阶段冻结所有显式日常表面及其边界。",
        "execution_before_complete_discovery": "将所有含执行、领取或完成物理操作的阶段，移到‘汇总以上各独立表面并冻结完整目标清单’阶段之后。",
        "missing_discovery_record": "只发现目标必须包含完整记录或冻结已覆盖目标集的收尾阶段。",
        "missing_execution": "普通完成目标缺少执行当前可行条目或明确保留阻塞项的阶段。",
        "missing_final_reread": "普通完成目标缺少从新鲜画面重新打开并独立复读最终清单或目标集的阶段。",
    }
    detail = "".join(messages[item] for item in issues if item in messages)
    rejected_plan = (
        "\n上一份被拒绝计划的完整 JSON 是："
        + json.dumps(
            {"subgoals": list(rejected_subgoals)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "。必须复制其中仍有效的阶段及其顺序，只修复上面指出的问题；"
        "不要从头另拟整份计划。需要增加缺失阶段时，优先删除重复卡片详情或重复的"
        "序号占位执行阶段，且总数不得超过24。"
        if rejected_subgoals
        else ""
    )
    return (
        "\n上一份计划未完整保持原始目标：" + detail
        + rejected_plan
        + "仍须返回一份完整计划；不得合并不同页面、不同边界或多个执行结果。"
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
        target = ""
        if intent is not None and intent.name in {"tap", "long_press"}:
            description = intent.arguments.get("target_description")
            if isinstance(description, str) and description.strip():
                target = f"; target={description.strip()[:160]}"
        evidence = (
            f"; verifier={verification.evidence.strip()[:280]}"
            if verification is not None and verification.evidence.strip() else ""
        )
        recent.append(
            f"attempt {attempt.sequence}: {action}{target}; {transport}; {result}{evidence}"
        )
    history = "\n".join(recent) if recent else "None"
    return (
        "Generate exactly one atomic next move from the current screenshot.\n\n"
        f"Overall goal: {context.goal}\n"
        f"Current subgoal: {context.subgoal.description}\n"
        f"Current strategy: {context.strategy}\n"
        f"Consecutive no-progress attempts: {context.consecutive_no_progress}\n"
        f"Owner updates: {_owner_updates(context.owner_inputs)}\n"
        f"Verified Skill Memory: {_skill_memory(context.skill_memory)}\n\n"
        f"Scene-conditioned Experience: {_experience_hints(context.experience_hints)}\n\n"
        f"Recent attempts (text only):\n{history}"
        f"{_stzb_daily_executor_instruction(context.goal, context.subgoal.description)}"
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
        if intent is not None and intent.name in {"tap", "long_press"}:
            target = intent.arguments.get("target_description")
            if isinstance(target, str) and target.strip():
                action = f"{action}:{target.strip()[:160]}"
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
        + _stzb_daily_reflection_instruction(context.goal, context.subgoal.description)
    )


def _stzb_daily_executor_instruction(goal: str, subgoal: str) -> str:
    if normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY:
        return ""
    activity_boundary = _stzb_activity_boundary_kind(goal, subgoal)
    boundary_guidance = (
        " For this left-boundary stage, do not terminate based only on the apparent "
        "layout. Start on the center artwork or caption of a visible activity card and "
        "drag the card content toward the right. Repeat on later attempts until the same "
        "directed swipe produces no material visual change; that unchanged terminal probe "
        "is the boundary evidence."
        if activity_boundary == "left"
        else
        " For this right-boundary stage, do not terminate based only on the apparent "
        "layout. Start on the center artwork or caption of a visible activity card and "
        "drag the card content toward the left. Repeat on later attempts until the same "
        "directed swipe produces no material visual change; that unchanged terminal probe "
        "is the boundary evidence."
        if activity_boundary == "right"
        else ""
    )
    discovery_boundary = (
        " This goal is discovery_only: actions may navigate, scroll, open a read-only "
        "detail, or close it, but must not claim, recruit, complete, or execute a daily "
        "item. If a visible control could trigger the item rather than inspect it, do not "
        "press it. When the current frame already visibly proves a read/record/inspect "
        "subgoal, terminate that subgoal with success before navigating away; do not "
        "close or change the page until the observation subgoal has been verified."
        if is_stzb_discovery_only_goal(goal)
        else ""
    )
    return (
        "\n\nSTZB daily visual boundary: a map screen with one tracked objective, "
        "progress such as 1/4, or commander/party rows is only a quick task "
        "strip, never the complete daily checklist. If the global task ribbon "
        "has already only toggled that strip, do not repeat it while the strip "
        "is visible. Re-ground one different affordance that is visibly part of "
        "the strip or its task row (for example a visible disclosure/detail "
        "control), and target the visible control itself rather than an estimated "
        "location. On an activity carousel, explicit cards containing 每日/每天 are "
        "candidate daily items: open the visible card or move toward an uninspected "
        "carousel view. For a middle-viewport stage, use one short page-wise swipe that "
        "starts on the body of a visible activity card, not on empty background, and drags "
        "the card content toward the left to reveal cards farther to the right. Keep some "
        "prior cards visible, so the AFTER frame overlaps the prior viewport; do not jump "
        "directly from the first view to the last. Do not repeat a swipe whose before/after scene is "
        "perceptually unchanged. Current screenshot evidence outranks this guidance."
        + boundary_guidance
        + discovery_boundary
    )


def _stzb_activity_boundary_kind(goal: str, subgoal: str) -> str | None:
    if normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY:
        return None
    if re.search(r"(?:精彩活动|活动|轮播|卡片)", subgoal) is None:
        return None
    left = re.search(r"(?:左侧边界|左边界|最左)", subgoal) is not None
    right = re.search(r"(?:右侧边界|右边界|最右)", subgoal) is not None
    if left == right:
        return None
    return "left" if left else "right"


def _looks_truncated_subgoal(value: str) -> bool:
    compact = value.strip().rstrip("。.!！?？)）]】\"'”’」")
    return bool(
        re.search(
            r"(?:标注为|显示为|定位到|进入到|点击到|的|与|和|或|并|在|到)$",
            compact,
        )
    )


def _stzb_daily_verification_instruction(goal: str, subgoal: str) -> str:
    if normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY:
        return ""
    completeness_boundary = (
        " 当前子目标明确要求完整清单、全部条目、覆盖列表边界或记录完成状态，"
        "所以只有相关独立页面明确显示所需条目及状态、且画面证据达到该子目标要求时"
        "才可 satisfied。"
        if re.search(
            r"(?:完整.{0,8}(?:清单|记录|覆盖)|全部.{0,8}(?:条目|入口|边界)|"
            r"所有可见|各自状态|完成状态|列表.{0,8}(?:上|下|边界))",
            subgoal,
            re.IGNORECASE,
        )
        else (
            " 当前子目标只是导航、关闭、返回、打开或切换页面时，只按该当前子目标"
            "判断；画面已证明要求的页面状态即可 satisfied，不得因整体目标尚未完成而"
            "降级为 progress。"
        )
    )
    activity_boundary = (
        " 活动轮播左右边界是几何和终端手势结果，不绑定任何固定卡片标题。"
        "不得因为过去画面出现过某张卡，就要求该卡必须成为本轮第一张或最后一张；"
        "活动内容会变化，当前方向正确的终端探测证据优先。"
        if _stzb_activity_boundary_kind(goal, subgoal) is not None
        else ""
    )
    return (
        " 率土每日边界：地图上的单条追踪任务、1/4 等进度或武将/队伍行只是快捷任务条。"
        "它可以算 progress 并作为入口或导航证据，但不能单独证明完整每日清单。"
        "“名望”“主要事宜”“事务”等通用分类本身不是每日身份；但新版界面若同时可见"
        "“巡察”、每日/每天刷新或今日周期证据，可证明“事务”的日常迁移身份。活动轮播中"
        "带“每日/每天”文案的卡片可作为清单候选项，但只有覆盖边界完整时才能满足完整发现。"
        + activity_boundary
        + completeness_boundary
    )


def _stzb_daily_verdict_guard(
    goal: str, subgoal: str, verdict: str, evidence: str,
) -> tuple[str, str]:
    """Require a visible daily-category identity before checklist satisfaction."""

    numeric_guard = _stzb_explicit_numeric_target_guard(
        goal, subgoal, verdict, evidence
    )
    if numeric_guard is not None:
        return numeric_guard

    if (
        normalize_goal_family(goal) == STZB_DAILY_GOAL_FAMILY
        and verdict == "satisfied"
        and re.search(
            r"(?:确认|判断|查看).{0,24}(?:是否|属于|每日|每天|今日)",
            subgoal,
            re.IGNORECASE,
        )
        and re.search(
            r"(?:(?:每日|每天|今日)(?:进入|登录|招募|刷新|重置)|"
            r"(?:每日|每天|今日).{0,24}(?:可获得|可领取|增加.{0,6}次数))",
            evidence,
            re.IGNORECASE,
        )
    ):
        # A detail page can prove that an activity is a daily candidate through
        # an explicit reset/earning mechanic in its body even when its title is
        # a seasonal event name.  This closes only the identity-inspection
        # subgoal; it does not prove a complete checklist or task completion.
        return verdict, evidence
    if (
        normalize_goal_family(goal) == STZB_DAILY_GOAL_FAMILY
        and verdict == "satisfied"
        and (
            re.search(r"(?:一个|该|此).{0,64}(?:活动)?详情", subgoal)
            or re.search(r"每日候选《登录奖励》.{0,32}详情", subgoal)
        )
        and re.search(r"(?:每日|每天|今日)", subgoal)
        and re.search(
            r"(?:(?:第一日.{0,80}第十四日|第十四日.{0,80}第一日)"
            r".{0,80}(?:登录奖励|累计登录)|"
            r"(?:登录奖励|累计登录).{0,80}"
            r"(?:第一日.{0,80}第十四日|第十四日.{0,80}第一日)|"
            r"逐日.{0,24}(?:登录|奖励|任务|刷新))",
            evidence,
            re.IGNORECASE,
        )
    ):
        # A numbered day-by-day login schedule is explicit current-cycle
        # identity even when the renamed detail title omits 每日/每天. This only
        # closes the single activity-detail inspection result.
        return verdict, evidence
    if (
        normalize_goal_family(goal) == STZB_DAILY_GOAL_FAMILY
        and verdict == "satisfied"
        and re.search(
            r"(?:(?:进入|打开|定位).{0,32}(?:入口|页面|画面)|"
            r"点击.{0,64}入口(?:.{0,24}(?:进入|打开).{0,24}(?:详情)?画面)?)",
            subgoal,
        )
        and re.search(
            r"(?:(?:每日|每天|巡察).{0,24}(?:或|/|、).{0,16}活动|"
            r"活动.{0,16}(?:或|/|、).{0,24}(?:每日|每天|巡察))",
            subgoal,
            re.IGNORECASE,
        )
        and re.search(r"(?:精彩活动|活动入口|活动页|活动面板)", evidence)
    ):
        # An explicit navigation alternative may be satisfied by reaching the
        # visible activity branch. This closes only that routing subgoal; later
        # checklist/detail stages still require positive daily-cycle identity.
        return verdict, evidence
    candidate_marker = re.search(r"《([^》]+)》", subgoal)
    if (
        normalize_goal_family(goal) == STZB_DAILY_GOAL_FAMILY
        and verdict == "satisfied"
        and candidate_marker is not None
        and re.search(r"(?:定位|清晰显示).{0,48}(?:每日候选|卡片)", subgoal)
        and candidate_marker.group(1).strip() in evidence
        and re.search(
            r"(?:(?:每日|每天|今日)(?:进入|登录|招募|刷新|重置)|"
            r"(?:每日|每天|今日).{0,24}(?:可获|可获得|可领取|增加.{0,6}次数))",
            evidence,
            re.IGNORECASE,
        )
    ):
        # A candidate-location stage is complete when the exact manifest title
        # and its visible positive daily mechanic are both on screen. This does
        # not close the following detail inspection or any execution result.
        return verdict, evidence
    if (
        normalize_goal_family(goal) == STZB_DAILY_GOAL_FAMILY
        and verdict == "satisfied"
        and re.search(
            r"(?:是否|判断.{0,12}(?:性质|身份)|确认.{0,12}(?:属于|是否|性质|身份))",
            subgoal,
            re.IGNORECASE,
        )
        and re.search(
            r"(?:不是|并非|而非|属于.{0,12}(?:主线|章节|通用)|"
            r"(?:无|未见|没有).{0,12}(?:今日|每日|每天|日常)"
            r".{0,8}(?:标识|身份|字样|文案|任务|条目|证据))",
            evidence,
            re.IGNORECASE,
        )
    ):
        # A discovery walk must be able to close a negative branch.  Proving
        # that a visible surface is *not* the daily checklist completes an
        # identity-inspection subgoal without claiming daily identity.
        return verdict, evidence
    if (
        normalize_goal_family(goal) == STZB_DAILY_GOAL_FAMILY
        and verdict == "satisfied"
        and re.search(
            r"(?:带有|标注|写有).{0,24}(?:每日|每天).{0,24}(?:卡片|入口)",
            subgoal,
            re.IGNORECASE,
        )
        and re.search(
            r"(?:内部页面|活动内部|登录奖励|俸禄).{0,80}(?:条目|第一日|第十四日|累计登录)",
            evidence,
            re.IGNORECASE,
        )
    ):
        # Some activity cards carry the daily identity only on the outer
        # carousel; their detail page changes the title (for example 登录奖励
        # -> 俸禄).  Preserve that already observed parent identity while the
        # AFTER frame proves the matching internal checklist and its states.
        return verdict, evidence
    if (
        normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY
        or verdict != "satisfied"
        or not re.search(r"(?:每日|日常|今日|活跃)", subgoal, re.IGNORECASE)
        or re.search(
            r"(?:不是|并非|而非|均非|非每日|未出现|未见|不存在|缺少).{0,20}"
            r"(?:每日|日常|今日|活跃)?",
            subgoal,
            re.IGNORECASE,
        )
    ):
        return verdict, evidence
    daily_identity = re.search(
        r"(?:标题|页签|标签|栏目|入口|页面名|页面标题)"
        r"(?:为|是|显示|写有|标注|名为)?[\s\"“”'「」]{0,3}"
        r"(?:每日|日常|今日|活跃)"
        r"|(?:每日|日常|今日|活跃)(?:任务|活跃度)?"
        r"[\s\"“”'「」]{0,3}(?:标题|页签|标签|栏目|入口)",
        evidence,
        re.IGNORECASE,
    )
    if daily_identity is not None:
        return verdict, evidence
    migrated_affairs_identity = (
        re.search(r"(?:事务|巡察)", evidence, re.IGNORECASE) is not None
        and re.search(
            r"(?:巡察|每日.{0,12}刷新|每天.{0,12}刷新|今日.{0,12}(?:次数|事务|任务)|"
            r"每天.{0,12}(?:获得|增加).{0,12}次数)",
            evidence,
            re.IGNORECASE,
        )
        is not None
    )
    if migrated_affairs_identity:
        return verdict, evidence
    return (
        "progress",
        evidence.strip()[:7_600]
        + " [STZB daily guard: no visible daily/today/activity category identity]",
    )


def _stzb_explicit_numeric_target_guard(
    goal: str, subgoal: str, verdict: str, evidence: str,
) -> tuple[str, str] | None:
    """Reject satisfaction when fresh evidence shows a different explicit ratio.

    STZB exposes many compact state counters (for example occupation ``1/4`` or
    patrol ``2/5``).  A visual role can correctly transcribe the current value
    in its evidence while still choosing ``satisfied`` for a subgoal that asks
    for a different value.  The last ratio in the subgoal is treated as its
    requested target; only an explicit contradictory current-state cue or an
    explicit negative statement activates this guard.
    """

    if (
        normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY
        or verdict != "satisfied"
    ):
        return None
    requested = re.findall(r"(?<!\d)(\d{1,4})\s*/\s*(\d{1,4})(?!\d)", subgoal)
    if not requested:
        return None
    target_numerator, target_denominator = requested[-1]
    target_pattern = (
        rf"(?<!\d){re.escape(target_numerator)}\s*/\s*"
        rf"{re.escape(target_denominator)}(?!\d)"
    )
    negative_target = re.search(
        rf"(?:未|没有|尚未|并未).{{0,28}}"
        rf"(?:达到|变为|更新为|显示为|出现)?\s*{target_pattern}"
        rf"|{target_pattern}.{{0,20}}(?:未达成|未出现|未显示|没有达到)",
        evidence,
        re.IGNORECASE,
    )
    visible_current = re.search(
        rf"(?:显示|当前|仍|保持|进度|次数|条目).{{0,32}}"
        rf"(?<!\d)(\d{{1,4}})\s*/\s*{re.escape(target_denominator)}(?!\d)",
        evidence,
        re.IGNORECASE,
    )
    conflicting_current = (
        visible_current is not None
        and visible_current.group(1) != target_numerator
    )
    if negative_target is None and not conflicting_current:
        return None
    guarded_verdict = (
        "no_progress"
        if re.search(r"(?:仍|保持|未变化|没有变化|未更新|无变化)", evidence)
        else "progress"
    )
    return (
        guarded_verdict,
        evidence.strip()[:7_520]
        + " [STZB numeric guard: visible ratio contradicts requested target]",
    )


def _stzb_daily_reflection_instruction(goal: str, subgoal: str) -> str:
    if normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY:
        return ""
    return (
        "\nSTZB daily recovery boundary: do not relabel the map quick task strip "
        "as the complete checklist and do not repeat the global task ribbon after "
        "it has only toggled that strip. Try a different currently visible semantic "
        "affordance, without coordinates. Exhausting the visible tabs of one task "
        "surface is not permission to terminate the whole goal when a visible Back/Close "
        "control can return to a previously observed navigation surface. In that case, "
        "insert recovery outcomes that return to the main navigation and inspect a "
        "currently visible activity/daily entry, 巡察 entry, or explicit 每日/每天 card, "
        "while preserving the original tail. Treat 事务 alone as ambiguous, not forbidden: "
        "accept it only with visible current-cycle evidence. "
        "A limited-time activity is still a daily candidate when its detail body explicitly "
        "states a 每日/每天/今日 entry, reset, attempt, earning, or reward mechanic; do not "
        "dismiss that candidate merely because the activity itself has start and end dates. "
        "Terminate honestly only after no visible alternate navigation surface remains; "
        "never invent a hidden page or loop."
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


def _experience_hints(hints: tuple[Any, ...]) -> str:
    if not hints:
        return "None"
    rendered = []
    for item in hints[:8]:
        if item.kind == "negative":
            instruction = f"avoid {item.semantic_action}"
        elif item.kind == "recovery":
            instruction = f"recovery {item.recovery_action or item.semantic_action}"
        else:
            instruction = f"prefer {item.semantic_action}"
        rendered.append(
            f"{item.candidate_id}:{item.kind}:{instruction}; "
            f"expect={item.expected_next_scene or 'unknown'}; confidence={item.confidence:.2f}"
        )
    return " | ".join(rendered) + (
        " | Current screenshot always outranks experience; re-ground every target."
    )


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
