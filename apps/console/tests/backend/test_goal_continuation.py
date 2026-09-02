from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ai_game_console.agent_runtime.domain import (
    AttentionDecisionDraft,
    AttentionDecisionOutcome,
    AttentionSelectorKind,
    ContinuationCheckpointKind,
    ContinuationDraft,
    ContinuationYieldReason,
    GoalEligibilityDraft,
    GoalEligibilityStatus,
    GoalGraphRevisionDraft,
    GoalNodeDraft,
    GoalNodeStatus,
    SessionIdempotencyConflict,
    SessionEventType,
    WakeConditionDraft,
    WakeConditionKind,
)
from ai_game_console.agent_runtime.agenda import AgendaContext, AgendaGoalSnapshot
from ai_game_console.agent_runtime.scheduler import AttentionScheduler
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore


def _session_with_goals(store: SQLiteAgentRuntimeStore):
    session, _ = store.create_unplanned_session(instruction="A；B", client_request_id="create-1")
    directive = store.directives(session.id)[0]
    goal_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(
            authority_revision=1, source_directive_id=directive.id, base_graph_revision=0
        ),
        nodes=[
            GoalNodeDraft(id=goal_id, title=title, source_directive_id=directive.id, original_fragment=title)
            for goal_id, title in zip(goal_ids, ("A", "B"), strict=True)
        ],
    )
    return session.id, goal_ids


def _candidate(goal_id: str, rank: int = 1) -> GoalEligibilityDraft:
    return GoalEligibilityDraft(
        goal_id=goal_id,
        eligibility=GoalEligibilityStatus.ELIGIBLE,
        reason="ready",
        goal_status=GoalNodeStatus.READY,
        total_score=100,
        hard_tier=1,
        rank=rank,
    )


def test_attention_decision_and_reason_are_persisted(tmp_path):
    path = tmp_path / "agent-runtime.db"
    store = SQLiteAgentRuntimeStore(path)
    session_id, (first, second) = _session_with_goals(store)
    session = store.get_session(session_id)
    first_decision, created, first_dispatch = store.commit_attention_decision(
        session_id,
        draft=AttentionDecisionDraft(
            trigger_key="test:first",
            authority_revision=1,
            graph_revision=1,
            event_cursor=session.event_cursor,
            outcome=AttentionDecisionOutcome.SELECTED,
            selected_goal_id=first,
            selector_kind=AttentionSelectorKind.DETERMINISTIC,
            reason="first is ready",
            selected_hard_tier=1,
        ),
        candidates=[_candidate(first), _candidate(second, 2)],
    )
    assert created is True and first_dispatch is not None
    assert store.get_session(session_id).active_goal_id == first

    snapshot = store.get_session(session_id)
    continuation = ContinuationDraft(
        authority_revision=1,
        graph_revision=1,
        checkpoint_kind=ContinuationCheckpointKind.SCHEDULER_BOUNDARY,
        yield_reason=ContinuationYieldReason.PREEMPTED,
        idempotency_key="continuation:first-to-second",
        stage_id="stage-A",
        pending_intent={"kind": "resume"},
        resume_preconditions={"screen": "known"},
    )
    second_decision, _, second_dispatch = store.commit_attention_decision(
        session_id,
        draft=AttentionDecisionDraft(
            trigger_key="test:second",
            authority_revision=1,
            graph_revision=1,
            event_cursor=snapshot.event_cursor,
            outcome=AttentionDecisionOutcome.SELECTED,
            selected_goal_id=second,
            selector_kind=AttentionSelectorKind.DETERMINISTIC,
            reason="second became urgent",
            selected_hard_tier=2,
        ),
        candidates=[_candidate(second)],
        previous_continuation=(first, continuation),
    )
    assert second_dispatch is not None

    restarted = SQLiteAgentRuntimeStore(path)
    restored = restarted.latest_attention_decision(session_id)
    saved = restarted.latest_continuation(first)
    assert restored == second_decision
    assert restored.id != first_decision.id
    assert saved is not None and saved.stage_id == "stage-A"
    assert saved.pending_intent == {"kind": "resume"}
    assert restarted.get_session(session_id).active_goal_id == second
    assert [item.id for item in restarted.pending_attention_dispatches(session_id)] == [
        first_dispatch.id,
        second_dispatch.id,
    ]


def test_continuation_is_append_only_and_idempotent_by_payload(tmp_path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session_id, (goal_id, _) = _session_with_goals(store)
    draft = ContinuationDraft(
        authority_revision=1,
        graph_revision=1,
        checkpoint_kind=ContinuationCheckpointKind.NO_INFLIGHT_ACTION,
        yield_reason=ContinuationYieldReason.PROCESS_RESTART,
        idempotency_key="restart:1",
    )
    first, created = store.append_continuation(session_id, goal_id, draft)
    replay, replay_created = store.append_continuation(session_id, goal_id, draft)
    assert created is True and replay_created is False and replay.id == first.id
    with pytest.raises(SessionIdempotencyConflict):
        store.append_continuation(
            session_id,
            goal_id,
            ContinuationDraft(
                authority_revision=1,
                graph_revision=1,
                checkpoint_kind=ContinuationCheckpointKind.WAIT_ENTRY,
                yield_reason=ContinuationYieldReason.WAITING,
                idempotency_key="restart:1",
            ),
        )


def test_enter_goal_waiting_commits_continuation_wake_and_goal_state_together(tmp_path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session_id, (goal_id, _) = _session_with_goals(store)
    continuation_draft = ContinuationDraft(
        authority_revision=1,
        graph_revision=1,
        checkpoint_kind=ContinuationCheckpointKind.WAIT_ENTRY,
        yield_reason=ContinuationYieldReason.WAITING,
        idempotency_key="wait:event-1",
        waiting_kind="EVENT",
        resume_preconditions={"wake_condition_required": True},
    )
    wake_draft = WakeConditionDraft(
        kind=WakeConditionKind.EVENT,
        matcher={"application_package": "com.tencent.mm", "conversation_hint": "A"},
    )

    continuation, wake = store.enter_goal_waiting(
        session_id, goal_id, continuation_draft, wake_draft
    )
    replay_continuation, replay_wake = store.enter_goal_waiting(
        session_id, goal_id, continuation_draft, wake_draft
    )

    node = next(item for item in store.goal_nodes(session_id) if item.id == goal_id)
    assert node.status is GoalNodeStatus.WAITING_EVENT
    assert node.continuation_id == continuation.id
    assert node.waiting_ref == wake.id
    assert replay_continuation.id == continuation.id
    assert replay_wake.id == wake.id
    assert len(store.continuations(goal_id)) == 1
    assert len(store.wake_conditions(session_id, pending_only=True)) == 1

    waited_since = "2026-08-24T05:50:00Z"
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE goal_nodes SET wait_started_at=? WHERE goal_node_id=?",
            (waited_since, goal_id),
        )
    event, _ = store.ingest_event(
        session_id,
        source_namespace="test:wake-age",
        source_event_id="notification-A",
        event_type=SessionEventType.NOTIFICATION_POSTED,
        payload={"application_package": "com.tencent.mm", "conversation_hint": "A"},
        occurred_at="2026-08-24T06:00:00Z",
    )
    store.route_inbox_event(event.id)
    awakened = next(item for item in store.goal_nodes(session_id) if item.id == goal_id)
    assert awakened.status is GoalNodeStatus.READY
    assert awakened.wait_started_at == waited_since
    plan = AttentionScheduler().request_attention(
        session_id=session_id,
        trigger_key="test:wake-age",
        original_instruction="A；B",
        authority_revision=1,
        goals=(AgendaGoalSnapshot.from_goal_node(awakened, binding_ready=True),),
        context=AgendaContext(),
        now=datetime(2026, 8, 24, 6, 0, tzinfo=UTC),
    )
    assert plan.evaluation.for_goal(goal_id).waiting_age_component == 10
    store.commit_attention_decision(
        session_id,
        draft=plan.to_decision_draft(
            authority_revision=1,
            graph_revision=1,
            event_cursor=store.get_session(session_id).event_cursor,
            trigger_event_id=event.id,
        ),
        candidates=plan.evaluation.all_goals,
    )
    selected = next(item for item in store.goal_nodes(session_id) if item.id == goal_id)
    assert selected.wait_started_at is None


def test_device_busy_yield_is_atomic_and_old_dispatch_cannot_reactivate_waiting_goal(tmp_path):
    path = tmp_path / "agent-runtime.db"
    store = SQLiteAgentRuntimeStore(path)
    session_id, (selected_id, sibling_id) = _session_with_goals(store)
    store.set_goal_ready(session_id, selected_id)
    store.set_goal_ready(session_id, sibling_id)
    session = store.get_session(session_id)
    _, _, selected_dispatch = store.commit_attention_decision(
        session_id,
        draft=AttentionDecisionDraft(
            trigger_key="device-busy:initial",
            authority_revision=1,
            graph_revision=1,
            event_cursor=session.event_cursor,
            outcome=AttentionDecisionOutcome.SELECTED,
            selected_goal_id=selected_id,
            selector_kind=AttentionSelectorKind.DETERMINISTIC,
            reason="initial selection",
        ),
        candidates=[_candidate(selected_id), _candidate(sibling_id, 2)],
    )
    assert selected_dispatch is not None
    occurred = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)
    retry_at = occurred + timedelta(minutes=1)
    continuation_draft = ContinuationDraft(
        authority_revision=1,
        graph_revision=1,
        checkpoint_kind=ContinuationCheckpointKind.NO_INFLIGHT_ACTION,
        yield_reason=ContinuationYieldReason.DEVICE_BUSY,
        idempotency_key=f"dispatch:{selected_dispatch.id}:device-busy:continuation",
        pending_intent={"kind": "activate-selected-goal"},
        resume_preconditions={"device_available": True},
    )
    wake_draft = WakeConditionDraft(
        kind=WakeConditionKind.DEVICE,
        matcher={"event_type": "DeviceAvailableEvent"},
    )

    yielded, busy_event, continuation, wake = store.yield_attention_dispatch_device_busy(
        selected_dispatch.id,
        continuation_draft,
        wake_draft,
        occurred_at=occurred.isoformat().replace("+00:00", "Z"),
        retry_at=retry_at.isoformat().replace("+00:00", "Z"),
    )
    replay = store.yield_attention_dispatch_device_busy(
        selected_dispatch.id,
        continuation_draft,
        wake_draft,
        occurred_at=occurred.isoformat().replace("+00:00", "Z"),
        retry_at=retry_at.isoformat().replace("+00:00", "Z"),
    )
    assert yielded.status.value == "RETRYABLE"
    assert busy_event.id == f"dispatch:{selected_dispatch.id}:device-busy"
    assert replay[1].id == busy_event.id
    assert replay[2].id == continuation.id
    assert replay[3].id == wake.id
    assert store.get_session(session_id).active_goal_id is None
    nodes = {item.id: item for item in store.goal_nodes(session_id)}
    assert nodes[selected_id].status is GoalNodeStatus.WAITING_DEVICE
    assert nodes[selected_id].backoff_until == retry_at.isoformat().replace("+00:00", "Z")
    assert nodes[selected_id].consecutive_failure_count == 1
    assert nodes[sibling_id].status is GoalNodeStatus.READY
    assert [item.id for item in store.pending_attention_dispatches(session_id)] == [
        selected_dispatch.id
    ]

    restarted = SQLiteAgentRuntimeStore(path)
    assert [item.id for item in restarted.pending_attention_dispatches(session_id)] == [
        selected_dispatch.id
    ]
    pending_events = restarted.pending_inbox_events(session_id)
    assert [item.id for item in pending_events] == [busy_event.id]
    routed, _ = restarted.route_inbox_event(busy_event.id)
    assert routed.handling_status.value == "CLASSIFIED"

    session = restarted.get_session(session_id)
    goals = restarted.goal_nodes(session_id)
    plan = AttentionScheduler().request_attention(
        session_id=session_id,
        trigger_key=f"event:{busy_event.id}",
        original_instruction=session.original_instruction,
        authority_revision=session.authority_revision,
        goals=tuple(
            AgendaGoalSnapshot.from_goal_node(
                item, binding_ready=True, switch_checkpoint_ready=True
            )
            for item in goals
        ),
        context=AgendaContext(),
        now=occurred,
    )
    assert plan.selected_goal_id == sibling_id
    assert plan.evaluation.for_goal(selected_id).recent_failure_component == 200
    decision, _, replacement_dispatch = restarted.commit_attention_decision(
        session_id,
        draft=plan.to_decision_draft(
            authority_revision=1,
            graph_revision=1,
            event_cursor=restarted.get_session(session_id).event_cursor,
            trigger_event_id=busy_event.id,
        ),
        candidates=plan.evaluation.all_goals,
    )
    assert decision.selected_goal_id == sibling_id
    assert replacement_dispatch is not None
    pending_by_id = {
        item.id: item.status.value
        for item in restarted.pending_attention_dispatches(session_id)
    }
    assert pending_by_id == {
        selected_dispatch.id: "RETRYABLE",
        replacement_dispatch.id: "PENDING",
    }
