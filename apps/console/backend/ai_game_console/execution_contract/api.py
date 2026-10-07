"""Authenticated FastAPI surface for the WeftMate/Harness execution seam."""

from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .service import (
    ExecutionContractError,
    ExecutionContractService,
    V2ExecutionContractService,
)
from .v2_models import (
    AnswerV2Request,
    ArchiveRequest,
    CapabilityAuthContext,
    ControlRequest,
    CreateTaskRequest,
    DSHOriginIdentity,
    RevisionRequest,
)


CLIENT_ID = "weftmate-harness-v1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceProfileMutationRequest(_Strict):
    """Profile configuration is owner-authenticated, not a DSH Task action."""

    idempotency_key: str = Field(min_length=1, max_length=128)
    profile: dict[str, Any] = Field(default_factory=dict)

    @field_validator("idempotency_key")
    @classmethod
    def operation_key(cls, value: str) -> str:
        return _inert(value)


class CallIdentity(_Strict):
    dsh_session_id: str = Field(min_length=1, max_length=256)
    dsh_turn_id: str | int
    tool_call_id: str = Field(min_length=1, max_length=256)
    root_call_id: str = Field(min_length=1, max_length=256)

    @field_validator("dsh_session_id", "tool_call_id", "root_call_id")
    @classmethod
    def inert(cls, value: str) -> str:
        return _inert(value)

    @field_validator("dsh_turn_id")
    @classmethod
    def turn(cls, value: str | int) -> str | int:
        if isinstance(value, bool):
            raise ValueError("dsh_turn_id must be a string or integer")
        if isinstance(value, int):
            if value < 0:
                raise ValueError("dsh_turn_id must not be negative")
            return value
        return _inert(value)


class GoalInput(_Strict):
    summary: str = Field(min_length=1, max_length=10_000)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("summary")
    @classmethod
    def summary_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be blank")
        return value


class SubmitRequest(_Strict):
    identity: CallIdentity
    goal: GoalInput
    client_request_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    # DSH is the sole permission authority.  Omission deliberately preserves
    # the legacy phone-confirmation path for executions created before this
    # seam was introduced.
    authorization_mode: Literal["full-access", "allowed-once"] | None = None

    @field_validator("client_request_id", "idempotency_key")
    @classmethod
    def request_key(cls, value: str) -> str:
        return _inert(value)


class LookupRequest(_Strict):
    identity: CallIdentity


class OperationRequest(_Strict):
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def request_key(cls, value: str) -> str:
        return _inert(value)


class AnswerRequest(OperationRequest):
    question_id: str = Field(min_length=1, max_length=256)
    value: Any

    @field_validator("question_id")
    @classmethod
    def question_key(cls, value: str) -> str:
        return _inert(value)


def execution_contract_error_handler(
    _: Request, error: ExecutionContractError
) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.as_payload())


def create_execution_router(
    service: ExecutionContractService, *, token: str | None
) -> APIRouter:
    router = APIRouter(prefix="/api/execution/v1", tags=["execution-v1"])

    def reject_legacy_mutation() -> None:
        raise ExecutionContractError(
            "EXECUTION_V1_READ_ONLY",
            "Execution v1 is retained for historical reads; create and control use v2.",
            410,
        )

    def authorize(
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> None:
        if token is None:
            raise ExecutionContractError(
                "EXECUTION_API_NOT_CONFIGURED",
                "The Harness execution capability is not configured.", 503,
            )
        expected = f"Bearer {token}"
        if x_ai_game_client != CLIENT_ID or authorization is None or not hmac.compare_digest(authorization, expected):
            raise ExecutionContractError(
                "EXECUTION_CLIENT_UNAUTHORIZED",
                "The local execution client is not authorized.", 403,
            )

    @router.get("/health", dependencies=[])
    def health(
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization)
        return service.health()

    @router.post("/executions:submit", status_code=202)
    def submit(
        request: SubmitRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization)
        reject_legacy_mutation()

    @router.post("/executions:lookup")
    def lookup(
        request: LookupRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization)
        snapshot = service.lookup(request.identity.model_dump(mode="json"))
        return {"found": snapshot is not None, "execution": snapshot}

    @router.get("/executions/{execution_id}")
    def inspect(
        execution_id: str,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization)
        return service.inspect(_inert(execution_id))

    @router.get("/executions/{execution_id}/events")
    def events(
        execution_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization)
        return service.events(_inert(execution_id), after, limit)

    @router.post("/executions/{execution_id}:cancel", status_code=202)
    def cancel(
        execution_id: str, request: OperationRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization)
        reject_legacy_mutation()

    @router.post("/executions/{execution_id}:resume", status_code=202)
    def resume(
        execution_id: str, request: OperationRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization)
        reject_legacy_mutation()

    @router.post("/executions/{execution_id}:answer", status_code=202)
    def answer(
        execution_id: str, request: AnswerRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization)
        reject_legacy_mutation()

    @router.get("/executions/{execution_id}/evidence/{evidence_id}")
    def evidence(
        execution_id: str, evidence_id: str,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> Response:
        authorize(x_ai_game_client, authorization)
        content, content_type = service.read_evidence(
            _inert(execution_id), _inert(evidence_id)
        )
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    return router


def create_v2_authorizer(
    *, token: str | None, capability_context: dict[str, Any] | None = None,
):
    """Shared authenticated host identity for task and direct-device routes."""
    expected_context: dict[str, Any] | None = None
    if capability_context is not None:
        expected_context = CapabilityAuthContext.model_validate(capability_context).model_dump(mode="json")

    def authorize(
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(
            default=None, alias="X-AI-Game-Principal-Id"
        ),
        x_ai_game_controller_id: str | None = Header(
            default=None, alias="X-AI-Game-Controller-Id"
        ),
    ) -> dict[str, Any]:
        if token is None:
            raise ExecutionContractError(
                "EXECUTION_API_NOT_CONFIGURED",
                "The Harness execution capability is not configured.", 503,
            )
        expected = f"Bearer {token}"
        if (
            x_ai_game_client != CLIENT_ID
            or authorization is None
            or not hmac.compare_digest(authorization, expected)
        ):
            raise ExecutionContractError(
                "EXECUTION_CLIENT_UNAUTHORIZED",
                "The local execution client is not authorized.", 403,
            )
        if x_ai_game_principal_id is None or x_ai_game_controller_id is None:
            raise ExecutionContractError(
                "EXECUTION_CLIENT_UNAUTHORIZED",
                "The local execution client is not authorized.", 403,
            )
        try:
            supplied_context = CapabilityAuthContext(
                principal_id=x_ai_game_principal_id,
                controller_id=x_ai_game_controller_id,
            ).model_dump(mode="json")
            if expected_context is not None:
                for field in ("principal_id", "controller_id"):
                    if not hmac.compare_digest(
                        str(supplied_context[field]), str(expected_context[field])
                    ):
                        raise ValueError("owner mismatch")
                return expected_context
            return supplied_context
        except (TypeError, ValueError):
            # Header values are opaque authenticated context.  Never echo them.
            raise ExecutionContractError(
                "EXECUTION_CLIENT_UNAUTHORIZED",
                "The local execution client is not authorized.", 403,
            ) from None

    return authorize


def create_execution_v2_router(
    service: V2ExecutionContractService, *, token: str | None,
    capability_context: dict[str, Any] | None = None,
) -> APIRouter:
    """Build, but do not compose, the long-lived v2 router.

    ``capability_context`` is authenticated host context, never request data.
    Its stable principal/controller IDs are deliberately separate from DSH
    session/turn/tool provenance.  Final application composition is owned by the integration
    package.  Keeping construction here lets focused tests compose this router
    with fake canonical Task/Profile/Experience ports.
    """
    router = APIRouter(prefix="/api/execution/v2", tags=["execution-v2"])
    authorize = create_v2_authorizer(token=token, capability_context=capability_context)

    def query_origin(
        dsh_session_id: str = Query(min_length=1, max_length=256),
        dsh_turn_id: str = Query(min_length=1, max_length=256),
        tool_call_id: str = Query(min_length=1, max_length=256),
        root_call_id: str = Query(min_length=1, max_length=256),
    ) -> dict[str, Any]:
        return DSHOriginIdentity(
            dsh_session_id=dsh_session_id,
            dsh_turn_id=dsh_turn_id,
            tool_call_id=tool_call_id,
            root_call_id=root_call_id,
        ).model_dump(mode="json")

    @router.get("/health")
    def health(
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.health()

    @router.post("/tasks", status_code=202)
    def create_task(
        request: CreateTaskRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.create_task(request.model_dump(mode="json", exclude_none=True), auth_context=context)

    @router.get("/tasks")
    def list_tasks(
        status: str | None = Query(default=None, max_length=64),
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=100, ge=1, le=200),
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.list_tasks(None, auth_context=context, status=status, cursor=cursor, limit=limit)

    @router.get("/tasks/{task_id}")
    def get_task(
        task_id: str,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.get_task(_inert(task_id), None, auth_context=context)

    @router.get("/tasks/{task_id}/events")
    def task_events(
        task_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.events(_inert(task_id), None, auth_context=context, after=after, limit=limit)

    @router.get("/tasks/{task_id}/frame")
    async def task_frame_metadata(
        task_id: str,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.frame_metadata(_inert(task_id), auth_context=context)

    @router.get("/tasks/{task_id}/frames/{frame_id}")
    async def task_frame_content(
        task_id: str,
        frame_id: str,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> Response:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        metadata, content = service.frame_content(
            _inert(task_id), _inert(frame_id), auth_context=context
        )
        return Response(
            content=content,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(metadata["size_bytes"]),
            },
        )

    @router.post("/tasks/{task_id}/revisions", status_code=202)
    def revise_task(
        task_id: str, request: RevisionRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.revise(_inert(task_id), request.model_dump(mode="json"), auth_context=context)

    @router.post("/tasks/{task_id}/controls", status_code=202)
    def control_task(
        task_id: str, request: ControlRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.control(_inert(task_id), request.model_dump(mode="json"), auth_context=context)

    @router.post("/tasks/{task_id}/answers", status_code=202)
    def answer_task(
        task_id: str, request: AnswerV2Request,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.answer(_inert(task_id), request.model_dump(mode="json"), auth_context=context)

    @router.post("/tasks/{task_id}:archive", status_code=202)
    def archive_task(
        task_id: str, request: ArchiveRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.archive(_inert(task_id), request.model_dump(mode="json"), auth_context=context)

    @router.get("/device-profiles")
    async def list_device_profiles(
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=100, ge=1, le=200),
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.list_device_profiles(None, auth_context=context, cursor=cursor, limit=limit)

    @router.get("/device-profiles/discovery")
    async def discover_emulators(
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.discover_emulators(auth_context=context)

    @router.post("/device-profiles", status_code=202)
    async def create_device_profile(
        request: DeviceProfileMutationRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.create_device_profile(request.model_dump(mode="json"), auth_context=context)

    @router.get("/device-profiles/{profile_id}")
    async def get_device_profile(
        profile_id: str,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.get_device_profile(_inert(profile_id), None, auth_context=context)

    @router.patch("/device-profiles/{profile_id}", status_code=202)
    async def update_device_profile(
        profile_id: str, request: DeviceProfileMutationRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.update_device_profile(_inert(profile_id), request.model_dump(mode="json"), auth_context=context)

    @router.post("/device-profiles/{profile_id}/verify", status_code=202)
    async def verify_device_profile(
        profile_id: str, request: DeviceProfileMutationRequest,
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.verify_device_profile(_inert(profile_id), request.model_dump(mode="json"), auth_context=context)

    @router.get("/tasks/{task_id}/experience")
    def task_experience(
        task_id: str,
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=100, ge=1, le=200),
        x_ai_game_client: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        x_ai_game_principal_id: str | None = Header(default=None, alias="X-AI-Game-Principal-Id"),
        x_ai_game_controller_id: str | None = Header(default=None, alias="X-AI-Game-Controller-Id"),
    ) -> dict[str, Any]:
        context = authorize(x_ai_game_client, authorization, x_ai_game_principal_id, x_ai_game_controller_id)
        return service.experience(_inert(task_id), None, auth_context=context, cursor=cursor, limit=limit)

    return router


def _inert(value: str) -> str:
    value = value.strip()
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
    if not value or any(character not in allowed for character in value):
        raise ValueError("identity contains unsupported characters")
    return value


__all__ = [
    "CLIENT_ID", "create_execution_router", "create_execution_v2_router",
    "execution_contract_error_handler",
]
