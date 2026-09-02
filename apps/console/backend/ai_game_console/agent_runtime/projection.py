"""JSON-safe AgentSession projections used by the v3 API and Console."""

from __future__ import annotations

from typing import Any

from .domain import (
    AgentSession,
    GoalNode,
    SessionGoalBinding,
    Task,
    TaskControlAction,
    TaskSubtask,
    UserDirective,
    domain_dict,
)


def session_projection(
    session: AgentSession,
    *,
    directives: list[UserDirective],
    goal_nodes: list[GoalNode],
    binding: SessionGoalBinding | None,
    goal_run: Any | None = None,
    bindings: list[SessionGoalBinding] | None = None,
    goal_runs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the R1 Console shape without making GoalRun the source of truth.

    ``goal_run`` is intentionally optional: losing the old Goal database or an
    unavailable compatibility executor must not erase the durable Session.
    """

    payload = domain_dict(session)
    payload["directives"] = [domain_dict(item) for item in directives]
    payload["goal_nodes"] = [domain_dict(item) for item in goal_nodes]
    payload["binding"] = domain_dict(binding) if binding is not None else None
    payload["bindings"] = [domain_dict(item) for item in (bindings or ())]
    payload["current_goal"] = _current_goal(session, goal_nodes)
    # ``goal_run`` is retained for v3/R1 consumers.  R2 clients use the
    # per-node mapping so independent goals never overwrite one another.
    payload["goal_run"] = _goal_run_projection(goal_run)
    payload["goal_runs"] = {
        goal_node_id: _goal_run_projection(item)
        for goal_node_id, item in (goal_runs or {}).items()
    }
    return payload


def event_projection(event: Any) -> dict[str, Any]:
    return domain_dict(event)


def task_projection(
    task: Task, *, subtasks: list[TaskSubtask] | None = None,
) -> dict[str, Any]:
    """Return the bounded v2 view without internal plans, credentials or IDs.

    The actual v2 route is Package A's ownership.  This shape is intentionally
    small so that adapter can only add source-approved fields rather than
    accidentally expose AgentRuntime's raw event payloads.
    """

    payload = domain_dict(task)
    # Capability owner/controller and DSH-origin identity remain persistence
    # facts.  They are deliberately absent from the renderer-safe projection:
    # owner filtering happens before this function is called, and origin is for
    # backend provenance/notice/deep-link routing rather than UI authority.
    payload.pop("owner_principal_id", None)
    payload.pop("controller_id", None)
    payload.pop("origin", None)
    payload["task_id"] = task.id
    payload["terminal"] = task.terminal
    payload["reason"] = domain_dict(task.reason)
    payload["allowed_controls"] = _allowed_task_controls(task)
    payload["subtasks"] = [
        {
            "subtask_id": item.id,
            "kind": item.kind,
            "object_ref": item.object_ref,
            "conversation_ref": item.conversation_ref,
            "status": item.status,
            "priority": item.priority,
            "current_stage": item.current_stage,
            "updated_at": item.updated_at,
        }
        for item in (subtasks or ())
    ]
    return payload


def task_event_projection(event: Any) -> dict[str, Any]:
    """Project only canonical task event fields through an allowlist."""

    data = event.data if isinstance(event.data, dict) else dict(event.data)
    allowed = {
        "status", "from_status", "reason_code", "recoverable", "next_wake_at",
        "revision", "revision_id", "base_revision", "kind", "effective_boundary",
        "control_id", "action", "subtask_id", "object_ref", "conversation_ref",
        "priority", "integrity_state",
    }
    return {
        "schema_version": 2,
        "event_id": event.id,
        "cursor": event.cursor,
        "task_id": event.session_id,
        "type": event.event_type.value,
        "task_revision": data.get("revision"),
        "occurred_at": event.occurred_at or event.created_at,
        "recorded_at": event.created_at,
        "data": {key: data[key] for key in allowed if key in data},
    }


def _allowed_task_controls(task: Task) -> list[str]:
    if task.terminal:
        return [TaskControlAction.ARCHIVE.value] if task.archived_at is None else []
    if task.status.value == "user_takeover":
        return [TaskControlAction.PAUSE.value, TaskControlAction.CANCEL.value, TaskControlAction.RELEASE_TAKEOVER.value,
                TaskControlAction.REPRIORITIZE.value]
    values = [TaskControlAction.PAUSE.value, TaskControlAction.CANCEL.value,
              TaskControlAction.TAKEOVER.value, TaskControlAction.REPRIORITIZE.value]
    if task.status.value in {"paused", "waiting_time", "waiting_event"}:
        values.insert(1, TaskControlAction.RESUME.value)
    return values


def _current_goal(session: AgentSession, goal_nodes: list[GoalNode]) -> dict[str, Any] | None:
    if session.active_goal_id is None:
        return None
    for goal_node in goal_nodes:
        if goal_node.id == session.active_goal_id:
            return domain_dict(goal_node)
    return None


def _goal_run_projection(goal_run: Any | None) -> dict[str, Any] | None:
    if goal_run is None:
        return None
    fields = (
        "id", "original_goal", "execution_status", "control_state",
        "resume_execution_status", "active_stage", "binding_kind", "binding_state",
        "bound_task_id", "target_id", "waiting_reason", "error", "result_summary",
        "created_at", "updated_at", "terminal_at",
    )
    return {field: _value(goal_run, field) for field in fields}


def _value(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)
