"""Durable generic Mobile Task Runtime.

The task Interface is ``MobileTaskRuntime.start/send/stop/inspect/list``;
``shutdown`` is its explicit process-lifecycle hook.
Device transport and model roles enter through the injected ``TaskDriver`` and
``RoleModel`` ports, keeping this Module independent of ADB and HTTP.
"""

from .domain import (
    ActionAttempt,
    ActionDecision,
    DecisionContext,
    IdempotencyConflict,
    InputRevision,
    MobileTaskError,
    MobileTaskState,
    Observation,
    PhysicalIntent,
    PlanContext,
    PlanDraft,
    Reflection,
    ReflectionContext,
    ReflectionDecision,
    RoleModel,
    SkillMemory,
    SkillScopeResolver,
    Subgoal,
    TaskDriver,
    TaskSession,
    TaskEvent,
    TaskNotFound,
    TaskPlan,
    TaskQueueFull,
    TaskRuntimeClosed,
    TaskStateConflict,
    TransportReceipt,
    Verification,
    VerificationContext,
)
from .runtime import MobileTaskRuntime
from .archive import MobileTaskArchive

__all__ = [
    "ActionAttempt",
    "ActionDecision",
    "DecisionContext",
    "IdempotencyConflict",
    "InputRevision",
    "MobileTaskError",
    "MobileTaskArchive",
    "MobileTaskRuntime",
    "MobileTaskState",
    "Observation",
    "PhysicalIntent",
    "PlanContext",
    "PlanDraft",
    "Reflection",
    "ReflectionContext",
    "ReflectionDecision",
    "RoleModel",
    "SkillMemory",
    "SkillScopeResolver",
    "Subgoal",
    "TaskDriver",
    "TaskSession",
    "TaskEvent",
    "TaskNotFound",
    "TaskPlan",
    "TaskQueueFull",
    "TaskRuntimeClosed",
    "TaskStateConflict",
    "TransportReceipt",
    "Verification",
    "VerificationContext",
]
