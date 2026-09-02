"""R1 AgentSession application service.

The key ordering is deliberate: Session facts and the outbox commit first;
GoalRuntime is then called with the persisted derived key; binding and outbox
settlement commit last.  Repeating the middle call is safe because the
GoalRuntime already has its own idempotency fence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .agenda import AgendaContext, AgendaGoalSnapshot, EventUrgency
from .domain import (
    AgentSession,
    ContinuationCheckpointKind,
    ContinuationDraft,
    ContinuationYieldReason,
    DirectiveKind,
    EventHandlingStatus,
    GoalCriterionStatus,
    GoalEdgeKind,
    GoalNodeStatus,
    PreemptionRequestStatus,
    SessionControlMode,
    SessionEventType,
    SessionControlAction,
    SessionControlUnsupported,
    SessionStateConflict,
    TaskControlAction,
    TaskControlConflict,
    RunnerDispatchCommit,
    RunnerDispatchRequest,
    TaskReason,
    TaskRevisionKind,
    TaskStatus,
    WakeConditionDraft,
    WakeConditionKind,
    domain_dict,
)
from .planner import DeterministicSessionPlanner, SessionPlanner
from .scheduler import AttentionScheduler
from .coverage import validate_complete_coverage
from .event_router import SessionEventRouter, classify_event_routing
from .projection import session_projection, task_event_projection, task_projection
from .store import SQLiteAgentRuntimeStore


class CanonicalTaskService:
    """Narrow application port for `/api/execution/v2` and host projections.

    It intentionally has no GoalRuntime, device, scheduler, or HTTP dependency.
    Package A can inject it into its adapter while Package C later drives the
    safe-boundary and wake hooks through the same explicit store methods.
    """

    def __init__(
        self, store: SQLiteAgentRuntimeStore, *, principal_id: str = "local-default",
        controller_id: str = "local-installation",
    ) -> None:
        self.store = store
        self.principal_id = principal_id
        self.controller_id = controller_id
        if not self.principal_id.strip() or not self.controller_id.strip():
            raise ValueError("principal_id and controller_id are required")
        self.store.initialize()

    def create_task(
        self, instruction: str, idempotency_key: str, *, origin: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        session, _created = self.store.create_unplanned_session(
            instruction=instruction, client_request_id=idempotency_key,
            owner_principal_id=self.principal_id, controller_id=self.controller_id, origin=origin,
        )
        return self.inspect_task(session.id)

    def inspect_task(self, task_id: str) -> dict[str, Any]:
        return task_projection(self._owned_task(task_id), subtasks=self.store.task_subtasks(task_id))

    def list_tasks(self, *, limit: int = 100, include_archived: bool = False) -> list[dict[str, Any]]:
        return [
            task_projection(task, subtasks=self.store.task_subtasks(task.id))
            for task in self.store.list_tasks_for_owner(
                self.principal_id, self.controller_id, limit=limit, include_archived=include_archived
            )
        ]

    def events(self, task_id: str, *, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self._owned_task(task_id)
        return [task_event_projection(event) for event in self.store.events(task_id, after=after, limit=limit)
                if event.event_type.value.startswith("task.") or event.event_type.value == "integrity.blocked"]

    def revise_task(
        self, task_id: str, *, base_revision: int, kind: str, instruction: str,
        patch: Mapping[str, Any], idempotency_key: str, requested_by: Mapping[str, Any],
        effective_boundary: str = "after_current_action",
    ) -> dict[str, Any]:
        self._owned_task(task_id)
        revision = self.store.record_task_revision(
            task_id, base_revision=base_revision, kind=TaskRevisionKind(kind), instruction=instruction,
            patch=patch, idempotency_key=idempotency_key, requested_by=requested_by,
            effective_boundary=effective_boundary,
        )
        return {"revision": domain_dict(revision), "task": self.inspect_task(task_id)}

    def apply_revision_boundary(self, task_id: str) -> dict[str, Any]:
        self._owned_task(task_id)
        return {
            "applied": [domain_dict(item) for item in self.store.apply_task_revisions_at_action_boundary(task_id)],
            "task": self.inspect_task(task_id),
        }

    def commit_runner_dispatch(
        self, task_id: str, *, request: RunnerDispatchRequest,
    ) -> RunnerDispatchCommit:
        """Commit the sole canonical action boundary for a generic runner.

        This intentionally returns an internal durable fact, not a renderer
        projection.  The caller still has to acquire a generic command claim
        before it may invoke a physical dispatcher.
        """

        self._owned_task(task_id)
        return self.store.commit_runner_dispatch(
            task_id,
            owner_principal_id=self.principal_id,
            controller_id=self.controller_id,
            request=request,
        )

    def settle_runner_effect(self, task_id: str, *, command_id: str, outcome: str) -> None:
        self._owned_task(task_id)
        self.store.settle_runner_effect(
            task_id, owner_principal_id=self.principal_id,
            controller_id=self.controller_id, command_id=command_id, outcome=outcome,
        )

    def require_active_runner_dispatch(
        self, task_id: str, *, dispatch_id: str, command_id: str,
    ) -> RunnerDispatchCommit:
        """Return only the exact canonical dispatch with a still-ACTIVE effect."""

        self._owned_task(task_id)
        return self.store.require_active_runner_dispatch(
            task_id,
            owner_principal_id=self.principal_id,
            controller_id=self.controller_id,
            dispatch_id=dispatch_id,
            command_id=command_id,
        )

    def control_task(
        self, task_id: str, *, action: str, idempotency_key: str, expected_revision: int,
        requested_by: Mapping[str, Any], priority: int | None = None,
    ) -> dict[str, Any]:
        self._owned_task(task_id)
        control = self.store.control_task(
            task_id, action=TaskControlAction(action), idempotency_key=idempotency_key,
            expected_revision=expected_revision, requested_by=requested_by, priority=priority,
        )
        return {"control": domain_dict(control), "task": self.inspect_task(task_id)}

    def ordinary_failure(
        self, task_id: str, *, reason_code: str, summary: str, idempotency_key: str,
        replan: bool = False, next_wake_at: str | None = None,
        expected_current_status: TaskStatus | str | None = None,
    ) -> dict[str, Any]:
        self._owned_task(task_id)
        try:
            expected = (
                TaskStatus(expected_current_status)
                if expected_current_status is not None
                else None
            )
        except ValueError as error:
            raise TaskControlConflict(
                f"unsupported expected current task status: {expected_current_status}"
            ) from error
        self.store.record_ordinary_failure(
            task_id, reason_code=reason_code, summary=summary, idempotency_key=idempotency_key,
            replan=replan, next_wake_at=next_wake_at,
            expected_current_status=expected,
        )
        return self.inspect_task(task_id)

    def transition_task(
        self, task_id: str, *, status: str, reason_code: str, summary: str,
        recoverable: bool, idempotency_key: str, next_wake_at: str | None = None,
        expected_current_status: TaskStatus | str | None = None,
    ) -> dict[str, Any]:
        self._owned_task(task_id)
        try:
            expected = (
                TaskStatus(expected_current_status)
                if expected_current_status is not None
                else None
            )
        except ValueError as error:
            raise TaskControlConflict(
                f"unsupported expected current task status: {expected_current_status}"
            ) from error
        self.store.transition_task(
            task_id, status=TaskStatus(status), reason=TaskReason(reason_code, summary, recoverable),
            idempotency_key=idempotency_key, next_wake_at=next_wake_at,
            expected_current_status=expected,
        )
        return self.inspect_task(task_id)

    def fence_integrity(self, task_id: str, *, reason_code: str, summary: str, idempotency_key: str) -> dict[str, Any]:
        self._owned_task(task_id)
        self.store.fence_task_integrity(task_id, reason_code=reason_code, summary=summary, idempotency_key=idempotency_key)
        return self.inspect_task(task_id)

    def upsert_subtask(
        self, task_id: str, *, kind: str, object_ref: str | None, conversation_ref: str | None,
        status: str, priority: int = 50, current_stage: str | None = None,
    ) -> dict[str, Any]:
        self._owned_task(task_id)
        subtask = self.store.upsert_task_subtask(
            task_id, kind=kind, object_ref=object_ref, conversation_ref=conversation_ref,
            status=status, priority=priority, current_stage=current_stage,
        )
        return {"subtask": domain_dict(subtask), "task": self.inspect_task(task_id)}

    def project_runner_result(
        self,
        task_id: str,
        *,
        expected_revision: int,
        expected_current_status: TaskStatus | str,
        kind: str,
        object_ref: str | None,
        conversation_ref: str | None,
        subtask_status: str,
        current_stage: str,
        target_status: TaskStatus | str,
        reason_code: str,
        summary: str,
        recoverable: bool,
        idempotency_key: str,
        next_wake_at: str | None = None,
        ordinary_failure: bool = False,
    ) -> dict[str, Any]:
        """CAS a runner's subtask and Task lifecycle projection atomically."""

        subtask, task = self.store.project_task_subtask_transition(
            task_id,
            owner_principal_id=self.principal_id,
            controller_id=self.controller_id,
            expected_revision=expected_revision,
            expected_current_status=expected_current_status,
            kind=kind,
            object_ref=object_ref,
            conversation_ref=conversation_ref,
            subtask_status=subtask_status,
            current_stage=current_stage,
            target_status=target_status,
            reason=TaskReason(reason_code, summary, recoverable),
            idempotency_key=idempotency_key,
            next_wake_at=next_wake_at,
            ordinary_failure=ordinary_failure,
        )
        return {
            "subtask": domain_dict(subtask),
            "task": task_projection(task, subtasks=self.store.task_subtasks(task_id)),
        }

    def _owned_task(self, task_id: str) -> Any:
        return self.store.get_task_for_owner(
            task_id, owner_principal_id=self.principal_id, controller_id=self.controller_id,
        )


class AgentSessionService:
    """Own R2 graph commit, activation replay, and Session projections."""

    def __init__(
        self,
        store: SQLiteAgentRuntimeStore,
        goal_service: Any,
        *,
        planner: SessionPlanner | None = None,
        attention_scheduler: Any | None = None,
        crash_hook: Callable[[str, dict[str, Any]], None] | None = None,
        recover_on_start: bool = True,
        device_body_store: Any | None = None,
        context_recovery: Callable[[str], str] | None = None,
        fact_context_provider: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.store = store
        self.goal_service = goal_service
        self.planner = planner or DeterministicSessionPlanner()
        self.attention_scheduler = attention_scheduler
        self.crash_hook = crash_hook
        self.device_body_store = device_body_store
        self.context_recovery = context_recovery
        self.fact_context_provider = fact_context_provider
        self.store.initialize()
        if recover_on_start:
            self.recover_planning()
            self.recover_pending()
            self.recover_inbox_events()
            self.recover_attention()
            self.recover_attention_dispatches()
            self.recover_stopping()

    def create(self, instruction: str, client_request_id: str) -> dict[str, Any]:
        session, created = self.store.create_unplanned_session(
            instruction=instruction, client_request_id=client_request_id
        )
        if created or self._needs_planning(session.id):
            self._plan_and_commit(session.id)
        self.recover_pending(session_id=session.id)
        if self.attention_scheduler is not None:
            latest = self.store.directives(session.id)[-1]
            self.reconcile_attention(
                session.id, trigger_key=f"directive:{latest.id}"
            )
        return self.inspect(session.id)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return [self._projection(session) for session in self.store.list_sessions(limit)]

    def inspect(self, session_id: str) -> dict[str, Any]:
        return self._projection(self.store.get_session(session_id))

    def send_message(
        self, session_id: str, content: str, client_request_id: str,
        directive_kind: str = DirectiveKind.ADD.value,
    ) -> dict[str, Any]:
        try:
            kind = DirectiveKind(directive_kind)
        except ValueError as error:
            raise SessionStateConflict(f"不支持的 Session 指令类型 {directive_kind}。") from error
        if kind in {DirectiveKind.ORIGINAL, DirectiveKind.STOP}:
            raise SessionStateConflict(f"消息端点不能写入 {kind.value} 指令。")
        recorder = getattr(self.store, "record_directive", None)
        if callable(recorder):
            directive, created = recorder(
                session_id, content=content, client_request_id=client_request_id, kind=kind
            )
        elif kind is DirectiveKind.ADD:
            directive, created = self.store.record_message(
                session_id, content=content, client_request_id=client_request_id
            )
        else:
            raise SessionStateConflict("当前 runtime 尚未支持该指令类型。")
        if created or self._needs_planning(session_id):
            self._plan_and_commit(session_id)
        # An idempotent HTTP retry also closes a graph-committed/outbox-pending
        # interruption without waiting for a process restart.
        self.recover_pending(session_id=session_id)
        if self.attention_scheduler is not None:
            self.reconcile_attention(
                session_id, trigger_key=f"directive:{directive.id}"
            )
        return self.inspect(session_id)

    def control(self, session_id: str, action: str, client_request_id: str) -> dict[str, Any]:
        try:
            parsed_action = SessionControlAction(action)
        except ValueError as error:
            raise SessionControlUnsupported(f"当前 Session 不支持控制动作 {action}。") from error

        newly_recorded = self.store.record_control(
            session_id, action=parsed_action.value, client_request_id=client_request_id
        )
        if parsed_action is SessionControlAction.STOP and newly_recorded:
            self.store.record_stop_directive(session_id, client_request_id=client_request_id)

        # A repeated HTTP delivery returns the existing Session projection.  It
        # must not issue a second owner command.  A prior stop may however
        # have persisted before its owner command met a terminal GoalRun; in
        # that case this exact retry is also the recovery trigger.
        if not newly_recorded:
            if parsed_action is SessionControlAction.STOP:
                self.recover_stopping(session_id=session_id)
            return self.inspect(session_id)

        # A stop can arrive while the R1 GoalRun is still an outbox intent.
        # Recover it first, then send the compatible owner stop; this prevents
        # an unbound owner from being created after the Session was settled.
        self.recover_pending(session_id=session_id)
        bindings = self.store.bindings(session_id)
        if not bindings:
            if (
                parsed_action is SessionControlAction.STOP
                and not self._has_pending_activation(session_id)
            ):
                self.store.settle_control(
                    session_id, action="stop", client_request_id=client_request_id
                )
                return self.inspect(session_id)
            raise SessionStateConflict("Session 当前没有可控制的 GoalRun binding。")
        if (
            self.attention_scheduler is not None
            and parsed_action is not SessionControlAction.STOP
        ):
            active_goal_id = self.store.get_session(session_id).active_goal_id
            bindings = [
                binding
                for binding in bindings
                if binding.goal_node_id == active_goal_id
            ]
        for binding in bindings:
            if (
                parsed_action is SessionControlAction.STOP
                and self._goal_run_is_terminal(binding.goal_run_id)
            ):
                continue
            self.goal_service.control(
                binding.goal_run_id, parsed_action.value,
                f"session:{session_id}:goal:{binding.goal_node_id}:control:{parsed_action.value}:{client_request_id}",
            )
        self.store.settle_control(
            session_id, action=parsed_action.value, client_request_id=client_request_id
        )
        if (
            self.attention_scheduler is not None
            and parsed_action is SessionControlAction.RESUME
        ):
            self.reconcile_attention(
                session_id,
                trigger_key=f"control:{client_request_id}:resume",
            )
        return self.inspect(session_id)

    def recover_stopping(self, *, session_id: str | None = None) -> int:
        """Finish durable stop requests after a launcher interruption."""

        settled = 0
        for session, client_request_id in self.store.stopping_sessions():
            if session_id is not None and session.id != session_id:
                continue
            bindings = self.store.bindings(session.id)
            if not bindings:
                # A pending or failed activation can still materialize an
                # owner, so retain the stop fence until it is resolved.  A
                # planner-only Session has no such owner path and can settle.
                if not self._has_pending_activation(session.id):
                    self.store.settle_control(
                        session.id, action="stop", client_request_id=client_request_id
                    )
                    settled += 1
                continue
            try:
                for binding in bindings:
                    if self._goal_run_is_terminal(binding.goal_run_id):
                        continue
                    self.goal_service.control(
                        binding.goal_run_id, "stop",
                        f"session:{session.id}:goal:{binding.goal_node_id}:control:stop:{client_request_id}",
                    )
            except Exception:
                continue
            self.store.settle_control(
                session.id, action="stop", client_request_id=client_request_id
            )
            settled += 1
        return settled

    def _has_pending_activation(self, session_id: str) -> bool:
        return any(
            intent.session_id == session_id
            for intent in self.store.pending_outbox()
        )

    def _goal_run_is_terminal(self, goal_run_id: str) -> bool:
        """Return whether a bound GoalRun has already reached its final state.

        A Session stop owns the Session boundary, not a second terminal
        transition for every legacy GoalRun.  Reading the owner first keeps a
        recovered stop idempotent when a prior lifecycle has already cancelled
        or completed that GoalRun.  Inspection failures deliberately propagate
        so a nonterminal or unavailable owner cannot be silently treated as
        stopped.
        """

        goal_run = self.goal_service.inspect(goal_run_id)
        terminal_at = _goal_run_value(goal_run, "terminal_at")
        if terminal_at is not None:
            return True
        return _goal_run_value(goal_run, "execution_status") in {
            "CANCELLED",
            "COMPLETED",
            "FAILED",
            "UNCERTAIN",
        }

    def recover_planning(self) -> int:
        """Rebuild graph revisions left pending after a planner/process failure.

        User directives are durable before model planning.  Comparing the
        latest committed graph's authority revision with the Session ledger
        gives startup a deterministic recovery signal without inventing a
        Goal or replaying a completed revision.
        """

        recovered = 0
        for session in self.store.sessions_for_recovery():
            if session.terminal or not self._needs_planning(session.id):
                continue
            try:
                self._plan_and_commit(session.id)
            except Exception:
                continue
            recovered += 1
        return recovered

    def events(self, session_id: str, *, after: int, limit: int) -> list[Any]:
        return self.store.events(session_id, after=after, limit=limit)

    def recover_inbox_events(self, *, dispatch: bool = True) -> int:
        """Finish persisted inbox events without replaying duplicate effects."""

        if self.attention_scheduler is None:
            return 0
        handled = 0
        pending_events = list(self.store.pending_inbox_events(limit=None))
        seen = {event.id for event in pending_events}
        # GoalStateChangedEvent is first routed against existing wake
        # conditions, so its intake row can be classified IGNORED before the
        # service creates the new wait.  Include that exact crash window in
        # launcher recovery; unrelated ignored notifications stay inert.
        for session in self.store.sessions_for_recovery():
            for event in self.store.all_events(session.id):
                if (
                    event.id not in seen
                    and event.source_namespace is not None
                    and event.decision_id is None
                    and event.handling_status is EventHandlingStatus.IGNORED
                    and event.event_type
                    in {
                        SessionEventType.GOAL_STATE_CHANGED,
                        SessionEventType.DEVICE_BUSY,
                    }
                ):
                    pending_events.append(event)
                    seen.add(event.id)
        pending_events.sort(key=lambda item: item.cursor)
        for pending in pending_events:
            try:
                self.store.route_inbox_event(pending.id)
                self.handle_inbox_event(pending.id, dispatch=dispatch)
            except Exception:
                continue
            handled += 1
        return handled

    def handle_inbox_event(self, event_id: str, *, dispatch: bool = True) -> Any:
        """Translate one durable, classified inbox event into agenda facts."""

        event = self._event_by_id(event_id)
        if event is None:
            raise KeyError(f"unknown Session inbox event: {event_id}")
        if event.handling_status in {
            EventHandlingStatus.HANDLED,
            EventHandlingStatus.IGNORED,
        }:
            return event
        payload = dict(event.data)
        if event.event_type in {
            SessionEventType.HUMAN_TOUCH_STARTED,
            SessionEventType.HUMAN_TOUCH_ENDED,
            SessionEventType.HUMAN_IDLE,
        }:
            return self._handle_human_activity_event(event, dispatch=dispatch)
        if event.event_type is SessionEventType.GOAL_STATE_CHANGED:
            goal_id = str(payload.get("goal_id") or "").strip()
            node = next(
                (
                    item
                    for item in self.store.goal_nodes(event.session_id)
                    if item.id == goal_id
                ),
                None,
            )
            if node is None:
                raise SessionStateConflict(
                    "GoalStateChangedEvent 的 Goal 不属于该 Session。"
                )
            supplied_goal_run = str(payload.get("goal_run_id") or "").strip()
            if supplied_goal_run:
                binding = next(
                    (
                        item
                        for item in self.store.bindings(event.session_id)
                        if item.goal_node_id == goal_id
                    ),
                    None,
                )
                if binding is None or binding.goal_run_id != supplied_goal_run:
                    raise SessionStateConflict(
                        "GoalStateChangedEvent 的 GoalRun binding 不匹配。"
                    )
            status_text = str(payload.get("status") or "").strip().upper()
            try:
                target_status = GoalNodeStatus(status_text)
            except ValueError as error:
                raise SessionStateConflict(
                    f"GoalStateChangedEvent 不支持状态 {status_text}。"
                ) from error
            if node.terminal:
                raise SessionStateConflict("终态 Goal 不能被事件重新打开。")
            graph = self.store.graph_revision(event.session_id)
            if graph is None:
                raise SessionStateConflict("Session 尚未形成 GoalGraph。")
            if target_status in {
                GoalNodeStatus.WAITING_EVENT,
                GoalNodeStatus.WAITING_TIME,
                GoalNodeStatus.WAITING_USER_FACT,
                GoalNodeStatus.WAITING_DEVICE,
                GoalNodeStatus.WAITING_ACCOUNT,
                GoalNodeStatus.WAITING_IDENTITY,
            }:
                waiting = payload.get("waiting")
                if not isinstance(waiting, dict):
                    raise SessionStateConflict("等待状态必须包含 waiting 合同。")
                kind = {
                    GoalNodeStatus.WAITING_EVENT: WakeConditionKind.EVENT,
                    GoalNodeStatus.WAITING_TIME: WakeConditionKind.TIME,
                    GoalNodeStatus.WAITING_USER_FACT: WakeConditionKind.USER_FACT,
                    GoalNodeStatus.WAITING_DEVICE: WakeConditionKind.DEVICE,
                    GoalNodeStatus.WAITING_ACCOUNT: WakeConditionKind.ACCOUNT,
                    GoalNodeStatus.WAITING_IDENTITY: WakeConditionKind.IDENTITY,
                }[target_status]
                due_at = waiting.get("due_at")
                matcher = waiting.get("matcher")
                if kind is WakeConditionKind.TIME and not due_at:
                    raise SessionStateConflict("WAITING_TIME 必须给出 due_at。")
                if kind is WakeConditionKind.EVENT and not isinstance(matcher, dict):
                    raise SessionStateConflict("WAITING_EVENT 必须给出 matcher。")
                continuation_draft = ContinuationDraft(
                        authority_revision=self.store.get_session(
                            event.session_id
                        ).authority_revision,
                        graph_revision=graph.revision,
                        checkpoint_kind=ContinuationCheckpointKind.WAIT_ENTRY,
                        yield_reason=(
                            ContinuationYieldReason.DEVICE_BUSY
                            if kind is WakeConditionKind.DEVICE
                            else ContinuationYieldReason.WAITING
                        ),
                        idempotency_key=f"event:{event.id}:wait:{goal_id}",
                        checkpoint_ref=f"event:{event.id}:wait-entry",
                        pending_intent=(
                            payload.get("pending_intent")
                            if isinstance(payload.get("pending_intent"), dict)
                            else None
                        ),
                        # ``WAITING_CAPABILITY`` is a waiting reason, not a
                        # second GoalNode status.  R4 keeps the established
                        # device wake machinery and records the precise reason
                        # on the continuation for API/Console projection.
                        waiting_kind=str(
                            waiting.get("waiting_kind") or kind.value
                        ),
                        waiting_ref=event.id,
                        resume_preconditions={"wake_condition_required": True},
                        next_eligible_at=str(due_at) if due_at else None,
                    )
                wake_draft = WakeConditionDraft(
                    kind=kind,
                    matcher=matcher if isinstance(matcher, dict) else None,
                    due_at=str(due_at) if due_at else None,
                    created_by_event_id=event.id,
                )
                self.store.enter_goal_waiting(
                    event.session_id,
                    goal_id,
                    continuation_draft,
                    wake_draft,
                )
            elif target_status is GoalNodeStatus.READY:
                ready = getattr(self.store, "set_goal_ready", None)
                if not callable(ready):
                    raise SessionStateConflict(
                        "AgentRuntime store 尚未提供 READY 等待清理事务。"
                    )
                ready(event.session_id, goal_id)
            else:
                raise SessionStateConflict(
                    "GoalStateChangedEvent 只接收 READY 或 WAITING_*。"
                )
        elif (
            event.event_type is SessionEventType.NOTIFICATION_POSTED
            and not event.affected_goal_ids
        ):
            # Exact router proved this notification cannot wake any Goal.
            return event
        elif event.event_type in _EXTERNAL_ATTENTION_EVENT_TYPES:
            # DeviceBusyEvent is emitted by the atomic dispatch-yield
            # transaction itself.  It must replace the interrupted selection
            # with a persisted full-agenda decision even though it does not
            # satisfy an existing external WakeCondition.
            if (
                event.event_type is not SessionEventType.DEVICE_BUSY
                and not event.affected_goal_ids
            ):
                return self.store.mark_event_handled(event.id, decision_id=None)
            session = self.store.get_session(event.session_id)
            if session.control_mode is not SessionControlMode.AGENT_ACTIVE:
                # Wake facts stay durable/READY.  USER_ACTIVE and TAKEOVER
                # suppress actions; a later explicit/idle recovery decision
                # evaluates the complete agenda.
                return self.store.mark_event_handled(event.id, decision_id=None)
            if session.active_slice_id is not None:
                self.store.create_preemption_request(
                    event.id,
                    reason=_event_preemption_reason(event),
                )
                return self._event_by_id(event.id)
            request = self.store.preemption_request_for_event(event.id)
            if (
                request is not None
                and request.status is PreemptionRequestStatus.PENDING_CHECKPOINT
            ):
                return event
            if (
                request is not None
                and request.status is not PreemptionRequestStatus.CHECKPOINTED
            ):
                return event
        decision = self.reconcile_attention(
            event.session_id,
            trigger_key=f"event:{event.id}",
            trigger_event_id=event.id,
            dispatch=dispatch,
        )
        request = self.store.preemption_request_for_event(event.id)
        if request is not None:
            self.store.settle_preemption_request(
                event.id, attention_decision_id=decision.id
            )
        return decision

    def _handle_human_activity_event(self, event: Any, *, dispatch: bool) -> Any:
        """Apply the R7 USER_ACTIVE state machine after durable intake."""

        session = self.store.get_session(event.session_id)
        if event.event_type is SessionEventType.HUMAN_TOUCH_ENDED:
            # Touch end starts the Companion's idle grace; only HumanIdleEvent
            # may recover agent authority.
            return self.store.mark_event_handled(event.id, decision_id=None)
        if event.event_type is SessionEventType.HUMAN_TOUCH_STARTED:
            if not _trusted_human_activity(event):
                return self.store.mark_event_handled(event.id, decision_id=None)
            if session.control_mode is SessionControlMode.TAKEOVER:
                return self.store.mark_event_handled(event.id, decision_id=None)
            if session.active_slice_id is not None:
                self.store.create_preemption_request(
                    event.id, reason="human touch requires verified checkpoint"
                )
                return self._event_by_id(event.id)
            if session.control_mode is not SessionControlMode.USER_ACTIVE:
                self.store.transition_control_mode(
                    session.id,
                    event_id=event.id,
                    to_mode=SessionControlMode.USER_ACTIVE,
                    reason="human touch started",
                    idempotency_key=f"event:{event.id}:control:user-active",
                    expected_from=frozenset(
                        {
                            SessionControlMode.AGENT_ACTIVE,
                            SessionControlMode.RECOVERING_CONTEXT,
                        }
                    ),
                )
                session = self.store.get_session(session.id)
            return self.store.mark_event_handled(event.id, decision_id=None)

        # HumanIdleEvent never releases an explicit TAKEOVER.
        if session.control_mode is SessionControlMode.TAKEOVER:
            return self.store.mark_event_handled(event.id, decision_id=None)
        if session.control_mode is SessionControlMode.USER_ACTIVE:
            self.store.transition_control_mode(
                session.id,
                event_id=event.id,
                to_mode=SessionControlMode.RECOVERING_CONTEXT,
                reason="human idle grace elapsed",
                idempotency_key=f"event:{event.id}:control:recovering",
                expected_from=frozenset({SessionControlMode.USER_ACTIVE}),
            )
            session = self.store.get_session(session.id)
        if session.active_slice_id is not None:
            self.store.create_preemption_request(
                event.id, reason="human idle waits for verified checkpoint"
            )
            return self._event_by_id(event.id)
        if session.control_mode is not SessionControlMode.RECOVERING_CONTEXT:
            return self.store.mark_event_handled(event.id, decision_id=None)
        if self.context_recovery is None:
            raise SessionStateConflict(
                "RECOVERING_CONTEXT requires a fresh observation provider"
            )
        fresh_ref = str(self.context_recovery(session.id) or "").strip()
        if not fresh_ref:
            raise SessionStateConflict(
                "RECOVERING_CONTEXT provider returned no fresh observation"
            )
        self.store.transition_control_mode(
            session.id,
            event_id=event.id,
            to_mode=SessionControlMode.AGENT_ACTIVE,
            reason="fresh context recovered after human idle",
            idempotency_key=f"event:{event.id}:control:agent-active",
            expected_from=frozenset({SessionControlMode.RECOVERING_CONTEXT}),
            fresh_observation_ref=fresh_ref,
        )
        decision = self.reconcile_attention(
            session.id,
            trigger_key=f"event:{event.id}:human-idle-recovery",
            trigger_event_id=event.id,
            dispatch=dispatch,
        )
        request = self.store.preemption_request_for_event(event.id)
        if request is not None:
            self.store.settle_preemption_request(
                event.id, attention_decision_id=decision.id
            )
        return decision

    def recover_pending(self, *, session_id: str | None = None) -> int:
        """Replay every unfinished GoalRun-prepare intent once.

        Exceptions from GoalRuntime are retained in the outbox for a future
        launcher recovery.  A deliberate ``crash_hook`` is invoked *outside*
        that exception boundary so tests can model process death after the
        GoalRun was durably prepared but before the Session binding
        transaction.  In R3 composition this never activates an execution
        owner: only a persisted AttentionDecision dispatch may do that.

        ``attention_scheduler is None`` keeps the narrow R1/R2 injected-fake
        compatibility path.  Normal application composition supplies the R3
        scheduler and therefore always takes the prepare-only path.
        """

        settled = 0
        for pending in self.store.pending_outbox():
            if session_id is not None and pending.session_id != session_id:
                continue
            intent = self.store.mark_outbox_processing(pending.id)
            if intent.status.value == "DELIVERED":
                continue
            try:
                goal_run = (
                    self._prepare_only(intent)
                    if self.attention_scheduler is not None
                    else self._prepare_then_activate(intent)
                )
            except Exception as error:
                self.store.fail_outbox(intent.id, f"{type(error).__name__}: {error}")
                continue
            goal_run_id = _goal_run_id(goal_run)
            if self.crash_hook is not None:
                self.crash_hook(
                    (
                        "after_goal_run_prepared"
                        if self.attention_scheduler is not None
                        else "after_goal_run_activated"
                    ),
                    {"intent_id": intent.id, "session_id": intent.session_id, "goal_run_id": goal_run_id},
                )
            self.store.settle_goal_run(intent.id, goal_run_id=goal_run_id)
            settled += 1
        return settled

    def selected_goal_run_ids(self) -> set[str]:
        """Resolve durable Session selections to GoalRuntime owner ids.

        This is used before downstream runtime startup, so it deliberately
        reads only AgentSession/SessionGoalBinding facts and never inspects or
        starts a GoalRuntime owner.
        """

        recovery_bindings = getattr(
            self.store, "selected_bindings_for_recovery", None
        )
        if callable(recovery_bindings):
            return {
                binding.goal_run_id for binding in recovery_bindings()
            }

        # Compatibility shim for existing injected stores.  Production uses
        # the unbounded joined recovery query above.
        selected: set[str] = set()
        sessions_for_recovery = getattr(self.store, "sessions_for_recovery", None)
        sessions = (
            sessions_for_recovery()
            if callable(sessions_for_recovery)
            else self.store.list_sessions()
        )
        for session in sessions:
            goal_node_id = session.active_goal_id
            if (
                session.terminal
                or session.control_mode.value != "AGENT_ACTIVE"
                or not goal_node_id
            ):
                continue
            binding = next(
                (
                    item
                    for item in self.store.bindings(session.id)
                    if item.goal_node_id == goal_node_id
                ),
                None,
            )
            if binding is not None:
                selected.add(binding.goal_run_id)
        return selected

    def recover_attention(
        self, *, dispatch: bool = True, now: datetime | None = None
    ) -> int:
        """Create only missing/stale scheduler decisions during launcher recovery."""

        if self.attention_scheduler is None:
            return 0
        clock = now or datetime.now(UTC)
        timer_recoveries = self.poll_due_time_events(
            dispatch=dispatch, now=clock
        )
        recovered = 0
        for session in self.store.sessions_for_recovery():
            if session.terminal:
                continue
            graph = self.store.graph_revision(session.id)
            if graph is None or not self.store.bindings(session.id):
                continue
            latest = self.store.latest_attention_decision(session.id)
            selected_node = next(
                (
                    node
                    for node in self.store.goal_nodes(session.id)
                    if node.id == latest.selected_goal_id
                ),
                None,
            ) if latest is not None and latest.selected_goal_id is not None else None
            stable_selected = (
                latest is not None
                and latest.selected_goal_id is not None
                and latest.selected_goal_id == session.active_goal_id
                and selected_node is not None
                and selected_node.status is GoalNodeStatus.ACTIVE
                and not _goal_not_yet_eligible(selected_node, datetime.now(UTC))
            )
            stable_empty = (
                latest is not None
                and latest.selected_goal_id is None
                and session.active_goal_id is None
            )
            if (
                latest is not None
                and latest.authority_revision == session.authority_revision
                and latest.graph_revision == graph.revision
                and latest.agenda_revision == session.agenda_revision
                and (stable_selected or stable_empty)
            ):
                # Pending dispatch recovery owns the crash-after-decision
                # window.  A clean restart must not invent a new Decision.
                continue
            try:
                self.reconcile_attention(
                    session.id, recovery=True, dispatch=dispatch, now=clock
                )
            except Exception:
                continue
            recovered += 1
        return timer_recoveries + recovered

    def poll_due_time_events(
        self, *, dispatch: bool = True, now: datetime | None = None
    ) -> int:
        """Materialize and handle due TimerDueEvent facts without a restart.

        A TIME wake is only a query result until this boundary writes its
        stable EventInbox identity.  Routing then satisfies the exact wake,
        and ``handle_inbox_event`` owns the resulting AttentionDecision or
        verified-checkpoint preemption request.  Pending timer events are
        included on every poll so a crash after routing cannot strand them.
        """

        if self.attention_scheduler is None:
            return 0
        clock = (now or datetime.now(UTC)).astimezone(UTC)
        router = SessionEventRouter(self.store)
        pending: dict[str, Any] = {}
        occurred_at = clock.isoformat()
        for wake in self.store.due_time_wake_conditions(now=clock):
            result = router.ingest(
                wake.session_id,
                source_namespace="agent-runtime-timer",
                source_event_id=f"wake:{wake.id}:due:{wake.due_at}",
                event_type=SessionEventType.TIMER_DUE,
                occurred_at=occurred_at,
                payload={
                    "wake_condition_id": wake.id,
                    "due_at": wake.due_at,
                    "goal_id": wake.goal_id,
                    "urgency": "HIGH",
                },
            )
            pending[result.event.id] = result.event

        for event in self.store.pending_inbox_events(limit=None):
            if event.event_type is SessionEventType.TIMER_DUE:
                pending[event.id] = event

        handled = 0
        for event in sorted(pending.values(), key=lambda item: item.cursor):
            routed, wakes = self.store.route_inbox_event(event.id)
            classification = classify_event_routing(routed, wakes)
            self.store.record_event_routing(
                routed.id, classification.to_payload()
            )
            current = self._event_by_id(routed.id)
            if current is None or current.handling_status in {
                EventHandlingStatus.HANDLED,
                EventHandlingStatus.IGNORED,
            }:
                continue
            self.handle_inbox_event(current.id, dispatch=dispatch)
            handled += 1
        return handled

    def reconcile_attention(
        self,
        session_id: str,
        *,
        trigger_key: str | None = None,
        trigger_event_id: str | None = None,
        recovery: bool = False,
        dispatch: bool = True,
        now: datetime | None = None,
    ) -> Any:
        """Evaluate, atomically commit, and dispatch one attention decision."""

        scheduler = self.attention_scheduler
        if scheduler is None:
            return None
        if not isinstance(scheduler, AttentionScheduler) and not callable(
            getattr(scheduler, "request_attention", None)
        ):
            raise TypeError("attention_scheduler must expose request_attention")
        clock = now or datetime.now(UTC)
        last_conflict: Exception | None = None
        for _ in range(3):
            session = self.store.get_session(session_id)
            if session.active_slice_id is not None:
                raise SessionStateConflict(
                    "AttentionDecision must wait for the active ActivitySlice checkpoint"
                )
            if session.control_mode is not SessionControlMode.AGENT_ACTIVE:
                raise SessionStateConflict(
                    "Session control mode blocks AttentionDecision dispatch"
                )
            graph = self.store.graph_revision(session_id)
            if graph is None:
                raise SessionStateConflict("Session 尚未形成可调度的 GoalGraph。")
            effective_trigger_key = (
                (
                    f"recovery:{session.id}:{session.authority_revision}:"
                    f"{graph.revision}:{session.event_cursor}:{session.agenda_revision}"
                )
                if recovery
                else trigger_key
            )
            if not effective_trigger_key:
                raise ValueError("attention trigger_key must not be blank")
            existing = self.store.attention_decision_for_trigger(
                session_id, effective_trigger_key
            )
            if existing is not None:
                return existing
            nodes = self.store.goal_nodes(session_id)
            node_by_id = {node.id: node for node in nodes}
            current_edges = self.store.goal_edges(
                session_id, revision=graph.revision
            )
            hard_predecessors: dict[str, set[str]] = {}
            for edge in current_edges:
                if edge.edge_kind not in {
                    GoalEdgeKind.BLOCKS,
                    GoalEdgeKind.UNBLOCKS,
                }:
                    continue
                # R3 treats both directed labels as successful-prerequisite
                # evidence: ``A -> B`` keeps B blocked until A succeeds.  The
                # distinct labels remain persisted so later product authority
                # can refine their meaning without silently reversing history.
                hard_predecessors.setdefault(edge.to_goal_id, set()).add(
                    edge.from_goal_id
                )

            dependency_success: dict[str, bool] = {}
            for node in nodes:
                required = [
                    criterion
                    for criterion in self.store.criteria(
                        node.id, revision=graph.revision
                    )
                    if criterion.required
                ]
                dependency_success[node.id] = (
                    node.status is GoalNodeStatus.COMPLETED
                    or (
                        (
                            node.status is GoalNodeStatus.CANDIDATE_COMPLETE
                            or bool(node.verified_result_ref)
                        )
                        and all(
                            criterion.status is GoalCriterionStatus.VERIFIED
                            for criterion in required
                        )
                    )
                )

            dependencies_satisfied = {
                node.id: all(
                    predecessor_id in node_by_id
                    and dependency_success.get(predecessor_id, False)
                    for predecessor_id in hard_predecessors.get(node.id, ())
                )
                for node in nodes
            }
            bindings = {
                item.goal_node_id: item for item in self.store.bindings(session_id)
            }
            availability_probe = getattr(
                self.goal_service, "goal_available_for_activation", None
            )
            binding_ready: dict[str, bool] = {}
            for node in nodes:
                binding = bindings.get(node.id)
                if binding is None:
                    binding_ready[node.id] = False
                    continue
                if not callable(availability_probe):
                    binding_ready[node.id] = True
                    continue
                try:
                    binding_ready[node.id] = bool(
                        availability_probe(binding.goal_run_id)
                    )
                except Exception:
                    # Availability is a scheduling fence.  Unknown inspection
                    # must keep the historical owner out of the selected set.
                    binding_ready[node.id] = False
            directives = self.store.directives(session_id)
            latest_directive = directives[-1] if directives else None
            latest_targets = frozenset(
                node.id
                for node in nodes
                if latest_directive is not None
                and bool(trigger_key and trigger_key.startswith("directive:"))
                and node.source_directive_id == latest_directive.id
            )
            trigger_event = (
                self._event_by_id(trigger_event_id)
                if trigger_event_id is not None
                else None
            )
            event_goal_ids = set(
                trigger_event.affected_goal_ids if trigger_event is not None else ()
            )
            if (
                trigger_event is not None
                and trigger_event.event_type
                in {SessionEventType.GOAL_STATE_CHANGED, SessionEventType.DEVICE_BUSY}
            ):
                payload_goal = str(trigger_event.data.get("goal_id") or "").strip()
                if payload_goal:
                    event_goal_ids.add(payload_goal)
            urgency_text = (
                str(trigger_event.data.get("urgency") or "NONE").upper()
                if trigger_event is not None
                else "NONE"
            )
            try:
                event_urgency = EventUrgency[urgency_text]
            except KeyError:
                event_urgency = EventUrgency.NONE
            current = next(
                (node for node in nodes if node.id == session.active_goal_id),
                None,
            )
            context = AgendaContext(
                active_goal_id=session.active_goal_id,
                current_application_hint=(
                    current.application_hint if current is not None else None
                ),
                control_allows_scheduling=(
                    session.control_mode.value == "AGENT_ACTIVE"
                    and not session.terminal
                ),
                latest_directive_goal_ids=latest_targets,
                event_goal_ids=frozenset(event_goal_ids),
                event_urgency=event_urgency,
            )
            snapshots = tuple(
                AgendaGoalSnapshot.from_goal_node(
                    node,
                    binding_ready=binding_ready[node.id],
                    dependencies_satisfied=dependencies_satisfied[node.id],
                    # R3 scheduler boundaries are checkpoints until R6 adds
                    # real in-flight Kernel action facts.
                    switch_checkpoint_ready=True,
                )
                for node in nodes
            )
            if recovery:
                plan = scheduler.recover(
                    session_id=session.id,
                    original_instruction=session.original_instruction,
                    authority_revision=session.authority_revision,
                    graph_revision=graph.revision,
                    event_cursor=session.event_cursor,
                    agenda_revision=session.agenda_revision,
                    goals=snapshots,
                    context=context,
                    now=clock,
                )
            else:
                if not trigger_key:
                    raise ValueError("attention trigger_key must not be blank")
                plan = scheduler.request_attention(
                    session_id=session.id,
                    trigger_key=trigger_key,
                    original_instruction=session.original_instruction,
                    authority_revision=session.authority_revision,
                    goals=snapshots,
                    context=context,
                    now=clock,
                )
            previous = session.active_goal_id
            changing = previous is not None and previous != plan.selected_goal_id
            continuation = None
            checkpoint_ref = None
            if changing:
                latest_previous = self.store.latest_continuation(previous)
                preemption = (
                    self.store.preemption_request_for_event(trigger_event_id)
                    if trigger_event_id is not None
                    else None
                )
                if preemption is not None:
                    if (
                        preemption.status
                        is not PreemptionRequestStatus.CHECKPOINTED
                        or not preemption.checkpoint_ref
                    ):
                        raise SessionStateConflict(
                            "event preemption has not reached a verified checkpoint"
                        )
                    checkpoint_ref = preemption.checkpoint_ref
                else:
                    checkpoint_ref = (
                        latest_previous.checkpoint_ref
                        if latest_previous is not None
                        else f"attention:{plan.trigger_key}:scheduler-boundary"
                    )
                if current is not None and current.status is GoalNodeStatus.ACTIVE:
                    continuation = (
                        previous,
                        ContinuationDraft(
                            authority_revision=session.authority_revision,
                            graph_revision=graph.revision,
                            checkpoint_kind=ContinuationCheckpointKind.SCHEDULER_BOUNDARY,
                            yield_reason=ContinuationYieldReason.PREEMPTED,
                            idempotency_key=f"attention:{plan.trigger_key}:preempt:{previous}",
                            checkpoint_ref=checkpoint_ref,
                            resume_preconditions={"scheduler_checkpoint": True},
                        ),
                    )
            draft = replace(plan.to_decision_draft(
                authority_revision=session.authority_revision,
                graph_revision=graph.revision,
                event_cursor=session.event_cursor,
                trigger_event_id=trigger_event_id,
                preemption_checkpoint_ref=checkpoint_ref,
            ), agenda_revision=session.agenda_revision)
            try:
                decision, _created, _dispatch = self.store.commit_attention_decision(
                    session_id,
                    draft=draft,
                    candidates=plan.evaluation.all_goals,
                    previous_continuation=continuation,
                )
            except SessionStateConflict as error:
                last_conflict = error
                continue
            if changing and dispatch:
                self.pause_goal_at_checkpoint(
                    session_id,
                    previous,
                    f"attention:{decision.id}:pause:{previous}",
                )
            if dispatch:
                self.recover_attention_dispatches(session_id=session_id)
            return decision
        assert last_conflict is not None
        raise last_conflict

    def recover_attention_dispatches(
        self, *, session_id: str | None = None, _followup: bool = True
    ) -> int:
        """Replay scheduler dispatches through stable GoalService adapter keys."""

        if self.attention_scheduler is None:
            return 0
        delivered = 0
        needs_followup = False
        for pending in self.store.pending_attention_dispatches(session_id):
            device_busy_event = None
            dispatch = self.store.mark_attention_dispatch_processing(pending.id)
            if dispatch.status.value == "DELIVERED":
                continue
            current = self.store.get_session(dispatch.session_id).active_goal_id
            if current != dispatch.goal_id:
                self.store.fail_attention_dispatch(
                    dispatch.id,
                    "attention dispatch was superseded by a later decision",
                    retryable=False,
                )
                continue
            node = next(
                (
                    item
                    for item in self.store.goal_nodes(dispatch.session_id)
                    if item.id == dispatch.goal_id
                ),
                None,
            )
            if (
                node is None
                or node.status is not GoalNodeStatus.ACTIVE
                or _goal_not_yet_eligible(node, datetime.now(UTC))
            ):
                self.store.fail_attention_dispatch(
                    dispatch.id,
                    "attention dispatch is fenced by current Goal eligibility",
                    retryable=True,
                )
                if node is not None:
                    try:
                        self.reconcile_attention(
                            dispatch.session_id,
                            trigger_key=(
                                f"dispatch-fence:{dispatch.id}:{node.updated_at}"
                            ),
                            dispatch=False,
                        )
                    except Exception:
                        continue
                    self.store.fail_attention_dispatch(
                        dispatch.id,
                        "attention dispatch was reconciled by a fresh eligibility decision",
                        retryable=False,
                    )
                    needs_followup = True
                continue
            device_busy_event = None
            try:
                # Recovery cannot depend on volatile knowledge of the prior
                # active Goal.  Fence every sibling; unactivated GoalRuns make
                # this a side-effect-free no-op.
                for binding in self.store.bindings(dispatch.session_id):
                    if binding.goal_node_id == dispatch.goal_id:
                        continue
                    self.pause_goal_at_checkpoint(
                        dispatch.session_id,
                        binding.goal_node_id,
                        f"{dispatch.idempotency_key}:fence:{binding.goal_node_id}",
                    )
                result = self.activate_selected_goal(
                    dispatch.session_id,
                    dispatch.goal_id,
                    dispatch.idempotency_key,
                )
                if _retryable_device_busy(result):
                    device_busy_event = self._yield_device_busy(dispatch)
            except Exception as error:
                retryable = _is_retryable_dispatch_error(error)
                self.store.fail_attention_dispatch(
                    dispatch.id,
                    f"{type(error).__name__}: {error}",
                    retryable=retryable,
                )
                continue
            if device_busy_event is not None:
                handled_busy = False
                try:
                    self.store.route_inbox_event(device_busy_event.id)
                    self.handle_inbox_event(device_busy_event.id, dispatch=False)
                    handled_busy = True
                except Exception:
                    # The atomic RETRYABLE + wait + continuation + inbox fact
                    # is already sufficient for launcher recovery.  Never
                    # rewrite it to FAILED because post-commit handling was
                    # interrupted.
                    pass
                needs_followup = needs_followup or handled_busy
                continue
            self.store.settle_attention_dispatch(dispatch.id)
            delivered += 1
        if needs_followup and _followup:
            delivered += self.recover_attention_dispatches(
                session_id=session_id, _followup=False
            )
        return delivered

    def _yield_device_busy(self, dispatch: Any) -> Any:
        """Persist retryable device wait, continuation and a new attention trigger."""

        session = self.store.get_session(dispatch.session_id)
        graph = self.store.graph_revision(dispatch.session_id)
        if graph is None:
            raise SessionStateConflict("device-busy dispatch has no GoalGraph")
        occurred = datetime.fromisoformat(
            str(dispatch.created_at).replace("Z", "+00:00")
        )
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=UTC)
        occurred = occurred.astimezone(UTC)
        retry_at = (occurred + timedelta(seconds=60)).isoformat()
        continuation_draft = ContinuationDraft(
            authority_revision=session.authority_revision,
            graph_revision=graph.revision,
            checkpoint_kind=ContinuationCheckpointKind.WAIT_ENTRY,
            yield_reason=ContinuationYieldReason.DEVICE_BUSY,
            idempotency_key=f"{dispatch.idempotency_key}:device-busy",
            checkpoint_ref=f"{dispatch.idempotency_key}:device-busy-checkpoint",
            pending_intent={"attention_dispatch_id": dispatch.id},
            waiting_kind=WakeConditionKind.DEVICE.value,
            waiting_ref=dispatch.id,
            resume_preconditions={"device_available": True},
            next_eligible_at=retry_at,
        )
        wake_draft = WakeConditionDraft(
            kind=WakeConditionKind.DEVICE,
            matcher={},
            due_at=retry_at,
            created_by_decision_id=dispatch.decision_id,
        )
        _retryable, event, _continuation, _wake = (
            self.store.yield_attention_dispatch_device_busy(
                dispatch.id,
                continuation_draft,
                wake_draft,
                occurred_at=occurred.isoformat(),
                retry_at=retry_at,
            )
        )
        return event

    def activate_selected_goal(
        self, session_id: str, goal_node_id: str, idempotency_key: str
    ) -> Any:
        """Dispatch one persisted selection through GoalService's adapter."""

        binding = next(
            (
                item
                for item in self.store.bindings(session_id)
                if item.goal_node_id == goal_node_id
            ),
            None,
        )
        if binding is None:
            raise SessionStateConflict("selected Goal 尚未绑定已准备的 GoalRun。")
        activate = getattr(self.goal_service, "activate_selected_goal", None)
        if callable(activate):
            return activate(binding.goal_run_id, idempotency_key)
        # Compatibility for existing injected fakes.  Production GoalService
        # exposes the scheduler-specific adapter above.
        return self.goal_service.activate_goal(binding.goal_run_id)

    def pause_goal_at_checkpoint(
        self, session_id: str, goal_node_id: str, idempotency_key: str
    ) -> Any:
        """Fence one deselected owner without coupling to ApplicationRuntime."""

        binding = next(
            (
                item
                for item in self.store.bindings(session_id)
                if item.goal_node_id == goal_node_id
            ),
            None,
        )
        if binding is None:
            return None
        pause = getattr(self.goal_service, "pause_goal_at_checkpoint", None)
        if callable(pause):
            return pause(binding.goal_run_id, idempotency_key)
        return None

    def _plan_and_commit(self, session_id: str) -> None:
        """Plan and atomically persist a whole graph before any GoalRuntime call."""

        last_conflict: ValueError | None = None
        for _ in range(3):
            session = self.store.get_session(session_id)
            directives = self.store.directives(session_id)
            existing_nodes = self.store.goal_nodes(session_id)
            current_graph = self.store.graph_revision(session_id)
            planner_arguments: dict[str, Any] = {
                "session_id": session_id,
                "directives": directives,
                "authority_revision": session.authority_revision,
                "graph_revision": current_graph.revision if current_graph is not None else 0,
                "existing_nodes": existing_nodes,
            }
            if self.fact_context_provider is not None:
                planner_arguments["applicable_user_facts"] = (
                    self.fact_context_provider(session_id)
                )
            plan = self.planner.plan(**planner_arguments)
            validate_complete_coverage(
                _directive_clauses(directives), plan.coverage, plan.nodes
            )
            committed_nodes = tuple(
                replace(node, execution_goal=plan.goal_text(node.id))
                if node.operation == "CREATE" else node
                for node in plan.nodes
            )
            try:
                self.store.apply_graph_revision(
                    session_id, revision=plan.revision, nodes=committed_nodes,
                    edges=plan.edges, criteria=plan.criteria, coverage=plan.coverage,
                )
            except ValueError as error:
                if str(error) not in {
                    "graph authority_revision must equal persisted Session authority",
                    "graph base revision is stale",
                }:
                    raise
                last_conflict = error
                continue
            if self.crash_hook is not None:
                self.crash_hook("after_goal_graph_committed", {"session_id": session_id})
            return
        assert last_conflict is not None
        raise last_conflict

    def _needs_planning(self, session_id: str) -> bool:
        session = self.store.get_session(session_id)
        graph = self.store.graph_revision(session_id)
        return graph is None or graph.authority_revision < session.authority_revision

    def _prepare_then_activate(self, intent: Any) -> Any:
        """Use the R2 two-step owner boundary, with a narrow R1 test shim."""

        prepare = getattr(self.goal_service, "prepare_goal", None)
        activate = getattr(self.goal_service, "activate_goal", None)
        if not callable(prepare) or not callable(activate):
            # Existing injected R1 fakes are intentionally kept usable while
            # real GoalService exposes the strict prepare/activate boundary.
            return self.goal_service.create(intent.goal_text, intent.idempotency_key)
        prepared = prepare(intent.goal_text, intent.idempotency_key)
        prepared_id = _goal_run_id(prepared)
        if self.crash_hook is not None:
            self.crash_hook(
                "after_goal_run_prepared",
                {"intent_id": intent.id, "session_id": intent.session_id, "goal_run_id": prepared_id},
            )
        return activate(prepared_id)

    def _prepare_only(self, intent: Any) -> Any:
        """Persist a GoalRun without crossing the scheduler execution gate."""

        prepare = getattr(self.goal_service, "prepare_goal", None)
        if not callable(prepare):
            raise TypeError(
                "R3 GoalService adapter must expose prepare_goal before scheduling"
            )
        return prepare(intent.goal_text, intent.idempotency_key)

    def _event_by_id(self, event_id: str) -> Any | None:
        return self.store.event_by_id(event_id)

    def _projection(self, session: AgentSession) -> dict[str, Any]:
        bindings = self.store.bindings(session.id)
        goal_runs: dict[str, Any] = {}
        for binding in bindings:
            if (
                self.attention_scheduler is not None
                and binding.goal_node_id != session.active_goal_id
            ):
                continue
            try:
                goal_runs[binding.goal_node_id] = self.goal_service.inspect(binding.goal_run_id)
            except Exception:
                # GoalRun is a compatibility projection owned by a different
                # database.  Preserve the Session projection during a partial
                # runtime outage so the durable recovery information remains
                # inspectable.
                continue
        for goal_run in goal_runs.values():
            execution_status = (
                goal_run.get("execution_status")
                if isinstance(goal_run, dict)
                else getattr(goal_run, "execution_status", None)
            )
            if self.attention_scheduler is not None and execution_status not in {
                "COMPLETED",
                "PARTIAL",
                "FAILED",
                "UNCERTAIN",
                "CANCELLED",
                "STOPPED",
                "CANDIDATE_COMPLETE",
            }:
                # R3 scheduler owns READY/ACTIVE/WAITING.  GoalRuntime remains
                # a terminal/evidence compatibility projection and cannot
                # overwrite a persisted AttentionDecision with ACCEPTED.
                continue
            session = self.store.project_goal_run(session.id, goal_run)
        # Projecting the GoalRun can update both the one R1 GoalNode and the
        # one Session lifecycle, so read the authoritative SQLite rows only
        # after that projection commit.
        directives = self.store.directives(session.id)
        goal_nodes = self.store.goal_nodes(session.id)
        binding = (
            next(
                (
                    item
                    for item in bindings
                    if item.goal_node_id == session.active_goal_id
                ),
                None,
            )
            if self.attention_scheduler is not None
            else self.store.binding(session.id)
        )
        goal_run = goal_runs.get(binding.goal_node_id) if binding is not None else None
        payload = session_projection(
            session, directives=directives, goal_nodes=goal_nodes,
            binding=binding, goal_run=goal_run, bindings=bindings, goal_runs=goal_runs,
        )
        # Foreground is observed device state.  In particular it must not be
        # synthesized from the selected Goal's desired application hint: a
        # Goal can request an app switch while the body is still elsewhere.
        # The optional lookup preserves R1-R3 injected-store compatibility.
        device_binding = None
        device_capabilities: list[Any] = []
        latest_snapshot = None
        if session.device_binding_id is not None:
            load_binding = getattr(self.device_body_store, "load_binding", None)
            if callable(load_binding):
                device_binding = load_binding(session.device_binding_id)
            lookup_capabilities = getattr(
                self.device_body_store, "capabilities_at_revision", None
            )
            if (
                device_binding is not None
                and device_binding.capability_revision > 0
                and callable(lookup_capabilities)
            ):
                device_capabilities = list(
                    lookup_capabilities(
                        device_binding.id, device_binding.capability_revision
                    )
                )
            lookup_snapshot = getattr(self.device_body_store, "latest_snapshot", None)
            if callable(lookup_snapshot):
                latest_snapshot = lookup_snapshot(session.device_binding_id)
        payload["device_body_binding"] = (
            domain_dict(device_binding) if device_binding is not None else None
        )
        payload["device_capabilities"] = [
            domain_dict(item) for item in device_capabilities
        ]
        payload["latest_device_snapshot"] = (
            domain_dict(latest_snapshot) if latest_snapshot is not None else None
        )
        payload["current_application"] = (
            getattr(latest_snapshot, "foreground_package", None)
            if latest_snapshot is not None
            else None
        )
        graph = self.store.graph_revision(session.id)
        payload["goal_graph"] = domain_dict(graph) if graph is not None else None
        graph_revisions = [
            domain_dict(item) for item in self.store.graph_revisions(session.id)
        ]
        payload["goal_graph_revisions"] = graph_revisions
        # Console-facing aliases keep the compact R2 shape while the explicit
        # domain objects above remain available to API/debug consumers.
        payload["goal_graph_revision"] = graph.revision if graph is not None else None
        payload["graph_revisions"] = graph_revisions
        payload["goal_edges"] = [
            domain_dict(item) for item in self.store.goal_edges(session.id)
        ]
        payload["goal_coverage"] = [
            domain_dict(item) for item in self.store.coverage(session.id)
        ]
        attention = self.store.latest_attention_decision(session.id)
        candidates = (
            [
                domain_dict(item)
                for item in self.store.attention_candidates(attention.id)
            ]
            if attention is not None
            else []
        )
        payload["attention_decision"] = (
            domain_dict(attention) if attention is not None else None
        )
        payload["latest_attention_decision"] = payload["attention_decision"]
        payload["attention_decision_revision"] = (
            attention.decision_revision if attention is not None else None
        )
        payload["attention_candidates"] = candidates
        candidate_by_goal = {item["goal_id"]: item for item in candidates}
        wake_conditions = [
            domain_dict(item) for item in self.store.wake_conditions(session.id)
        ]
        payload["wake_conditions"] = wake_conditions
        pending_wake_by_goal = {
            item["goal_id"]: item
            for item in wake_conditions
            if item["status"] == "PENDING"
        }
        continuation_by_goal: dict[str, list[dict[str, Any]]] = {
            node.id: [
                domain_dict(item) for item in self.store.continuations(node.id)
            ]
            for node in goal_nodes
        }
        payload["continuations"] = sorted(
            (
                item
                for history in continuation_by_goal.values()
                for item in history
            ),
            key=lambda item: (item.get("created_at") or "", item.get("id") or ""),
        )
        payload["pending_event_count"] = len(
            self.store.pending_inbox_events(session.id)
        )
        criteria_by_goal = {
            item.id: [domain_dict(criterion) for criterion in self.store.criteria(item.id)]
            for item in goal_nodes
        }
        payload["goal_criteria"] = criteria_by_goal
        activation_intents = [
            domain_dict(item) for item in self.store.activation_intents(session.id)
        ]
        payload["activation_intents"] = activation_intents
        activation_by_goal = {item["goal_node_id"]: item for item in activation_intents}
        binding_by_goal = {item.goal_node_id: domain_dict(item) for item in bindings}
        for node in payload["goal_nodes"]:
            node_id = node["id"]
            activation = activation_by_goal.get(node_id)
            node["criteria"] = criteria_by_goal.get(node_id, [])
            node["activation_state"] = activation["status"] if activation else None
            node["activation_intent"] = activation
            node["binding"] = binding_by_goal.get(node_id)
            node["goal_run"] = payload["goal_runs"].get(node_id)
            candidate = candidate_by_goal.get(node_id)
            node["eligibility"] = (
                candidate.get("eligibility") if candidate is not None else None
            )
            node["eligible"] = (
                candidate is not None and candidate.get("eligibility") == "ELIGIBLE"
            )
            node["eligibility_reason"] = (
                candidate.get("reason") if candidate is not None else None
            )
            node["hard_tier"] = (
                candidate.get("hard_tier") if candidate is not None else None
            )
            node["attention_score"] = (
                candidate.get("total_score") if candidate is not None else None
            )
            if candidate is not None:
                node["scheduling_class"] = candidate.get("scheduling_class")
                node["next_eligible_at"] = candidate.get("next_eligible_at")
            node["wake_condition"] = pending_wake_by_goal.get(node_id)
            history = continuation_by_goal.get(node_id, [])
            node["continuation"] = history[-1] if history else None
        if payload["current_goal"] is not None:
            payload["current_goal"] = next(
                (
                    item
                    for item in payload["goal_nodes"]
                    if item["id"] == payload["current_goal"]["id"]
                ),
                payload["current_goal"],
            )
        raw_observations = self.store.raw_observations(session.id)
        control_transitions = self.store.control_transitions(session.id)
        pending_preemptions = self.store.pending_preemption_requests(session.id)
        latest_transition = control_transitions[-1] if control_transitions else None
        latest_raw = raw_observations[-1] if raw_observations else None
        latest_preemption = (
            pending_preemptions[-1] if pending_preemptions else None
        )
        payload["event_runtime"] = {
            "pending_preemption_event_id": (
                latest_preemption.event_id if latest_preemption is not None else None
            ),
            "pending_preemption_reason": (
                latest_preemption.reason if latest_preemption is not None else None
            ),
            "pending_preemption_requests": [
                domain_dict(item) for item in pending_preemptions
            ],
            "raw_observation_count": len(raw_observations),
            "latest_raw_observation": (
                domain_dict(latest_raw) if latest_raw is not None else None
            ),
            "recovery_context_ref": (
                latest_transition.fresh_observation_ref
                if latest_transition is not None
                else None
            ),
            "control_transition_reason": (
                latest_transition.reason if latest_transition is not None else None
            ),
        }
        return payload


def _goal_run_id(goal_run: Any) -> str:
    value = goal_run.get("id") if isinstance(goal_run, dict) else getattr(goal_run, "id", None)
    if not isinstance(value, str) or not value:
        raise TypeError("GoalService.create must return a GoalRun with a non-blank id")
    return value


def _goal_run_value(goal_run: Any, field: str) -> Any:
    if isinstance(goal_run, dict):
        return goal_run.get(field)
    return getattr(goal_run, field, None)


def _directive_clauses(directives: list[Any]) -> tuple[str, ...]:
    """Keep the coverage validator at the natural user-clause boundary."""

    import re

    clauses: list[str] = []
    for directive in directives:
        clauses.extend(
            part.strip() for part in re.split(r"[；;。]", directive.content) if part.strip()
        )
    return tuple(clauses)


def _retryable_device_busy(result: Any) -> bool:
    waiting = (
        result.get("waiting_reason")
        if isinstance(result, dict)
        else getattr(result, "waiting_reason", None)
    )
    if not isinstance(waiting, dict):
        return False
    code = str(waiting.get("code") or "").lower()
    return any(token in code for token in ("device_busy", "target_busy", "queue_full"))


def _goal_not_yet_eligible(goal: Any, now: datetime) -> bool:
    values = (
        getattr(goal, "next_eligible_at", None),
        getattr(goal, "backoff_until", None),
    )
    deadlines: list[datetime] = []
    for value in values:
        if not value:
            continue
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        deadlines.append(parsed.astimezone(UTC))
    return bool(deadlines and now.astimezone(UTC) < max(deadlines))


_EXTERNAL_ATTENTION_EVENT_TYPES = frozenset(
    {
        SessionEventType.NOTIFICATION_POSTED,
        SessionEventType.NOTIFICATION_REMOVED,
        SessionEventType.FOREGROUND_APPLICATION_CHANGED,
        SessionEventType.SCREEN_STATE_CHANGED,
        SessionEventType.LOCK_STATE_CHANGED,
        SessionEventType.NETWORK_STATE_CHANGED,
        SessionEventType.ORIENTATION_CHANGED,
        SessionEventType.CAPABILITIES_CHANGED,
        SessionEventType.TIMER_DUE,
        SessionEventType.DEVICE_BUSY,
        SessionEventType.DEVICE_AVAILABLE,
        SessionEventType.USER_FACT_ANSWERED,
        SessionEventType.COMPANION_CONNECTED,
        SessionEventType.COMPANION_DISCONNECTED,
        SessionEventType.COMPANION_HEARTBEAT,
        SessionEventType.BODY_EVENT,
    }
)


def _event_preemption_reason(event: Any) -> str:
    routing = event.data.get("routing") if isinstance(event.data, dict) else None
    if isinstance(routing, dict) and routing.get("reason"):
        return str(routing["reason"])
    return f"{event.event_type.value} affected an eligible Goal"


def _trusted_human_activity(event: Any) -> bool:
    payload = event.data if isinstance(event.data, dict) else {}
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        facts = payload
    caused_by = payload.get("caused_by_command_id") or facts.get(
        "caused_by_command_id"
    )
    if caused_by:
        return False
    presence = payload.get("human_presence") or facts.get("human_presence")
    if presence is not None and str(presence).upper() != "PRESENT":
        return False
    classification = facts.get("human_presence_classification")
    if classification is not None and str(classification).casefold() != "human":
        return False
    return True


def _is_retryable_dispatch_error(error: Exception) -> bool:
    code = str(getattr(error, "code", "")).lower()
    name = type(error).__name__.lower()
    text = f"{code} {name} {error}".lower()
    return any(
        token in text
        for token in (
            "device_busy",
            "target_busy",
            "targetbusy",
            "queue_full",
            "queuefull",
            "temporarily unavailable",
            "temporarily_unavailable",
        )
    )
