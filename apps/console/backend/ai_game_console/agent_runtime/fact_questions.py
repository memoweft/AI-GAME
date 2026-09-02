"""R8 bridge between UserFact authority and AgentSession wait/wake authority."""

from __future__ import annotations

from typing import Any

from ..user_fact_runtime import NeedUserFact, NeedUserFactStatus, UserFactService
from .domain import (
    ContinuationCheckpointKind,
    ContinuationDraft,
    ContinuationYieldReason,
    SessionEventType,
    WakeConditionDraft,
    WakeConditionKind,
)
from .event_router import classify_event_routing
from .service import AgentSessionService


class FactQuestionCoordinator:
    """Persist an exact question saga without creating a second scheduler."""

    def __init__(self, facts: UserFactService, sessions: AgentSessionService) -> None:
        self.facts = facts
        self.sessions = sessions

    def request_fact(
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
        dispatch: bool = True,
    ) -> tuple[Any, NeedUserFact | None, bool]:
        resolution, need, created = self.facts.resolve_or_create_need(
            session_id=session_id,
            goal_id=goal_id,
            user_scope=user_scope,
            fact_key=fact_key,
            question=question,
            why_needed=why_needed,
            answer_schema=answer_schema,
            applicability=applicability,
            resume_stage=resume_stage,
            conversation_hint=conversation_hint,
        )
        if need is None:
            return resolution, None, False
        self._ensure_wait(need)
        session = self.sessions.store.get_session(session_id)
        if session.active_slice_id is None:
            self.sessions.reconcile_attention(
                session_id,
                trigger_key=f"fact-need:{need.id}:wait",
                dispatch=dispatch,
            )
        return resolution, need, created

    def answer(
        self,
        *,
        need_id: str,
        value: Any,
        idempotency_key: str,
        provenance: dict[str, Any] | None = None,
        dispatch: bool = True,
    ) -> tuple[NeedUserFact, Any, bool]:
        need, revision, created = self.facts.answer(
            need_id=need_id,
            value=value,
            idempotency_key=idempotency_key,
            provenance=provenance or {"source": "console-question-card"},
        )
        self._apply_answer(need, revision, dispatch=dispatch)
        return self.facts.store.get_need(need.id), revision, created

    def recover(self, *, dispatch: bool = True) -> int:
        recovered = 0
        for need in self.facts.store.list_needs(status=NeedUserFactStatus.OPEN):
            self._ensure_wait(need)
            recovered += 1
        for need in self.facts.store.list_needs(status=NeedUserFactStatus.ANSWERED):
            if need.answer_revision_id is None:
                continue
            revision = next(
                (
                    item
                    for item in self.facts.store.revisions(
                        user_scope=need.user_scope, fact_key=need.fact_key
                    )
                    if item.id == need.answer_revision_id
                ),
                None,
            )
            if revision is None:
                continue
            self._apply_answer(need, revision, dispatch=dispatch)
            recovered += 1
        return recovered

    def _ensure_wait(self, need: NeedUserFact) -> None:
        session = self.sessions.store.get_session(need.session_id)
        graph = self.sessions.store.graph_revision(need.session_id)
        if graph is None:
            raise RuntimeError("fact need requires a persisted GoalGraph")
        self.sessions.store.enter_goal_waiting(
            need.session_id,
            need.goal_id,
            ContinuationDraft(
                authority_revision=session.authority_revision,
                graph_revision=graph.revision,
                checkpoint_kind=ContinuationCheckpointKind.WAIT_ENTRY,
                yield_reason=ContinuationYieldReason.WAITING,
                idempotency_key=f"fact-need:{need.id}:wait",
                checkpoint_ref=f"fact-need:{need.id}:wait-entry",
                stage_id=need.resume_stage,
                conversation_id=None,
                waiting_kind=WakeConditionKind.USER_FACT.value,
                waiting_ref=need.id,
                resume_preconditions={
                    "need_id": need.id,
                    "fact_key": need.fact_key,
                    "fresh_observation_required": True,
                },
            ),
            WakeConditionDraft(
                kind=WakeConditionKind.USER_FACT,
                matcher={
                    "event_type": SessionEventType.USER_FACT_ANSWERED.value,
                    "need_id": need.id,
                    "fact_key": need.fact_key,
                },
            ),
        )

    def _apply_answer(self, need: NeedUserFact, revision: Any, *, dispatch: bool) -> None:
        event, _ = self.sessions.store.ingest_event(
            need.session_id,
            source_namespace="user-fact-runtime",
            source_event_id=f"need:{need.id}:revision:{revision.id}",
            event_type=SessionEventType.USER_FACT_ANSWERED,
            occurred_at=revision.created_at.isoformat(),
            payload={
                "need_id": need.id,
                "fact_key": need.fact_key,
                "fact_revision_id": revision.id,
                "goal_id": need.goal_id,
                "urgency": "HIGH",
            },
        )
        routed, wakes = self.sessions.store.route_inbox_event(event.id)
        classification = classify_event_routing(routed, wakes)
        self.sessions.store.record_event_routing(event.id, classification.to_payload())
        if need.goal_id not in {wake.goal_id for wake in wakes}:
            raise RuntimeError("user fact answer did not match its exact origin Goal wake")
        self.sessions.handle_inbox_event(event.id, dispatch=dispatch)
        self.facts.store.mark_need_applied(need.id)


__all__ = ["FactQuestionCoordinator"]
