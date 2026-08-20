"""HTTP surface for the frozen Gateway contract (Phase 6, Weeks 3-4).

Implements the §2 canonical paths on a standalone ``APIRouter`` that the
app factory mounts **only when a ``GatewayComposition`` is explicitly
passed** (default OFF: the default composition stays byte-identical to
the legacy app). When enabled, the gateway router is registered *before*
the legacy router, so the conflicting canonical paths (``POST /tasks``,
``GET /tasks``, ``GET /tasks/{task_id}``) resolve to the new contract —
registration-order precedence previews the cutover end state; the legacy
duplicates are removed by the separate cutover work order (§2: no payload
sniffing, no hybrid mounting).

Contract rules enforced at this layer:

* Bodies are parsed manually (``request.json()``) — no Pydantic models —
  so every malformed request returns the §14 ``VALIDATION_ERROR`` shape
  instead of a framework 422 payload.
* ``Idempotency-Key`` is required for create / message / control (§3);
  ``X-Client-Id`` is required for create and the conversation entry.
* ``GET /devices`` returns the Week-3 port-only shape
  ``{"items": [{"id", "availability"}]}``; the richer §4 fields are
  deferred to the lease-layer work order.
* Event pagination (§10) is a task-scoped, strictly ascending sequence
  cursor with ``next_after_sequence`` = last item (or the cursor on an
  empty page).
* The SSE stream (§11) is a read-only, task-scoped sequence cursor:
  ``id:`` carries the sequence so clients reconnect with
  ``after_sequence`` = last successfully processed id. Heartbeats are
  comment frames — they consume no Task sequence. The stream only ever
  reads the committed event log (slow clients cannot block the Kernel),
  and a delivery failure never rolls back committed Runtime Events;
  the client reconnects and calibrates against a fresh Snapshot (§11).
* Every unhandled exception becomes ``INTERNAL_ERROR`` — no traceback,
  ADB, or vendor detail may cross the API boundary (§14).
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings
from .discovery import AdbTargetDiscovery
from .gateway import (
    DeviceRegistry,
    EventCursorInvalid,
    GatewayError,
    GatewayStore,
    IdempotencyService,
    InternalError,
    TaskGateway,
    TaskNotFound,
    ValidationError,
    build_task_snapshot,
)
from .runtime_adapters.android import AndroidObservationProvider, AdbDeviceRegistry
from .runtime_adapters.artifacts import FilesystemArtifactStore
from .runtime_adapters.sqlite import SQLiteRuntimeStore
from .runtime_kernel import (
    ChannelAvailability,
    RecordNotFound,
    RuntimeEvent,
    RuntimeKernel,
)

__all__ = [
    "GATEWAY_ERROR_STATUS",
    "GatewayComposition",
    "build_gateway_composition",
    "create_gateway_router",
    "gateway_error_handler",
]

# §14 code -> HTTP status. The contract freezes the error *shape* and the
# ten codes; the status mapping is the Week-3 design decision documented
# in PHASE_6_GATEWAY_CONTRACT_PLAN.md.
GATEWAY_ERROR_STATUS: dict[str, int] = {
    "VALIDATION_ERROR": 400,
    "EVENT_CURSOR_INVALID": 400,
    "TASK_NOT_FOUND": 404,
    "DEVICE_NOT_FOUND": 404,
    "TASK_NOT_ACTIVE": 409,
    "CONVERSATION_CONFLICT": 409,
    "DEVICE_NOT_AVAILABLE": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "LEGACY_TASK_WRITE_DISABLED": 403,
    "INTERNAL_ERROR": 500,
}

DEFAULT_EVENT_PAGE_LIMIT = 100
MAX_EVENT_PAGE_LIMIT = 500

# §11: the stream polls the committed event log on a fixed cadence.
# Injectable via ``create_gateway_router(..., poll_interval=...)`` for
# tests; the production default keeps stream traffic minimal.
SSE_POLL_INTERVAL_SECONDS = 15.0
SSE_EVENT_NAME = "runtime_event"


def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    """Render any GatewayError as the frozen §14 payload, no leakage."""
    return JSONResponse(
        status_code=GATEWAY_ERROR_STATUS.get(exc.code, 500),
        content={"error": exc.to_dict()},
    )


@dataclass(frozen=True, slots=True)
class GatewayComposition:
    """The live gateway runtime owned by one app instance.

    ``poll_interval`` tunes the §11 SSE stream's poll cadence (seconds);
    tests shorten it so heartbeat/resume scenarios run in milliseconds.

    ``create_app(gateway=composition)`` mounts the §2 routes, registers
    the §14 error handler, and closes ``store`` then ``kernel`` on
    shutdown. The composition is passed explicitly — the default app
    factory never builds one.
    """

    kernel: RuntimeKernel
    gateway: TaskGateway
    device_registry: DeviceRegistry
    store: GatewayStore
    poll_interval: float = SSE_POLL_INTERVAL_SECONDS


def build_gateway_composition(
    *,
    settings: Settings,
    kernel: RuntimeKernel | None = None,
    store: GatewayStore | None = None,
    device_registry: DeviceRegistry | None = None,
) -> GatewayComposition:
    """Production wiring for the gateway on the shared runtime directory.

    Phase 7 cutover wiring: the ``AdbDeviceRegistry`` receives the
    Kernel's ``active_leased_device_ids`` as its active-device overlay,
    so any device the Kernel currently holds via an unexpired Lease is
    reported ``IN_USE`` in the device catalog and rejected with
    ``DEVICE_NOT_AVAILABLE`` (409) on ``POST /tasks`` — device
    exclusivity is now enforced end-to-end once the runtime mode is
    ``kernel_active``.
    """
    runtime_dir = settings.data_dir / "runtime"
    if kernel is None:
        kernel = RuntimeKernel(
            SQLiteRuntimeStore(runtime_dir / "runtime.db"),
            observation_provider=(
                AndroidObservationProvider(adb_path=settings.adb_path)
                if settings.adb_path
                else None
            ),
            artifact_store=FilesystemArtifactStore(runtime_dir / "artifacts"),
        )
    if store is None:
        store = GatewayStore(runtime_dir / "gateway.db")
        store.initialize()
    if device_registry is None:
        device_registry = AdbDeviceRegistry(
            AdbTargetDiscovery(adb_path=settings.adb_path),
            active_device_ids=kernel.active_leased_device_ids,
        )
    gateway = TaskGateway(
        kernel=kernel,
        idempotency=IdempotencyService(store),
        device_registry=device_registry,
    )
    return GatewayComposition(
        kernel=kernel,
        gateway=gateway,
        device_registry=device_registry,
        store=store,
    )


# -- request helpers (manual body parsing keeps §14 total) ------------------


def _header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    return value.strip() if isinstance(value, str) else ""


def _require_idempotency_key(request: Request) -> str:
    key = _header(request, "Idempotency-Key")
    if not key:
        raise ValidationError("Idempotency-Key header is required.")
    return key


async def _json_object_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise ValidationError("Request body must be a JSON object.") from None
    if not isinstance(body, dict):
        raise ValidationError("Request body must be a JSON object.")
    return body


def _parse_after_sequence(value: str | None) -> int:
    if value is None or value.strip() == "":
        return 0
    try:
        cursor = int(value.strip())
    except ValueError:
        raise EventCursorInvalid("after_sequence must be an integer.") from None
    if cursor < 0:
        raise EventCursorInvalid("after_sequence must not be negative.")
    return cursor


def _parse_limit(value: str | None) -> int:
    if value is None or value.strip() == "":
        return DEFAULT_EVENT_PAGE_LIMIT
    try:
        limit = int(value.strip())
    except ValueError:
        raise ValidationError("limit must be an integer.") from None
    if not 1 <= limit <= MAX_EVENT_PAGE_LIMIT:
        raise ValidationError(f"limit must be between 1 and {MAX_EVENT_PAGE_LIMIT}.")
    return limit


def _availability(status: str) -> str:
    """Registry status -> contract §4 lowercase availability."""
    if status == "AVAILABLE":
        return "available"
    if status == "IN_USE":
        return "in_use"
    return "unavailable"


def _event_to_dict(event: RuntimeEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "sequence": event.sequence,
        "type": event.type,
        "actor": event.actor.value,
        "payload": dict(event.payload),
        "created_at": event.created_at,
    }


def _sse_event_payload(event: RuntimeEvent) -> dict[str, Any]:
    """Frozen §11 ``data:`` shape: sequence / type / payload only.

    Raw screenshots and UI Trees never cross the stream — kernel event
    payloads reference observations by id, and this projection keeps the
    wire format to the three contract keys.
    """
    return {
        "sequence": event.sequence,
        "type": event.type,
        "payload": dict(event.payload),
    }


def _guarded(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Convert any non-gateway exception into a frozen INTERNAL_ERROR."""

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except GatewayError:
            raise
        except Exception:
            # Never leak tracebacks, vendor output, or framework internals (§14).
            raise InternalError("Gateway failed to process the request.") from None

    return wrapper


def create_gateway_router(composition: GatewayComposition) -> APIRouter:
    """Build the §2 canonical router for one gateway composition."""
    kernel = composition.kernel
    gateway = composition.gateway
    poll_interval = composition.poll_interval
    router = APIRouter(prefix="/api/v1")

    # -- devices (contract §4, Week-3 port-only shape) --------------------

    @router.get("/devices")
    @_guarded
    async def list_devices() -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": device.device_id,
                    "availability": _availability(device.status),
                }
                for device in composition.device_registry.list_devices()
            ]
        }

    # -- tasks (contract §5 / §6) ------------------------------------------

    @router.post("/tasks")
    @_guarded
    async def create_task(request: Request) -> dict[str, Any]:
        body = await _json_object_body(request)
        return gateway.create_task(
            client_id=_header(request, "X-Client-Id"),
            goal=body.get("goal"),
            device_id=body.get("device_id"),
            conversation_id=body.get("conversation_id"),
            message_id=body.get("message_id"),
            idempotency_key=_require_idempotency_key(request),
        )

    @router.get("/tasks")
    @_guarded
    async def list_tasks() -> dict[str, Any]:
        tasks = kernel.list_tasks()
        return {
            "items": [build_task_snapshot(kernel, task).to_dict() for task in tasks],
            "count": len(tasks),
        }

    @router.get("/tasks/{task_id}")
    @_guarded
    async def get_task(task_id: str) -> dict[str, Any]:
        return gateway.get_task(task_id)

    # -- task messages (contract §7) ----------------------------------------

    @router.post("/tasks/{task_id}/messages")
    @_guarded
    async def send_task_message(task_id: str, request: Request) -> dict[str, Any]:
        body = await _json_object_body(request)
        return gateway.send_message(
            task_id=task_id,
            message_id=body.get("message_id"),
            conversation_id=body.get("conversation_id"),
            text=body.get("text"),
            idempotency_key=_require_idempotency_key(request),
        )

    # -- task controls (contract §9) -----------------------------------------

    @router.post("/tasks/{task_id}/controls")
    @_guarded
    async def send_task_control(task_id: str, request: Request) -> dict[str, Any]:
        body = await _json_object_body(request)
        return gateway.control(
            task_id=task_id,
            command=body.get("command"),
            reason=body.get("reason"),
            idempotency_key=_require_idempotency_key(request),
        )

    # -- task events (contract §10) ------------------------------------------

    @router.get("/tasks/{task_id}/events")
    @_guarded
    async def list_task_events(
        task_id: str,
        after_sequence: str | None = None,
        limit: str | None = None,
    ) -> dict[str, Any]:
        cursor = _parse_after_sequence(after_sequence)
        page_limit = _parse_limit(limit)
        try:
            # Task existence check first: list_events would silently return
            # an empty page for an unknown task, masking the 404.
            kernel.load_task(task_id)
        except RecordNotFound:
            raise TaskNotFound(f"Task {task_id} was not found.") from None
        page = kernel.events(task_id, after_sequence=cursor)[:page_limit]
        return {
            "items": [_event_to_dict(event) for event in page],
            "next_after_sequence": page[-1].sequence if page else cursor,
        }

    # -- task event stream (contract §11, SSE) --------------------------------

    @router.get("/tasks/{task_id}/events/stream")
    @_guarded
    async def stream_task_events(
        task_id: str,
        after_sequence: str | None = None,
    ):
        # Validation is synchronous, *before* streaming starts, so §14
        # errors (404 TASK_NOT_FOUND / 400 EVENT_CURSOR_INVALID) come back
        # as normal JSON responses — never as a broken stream.
        cursor = _parse_after_sequence(after_sequence)
        try:
            kernel.load_task(task_id)
        except RecordNotFound:
            raise TaskNotFound(f"Task {task_id} was not found.") from None

        async def stream():
            last_sequence = cursor
            while True:
                events = kernel.events(task_id, after_sequence=last_sequence)
                if not events:
                    # Heartbeat: a comment frame. Not a Runtime Event and
                    # consumes no Task sequence (§11).
                    yield ": heartbeat\n\n"
                else:
                    for event in events:
                        last_sequence = event.sequence
                        data = json.dumps(
                            _sse_event_payload(event), ensure_ascii=False
                        )
                        yield f"id: {event.sequence}\n"
                        yield f"event: {SSE_EVENT_NAME}\n"
                        yield f"data: {data}\n"
                        yield "\n"
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # Disable proxy buffering (nginx) so events flush promptly.
                "X-Accel-Buffering": "no",
            },
        )

    # -- task observations (contract §12) -------------------------------------

    @router.get("/tasks/{task_id}/observations/{observation_id}")
    @_guarded
    async def get_observation(task_id: str, observation_id: str) -> dict[str, Any]:
        try:
            kernel.load_task(task_id)
            observation = kernel.load_observation(observation_id)
        except RecordNotFound:
            raise TaskNotFound(
                f"Observation {observation_id} was not found for task {task_id}."
            ) from None
        if observation.task_id != task_id:
            raise TaskNotFound(
                f"Observation {observation_id} does not belong to task {task_id}."
            ) from None
        screenshot = observation.screenshot
        return {
            "observation": {
                "id": observation.id,
                "captured_at": observation.captured_at,
                # Committed observations always have an AVAILABLE screenshot
                # channel (domain invariant), so the URL is unconditional.
                "screenshot_url": (
                    f"/api/v1/artifacts/{screenshot.artifact.reference}"
                ),
                "width": screenshot.width,
                "height": screenshot.height,
                "foreground_app": observation.device_state.foreground_app,
                "ui_tree_available": (
                    observation.ui_tree.status is ChannelAvailability.AVAILABLE
                ),
                "consistency_status": observation.consistency.status.value,
            }
        }

    # -- conversation entry (contract §8, POST) -------------------------------

    @router.post("/conversations/{conversation_id}/messages")
    @_guarded
    async def post_conversation_message(
        conversation_id: str, request: Request
    ) -> dict[str, Any]:
        body = await _json_object_body(request)
        return gateway.send_conversation_message(
            client_id=_header(request, "X-Client-Id"),
            conversation_id=conversation_id,
            message_id=body.get("message_id"),
            text=body.get("text"),
            device_id=body.get("device_id"),
            idempotency_key=_require_idempotency_key(request),
        )

    return router
