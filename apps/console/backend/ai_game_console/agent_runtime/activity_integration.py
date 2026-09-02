"""Normal-composition adapter between AgentSession facts and ActivitySlice."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any, Callable

from ..kernel_canary import KernelApplicationCycleResult
from ..long_lived_mobile_application_composition import (
    ActivitySliceDispatchRequest,
    ActivitySliceDispatchResult,
    ActivitySliceStepDispatch,
)
from .activity_slice import (
    ActivitySlice,
    ActivitySliceStatus,
    ActivitySliceSupervisor,
    ActivityStepContext,
    ActivityStepPhase,
    BoundarySnapshot,
    ContinuationDirective,
    SliceBoundaryRequest,
    SliceBudget,
    SliceYieldReason,
    StepExecutionResult,
    StepOutcome,
    StepProgress,
    WakePlanKind,
)
from .activity_store import ActivitySliceStore
from .domain import (
    ContinuationCheckpointKind,
    ContinuationDraft,
    ContinuationYieldReason,
)
from .store import SQLiteAgentRuntimeStore
from .preemption import (
    ActivitySlicePreemptionCoordinatorPort,
    PersistedPreemptionHandoff,
    restore_persisted_preemption_handoff,
)


class AgentRuntimeActivitySlicePreemptionCoordinator:
    """Consume R7 Slice handoffs through the durable AgentRuntime saga."""

    def __init__(
        self,
        store: SQLiteAgentRuntimeStore,
        *,
        event_handler: Callable[[str], Any] | None = None,
    ) -> None:
        self.store = store
        self.event_handler = event_handler

    def bind_event_handler(self, event_handler: Callable[[str], Any]) -> None:
        self.event_handler = event_handler

    def claim_active_slice(
        self,
        *,
        session_id: str,
        goal_id: str,
        attention_decision_id: str,
        activity_slice_id: str,
        slice_idempotency_key: str,
    ) -> None:
        # The deterministic Slice key is validated by ActivitySliceStore; the
        # AgentRuntime fence records only the canonical Slice identity.
        if not slice_idempotency_key.strip():
            raise ValueError("slice idempotency key must not be blank")
        self.store.claim_active_slice(
            session_id,
            goal_id=goal_id,
            attention_decision_id=attention_decision_id,
            slice_id=activity_slice_id,
        )

    def settle_preemption_checkpoint(
        self, handoff: PersistedPreemptionHandoff
    ) -> None:
        request = handoff.request
        session = self.store.get_session(request.session_id)
        if (
            session.active_slice_id != request.activity_slice_id
            or session.active_goal_id != request.current_goal_id
            or session.current_attention_decision_id
            != request.attention_decision_id
        ):
            raise RuntimeError("preemption handoff no longer owns Session authority")

        # Close the rare route-handler race: the boundary may observe the
        # committed EventInbox cursor just before its HTTP intake thread asks
        # the service to create the durable request.
        pending_events = [
            event
            for event in self.store.pending_inbox_events(request.session_id)
            if event.cursor <= request.event_cursor
        ]
        existing_request_event_ids = {
            item.event_id
            for item in self.store.pending_preemption_requests(request.session_id)
        }
        for event in pending_events:
            if event.event_type.value not in {
                "NotificationPostedEvent",
                "NotificationRemovedEvent",
                "ForegroundApplicationChangedEvent",
                "TimerDueEvent",
                "HumanTouchStartedEvent",
                "HumanIdleEvent",
                "DeviceAvailableEvent",
                "CompanionConnectedEvent",
            }:
                continue
            if (
                event.event_type.value
                not in {"HumanTouchStartedEvent", "HumanIdleEvent"}
                and not event.affected_goal_ids
            ):
                continue
            if event.id in existing_request_event_ids:
                continue
            self.store.create_preemption_request(
                event.id,
                reason=f"{event.event_type.value} reached verified Slice boundary",
            )

        graph = self.store.graph_revision(request.session_id)
        if graph is None:
            raise RuntimeError("preemption handoff Session has no GoalGraph")
        event_by_id = {
            event.id: event
            for event in self.store.pending_inbox_events(request.session_id)
        }
        pending_requests = [
            item
            for item in self.store.pending_preemption_requests(request.session_id)
            if item.prior_slice_id == request.activity_slice_id
        ]
        user_activity = any(
            event_by_id.get(item.event_id) is not None
            and event_by_id[item.event_id].event_type.value
            == "HumanTouchStartedEvent"
            for item in pending_requests
        )
        continuation, _created = self.store.append_continuation(
            request.session_id,
            request.current_goal_id,
            ContinuationDraft(
                authority_revision=session.authority_revision,
                graph_revision=graph.revision,
                attention_decision_id=session.current_attention_decision_id,
                checkpoint_kind=(
                    ContinuationCheckpointKind.VERIFIED_ACTION
                    if request.last_settled_step_id is not None
                    else ContinuationCheckpointKind.NO_INFLIGHT_ACTION
                ),
                yield_reason=(
                    ContinuationYieldReason.USER_ACTIVITY
                    if user_activity
                    else ContinuationYieldReason.PREEMPTED
                ),
                idempotency_key=f"{request.idempotency_key}:goal-continuation",
                checkpoint_ref=request.checkpoint_ref,
                resume_preconditions={
                    "fresh_observation_required": True,
                    "scheduler_reselection_required": True,
                },
                waiting_ref=(
                    pending_requests[0].event_id if pending_requests else None
                ),
            ),
        )
        checked = self.store.checkpoint_preemption_requests(
            request.session_id,
            slice_id=request.activity_slice_id,
            observed_event_cursor=request.event_cursor,
            checkpoint_ref=request.checkpoint_ref,
            continuation_id=continuation.id,
        )
        self.store.release_active_slice(
            request.session_id, slice_id=request.activity_slice_id
        )
        if self.event_handler is not None:
            events = {event.id: event for event in pending_events}
            for item in sorted(
                checked,
                key=lambda value: events.get(value.event_id).cursor
                if events.get(value.event_id) is not None
                else 0,
            ):
                self.event_handler(item.event_id)


class NormalActivitySliceRunner:
    """Concrete synchronous runner used by the normal kernel-active graph."""

    def __init__(
        self,
        store: ActivitySliceStore,
        *,
        agent_runtime_store: SQLiteAgentRuntimeStore,
        device_body_store: Any,
        observation_provider: Any,
        preemption_coordinator: ActivitySlicePreemptionCoordinatorPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.agent_runtime_store = agent_runtime_store
        self.device_body_store = device_body_store
        self.observation_provider = observation_provider
        self.preemption_coordinator = preemption_coordinator
        self.fact_question_coordinator: Any | None = None
        self.clock = clock or (lambda: datetime.now(UTC))
        self._local = threading.local()
        self.supervisor = ActivitySliceSupervisor(
            store, self, self, clock=self.clock
        )

    def bind_preemption_coordinator(
        self, coordinator: ActivitySlicePreemptionCoordinatorPort
    ) -> None:
        self.preemption_coordinator = coordinator

    def bind_fact_question_coordinator(self, coordinator: Any) -> None:
        self.fact_question_coordinator = coordinator

    def dispatch_activity_slice(
        self,
        request: ActivitySliceDispatchRequest,
        execute_step: Callable[
            [ActivitySliceStepDispatch], KernelApplicationCycleResult
        ],
    ) -> ActivitySliceDispatchResult:
        existing = self._slice_by_key(request.idempotency_key)
        if existing is not None:
            return self._project(existing)

        binding, session, decision = self._selected_authority(request.goal_id)
        device_binding = self.device_body_store.binding_for_session(session.id)
        if device_binding is not None and device_binding.device_id != request.target_id:
            raise RuntimeError("ActivitySlice target is not the selected Session device")
        activity_slice, created = self.supervisor.start_slice(
            session_id=session.id,
            goal_id=binding.goal_node_id,
            attention_decision_id=decision.id,
            attention_decision_revision=decision.decision_revision,
            objective=request.objective,
            budget=SliceBudget(
                time_budget_seconds=decision.slice_budget.time_budget_ms / 1000,
                action_limit=decision.slice_budget.action_budget,
            ),
            directive_revision=session.authority_revision,
            event_cursor=session.event_cursor,
            idempotency_key=request.idempotency_key,
        )
        if not created:
            return self._project(activity_slice)
        if self.preemption_coordinator is not None:
            self.preemption_coordinator.claim_active_slice(
                session_id=activity_slice.session_id,
                goal_id=activity_slice.goal_id,
                attention_decision_id=activity_slice.attention_decision_id,
                activity_slice_id=activity_slice.id,
                slice_idempotency_key=activity_slice.idempotency_key,
            )
        self._local.execute_step = execute_step
        self._local.target_id = request.target_id
        try:
            result = self.supervisor.run_slice(activity_slice.id)
            if result.preemption_handoff is not None:
                if self.preemption_coordinator is None:
                    raise RuntimeError(
                        "R7 event preemption requires an AgentRuntime coordinator"
                    )
                self.preemption_coordinator.settle_preemption_checkpoint(
                    result.preemption_handoff
                )
            else:
                need = self._settle_fact_wait(result)
                self.agent_runtime_store.release_active_slice(
                    activity_slice.session_id, slice_id=activity_slice.id
                )
                if need is not None:
                    self.fact_question_coordinator.sessions.reconcile_attention(
                        activity_slice.session_id,
                        trigger_key=f"fact-need:{need.id}:wait",
                    )
            return self._project(result.slice)
        finally:
            self._local.execute_step = None
            self._local.target_id = None

    def inspect_activity_slice(self, receipt_id: str) -> ActivitySliceDispatchResult:
        return self._project(self.store.get_slice(receipt_id))

    def reconcile_activity_slice(
        self, idempotency_key: str
    ) -> ActivitySliceDispatchResult | None:
        activity_slice = self._slice_by_key(idempotency_key)
        if activity_slice is None:
            return None
        if activity_slice.status is ActivitySliceStatus.RUNNING:
            now = self.clock().astimezone(UTC)
            continuation = ContinuationDirective(
                idempotency_key=(
                    f"{activity_slice.idempotency_key}:yield:"
                    f"{SliceYieldReason.PROCESS_RESTART.value}:"
                    f"{activity_slice.next_step_ordinal}"
                ),
                slice_id=activity_slice.id,
                session_id=activity_slice.session_id,
                goal_id=activity_slice.goal_id,
                reason=SliceYieldReason.PROCESS_RESTART,
                last_settled_step_id=activity_slice.last_step_id,
                resume_step_ordinal=activity_slice.next_step_ordinal,
                observed_directive_revision=activity_slice.observed_directive_revision,
                observed_event_cursor=activity_slice.observed_event_cursor,
                remaining_time_seconds=max(
                    0.0,
                    (
                        datetime.fromisoformat(
                            activity_slice.deadline_at.replace("Z", "+00:00")
                        )
                        - now
                    ).total_seconds(),
                ),
                remaining_actions=activity_slice.remaining_actions,
                checkpoint_ref=(
                    f"slice:{activity_slice.id}:step:"
                    f"{activity_slice.next_step_ordinal - 1}:settled"
                ),
                wake_plan_id=None,
                created_at=now.isoformat().replace("+00:00", "Z"),
            )
            activity_slice = self.store.yield_slice(
                activity_slice.id,
                status=ActivitySliceStatus.YIELDED,
                reason=SliceYieldReason.PROCESS_RESTART,
                continuation=continuation,
                boundary=None,
                now=now,
            )
            self.agent_runtime_store.release_active_slice(
                activity_slice.session_id, slice_id=activity_slice.id
            )
        elif (
            activity_slice.status is ActivitySliceStatus.YIELDED
            and activity_slice.yield_reason is SliceYieldReason.EVENT_AVAILABLE
            and self.preemption_coordinator is not None
        ):
            # The outer ApplicationRuntime may time out while a slow local
            # model is still finishing verification.  In that race the Slice
            # can durably yield after the original dispatch caller has already
            # moved to reconciliation, leaving the AgentRuntime preemption
            # request at PENDING_CHECKPOINT.  Replay the persisted handoff
            # exactly once while this Slice still owns Session authority.
            session = self.agent_runtime_store.get_session(activity_slice.session_id)
            if session.active_slice_id == activity_slice.id:
                handoff = restore_persisted_preemption_handoff(
                    activity_slice, self.store.continuation(activity_slice.id)
                )
                self.preemption_coordinator.settle_preemption_checkpoint(handoff)
        return self._project(activity_slice)

    def recover_persisted_preemptions(self) -> int:
        """Close yielded Slice handoffs before startup replays EventInbox rows."""

        recovered = 0
        for session in self.agent_runtime_store.sessions_for_recovery():
            if session.active_slice_id is None:
                continue
            try:
                activity_slice = self.store.get_slice(session.active_slice_id)
            except KeyError:
                continue
            if (
                activity_slice.status is ActivitySliceStatus.WAITING
                and activity_slice.yield_reason is SliceYieldReason.NEED_USER_FACT
            ):
                need = self._settle_fact_wait(
                    self.supervisor._result(activity_slice)  # durable read-only projection
                )
                self.agent_runtime_store.release_active_slice(
                    activity_slice.session_id, slice_id=activity_slice.id
                )
                if need is not None:
                    recovered += 1
                continue
            if (
                activity_slice.status is not ActivitySliceStatus.YIELDED
                or activity_slice.yield_reason is not SliceYieldReason.EVENT_AVAILABLE
            ):
                continue
            self.reconcile_activity_slice(activity_slice.idempotency_key)
            recovered += 1
        return recovered

    def _settle_fact_wait(self, result: Any) -> Any | None:
        wake = result.wake_plan
        if wake is None or wake.kind is not WakePlanKind.USER_FACT:
            return None
        if self.fact_question_coordinator is None:
            raise RuntimeError("user-fact wait requires FactQuestionCoordinator")
        steps = tuple(result.steps)
        proposal = dict(steps[-1].result_facts.get("fact_need") or {}) if steps else {}
        required = ("fact_key", "question", "why_needed", "answer_schema")
        if any(not proposal.get(key) for key in required):
            raise RuntimeError("R8 fact need proposal is missing its question contract")
        _resolution, need, _created = self.fact_question_coordinator.request_fact(
            session_id=result.slice.session_id,
            goal_id=result.slice.goal_id,
            user_scope=str(proposal.get("user_scope") or "local-owner"),
            fact_key=str(proposal["fact_key"]),
            question=str(proposal["question"]),
            why_needed=str(proposal["why_needed"]),
            answer_schema=dict(proposal["answer_schema"]),
            applicability=dict(proposal.get("applicability") or {}),
            resume_stage=str(proposal.get("resume_stage") or result.slice.objective),
            conversation_hint=(
                str(proposal["conversation_hint"])
                if proposal.get("conversation_hint") is not None
                else None
            ),
            dispatch=False,
        )
        return need

    def active_activity_step(
        self, idempotency_key: str
    ) -> ActivitySliceStepDispatch | None:
        activity_slice = self._slice_by_key(idempotency_key)
        if activity_slice is None or activity_slice.status is not ActivitySliceStatus.RUNNING:
            return None
        step = self.store.pending_step(activity_slice.id)
        if step is None:
            return None
        return ActivitySliceStepDispatch(step.id, step.ordinal, step.idempotency_key)

    def capture_boundary(self, request: SliceBoundaryRequest) -> BoundarySnapshot:
        session = self.agent_runtime_store.get_session(request.slice.session_id)
        decision = self.agent_runtime_store.attention_decision(
            request.slice.attention_decision_id
        )
        if decision is None:
            raise RuntimeError("ActivitySlice AttentionDecision no longer exists")
        device_binding = self.device_body_store.binding_for_session(session.id)
        target_id = (
            device_binding.device_id
            if device_binding is not None
            else str(getattr(self._local, "target_id", "") or "").strip()
        )
        if not target_id:
            raise RuntimeError(
                "ActivitySlice cannot recover an unbound Session without its durable target"
            )
        state = self.observation_provider.read_device_state(target_id)
        material = json.dumps(
            {
                "slice_id": request.slice.id,
                "ordinal": request.slice.next_step_ordinal,
                "captured_at": state.captured_at,
                "foreground_app": state.foreground_app,
                "authority_revision": session.authority_revision,
                "control_mode": session.control_mode.value,
                "event_cursor": session.event_cursor,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        human_active = (
            session.status.value == "USER_ACTIVE"
            or session.control_mode.value == "USER_ACTIVE"
        )
        return BoundarySnapshot(
            captured_at=str(state.captured_at),
            fresh_observation_ref=(
                "device-state:v1:" + hashlib.sha256(material.encode()).hexdigest()
            ),
            directive_revision=session.authority_revision,
            event_cursor=session.event_cursor,
            human_active=human_active,
            control_mode=session.control_mode.value,
            stop_requested=(
                session.status.value in {"STOPPING", "STOPPED"}
                or session.control_mode.value in {"STOPPING", "STOPPED"}
            ),
            directive_requires_yield=(
                session.authority_revision > request.slice.observed_directive_revision
            ),
            event_requires_yield=(
                session.event_cursor > request.slice.observed_event_cursor
            ),
            facts={
                "attention_decision_id": decision.id,
                "decision_event_cursor": decision.event_cursor,
                "device_binding_id": device_binding.id if device_binding is not None else None,
                "device_event_cursor": (
                    device_binding.event_cursor if device_binding is not None else 0
                ),
                "target_id": target_id,
                "foreground_app": state.foreground_app,
            },
        )

    def execute_activity_step(self, context: ActivityStepContext) -> StepExecutionResult:
        callback = getattr(self._local, "execute_step", None)
        if callback is None:
            raise RuntimeError("ActivitySlice step execution is available only during dispatch")
        dispatch = ActivitySliceStepDispatch(
            context.step.id, context.step.ordinal, context.step.idempotency_key
        )
        context.record_progress(
            StepProgress(
                ActivityStepPhase.DECISION_COMMITTED,
                facts={"objective": context.slice.objective},
            )
        )
        context.record_progress(StepProgress(ActivityStepPhase.DISPATCHING))
        result = callback(dispatch)
        facts = {
            "kernel_task_id": result.task_id,
            "kernel_cycle_key": result.cycle_key,
            "kernel_status": result.status,
            "kernel_evidence": result.evidence,
            "kernel_before_evidence_id": result.before_evidence_id,
            "kernel_after_evidence_id": result.after_evidence_id,
        }
        if result.fact_need is not None:
            if result.physical_action_sent:
                raise RuntimeError("a fact need cannot be combined with a physical action")
            proposal = dict(result.fact_need)
            fact_key = str(proposal.get("fact_key") or "").strip()
            if not fact_key:
                raise RuntimeError("fact need proposal requires fact_key")
            return StepExecutionResult(
                StepOutcome.NEED_USER_FACT,
                user_fact_key=fact_key,
                decision={"kernel_status": result.status, "fact_status": proposal.get("status", "UNKNOWN")},
                facts={**facts, "fact_need": proposal},
            )
        if not result.physical_action_sent:
            return StepExecutionResult(
                StepOutcome.YIELD_TO_SCHEDULER,
                decision={"kernel_status": result.status},
                facts=facts,
            )
        if not all(
            (result.action_id, result.execution_id, result.after_evidence_id, result.verification_id)
        ):
            raise RuntimeError("physical Kernel result is missing durable step evidence")
        progress = dict(
            physical_action_ref=result.action_id,
            action_receipt_ref=result.execution_id,
            after_observation_ref=result.after_evidence_id,
            verification_ref=result.verification_id,
        )
        context.record_progress(
            StepProgress(ActivityStepPhase.RECEIPT_RECORDED, **{
                "physical_action_ref": result.action_id,
                "action_receipt_ref": result.execution_id,
            })
        )
        context.record_progress(
            StepProgress(ActivityStepPhase.AFTER_OBSERVATION_CAPTURED, **{
                "physical_action_ref": result.action_id,
                "action_receipt_ref": result.execution_id,
                "after_observation_ref": result.after_evidence_id,
            })
        )
        context.record_progress(
            StepProgress(ActivityStepPhase.VERIFICATION_RECORDED, **progress)
        )
        return StepExecutionResult(
            StepOutcome.ACT,
            decision={"kernel_status": result.status},
            verified=result.status == "confirmed_success",
            facts=facts,
            **progress,
        )

    def _selected_authority(self, goal_run_id: str):
        matches = [
            item
            for item in self.agent_runtime_store.selected_bindings_for_recovery()
            if item.goal_run_id == goal_run_id
        ]
        if len(matches) != 1:
            raise RuntimeError("ActivitySlice requires exactly one selected GoalRun binding")
        binding = matches[0]
        session = self.agent_runtime_store.get_session(binding.session_id)
        decision = self.agent_runtime_store.attention_decision(
            session.current_attention_decision_id or ""
        )
        if (
            decision is None
            or decision.selected_goal_id != binding.goal_node_id
            or decision.id != session.current_attention_decision_id
        ):
            raise RuntimeError("ActivitySlice selection does not match current AttentionDecision")
        return binding, session, decision

    def _slice_by_key(self, idempotency_key: str) -> ActivitySlice | None:
        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute(
                "SELECT slice_id FROM activity_slices WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return self.store.get_slice(str(row[0])) if row is not None else None

    def _project(self, activity_slice: ActivitySlice) -> ActivitySliceDispatchResult:
        steps = self.store.list_steps(activity_slice.id)
        facts = [dict(item.result_facts) for item in steps]
        kernel_statuses = {str(item.get("kernel_status")) for item in facts}
        action_steps = [item for item in steps if item.physical_action_ref]
        if "uncertain" in kernel_statuses:
            status = "uncertain"
        elif activity_slice.yield_reason is SliceYieldReason.VERIFICATION_FAILED:
            status = "confirmed_failure"
        elif activity_slice.status is ActivitySliceStatus.RUNNING:
            status = "incomplete"
        elif action_steps and all(item.verified is True for item in action_steps):
            status = "confirmed_success"
        else:
            # A wait/yield/restart without one persisted, verified physical
            # action is not progress and must never make the outer runtime
            # emit a false confirmed-success outcome.
            status = "incomplete"
        last_kernel_evidence = next(
            (
                str(item["kernel_evidence"])
                for item in reversed(facts)
                if item.get("kernel_evidence")
            ),
            None,
        )
        return ActivitySliceDispatchResult(
            receipt_id=activity_slice.id,
            slice_id=activity_slice.id,
            status=status,
            evidence=(
                last_kernel_evidence
                if last_kernel_evidence is not None
                else f"ActivitySlice {activity_slice.status.value}"
            ),
            accepted=bool(action_steps),
            physical_action_sent=bool(action_steps),
            before_evidence_id=(
                str(facts[0].get("kernel_before_evidence_id"))
                if facts and facts[0].get("kernel_before_evidence_id")
                else None
            ),
            after_evidence_id=(
                str(facts[-1].get("kernel_after_evidence_id"))
                if facts and facts[-1].get("kernel_after_evidence_id")
                else None
            ),
            step_ids=tuple(item.id for item in steps),
            kernel_task_ids=tuple(
                str(item["kernel_task_id"])
                for item in facts
                if item.get("kernel_task_id")
            ),
        )


__all__ = ["NormalActivitySliceRunner"]
