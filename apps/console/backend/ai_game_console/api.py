from __future__ import annotations

import hmac
import logging
import os
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adb_executor import AdbGuiExecutor
from .android_chat_adapter import RepositoryAndroidAutomationFactory
from .application_runtime import (
    Input as ApplicationInput,
    Pause as ApplicationPause,
    Resume as ApplicationResume,
    Stop as ApplicationStop,
)
from .application_runtime_catalog import ApplicationRuntimeCatalog
from .application_runtime.domain import (
    ApplicationRuntimeError,
    IdempotencyConflict as ApplicationIdempotencyConflict,
    QueueFull as ApplicationQueueFull,
    RuntimeClosed as ApplicationRuntimeClosed,
    RuntimeNotFound as ApplicationRuntimeNotFound,
)
from .agent_runtime.api import (
    agent_runtime_error_handler,
    create_agent_session_router,
)
from .agent_runtime.event_router import DeviceBodyEventInbox
from .agent_runtime.event_pump import AgentRuntimeEventPump
from .agent_runtime.fact_questions import FactQuestionCoordinator
from .agent_runtime.activity_integration import (
    AgentRuntimeActivitySlicePreemptionCoordinator,
    NormalActivitySliceRunner,
)
from .agent_runtime.activity_store import ActivitySliceStore
from .agent_runtime.domain import AgentRuntimeError
from .agent_runtime.planner import DeterministicSessionPlanner, FallbackSessionPlanner
from .agent_runtime.scheduler import AttentionScheduler, SQLiteSchedulerCoordination
from .agent_runtime.service import AgentSessionService
from .agent_runtime.store import SQLiteAgentRuntimeStore
from .user_fact_runtime import SQLiteUserFactStore, UserFactService
from .user_fact_runtime.api import create_user_fact_router
from .chat import AndroidAutomationFactory, ChatCoordinator, ChatCoordinatorError
from .cloud_config import CloudChatConfiguration, CloudConfigError
from .config import Settings
from .discovery import AdbTargetDiscovery
from .device_body.adb_adapter import AdbDeviceBodyAdapter
from .device_body.domain import ConnectionState, DeviceBodyBinding
from .device_body.kernel_bridge import KernelDeviceBodyBridge
from .device_body.store import SQLiteDeviceBodyStore
from .device_lease import DeviceExecutionLease
from .execution import GuiExecutor
from .experience_runtime import ExperienceService, SQLiteExperienceStore
from .gui_owl_client import OpenAICompatibleGuiOwlClient
from .game_learning import (
    GameLearner,
    GameLearningError,
    LocalArtifactStore,
    SQLiteLearningStore,
)
from .game_learning.android_adapter import StzbAndroidEnvironmentFactory
from .game_learning.profiles import stzb_game_profile
from .game_learning.verifier import OpenAICompatibleStzbEvidenceAssessor
from .gateway_api import (
    GatewayComposition,
    build_gateway_composition,
    create_gateway_router,
    gateway_error_handler,
)
from .gateway import GatewayError
from .kernel_canary import KernelCanaryCoordinator
from .goal_runtime import (
    GoalPreflight,
    GoalRepairManager,
    GoalService,
    ProductionGoalRepairs,
    SQLiteGoalStore,
    SQLiteDailyChecklistStore,
    StzbDailyProgressController,
    StructuredGoalModel,
    StructuredSessionPlanner,
    build_qwen_attention_scheduler,
    create_goal_router,
    goal_error_handler,
)
from .goal_runtime.domain import GoalError
from .mobile_agent import (
    IdempotencyConflict,
    MobileTaskArchive,
    MobileTaskError,
    MobileTaskRuntime,
    TaskNotFound,
    TaskQueueFull,
    TaskStateConflict,
)
from .mobile_task_adapter import (
    LocalMobileEvidenceStore,
    MobileTaskAdapterError,
    MobileTaskAndroidDriver,
    OpenAICompatibleMobileRoleModel,
    OpenAICompatibleToolRoleModel,
    _is_stzb_execution_stage,
)
from .mobile_task_profiles import resolve_mobile_skill_scope
from .openai_chat import OpenAIChatProvider
from .repository import SQLiteRepository
from .legacy_cutover import append_mode_journal
from .runtime_mode import RuntimeModeError, RuntimeModeGuard, validate_runtime_mode
from .runtime_adapters.adb_executor import GuiExecutorActionAdapter
from .runtime_adapters.android import AndroidObservationProvider
from .runtime_adapters.artifacts import FilesystemArtifactStore
from .execution_contract import (
    ExecutionContractError,
    ExecutionContractService,
    V2ExecutionContractService,
    SQLiteExecutionContractStore,
    create_execution_router,
    create_execution_v2_router,
    execution_contract_error_handler,
)
from .execution_v2_composition import (
    CanonicalTaskPortAdapter,
    ExperiencePortAdapter,
)
from .emulator_runtime.production import compose_production_emulator_runtime
from .emulator_runtime.task_runner import ResidentV2TaskScheduler
from .runtime_adapters.sqlite import SQLiteRuntimeStore
from .runtime_kernel import RuntimeKernel
from .runtime_admin import LeaseAdminService, create_lease_admin_router
from .local_managed_application_composition import (
    PROFILE_ID as LOCAL_MANAGED_PROFILE_ID,
    compose_local_managed_application_runtime,
)
from .long_lived_mobile_application_composition import (
    compose_long_lived_mobile_application_runtime,
)
from .schemas import (
    ApplicationCommandCreate,
    ApplicationInstanceCreate,
    ApplicationInstanceListResponse,
    ApplicationInstanceSchema,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalListResponse,
    ChatSessionCreate,
    ChatSessionListResponse,
    ChatSessionSchema,
    ChatTranscriptResponse,
    ChatTurnCreate,
    ChatTurnSchema,
    CloudChatConfigResponse,
    CloudChatConfigRevision,
    CloudChatConfigUpdate,
    CloudConnectionTestResponse,
    EventListResponse,
    ExecutorActionRequest,
    ExecutorActionResponse,
    HealthResponse,
    LearningJobCreate,
    LearningJobListResponse,
    LearningJobSchema,
    LearningProfileListResponse,
    MobileTaskCreate,
    MobileTaskInputCreate,
    MobileTaskListResponse,
    MobileTaskSchema,
    MobileTaskStopRequest,
    OverviewResponse,
    RunActionRequest,
    RunCreate,
    RunDetailSchema,
    RunListResponse,
    RunSummarySchema,
    RuntimeModeResponse,
    RuntimeResponse,
    TargetDiscoveryResponse,
    TargetListResponse,
    WorkflowListResponse,
)
from .service import ControlPlaneError, ControlPlaneService


logger = logging.getLogger(__name__)
_AUTO_LONG_TASK_SCHEDULER = object()

WRITE_CLIENT_HEADER = "console-v1"
CONSOLE_SHUTDOWN_TOKEN_HEADER = "X-AI-Game-Shutdown-Token"


class _DeferredBodyEventInbox:
    """Bind the durable BodyEvent intake only after normal composition finishes.

    ``RuntimeKernel`` does not execute an action while it is being composed,
    but keeping this narrow proxy makes that ordering explicit: a BodyEvent
    can neither be dropped nor routed through an unfinished AgentSession
    service.
    """

    def __init__(self) -> None:
        self._target: DeviceBodyEventInbox | None = None

    def bind(self, target: DeviceBodyEventInbox) -> None:
        if self._target is not None and self._target is not target:
            raise RuntimeError("DeviceBody EventInbox is already bound")
        self._target = target

    def ingest(self, event: Any) -> Any:
        if self._target is None:
            raise RuntimeError("DeviceBody EventInbox is unavailable during composition")
        return self._target.ingest(event)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _UnavailableLearningEnvironmentFactory:
    """Fail-closed production Adapter when Android learning dependencies are absent."""

    def open(self, *, profile: Any, target_id: str | None, is_cancelled: Any) -> Any:
        del profile, target_id, is_cancelled
        raise GameLearningError(
            code="learning_environment_not_configured",
            public_message="Android 学习环境或本地视觉模型尚未配置。",
            status_code=409,
        )


def _learning_value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _learning_profile_payload(profile: Any) -> dict[str, Any]:
    profile_id = str(
        _learning_value(profile, "profile_id", _learning_value(profile, "id", ""))
    )
    max_transitions = int(
        _learning_value(
            profile,
            "max_actions",
            _learning_value(profile, "max_transitions", 0),
        )
    )
    max_duration_seconds = float(
        _learning_value(profile, "max_duration_seconds", 0.0)
    )
    if profile_id == "stzb-tutorial-v1":
        game = "率土之滨"
        scope_summary = "固定测试环境中的低频教程与只读菜单导航。"
        safety_summary = (
            "登录、协议、实名、验证码、支付、购买、招募、领取、聊天、联盟、"
            "匹配、真人交互和账号设置均不在允许范围内。"
        )
    else:
        game = str(_learning_value(profile, "game", _learning_value(profile, "name", "")))
        scope_summary = str(_learning_value(profile, "scope_summary", "受 GameProfile 限定。"))
        safety_summary = str(
            _learning_value(profile, "safety_summary", "只执行 GameProfile 明确允许的动作。")
        )
    return {
        "id": profile_id,
        "name": str(_learning_value(profile, "name", "")),
        "game": game,
        "scope_summary": scope_summary,
        "safety_summary": safety_summary,
        "budget_summary": (
            f"每个 LearningEpisode 最多 {max_transitions} 个 Transition，"
            f"最长 {max_duration_seconds:g} 秒。"
        ),
        "revision": int(_learning_value(profile, "revision", 1)),
        "allowed_actions": list(_learning_value(profile, "allowed_actions", ())),
        "max_transitions": max_transitions,
        "max_duration_seconds": max_duration_seconds,
        "default_target_id": _learning_value(profile, "default_target_id"),
    }


def _learning_job_payload(job: Any, profiles: list[Any]) -> dict[str, Any]:
    status = str(_learning_value(job, "status", "failed"))
    profile_id = str(_learning_value(job, "profile_id", ""))
    profile = next(
        (
            item
            for item in profiles
            if str(_learning_value(item, "profile_id", _learning_value(item, "id", "")))
            == profile_id
        ),
        None,
    )
    profile_revision = int(
        _learning_value(job, "profile_revision", _learning_value(profile, "revision", 1))
    )
    max_transitions = int(
        _learning_value(profile, "max_actions", _learning_value(profile, "max_transitions", 0))
    )
    state = {
        "queued": ("accepted", "pending", "neutral", "unknown", "unchanged"),
        "running": ("collecting", "pending", "active", "unknown", "unchanged"),
        "stopping": ("stopping", "pending", "neutralizing", "unknown", "unchanged"),
        "learned": ("terminal", "learned", "neutral", "confirmed_success", "promoted"),
        "not_learned": ("terminal", "not_learned", "neutral", "unconfirmed", "unchanged"),
        "failed": ("terminal", "failed", "neutral", "unconfirmed", "unchanged"),
        "stopped": ("terminal", "stopped", "neutral", "unknown", "unchanged"),
        "stopped_uncertain": (
            "terminal",
            "stopped_uncertain",
            "uncertain",
            "unknown",
            "unchanged",
        ),
    }.get(status, ("terminal", "failed", "uncertain", "unconfirmed", "unchanged"))
    phase, result, control_state, fallback_outcome, fallback_policy_state = state
    outcome = str(_learning_value(job, "outcome", fallback_outcome))
    policy_state = str(_learning_value(job, "policy_state", fallback_policy_state))
    policy_version = int(
        _learning_value(
            job,
            "policy_version",
            _learning_value(job, "policy_memory_revision", 0),
        )
    )
    return {
        "id": str(_learning_value(job, "job_id", _learning_value(job, "id", ""))),
        "client_request_id": str(_learning_value(job, "client_request_id", "")),
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "target_id": _learning_value(job, "target_id"),
        "phase": phase,
        "result": result,
        "outcome": outcome,
        "control_state": control_state,
        "policy_state": policy_state,
        "transition_count": int(_learning_value(job, "transition_count", 0)),
        "max_transitions": max_transitions,
        "total_reward": _learning_value(job, "total_reward"),
        "verified_successes": _learning_value(job, "verified_successes"),
        "policy_memory_revision": policy_version,
        "policy_memory_count": _learning_value(job, "policy_memory_count"),
        "cancel_requested": bool(_learning_value(job, "cancel_requested", False)),
        "detail": _learning_value(job, "detail"),
        "error_code": _learning_value(job, "error_code"),
        "created_at": str(_learning_value(job, "created_at", "")),
        "started_at": _learning_value(job, "started_at"),
        "finished_at": _learning_value(job, "finished_at"),
        "updated_at": str(
            _learning_value(job, "updated_at", _learning_value(job, "created_at", ""))
        ),
    }


def _mobile_task_value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _mobile_task_payload(state: Any) -> dict[str, Any]:
    plan = _mobile_task_value(state, "plan")
    plan_payload: dict[str, Any] | None = None
    if plan is not None:
        plan_payload = {
            "revision": int(_mobile_task_value(plan, "revision", 0)),
            "subgoals": [
                {
                    "index": int(_mobile_task_value(item, "index", 0)),
                    "description": str(_mobile_task_value(item, "description", "")),
                    "status": str(_mobile_task_value(item, "status", "pending")),
                }
                for item in _mobile_task_value(plan, "subgoals", ())
            ],
        }

    attempts: list[dict[str, Any]] = []
    for attempt in _mobile_task_value(state, "attempts", ()):
        decision = _mobile_task_value(attempt, "decision")
        intent = _mobile_task_value(decision, "intent") if decision is not None else None
        decision_kind = str(_mobile_task_value(decision, "kind", "unknown"))
        action_type = (
            str(_mobile_task_value(intent, "name", "unknown"))
            if intent is not None
            else decision_kind
        )
        transport = _mobile_task_value(attempt, "transport")
        verification = _mobile_task_value(attempt, "verification")
        verification_payload = None
        if verification is not None:
            verification_payload = {
                "satisfied": bool(_mobile_task_value(verification, "satisfied", False)),
                "progress": bool(_mobile_task_value(verification, "progress", False)),
                "uncertain": bool(_mobile_task_value(verification, "uncertain", False)),
                "evidence": str(_mobile_task_value(verification, "evidence", "")),
            }
        attempts.append(
            {
                "id": str(_mobile_task_value(attempt, "attempt_id", "")),
                "sequence": int(_mobile_task_value(attempt, "sequence", 0)),
                "subgoal_index": int(_mobile_task_value(attempt, "subgoal_index", 0)),
                # Deliberately omit PhysicalIntent.arguments. It may contain
                # typed content and is not needed to understand task progress.
                "action_type": action_type,
                "transport_status": (
                    str(_mobile_task_value(transport, "status", "not_sent"))
                    if transport is not None
                    else None
                ),
                "verification": verification_payload,
                "created_at": str(_mobile_task_value(attempt, "created_at", "")),
                "finalized_at": _mobile_task_value(attempt, "finalized_at"),
            }
        )

    return {
        "id": str(_mobile_task_value(state, "task_id", _mobile_task_value(state, "id", ""))),
        "goal": str(_mobile_task_value(state, "goal", "")),
        "target_id": _mobile_task_value(state, "target_id"),
        "skill_id": _mobile_task_value(state, "skill_id"),
        "status": str(_mobile_task_value(state, "status", "failed")),
        "input_revision": int(_mobile_task_value(state, "input_revision", 0)),
        "plan": plan_payload,
        "active_subgoal_index": int(_mobile_task_value(state, "active_subgoal_index", 0)),
        "strategy": str(_mobile_task_value(state, "strategy", "")),
        "no_progress_count": int(_mobile_task_value(state, "no_progress_count", 0)),
        "reflection_count": int(_mobile_task_value(state, "reflection_count", 0)),
        "attempt_count": int(_mobile_task_value(state, "attempt_count", len(attempts))),
        "cancel_requested": bool(_mobile_task_value(state, "cancel_requested", False)),
        "verification_satisfied": bool(
            _mobile_task_value(state, "verification_satisfied", False)
        ),
        "detail": _mobile_task_value(state, "detail"),
        "error_code": _mobile_task_value(state, "error_code"),
        "skill_memory_version": int(_mobile_task_value(state, "skill_memory_version", 0)),
        "inputs": [
            {
                "revision": int(_mobile_task_value(item, "revision", 0)),
                "content": str(_mobile_task_value(item, "content", "")),
                "lifecycle": str(_mobile_task_value(item, "lifecycle", "accepted")),
                "client_request_id": str(
                    _mobile_task_value(item, "client_request_id", "")
                ),
                "created_at": str(_mobile_task_value(item, "created_at", "")),
                "applied_at": _mobile_task_value(item, "applied_at"),
            }
            for item in _mobile_task_value(state, "inputs", ())
        ],
        "attempts": attempts,
        "reflections": [
            {
                "sequence": int(_mobile_task_value(item, "sequence", 0)),
                "previous_strategy": str(
                    _mobile_task_value(item, "previous_strategy", "")
                ),
                "strategy": str(_mobile_task_value(item, "strategy", "")),
                "reason": str(_mobile_task_value(item, "reason", "")),
                "consecutive_no_progress": int(
                    _mobile_task_value(item, "consecutive_no_progress", 0)
                ),
                "created_at": str(_mobile_task_value(item, "created_at", "")),
            }
            for item in _mobile_task_value(state, "reflections", ())
        ],
        "events": [
            {
                "sequence": int(_mobile_task_value(item, "sequence", 0)),
                "event_type": str(_mobile_task_value(item, "event_type", "")),
                "created_at": str(_mobile_task_value(item, "created_at", "")),
            }
            for item in _mobile_task_value(state, "events", ())
        ],
        "created_at": str(_mobile_task_value(state, "created_at", "")),
        "updated_at": str(_mobile_task_value(state, "updated_at", "")),
        "finished_at": _mobile_task_value(state, "finished_at"),
    }


def _application_value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _safe_application_token(
    value: Any,
    *,
    fallback: str,
    maximum: int = 256,
) -> str:
    """Project one opaque control token without forwarding arbitrary payload."""

    candidate = str(value or "").strip()
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-/"
    )
    if (
        not candidate
        or len(candidate) > maximum
        or any(character not in allowed for character in candidate)
    ):
        return fallback
    return candidate


def _application_instance_payload(state: Any) -> dict[str, Any]:
    """Return runtime control facts while omitting every application payload.

    The durable runtime may contain user input, observations, intent arguments,
    owner reservations/receipts, verification evidence and event data.  None
    of those values cross this HTTP projection.  This allow-list deliberately
    exposes only lifecycle facts required by the console.
    """

    raw_status = str(_application_value(state, "status", "failed"))
    status = (
        raw_status
        if raw_status
        in {
            "queued",
            "running",
            "waiting",
            "paused",
            "stopping",
            "stopped",
            "completed",
            "failed",
        }
        else "failed"
    )
    raw_intents = tuple(_application_value(state, "intents", ()) or ())
    raw_outcomes = tuple(_application_value(state, "outcomes", ()) or ())
    raw_inputs = tuple(_application_value(state, "inputs", ()) or ())
    raw_events = tuple(_application_value(state, "events", ()) or ())

    intents: list[dict[str, Any]] = []
    for item in raw_intents:
        cycle = int(_application_value(item, "cycle", 0))
        intent = _application_value(item, "intent")
        intents.append(
            {
                "id": _safe_application_token(
                    _application_value(item, "intent_id", ""),
                    fallback=f"intent-{cycle}",
                ),
                "cycle": cycle,
                "revision": int(_application_value(item, "revision", 0)),
                "phase": _safe_application_token(
                    _application_value(item, "phase", "unknown"),
                    fallback="unknown",
                    maximum=64,
                ),
                "hard_risk": bool(
                    _application_value(intent, "hard_risk", False)
                ),
                "created_at": str(
                    _application_value(item, "created_at", "")
                ),
                "finalized_at": _application_value(item, "finalized_at"),
            }
        )

    outcomes: list[dict[str, Any]] = []
    allowed_outcomes = {
        "confirmed_success",
        "confirmed_failure",
        "unconfirmed",
        "uncertain",
    }
    for item in raw_outcomes:
        outcome_status = str(_application_value(item, "status", "unconfirmed"))
        outcomes.append(
            {
                "cycle": int(_application_value(item, "cycle", 0)),
                "status": (
                    outcome_status
                    if outcome_status in allowed_outcomes
                    else "unconfirmed"
                ),
                "hard_risk": bool(
                    _application_value(item, "hard_risk", False)
                ),
                "terminal": bool(_application_value(item, "terminal", True)),
                "created_at": str(
                    _application_value(item, "created_at", "")
                ),
            }
        )

    raw_error_code = _application_value(state, "error_code")
    error_code = (
        _safe_application_token(
            raw_error_code,
            fallback="application_runtime_failed",
            maximum=128,
        )
        if raw_error_code is not None
        else None
    )
    return {
        "id": _safe_application_token(
            _application_value(state, "instance_id", _application_value(state, "id")),
            fallback="unknown-instance",
        ),
        "profile_id": _safe_application_token(
            _application_value(state, "profile_id", "unknown-profile"),
            fallback="unknown-profile",
        ),
        "status": status,
        "revision": int(_application_value(state, "revision", 0)),
        "degraded": bool(_application_value(state, "degraded", False)),
        "hard_risk": bool(_application_value(state, "hard_risk", False)),
        "error_code": error_code,
        "memory_version": int(_application_value(state, "memory_version", 0)),
        "input_count": len(raw_inputs),
        "intent_count": len(raw_intents),
        "outcome_count": len(raw_outcomes),
        "event_count": len(raw_events),
        "intents": intents,
        "outcomes": outcomes,
        "created_at": str(_application_value(state, "created_at", "")),
        "updated_at": str(_application_value(state, "updated_at", "")),
        "finished_at": _application_value(state, "finished_at"),
        "wake_at": _application_value(state, "wake_at"),
    }


def _resolve_kernel_task_session_owner(
    *,
    task: Any,
    goal_store: SQLiteGoalStore,
    agent_runtime_store: SQLiteAgentRuntimeStore,
    long_lived_mobile_runtime: Any | None = None,
    runtime_kernel: RuntimeKernel | Any | None = None,
) -> Any:
    """Return the one Session that durably owns this exact Kernel Task.

    ``TaskSource.conversation_id`` is an index into a GoalRun, never an
    authorization grant on its own.  Direct Kernel bindings must name this
    exact Task.  A long-lived mobile GoalRun instead binds its Application
    instance; only the single in-flight, durably correlated cycle child may
    borrow that instance's DeviceBody authority.  Both routes reject a second
    task forged with the same ``goal:<id>`` conversation prefix.
    """

    conversation_id = task.source.conversation_id
    goal_prefix = "goal:"
    if not conversation_id.startswith(goal_prefix) or not conversation_id[len(goal_prefix):]:
        raise RuntimeError("Kernel Task has no GoalRun ownership key")
    goal_run_id = conversation_id[len(goal_prefix):]
    goal_run = goal_store.inspect(goal_run_id)
    binding_kind = str(getattr(goal_run, "binding_kind", ""))
    if binding_kind in {"runtime_kernel", "runtime_kernel_canary"}:
        if goal_run.bound_task_id != task.id:
            raise RuntimeError("Kernel Task is not the GoalRun's bound task")
    elif binding_kind == "long_lived_mobile_composition":
        _authorize_long_lived_mobile_cycle_child(
            task=task,
            goal_run=goal_run,
            goal_run_id=goal_run_id,
            long_lived_mobile_runtime=long_lived_mobile_runtime,
            runtime_kernel=runtime_kernel,
        )
    else:
        raise RuntimeError("Kernel Task GoalRun binding kind cannot grant DeviceBody authority")

    ownership_bindings = [
        (session, binding)
        for session in agent_runtime_store.sessions_for_recovery()
        for binding in agent_runtime_store.bindings(session.id)
        if binding.goal_run_id == goal_run_id
    ]
    if len(ownership_bindings) != 1:
        raise RuntimeError(
            "Kernel Task GoalRun must resolve to exactly one AgentSession binding"
        )
    return ownership_bindings[0][0]


def _authorize_long_lived_mobile_cycle_child(
    *,
    task: Any,
    goal_run: Any,
    goal_run_id: str,
    long_lived_mobile_runtime: Any | None,
    runtime_kernel: RuntimeKernel | Any | None,
) -> None:
    """Fail closed unless a Task is the active durable long-lived child.

    The public GoalRun deliberately names the ApplicationRuntime instance,
    not its short-lived Kernel cycle Tasks.  This helper consumes the narrow
    read-only correlation projection exposed by that private runtime and the
    immutable accepted-cycle event retained in the Kernel ledger.  It does
    not authorize by foreground application, active Goal, or a source prefix.
    """

    source = getattr(task, "source", None)
    if (
        source is None
        or getattr(source, "client_id", None) != "goal-v2-long-lived-mobile"
        or not isinstance(getattr(source, "initial_message_id", None), str)
        or not source.initial_message_id.strip()
    ):
        raise RuntimeError("Kernel Task is not a durable long-lived mobile cycle")
    instance_id = getattr(goal_run, "bound_task_id", None)
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise RuntimeError("long-lived GoalRun has no bound application instance")
    if long_lived_mobile_runtime is None or runtime_kernel is None:
        raise RuntimeError("long-lived mobile authorization dependencies are unavailable")
    authorization_reader = getattr(
        long_lived_mobile_runtime, "durable_child_authorization", None
    )
    if not callable(authorization_reader):
        raise RuntimeError("long-lived mobile authorization reader is unavailable")
    try:
        authorization = authorization_reader(instance_id)
    except (KeyError, OSError, RuntimeError, ValueError):
        raise RuntimeError("long-lived mobile authorization facts are unavailable") from None
    if authorization is None:
        raise RuntimeError("long-lived mobile child is not currently dispatching")
    if (
        getattr(authorization, "instance_id", None) != instance_id
        or getattr(authorization, "goal_id", None) != goal_run_id
        or getattr(authorization, "target_id", None) != task.device_id
        or not isinstance(getattr(authorization, "activated_at", None), str)
        or not authorization.activated_at.strip()
        or getattr(authorization, "intent_phase", None) != "dispatching"
        or getattr(authorization, "reservation_id", None)
        != source.initial_message_id
    ):
        raise RuntimeError("Kernel Task does not match the dispatching long-lived child")
    try:
        events = tuple(runtime_kernel.events(task.id))
    except (KeyError, OSError, RuntimeError, ValueError):
        raise RuntimeError("Kernel Task accepted-cycle facts are unavailable") from None
    accepted = tuple(
        event for event in events
        if getattr(event, "type", None) == "KernelApplicationCycleAccepted"
    )
    if len(accepted) != 1:
        raise RuntimeError("Kernel Task must have exactly one accepted long-lived cycle event")
    payload = getattr(accepted[0], "payload", None)
    if not isinstance(payload, dict):
        raise RuntimeError("Kernel Task accepted-cycle event payload is invalid")
    if (
        payload.get("goal_id") != goal_run_id
        or payload.get("application_instance_id") != instance_id
        or payload.get("cycle_key") != source.initial_message_id
        or payload.get("target_id") != task.device_id
        or payload.get("application_cycle")
        != getattr(authorization, "application_cycle", None)
    ):
        raise RuntimeError("Kernel Task accepted-cycle event does not match long-lived authorization")


def create_app(
    *,
    settings: Settings | None = None,
    repository: SQLiteRepository | None = None,
    adb_discovery: AdbTargetDiscovery | None = None,
    adb_executor: GuiExecutor | None = None,
    chat_coordinator: ChatCoordinator | None = None,
    automation_factory: AndroidAutomationFactory | None = None,
    cloud_configuration: CloudChatConfiguration | None = None,
    game_learner: GameLearner | Any | None = None,
    mobile_task_runtime: MobileTaskRuntime | Any | None = None,
    mobile_task_archive: MobileTaskArchive | Any | None = None,
    application_runtime: Any | None = None,
    application_runtime_archive: Any | None = None,
    runtime_admin: LeaseAdminService | None = None,
    console_shutdown_callback: Callable[[], None] | None = None,
    gateway: GatewayComposition | None = None,
    goal_service: GoalService | None = None,
    agent_session_service: AgentSessionService | None = None,
    execution_contract_service: ExecutionContractService | None = None,
    execution_contract_v2_service: V2ExecutionContractService | None = None,
    execution_v2_device_profiles: Any | None = None,
    execution_v2_frames: Any | None = None,
    long_task_scheduler: Any = _AUTO_LONG_TASK_SCHEDULER,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    def canonical_runtime_device_id(device_id: str) -> str:
        return device_id

    def transport_runtime_device_id(device_id: str) -> str:
        return device_id
    # Phase 7: 启动期校验运行时模式（配置非法即拒绝启动，fail-fast）
    validate_runtime_mode(resolved_settings.runtime_mode)
    runtime_mode_guard = RuntimeModeGuard(resolved_settings.runtime_mode)
    if runtime_mode_guard.is_draining() and resolved_settings.kernel_canary_enabled:
        raise RuntimeError(
            "Kernel canary cannot run while Legacy work is draining."
        )
    kernel_binding_kind = (
        "runtime_kernel"
        if runtime_mode_guard.is_kernel_active()
        else "runtime_kernel_canary"
        if resolved_settings.kernel_canary_enabled
        else None
    )
    kernel_runtime_enabled = kernel_binding_kind is not None
    append_mode_journal(
        resolved_settings.project_root / "runtime" / "logs" / "runtime-mode.jsonl",
        mode=resolved_settings.runtime_mode,
        event="composition_requested",
        details={"kernel_binding_kind": kernel_binding_kind},
    )
    resolved_runtime_admin = runtime_admin or LeaseAdminService(
        resolved_settings.data_dir / "runtime" / "runtime.db"
    )
    resolved_repository = repository or SQLiteRepository(resolved_settings.database_path)
    resolved_cloud_configuration = cloud_configuration or CloudChatConfiguration(
        resolved_repository,
        resolved_settings,
    )
    resolved_executor = adb_executor or AdbGuiExecutor.from_settings(resolved_settings)
    # Resolution is lazy: constructing the application may resolve filesystem
    # configuration later, but never runs ``adb devices`` or any shell command.
    resolved_adb_discovery = adb_discovery or AdbTargetDiscovery(
        adb_path=resolved_settings.adb_path
    )
    resolved_mobile_tasks = mobile_task_runtime
    resolved_application_runtime = application_runtime
    resolved_application_archive = application_runtime_archive
    if (
        resolved_application_runtime is None
        and resolved_application_archive is None
    ):
        local_application_composition = compose_local_managed_application_runtime(
            resolved_settings
        )
        resolved_application_runtime = ApplicationRuntimeCatalog(
            {
                LOCAL_MANAGED_PROFILE_ID: local_application_composition.runtime,
            },
            scheduler_profile_id=LOCAL_MANAGED_PROFILE_ID,
        )
        resolved_application_archive = resolved_application_runtime
    elif resolved_application_archive is None:
        resolved_application_archive = resolved_application_runtime
    device_execution_lease = DeviceExecutionLease()
    mobile_evidence = LocalMobileEvidenceStore(
        resolved_settings.project_root
        / "runtime"
        / "sessions"
        / "mobile-tasks"
        / "evidence"
    )
    experience_service = ExperienceService(
        SQLiteExperienceStore(resolved_settings.data_dir / "experience.db"),
        observation_payload=lambda evidence_id: mobile_evidence.load(evidence_id).png_bytes,
    )
    structured_goal_model: StructuredGoalModel | None = None
    session_planner: StructuredSessionPlanner | None = None
    attention_scheduler = AttentionScheduler()
    role_model: Any | None = None
    daily_checklist_store = SQLiteDailyChecklistStore(
        resolved_settings.data_dir / "stzb-daily.db"
    )
    mobile_role_endpoint = (
        resolved_settings.mobile_role_endpoint or resolved_settings.local_chat_endpoint
    )
    mobile_role_model = (
        resolved_settings.mobile_role_model or resolved_settings.local_chat_model
    )
    mobile_role_api_key = (
        resolved_settings.mobile_role_api_key
        if resolved_settings.mobile_role_endpoint
        else resolved_settings.local_chat_api_key
    )
    # Goal composition is a local text/tool task, not a device-action task.
    # Keep it available whenever its local model is configured so a local
    # managed long-lived GoalRun can be frozen even when no ADB target is
    # present. Device ownership is still checked only by the finite route.
    if mobile_role_endpoint and mobile_role_model:
        goal_role_model = OpenAICompatibleToolRoleModel(
                endpoint=mobile_role_endpoint,
                model=mobile_role_model,
                api_key=mobile_role_api_key,
                timeout_seconds=resolved_settings.chat_request_timeout_seconds,
                evidence=mobile_evidence,
        )
        structured_goal_model = StructuredGoalModel(goal_role_model)
        session_planner = FallbackSessionPlanner(
            StructuredSessionPlanner(goal_role_model),
            fallback=DeterministicSessionPlanner(),
            recoverable_exceptions=(MobileTaskAdapterError,),
        )
        attention_scheduler = build_qwen_attention_scheduler(goal_role_model)
    if (
        isinstance(resolved_executor, AdbGuiExecutor)
        and resolved_settings.gui_executor_enabled
        and resolved_settings.adb_path
        and mobile_role_endpoint
        and mobile_role_model
    ):
        role_model = (
            OpenAICompatibleToolRoleModel(
                endpoint=mobile_role_endpoint,
                model=mobile_role_model,
                api_key=mobile_role_api_key,
                # The local 27B multimodal role can spend more than two minutes
                # encoding a dense 1280x720 game frame. Keep the request
                # bounded, but leave enough headroom for one definitive result
                # so the runtime does not turn slow inference into an unknown
                # action outcome.
                timeout_seconds=max(resolved_settings.chat_request_timeout_seconds, 180.0),
                evidence=mobile_evidence,
            )
            if resolved_settings.mobile_role_endpoint
            else OpenAICompatibleMobileRoleModel(
                endpoint=mobile_role_endpoint,
                model=mobile_role_model,
                api_key=mobile_role_api_key,
                timeout_seconds=resolved_settings.chat_request_timeout_seconds,
                evidence=mobile_evidence,
            )
        )
        if isinstance(role_model, OpenAICompatibleToolRoleModel):
            structured_goal_model = StructuredGoalModel(role_model)
            session_planner = FallbackSessionPlanner(
                StructuredSessionPlanner(role_model),
                fallback=DeterministicSessionPlanner(),
                recoverable_exceptions=(MobileTaskAdapterError,),
            )
            attention_scheduler = build_qwen_attention_scheduler(role_model)
        if (
            resolved_mobile_tasks is None
            and runtime_mode_guard.is_legacy_runtime_available()
        ):
            resolved_mobile_tasks = MobileTaskRuntime(
                resolved_settings.data_dir / "mobile-tasks.db",
                driver=MobileTaskAndroidDriver(
                    repository=resolved_repository,
                    executor=resolved_executor,
                    evidence=mobile_evidence,
                    device_lease=device_execution_lease,
                ),
                model=role_model,
                # Production tasks may span a long game session. These are only
                # runaway guards; ordinary recovery is driven by visual progress,
                # reflection, owner input, or an explicit stop.
                max_reflections=64,
                max_attempts=2_048,
                queue_capacity=32,
                scope_resolver=resolve_mobile_skill_scope,
                experience=experience_service,
                progress_controller=(
                    StzbDailyProgressController(
                        daily_checklist_store,
                        inspect_daily_checklist=structured_goal_model.inspect_daily_checklist,
                        is_execution_stage=_is_stzb_execution_stage,
                    )
                    if structured_goal_model else None
                ),
            )
    resolved_automation_factory = automation_factory
    if (
        resolved_automation_factory is None
        and isinstance(resolved_executor, AdbGuiExecutor)
        and resolved_settings.gui_executor_enabled
        and resolved_settings.adb_path
        and resolved_settings.adb_serial
        and resolved_settings.local_chat_endpoint
        and resolved_settings.local_chat_model
    ):
        resolved_automation_factory = RepositoryAndroidAutomationFactory(
            repository=resolved_repository,
            executor=resolved_executor,
            model=OpenAICompatibleGuiOwlClient(
                endpoint=resolved_settings.local_chat_endpoint,
                model=resolved_settings.local_chat_model,
                api_key=resolved_settings.local_chat_api_key,
                timeout_seconds=resolved_settings.chat_request_timeout_seconds,
            ),
            device_lease=device_execution_lease,
        )
    service = ControlPlaneService(
        resolved_repository,
        resolved_settings,
        adb_discovery=resolved_adb_discovery,
        adb_executor=resolved_executor,
        cloud_configuration=resolved_cloud_configuration,
    )
    resolved_chat = chat_coordinator or ChatCoordinator(
        resolved_repository,
        local_provider=OpenAIChatProvider.from_local_settings(resolved_settings),
        cloud_provider=None,
        cloud_provider_resolver=resolved_cloud_configuration.resolve_provider,
        automation_factory=resolved_automation_factory,
        max_workers=resolved_settings.chat_max_workers,
        max_pending=resolved_settings.chat_max_pending,
    )
    learning_environment: Any = _UnavailableLearningEnvironmentFactory()
    if (
        isinstance(resolved_executor, AdbGuiExecutor)
        and resolved_settings.gui_executor_enabled
        and resolved_settings.adb_path
        and resolved_settings.adb_serial
        and resolved_settings.local_chat_endpoint
        and resolved_settings.local_chat_model
    ):
        learning_environment = StzbAndroidEnvironmentFactory(
            executor=resolved_executor,
            model=OpenAICompatibleGuiOwlClient(
                endpoint=resolved_settings.local_chat_endpoint,
                model=resolved_settings.local_chat_model,
                api_key=resolved_settings.local_chat_api_key,
                timeout_seconds=resolved_settings.chat_request_timeout_seconds,
            ),
            assessor=OpenAICompatibleStzbEvidenceAssessor(
                endpoint=resolved_settings.local_chat_endpoint,
                model=resolved_settings.local_chat_model,
                api_key=resolved_settings.local_chat_api_key,
                timeout_seconds=resolved_settings.chat_request_timeout_seconds,
            ),
            device_lease=device_execution_lease,
        )
    resolved_game_learner = game_learner
    if resolved_game_learner is None:
        resolved_game_learner = GameLearner(
            store=SQLiteLearningStore(resolved_settings.data_dir / "learning.db"),
            artifacts=LocalArtifactStore(
                resolved_settings.project_root
                / "runtime"
                / "sessions"
                / "game-learning"
            ),
            environment_factory=learning_environment,
            profiles=[stzb_game_profile()],
            max_workers=1,
            max_pending=8,
        )

    resolved_mobile_task_archive = (
        mobile_task_archive
        or resolved_mobile_tasks
        or MobileTaskArchive(resolved_settings.data_dir / "mobile-tasks.db")
    )
    # AgentSession and DeviceBody share one durable ledger.  Construct this
    # before RuntimeKernel so its dispatch bridge can resolve a Kernel Task to
    # the already-established GoalRun -> AgentSession relationship.
    agent_runtime_database = resolved_settings.data_dir / "agent-runtime.db"
    agent_runtime_store = SQLiteAgentRuntimeStore(agent_runtime_database)
    harness_execution_store = SQLiteExecutionContractStore(
        resolved_settings.data_dir / "harness-executions.db"
    )
    user_fact_store = SQLiteUserFactStore(agent_runtime_database)
    user_fact_service = UserFactService(user_fact_store)

    def session_fact_context(_: str) -> list[dict[str, Any]]:
        context: list[dict[str, Any]] = []
        for fact in user_fact_store.list_facts(user_scope="local-owner"):
            revisions = user_fact_store.revisions(
                user_scope=fact.user_scope, fact_key=fact.fact_key
            )
            if not revisions:
                continue
            latest = revisions[-1]
            context.append(
                {
                    "fact_key": latest.fact_key,
                    "revision_id": latest.id,
                    "value": latest.value,
                    "applicability": latest.applicability,
                    "source_kind": latest.source_kind.value,
                    "source_ref": latest.source_ref,
                    "valid_until": (
                        latest.valid_until.isoformat()
                        if latest.valid_until is not None
                        else None
                    ),
                }
            )
        return context
    device_body_store = SQLiteDeviceBodyStore(agent_runtime_database)
    adb_device_body_adapter = (
        AdbDeviceBodyAdapter(adb_path=resolved_settings.adb_path)
        if resolved_settings.adb_path
        else None
    )
    # The active product path is emulator-only. DeviceBody commands use the
    # exact ADB transport that was accepted by the saved emulator Profile;
    # there is no secondary Companion identity or pairing lifecycle.
    device_body_adapter = adb_device_body_adapter
    deferred_device_body_event_inbox: _DeferredBodyEventInbox | None = None
    device_body_bridge: KernelDeviceBodyBridge | None = None
    kernel: RuntimeKernel | None = None
    resolved_gateway = gateway
    kernel_coordinator: KernelCanaryCoordinator | None = None
    kernel_observation_provider: AndroidObservationProvider | None = None
    long_lived_mobile_composition: Any | None = None
    activity_slice_runner: NormalActivitySliceRunner | None = None
    activity_preemption_coordinator = (
        AgentRuntimeActivitySlicePreemptionCoordinator(agent_runtime_store)
    )
    kernel_artifacts = None
    if kernel_runtime_enabled:
        if (
            role_model is None
            or not resolved_settings.adb_path
            or not resolved_settings.gui_executor_enabled
        ):
            raise RuntimeError(
                "Kernel-active startup requires the configured Android role model and ADB executor."
            )
        runtime_dir = resolved_settings.data_dir / "runtime"
        kernel_artifacts = FilesystemArtifactStore(runtime_dir / "artifacts")
        kernel_observation_provider = AndroidObservationProvider(
            adb_path=resolved_settings.adb_path,
            transport_device_id_resolver=transport_runtime_device_id,
        )
        if adb_device_body_adapter is None:
            raise RuntimeError("Kernel-active startup requires the DeviceBody ADB adapter.")
        runtime_store = SQLiteRuntimeStore(runtime_dir / "runtime.db")

        def resolve_device_body_binding(
            task_id: str, device_id: str
        ) -> DeviceBodyBinding:
            """Resolve only the Session that owns this Kernel Task's GoalRun.

            KernelCanaryCoordinator persists ``goal:<GoalRun id>`` in the
            task source conversation.  Direct bindings name this exact Task;
            long-lived mobile bindings name their ApplicationRuntime instance
            and only grant its one durably dispatching cycle child.  Either
            route still requires the unique SessionGoalBinding.  A foreground
            application hint, an active Goal, a visible app, or merely a
            matching conversation prefix is never sufficient.
            """

            task = runtime_store.load_task(task_id)
            if task.device_id != device_id:
                raise ValueError("Kernel Task device does not match dispatch device")
            session = _resolve_kernel_task_session_owner(
                task=task,
                goal_store=goal_store,
                agent_runtime_store=agent_runtime_store,
                long_lived_mobile_runtime=(
                    long_lived_mobile_composition.runtime
                    if long_lived_mobile_composition is not None
                    else None
                ),
                runtime_kernel=kernel,
            )
            existing = device_body_store.binding_for_session(session.id)
            if existing is not None:
                if existing.device_id != device_id:
                    raise ValueError("AgentSession DeviceBody binding would drift devices")
                if existing.adapter_id != adb_device_body_adapter.adapter_id:
                    raise ValueError("AgentSession DeviceBody adapter is not emulator ADB")
                return existing

            now = _utc_now()
            binding_adapter = adb_device_body_adapter
            created = device_body_store.create_binding(
                DeviceBodyBinding(
                    id=str(uuid4()),
                    session_id=session.id,
                    device_id=device_id,
                    adapter_id=binding_adapter.adapter_id,
                    device_boot_id=binding_adapter.read_boot_id(device_id),
                    connection_state=ConnectionState.CONNECTED,
                    capability_revision=0,
                    event_cursor=0,
                    action_cursor=0,
                    bound_at=now,
                    updated_at=now,
                )
            )
            if created.capability_revision == 0:
                device_body_store.replace_capability_revision(
                    created.id, binding_adapter.discover_capabilities(created)
                )
            return device_body_store.load_binding(created.id)

        deferred_device_body_event_inbox = _DeferredBodyEventInbox()
        device_body_bridge = KernelDeviceBodyBridge(
            store=device_body_store,
            adapter=device_body_adapter,
            binding_resolver=resolve_device_body_binding,
            clock=_utc_now,
            event_inbox=deferred_device_body_event_inbox,
        )
        kernel = RuntimeKernel(
            runtime_store,
            observation_provider=kernel_observation_provider,
            artifact_store=kernel_artifacts,
            action_executor=GuiExecutorActionAdapter(
                resolved_executor.for_serial,
                transport_device_id_resolver=transport_runtime_device_id,
            ),
            # RuntimeKernel prioritizes this dispatcher over the legacy
            # executor, so one Kernel Action can create only one Body command.
            body_dispatcher=device_body_bridge,
        )
        kernel_coordinator = KernelCanaryCoordinator(
            kernel=kernel,
            model=role_model,
            artifacts=kernel_artifacts,
            evidence=mobile_evidence,
            device_lease=device_execution_lease,
            experience=experience_service,
            binding_kind=kernel_binding_kind,
            application_readiness_assessor=(
                structured_goal_model.assess_mobile_application_readiness
                if structured_goal_model is not None
                else None
            ),
            device_id_resolver=canonical_runtime_device_id,
        )
        activity_slice_store = ActivitySliceStore(
            resolved_settings.data_dir / "activity-slices.db"
        )
        activity_slice_runner = NormalActivitySliceRunner(
            activity_slice_store,
            agent_runtime_store=agent_runtime_store,
            device_body_store=device_body_store,
            observation_provider=kernel_observation_provider,
            preemption_coordinator=activity_preemption_coordinator,
        )
        long_lived_mobile_composition = (
            compose_long_lived_mobile_application_runtime(
                resolved_settings.data_dir,
                kernel=kernel_coordinator,
                observation_provider=kernel_observation_provider,
                activity_slice_runner=activity_slice_runner,
            )
        )
        if resolved_gateway is None:
            resolved_gateway = build_gateway_composition(
                settings=resolved_settings,
                kernel=kernel,
                worker=kernel_coordinator,
            )

    def discover_mobile_application(target_id: str) -> str:
        if kernel_observation_provider is None:
            raise RuntimeError("RuntimeKernel Android observation is unavailable")
        state = kernel_observation_provider.read_device_state(target_id)
        application_id = str(state.foreground_app or "").strip()
        if not application_id:
            raise RuntimeError("foreground application is unavailable")
        return application_id

    def recover_session_context(session_id: str) -> str:
        if kernel_observation_provider is None:
            raise RuntimeError("context recovery observation is unavailable")
        binding = device_body_store.binding_for_session(session_id)
        if binding is None:
            raise RuntimeError("context recovery Session has no DeviceBody binding")
        state = kernel_observation_provider.read_device_state(binding.device_id)
        captured_at = str(state.captured_at or "").strip()
        if not captured_at:
            raise RuntimeError("context recovery observation has no capture time")
        foreground = str(state.foreground_app or "unknown")
        return f"device-state:recovery:{binding.device_id}:{captured_at}:{foreground}"
    def probe_configured_mobile_target() -> tuple[bool, str, str]:
        probe = resolved_executor.probe()
        if probe.status == "ready":
            return True, "ready", probe.detail
        blocker = probe.blocker or {}
        return (
            False,
            str(blocker.get("code") or "default_android_target_not_ready"),
            str(blocker.get("message") or probe.detail or "默认 Android 目标当前不可用。"),
        )

    goal_store = SQLiteGoalStore(resolved_settings.data_dir / "goals.db")

    def promote_verified_goal_experience(
        task_id: str, goal_id: str, completion_revision: int
    ) -> None:
        record = goal_store.inspect(goal_id)
        if (
            record.binding_kind in {"runtime_kernel", "runtime_kernel_canary"}
            and kernel_coordinator is not None
        ):
            kernel_coordinator.confirm_goal_completion(
                task_id, goal_id, completion_revision
            )
            return
        # The legacy hint and canonical ledger are independent additive writes;
        # either can be reconciled later without rewriting Goal completion.
        try:
            resolved_mobile_tasks.promote_goal_verified_memory(
                task_id, goal_id=goal_id, completion_revision=completion_revision
            )
        except Exception:
            pass
        try:
            specification = goal_store.specification(goal_id)
            resolved_mobile_tasks.promote_goal_verified_experience(
                task_id, goal_id=goal_id, completion_revision=completion_revision,
                goal_spec_revision=int(specification["revision"]),
                frozen_criteria_ids=tuple(
                    str(item["id"])
                    for item in specification.get("success_criteria", ())
                ),
            )
        except Exception:
            pass
    production_goal_repairs = ProductionGoalRepairs(
        manager=GoalRepairManager(goal_store),
        project_root=resolved_settings.project_root,
        capability_snapshot=service.runtime_probe.snapshot,
        discover_targets=service.discover_targets,
        adb_path=resolved_settings.adb_path,
        model_control_script=resolved_settings.mobile_role_control_script,
        repository_model_enabled=not bool(resolved_settings.mobile_role_endpoint),
    )
    resolved_goal_service = goal_service or GoalService(
        goal_store,
        mobile_runtime=resolved_mobile_tasks,
        mobile_archive=resolved_mobile_task_archive,
        kernel_runtime=kernel_coordinator,
        kernel_canary_enabled=(kernel_binding_kind == "runtime_kernel_canary"),
        kernel_binding_kind=kernel_binding_kind,
        configured_serial=resolved_settings.adb_serial,
        target_probe=(
            probe_configured_mobile_target
            if isinstance(resolved_mobile_tasks, MobileTaskRuntime)
            else None
        ),
        preflight=(
            GoalPreflight(
                capability_snapshot=service.runtime_probe.snapshot,
                discover_targets=service.discover_targets,
                lease_is_held=device_execution_lease.is_held,
                runtime_available=lambda: (
                    False
                    if runtime_mode_guard.is_draining()
                    else kernel_coordinator is not None
                    if kernel_runtime_enabled
                    else resolved_mobile_tasks is not None
                ),
                preferred_serial=resolved_settings.adb_serial,
                runtime_kind=(
                    kernel_binding_kind
                    or (
                        "runtime_transition_draining"
                        if runtime_mode_guard.is_draining()
                        else "mobile_task_compat"
                    )
                ),
            ).assess
            if resolved_mobile_tasks is None
            or isinstance(resolved_mobile_tasks, MobileTaskRuntime)
            else None
        ),
        repair=(
            production_goal_repairs.attempt
            if resolved_mobile_tasks is None
            or isinstance(resolved_mobile_tasks, MobileTaskRuntime)
            else None
        ),
        specify_goal=(structured_goal_model.specify if structured_goal_model else None),
        verify_completion=(structured_goal_model.verify if structured_goal_model else None),
        promote_verified_success=(
            promote_verified_goal_experience
            if isinstance(resolved_mobile_tasks, MobileTaskRuntime)
            or kernel_coordinator is not None
            else None
        ),
        daily_checklist_store=daily_checklist_store,
        inspect_daily_checklist=(
            structured_goal_model.inspect_daily_checklist
            if structured_goal_model else None
        ),
        application_runtime=resolved_application_runtime,
        application_archive=resolved_application_archive,
        long_lived_mobile_runtime=(
            long_lived_mobile_composition.runtime
            if long_lived_mobile_composition is not None
            else None
        ),
        long_lived_mobile_archive=(
            long_lived_mobile_composition.runtime
            if long_lived_mobile_composition is not None
            else None
        ),
        discover_mobile_application=(
            discover_mobile_application
            if long_lived_mobile_composition is not None
            else None
        ),
        target_id_resolver=canonical_runtime_device_id,
        experience=experience_service,
        answer_language=(
            structured_goal_model.answer_language
            if structured_goal_model else None
        ),
    )
    resolved_agent_session_service = agent_session_service or AgentSessionService(
        agent_runtime_store,
        resolved_goal_service,
        planner=session_planner,
        attention_scheduler=attention_scheduler,
        # GoalService can touch Kernel/ApplicationRuntime while delivering an
        # activation outbox.  Normal composition defers replay until every
        # downstream runtime has completed its own startup/recovery.
        recover_on_start=False,
        device_body_store=device_body_store,
        context_recovery=(
            recover_session_context if kernel_observation_provider is not None else None
        ),
        fact_context_provider=session_fact_context,
    )
    fact_question_coordinator = FactQuestionCoordinator(
        user_fact_service, resolved_agent_session_service
    )
    if activity_slice_runner is not None:
        activity_slice_runner.bind_fact_question_coordinator(
            fact_question_coordinator
        )
    activity_preemption_coordinator.bind_event_handler(
        resolved_agent_session_service.handle_inbox_event
    )
    device_body_event_inbox = DeviceBodyEventInbox(
        agent_runtime_store,
        device_body_store=device_body_store,
        event_handler=resolved_agent_session_service.handle_inbox_event,
    )
    if deferred_device_body_event_inbox is not None:
        deferred_device_body_event_inbox.bind(device_body_event_inbox)
    resolved_execution_contract = execution_contract_service or ExecutionContractService(
        harness_execution_store,
        resolved_agent_session_service,
        fact_question_coordinator,
        kernel=kernel,
        artifact_store=kernel_artifacts,
    )
    production_emulator = compose_production_emulator_runtime(
        data_dir=resolved_settings.data_dir,
        adb_discovery=resolved_adb_discovery,
        agent_runtime_store=agent_runtime_store,
        clock=_utc_now,
        gui_executor_enabled=resolved_settings.gui_executor_enabled,
        role_model=(role_model if isinstance(role_model, OpenAICompatibleToolRoleModel) else None),
        mobile_evidence=mobile_evidence,
        experience_service=experience_service,
    )
    resolved_execution_v2_device_profiles = (
        execution_v2_device_profiles or production_emulator.profile_port
    )
    resolved_execution_v2_frames = execution_v2_frames or production_emulator.frame_port
    resolved_execution_contract_v2 = (
        execution_contract_v2_service
        or V2ExecutionContractService(
            harness_execution_store,
            CanonicalTaskPortAdapter(
                runtime_store=agent_runtime_store,
                execution_store=harness_execution_store,
                fact_questions=fact_question_coordinator,
                profiles=getattr(resolved_execution_v2_device_profiles, "profiles", None),
            ),
            device_profiles=resolved_execution_v2_device_profiles,
            experiences=ExperiencePortAdapter(experience_service),
            frames=resolved_execution_v2_frames,
        )
    )
    resolved_long_task_scheduler = long_task_scheduler
    if resolved_long_task_scheduler is _AUTO_LONG_TASK_SCHEDULER:
        resolved_long_task_scheduler = ResidentV2TaskScheduler(
            runtime_store=agent_runtime_store,
            coordination=SQLiteSchedulerCoordination(agent_runtime_database),
            profiles=production_emulator.profiles,
            operator=None,
            owner_id=f"process-{uuid4()}",
            runner_admission=resolved_execution_contract_v2,
            general_handler=(
                production_emulator.general_ui.runner
                if production_emulator.general_ui is not None
                else None
            ),
        )
    agent_runtime_event_pump = AgentRuntimeEventPump(
        resolved_agent_session_service,
        long_task_scheduler=resolved_long_task_scheduler,
    )
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        chat_start_attempted = False
        game_start_attempted = False
        application_start_attempted = False
        long_lived_mobile_start_attempted = False
        try:
            service.initialize()
            user_fact_store.initialize()
            # An outer ApplicationRuntime timeout can leave a fully persisted
            # EVENT_AVAILABLE Slice handoff owning Session authority.  Close
            # that checkpoint before the watchdog or EventInbox recovery can
            # ask the scheduler for a new AttentionDecision.
            if activity_slice_runner is not None:
                activity_slice_runner.recover_persisted_preemptions()
            resolved_cloud_configuration.start()
            # Rebuild the durable agenda before any long-lived mobile worker
            # can recover.  GoalRun preparation is side-effect free; selected
            # dispatch remains pending until the downstream runtime has
            # started behind the non-selected pause fence below.
            resolved_agent_session_service.recover_planning()
            resolved_agent_session_service.recover_pending()
            fact_question_coordinator.recover(dispatch=False)
            resolved_agent_session_service.recover_inbox_events(dispatch=False)
            resolved_agent_session_service.recover_attention(dispatch=False)
            selected_goal_run_ids = (
                resolved_agent_session_service.selected_goal_run_ids()
            )
            application_startup = getattr(
                resolved_application_runtime, "startup", None
            )
            if callable(application_startup):
                resolved_goal_service.fence_nonselected_application_bindings_before_start(
                    selected_goal_run_ids
                )
                application_start_attempted = True
                application_startup()
                resolved_goal_service.recover_selected_application_bindings(
                    selected_goal_run_ids
                )
            if runtime_mode_guard.is_legacy_runtime_available():
                chat_start_attempted = True
                resolved_chat.start()
                if resolved_game_learner is not None:
                    game_start_attempted = True
                    resolved_game_learner.start()
            if kernel_coordinator is not None:
                resolved_goal_service.fence_nonselected_kernel_bindings_before_recover(
                    selected_goal_run_ids
                )
                kernel_coordinator.recover()
                resolved_goal_service.recover_selected_kernel_bindings(
                    selected_goal_run_ids
                )
            if long_lived_mobile_composition is not None:
                resolved_goal_service.fence_stopped_long_lived_mobile_bindings_before_start()
                resolved_goal_service.fence_nonselected_long_lived_bindings_before_start(
                    selected_goal_run_ids
                )
                long_lived_mobile_start_attempted = True
                long_lived_mobile_composition.runtime.startup()
                resolved_goal_service.recover_long_lived_mobile_bindings(
                    selected_goal_run_ids
                )
            # Finish an interrupted Session stop before creating any missing
            # binding; then replay activation outboxes and settle stops whose
            # binding was the interrupted part.  Stable downstream request
            # keys make all three passes idempotent.
            resolved_agent_session_service.recover_stopping()
            resolved_agent_session_service.recover_attention_dispatches()
            resolved_agent_session_service.recover_stopping()
            agent_runtime_event_pump.start()
            append_mode_journal(
                resolved_settings.project_root
                / "runtime"
                / "logs"
                / "runtime-mode.jsonl",
                mode=resolved_settings.runtime_mode,
                event="runtime_started",
                details={
                    "kernel_binding_kind": kernel_binding_kind,
                    "legacy_workers_started": runtime_mode_guard.is_legacy_runtime_available(),
                    "long_lived_runtime_managed": application_start_attempted,
                    "long_lived_mobile_composed": long_lived_mobile_start_attempted,
                },
            )
            yield
        finally:
            # Every component gets an independent best-effort cleanup attempt,
            # including a component whose start method failed after partially
            # allocating resources. Preserve the original startup/request
            # failure; on an otherwise clean exit, surface the first shutdown
            # failure after all cleanup callbacks have run.
            cleanup_error: Exception | None = None
            active_error = sys.exc_info()[0] is not None
            shutdown_callbacks: list[Any] = []
            shutdown_callbacks.append(agent_runtime_event_pump.shutdown)
            if game_start_attempted and resolved_game_learner is not None:
                shutdown_callbacks.append(resolved_game_learner.shutdown)
            if chat_start_attempted:
                shutdown_callbacks.append(resolved_chat.shutdown)
            mobile_task_shutdown = getattr(
                resolved_mobile_tasks, "shutdown", None
            )
            if callable(mobile_task_shutdown):
                shutdown_callbacks.append(mobile_task_shutdown)
            if long_lived_mobile_start_attempted:
                shutdown_callbacks.append(
                    long_lived_mobile_composition.runtime.shutdown
                )
            if kernel_coordinator is not None:
                shutdown_callbacks.append(kernel_coordinator.shutdown)
            # Week 6: 停止 Lease 后台清理并关闭 runtime.db（未初始化时为空操作）
            shutdown_callbacks.append(resolved_runtime_admin.shutdown)
            shutdown_callbacks.append(production_emulator.close)
            # Phase 6 Week 3: 显式启用 Gateway 时关闭 gateway.db 与 Runtime 存储
            if resolved_gateway is not None:
                shutdown_callbacks.append(resolved_gateway.store.close)
                shutdown_callbacks.append(resolved_gateway.kernel.close)
            application_shutdown = getattr(
                resolved_application_runtime, "shutdown", None
            )
            if callable(application_shutdown):
                shutdown_callbacks.append(application_shutdown)
            for shutdown_callback in shutdown_callbacks:
                try:
                    shutdown_callback()
                except Exception as error:
                    if cleanup_error is None:
                        cleanup_error = error
            if cleanup_error is not None and not active_error:
                raise cleanup_error
            append_mode_journal(
                resolved_settings.project_root
                / "runtime"
                / "logs"
                / "runtime-mode.jsonl",
                mode=resolved_settings.runtime_mode,
                event="runtime_stopped",
                details={"kernel_binding_kind": kernel_binding_kind},
            )

    app = FastAPI(
        title="AI-GAME Local Console",
        version="0.1.0",
        description="Local-only control plane with explicitly restricted ADB input actions.",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.runtime_mode_guard = runtime_mode_guard
    app.state.repository = resolved_repository
    app.state.control_plane = service
    app.state.chat_coordinator = resolved_chat
    app.state.cloud_configuration = resolved_cloud_configuration
    app.state.game_learner = resolved_game_learner
    app.state.device_execution_lease = device_execution_lease
    app.state.mobile_task_runtime = resolved_mobile_tasks
    app.state.mobile_task_archive = resolved_mobile_task_archive
    app.state.goal_service = resolved_goal_service
    app.state.agent_session_service = resolved_agent_session_service
    app.state.user_fact_service = user_fact_service
    app.state.fact_question_coordinator = fact_question_coordinator
    app.state.agent_runtime_event_pump = agent_runtime_event_pump
    app.state.execution_contract_v2 = resolved_execution_contract_v2
    app.state.emulator_profiles = production_emulator.profiles
    app.state.emulator_profile_port = resolved_execution_v2_device_profiles
    app.state.emulator_frame_bindings = production_emulator.frame_bindings
    app.state.emulator_frame_port = resolved_execution_v2_frames
    app.state.emulator_android_ui_runner = (
        production_emulator.general_ui.runner
        if production_emulator.general_ui is not None
        else None
    )
    app.state.device_body_store = device_body_store
    app.state.device_body_adapter = device_body_adapter
    app.state.adb_device_body_adapter = adb_device_body_adapter
    app.state.device_body_event_inbox = device_body_event_inbox
    app.state.device_body_bridge = device_body_bridge
    app.state.kernel_canary = kernel_coordinator
    app.state.kernel_runtime = kernel_coordinator
    app.state.application_runtime = resolved_application_runtime
    app.state.application_runtime_archive = resolved_application_archive
    app.state.long_lived_mobile_runtime = (
        long_lived_mobile_composition.runtime
        if long_lived_mobile_composition is not None
        else None
    )
    app.state.long_lived_mobile_runtime_archive = (
        long_lived_mobile_composition.runtime
        if long_lived_mobile_composition is not None
        else None
    )
    app.state.runtime_admin = resolved_runtime_admin
    app.state.console_shutdown_callback = console_shutdown_callback
    # Phase 6 Week 3: Gateway 契约表面（默认 OFF，显式传入才挂载）
    app.state.gateway = resolved_gateway
    if resolved_gateway is not None:
        app.add_exception_handler(GatewayError, gateway_error_handler)

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @app.middleware("http")
    async def fence_legacy_device_writes(request: Request, call_next):
        """Retire every non-Kernel physical write surface during U7.

        Gateway and v2 Goal writes stay available in ``kernel_active``.  The
        old MobileTask endpoints keep their more specific route-level error,
        while the remaining Legacy execution islands are fenced here before a
        handler can enqueue or dispatch work.
        """
        path = request.url.path
        legacy_write = request.method == "POST" and (
            path == "/api/v1/executor/actions"
            or path == "/api/v1/runs"
            or path.startswith("/api/v1/runs/")
            or path.startswith("/api/v1/approvals/")
            or path.startswith("/api/v1/chat/")
            or path == "/api/v1/application-instances"
            or path.startswith("/api/v1/application-instances/")
            or path == "/api/v1/learning/jobs"
            or path.startswith("/api/v1/learning/jobs/")
        )
        legacy_admission = request.method == "POST" and (
            path == "/api/v1/executor/actions"
            or path == "/api/v1/runs"
            or path.startswith("/api/v1/runs/")
            or path.startswith("/api/v1/approvals/")
            or path == "/api/v1/chat/sessions"
            or (path.startswith("/api/v1/chat/sessions/") and path.endswith("/turns"))
            or path == "/api/v1/application-instances"
            or path == "/api/v1/learning/jobs"
        )
        if legacy_admission and runtime_mode_guard.is_draining():
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "LEGACY_DEVICE_WRITE_DISABLED",
                        "message": "Legacy 正在排空；不再接收新的旧运行时工作。",
                    }
                },
            )
        if legacy_write and runtime_mode_guard.is_kernel_active():
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "LEGACY_DEVICE_WRITE_DISABLED",
                        "message": (
                            "Kernel 已拥有设备执行权；该 Legacy 写入口仅保留只读归档。"
                        ),
                    }
                },
            )
        return await call_next(request)

    @app.middleware("http")
    async def require_console_client_for_writes(request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith(
            ("/api/v1/", "/api/v2/", "/api/v3/")
        ):
            if request.headers.get("X-AI-Game-Client") != WRITE_CLIENT_HEADER:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "console_client_required",
                            "message": (
                                "写操作需要请求头 "
                                "X-AI-Game-Client: console-v1。"
                            ),
                        }
                    },
                )
        return await call_next(request)

    @app.exception_handler(ControlPlaneError)
    async def handle_control_plane_error(
        _: Request, error: ControlPlaneError
    ) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.as_payload())

    @app.exception_handler(ChatCoordinatorError)
    async def handle_chat_coordinator_error(
        _: Request, error: ChatCoordinatorError
    ) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.as_payload())

    @app.exception_handler(CloudConfigError)
    async def handle_cloud_config_error(
        _: Request, error: CloudConfigError
    ) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.as_payload())

    @app.exception_handler(MobileTaskError)
    async def handle_mobile_task_error(
        _: Request, error: MobileTaskError
    ) -> JSONResponse:
        if isinstance(error, TaskNotFound):
            status_code = 404
            message = "未找到该智能任务。"
        elif isinstance(error, TaskQueueFull):
            status_code = 429
            message = "智能任务队列已满，请等待当前任务结束后重试。"
        elif isinstance(error, IdempotencyConflict):
            status_code = 409
            message = "client_request_id 已用于不同的智能任务操作。"
        elif isinstance(error, TaskStateConflict):
            status_code = 409
            message = "智能任务当前状态不接受该操作。"
        else:
            status_code = 409
            message = "智能任务请求无法执行。"
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": error.code, "message": message}},
        )

    app.add_exception_handler(GoalError, goal_error_handler)
    app.add_exception_handler(AgentRuntimeError, agent_runtime_error_handler)
    app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)

    @app.exception_handler(ApplicationRuntimeError)
    async def handle_application_runtime_error(
        _: Request, error: ApplicationRuntimeError
    ) -> JSONResponse:
        if isinstance(error, ApplicationRuntimeNotFound):
            status_code = 404
            message = "未找到该 Application 运行实例。"
        elif isinstance(error, ApplicationIdempotencyConflict):
            status_code = 409
            message = "client_request_id 已用于不同的 Application 运行操作。"
        elif isinstance(error, ApplicationQueueFull):
            status_code = 429
            message = "Application 运行队列已满，请稍后重试。"
        elif isinstance(error, ApplicationRuntimeClosed):
            status_code = 503
            message = "Application 运行时当前不可用。"
        else:
            status_code = 409
            message = "Application 运行请求无法执行。"
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": error.code, "message": message}},
        )

    @app.exception_handler(GameLearningError)
    async def handle_game_learning_error(
        _: Request, error: GameLearningError
    ) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.as_payload())

    @app.exception_handler(RequestValidationError)
    async def redact_executor_action_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default validation payload can contain rejected input.
        # Every write surface returns a redacted error instead of echoing it.
        if request.url.path.startswith("/api/execution/v1/"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "INVALID_EXECUTION_REQUEST",
                        "message": "The Harness execution request is invalid.",
                    }
                },
            )
        if request.url.path == "/api/v2/goals" or request.url.path.startswith(
            "/api/v2/goals/"
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_goal_request",
                        "message": "GoalRun 请求无效。",
                    }
                },
            )
        if request.url.path == "/api/v1/executor/actions":
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_executor_action",
                        "message": "受限 ADB 动作请求无效。",
                    }
                },
            )
        if request.url.path.startswith("/api/v1/chat/"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_chat_request",
                        "message": "聊天请求无效。",
                    }
                },
            )
        if request.url.path.startswith("/api/v1/settings/cloud"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_cloud_chat_config",
                        "message": "云端模型配置无效。",
                    }
                },
            )
        if request.url.path.startswith("/api/v1/learning/"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_game_learning_request",
                        "message": "游戏学习请求无效。",
                    }
                },
            )
        if request.url.path == "/api/v1/tasks" or request.url.path.startswith(
            "/api/v1/tasks/"
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_mobile_task_request",
                        "message": "智能任务请求无效。",
                    }
                },
            )
        if request.url.path == "/api/v1/runs" or request.url.path.startswith(
            "/api/v1/runs/"
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_run_request",
                        "message": "运行请求无效。",
                    }
                },
            )
        if request.url.path == "/api/v1/approvals" or request.url.path.startswith(
            "/api/v1/approvals/"
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_approval_request",
                        "message": "审批请求无效。",
                    }
                },
            )
        if request.url.path == "/api/v1/application-instances" or (
            request.url.path.startswith("/api/v1/application-instances/")
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_application_runtime_request",
                        "message": "Application 运行请求无效。",
                    }
                },
            )
        if request.method == "POST" and request.url.path.startswith("/api/v1/"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_api_request",
                        "message": "请求无效。",
                    }
                },
            )
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, error)

    router = APIRouter(prefix="/api/v1")

    @router.get("/health", response_model=HealthResponse)
    def api_health():
        return service.health()

    @router.post("/shutdown", status_code=202)
    def request_console_shutdown(request: Request):
        token = resolved_settings.console_shutdown_token
        if token is None or not hmac.compare_digest(
            request.headers.get(CONSOLE_SHUTDOWN_TOKEN_HEADER, ""), token
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "console_shutdown_forbidden",
                        "message": "控制台关闭请求未获授权。",
                    }
                },
            )
        callback = app.state.console_shutdown_callback
        if not callable(callback):
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "console_shutdown_unavailable",
                        "message": "当前控制台不支持优雅关闭。",
                    }
                },
            )
        callback()
        return {"status": "accepted"}

    @router.get("/overview", response_model=OverviewResponse)
    def overview():
        return service.overview()

    @router.get("/targets", response_model=TargetListResponse)
    def list_targets():
        items = service.list_targets()
        return {"items": items, "count": len(items)}

    @router.post("/targets/discover", response_model=TargetDiscoveryResponse)
    def discover_targets():
        view = service.discover_targets()
        return {
            "items": view.targets,
            "count": len(view.targets),
            "discovery": {
                "adb_status": view.discovery.status,
                "adb_path": view.discovery.adb_path,
                "message": view.discovery.message,
                "device_count": len(view.discovery.targets),
            },
        }

    @router.get("/workflows", response_model=WorkflowListResponse)
    def list_workflows():
        items = service.list_workflows()
        return {"items": items, "count": len(items)}

    @router.get("/runs", response_model=RunListResponse)
    def list_runs(limit: int = Query(default=100, ge=1, le=500)):
        items = service.list_runs(limit=limit)
        return {"items": items, "count": len(items)}

    @router.post("/runs", status_code=201, response_model=RunSummarySchema)
    def create_run(request: RunCreate):
        return service.create_run(request)

    @router.get("/runs/{run_id}", response_model=RunDetailSchema)
    def get_run(run_id: str):
        return service.get_run(run_id)

    @router.post("/runs/{run_id}/actions", response_model=RunSummarySchema)
    def act_on_run(run_id: str, request: RunActionRequest):
        return service.act_on_run(run_id, request.action)

    @router.post(
        "/executor/actions",
        status_code=202,
        response_model=ExecutorActionResponse,
    )
    def execute_adb_action(request: ExecutorActionRequest):
        return service.execute_adb_action(request)

    @router.get("/approvals", response_model=ApprovalListResponse)
    def list_approvals():
        items = service.list_approvals()
        return {"items": items, "count": len(items)}

    @router.post(
        "/approvals/{approval_id}/decision",
        response_model=ApprovalDecisionResponse,
    )
    def decide_approval(approval_id: str, request: ApprovalDecisionRequest):
        approval, run = service.decide_approval(approval_id, request)
        return {"approval": approval, "run": run}

    @router.get("/events", response_model=EventListResponse)
    def list_events(
        response: Response,
        limit: int = Query(default=100, ge=1, le=500),
        run_id: str | None = Query(default=None),
    ):
        # Phase 7: 软弃用全局事件列表；任务级事件请改用 Gateway 契约 SSE 流
        response.headers["Deprecation"] = "true"
        items = service.list_events(limit=limit, run_id=run_id)
        return {"items": items, "count": len(items)}

    @router.get("/runtime", response_model=RuntimeResponse)
    def runtime():
        return service.runtime()

    @router.get("/runtime/mode", response_model=RuntimeModeResponse)
    def runtime_mode_view():
        # Phase 7: 运行时模式监控。运维据此判断切流进度（draining 期观察
        # active_legacy_task_count 归零后再切到 kernel_active）。
        active_count, active_ids = _active_legacy_tasks()
        return {
            "mode": runtime_mode_guard.mode,
            "legacy_writable": runtime_mode_guard.is_legacy_writable(),
            "kernel_active": runtime_mode_guard.is_kernel_active(),
            "draining": runtime_mode_guard.is_draining(),
            "active_legacy_task_count": active_count,
            "active_task_ids": active_ids,
        }

    @router.get(
        "/settings/cloud",
        response_model=CloudChatConfigResponse,
    )
    def get_cloud_chat_config():
        return resolved_cloud_configuration.public_view()

    @router.post(
        "/settings/cloud",
        response_model=CloudChatConfigResponse,
    )
    def save_cloud_chat_config(request: CloudChatConfigUpdate):
        return resolved_cloud_configuration.configure(
            endpoint=request.endpoint,
            model=request.model,
            api_key=request.api_key,
            expected_revision=request.expected_revision,
        )

    @router.post(
        "/settings/cloud/test",
        response_model=CloudConnectionTestResponse,
    )
    def test_cloud_chat_config():
        return resolved_cloud_configuration.test_connection()

    @router.post(
        "/settings/cloud/clear",
        response_model=CloudChatConfigResponse,
    )
    def clear_cloud_chat_config(request: CloudChatConfigRevision):
        return resolved_cloud_configuration.clear(
            expected_revision=request.expected_revision
        )

    @router.get("/chat/sessions", response_model=ChatSessionListResponse)
    def list_chat_sessions(limit: int = Query(default=100, ge=1, le=500)):
        items = resolved_chat.list_sessions(limit=limit)
        return {"items": items, "count": len(items)}

    @router.post(
        "/chat/sessions",
        status_code=201,
        response_model=ChatSessionSchema,
    )
    def create_chat_session(request: ChatSessionCreate):
        return resolved_chat.create_session(
            title=request.title,
            mode=request.mode,
            target_id=request.target_id,
            auto_execute=request.auto_execute,
        )

    @router.get(
        "/chat/sessions/{session_id}",
        response_model=ChatTranscriptResponse,
    )
    def get_chat_transcript(session_id: str):
        return resolved_chat.transcript(session_id)

    @router.post(
        "/chat/sessions/{session_id}/turns",
        status_code=202,
        response_model=ChatTurnSchema,
    )
    def send_chat_turn(session_id: str, request: ChatTurnCreate):
        return resolved_chat.send_turn(
            session_id=session_id,
            content=request.content,
            client_request_id=request.client_request_id,
        )

    @router.post(
        "/chat/turns/{turn_id}/cancel",
        status_code=202,
        response_model=ChatTurnSchema,
    )
    def cancel_chat_turn(turn_id: str):
        return resolved_chat.cancel_turn(turn_id)

    def require_application_runtime() -> Any:
        if resolved_application_runtime is None:
            raise ControlPlaneError(
                code="application_runtime_not_configured",
                message="Application 运行时尚未配置。",
                status_code=503,
            )
        return resolved_application_runtime

    def require_application_archive() -> Any:
        if resolved_application_archive is None:
            raise ControlPlaneError(
                code="application_runtime_history_not_configured",
                message="Application 运行历史尚未配置。",
                status_code=503,
            )
        return resolved_application_archive

    def invoke_application_writer(operation: str, callback):
        try:
            return callback()
        except ApplicationRuntimeError:
            raise
        except (TypeError, ValueError, RuntimeError) as error:
            raise ControlPlaneError(
                code=f"application_runtime_{operation}_rejected",
                message="Application 运行请求无法执行。",
                status_code=409,
            ) from error
        except Exception as error:
            raise ControlPlaneError(
                code=f"application_runtime_{operation}_unavailable",
                message="Application 运行时当前不可用。",
                status_code=503,
            ) from error

    def invoke_application_reader(callback):
        try:
            return callback()
        except ApplicationRuntimeError:
            raise
        except Exception as error:
            raise ControlPlaneError(
                code="application_runtime_history_unavailable",
                message="Application 运行历史当前不可用。",
                status_code=503,
            ) from error

    @router.post(
        "/application-instances",
        status_code=202,
        response_model=ApplicationInstanceSchema,
    )
    def create_application_instance(request: ApplicationInstanceCreate):
        runtime = require_application_runtime()
        state = invoke_application_writer(
            "start",
            lambda: runtime.start(
                request.profile_id,
                request.client_request_id,
                target_id=request.target_id,
                initial_input=request.initial_input,
            ),
        )
        return _application_instance_payload(state)

    @router.get(
        "/application-instances",
        response_model=ApplicationInstanceListResponse,
    )
    def list_application_instances(
        limit: int = Query(default=100, ge=1, le=500),
    ):
        archive = require_application_archive()
        states = invoke_application_reader(lambda: archive.list(limit=limit))
        items = [_application_instance_payload(item) for item in states]
        return {"items": items, "count": len(items)}

    @router.get(
        "/application-instances/{instance_id}",
        response_model=ApplicationInstanceSchema,
    )
    def inspect_application_instance(instance_id: str):
        archive = require_application_archive()
        state = invoke_application_reader(lambda: archive.inspect(instance_id))
        return _application_instance_payload(state)

    @router.post(
        "/application-instances/{instance_id}/commands",
        status_code=202,
        response_model=ApplicationInstanceSchema,
    )
    def command_application_instance(
        instance_id: str,
        request: ApplicationCommandCreate,
    ):
        if runtime_mode_guard.is_draining() and request.command != "Stop":
            raise ControlPlaneError(
                code="LEGACY_DEVICE_WRITE_DISABLED",
                message="Legacy 正在排空；仅允许停止存量 Application 实例。",
                status_code=403,
            )
        if request.command == "Input":
            command = ApplicationInput(request.content or "")
        elif request.command == "Pause":
            command = ApplicationPause()
        elif request.command == "Resume":
            command = ApplicationResume()
        else:
            command = ApplicationStop()
        runtime = require_application_runtime()
        state = invoke_application_writer(
            "command",
            lambda: runtime.command(
                instance_id,
                command,
                request.client_request_id,
            ),
        )
        return _application_instance_payload(state)

    def require_mobile_task_runtime() -> Any:
        if resolved_mobile_tasks is None:
            raise ControlPlaneError(
                code="mobile_task_runtime_not_configured",
                message="通用智能任务运行时尚未配置。",
                status_code=503,
            )
        return resolved_mobile_tasks

    def _reject_legacy_write(endpoint: str, error: RuntimeModeError) -> ControlPlaneError:
        """把守卫拒绝转成 403 契约错误，并打 warning 日志（含端点 + 模式）。"""
        logger.warning(
            "Legacy 写被运行时模式拒绝 endpoint=%s mode=%s reason=%s",
            endpoint,
            runtime_mode_guard.mode,
            str(error),
        )
        return ControlPlaneError(
            code="LEGACY_TASK_WRITE_DISABLED",
            message=str(error),
            status_code=403,
        )

    def enforce_legacy_writable() -> None:
        """新建 Legacy Task 门控（DRAINING/KERNEL_ACTIVE 拒绝）。"""
        try:
            runtime_mode_guard.require_legacy_writable()
        except RuntimeModeError as error:
            raise _reject_legacy_write("POST /tasks", error) from error

    def enforce_legacy_runtime_available(endpoint: str) -> None:
        """存量任务输入/停止门控（仅 KERNEL_ACTIVE 拒绝）。"""
        try:
            runtime_mode_guard.require_legacy_runtime_available()
        except RuntimeModeError as error:
            raise _reject_legacy_write(endpoint, error) from error

    def _active_legacy_tasks() -> tuple[int, list[str]]:
        """读取排空门禁数据；对不支持该查询的归档（如测试 Fake）返回 (0, [])。"""
        query = getattr(resolved_mobile_task_archive, "active_tasks", None)
        if not callable(query):
            return 0, []
        return query()

    @router.post(
        "/tasks",
        status_code=202,
        response_model=MobileTaskSchema,
    )
    def create_mobile_task(request: MobileTaskCreate):
        enforce_legacy_writable()
        runtime = require_mobile_task_runtime()
        return _mobile_task_payload(
            runtime.start(
                request.goal,
                request.client_request_id,
                target_id=request.target_id,
                skill_id=request.skill_id,
            )
        )

    @router.get("/tasks", response_model=MobileTaskListResponse)
    def list_mobile_tasks(limit: int = Query(default=100, ge=1, le=500)):
        items = [
            _mobile_task_payload(item)
            for item in resolved_mobile_task_archive.list(limit=limit)
        ]
        return {"items": items, "count": len(items)}

    @router.get("/tasks/{task_id}", response_model=MobileTaskSchema)
    def inspect_mobile_task(task_id: str):
        return _mobile_task_payload(resolved_mobile_task_archive.inspect(task_id))

    # U7 explicit, read-only compatibility namespace.  Once Gateway owns the
    # canonical /api/v1/tasks paths, old MobileTask history remains reachable
    # without payload sniffing or an ambiguous dual contract.
    compatibility_router = APIRouter(prefix="/api/compat/v1")

    @compatibility_router.get(
        "/mobile-tasks", response_model=MobileTaskListResponse
    )
    def list_legacy_mobile_task_archive(
        limit: int = Query(default=100, ge=1, le=500),
    ):
        items = [
            _mobile_task_payload(item)
            for item in resolved_mobile_task_archive.list(limit=limit)
        ]
        return {"items": items, "count": len(items)}

    @compatibility_router.get(
        "/mobile-tasks/{task_id}", response_model=MobileTaskSchema
    )
    def inspect_legacy_mobile_task_archive(task_id: str):
        return _mobile_task_payload(resolved_mobile_task_archive.inspect(task_id))

    @router.post(
        "/tasks/{task_id}/inputs",
        status_code=202,
        response_model=MobileTaskSchema,
    )
    def send_mobile_task_input(task_id: str, request: MobileTaskInputCreate):
        enforce_legacy_runtime_available(f"POST /tasks/{task_id}/inputs")
        return _mobile_task_payload(
            require_mobile_task_runtime().send(
                task_id,
                request.content,
                request.client_request_id,
            )
        )

    @router.post(
        "/tasks/{task_id}/stop",
        status_code=202,
        response_model=MobileTaskSchema,
    )
    def stop_mobile_task(task_id: str, request: MobileTaskStopRequest):
        enforce_legacy_runtime_available(f"POST /tasks/{task_id}/stop")
        return _mobile_task_payload(
            require_mobile_task_runtime().stop(task_id, request.client_request_id)
        )

    def require_game_learner() -> Any:
        if resolved_game_learner is None:
            raise ControlPlaneError(
                code="game_learner_unavailable",
                message="游戏学习模块尚未配置。",
                status_code=503,
            )
        return resolved_game_learner

    @router.get(
        "/learning/profiles",
        response_model=LearningProfileListResponse,
    )
    def list_learning_profiles():
        items = [
            _learning_profile_payload(item)
            for item in require_game_learner().list_profiles()
        ]
        return {"items": items, "count": len(items)}

    @router.post(
        "/learning/jobs",
        status_code=202,
        response_model=LearningJobSchema,
    )
    def create_learning_job(request: LearningJobCreate):
        learner = require_game_learner()
        job = learner.learn(
            request.instruction,
            client_request_id=request.client_request_id,
            profile_id=request.profile_id or "stzb-tutorial-v1",
            target_id=request.target_id,
        )
        return _learning_job_payload(job, list(learner.list_profiles()))

    @router.get(
        "/learning/jobs",
        response_model=LearningJobListResponse,
    )
    def list_learning_jobs(limit: int = Query(default=100, ge=1, le=500)):
        learner = require_game_learner()
        profiles = list(learner.list_profiles())
        items = [
            _learning_job_payload(item, profiles)
            for item in learner.list_jobs(limit=limit)
        ]
        return {"items": items, "count": len(items)}

    @router.get("/learning/jobs/{job_id}", response_model=LearningJobSchema)
    def inspect_learning_job(job_id: str):
        learner = require_game_learner()
        return _learning_job_payload(
            learner.inspect(job_id), list(learner.list_profiles())
        )

    @router.post(
        "/learning/jobs/{job_id}/stop",
        status_code=202,
        response_model=LearningJobSchema,
    )
    def stop_learning_job(job_id: str):
        learner = require_game_learner()
        return _learning_job_payload(
            learner.stop(job_id), list(learner.list_profiles())
        )

    # Kernel-active 正常装配会提供 Gateway；显式注入仍用于兼容测试。
    # 它注册在 Legacy 路由之前，使 canonical 路径稳定解析到新契约。
    if resolved_gateway is not None:
        app.include_router(create_gateway_router(resolved_gateway))
    app.include_router(
        create_execution_router(
            resolved_execution_contract,
            token=resolved_settings.harness_api_token,
        )
    )
    app.include_router(
        create_execution_v2_router(
            resolved_execution_contract_v2,
            token=resolved_settings.harness_api_token,
        )
    )
    app.include_router(create_goal_router(resolved_goal_service))
    app.include_router(create_agent_session_router(resolved_agent_session_service))
    app.include_router(create_user_fact_router(fact_question_coordinator))
    app.include_router(compatibility_router)
    app.include_router(router)
    # Week 6: Runtime Lease 管理 API（懒初始化，不产生额外数据库文件）
    app.include_router(create_lease_admin_router(resolved_runtime_admin))

    @app.get("/health", response_model=HealthResponse)
    def root_health():
        return service.health()

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def frontend_or_spa_fallback(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "api_route_not_found",
                        "message": "请求的 API 路由不存在。",
                    }
                },
            )

        dist = resolved_settings.frontend_dist
        index = dist / "index.html"
        if not dist.is_dir() or not index.is_file():
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "error": {
                        "code": "frontend_not_built",
                        "message": (
                            "控制台前端尚未构建。请先构建 "
                            "apps/console/frontend，再打开此路径。"
                        ),
                    },
                    "expected_path": str(index),
                },
            )

        candidate = _safe_static_candidate(dist, full_path)
        if candidate is not None and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def _safe_static_candidate(dist: Path, requested_path: str) -> Path | None:
    if not requested_path:
        return None
    dist_root = dist.resolve()
    candidate = (dist_root / requested_path).resolve()
    try:
        candidate.relative_to(dist_root)
    except ValueError:
        return None
    return candidate
