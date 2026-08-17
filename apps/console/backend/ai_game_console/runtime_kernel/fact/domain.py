from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable


class FactStatus(StrEnum):
    VERIFIED = "VERIFIED"
    USER_PROVIDED = "USER_PROVIDED"
    UNVERIFIED = "UNVERIFIED"


class FactScope(StrEnum):
    TASK = "task"
    STAGE = "stage"


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    task_id: str
    key: str
    value: Any
    status: FactStatus
    confidence: float | None
    scope: FactScope
    stage_id: str | None
    source_refs: tuple[str, ...]
    supersedes_fact_id: str | None
    created_at: str

    def __post_init__(self) -> None:
        _required(self.id, "fact.id")
        _required(self.task_id, "fact.task_id")
        _required(self.key, "fact.key")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("fact.confidence must be between zero and one")
        if self.scope is FactScope.STAGE:
            _required(self.stage_id or "", "fact.stage_id")
        elif self.stage_id is not None:
            raise ValueError("task-scoped Fact cannot reference a Stage")
        if not self.source_refs:
            raise ValueError("fact.source_refs must not be empty")
        for reference in self.source_refs:
            _required(reference, "fact.source_refs item")
        if self.supersedes_fact_id is not None:
            _required(self.supersedes_fact_id, "fact.supersedes_fact_id")
            if self.supersedes_fact_id == self.id:
                raise ValueError("Fact cannot supersede itself")
        _utc_timestamp(self.created_at, "fact.created_at")

    @classmethod
    def create(
        cls,
        *,
        fact_id: str,
        task_id: str,
        key: str,
        value: Any,
        status: FactStatus,
        confidence: float | None,
        scope: FactScope,
        stage_id: str | None,
        source_refs: Iterable[str],
        supersedes_fact_id: str | None,
        created_at: str,
    ) -> Fact:
        return cls(
            id=fact_id,
            task_id=task_id,
            key=key,
            value=value,
            status=status,
            confidence=confidence,
            scope=scope,
            stage_id=stage_id,
            source_refs=tuple(source_refs),
            supersedes_fact_id=supersedes_fact_id,
            created_at=created_at,
        )


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def _utc_timestamp(value: str, name: str) -> datetime:
    _required(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
    return parsed
