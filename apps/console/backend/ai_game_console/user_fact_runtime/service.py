from __future__ import annotations

from datetime import datetime
from typing import Any

from .domain import (
    FactResolution,
    FactResolutionStatus,
    NeedUserFact,
    UntrustedFactSource,
    UserFactAnswerValidationError,
    UserFactRevision,
    UserFactSourceKind,
    utc_now,
)
from .store import SQLiteUserFactStore


class UserFactService:
    def __init__(self, store: SQLiteUserFactStore) -> None:
        self.store = store

    def resolve(
        self,
        *,
        user_scope: str,
        fact_key: str,
        applicability: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> FactResolution:
        query_scope = applicability or {}
        instant = at or utc_now()
        revisions = self.store.revisions(user_scope=user_scope, fact_key=fact_key)
        matching = [item for item in revisions if _applies(item.applicability, query_scope)]
        if not matching:
            return FactResolution(FactResolutionStatus.UNKNOWN)

        # A newer revision supersedes an older value only within the exact same
        # applicability scope.  Independent equally-specific scopes may conflict.
        latest_by_scope: dict[str, UserFactRevision] = {}
        for revision in matching:
            if revision.valid_from > instant:
                continue
            scope_key = _scope_key(revision.applicability)
            if scope_key not in latest_by_scope or revision.revision > latest_by_scope[scope_key].revision:
                latest_by_scope[scope_key] = revision
        candidates = tuple(latest_by_scope.values())
        if not candidates:
            return FactResolution(FactResolutionStatus.STALE, candidates=tuple(matching))
        valid = tuple(
            item for item in candidates
            if item.valid_from <= instant and (item.valid_until is None or instant < item.valid_until)
        )
        if not valid:
            return FactResolution(FactResolutionStatus.STALE, candidates=candidates)

        specificity = max(len(item.applicability) for item in valid)
        best = tuple(item for item in valid if len(item.applicability) == specificity)
        distinct_values = {_scope_key({"value": item.value}) for item in best}
        if len(distinct_values) > 1:
            return FactResolution(FactResolutionStatus.CONFLICTING, candidates=best)
        selected = max(best, key=lambda item: item.revision)
        return FactResolution(FactResolutionStatus.KNOWN, revision=selected, candidates=best)

    def resolve_or_create_need(
        self,
        *,
        session_id: str,
        goal_id: str,
        user_scope: str,
        fact_key: str,
        question: str,
        why_needed: str,
        answer_schema: dict[str, Any],
        applicability: dict[str, Any] | None,
        resume_stage: str,
        conversation_hint: str | None = None,
        at: datetime | None = None,
    ) -> tuple[FactResolution, NeedUserFact | None, bool]:
        resolution = self.resolve(
            user_scope=user_scope, fact_key=fact_key, applicability=applicability, at=at
        )
        if resolution.status is FactResolutionStatus.KNOWN:
            return resolution, None, False
        need, created = self.store.create_need(
            session_id=session_id, goal_id=goal_id, user_scope=user_scope,
            fact_key=fact_key, question=question, why_needed=why_needed,
            answer_schema=answer_schema, applicability=applicability,
            resolution_status=resolution.status, resume_stage=resume_stage,
            conversation_hint=conversation_hint,
        )
        return resolution, need, created

    def answer(
        self,
        *,
        need_id: str,
        value: Any,
        idempotency_key: str,
        provenance: dict[str, Any] | None = None,
        confidence: float = 1.0,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> tuple[NeedUserFact, UserFactRevision, bool]:
        need = self.store.get_need(need_id)
        _validate_answer(value, need.answer_schema)
        return self.store.answer_need(
            need_id=need_id, value=value, idempotency_key=idempotency_key,
            provenance=provenance, confidence=confidence, valid_from=valid_from,
            valid_until=valid_until,
        )

    def record_trusted_revision(
        self,
        *,
        user_scope: str,
        fact_key: str,
        value: Any,
        applicability: dict[str, Any] | None,
        source_kind: UserFactSourceKind,
        source_ref: str,
        provenance: dict[str, Any],
        confidence: float = 1.0,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> UserFactRevision:
        if source_kind not in {UserFactSourceKind.IMPORTED, UserFactSourceKind.OBSERVED}:
            raise UntrustedFactSource(
                "user answers must be written through a concrete NeedUserFact"
            )
        if not source_ref.strip() or not provenance:
            raise UntrustedFactSource("trusted revisions require source_ref and provenance")
        return self.store.append_revision(
            user_scope=user_scope, fact_key=fact_key, value=value,
            applicability=applicability, source_kind=source_kind,
            source_ref=source_ref, provenance=provenance, confidence=confidence,
            valid_from=valid_from, valid_until=valid_until,
        )


def _applies(candidate: dict[str, Any], requested: dict[str, Any]) -> bool:
    return all(key in requested and requested[key] == value for key, value in candidate.items())


def _scope_key(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _validate_answer(value: Any, schema: dict[str, Any]) -> None:
    expected = schema.get("type")
    valid_type = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "null": value is None,
    }.get(expected, True)
    if not valid_type:
        raise UserFactAnswerValidationError(f"answer must satisfy type={expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise UserFactAnswerValidationError("answer is not one of the allowed values")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise UserFactAnswerValidationError("answer is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise UserFactAnswerValidationError("answer is above maximum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise UserFactAnswerValidationError("answer is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise UserFactAnswerValidationError("answer is longer than maxLength")
