from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


class ActionType(StrEnum):
    TAP = "tap"
    LONG_PRESS = "long_press"
    SWIPE = "swipe"
    INPUT_TEXT = "input_text"
    BACK = "back"
    HOME = "home"
    OPEN_APP = "open_app"
    WAIT = "wait"
    SCREENSHOT = "screenshot"


class ActionValidationStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class InvalidActionTransition(ValueError):
    """Raised when Action facts would be advanced out of order."""


@dataclass(frozen=True, slots=True)
class ExecutionError:
    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        _required(self.code, "execution_error.code")
        _required(self.message, "execution_error.message")


@dataclass(frozen=True, slots=True)
class ActionExecution:
    id: str
    action_id: str
    device_id: str
    lease_ref: str | None
    accepted: bool
    adapter_code: int | None
    error: ExecutionError | None
    started_at: str
    finished_at: str

    def __post_init__(self) -> None:
        _required(self.id, "action_execution.id")
        _required(self.action_id, "action_execution.action_id")
        _required(self.device_id, "action_execution.device_id")
        if self.lease_ref is not None:
            _required(self.lease_ref, "action_execution.lease_ref")
        started = _utc_timestamp(self.started_at, "action_execution.started_at")
        finished = _utc_timestamp(self.finished_at, "action_execution.finished_at")
        if finished < started:
            raise ValueError("action_execution finish precedes start")
        if self.accepted and self.error is not None:
            raise ValueError("accepted ActionExecution cannot carry an error")
        if not self.accepted and self.error is None:
            raise ValueError("rejected ActionExecution requires an error")


@dataclass(frozen=True, slots=True)
class Action:
    id: str
    task_id: str
    stage_id: str
    based_on_observation_id: str
    type: ActionType
    params: Mapping[str, Any]
    expected_outcome: str
    proposed_by_call_id: str
    proposed_at: str
    validation_status: ActionValidationStatus
    rejection_code: str | None
    status: ActionStatus
    updated_at: str

    def __post_init__(self) -> None:
        _required(self.id, "action.id")
        _required(self.task_id, "action.task_id")
        _required(self.stage_id, "action.stage_id")
        _required(self.based_on_observation_id, "action.based_on_observation_id")
        if not isinstance(self.params, Mapping):
            raise ValueError("action.params must be a mapping")
        _required(self.expected_outcome, "action.expected_outcome")
        _required(self.proposed_by_call_id, "action.proposed_by_call_id")
        _utc_timestamp(self.proposed_at, "action.proposed_at")
        _utc_timestamp(self.updated_at, "action.updated_at")
        if self.validation_status is ActionValidationStatus.ACCEPTED:
            if self.rejection_code is not None:
                raise ValueError("accepted Action cannot carry rejection_code")
        else:
            _required(self.rejection_code or "", "action.rejection_code")
            if self.status is not ActionStatus.FAILED:
                raise ValueError("rejected Action must be FAILED")

    @classmethod
    def propose(
        cls,
        *,
        action_id: str,
        task_id: str,
        stage_id: str,
        based_on_observation_id: str,
        action_type: ActionType,
        params: Mapping[str, Any],
        expected_outcome: str,
        proposed_by_call_id: str,
        proposed_at: str,
    ) -> Action:
        return cls(
            id=action_id,
            task_id=task_id,
            stage_id=stage_id,
            based_on_observation_id=based_on_observation_id,
            type=action_type,
            params=dict(params),
            expected_outcome=expected_outcome,
            proposed_by_call_id=proposed_by_call_id,
            proposed_at=proposed_at,
            validation_status=ActionValidationStatus.ACCEPTED,
            rejection_code=None,
            status=ActionStatus.PROPOSED,
            updated_at=proposed_at,
        )

    def record_execution(self, execution: ActionExecution) -> Action:
        if execution.action_id != self.id:
            raise ValueError("ActionExecution does not belong to Action")
        if self.status is not ActionStatus.PROPOSED:
            raise InvalidActionTransition(
                f"Action in {self.status.value} cannot record an execution"
            )
        return replace(
            self,
            status=ActionStatus.EXECUTED if execution.accepted else ActionStatus.FAILED,
            updated_at=execution.finished_at,
        )

    def record_verdict(self, verdict: str, *, at: str) -> Action:
        if self.status is not ActionStatus.EXECUTED:
            raise InvalidActionTransition(
                f"Action in {self.status.value} cannot be verified"
            )
        _utc_timestamp(at, "action verification timestamp")
        statuses = {
            "SUCCESS": ActionStatus.VERIFIED,
            "FAIL": ActionStatus.FAILED,
            "UNCERTAIN": ActionStatus.UNCERTAIN,
        }
        try:
            status = statuses[verdict]
        except KeyError as exc:
            raise ValueError("unknown verification verdict") from exc
        return replace(self, status=status, updated_at=at)


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
