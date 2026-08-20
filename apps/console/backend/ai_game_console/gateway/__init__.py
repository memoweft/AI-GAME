"""Gateway application service for the frozen Phase 6 contract.

The Gateway sits between HTTP clients and the Runtime Kernel:

* ``TaskGateway`` — create/get/message/control/conversation entry
  (contract §5-§9), the only entry point for Task mutations;
* ``IdempotencyService`` — per-scope idempotency keys with canonical
  replay (contract §3);
* ``ConversationService`` — unique conversation <-> active Task
  association (contract §8);
* ``build_task_snapshot`` — the §6 Gateway Snapshot projection;
* ``GatewayError`` subclasses — the §14 error code surface.

The Gateway never touches Kernel Store tables, Device Leases, or raw
device channels (contract §15).
"""

from .conversations import ConversationService
from .device_registry import DeviceRegistry, DeviceSummary
from .errors import (
    ConversationConflict,
    DeviceNotAvailable,
    DeviceNotFound,
    EventCursorInvalid,
    GatewayError,
    IdempotencyConflict,
    InternalError,
    LegacyTaskWriteDisabled,
    TaskNotActive,
    TaskNotFound,
    ValidationError,
)
from .idempotency import (
    SCOPE_CONVERSATION_MESSAGE,
    SCOPE_TASK_CONTROL,
    SCOPE_TASK_CREATE,
    SCOPE_TASK_MESSAGE,
    IdempotencyService,
    canonical_payload_hash,
)
from .snapshot import (
    CompletedStageProjection,
    StageProjection,
    TaskSnapshot,
    VerifiedFactProjection,
    build_task_snapshot,
)
from .store import GatewayStore, GatewayStoreConflict, IdempotencyRecord
from .task_gateway import TaskGateway

__all__ = [
    "CompletedStageProjection",
    "ConversationConflict",
    "ConversationService",
    "DeviceNotAvailable",
    "DeviceNotFound",
    "DeviceRegistry",
    "DeviceSummary",
    "EventCursorInvalid",
    "GatewayError",
    "GatewayStore",
    "GatewayStoreConflict",
    "IdempotencyConflict",
    "IdempotencyRecord",
    "IdempotencyService",
    "InternalError",
    "LegacyTaskWriteDisabled",
    "SCOPE_CONVERSATION_MESSAGE",
    "SCOPE_TASK_CONTROL",
    "SCOPE_TASK_CREATE",
    "SCOPE_TASK_MESSAGE",
    "StageProjection",
    "TaskGateway",
    "TaskNotActive",
    "TaskNotFound",
    "TaskSnapshot",
    "ValidationError",
    "VerifiedFactProjection",
    "build_task_snapshot",
    "canonical_payload_hash",
]
