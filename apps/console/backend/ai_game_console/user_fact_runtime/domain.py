from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class FactResolutionStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    CONFLICTING = "CONFLICTING"
    KNOWN = "KNOWN"


class UserFactSourceKind(str, Enum):
    USER_ANSWER = "user_answer"
    IMPORTED = "imported"
    OBSERVED = "observed"


class NeedUserFactStatus(str, Enum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    APPLIED = "APPLIED"
    SUPERSEDED = "SUPERSEDED"
    RESOLVED_FROM_EXISTING_FACT = "RESOLVED_FROM_EXISTING_FACT"


@dataclass(frozen=True, slots=True)
class UserFact:
    id: str
    user_scope: str
    fact_key: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserFactRevision:
    id: str
    fact_id: str
    user_scope: str
    fact_key: str
    value: Any
    applicability: dict[str, Any]
    source_kind: UserFactSourceKind
    source_ref: str
    provenance: dict[str, Any]
    confidence: float
    valid_from: datetime
    valid_until: datetime | None
    revision: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NeedUserFact:
    id: str
    session_id: str
    goal_id: str
    user_scope: str
    fact_key: str
    question: str
    why_needed: str
    answer_schema: dict[str, Any]
    applicability: dict[str, Any]
    resolution_status: FactResolutionStatus
    status: NeedUserFactStatus
    resume_stage: str
    conversation_hint: str | None
    answer_fact_id: str | None
    answer_revision_id: str | None
    created_at: datetime
    answered_at: datetime | None
    applied_at: datetime | None


@dataclass(frozen=True, slots=True)
class FactResolution:
    status: FactResolutionStatus
    revision: UserFactRevision | None = None
    candidates: tuple[UserFactRevision, ...] = field(default_factory=tuple)


class UserFactError(RuntimeError):
    pass


class UserFactNotFound(UserFactError):
    pass


class NeedUserFactNotFound(UserFactError):
    pass


class NeedUserFactStateConflict(UserFactError):
    pass


class UserFactIdempotencyConflict(UserFactError):
    pass


class UserFactAnswerValidationError(UserFactError):
    pass


class UntrustedFactSource(UserFactError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)
