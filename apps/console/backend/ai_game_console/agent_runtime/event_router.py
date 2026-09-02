"""Persistent, deterministic Session EventInbox routing.

R7 keeps classification deliberately smaller than scheduling.  This module
may say which durable wake conditions an event matched and why, but it cannot
select, activate, or switch a Goal.  A route effect is only a request for a
new :class:`AttentionDecision` over the complete agenda.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable

from .agenda import EventUrgency

from .domain import (
    GoalNodeStatus,
    SessionEvent,
    SessionEventType,
    WakeCondition,
    WakeConditionDraft,
    WakeConditionKind,
)

if TYPE_CHECKING:
    from .store import SQLiteAgentRuntimeStore


class EventRouteClass(str, Enum):
    """Stable, typed classes used by the persisted R7 routing explanation."""

    NOTIFICATION = "notification"
    FOREGROUND = "foreground"
    TIMER = "timer"
    HUMAN_ACTIVITY = "human_activity"
    DEVICE_SYSTEM = "device_system"
    USER_DIRECTIVE = "user_directive"
    GOAL_STATE = "goal_state"
    USER_FACT = "user_fact"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class AffectedGoalRoute:
    """One exact WakeCondition match; never an execution instruction."""

    goal_id: str
    wake_condition_id: str
    matcher_kind: WakeConditionKind
    reason: str

    def to_payload(self) -> dict[str, str]:
        return {
            "goal_id": self.goal_id,
            "wake_condition_id": self.wake_condition_id,
            "matcher_kind": self.matcher_kind.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class EventRoutingClassification:
    """Server-owned explanation persisted alongside one EventInbox fact."""

    event_class: EventRouteClass
    urgency: EventUrgency
    reason: str
    routes: tuple[AffectedGoalRoute, ...]

    @property
    def affected_goal_ids(self) -> tuple[str, ...]:
        # A Goal can own more than one matching wake condition.  Scheduling
        # still receives one affected-Goal identity in first-match order.
        return tuple(dict.fromkeys(item.goal_id for item in self.routes))

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible ``routing`` payload."""

        return {
            "event_class": self.event_class.value,
            "urgency": self.urgency.name,
            "reason": self.reason,
            "routes": [item.to_payload() for item in self.routes],
        }


@dataclass(frozen=True, slots=True)
class EventRoutingResult:
    event: SessionEvent
    created: bool
    affected_wake_conditions: tuple[WakeCondition, ...]
    classification: EventRoutingClassification

    @property
    def affected_goal_ids(self) -> tuple[str, ...]:
        return self.classification.affected_goal_ids

    @property
    def routing(self) -> dict[str, Any]:
        return self.classification.to_payload()

    @property
    def duplicate(self) -> bool:
        return not self.created

    @property
    def new_route_effects(self) -> tuple[AffectedGoalRoute, ...]:
        """Exact wake effects caused by *this* intake call.

        Replays expose the original classification for diagnostics but must
        not request a second scheduling/preemption action.
        """

        return self.classification.routes if self.created else ()

    @property
    def requires_attention_decision(self) -> bool:
        """Whether routing should request, but never perform, Goal selection."""

        return bool(self.new_route_effects)


class SessionEventRouter:
    """Persist first, then apply exact WakeCondition matching once."""

    def __init__(self, store: SQLiteAgentRuntimeStore) -> None:
        self.store = store

    def ingest(
        self,
        session_id: str,
        *,
        source_namespace: str,
        source_event_id: str,
        event_type: SessionEventType | str,
        occurred_at: str,
        payload: dict[str, Any],
        device_id: str | None = None,
        device_boot_id: str | None = None,
        source_cursor: str | None = None,
        source_stream_id: str | None = None,
    ) -> EventRoutingResult:
        kind = _session_event_type(event_type)
        normalized_payload = normalize_event_payload(kind, payload)
        event, created = self.store.ingest_event(
            session_id,
            source_namespace=source_namespace,
            source_event_id=source_event_id,
            event_type=kind,
            occurred_at=occurred_at,
            payload=normalized_payload,
            device_id=device_id,
            device_boot_id=device_boot_id,
            source_cursor=source_cursor,
            source_stream_id=source_stream_id,
        )
        routed, wakes = self.store.route_inbox_event(event.id)
        classification = classify_event_routing(routed, wakes)
        routed = self.store.record_event_routing(
            routed.id, classification.to_payload()
        )
        return EventRoutingResult(
            event=routed,
            created=created,
            affected_wake_conditions=wakes,
            classification=classification,
        )


class SessionEventInbox(SessionEventRouter):
    """Product-contract name for the persistent R3 event intake boundary."""


class ExternalEventOrder(str, Enum):
    """Delivery ordering diagnostics; not an execution instruction."""

    NEW = "new"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"


@dataclass(frozen=True, slots=True)
class ExternalEventReceipt:
    source_key: str
    source_cursor: int | None
    ordering: ExternalEventOrder


class ExternalEventDeliveryGuard:
    """Keep duplicate/out-of-order producers from multiplying wake effects.

    Durable EventInbox dedupe remains the source of truth.  This lightweight
    guard deliberately accepts an unseen out-of-order event so exact wake
    matching can still decide whether it matters; it only reports its order
    and never drops a fact based solely on transport sequencing.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._last_cursor_by_stream: dict[str, int] = {}

    def receive(
        self, *, source_namespace: str, source_event_id: str,
        source_stream_id: str | None = None, source_cursor: str | int | None = None,
    ) -> ExternalEventReceipt:
        if not source_namespace.strip() or not source_event_id.strip():
            raise ValueError("external event source namespace and id are required")
        source_key = f"{source_namespace}:{source_event_id}"
        parsed_cursor = _external_cursor(source_cursor)
        if source_key in self._seen:
            return ExternalEventReceipt(source_key, parsed_cursor, ExternalEventOrder.DUPLICATE)
        self._seen.add(source_key)
        ordering = ExternalEventOrder.NEW
        if source_stream_id and parsed_cursor is not None:
            previous = self._last_cursor_by_stream.get(source_stream_id)
            if previous is not None and parsed_cursor < previous:
                ordering = ExternalEventOrder.OUT_OF_ORDER
            self._last_cursor_by_stream[source_stream_id] = max(previous or parsed_cursor, parsed_cursor)
        return ExternalEventReceipt(source_key, parsed_cursor, ordering)


def _external_cursor(value: str | int | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("external source_cursor must be an integer when supplied") from error
    if parsed < 0:
        raise ValueError("external source_cursor must not be negative")
    return parsed


def normalize_event_payload(
    event_type: SessionEventType | str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Normalize only stable event facts used by exact R7 matching.

    ``routing`` is server-owned and cannot be supplied by a Companion or API
    client.  Notification and foreground producers have historically used
    both ``package_name`` and ``foreground_package``; both are projected to
    the existing exact-match key ``application_package`` before persistence.
    """

    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    kind = _session_event_type(event_type)
    normalized = dict(payload)
    normalized.pop("routing", None)
    event_class = event_route_class(kind)
    if event_class in {
        EventRouteClass.NOTIFICATION,
        EventRouteClass.FOREGROUND,
        EventRouteClass.TIMER,
        EventRouteClass.HUMAN_ACTIVITY,
    }:
        normalized["urgency"] = _event_urgency(kind, normalized).name
    if event_class is EventRouteClass.NOTIFICATION:
        application_package = (
            normalized.get("application_package")
            or normalized.get("package_name")
            or normalized.get("foreground_package")
        )
        if application_package is not None:
            normalized["application_package"] = application_package
    elif event_class is EventRouteClass.FOREGROUND:
        application_package = (
            normalized.get("application_package")
            or normalized.get("foreground_package")
            or normalized.get("package_name")
        )
        if application_package is not None:
            normalized["application_package"] = application_package
    return normalized


def classify_event_routing(
    event: SessionEvent,
    affected_wake_conditions: Iterable[WakeCondition],
) -> EventRoutingClassification:
    """Build a deterministic route explanation from persisted facts.

    The matched conditions must come from the store's exact matcher.  Payload
    claims such as ``affected_goal_ids`` are intentionally never consulted.
    """

    event_class = event_route_class(event.event_type)
    routes = tuple(
        AffectedGoalRoute(
            goal_id=wake.goal_id,
            wake_condition_id=wake.id,
            matcher_kind=wake.kind,
            reason=_route_reason(event_class, wake),
        )
        for wake in affected_wake_conditions
    )
    goal_count = len(dict.fromkeys(item.goal_id for item in routes))
    if routes:
        reason = (
            f"exact {event_class.value} match affected {goal_count} Goal(s) "
            f"through {len(routes)} WakeCondition(s)"
        )
    else:
        reason = f"no pending WakeCondition matched the {event_class.value} event"
    return EventRoutingClassification(
        event_class=event_class,
        urgency=_event_urgency(event.event_type, event.data),
        reason=reason,
        routes=routes,
    )


def event_route_class(event_type: SessionEventType | str) -> EventRouteClass:
    kind = _session_event_type(event_type)
    if kind in {
        SessionEventType.NOTIFICATION_POSTED,
        SessionEventType.NOTIFICATION_REMOVED,
    }:
        return EventRouteClass.NOTIFICATION
    if kind is SessionEventType.FOREGROUND_APPLICATION_CHANGED:
        return EventRouteClass.FOREGROUND
    if kind is SessionEventType.TIMER_DUE:
        return EventRouteClass.TIMER
    if kind in {
        SessionEventType.HUMAN_TOUCH_STARTED,
        SessionEventType.HUMAN_TOUCH_ENDED,
        SessionEventType.HUMAN_IDLE,
    }:
        return EventRouteClass.HUMAN_ACTIVITY
    if kind is SessionEventType.USER_DIRECTIVE_EVENT:
        return EventRouteClass.USER_DIRECTIVE
    if kind is SessionEventType.GOAL_STATE_CHANGED:
        return EventRouteClass.GOAL_STATE
    if kind is SessionEventType.USER_FACT_ANSWERED:
        return EventRouteClass.USER_FACT
    if kind in {
        SessionEventType.SCREEN_STATE_CHANGED,
        SessionEventType.LOCK_STATE_CHANGED,
        SessionEventType.NETWORK_STATE_CHANGED,
        SessionEventType.ORIENTATION_CHANGED,
        SessionEventType.CAPABILITIES_CHANGED,
        SessionEventType.COMPANION_CONNECTED,
        SessionEventType.COMPANION_DISCONNECTED,
        SessionEventType.COMPANION_HEARTBEAT,
        SessionEventType.DEVICE_BUSY,
        SessionEventType.DEVICE_AVAILABLE,
        SessionEventType.BODY_EVENT,
    }:
        return EventRouteClass.DEVICE_SYSTEM
    return EventRouteClass.INTERNAL


def _event_urgency(
    event_type: SessionEventType | str,
    payload: Any,
) -> EventUrgency:
    kind = _session_event_type(event_type)
    supplied = payload.get("urgency") if isinstance(payload, dict) else None
    if supplied is not None:
        if isinstance(supplied, EventUrgency):
            return supplied
        if isinstance(supplied, str):
            try:
                return EventUrgency[supplied.strip().upper()]
            except KeyError as error:
                raise ValueError(f"unsupported event urgency: {supplied}") from error
        try:
            return EventUrgency(int(supplied))
        except (TypeError, ValueError) as error:
            raise ValueError(f"unsupported event urgency: {supplied}") from error
    defaults = {
        SessionEventType.NOTIFICATION_POSTED: EventUrgency.HIGH,
        SessionEventType.NOTIFICATION_REMOVED: EventUrgency.LOW,
        # A matching foreground transition must be able to outrank the
        # active Goal's ordinary continuity bonus and request re-evaluation.
        SessionEventType.FOREGROUND_APPLICATION_CHANGED: EventUrgency.NORMAL,
        SessionEventType.TIMER_DUE: EventUrgency.NORMAL,
        SessionEventType.HUMAN_TOUCH_STARTED: EventUrgency.CRITICAL,
        SessionEventType.HUMAN_TOUCH_ENDED: EventUrgency.HIGH,
        SessionEventType.HUMAN_IDLE: EventUrgency.HIGH,
    }
    return defaults.get(kind, EventUrgency.NONE)


def _route_reason(event_class: EventRouteClass, wake: WakeCondition) -> str:
    if wake.kind is WakeConditionKind.TIME:
        return "matched exact timer wake_condition_id"
    if wake.kind is WakeConditionKind.USER_FACT:
        return "matched exact user-fact need_id"
    matcher = wake.matcher or {}
    fields = tuple(
        sorted(
            key
            for key in matcher
            if key
            in {
                "event_type",
                "application_package",
                "person_hint",
                "conversation_hint",
                "device_id",
            }
        )
    )
    field_text = ", ".join(fields) if fields else "event identity"
    return f"matched exact {event_class.value} fields: {field_text}"


def _session_event_type(value: SessionEventType | str) -> SessionEventType:
    return value if isinstance(value, SessionEventType) else SessionEventType(value)


class DeviceBodyEventInbox:
    """Project one bound DeviceBody's facts into the sole Session EventInbox.

    This adapter intentionally has no Goal, wake-result, decision, or
    dispatch API.  It first validates that the supplied BodyEvent belongs to
    this Session's durable device binding, then delegates all persistence and
    idempotency to :class:`SessionEventInbox`.
    """

    def __init__(
        self,
        store: SQLiteAgentRuntimeStore,
        *,
        session_id: str | None = None,
        device_body_store: Any | None = None,
        event_handler: Callable[[str], Any] | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.device_body_store = device_body_store
        self.event_handler = event_handler
        self._inbox = SessionEventInbox(store)

    def ingest(self, event: Any) -> EventRoutingResult:
        session_id = self.session_id
        load_binding = getattr(self.device_body_store, "load_binding", None)
        binding = load_binding(event.binding_id) if callable(load_binding) else None
        if session_id is None:
            if binding is None:
                raise RuntimeError("DeviceBody EventInbox requires a binding resolver")
            session_id = binding.session_id
        session = self.store.get_session(session_id)
        if session.device_binding_id != event.binding_id:
            raise ValueError("BodyEvent does not belong to this AgentSession binding")
        event_type = _body_event_session_type(event)
        result = self._inbox.ingest(
            session_id,
            source_namespace=str(
                getattr(event, "source_namespace", "device-body")
            ),
            source_event_id=str(event.source_event_id),
            event_type=event_type,
            occurred_at=str(event.occurred_at),
            payload=_body_event_payload(event),
            device_id=str(event.device_id),
            device_boot_id=str(event.device_boot_id),
            source_cursor=(
                str(event.source_cursor)
                if event.source_cursor is not None
                else None
            ),
            source_stream_id=_body_event_source_stream_id(event),
        )
        if (
            getattr(getattr(event, "event_type", None), "value", None)
            == "CONNECTION_CHANGED"
            and getattr(getattr(event, "connection_state", None), "value", None)
            == "DISCONNECTED"
            and (
                binding is None
                or str(event.device_boot_id) == str(binding.device_boot_id)
            )
        ):
            self._project_bound_goals_waiting_device(
                session_id=session_id,
                device_id=str(event.device_id),
                created_by_event_id=result.event.id,
            )
        # The handler is the existing AgentSession service boundary.  It is
        # invoked only after durable intake/classification, so DeviceBody
        # never selects a Goal or bypasses EventInbox on its own.
        if (
            self.event_handler is not None
            and result.event.handling_status.value in {"RECEIVED", "CLASSIFIED"}
            and result.event.decision_id is None
        ):
            self.event_handler(result.event.id)
        return result

    def _project_bound_goals_waiting_device(
        self,
        *,
        session_id: str,
        device_id: str,
        created_by_event_id: str,
    ) -> None:
        """Fence only Goals already bound to device work; never select a Goal."""

        bound_goal_ids = {
            item.goal_node_id for item in self.store.bindings(session_id)
        }
        eligible = {
            GoalNodeStatus.PLANNED,
            GoalNodeStatus.READY,
            GoalNodeStatus.ACTIVE,
            GoalNodeStatus.CANDIDATE_COMPLETE,
        }
        for node in self.store.goal_nodes(session_id):
            if node.id not in bound_goal_ids or node.status not in eligible:
                continue
            self.store.create_wake_condition(
                session_id,
                node.id,
                WakeConditionDraft(
                    kind=WakeConditionKind.DEVICE,
                    matcher={"device_id": device_id},
                    created_by_event_id=created_by_event_id,
                ),
            )


def _body_event_session_type(event: Any | None = None) -> SessionEventType:
    """Project typed R5 facts while preserving the one EventInbox boundary."""

    try:
        body_type = getattr(getattr(event, "event_type", None), "value", None)
        source_namespace = str(
            getattr(event, "source_namespace", "device-body")
        )
        if source_namespace not in {
            "android-companion-v1",
            "pc-companion-watchdog",
        }:
            return SessionEventType.BODY_EVENT
        typed = {
            "NOTIFICATION_POSTED": SessionEventType.NOTIFICATION_POSTED,
            "NOTIFICATION_REMOVED": SessionEventType.NOTIFICATION_REMOVED,
            "FOREGROUND_CHANGED": SessionEventType.FOREGROUND_APPLICATION_CHANGED,
            "HUMAN_TOUCH_STARTED": SessionEventType.HUMAN_TOUCH_STARTED,
            "HUMAN_TOUCH_ENDED": SessionEventType.HUMAN_TOUCH_ENDED,
            "HUMAN_IDLE": SessionEventType.HUMAN_IDLE,
            "SCREEN_CHANGED": SessionEventType.SCREEN_STATE_CHANGED,
            "LOCK_CHANGED": SessionEventType.LOCK_STATE_CHANGED,
            "NETWORK_CHANGED": SessionEventType.NETWORK_STATE_CHANGED,
            "ORIENTATION_CHANGED": SessionEventType.ORIENTATION_CHANGED,
            "CAPABILITY_REVISION_READY": SessionEventType.CAPABILITIES_CHANGED,
            "HEARTBEAT": SessionEventType.COMPANION_HEARTBEAT,
        }
        if body_type in typed:
            return typed[body_type]
        if body_type == "CONNECTION_CHANGED":
            connection = getattr(
                getattr(event, "connection_state", None), "value", None
            )
            if connection == "CONNECTED":
                return SessionEventType.COMPANION_CONNECTED
            if connection == "DISCONNECTED":
                return SessionEventType.COMPANION_DISCONNECTED
        return SessionEventType.BODY_EVENT
    except AttributeError as error:  # pragma: no cover - old R3 package guard
        raise RuntimeError("AgentRuntime schema lacks the R4 BodyEvent type") from error


def _body_event_payload(event: Any) -> dict[str, Any]:
    """Copy only DeviceBody facts; selection and wake results stay server-side."""

    def value(name: str) -> Any:
        item = getattr(event, name, None)
        return getattr(item, "value", item)

    facts = getattr(event, "facts", {})
    if not isinstance(facts, dict):
        facts = dict(facts) if facts is not None else {}
    payload = {
        "body_event_id": str(event.id),
        "body_event_type": value("event_type"),
        "binding_id": str(event.binding_id),
        "source_cursor": event.source_cursor,
        "caused_by_command_id": value("caused_by_command_id"),
        "foreground_package": value("foreground_package"),
        "foreground_activity": value("foreground_activity"),
        "capability_revision": value("capability_revision"),
        "connection_state": value("connection_state"),
        "human_presence": value("human_presence"),
        "evidence_ref": value("evidence_ref"),
    }
    if facts:
        payload["facts"] = facts
    # The R3 exact wake matcher uses these stable names.  They remain device
    # facts; no Goal selection or completion field crosses this adapter.
    if value("event_type") == "NOTIFICATION_POSTED":
        payload.update(
            {
                "application_package": facts.get("application_package")
                or facts.get("package_name")
                or value("foreground_package"),
                "person_hint": facts.get("person_hint"),
                "conversation_hint": facts.get("conversation_hint"),
            }
        )
    return payload


def _body_event_source_stream_id(event: Any) -> str | None:
    """Keep per-install Companion cursors distinct inside the sole EventInbox."""

    if str(getattr(event, "source_namespace", "")) != "android-companion-v1":
        return None
    facts = getattr(event, "facts", {})
    if not isinstance(facts, dict):
        facts = dict(facts) if facts is not None else {}
    provenance = facts.get("provenance")
    if not isinstance(provenance, dict):
        return None
    install_id = provenance.get("install_id")
    if not isinstance(install_id, str) or not install_id.strip():
        return None
    return f"companion-install:{install_id.strip()}"
