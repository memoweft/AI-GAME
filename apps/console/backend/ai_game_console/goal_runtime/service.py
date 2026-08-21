from __future__ import annotations

import threading
import re
from collections.abc import Callable
from typing import Any

from .domain import (
    CriterionAssessment,
    GoalCompletionAssessment,
    GoalControlUnsupported,
    GoalRecord,
    GoalSpecificationDraft,
    GoalStateConflict,
)
from .preflight import PreflightResult
from .store import SQLiteGoalStore


class GoalService:
    """Owns GoalRun truth while MobileTask remains a compatibility executor."""

    def __init__(self, store: SQLiteGoalStore, *, mobile_runtime: Any | None,
                 mobile_archive: Any, configured_serial: str | None,
                 target_probe: Callable[[], tuple[bool, str, str]] | None = None,
                 preflight: Callable[[], PreflightResult] | None = None,
                 repair: Callable[[str, PreflightResult], bool] | None = None,
                 specify_goal: Callable[[str], GoalSpecificationDraft] | None = None,
                 verify_completion: Callable[
                     [str, dict[str, Any], Any], GoalCompletionAssessment
                 ] | None = None,
                 promote_verified_success: Callable[[str, str, int], Any] | None = None) -> None:
        self.store = store
        self.mobile_runtime = mobile_runtime
        self.mobile_archive = mobile_archive
        self.configured_serial = configured_serial.strip() if configured_serial else None
        self.target_probe = target_probe
        self.preflight = preflight
        self.repair = repair
        self.specify_goal = specify_goal
        self.verify_completion = verify_completion
        self.promote_verified_success = promote_verified_success
        self._binding_lock = threading.RLock()

    def create(self, goal: str, idempotency_key: str) -> GoalRecord:
        record, _ = self.store.create(goal=goal, idempotency_key=idempotency_key)
        return self._ensure_binding(record)

    def inspect(self, goal_id: str) -> GoalRecord:
        return self._project(self.store.inspect(goal_id))

    def list(self, limit: int) -> list[GoalRecord]:
        return [self._project(item) for item in self.store.list(limit)]

    def send_message(self, goal_id: str, content: str, idempotency_key: str) -> GoalRecord:
        record = self.inspect(goal_id)
        if record.bound_task_id is None or self.mobile_runtime is None:
            raise GoalStateConflict("GoalRun 尚未绑定可接收消息的兼容任务。")
        if record.execution_status in {"FAILED", "CANCELLED", "UNCERTAIN"}:
            raise GoalStateConflict("GoalRun 已终结，不能再追加消息。")
        revision, _ = self.store.add_message(
            goal_id, content=content, idempotency_key=idempotency_key
        )
        # The source runtime owns the second idempotency fence. Reissuing with
        # the same derived key closes a crash window between our durable record
        # and its acknowledgement without duplicating the owner input.
        self.mobile_runtime.send(
            record.bound_task_id,
            content,
            f"goal:{goal_id}:message:{revision}",
        )
        return self.inspect(goal_id)

    def control(self, goal_id: str, action: str, idempotency_key: str) -> GoalRecord:
        if action != "stop":
            raise GoalControlUnsupported(
                f"mobile_task_compat 当前不支持 {action}；仅支持 stop。"
            )
        record = self.inspect(goal_id)
        if record.bound_task_id is None or self.mobile_runtime is None:
            raise GoalStateConflict("GoalRun 当前没有可停止的兼容任务。")
        self.store.record_control(
            goal_id, action=action, idempotency_key=idempotency_key
        )
        state = self.mobile_runtime.stop(
            record.bound_task_id, f"goal:{goal_id}:stop:{idempotency_key}"
        )
        return self._project(self.store.inspect(goal_id), state=state)

    def events(self, goal_id: str, *, after: int, limit: int):
        self.inspect(goal_id)
        return self.store.events(goal_id, after=after, limit=limit)

    def retry_preflight(self, goal_id: str) -> GoalRecord:
        record = self.store.inspect(goal_id)
        if record.bound_task_id is not None:
            return self._project(record)
        return self._ensure_binding(record, retry=True)

    def retry_completion(self, goal_id: str) -> GoalRecord:
        record = self.inspect(goal_id)
        if record.execution_status == "COMPLETED":
            return record
        if record.execution_status != "CANDIDATE_COMPLETE" or record.bound_task_id is None:
            raise GoalStateConflict("GoalRun 当前没有可重试的候选完成证据。")
        if self.verify_completion is None:
            raise GoalStateConflict("独立 Goal Completion Verifier 尚未配置。")
        specification = self.store.specification(goal_id)
        if not specification["success_criteria"]:
            raise GoalStateConflict("GoalRun 尚未冻结成功标准。")
        source = self.mobile_archive.inspect(record.bound_task_id)
        try:
            proposed = self.verify_completion(record.original_goal, specification, source)
            assessment = _validated_completion(specification, source, proposed)
        except Exception:
            assessment = _uncertain_completion(specification)
        stored = self.store.record_completion(
            goal_id,
            specification_revision=int(specification["revision"]),
            source_task_id=record.bound_task_id,
            assessment=assessment,
        )
        self._promote_if_verified(record.bound_task_id, goal_id, stored)
        return self.store.inspect(goal_id)

    def select_target(self, goal_id: str, target_id: str) -> GoalRecord:
        record = self.store.inspect(goal_id)
        if record.bound_task_id is not None:
            if record.target_id == target_id:
                return self._project(record)
            raise GoalStateConflict("GoalRun 已绑定设备，不能静默切换目标。")
        if self.preflight is None:
            raise GoalStateConflict("当前 GoalRun 没有可选择的预检目标。")
        result = self.preflight()
        option = next(
            (item for item in result.target_options if item.get("target_id") == target_id),
            None,
        )
        if option is None:
            raise GoalStateConflict("所选设备已不可用；请重新预检后选择当前设备。")
        self.store.record_preflight(
            goal_id,
            state="READY",
            projection={
                **result.projection(),
                "state": "READY",
                "selected_target_id": target_id,
            },
            waiting_reason=None,
        )
        return self._bind(
            self.store.inspect(goal_id),
            target_id=target_id,
            target_label=str(option.get("connection") or target_id),
        )

    def _ensure_binding(self, record: GoalRecord, *, retry: bool = False) -> GoalRecord:
        if record.bound_task_id is not None:
            return self._project(record)
        if record.binding_state == "FAILED" and not retry:
            return record
        if self.specify_goal is not None:
            specification = self.store.specification(record.id)
            if not specification["success_criteria"]:
                try:
                    draft = self.specify_goal(record.original_goal)
                    _validate_specification(record.original_goal, draft)
                    self.store.record_specification(record.id, draft)
                except Exception:
                    self.store.mark_waiting_configuration(
                        record.id,
                        code="goal_specification_unavailable",
                        message="本地目标模型暂时无法冻结完整成功标准；未启动设备任务。",
                    )
                    return self.store.inspect(record.id)
        if self.preflight is not None:
            self.store.begin_preflight(record.id)
            try:
                result = self.preflight()
            except Exception:
                result = PreflightResult(
                    "WAITING_CONFIGURATION",
                    ({"capability": "environment", "state": "UNKNOWN",
                      "detail": "环境检查暂时不可用。"},),
                    waiting_reason={
                        "code": "environment_probe_unavailable",
                        "message": "环境检查暂时不可用，请稍后重试。",
                    },
                )
            self.store.record_preflight(
                record.id,
                state=result.state,
                projection=result.projection(),
                waiting_reason=result.waiting_reason,
            )
            if result.state != "READY" and self.repair is not None:
                try:
                    repair_attempted = self.repair(record.id, result)
                except Exception:
                    # Repair implementations own their durable failure facts.
                    # An unavailable coordinator must leave the GoalRun in an
                    # honest wait rather than turn environment trouble into a
                    # fabricated binding failure.
                    repair_attempted = False
                if repair_attempted:
                    try:
                        result = self.preflight()
                    except Exception:
                        result = PreflightResult(
                            "WAITING_CONFIGURATION",
                            ({"capability": "environment", "state": "UNKNOWN",
                              "detail": "修复后的环境复检暂时不可用。"},),
                            waiting_reason={
                                "code": "environment_post_repair_probe_unavailable",
                                "message": "环境修复后的检查暂时不可用，请稍后重试。",
                            },
                        )
                    self.store.record_preflight(
                        record.id,
                        state=result.state,
                        projection=result.projection(),
                        waiting_reason=result.waiting_reason,
                    )
            if result.state != "READY":
                return self.store.inspect(record.id)
            if result.selected_target_id is None or result.selected_serial is None:
                self.store.mark_binding_failed(
                    record.id,
                    code="preflight_target_missing",
                    message="预检声称就绪，但没有提供可绑定的 Android 目标。",
                )
                return self.store.inspect(record.id)
            return self._bind(
                self.store.inspect(record.id),
                target_id=result.selected_target_id,
                target_label=result.selected_serial,
            )
        if self.configured_serial is None:
            self.store.mark_waiting_configuration(
                record.id,
                code="default_android_target_not_configured",
                message="尚未配置默认 Android 目标；U1 不会自行猜测设备。",
            )
            return self.store.inspect(record.id)
        if self.mobile_runtime is None:
            self.store.mark_waiting_configuration(
                record.id,
                code="mobile_task_runtime_not_configured",
                message="默认 Android 目标已配置，但兼容执行运行时尚未就绪。",
            )
            return self.store.inspect(record.id)
        if self.target_probe is not None:
            ready, code, message = self.target_probe()
            if not ready:
                self.store.mark_waiting_configuration(
                    record.id, code=code, message=message
                )
                return self.store.inspect(record.id)
        return self._bind(
            self.store.inspect(record.id),
            target_id=None,
            target_label=self.configured_serial,
        )

    def _bind(
        self,
        record: GoalRecord,
        *,
        target_id: str | None,
        target_label: str,
    ) -> GoalRecord:
        with self._binding_lock:
            latest = self.store.inspect(record.id)
            if latest.bound_task_id is not None:
                return self._project(latest)
            try:
                specification = self.store.specification(latest.id)
                state = self.mobile_runtime.start(
                    latest.original_goal,
                    f"goal:{latest.id}:start",
                    target_id=target_id,
                    skill_id=None,
                    execution_origin="v2_goal_compat",
                    promote_success_memory=False,
                    goal_id=latest.id,
                    goal_spec_revision=int(specification["revision"]),
                    frozen_criteria_ids=tuple(
                        str(item["id"])
                        for item in specification.get("success_criteria", ())
                    ),
                )
            except Exception as error:
                code = getattr(error, "code", "mobile_task_binding_failed")
                self.store.mark_binding_failed(
                    latest.id,
                    code=str(code),
                    message="兼容执行任务创建失败；GoalRun 已保留，未声称开始执行。",
                )
                return self.store.inspect(latest.id)
            task_id = str(_value(state, "task_id", _value(state, "id", "")))
            if not task_id:
                self.store.mark_binding_failed(
                    latest.id,
                    code="mobile_task_binding_invalid",
                    message="兼容执行运行时未返回可持久化的任务标识。",
                )
                return self.store.inspect(latest.id)
            self.store.bind_task(
                latest.id,
                task_id,
                target_id or f"adb:{target_label}",
            )
            return self._project(self.store.inspect(latest.id), state=state)

    def _project(self, record: GoalRecord, *, state: Any | None = None) -> GoalRecord:
        if record.bound_task_id is None:
            return record
        try:
            source = state if state is not None else self.mobile_archive.inspect(record.bound_task_id)
        except Exception as error:
            # A durable binding remains truth even when its source archive is
            # temporarily unreadable. Do not turn an observation outage into a
            # fabricated business failure.
            del error
            return record
        projected = self.store.sync_mobile_projection(record.id, source)
        existing_completion = self.store.completion(record.id)
        if projected.execution_status == "COMPLETED" and existing_completion is not None:
            self._promote_if_verified(
                record.bound_task_id, record.id, existing_completion
            )
            return projected
        specification = self.store.specification(record.id)
        if (
            projected.execution_status == "CANDIDATE_COMPLETE"
            and self.specify_goal is not None
            and not specification["success_criteria"]
        ):
            try:
                draft = self.specify_goal(record.original_goal)
                _validate_specification(record.original_goal, draft)
                specification = self.store.record_specification(record.id, draft)
            except Exception:
                return projected
        if (
            projected.execution_status == "CANDIDATE_COMPLETE"
            and self.verify_completion is not None
            and self.store.completion(record.id) is None
        ):
            try:
                proposed = self.verify_completion(record.original_goal, specification, source)
                assessment = _validated_completion(specification, source, proposed)
            except Exception:
                assessment = _uncertain_completion(specification)
            stored = self.store.record_completion(
                record.id,
                specification_revision=int(specification["revision"]),
                source_task_id=str(record.bound_task_id),
                assessment=assessment,
            )
            self._promote_if_verified(record.bound_task_id, record.id, stored)
            projected = self.store.inspect(record.id)
        return projected

    def _promote_if_verified(
        self, task_id: str | None, goal_id: str, completion: dict[str, Any]
    ) -> None:
        if (
            task_id is None
            or completion.get("verdict") != "verified"
            or self.promote_verified_success is None
        ):
            return
        try:
            self.promote_verified_success(
                task_id, goal_id, int(completion["revision"])
            )
        except Exception:
            # Goal completion truth is independent from a learning write. The
            # absence of promotion remains visible in MobileTask state and can
            # be reconciled without downgrading a verified owner result.
            return


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _validated_completion(
    specification: dict[str, Any], state: Any, proposed: GoalCompletionAssessment
) -> GoalCompletionAssessment:
    expected = {
        str(item["id"]): item for item in specification.get("success_criteria", ())
    }
    received = {item.criterion_id: item for item in proposed.criteria}
    if not expected or set(received) != set(expected):
        raise ValueError("completion assessment must cover every frozen criterion exactly once")
    plan = _value(state, "plan")
    subgoals = tuple(_value(plan, "subgoals", ())) if plan is not None else ()
    valid_subgoals = {int(_value(item, "index", -1)) for item in subgoals}
    attempts = {
        int(_value(item, "sequence", -1)): item for item in _value(state, "attempts", ())
    }
    for item in proposed.criteria:
        if not item.satisfied:
            continue
        if not set(item.subgoal_indices).issubset(valid_subgoals):
            raise ValueError("completion assessment references an unknown subgoal")
        for sequence in item.attempt_sequences:
            attempt = attempts.get(sequence)
            verification = _value(attempt, "verification") if attempt is not None else None
            if (
                verification is None
                or not bool(_value(verification, "satisfied", False))
                or bool(_value(verification, "uncertain", False))
            ):
                raise ValueError("completion assessment references unverified attempt evidence")
    all_satisfied = all(item.satisfied for item in proposed.criteria)
    verdict = "verified" if proposed.verdict == "verified" and all_satisfied else (
        "uncertain" if proposed.verdict == "uncertain" else "partial"
    )
    if verdict == "verified" and not proposed.verified_facts:
        raise ValueError("verified goal completion requires at least one verified fact")
    return GoalCompletionAssessment(
        verdict=verdict,
        criteria=proposed.criteria,
        verified_facts=proposed.verified_facts if verdict == "verified" else (),
        result_summary=proposed.result_summary,
    )


def _validate_specification(goal: str, draft: GoalSpecificationDraft) -> None:
    compact_goal = _compact_text(goal)
    quotes = []
    for criterion in draft.success_criteria:
        quote = _compact_text(criterion.source_quote)
        if not quote or quote not in compact_goal:
            raise ValueError("success criterion source quote is not present in original goal")
        quotes.append(quote)
    clauses = []
    for item in re.split(r"[，,。；;！？!?]+|然后|并且|以及", goal):
        clause = re.sub(r"^(?:并|再|最后|接着|同时)", "", item.strip())
        compact = _compact_text(clause)
        if len(compact) >= 2:
            clauses.append(compact)
    for clause in clauses:
        if not any(quote in clause or clause in quote for quote in quotes):
            raise ValueError("goal specification silently omitted an original-goal clause")


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()


def _uncertain_completion(specification: dict[str, Any]) -> GoalCompletionAssessment:
    criteria = tuple(
        CriterionAssessment(
            criterion_id=str(item["id"]),
            satisfied=False,
            subgoal_indices=(),
            attempt_sequences=(),
            evidence="独立完成验证没有产生可接受的证据引用。",
        )
        for item in specification.get("success_criteria", ())
    )
    if not criteria:
        criteria = (
            CriterionAssessment(
                criterion_id="specification_missing",
                satisfied=False,
                subgoal_indices=(),
                attempt_sequences=(),
                evidence="目标成功标准尚未冻结。",
            ),
        )
    return GoalCompletionAssessment(
        verdict="uncertain",
        criteria=criteria,
        verified_facts=(),
        result_summary="兼容计划已结束，但独立完成验证输出无效或不可用。",
    )
