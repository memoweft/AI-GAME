from __future__ import annotations

import hmac
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
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
from .application_runtime.domain import (
    ApplicationRuntimeError,
    IdempotencyConflict as ApplicationIdempotencyConflict,
    QueueFull as ApplicationQueueFull,
    RuntimeClosed as ApplicationRuntimeClosed,
    RuntimeNotFound as ApplicationRuntimeNotFound,
)
from .chat import AndroidAutomationFactory, ChatCoordinator, ChatCoordinatorError
from .cloud_config import CloudChatConfiguration, CloudConfigError
from .config import Settings
from .discovery import AdbTargetDiscovery
from .device_lease import DeviceExecutionLease
from .execution import GuiExecutor
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
    MobileTaskAndroidDriver,
    OpenAICompatibleMobileRoleModel,
)
from .mobile_task_profiles import resolve_mobile_skill_scope
from .openai_chat import OpenAIChatProvider
from .repository import SQLiteRepository
from .runtime_admin import LeaseAdminService, create_lease_admin_router
from .soul_integration import SoulIntegration, SoulIntegrationError
from .soul_application_composition import (
    SoulApplicationUnavailable,
    compose_soul_application_runtime,
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
    SoulCommandRequest,
    SoulConversationResponse,
    SoulSchedulerStatusSchema,
    SoulWorkspaceResponse,
    RuntimeResponse,
    TargetDiscoveryResponse,
    TargetListResponse,
    WorkflowListResponse,
)
from .service import ControlPlaneError, ControlPlaneService


WRITE_CLIENT_HEADER = "console-v1"
CONSOLE_SHUTDOWN_TOKEN_HEADER = "X-AI-Game-Shutdown-Token"


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
    soul_integration: SoulIntegration | None = None,
    mobile_task_runtime: MobileTaskRuntime | Any | None = None,
    mobile_task_archive: MobileTaskArchive | Any | None = None,
    application_runtime: Any | None = None,
    application_runtime_archive: Any | None = None,
    runtime_admin: LeaseAdminService | None = None,
    console_shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_runtime_admin = runtime_admin or LeaseAdminService(
        resolved_settings.data_dir / "runtime" / "runtime.db"
    )
    resolved_repository = repository or SQLiteRepository(resolved_settings.database_path)
    resolved_cloud_configuration = cloud_configuration or CloudChatConfiguration(
        resolved_repository,
        resolved_settings,
    )
    resolved_executor = adb_executor or AdbGuiExecutor.from_settings(resolved_settings)
    resolved_soul_integration = soul_integration or SoulIntegration.from_settings(resolved_settings)
    resolved_mobile_tasks = mobile_task_runtime
    resolved_application_runtime = application_runtime
    resolved_application_archive = application_runtime_archive
    if (
        resolved_application_runtime is None
        and resolved_application_archive is None
    ):
        application_composition = compose_soul_application_runtime(
            resolved_settings,
            resolved_cloud_configuration,
        )
        resolved_application_runtime = application_composition.runtime
        resolved_application_archive = application_composition.archive
    elif resolved_application_archive is None:
        resolved_application_archive = resolved_application_runtime
    device_execution_lease = DeviceExecutionLease()
    if (
        resolved_mobile_tasks is None
        and isinstance(resolved_executor, AdbGuiExecutor)
        and resolved_settings.gui_executor_enabled
        and resolved_settings.adb_path
        and resolved_settings.local_chat_endpoint
        and resolved_settings.local_chat_model
    ):
        mobile_evidence = LocalMobileEvidenceStore(
            resolved_settings.project_root
            / "runtime"
            / "sessions"
            / "mobile-tasks"
            / "evidence"
        )
        resolved_mobile_tasks = MobileTaskRuntime(
            resolved_settings.data_dir / "mobile-tasks.db",
            driver=MobileTaskAndroidDriver(
                repository=resolved_repository,
                executor=resolved_executor,
                evidence=mobile_evidence,
                device_lease=device_execution_lease,
            ),
            model=OpenAICompatibleMobileRoleModel(
                endpoint=resolved_settings.local_chat_endpoint,
                model=resolved_settings.local_chat_model,
                api_key=resolved_settings.local_chat_api_key,
                timeout_seconds=resolved_settings.chat_request_timeout_seconds,
                evidence=mobile_evidence,
            ),
            # Production tasks may span a long game session. These are only
            # runaway guards; ordinary recovery is driven by visual progress,
            # reflection, owner input, or an explicit stop.
            max_reflections=64,
            max_attempts=2_048,
            queue_capacity=32,
            scope_resolver=resolve_mobile_skill_scope,
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
        adb_discovery=adb_discovery,
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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        chat_start_attempted = False
        game_start_attempted = False
        try:
            service.initialize()
            resolved_cloud_configuration.start()
            application_startup = getattr(
                resolved_application_runtime, "startup", None
            )
            if callable(application_startup):
                try:
                    application_startup()
                except SoulApplicationUnavailable:
                    # The archive remains usable and a later start/command
                    # retries activation against current cloud/owner state.
                    pass
            chat_start_attempted = True
            resolved_chat.start()
            if resolved_game_learner is not None:
                game_start_attempted = True
                resolved_game_learner.start()
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
            if game_start_attempted and resolved_game_learner is not None:
                shutdown_callbacks.append(resolved_game_learner.shutdown)
            if chat_start_attempted:
                shutdown_callbacks.append(resolved_chat.shutdown)
            mobile_task_shutdown = getattr(
                resolved_mobile_tasks, "shutdown", None
            )
            if callable(mobile_task_shutdown):
                shutdown_callbacks.append(mobile_task_shutdown)
            # Week 6: 停止 Lease 后台清理并关闭 runtime.db（未初始化时为空操作）
            shutdown_callbacks.append(resolved_runtime_admin.shutdown)
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

    app = FastAPI(
        title="AI-GAME Local Console",
        version="0.1.0",
        description="Local-only control plane with explicitly restricted ADB input actions.",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.repository = resolved_repository
    app.state.control_plane = service
    app.state.chat_coordinator = resolved_chat
    app.state.cloud_configuration = resolved_cloud_configuration
    app.state.game_learner = resolved_game_learner
    app.state.device_execution_lease = device_execution_lease
    app.state.soul_integration = resolved_soul_integration
    app.state.mobile_task_runtime = resolved_mobile_tasks
    app.state.mobile_task_archive = resolved_mobile_task_archive
    app.state.application_runtime = resolved_application_runtime
    app.state.application_runtime_archive = resolved_application_archive
    app.state.runtime_admin = resolved_runtime_admin
    app.state.console_shutdown_callback = console_shutdown_callback

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @app.middleware("http")
    async def require_console_client_for_writes(request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith("/api/v1/"):
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
        elif isinstance(error, SoulApplicationUnavailable):
            status_code = 503
            message = "Soul Application 运行依赖当前不可用。"
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

    @app.exception_handler(SoulIntegrationError)
    async def handle_soul_integration_error(
        _: Request, error: SoulIntegrationError
    ) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.as_payload())

    @app.exception_handler(RequestValidationError)
    async def redact_executor_action_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default validation payload can contain rejected input.
        # Every write surface returns a redacted error instead of echoing it.
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
        if request.url.path in {
            "/api/v1/soul/commands",
            "/api/v1/integrations/soul/commands",
        }:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_legacy_soul_request",
                        "message": "旧 Soul 写请求无效。",
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
                "device_count": len(view.discovery.devices),
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
        limit: int = Query(default=100, ge=1, le=500),
        run_id: str | None = Query(default=None),
    ):
        items = service.list_events(limit=limit, run_id=run_id)
        return {"items": items, "count": len(items)}

    @router.get("/runtime", response_model=RuntimeResponse)
    def runtime():
        return service.runtime()

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

    @router.get(
        "/application-profiles/soul-reply-v1/scheduler",
        response_model=SoulSchedulerStatusSchema,
    )
    def inspect_soul_application_scheduler():
        runtime = require_application_runtime()
        status_reader = getattr(runtime, "scheduler_status", None)
        if not callable(status_reader):
            raise ControlPlaneError(
                code="soul_scheduler_status_unavailable",
                message="Soul 全天调度状态当前不可用。",
                status_code=503,
            )
        return invoke_application_reader(status_reader)

    @router.post(
        "/application-instances/{instance_id}/commands",
        status_code=202,
        response_model=ApplicationInstanceSchema,
    )
    def command_application_instance(
        instance_id: str,
        request: ApplicationCommandCreate,
    ):
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

    @router.post(
        "/tasks",
        status_code=202,
        response_model=MobileTaskSchema,
    )
    def create_mobile_task(request: MobileTaskCreate):
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

    @router.post(
        "/tasks/{task_id}/inputs",
        status_code=202,
        response_model=MobileTaskSchema,
    )
    def send_mobile_task_input(task_id: str, request: MobileTaskInputCreate):
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
        return _mobile_task_payload(
            require_mobile_task_runtime().stop(task_id, request.client_request_id)
        )

    @router.get(
        "/integrations/soul",
        response_model=SoulWorkspaceResponse,
    )
    def soul_workspace():
        snapshot = resolved_soul_integration.workspace()
        return {**snapshot, "available_commands": []}

    @router.get(
        "/integrations/soul/conversations/{conversation_id}",
        response_model=SoulConversationResponse,
    )
    def soul_conversation(conversation_id: str):
        return resolved_soul_integration.conversation(conversation_id)

    @router.post("/soul/commands", status_code=410)
    @router.post("/integrations/soul/commands", status_code=410)
    def soul_command(request: SoulCommandRequest):
        del request
        raise ControlPlaneError(
            code="legacy_soul_write_disabled",
            message="旧 Soul 写入口已停用，请使用 ApplicationRuntime。",
            status_code=410,
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
