from __future__ import annotations

import threading
import re
from collections.abc import Callable
from typing import Any

from ..application_runtime import ApplicationRuntimeError, Input, Pause, Resume, Stop
from ..experience_runtime import ScopeKey

from .domain import (
    CriterionAssessment,
    GoalCompletionAssessment,
    GoalControlUnsupported,
    GoalRecord,
    GoalSpecificationDraft,
    GoalStateConflict,
)
from .preflight import PreflightResult
from .routing import (
    LANGUAGE_ONLY,
    LOCAL_MANAGED_APPLICATION,
    LONG_LIVED_MOBILE_APPLICATION,
    decide_route,
)
from .store import SQLiteGoalStore
from .stzb_daily import looks_like_daily_checklist_evidence


class GoalService:
    """Own GoalRun truth across compatibility, canary, and active Kernel paths."""

    def __init__(self, store: SQLiteGoalStore, *, mobile_runtime: Any | None,
                 mobile_archive: Any, configured_serial: str | None,
                 kernel_runtime: Any | None = None,
                 kernel_canary_enabled: bool = False,
                 kernel_binding_kind: str | None = None,
                 target_probe: Callable[[], tuple[bool, str, str]] | None = None,
                 preflight: Callable[[], PreflightResult] | None = None,
                 repair: Callable[[str, PreflightResult], bool] | None = None,
                 specify_goal: Callable[[str], GoalSpecificationDraft] | None = None,
                 verify_completion: Callable[
                     [str, dict[str, Any], Any], GoalCompletionAssessment
                 ] | None = None,
                 promote_verified_success: Callable[[str, str, int], Any] | None = None,
                 daily_checklist_store: Any | None = None,
                 inspect_daily_checklist: Callable[[str, tuple[Any, ...]], tuple[Any, ...]]
                 | None = None,
                 application_runtime: Any | None = None,
                 application_archive: Any | None = None,
                 application_profile_id: str | None = None,
                 long_lived_mobile_runtime: Any | None = None,
                 long_lived_mobile_archive: Any | None = None,
                 discover_mobile_application: Callable[[str], str] | None = None,
                 experience: Any | None = None,
                 answer_language: Callable[[str], str] | None = None) -> None:
        self.store = store
        self.mobile_runtime = mobile_runtime
        self.mobile_archive = mobile_archive
        self.kernel_runtime = kernel_runtime
        self.kernel_binding_kind = (
            kernel_binding_kind
            or ("runtime_kernel_canary" if kernel_canary_enabled else None)
        )
        self.configured_serial = configured_serial.strip() if configured_serial else None
        self.target_probe = target_probe
        self.preflight = preflight
        self.repair = repair
        self.specify_goal = specify_goal
        self.verify_completion = verify_completion
        self.promote_verified_success = promote_verified_success
        self.daily_checklist_store = daily_checklist_store
        self.inspect_daily_checklist = inspect_daily_checklist
        self.application_runtime = application_runtime
        self.application_archive = application_archive or application_runtime
        # Compatibility-only constructor argument.  The frozen
        # CapabilityBindingPlan is the sole source of an application profile
        # for new work; a process-global profile must not turn every
        # long-lived GoalRun into the same adapter.
        self.application_profile_id = application_profile_id
        self.long_lived_mobile_runtime = long_lived_mobile_runtime
        self.long_lived_mobile_archive = (
            long_lived_mobile_archive or long_lived_mobile_runtime
        )
        self.discover_mobile_application = discover_mobile_application
        self.experience = experience
        self.answer_language = answer_language
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
        runtime = self._runtime_for(record)
        if record.bound_task_id is None or runtime is None:
            raise GoalStateConflict("GoalRun 尚未绑定可接收消息的执行任务。")
        if record.control_state == "STOP_REQUESTED":
            raise GoalStateConflict("GoalRun 已请求停止，不能再追加消息。")
        if record.execution_status in {"FAILED", "CANCELLED", "UNCERTAIN"}:
            raise GoalStateConflict("GoalRun 已终结，不能再追加消息。")
        revision, _ = self.store.add_message(
            goal_id, content=content, idempotency_key=idempotency_key
        )
        # The source runtime owns the second idempotency fence. Reissuing with
        # the same derived key closes a crash window between our durable record
        # and its acknowledgement without duplicating the owner input.
        if self._is_application_binding(record):
            state = runtime.command(
                record.bound_task_id,
                Input(content),
                f"goal:{goal_id}:message:{revision}",
            )
            return self._project(self.store.inspect(goal_id), state=state)
        runtime.send(record.bound_task_id, content, f"goal:{goal_id}:message:{revision}")
        return self.inspect(goal_id)

    def report_application_outcome(
        self,
        goal_id: str,
        *,
        event_id: str,
        event_type: str,
        evidence_refs: tuple[str, ...],
        attribution_scope: str,
        confidence: float,
    ) -> GoalRecord:
        """Record one evidence-backed delayed result for a live mobile GoalRun."""

        record = self.inspect(goal_id)
        if record.control_state == "STOP_REQUESTED":
            raise GoalStateConflict("GoalRun 已请求停止，不能再记录应用结果。")
        if record.execution_status in {"FAILED", "CANCELLED", "UNCERTAIN", "COMPLETED"}:
            raise GoalStateConflict("GoalRun 已终结，不能再记录应用结果。")
        if (
            record.binding_kind != "long_lived_mobile_composition"
            or record.bound_task_id is None
            or self.long_lived_mobile_runtime is None
        ):
            raise GoalStateConflict("GoalRun 没有可接收延迟结果的长期移动应用绑定。")
        reporter = getattr(
            self.long_lived_mobile_runtime, "report_application_outcome", None
        )
        if not callable(reporter):
            raise GoalStateConflict("长期移动应用绑定尚未提供结果记录入口。")
        try:
            state = reporter(
                record.bound_task_id,
                event_id=event_id,
                event_type=event_type,
                evidence_refs=evidence_refs,
                attribution_scope=attribution_scope,
                confidence=confidence,
            )
        except (ApplicationRuntimeError, ValueError, RuntimeError) as error:
            raise GoalStateConflict("GoalRun 当前状态不允许记录应用结果。") from error
        return self._project(self.store.inspect(goal_id), state=state)

    def control(self, goal_id: str, action: str, idempotency_key: str) -> GoalRecord:
        record = self.inspect(goal_id)
        runtime = self._runtime_for(record)
        supported = (
            {"stop", "pause", "resume", "takeover"}
            if self._is_kernel_binding(record) or self._is_application_binding(record)
            else {"stop"}
        )
        if action not in supported:
            raise GoalControlUnsupported(
                f"{record.binding_kind} 当前不支持 {action}。"
            )
        if action == "stop":
            # [constraint-source: ARCH_INVARIANT; ref: user stop and one-owner fence]
            # Serialize an unbound stop with every binding path.  Otherwise an
            # owner can become ready between the local cancellation and a
            # pending bind, which would resurrect work the user has stopped.
            with self._binding_lock:
                record = self.store.inspect(goal_id)
                runtime = self._runtime_for(record)
                if record.bound_task_id is None:
                    # With no physical binding there is no downstream owner to
                    # reconcile, so cancellation is authoritative and
                    # side-effect free.
                    self.store.record_control(
                        goal_id, action=action, idempotency_key=idempotency_key
                    )
                    return self.store.cancel_unbound(goal_id)
        if record.bound_task_id is None or runtime is None:
            raise GoalStateConflict("GoalRun 当前没有可控制的执行任务。")
        self.store.record_control(
            goal_id, action=action, idempotency_key=idempotency_key
        )
        try:
            if self._is_kernel_binding(record):
                state = runtime.control(record.bound_task_id, action)
            elif self._is_application_binding(record):
                command = {
                    "pause": Pause,
                    "resume": Resume,
                    "stop": Stop,
                    # ApplicationRuntime's Pause is the physical-action fence;
                    # GoalRun retains TAKEOVER as the user-facing control fact.
                    "takeover": Pause,
                }[action]()
                state = runtime.command(
                    record.bound_task_id,
                    command,
                    f"goal:{goal_id}:control:{action}:{idempotency_key}",
                )
                if action == "takeover":
                    self.store.settle_takeover(goal_id)
            else:
                state = runtime.stop(
                    record.bound_task_id, f"goal:{goal_id}:stop:{idempotency_key}"
                )
        except (ValueError, RuntimeError) as error:
            raise GoalStateConflict(
                f"GoalRun 当前状态不允许执行 {action}。"
            ) from error
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
        source = self._archive_for(record).inspect(record.bound_task_id)
        checklist = self._reconcile_daily_checklist(
            record, specification, source
        )
        try:
            proposed = self.verify_completion(record.original_goal, specification, source)
            assessment = _validated_completion(specification, source, proposed)
            assessment = _daily_checklist_gate(specification, assessment, checklist)
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
        with self._binding_lock:
            return self._select_target_locked(goal_id, target_id)

    def _select_target_locked(self, goal_id: str, target_id: str) -> GoalRecord:
        record = self.store.inspect(goal_id)
        if record.execution_status == "CANCELLED":
            return self._project(record)
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
        plan = self.store.binding_plan(goal_id)
        if (
            plan is not None
            and plan.route_kind == LONG_LIVED_MOBILE_APPLICATION
        ):
            if (
                self.long_lived_mobile_runtime is None
                or self.discover_mobile_application is None
            ):
                raise GoalStateConflict("长期移动运行时尚未配置。")
            try:
                application_id = self.discover_mobile_application(target_id).strip()
            except (OSError, RuntimeError, ValueError):
                application_id = ""
            if not application_id:
                self.store.mark_waiting_configuration(
                    goal_id,
                    code="foreground_application_not_ready",
                    message="所选 Android 目标没有可冻结的前台业务应用。",
                )
                return self.store.inspect(goal_id)
            self.store.record_preflight(
                goal_id,
                state="READY",
                projection={
                    **result.projection(),
                    "state": "READY",
                    "selected_target_id": target_id,
                    "selected_application_id": application_id,
                    "facts": [
                        *result.facts,
                        {
                            "capability": "android.foreground_application",
                            "state": "READY",
                            "detail": f"当前前台应用已冻结为 {application_id}。",
                        },
                    ],
                },
                waiting_reason=None,
            )
            return self._bind_long_lived_mobile(
                self.store.inspect(goal_id),
                specification=self.store.specification(goal_id),
                target_id=target_id,
                application_id=application_id,
            )
        return self._bind(
            self.store.inspect(goal_id),
            target_id=target_id,
            target_label=str(option.get("connection") or target_id),
        )

    def _ensure_binding(self, record: GoalRecord, *, retry: bool = False) -> GoalRecord:
        with self._binding_lock:
            return self._ensure_binding_locked(record, retry=retry)

    def _ensure_binding_locked(
        self, record: GoalRecord, *, retry: bool = False
    ) -> GoalRecord:
        record = self.store.inspect(record.id)
        if record.execution_status == "CANCELLED":
            return self._project(record)
        if record.bound_task_id is not None:
            return self._project(record)
        if record.binding_state == "FAILED" and not retry:
            return record
        existing_plan = self.store.binding_plan(record.id)
        if _is_legacy_default_soul_plan(existing_plan):
            # [constraint-source: USER_DECISION; ref: U8 route correction 2026-08-23]
            # Earlier U8 code froze every generic long-lived application goal
            # to Soul. Preserve that immutable ledger, but never auto-start or
            # replay it under the corrected generic mobile route.
            waiting_code = "legacy_default_soul_route_requires_migration"
            if (record.waiting_reason or {}).get("code") != waiting_code:
                self.store.mark_waiting_configuration(
                    record.id,
                    code=waiting_code,
                    message=(
                        "该 GoalRun 已冻结旧版默认 Soul owner 计划；未自动重绑或重放。"
                        "需要显式迁移或创建新的通用长期移动 GoalRun。"
                    ),
                )
            return self.store.inspect(record.id)
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
        specification = self.store.specification(record.id)
        finite_binding_kind = self.kernel_binding_kind or "mobile_task_compat"
        route = decide_route(
            specification,
            finite_binding_kind=finite_binding_kind,
        )
        if route is None:
            # [constraint-source: PRODUCT_SPEC; ref: U8 DESIGN]
            # An unrecognized classification cannot authorize a physical
            # owner. Keep the same GoalRun retryable and side-effect free.
            self.store.mark_waiting_configuration(
                record.id,
                code="goal_route_unavailable",
                message="目标分类没有匹配到可执行能力；未启动任何物理执行器。",
            )
            return self.store.inspect(record.id)
        binding_plan = self.store.record_binding_plan(
            record.id,
            route_kind=route.route_kind,
            binding_kind=route.binding_kind,
            capability_ids=route.capability_ids,
            owner_kind=route.owner_kind,
            profile_id=route.profile_id,
            classification=route.classification,
            rationale=route.rationale,
        )
        if route.route_kind == LONG_LIVED_MOBILE_APPLICATION:
            return self._bind_long_lived_mobile_route(
                self.store.inspect(record.id),
                specification=specification,
            )
        if route.route_kind == LOCAL_MANAGED_APPLICATION:
            return self._bind_application(
                self.store.inspect(record.id),
                specification=specification,
                binding_plan=binding_plan,
            )
        if route.route_kind == LANGUAGE_ONLY:
            return self._bind_language(self.store.inspect(record.id))
        if self.preflight is not None:
            result = self._assess_current_environment(record.id)
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
        selected_runtime = (
            self.kernel_runtime if self.kernel_binding_kind else self.mobile_runtime
        )
        if selected_runtime is None:
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

    def _assess_current_environment(self, goal_id: str) -> PreflightResult:
        if self.preflight is None:
            raise GoalStateConflict("当前 GoalRun 没有可执行的环境预检。")
        self.store.begin_preflight(goal_id)
        try:
            result = self.preflight()
        except Exception:
            result = PreflightResult(
                "WAITING_CONFIGURATION",
                (
                    {
                        "capability": "environment",
                        "state": "UNKNOWN",
                        "detail": "环境检查暂时不可用。",
                    },
                ),
                waiting_reason={
                    "code": "environment_probe_unavailable",
                    "message": "环境检查暂时不可用，请稍后重试。",
                },
            )
        self.store.record_preflight(
            goal_id,
            state=result.state,
            projection=result.projection(),
            waiting_reason=result.waiting_reason,
        )
        if result.state != "READY" and self.repair is not None:
            try:
                repair_attempted = self.repair(goal_id, result)
            except Exception:
                # Repair owns its durable attempt facts.  A coordinator outage
                # leaves the same resumable environment gate in place.
                repair_attempted = False
            if repair_attempted:
                try:
                    result = self.preflight()
                except Exception:
                    result = PreflightResult(
                        "WAITING_CONFIGURATION",
                        (
                            {
                                "capability": "environment",
                                "state": "UNKNOWN",
                                "detail": "修复后的环境复检暂时不可用。",
                            },
                        ),
                        waiting_reason={
                            "code": "environment_post_repair_probe_unavailable",
                            "message": "环境修复后的检查暂时不可用，请稍后重试。",
                        },
                    )
                self.store.record_preflight(
                    goal_id,
                    state=result.state,
                    projection=result.projection(),
                    waiting_reason=result.waiting_reason,
                )
        return result

    def _bind_long_lived_mobile_route(
        self,
        record: GoalRecord,
        *,
        specification: dict[str, Any],
    ) -> GoalRecord:
        if (
            self.long_lived_mobile_runtime is None
            or self.long_lived_mobile_archive is None
            or self.discover_mobile_application is None
        ):
            message = (
                "长期移动目标已冻结通用能力计划，但 ApplicationRuntime 与 "
                "RuntimeKernel 的有界设备周期尚未组合；未启动设备动作。"
            )
            self.store.record_preflight(
                record.id,
                state="WAITING_CONFIGURATION",
                projection={
                    "state": "WAITING_CONFIGURATION",
                    "facts": [
                        {
                            "capability": "long_lived.mobile_application",
                            "state": "WAITING_CONFIGURATION",
                            "detail": message,
                        }
                    ],
                    "target_options": [],
                    "selected_target_id": None,
                },
                waiting_reason={
                    "code": "long_lived_mobile_runtime_not_composed",
                    "message": message,
                },
            )
            return self.store.inspect(record.id)

        if self.preflight is not None:
            result = self._assess_current_environment(record.id)
            if result.state != "READY":
                return self.store.inspect(record.id)
            if result.selected_target_id is None or result.selected_serial is None:
                self.store.mark_binding_failed(
                    record.id,
                    code="preflight_target_missing",
                    message="预检声称就绪，但没有提供可绑定的 Android 目标。",
                )
                return self.store.inspect(record.id)
            target_id = result.selected_target_id
            facts = list(result.facts)
        else:
            if self.configured_serial is None:
                self.store.mark_waiting_configuration(
                    record.id,
                    code="default_android_target_not_configured",
                    message="尚未配置默认 Android 目标；不会自行猜测设备。",
                )
                return self.store.inspect(record.id)
            if self.target_probe is not None:
                ready, code, message = self.target_probe()
                if not ready:
                    self.store.mark_waiting_configuration(
                        record.id, code=code, message=message
                    )
                    return self.store.inspect(record.id)
            target_id = f"adb:{self.configured_serial}"
            facts = []

        try:
            application_id = self.discover_mobile_application(target_id).strip()
        except (OSError, RuntimeError, ValueError):
            application_id = ""
        if not application_id:
            message = "当前 Android 目标没有可冻结的前台业务应用；未启动设备动作。"
            self.store.record_preflight(
                record.id,
                state="WAITING_CONFIGURATION",
                projection={
                    "state": "WAITING_CONFIGURATION",
                    "facts": [
                        *facts,
                        {
                            "capability": "android.foreground_application",
                            "state": "WAITING_CONFIGURATION",
                            "detail": message,
                        },
                    ],
                    "target_options": [],
                    "selected_target_id": target_id,
                },
                waiting_reason={
                    "code": "foreground_application_not_ready",
                    "message": message,
                },
            )
            return self.store.inspect(record.id)

        facts.append(
            {
                "capability": "android.foreground_application",
                "state": "READY",
                "detail": f"当前前台应用已冻结为 {application_id}。",
            }
        )
        self.store.record_preflight(
            record.id,
            state="READY",
            projection={
                "state": "READY",
                "facts": facts,
                "target_options": [],
                "selected_target_id": target_id,
                "selected_application_id": application_id,
            },
            waiting_reason=None,
        )
        return self._bind_long_lived_mobile(
            self.store.inspect(record.id),
            specification=specification,
            target_id=target_id,
            application_id=application_id,
        )

    def _bind_long_lived_mobile(
        self,
        record: GoalRecord,
        *,
        specification: dict[str, Any],
        target_id: str,
        application_id: str,
    ) -> GoalRecord:
        with self._binding_lock:
            latest = self.store.inspect(record.id)
            if latest.execution_status == "CANCELLED":
                return self._project(latest)
            if latest.bound_task_id is not None:
                return self._project(latest)
            runtime = self.long_lived_mobile_runtime
            if runtime is None:
                raise GoalStateConflict("长期移动运行时尚未配置。")
            try:
                state = runtime.prepare(
                    client_request_id=f"goal:{latest.id}:prepare",
                    goal_id=latest.id,
                    target_id=target_id,
                    application_id=application_id,
                    initial_input=latest.original_goal,
                )
            except ApplicationRuntimeError as error:
                self.store.mark_waiting_configuration(
                    latest.id,
                    code=str(getattr(error, "code", "long_lived_mobile_unavailable")),
                    message="长期移动运行时当前不可用；实例未进入物理执行。",
                )
                return self.store.inspect(latest.id)
            instance_id = str(_value(state, "instance_id", ""))
            if not instance_id:
                self.store.mark_binding_failed(
                    latest.id,
                    code="long_lived_mobile_binding_invalid",
                    message="长期移动运行时未返回可持久化的实例标识。",
                )
                return self.store.inspect(latest.id)

            self.store.bind_task(
                latest.id,
                instance_id,
                target_id,
                binding_kind="long_lived_mobile_composition",
            )
            self._begin_long_lived_mobile_experience(
                latest,
                specification=specification,
                instance_id=instance_id,
                target_id=target_id,
                application_id=application_id,
            )
            try:
                state = runtime.activate(instance_id)
            except ApplicationRuntimeError:
                # The GoalRun->instance binding is already durable and the
                # prepared instance is physically fenced.  Normal-launcher
                # recovery retries this exact idempotent activation.
                state = self.long_lived_mobile_archive.inspect(instance_id)
            return self._project(self.store.inspect(latest.id), state=state)

    def _begin_long_lived_mobile_experience(
        self,
        record: GoalRecord,
        *,
        specification: dict[str, Any],
        instance_id: str,
        target_id: str,
        application_id: str,
    ) -> None:
        if self.experience is None:
            return
        try:
            intent = specification.get("normalized_intent") or {}
            self.experience.begin_application_episode(
                goal_run_id=record.id,
                source_instance_id=instance_id,
                goal_spec_revision=int(specification["revision"]),
                frozen_criteria_ids=tuple(
                    str(item["id"])
                    for item in specification.get("success_criteria", ())
                ),
                scope=ScopeKey(
                    user_scope="local-user",
                    # The generic Android route cannot inspect private account
                    # identity.  Goal-local scoping prevents cross-account or
                    # cross-person experience reuse while preserving exact U8
                    # attribution for this authorized run.
                    account_scope=f"goal-run:{record.id}",
                    application_id=application_id,
                    goal_family=str(
                        intent.get("goal_family") or "long-lived/mobile"
                    ),
                    ui_version="unknown",
                    device_class="android",
                    orientation="unknown",
                ),
            )
        except Exception as error:
            self.store.record_experience_unavailable(
                record.id, error_type=type(error).__name__
            )

    def fence_stopped_long_lived_mobile_bindings_before_start(self) -> None:
        runtime = self.long_lived_mobile_runtime
        if runtime is None:
            return
        fence = getattr(runtime, "fence_stop_before_start", None)
        if not callable(fence):
            return
        for record in self.store.list(500):
            if (
                record.binding_kind == "long_lived_mobile_composition"
                and record.bound_task_id is not None
                and record.terminal_at is None
                and record.control_state == "STOP_REQUESTED"
            ):
                fence(
                    record.bound_task_id,
                    f"goal:{record.id}:recovery-stop-fence",
                )

    def recover_long_lived_mobile_bindings(self) -> None:
        runtime = self.long_lived_mobile_runtime
        if runtime is None:
            return
        for record in self.store.list(500):
            if record.binding_kind != "long_lived_mobile_composition":
                continue
            if record.terminal_at is not None:
                continue
            if record.bound_task_id is None:
                environment = self.store.environment(record.id) or {}
                target_id = str(
                    environment.get("selected_target_id") or ""
                ).strip()
                application_id = str(
                    environment.get("selected_application_id") or ""
                ).strip()
                if environment.get("state") != "READY" or not (
                    target_id and application_id
                ):
                    continue
                self._bind_long_lived_mobile(
                    record,
                    specification=self.store.specification(record.id),
                    target_id=target_id,
                    application_id=application_id,
                )
                continue
            if record.control_state in {"STOP_REQUESTED", "TAKEOVER"}:
                self._project(record)
                continue
            try:
                state = runtime.activate(record.bound_task_id)
            except ApplicationRuntimeError:
                continue
            self._project(record, state=state)

    def _bind(
        self,
        record: GoalRecord,
        *,
        target_id: str | None,
        target_label: str,
    ) -> GoalRecord:
        with self._binding_lock:
            latest = self.store.inspect(record.id)
            if latest.execution_status == "CANCELLED":
                return self._project(latest)
            if latest.bound_task_id is not None:
                return self._project(latest)
            try:
                specification = self.store.specification(latest.id)
                start_arguments: dict[str, Any] = {
                    "target_id": target_id,
                    "skill_id": None,
                    "execution_origin": "v2_goal_compat",
                    "promote_success_memory": False,
                    "goal_id": latest.id,
                    "goal_spec_revision": int(specification["revision"]),
                    "frozen_criteria_ids": tuple(
                        str(item["id"])
                        for item in specification.get("success_criteria", ())
                    ),
                }
                family = (specification.get("normalized_intent") or {}).get(
                    "goal_family"
                )
                if family == "stzb/daily/vnext":
                    start_arguments["skill_scope_override"] = family
                    register = getattr(
                        self.daily_checklist_store,
                        "register_multi_surface_goal",
                        None,
                    )
                    if callable(register):
                        register(latest.id)
                runtime = (
                    self.kernel_runtime
                    if self.kernel_binding_kind
                    else self.mobile_runtime
                )
                if runtime is None:
                    raise GoalStateConflict("执行运行时尚未配置。")
                state = runtime.start(
                    latest.original_goal,
                    f"goal:{latest.id}:start",
                    **start_arguments,
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
                binding_kind=(
                    self.kernel_binding_kind
                    if self.kernel_binding_kind
                    else "mobile_task_compat"
                ),
            )
            return self._project(self.store.inspect(latest.id), state=state)

    def _bind_application(
        self,
        record: GoalRecord,
        *,
        specification: dict[str, Any],
        binding_plan: Any,
    ) -> GoalRecord:
        with self._binding_lock:
            latest = self.store.inspect(record.id)
            if latest.execution_status == "CANCELLED":
                return self._project(latest)
            if latest.bound_task_id is not None:
                return self._project(latest)
            if self.application_runtime is None:
                self.store.mark_waiting_configuration(
                    latest.id,
                    code="long_lived_runtime_not_configured",
                    message="长期运行能力尚未配置；未启动受管执行器。",
                )
                return self.store.inspect(latest.id)
            profile_id = str(getattr(binding_plan, "profile_id", "") or "").strip()
            owner_kind = str(getattr(binding_plan, "owner_kind", "") or "").strip()
            owner_binding_ref = getattr(binding_plan, "owner_binding_ref", None)
            if not profile_id:
                self.store.mark_binding_failed(
                    latest.id,
                    code="long_lived_profile_missing",
                    message="长期 GoalRun 缺少冻结的运行能力 profile。",
                )
                return self.store.inspect(latest.id)
            if owner_kind == "external_owner" and owner_binding_ref is None:
                # [constraint-source: ARCH_INVARIANT; ref: docs/product/03_TARGET_ARCHITECTURE.md section 8]
                # An external-effect GoalRun must resolve to this frozen owner
                # binding, not a process-global adapter label.  Local managed
                # GoalRuns intentionally do not need an external owner ref.
                self.store.mark_waiting_configuration(
                    latest.id,
                    code="owner_binding_ref_unavailable",
                    message="长期 GoalRun 缺少冻结的外部 owner 绑定标识；未启动 owner。",
                )
                return self.store.inspect(latest.id)
            target_id = (
                str(owner_binding_ref)
                if owner_kind == "external_owner"
                else "local-managed-runtime"
            )
            selected_capability = (
                f"application_profile.{profile_id}"
                if owner_kind == "external_owner"
                else "local.managed_notification"
            )
            try:
                state = self.application_runtime.start(
                    profile_id,
                    f"goal:{latest.id}:start",
                    target_id=target_id,
                    initial_input=latest.original_goal,
                )
            except ApplicationRuntimeError as error:
                # [constraint-source: PRODUCT_SPEC; ref: U8 resumable capability wait]
                reason = str(getattr(error, "reason", getattr(error, "code", "")))
                external = (
                    owner_kind == "external_owner"
                    and reason in {"owner_not_configured", "owner_unavailable"}
                )
                wait_state = "WAITING_EXTERNAL" if external else "WAITING_CONFIGURATION"
                message = (
                    "长期目标需要的外部 owner 或账号能力当前不可用；可在恢复后重试同一 GoalRun。"
                    if external
                    else "长期运行能力当前未就绪；未产生 GoalRun 执行绑定。"
                )
                self.store.record_preflight(
                    latest.id,
                    state=wait_state,
                    projection={
                        "state": wait_state,
                        "facts": [
                            {
                                "capability": selected_capability
                                if external else "long_lived.wait",
                                "state": wait_state,
                                "detail": message,
                            }
                        ],
                        "target_options": [],
                        "selected_target_id": None,
                    },
                    waiting_reason={
                        "code": reason or "long_lived_runtime_unavailable",
                        "message": message,
                    },
                )
                return self.store.inspect(latest.id)
            instance_id = str(_value(state, "instance_id", ""))
            if not instance_id:
                self.store.mark_binding_failed(
                    latest.id,
                    code="long_lived_binding_invalid",
                    message="长期运行能力未返回可持久化的实例标识。",
                )
                return self.store.inspect(latest.id)
            self.store.record_preflight(
                latest.id,
                state="READY",
                projection={
                    "state": "READY",
                    "facts": [
                        {
                            "capability": "long_lived.wait",
                            "state": "READY",
                            "detail": "ApplicationRuntime 已接收持久化长期实例。",
                        },
                        {
                            "capability": selected_capability,
                            "state": "READY",
                            "detail": (
                                "长期实例已通过外部 owner 能力检查并进入有界周期。"
                                if owner_kind == "external_owner"
                                else "长期实例已由 AI-GAME 本地受管能力接收并进入有界周期。"
                            ),
                        },
                    ],
                    "target_options": [],
                    "selected_target_id": target_id,
                },
                waiting_reason=None,
            )
            self.store.bind_task(
                latest.id,
                instance_id,
                target_id,
                binding_kind="application_runtime",
            )
            if self.experience is not None:
                try:
                    intent = specification.get("normalized_intent") or {}
                    self.experience.begin_application_episode(
                        goal_run_id=latest.id,
                        source_instance_id=instance_id,
                        goal_spec_revision=int(specification["revision"]),
                        frozen_criteria_ids=tuple(
                            str(item["id"])
                            for item in specification.get("success_criteria", ())
                        ),
                        scope=ScopeKey(
                            user_scope="local-user",
                            account_scope="local-default",
                            application_id=(
                                profile_id
                                if owner_kind == "external_owner"
                                else "local-managed"
                            ),
                            goal_family=str(
                                intent.get("goal_family")
                                or (
                                    "long-lived/application"
                                    if owner_kind == "external_owner"
                                    else "long-lived/local"
                                )
                            ),
                            ui_version="unknown",
                            device_class=(
                                "external-owner"
                                if owner_kind == "external_owner"
                                else "local-runtime"
                            ),
                            orientation="unknown",
                        ),
                    )
                except Exception as error:
                    # [constraint-source: ARCH_INVARIANT; ref: evidence truth separation]
                    # Execution truth remains owned by ApplicationRuntime. A
                    # failed additive learning write is recorded separately and
                    # must not fabricate a runtime failure.
                    self.store.record_experience_unavailable(
                        latest.id, error_type=type(error).__name__
                    )
            return self._project(self.store.inspect(latest.id), state=state)

    def _bind_language(self, record: GoalRecord) -> GoalRecord:
        with self._binding_lock:
            latest = self.store.inspect(record.id)
            if latest.execution_status == "CANCELLED":
                return self._project(latest)
            if latest.bound_task_id is not None:
                return latest
            if self.answer_language is None:
                self.store.mark_waiting_configuration(
                    latest.id,
                    code="local_language_capability_not_configured",
                    message="本地语言结果能力尚未配置。",
                )
                return self.store.inspect(latest.id)
            try:
                result = self.answer_language(latest.original_goal)
            except Exception:
                # [constraint-source: PRODUCT_SPEC; ref: U8 honest resumable capability wait]
                self.store.mark_waiting_configuration(
                    latest.id,
                    code="local_language_capability_unavailable",
                    message="本地语言结果能力暂时不可用；可重试同一 GoalRun。",
                )
                return self.store.inspect(latest.id)
            self.store.bind_task(
                latest.id,
                f"language:{latest.id}",
                "local-language",
                binding_kind="local_language",
            )
            self.store.record_preflight(
                latest.id,
                state="READY",
                projection={
                    "state": "READY",
                    "facts": [
                        {
                            "capability": "local.text_reasoning",
                            "state": "READY",
                            "detail": "本地语言能力已返回结构化结果。",
                        }
                    ],
                    "target_options": [],
                    "selected_target_id": "local-language",
                },
                waiting_reason=None,
            )
            return self.store.complete_language_result(
                latest.id, result_summary=str(result)
            )

    def _project(self, record: GoalRecord, *, state: Any | None = None) -> GoalRecord:
        if record.bound_task_id is None:
            return record
        if record.binding_kind == "local_language":
            return record
        try:
            source = state if state is not None else self._archive_for(record).inspect(record.bound_task_id)
        except Exception as error:
            # A durable binding remains truth even when its source archive is
            # temporarily unreadable. Do not turn an observation outage into a
            # fabricated business failure.
            del error
            return record
        if self._is_application_binding(record):
            self._record_delayed_application_outcomes(record, source)
            return self.store.sync_application_projection(record.id, source)
        projected = self.store.sync_mobile_projection(record.id, source)
        existing_completion = self.store.completion(record.id)
        if projected.execution_status == "COMPLETED" and existing_completion is not None:
            self._promote_if_verified(
                record.bound_task_id, record.id, existing_completion
            )
            return projected
        specification = self.store.specification(record.id)
        checklist = self.daily_checklist(record.id)
        if projected.execution_status == "CANDIDATE_COMPLETE":
            # Checklist extraction is a completion-gate operation, not a read
            # projection side effect. Failed, stopped, and uncertain runs must
            # remain cheap to inspect and must not silently retry a visual
            # model request every time list/detail is read.
            checklist = self._reconcile_daily_checklist(
                record, specification, source
            )
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
                assessment = _daily_checklist_gate(specification, assessment, checklist)
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

    def _runtime_for(self, record: GoalRecord) -> Any | None:
        if record.binding_kind == "long_lived_mobile_composition":
            return self.long_lived_mobile_runtime
        if self._is_application_binding(record):
            return self.application_runtime
        return (
            self.kernel_runtime
            if self._is_kernel_binding(record)
            else self.mobile_runtime
        )

    def _archive_for(self, record: GoalRecord) -> Any:
        if record.binding_kind == "long_lived_mobile_composition":
            return self.long_lived_mobile_archive
        if self._is_application_binding(record):
            return self.application_archive
        return (
            self.kernel_runtime
            if self._is_kernel_binding(record)
            else self.mobile_archive
        )

    @staticmethod
    def _is_kernel_binding(record: GoalRecord) -> bool:
        return record.binding_kind in {"runtime_kernel", "runtime_kernel_canary"}

    @staticmethod
    def _is_application_binding(record: GoalRecord) -> bool:
        return record.binding_kind in {
            "application_runtime",
            "long_lived_mobile_composition",
        }

    def _record_delayed_application_outcomes(
        self, record: GoalRecord, source: Any
    ) -> None:
        if self.experience is None or record.bound_task_id is None:
            return
        cursor = self.store.source_event_cursor(record.id)
        allowed = {
            "delayed_positive",
            "delayed_negative",
            "no_response",
            "user_approval",
            "user_rejection",
        }
        for event in _value(source, "events", ()):
            sequence = int(_value(event, "sequence", 0))
            kind = str(_value(event, "event_type", ""))
            if sequence <= cursor or kind not in allowed:
                continue
            data = _value(event, "data", {})
            if not isinstance(data, dict):
                continue
            attribution_scope = str(data.get("attribution_scope") or "").strip()
            evidence_refs = tuple(
                str(item)
                for item in data.get("evidence_refs", ())
                if isinstance(item, str) and item.strip()
            )
            try:
                self.experience.record_delayed_outcome(
                    source_instance_id=record.bound_task_id,
                    source_event_key=str(sequence),
                    kind=kind,
                    source="application_runtime",
                    attribution_scope=attribution_scope,
                    evidence_refs=evidence_refs,
                    confidence=float(data.get("confidence", 1.0)),
                )
            except (TypeError, ValueError):
                # [constraint-source: ARCH_INVARIANT; ref: attributable experience scope]
                # The source event still projects as evidence; malformed
                # attribution cannot contaminate reusable experience.
                continue

    def daily_checklist(self, goal_id: str) -> dict[str, Any] | None:
        if self.daily_checklist_store is None:
            return None
        specification = self.store.specification(goal_id)
        if (specification.get("normalized_intent") or {}).get("goal_family") != "stzb/daily/vnext":
            return None
        return self.daily_checklist_store.state(goal_id)

    def _reconcile_daily_checklist(
        self, record: GoalRecord, specification: dict[str, Any], source: Any
    ) -> dict[str, Any] | None:
        intent = specification.get("normalized_intent") or {}
        if intent.get("goal_family") != "stzb/daily/vnext":
            return None
        if self.daily_checklist_store is None or self.inspect_daily_checklist is None:
            return {"state": "NOT_AVAILABLE", "final_verified": False,
                    "remaining_items": [], "deviation": "checklist service unavailable"}
        source_status = str(_value(source, "status", ""))
        if source_status not in {"completed", "failed", "stopped", "uncertain"}:
            # The inspected deployment has one resident Qwen slot. Checklist
            # extraction must never compete with the physical owner's next
            # action or post-action verification request.
            return self.daily_checklist_store.state(record.id)
        scanned = self.daily_checklist_store.evidence_ids(record.id)
        observations = []
        non_candidates = []
        for attempt in _value(source, "attempts", ()):
            after = _value(attempt, "after")
            evidence_id = str(_value(after, "evidence_id", "")) if after is not None else ""
            if not evidence_id or evidence_id in scanned:
                continue
            verification = _value(attempt, "verification")
            evidence = str(_value(verification, "evidence", ""))
            if _looks_like_daily_checklist_evidence(evidence):
                observations.append(after)
            else:
                non_candidates.append(evidence_id)
        if non_candidates:
            self.daily_checklist_store.mark_scanned(record.id, tuple(non_candidates))
        for observation in observations:
            batch = (observation,)
            try:
                snapshots = self.inspect_daily_checklist(record.original_goal, batch)
            except Exception:
                break
            for snapshot in snapshots:
                self.daily_checklist_store.record(record.id, snapshot)
            self.daily_checklist_store.mark_scanned(
                record.id, tuple(str(_value(item, "evidence_id", "")) for item in batch)
            )
        return self.daily_checklist_store.state(record.id)

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


def _is_legacy_default_soul_plan(plan: Any | None) -> bool:
    """Recognize the exact generic-to-Soul plan shape emitted by the withdrawn route."""

    if plan is None:
        return False
    return (
        str(getattr(plan, "route_kind", "")) == "long_lived_application"
        and str(getattr(plan, "binding_kind", "")) == "application_runtime"
        and str(getattr(plan, "owner_kind", "")) == "external_owner"
        and str(getattr(plan, "profile_id", "")) == "soul-reply-v1"
        and str(getattr(plan, "classification", ""))
        in {
            "long_lived_application_goal",
            "long_lived_goal",
            "waiting_driven_application_goal",
        }
    )


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


def _daily_checklist_gate(
    specification: dict[str, Any], proposed: GoalCompletionAssessment,
    checklist: dict[str, Any] | None,
) -> GoalCompletionAssessment:
    intent = specification.get("normalized_intent") or {}
    if intent.get("goal_family") != "stzb/daily/vnext":
        return proposed
    discovery_only = intent.get("execution_policy") == "discovery_only"
    if (
        checklist is not None
        and (
            checklist.get("final_verified") is True
            or discovery_only
            and checklist.get("state") in {"FROZEN", "VERIFIED_COMPLETE"}
        )
    ):
        return proposed
    if checklist is None or checklist.get("state") in {"NOT_AVAILABLE", "NOT_DISCOVERED"}:
        evidence = "没有持久化覆盖完整的今日每日任务清单及独立最终复查画面。"
    elif checklist.get("deviation"):
        evidence = f"最终清单与冻结清单不一致：{checklist['deviation']}。"
    else:
        remaining = [
            str(item.get("title") or item.get("item_id"))
            for item in checklist.get("remaining_items", ())
        ]
        evidence = "最终清单仍有未完成或不确定项目：" + (
            "、".join(remaining) if remaining else "缺少不同证据画面的完整最终复查"
        )
    gated = []
    matched = False
    for item in proposed.criteria:
        criterion = next(
            (raw for raw in specification.get("success_criteria", ())
             if str(raw.get("id")) == item.criterion_id),
            {},
        )
        text = " ".join(
            str(criterion.get(key) or "")
            for key in ("description", "source_quote", "evidence_requirement")
        )
        if any(token in text for token in ("每日", "日常", "任务", "清单", "today", "daily")):
            gated.append(CriterionAssessment(item.criterion_id, False, (), (), evidence))
            matched = True
        else:
            gated.append(item)
    if not matched and gated:
        item = gated[0]
        gated[0] = CriterionAssessment(item.criterion_id, False, (), (), evidence)
    return GoalCompletionAssessment(
        verdict="partial",
        criteria=tuple(gated),
        verified_facts=(),
        result_summary=evidence,
    )


def _looks_like_daily_checklist_evidence(evidence: str) -> bool:
    return looks_like_daily_checklist_evidence(evidence)


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
