from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..execution_contract.api import create_v2_authorizer
from ..execution_contract.v2_models import DSHOriginIdentity
from .service import DeviceRunService


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRunRequest(_Strict):
    run_id: str = Field(min_length=1, max_length=256)
    device_profile_id: str = Field(min_length=1, max_length=256)
    origin: DSHOriginIdentity
    authorization_mode: Literal["full-access", "allowed-once"]

    @field_validator("run_id", "device_profile_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        if any(ord(character) < 33 or character in "/\\" for character in value):
            raise ValueError("identifier must not contain whitespace or path separators")
        return value


class ObserveRequest(_Strict):
    include_screenshot: bool = False
    ui_tree_format: Literal["compact", "xml"] = "xml"


class DeviceAction(_Strict):
    action: Literal["tap", "text", "swipe", "long_press", "open_app", "keyevent", "recents"]
    x: int | None = Field(default=None, ge=0, le=10000, strict=True)
    y: int | None = Field(default=None, ge=0, le=10000, strict=True)
    end_x: int | None = Field(default=None, ge=0, le=10000, strict=True)
    end_y: int | None = Field(default=None, ge=0, le=10000, strict=True)
    duration_ms: int | None = Field(default=None, ge=1, strict=True)
    keycode: str | None = None
    package: str | None = None
    component: str | None = None
    text: str | None = Field(default=None, repr=False)


class ActionsRequest(_Strict):
    command_id: str = Field(min_length=1, max_length=256)
    actions: list[DeviceAction] = Field(min_length=1)


class ControlRequest(_Strict):
    action: Literal["pause", "resume", "cancel", "complete"]


def create_device_runs_router(service: DeviceRunService, *, token: str | None) -> APIRouter:
    router = APIRouter(prefix="/api/execution/v2/device-runs", tags=["direct-device"])
    authorize = create_v2_authorizer(token=token)

    @router.post("")
    def create(request: CreateRunRequest, owner: dict = Depends(authorize)) -> dict[str, Any]:
        return service.create(request.model_dump(mode="json"), owner)

    @router.get("/{run_id}")
    def inspect(run_id: str, owner: dict = Depends(authorize)) -> dict[str, Any]:
        return service.inspect(run_id, owner)

    @router.post("/{run_id}/observe")
    def observe(run_id: str, request: ObserveRequest, owner: dict = Depends(authorize)) -> dict[str, Any]:
        return service.observe(
            run_id,
            owner,
            include_screenshot=request.include_screenshot,
            ui_tree_format=request.ui_tree_format,
        )

    @router.post("/{run_id}/actions")
    def actions(run_id: str, request: ActionsRequest, owner: dict = Depends(authorize)) -> dict[str, Any]:
        return service.actions(run_id, owner, request.model_dump(mode="json", exclude_none=True))

    @router.post("/{run_id}/controls")
    def control(run_id: str, request: ControlRequest, owner: dict = Depends(authorize)) -> dict[str, Any]:
        return service.control(run_id, owner, request.action)

    return router
