"""R8 UserFact and NeedUserFact HTTP contract."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..agent_runtime.fact_questions import FactQuestionCoordinator
from .domain import (
    NeedUserFactNotFound,
    NeedUserFactStateConflict,
    NeedUserFactStatus,
    UntrustedFactSource,
    UserFactAnswerValidationError,
    UserFactIdempotencyConflict,
    UserFactSourceKind,
)


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactAnswerCreate(_ApiModel):
    value: Any
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def inert_key(cls, value: str) -> str:
        value = value.strip()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
        if not value or any(character not in allowed for character in value):
            raise ValueError("idempotency_key contains unsupported characters")
        return value


class TrustedFactRevisionCreate(_ApiModel):
    value: Any
    user_scope: str = Field(min_length=1, max_length=256)
    applicability: dict[str, Any] = Field(default_factory=dict)
    source_kind: Literal["imported", "observed"]
    source_ref: str = Field(min_length=1, max_length=1024)
    provenance: dict[str, Any]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


def create_user_fact_router(coordinator: FactQuestionCoordinator) -> APIRouter:
    router = APIRouter(prefix="/api/v3", tags=["user-facts-v3"])
    service = coordinator.facts

    @router.get("/sessions/{session_id}/fact-needs")
    def list_fact_needs(
        session_id: str,
        status: NeedUserFactStatus | None = Query(default=NeedUserFactStatus.OPEN),
    ) -> dict[str, Any]:
        items = service.store.list_needs(session_id=session_id, status=status)
        return {"items": [_payload(item) for item in items], "count": len(items)}

    @router.post("/fact-needs/{need_id}/answers")
    def answer_fact_need(need_id: str, request: FactAnswerCreate) -> dict[str, Any]:
        try:
            need, revision, created = coordinator.answer(
                need_id=need_id,
                value=request.value,
                idempotency_key=request.idempotency_key,
            )
        except NeedUserFactNotFound as error:
            raise HTTPException(status_code=404, detail="fact need not found") from error
        except (NeedUserFactStateConflict, UserFactIdempotencyConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except UserFactAnswerValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "need": _payload(need),
            "revision": _payload(revision),
            "created": created,
        }

    @router.get("/user-facts")
    def list_user_facts(
        user_scope: str | None = Query(default=None),
        fact_key: str | None = Query(default=None),
    ) -> dict[str, Any]:
        if fact_key is not None:
            revisions = service.store.revisions(
                user_scope=user_scope or "default", fact_key=fact_key
            )
            return {"items": [_payload(item) for item in revisions], "count": len(revisions)}
        facts = service.store.list_facts(user_scope=user_scope)
        return {"items": [_payload(item) for item in facts], "count": len(facts)}

    @router.post("/user-facts/{fact_key}/revisions")
    def create_trusted_revision(
        fact_key: str, request: TrustedFactRevisionCreate
    ) -> dict[str, Any]:
        try:
            revision = service.record_trusted_revision(
                user_scope=request.user_scope,
                fact_key=fact_key,
                value=request.value,
                applicability=request.applicability,
                source_kind=UserFactSourceKind(request.source_kind),
                source_ref=request.source_ref,
                provenance=request.provenance,
                confidence=request.confidence,
            )
        except UntrustedFactSource as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _payload(revision)

    return router


def _payload(value: Any) -> Any:
    if is_dataclass(value):
        return _payload(asdict(value))
    if isinstance(value, dict):
        payload = {key: _payload(item) for key, item in value.items()}
        if "resume_stage" in payload:
            payload.setdefault("resume_stage_id", payload["resume_stage"])
        return payload
    if isinstance(value, (list, tuple)):
        return [_payload(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


__all__ = ["create_user_fact_router"]
