from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable


class VerificationVerdict(StrEnum):
    SUCCESS = "SUCCESS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"


class VerificationMethod(StrEnum):
    RUNTIME_RULE = "runtime_rule"
    ROLE_ASSISTED = "role_assisted"
    COMBINED = "combined"


@dataclass(frozen=True, slots=True)
class Verification:
    id: str
    task_id: str
    stage_id: str
    action_id: str
    before_observation_id: str
    after_observation_id: str
    verdict: VerificationVerdict
    reason: str
    evidence_refs: tuple[str, ...]
    method: VerificationMethod
    verification_call_id: str | None
    created_at: str

    def __post_init__(self) -> None:
        _required(self.id, "verification.id")
        _required(self.task_id, "verification.task_id")
        _required(self.stage_id, "verification.stage_id")
        _required(self.action_id, "verification.action_id")
        _required(self.before_observation_id, "verification.before_observation_id")
        _required(self.after_observation_id, "verification.after_observation_id")
        if self.before_observation_id == self.after_observation_id:
            raise ValueError("Verification requires a fresh after Observation")
        _required(self.reason, "verification.reason")
        if not self.evidence_refs:
            raise ValueError("verification.evidence_refs must not be empty")
        for reference in self.evidence_refs:
            _required(reference, "verification.evidence_refs item")
        if self.verification_call_id is not None:
            _required(self.verification_call_id, "verification.verification_call_id")
        _utc_timestamp(self.created_at, "verification.created_at")

    @classmethod
    def create(
        cls,
        *,
        verification_id: str,
        task_id: str,
        stage_id: str,
        action_id: str,
        before_observation_id: str,
        after_observation_id: str,
        verdict: VerificationVerdict,
        reason: str,
        evidence_refs: Iterable[str],
        method: VerificationMethod,
        verification_call_id: str | None,
        created_at: str,
    ) -> Verification:
        return cls(
            id=verification_id,
            task_id=task_id,
            stage_id=stage_id,
            action_id=action_id,
            before_observation_id=before_observation_id,
            after_observation_id=after_observation_id,
            verdict=verdict,
            reason=reason,
            evidence_refs=tuple(evidence_refs),
            method=method,
            verification_call_id=verification_call_id,
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
