from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any


class TargetKind(StrEnum):
    WINDOWS = "windows"
    ANDROID = "android"


class TargetStatus(StrEnum):
    READY = "ready"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class Workflow:
    id: str
    name: str
    description: str
    target_kind: TargetKind
    enabled: bool
    integration_status: str
    requires_approval: bool
    created_at: str
    updated_at: str

    @property
    def status(self) -> str:
        return "available" if self.enabled else self.integration_status

    @property
    def target_kinds(self) -> list[str]:
        return [self.target_kind.value]


@dataclass(frozen=True, slots=True)
class Target:
    id: str
    name: str
    kind: TargetKind
    status: str
    source: str
    external_id: str | None
    details: dict[str, Any] = field(default_factory=dict)
    discovered_at: str = ""
    last_seen_at: str = ""
    updated_at: str = ""

    @property
    def address(self) -> str | None:
        value = self.details.get("address", self.external_id)
        return str(value) if value is not None else None

    @property
    def detail(self) -> str | None:
        value = self.details.get("detail")
        return str(value) if value is not None else None

    @property
    def capabilities(self) -> list[str]:
        value = self.details.get("capabilities", [])
        return [str(item) for item in value] if isinstance(value, list) else []


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    name: str
    workflow_id: str
    target_id: str
    instruction: str
    exact_text: str | None
    requires_approval: bool
    status: RunStatus
    blocker: str | None
    workflow_name: str
    target_name: str
    created_at: str
    updated_at: str

    @property
    def blockers(self) -> list[dict[str, str]]:
        if self.blocker is None:
            return []
        messages = {
            "approval_required": "此运行正在等待本地审批。",
            "approval_rejected": "审批请求已被拒绝。",
            "workflow_executor_not_connected": (
                "传统任务队列尚未接入；请在“对话执行”中操作设备。"
            ),
            "executor_not_configured": (
                "传统任务队列尚未接入；请在“对话执行”中操作设备。"
            ),
        }
        return [
            {
                "code": self.blocker,
                "message": messages.get(self.blocker, self.blocker.replace("_", " ").capitalize()),
            }
        ]

    @property
    def has_exact_text(self) -> bool:
        return self.exact_text is not None

    @property
    def exact_text_length(self) -> int:
        return len(self.exact_text) if self.exact_text is not None else 0

    @property
    def exact_text_sha256(self) -> str | None:
        if self.exact_text is None:
            return None
        return sha256(self.exact_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    run_id: str
    status: ApprovalStatus
    note: str | None
    created_at: str
    decided_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    run_id: str | None
    event_type: str
    message: str
    level: str
    data: dict[str, Any]
    created_at: str

    @property
    def type(self) -> str:
        return self.event_type


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    id: str
    name: str
    status: str
    configured: bool
    detail: str
    blocker: dict[str, str] | None = None
