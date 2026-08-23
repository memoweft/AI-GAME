from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import GoalError, GoalRecord, StoredEvent
from .service import GoalService


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoalCreate(_ApiModel):
    goal: str = Field(min_length=1, max_length=10_000)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _inert_key(value)


class GoalMessageCreate(_ApiModel):
    content: str = Field(min_length=1, max_length=10_000)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _inert_key(value)


class GoalControlCreate(_ApiModel):
    action: Literal["pause", "resume", "stop", "takeover"]
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return _inert_key(value)


class GoalApplicationOutcomeCreate(_ApiModel):
    event_id: str = Field(min_length=1, max_length=128)
    event_type: Literal[
        "delayed_positive",
        "delayed_negative",
        "no_response",
        "user_approval",
        "user_rejection",
    ]
    evidence_refs: list[str] = Field(min_length=1, max_length=16)
    attribution_scope: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("event_id")
    @classmethod
    def normalize_event_id(cls, value: str) -> str:
        return _inert_key(value)

    @field_validator("evidence_refs")
    @classmethod
    def normalize_evidence_refs(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 512 for item in normalized):
            raise ValueError("evidence_refs must contain non-blank values up to 512 characters")
        return normalized

    @field_validator("attribution_scope")
    @classmethod
    def normalize_attribution_scope(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class GoalTargetSelection(_ApiModel):
    target_id: str = Field(min_length=1, max_length=512)

    @field_validator("target_id")
    @classmethod
    def normalize_target(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


def goal_error_handler(_: Request, error: GoalError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.as_payload())


def create_goal_router(service: GoalService) -> APIRouter:
    router = APIRouter(prefix="/api/v2/goals", tags=["goals-v2"])

    @router.post("", status_code=202)
    def create_goal(request: GoalCreate) -> dict[str, Any]:
        return _payload(service.create(request.goal, request.idempotency_key), service)

    @router.get("")
    def list_goals(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        items = [_payload(item, service) for item in service.list(limit)]
        return {"items": items, "count": len(items)}

    @router.get("/{goal_id}")
    def inspect_goal(goal_id: str) -> dict[str, Any]:
        return _payload(service.inspect(goal_id), service)

    @router.post("/{goal_id}/messages", status_code=202)
    def send_goal_message(goal_id: str, request: GoalMessageCreate) -> dict[str, Any]:
        return _payload(
            service.send_message(goal_id, request.content, request.idempotency_key), service
        )

    @router.post("/{goal_id}/application-outcomes", status_code=202)
    def report_application_outcome(
        goal_id: str, request: GoalApplicationOutcomeCreate
    ) -> dict[str, Any]:
        return _payload(
            service.report_application_outcome(
                goal_id,
                event_id=request.event_id,
                event_type=request.event_type,
                evidence_refs=tuple(request.evidence_refs),
                attribution_scope=request.attribution_scope,
                confidence=request.confidence,
            ),
            service,
        )

    @router.post("/{goal_id}/controls", status_code=202)
    def control_goal(goal_id: str, request: GoalControlCreate) -> dict[str, Any]:
        return _payload(
            service.control(goal_id, request.action, request.idempotency_key), service
        )

    @router.get("/{goal_id}/events")
    def goal_events(goal_id: str, after: int = Query(default=0, ge=0),
                    limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        items = [_event_payload(item) for item in service.events(goal_id, after=after, limit=limit)]
        return {"items": items, "count": len(items),
                "next_cursor": items[-1]["cursor"] if items else after}

    @router.post("/{goal_id}/preflight/retry", status_code=202)
    def retry_goal_preflight(goal_id: str) -> dict[str, Any]:
        return _payload(service.retry_preflight(goal_id), service)

    @router.post("/{goal_id}/preflight/selection", status_code=202)
    def select_goal_target(
        goal_id: str, request: GoalTargetSelection
    ) -> dict[str, Any]:
        return _payload(service.select_target(goal_id, request.target_id), service)

    @router.post("/{goal_id}/completion/retry", status_code=202)
    def retry_goal_completion(goal_id: str) -> dict[str, Any]:
        return _payload(service.retry_completion(goal_id), service)

    return router


def _payload(record: GoalRecord, service: GoalService) -> dict[str, Any]:
    completion = service.store.completion(record.id)
    binding_plan = service.store.binding_plan(record.id)
    notifications = service.store.notifications(record.id)
    daily_checklist = service.daily_checklist(record.id)
    incomplete = []
    if completion is not None:
        incomplete = [
            str(item["evidence"])
            for item in completion["criteria"]
            if not bool(item["satisfied"])
        ]
    elif record.execution_status == "CANDIDATE_COMPLETE":
        incomplete = ["原始目标尚未经过独立 Goal Completion Verifier 验证"]
    if daily_checklist is not None:
        incomplete.extend(
            str(item.get("title") or item.get("item_id"))
            for item in daily_checklist.get("remaining_items", ())
        )
    return {
        "id": record.id,
        "original_goal": record.original_goal,
        "goal_specification": service.store.specification(record.id),
        "execution_status": record.execution_status,
        "control_state": record.control_state,
        "resume_execution_status": record.resume_execution_status,
        "active_stage": record.active_stage,
        "binding": {
            "kind": record.binding_kind,
            "state": record.binding_state,
            "task_id": record.bound_task_id,
            "target_id": record.target_id,
            "completion_gate": (
                "verified_language_result"
                if record.binding_kind == "local_language"
                and record.execution_status == "COMPLETED"
                else (
                    "continuous_external_outcome"
                    if binding_plan is not None
                    and binding_plan.owner_kind == "external_owner"
                    else "continuous_managed_goal"
                )
                if record.binding_kind == "application_runtime"
                else (
                    "continuous_mobile_goal"
                    if record.bound_task_id is not None
                    else "continuous_mobile_goal_pending_runtime"
                )
                if record.binding_kind == "long_lived_mobile_composition"
                else
                "verified" if completion and completion["verdict"] == "verified"
                else completion["verdict"] if completion else "pending_u3"
            ),
        },
        "binding_plan": (
            {
                "revision": binding_plan.revision,
                "route_kind": binding_plan.route_kind,
                "binding_kind": binding_plan.binding_kind,
                "capability_ids": list(binding_plan.capability_ids),
                "owner_kind": binding_plan.owner_kind,
                "owner_binding_ref": binding_plan.owner_binding_ref,
                "profile_id": binding_plan.profile_id,
                "classification": binding_plan.classification,
                "rationale": binding_plan.rationale,
                "created_at": binding_plan.created_at,
            }
            if binding_plan is not None
            else None
        ),
        "environment_state": service.store.environment(record.id),
        "repair_attempts": service.store.repairs(record.id),
        "waiting_reason": record.waiting_reason,
        "error": record.error,
        "result_summary": record.result_summary,
        "completion_assessment": completion,
        "completion_history": service.store.completions(record.id),
        "verified_facts": completion["verified_facts"] if completion else [],
        "daily_checklist": daily_checklist,
        "uncompleted_items": incomplete,
        "notifications": [
            {
                "id": item.notification_id,
                "kind": item.kind,
                "summary": item.summary,
                "evidence_refs": list(item.evidence_refs),
                "source_event_sequence": item.source_event_sequence,
                "created_at": item.created_at,
            }
            for item in notifications
        ],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "terminal_at": record.terminal_at,
    }


def _event_payload(event: StoredEvent) -> dict[str, Any]:
    return {"cursor": event.cursor, "goal_id": event.goal_id,
            "event_type": event.event_type, "data": event.data,
            "created_at": event.created_at}


def _inert_key(value: str) -> str:
    value = value.strip()
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
    if not value or any(character not in allowed for character in value):
        raise ValueError("idempotency_key contains unsupported characters")
    return value
