from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_game_console.agent_runtime.domain import (
    ContinuationCheckpointKind,
    ContinuationDraft,
    ContinuationYieldReason,
    SessionEventType,
)
from ai_game_console.agent_runtime.event_router import SessionEventRouter
from ai_game_console.agent_runtime.scheduler import AttentionScheduler
from ai_game_console.agent_runtime.service import AgentSessionService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore


class RecordingGoalService:
    """Small GoalRuntime double that records every owner-side dispatch."""

    def __init__(self, trace: list[str] | None = None) -> None:
        self.by_key: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, ...]] = []
        self.trace = trace if trace is not None else []

    def prepare_goal(self, goal: str, key: str) -> dict[str, str]:
        self.calls.append(("prepare", key))
        return self.by_key.setdefault(
            key,
            {
                "id": f"goal-{len(self.by_key) + 1}",
                "original_goal": goal,
                "execution_status": "ACCEPTED",
                "control_state": "AUTOMATED",
            },
        )

    def activate_goal(self, goal_id: str) -> dict[str, str]:
        self.calls.append(("activate", goal_id))
        self.trace.append("goal:activate")
        return self.inspect(goal_id)

    def activate_selected_goal(self, goal_id: str, key: str) -> dict[str, str]:
        self.calls.append(("activate_selected", goal_id, key))
        self.trace.append("goal:activate_selected")
        return self.inspect(goal_id)

    def pause_goal_at_checkpoint(self, goal_id: str, key: str) -> None:
        self.calls.append(("pause_checkpoint", goal_id, key))

    def goal_available_for_activation(self, goal_id: str) -> bool:
        self.inspect(goal_id)
        return True

    def inspect(self, goal_id: str) -> dict[str, str]:
        return next(value for value in self.by_key.values() if value["id"] == goal_id)

    def control(self, goal_id: str, action: str, key: str) -> dict[str, str]:
        self.calls.append(("control", goal_id, action, key))
        return self.inspect(goal_id)

    @property
    def dispatch_count(self) -> int:
        return sum(
            call[0] in {"activate", "activate_selected"}
            for call in self.calls
        )


class RecordingContextRecovery:
    """Return a unique fresh snapshot reference for each recovery request."""

    def __init__(self, trace: list[str] | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.trace = trace if trace is not None else []

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        self.trace.append("context:fresh_snapshot")
        return f"snapshot:fresh:{len(self.calls)}"


@dataclass(slots=True)
class UserActiveRuntime:
    database: Path
    store: SQLiteAgentRuntimeStore
    goals: RecordingGoalService
    recovery: RecordingContextRecovery
    service: AgentSessionService
    router: SessionEventRouter
    session_id: str
    goal_id: str
    slice_id: str
    trace: list[str]

    @property
    def session(self):
        return self.store.get_session(self.session_id)


def create_user_active_runtime(tmp_path: Path) -> UserActiveRuntime:
    database = tmp_path / "agent-runtime.db"
    store = SQLiteAgentRuntimeStore(database)
    trace: list[str] = []
    goals = RecordingGoalService(trace)
    recovery = RecordingContextRecovery(trace)
    service = build_service(store, goals, recovery, recover_on_start=False)
    created = service.create("在微信处理一条消息", "r7-user-active-session")
    decision = store.latest_attention_decision(created["id"])
    assert decision is not None
    slice_id = "activity-slice:r7-user-active"
    store.claim_active_slice(
        created["id"],
        goal_id=created["goal_nodes"][0]["id"],
        attention_decision_id=decision.id,
        slice_id=slice_id,
    )
    return UserActiveRuntime(
        database=database,
        store=store,
        goals=goals,
        recovery=recovery,
        service=service,
        router=SessionEventRouter(store),
        session_id=created["id"],
        goal_id=created["goal_nodes"][0]["id"],
        slice_id=slice_id,
        trace=trace,
    )


def build_service(
    store: SQLiteAgentRuntimeStore,
    goals: RecordingGoalService,
    recovery: RecordingContextRecovery,
    *,
    recover_on_start: bool,
) -> AgentSessionService:
    kwargs: dict[str, Any] = {
        "attention_scheduler": AttentionScheduler(),
        "recover_on_start": recover_on_start,
    }
    # This lets the same acceptance file expose the next missing production
    # seam instead of failing every case during constructor collection.  Once
    # R7 supplies the frozen public parameter, every recovery case uses it.
    if "context_recovery" in inspect.signature(AgentSessionService).parameters:
        kwargs["context_recovery"] = recovery
    return AgentSessionService(store, goals, **kwargs)


def ingest_event(
    runtime: UserActiveRuntime,
    event_type: SessionEventType,
    source_event_id: str,
    *,
    occurred_at: str,
    payload: dict[str, Any],
):
    return runtime.router.ingest(
        runtime.session_id,
        source_namespace="android-companion-v1",
        source_event_id=source_event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        payload=payload,
        device_id="android:test-device",
        device_boot_id="boot-r7",
        source_cursor=source_event_id,
    )


def enter_user_active_at_verified_checkpoint(
    runtime: UserActiveRuntime,
    *,
    request: Any,
) -> Any:
    graph = runtime.store.graph_revision(runtime.session_id)
    assert graph is not None
    decision = runtime.store.latest_attention_decision(runtime.session_id)
    continuation, _ = runtime.store.append_continuation(
        runtime.session_id,
        runtime.goal_id,
        ContinuationDraft(
            authority_revision=runtime.session.authority_revision,
            graph_revision=graph.revision,
            attention_decision_id=decision.id if decision is not None else None,
            checkpoint_kind=ContinuationCheckpointKind.VERIFIED_ACTION,
            checkpoint_ref="verified-step:1",
            verified_fact_refs=("verification:step-1",),
            yield_reason=ContinuationYieldReason.USER_ACTIVITY,
            idempotency_key=f"preemption:{request.id}:verified-step:1",
            resume_preconditions={"fresh_observation_required": True},
        ),
    )
    checkpointed = runtime.store.checkpoint_preemption_requests(
        runtime.session_id,
        slice_id=runtime.slice_id,
        observed_event_cursor=runtime.store.get_session(runtime.session_id).event_cursor,
        checkpoint_ref="verified-step:1",
        continuation_id=continuation.id,
    )
    assert len(checkpointed) == 1
    runtime.store.release_active_slice(runtime.session_id, slice_id=runtime.slice_id)
    # This is the same callback that the normal ActivitySlice preemption
    # coordinator makes only after it has persisted the checkpoint and
    # released the Slice authority.
    runtime.service.handle_inbox_event(request.event_id)
    transition = runtime.store.control_transitions(runtime.session_id)[-1]
    return checkpointed[0], transition, continuation
