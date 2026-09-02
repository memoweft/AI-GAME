"""FastAPI v3 AgentSession routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import AgentRuntimeError, domain_dict
from .event_router import SessionEventRouter
from .projection import event_projection
from .service import AgentSessionService


_RESERVED_DEVICE_INGRESS_NAMESPACES = frozenset(
    {
        "android-companion-v1",
        "device-body",
        "pc-companion-watchdog",
    }
)


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreate(_ApiModel):
    instruction: str = Field(min_length=1, max_length=10_000)
    client_request_id: str = Field(min_length=1, max_length=128)

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        return _text(value)

    @field_validator("client_request_id")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _inert_key(value)


class SessionMessageCreate(_ApiModel):
    content: str = Field(min_length=1, max_length=10_000)
    client_request_id: str = Field(min_length=1, max_length=128)
    directive_kind: Literal["add", "revise", "reprioritize"] = "add"

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return _text(value)

    @field_validator("client_request_id")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _inert_key(value)


class SessionControlCreate(_ApiModel):
    action: Literal["pause", "resume", "takeover", "stop"]
    client_request_id: str = Field(min_length=1, max_length=128)

    @field_validator("client_request_id")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _inert_key(value)


class SessionEventCreate(_ApiModel):
    source_namespace: str = Field(min_length=1, max_length=256)
    source_event_id: str = Field(min_length=1, max_length=256)
    event_type: Literal[
        "UserDirectiveEvent",
        "GoalStateChangedEvent",
        "NotificationPostedEvent",
        "NotificationRemovedEvent",
        "ForegroundApplicationChangedEvent",
        "HumanTouchStartedEvent",
        "HumanTouchEndedEvent",
        "HumanIdleEvent",
        "ScreenStateChangedEvent",
        "LockStateChangedEvent",
        "NetworkStateChangedEvent",
        "OrientationChangedEvent",
        "TimerDueEvent",
        "DeviceBusyEvent",
        "DeviceAvailableEvent",
        "SchedulerRecoveryEvent",
    ]
    occurred_at: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]
    device_id: str | None = Field(default=None, max_length=256)
    device_boot_id: str | None = Field(default=None, max_length=256)
    source_cursor: str | None = Field(default=None, max_length=256)

    @field_validator("source_namespace", "source_event_id")
    @classmethod
    def normalize_source_identity(cls, value: str) -> str:
        return _text(value)

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: str) -> str:
        value = value.strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("occurred_at must be an ISO-8601 UTC timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise ValueError("occurred_at must be timezone-aware UTC")
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @model_validator(mode="after")
    def reject_reserved_device_ingress_provenance(self) -> SessionEventCreate:
        namespace = self.source_namespace.casefold()
        if (
            namespace in _RESERVED_DEVICE_INGRESS_NAMESPACES
            or namespace.startswith("device:")
        ):
            raise ValueError(
                "source_namespace is reserved for trusted DeviceBody/Companion ingress"
            )
        supplied_provenance = [
            name
            for name in ("device_id", "device_boot_id", "source_cursor")
            if getattr(self, name) is not None
        ]
        if supplied_provenance:
            raise ValueError(
                "device provenance fields are reserved for trusted "
                f"DeviceBody/Companion ingress: {', '.join(supplied_provenance)}"
            )
        return self


def agent_runtime_error_handler(_: Request, error: AgentRuntimeError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.as_payload())


def create_agent_session_router(service: AgentSessionService) -> APIRouter:
    router = APIRouter(prefix="/api/v3/sessions", tags=["sessions-v3"])
    event_router = SessionEventRouter(service.store)

    @router.post("", status_code=202)
    def create_session(request: SessionCreate) -> dict[str, Any]:
        return service.create(request.instruction, request.client_request_id)

    @router.get("")
    def list_sessions(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        items = service.list(limit)
        return {"items": items, "count": len(items)}

    @router.get("/{session_id}")
    def inspect_session(session_id: str) -> dict[str, Any]:
        return service.inspect(session_id)

    @router.post("/{session_id}/messages", status_code=202)
    def send_message(session_id: str, request: SessionMessageCreate) -> dict[str, Any]:
        return service.send_message(
            session_id, request.content, request.client_request_id, request.directive_kind
        )

    @router.post("/{session_id}/controls", status_code=202)
    def control_session(session_id: str, request: SessionControlCreate) -> dict[str, Any]:
        return service.control(session_id, request.action, request.client_request_id)

    @router.get("/{session_id}/events")
    def session_events(
        session_id: str, after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        items = [event_projection(item) for item in service.events(session_id, after=after, limit=limit)]
        return {
            "items": items,
            "count": len(items),
            "next_cursor": items[-1]["cursor"] if items else after,
        }

    @router.post("/{session_id}/events", status_code=202)
    def ingest_session_event(session_id: str, request: SessionEventCreate) -> dict[str, Any]:
        result = event_router.ingest(
            session_id,
            source_namespace=request.source_namespace,
            source_event_id=request.source_event_id,
            event_type=request.event_type,
            occurred_at=request.occurred_at,
            payload=request.payload,
            device_id=request.device_id,
            device_boot_id=request.device_boot_id,
            source_cursor=request.source_cursor,
        )
        # The service/scheduler owns AttentionDecision creation.  ``created``
        # only describes the inbox row: a prior request may have crashed after
        # persistence/classification but before handling.  Exact HTTP replay
        # must finish that durable event, while terminal handling states and a
        # linked Decision remain strict no-replay fences.
        handler = getattr(service, "handle_inbox_event", None)
        projected_event = result.event
        should_handle = (
            callable(handler)
            and result.event.handling_status.value in {"RECEIVED", "CLASSIFIED"}
            and result.event.decision_id is None
        )
        if should_handle:
            handling_result = handler(result.event.id)
            if handling_result is None and result.event.handling_status.value == "CLASSIFIED":
                service.store.mark_event_handled(result.event.id, decision_id=None)
            projected_event = next(
                (
                    item
                    for item in service.events(
                        session_id, after=result.event.cursor - 1, limit=10
                    )
                    if item.id == result.event.id
                ),
                result.event,
            )
        attention_decision = None
        if projected_event.decision_id is not None:
            persisted_decision = service.store.attention_decision(
                projected_event.decision_id
            )
            if persisted_decision is not None:
                attention_decision = domain_dict(persisted_decision)
        preemption_request = service.store.preemption_request_for_event(
            projected_event.id
        )
        return {
            "event": event_projection(projected_event),
            "created": result.created,
            "routing": result.routing,
            "affected_goal_ids": list(result.affected_goal_ids),
            "preemption_request": (
                domain_dict(preemption_request)
                if preemption_request is not None
                else None
            ),
            "attention_decision": attention_decision,
        }

    return router


def _text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def _inert_key(value: str) -> str:
    value = value.strip()
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
    if not value or any(character not in allowed for character in value):
        raise ValueError("client_request_id contains unsupported characters")
    return value
