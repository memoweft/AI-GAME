"""Transport-neutral R4 DeviceBody domain contract.

The objects here are intentionally narrower than RuntimeKernel facts.  They
describe a bound physical device, a single claimed command, device-side
receipts and observations.  In particular, neither receipts nor body events
can assert Goal, Stage, or verification success.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping


DEVICE_BODY_PROTOCOL_REVISION = 1
"""The frozen R4 DeviceBody wire/domain contract revision."""


class ConnectionState(StrEnum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    DEGRADED = "DEGRADED"
    UNAUTHORIZED = "UNAUTHORIZED"


class CapabilityKind(StrEnum):
    DEVICE_SNAPSHOT = "DEVICE_SNAPSHOT"
    SCREEN_CAPTURE = "SCREEN_CAPTURE"
    ACCESSIBILITY_TREE = "ACCESSIBILITY_TREE"
    FOREGROUND_APPLICATION = "FOREGROUND_APPLICATION"
    FOREGROUND_ACTIVITY = "FOREGROUND_ACTIVITY"
    OPEN_APP = "OPEN_APP"
    TAP = "TAP"
    LONG_PRESS = "LONG_PRESS"
    SWIPE = "SWIPE"
    INPUT_TEXT_UNICODE = "INPUT_TEXT_UNICODE"
    TEXT_READ_BACK = "TEXT_READ_BACK"
    BACK = "BACK"
    HOME = "HOME"
    RECENTS = "RECENTS"
    WAIT = "WAIT"
    CAPTURE_SNAPSHOT = "CAPTURE_SNAPSHOT"
    NOTIFICATION_LISTENER = "NOTIFICATION_LISTENER"
    GESTURE_DISPATCH = "GESTURE_DISPATCH"
    UNICODE_SET_TEXT = "UNICODE_SET_TEXT"
    UNICODE_IME = "UNICODE_IME"
    HUMAN_PRESENCE = "HUMAN_PRESENCE"
    SCREEN_STATE = "SCREEN_STATE"
    LOCK_STATE = "LOCK_STATE"
    NETWORK_STATE = "NETWORK_STATE"
    ORIENTATION_STATE = "ORIENTATION_STATE"
    EVENT_JOURNAL = "EVENT_JOURNAL"
    COMMAND_JOURNAL = "COMMAND_JOURNAL"
    HEARTBEAT = "HEARTBEAT"
    COMPANION_BRIDGE = "COMPANION_BRIDGE"


class CapabilityState(StrEnum):
    UNKNOWN = "UNKNOWN"
    READY = "READY"
    NEEDS_USER_SETUP = "NEEDS_USER_SETUP"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"


class BodyActionType(StrEnum):
    OPEN_APP = "OPEN_APP"
    TAP = "TAP"
    LONG_PRESS = "LONG_PRESS"
    SWIPE = "SWIPE"
    INPUT_TEXT_UNICODE = "INPUT_TEXT_UNICODE"
    BACK = "BACK"
    HOME = "HOME"
    RECENTS = "RECENTS"
    WAIT = "WAIT"
    CAPTURE_SNAPSHOT = "CAPTURE_SNAPSHOT"


class BodyCommandStatus(StrEnum):
    """Durable dispatch lifecycle; it is not a verification lifecycle."""

    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"


class ReceiptAcknowledgement(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    NOT_ACKNOWLEDGED = "NOT_ACKNOWLEDGED"


class TransportDisposition(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ExecutionReportState(StrEnum):
    REPORTED = "REPORTED"
    UNKNOWN = "UNKNOWN"


class NetworkState(StrEnum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    UNKNOWN = "UNKNOWN"


class Orientation(StrEnum):
    PORTRAIT = "PORTRAIT"
    LANDSCAPE = "LANDSCAPE"
    UNKNOWN = "UNKNOWN"


class HumanPresenceState(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class BodyEventType(StrEnum):
    NOTIFICATION_POSTED = "NOTIFICATION_POSTED"
    NOTIFICATION_REMOVED = "NOTIFICATION_REMOVED"
    FOREGROUND_CHANGED = "FOREGROUND_CHANGED"
    CAPABILITY_REVISION_READY = "CAPABILITY_REVISION_READY"
    CONNECTION_CHANGED = "CONNECTION_CHANGED"
    HUMAN_PRESENCE_CHANGED = "HUMAN_PRESENCE_CHANGED"
    HUMAN_TOUCH_STARTED = "HUMAN_TOUCH_STARTED"
    HUMAN_TOUCH_ENDED = "HUMAN_TOUCH_ENDED"
    HUMAN_IDLE = "HUMAN_IDLE"
    SCREEN_CHANGED = "SCREEN_CHANGED"
    LOCK_CHANGED = "LOCK_CHANGED"
    NETWORK_CHANGED = "NETWORK_CHANGED"
    ORIENTATION_CHANGED = "ORIENTATION_CHANGED"
    HEARTBEAT = "HEARTBEAT"
    SNAPSHOT_CAPTURED = "SNAPSHOT_CAPTURED"


class BodyCommandAlreadyClaimed(ValueError):
    """The Kernel action already owns a physical dispatch attempt."""


@dataclass(frozen=True, slots=True)
class DeviceBodyBinding:
    """A Session's reference to a device, never to its foreground app."""

    id: str
    session_id: str
    device_id: str
    adapter_id: str
    device_boot_id: str
    connection_state: ConnectionState
    capability_revision: int
    event_cursor: int
    action_cursor: int
    bound_at: str
    updated_at: str
    last_heartbeat_at: str | None = None
    protocol_revision: int = DEVICE_BODY_PROTOCOL_REVISION

    def __post_init__(self) -> None:
        _require_protocol_revision(self.protocol_revision, "binding protocol_revision")
        for value, label in (
            (self.id, "binding id"),
            (self.session_id, "binding session_id"),
            (self.device_id, "binding device_id"),
            (self.adapter_id, "binding adapter_id"),
            (self.device_boot_id, "binding device_boot_id"),
        ):
            _require_text(value, label)
        if self.capability_revision < 0:
            raise ValueError("binding capability_revision must not be negative")
        if self.event_cursor < 0 or self.action_cursor < 0:
            raise ValueError("binding cursors must not be negative")
        _require_utc(self.bound_at, "binding bound_at")
        _require_utc(self.updated_at, "binding updated_at")
        _require_optional_utc(self.last_heartbeat_at, "binding last_heartbeat_at")


@dataclass(frozen=True, slots=True)
class DeviceBodyCapability:
    """One member of a complete, append-only capability revision."""

    id: str
    binding_id: str
    device_id: str
    device_boot_id: str
    revision: int
    kind: CapabilityKind
    state: CapabilityState
    reason_code: str | None
    evidence_ref: str | None
    observed_at: str
    protocol_revision: int = DEVICE_BODY_PROTOCOL_REVISION

    def __post_init__(self) -> None:
        _require_protocol_revision(self.protocol_revision, "capability protocol_revision")
        for value, label in (
            (self.id, "capability id"),
            (self.binding_id, "capability binding_id"),
            (self.device_id, "capability device_id"),
            (self.device_boot_id, "capability device_boot_id"),
        ):
            _require_text(value, label)
        if self.revision < 1:
            raise ValueError("capability revision must be positive")
        if self.state is CapabilityState.READY:
            if self.reason_code is not None:
                raise ValueError("READY capability cannot carry reason_code")
        else:
            _require_text(self.reason_code or "", "unavailable capability reason_code")
        if self.evidence_ref is not None:
            _require_text(self.evidence_ref, "capability evidence_ref")
        _require_utc(self.observed_at, "capability observed_at")


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """A captured device fact used by Kernel for subsequent verification."""

    id: str
    binding_id: str
    device_id: str
    device_boot_id: str
    capture_request_id: str
    sequence: int
    foreground_package: str | None
    foreground_activity: str | None
    screen_on: bool
    locked: bool
    network_state: NetworkState
    orientation: Orientation
    human_presence: HumanPresenceState
    requested_at: str
    capture_started_at: str
    capture_completed_at: str
    received_at: str
    observed_at: str
    caused_by_command_id: str | None = None
    screenshot_ref: str | None = None
    accessibility_tree_ref: str | None = None
    input_read_back_sha256: str | None = None
    input_read_back_length: int | None = None
    input_method: str | None = None
    protocol_revision: int = DEVICE_BODY_PROTOCOL_REVISION

    def __post_init__(self) -> None:
        _require_protocol_revision(self.protocol_revision, "snapshot protocol_revision")
        for value, label in (
            (self.id, "snapshot id"),
            (self.binding_id, "snapshot binding_id"),
            (self.device_id, "snapshot device_id"),
            (self.device_boot_id, "snapshot device_boot_id"),
            (self.capture_request_id, "snapshot capture_request_id"),
        ):
            _require_text(value, label)
        if self.sequence < 1:
            raise ValueError("snapshot sequence must be positive")
        if self.foreground_package is not None:
            _require_text(self.foreground_package, "snapshot foreground_package")
        if self.foreground_activity is not None:
            _require_text(self.foreground_activity, "snapshot foreground_activity")
        if self.foreground_activity is not None and self.foreground_package is None:
            raise ValueError("snapshot foreground_activity requires foreground_package")
        if not isinstance(self.screen_on, bool) or not isinstance(self.locked, bool):
            raise ValueError("snapshot screen_on and locked must be booleans")
        _require_optional_text(self.caused_by_command_id, "snapshot caused_by_command_id")
        _require_optional_text(self.screenshot_ref, "snapshot screenshot_ref")
        _require_optional_text(self.accessibility_tree_ref, "snapshot accessibility_tree_ref")
        if (self.input_read_back_sha256 is None) != (
            self.input_read_back_length is None
        ):
            raise ValueError("snapshot read-back hash and length must be supplied together")
        if self.input_read_back_sha256 is not None:
            if (
                len(self.input_read_back_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.input_read_back_sha256
                )
            ):
                raise ValueError("snapshot input_read_back_sha256 must be lowercase SHA-256")
            if self.input_read_back_length is None or self.input_read_back_length < 0:
                raise ValueError("snapshot input_read_back_length must not be negative")
        if self.input_method is not None:
            if self.input_method not in {
                "ACCESSIBILITY_NODE_READ_BACK",
                "AI_GAME_IME_READ_BACK",
            }:
                raise ValueError("unsupported snapshot input_method")
            if self.input_read_back_sha256 is None:
                raise ValueError("snapshot input_method requires read-back hash/length")
        requested = _require_utc(self.requested_at, "snapshot requested_at")
        started = _require_utc(self.capture_started_at, "snapshot capture_started_at")
        completed = _require_utc(self.capture_completed_at, "snapshot capture_completed_at")
        received = _require_utc(self.received_at, "snapshot received_at")
        observed = _require_utc(self.observed_at, "snapshot observed_at")
        # ``requested_at`` and ``received_at`` use the PC clock, while the
        # capture interval is reported by Android.  Their absolute ordering
        # is not trustworthy under ordinary cross-host clock skew.  Preserve
        # the two same-clock intervals and the server-side request lifetime;
        # freshness against the device receipt is checked separately using
        # the Android timestamps in the Companion client.
        if started > completed or requested > received or completed > received:
            raise ValueError("snapshot capture timestamps are out of order")
        if observed != completed:
            raise ValueError("snapshot observed_at must equal capture_completed_at")

    def is_fresh_for(
        self,
        command: BodyActionCommand,
        receipt: BodyExecutionReceipt,
        *,
        previous_sequence: int,
    ) -> bool:
        """Return the correlation facts that a store can decide without Kernel.

        The caller still must check that this is the binding's persisted latest
        snapshot and that its projection is the Kernel task's latest
        Observation.  Those are cross-store facts, intentionally outside this
        value object.
        """

        receipt.ensure_matches(command)
        receipt_finished = _require_utc(receipt.finished_at, "receipt finished_at")
        requested = _require_utc(self.requested_at, "snapshot requested_at")
        return (
            self.binding_id == command.binding_id
            and self.device_id == command.device_id
            and self.device_boot_id == command.device_boot_id
            and self.caused_by_command_id == command.id
            and self.sequence > previous_sequence
            and requested >= receipt_finished
        )


@dataclass(frozen=True, slots=True)
class BodyActionCommand:
    """A single DeviceBody dispatch correlated one-to-one with a Kernel Action."""

    id: str
    binding_id: str
    session_id: str
    device_id: str
    device_boot_id: str
    kernel_action_id: str
    action_cursor: int
    action_type: BodyActionType
    parameters: Mapping[str, Any]
    expected_state: Mapping[str, Any]
    required_capabilities: tuple[CapabilityKind, ...]
    status: BodyCommandStatus
    issued_at: str
    target_companion_install_id: str | None = None
    slice_id: str | None = None
    dispatched_at: str | None = None
    acknowledged_at: str | None = None
    settled_at: str | None = None
    protocol_revision: int = DEVICE_BODY_PROTOCOL_REVISION

    def __post_init__(self) -> None:
        _require_protocol_revision(self.protocol_revision, "command protocol_revision")
        for value, label in (
            (self.id, "command id"),
            (self.binding_id, "command binding_id"),
            (self.session_id, "command session_id"),
            (self.device_id, "command device_id"),
            (self.device_boot_id, "command device_boot_id"),
            (self.kernel_action_id, "command kernel_action_id"),
        ):
            _require_text(value, label)
        if self.slice_id is not None:
            raise ValueError("R4 BodyActionCommand slice_id must be None")
        _require_optional_text(
            self.target_companion_install_id,
            "command target_companion_install_id",
        )
        if self.action_cursor < 1:
            raise ValueError("command action_cursor must be positive")
        if not isinstance(self.required_capabilities, tuple) or not self.required_capabilities:
            raise ValueError("command required_capabilities must be a non-empty tuple")
        if any(not isinstance(item, CapabilityKind) for item in self.required_capabilities):
            raise ValueError("command required_capabilities must contain CapabilityKind values")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("command required_capabilities must not contain duplicates")
        parameters = canonical_parameters(self.action_type, self.parameters)
        expected_state = canonical_expected_state(
            self.action_type, parameters, self.expected_state
        )
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "expected_state", expected_state)
        issued = _require_utc(self.issued_at, "command issued_at")
        dispatched = _require_optional_utc(self.dispatched_at, "command dispatched_at")
        acknowledged = _require_optional_utc(
            self.acknowledged_at, "command acknowledged_at"
        )
        settled = _require_optional_utc(self.settled_at, "command settled_at")
        if dispatched is not None and dispatched < issued:
            raise ValueError("command dispatch precedes issue")
        if acknowledged is not None and (dispatched is None or acknowledged < dispatched):
            raise ValueError("command acknowledgement precedes dispatch")
        if settled is not None and (dispatched is None or settled < dispatched):
            raise ValueError("command settlement precedes dispatch")
        if self.status is BodyCommandStatus.PREPARED:
            if any(value is not None for value in (self.dispatched_at, self.acknowledged_at, self.settled_at)):
                raise ValueError("PREPARED command cannot have dispatch timestamps")
        elif self.status is BodyCommandStatus.DISPATCHING:
            if self.dispatched_at is None or self.acknowledged_at is not None or self.settled_at is not None:
                raise ValueError("DISPATCHING command requires only dispatched_at")
        elif self.status is BodyCommandStatus.ACKNOWLEDGED:
            if self.dispatched_at is None or self.acknowledged_at is None or self.settled_at is not None:
                raise ValueError("ACKNOWLEDGED command requires dispatch and acknowledgement")
        elif self.status in {
            BodyCommandStatus.SETTLED,
            BodyCommandStatus.REJECTED,
            BodyCommandStatus.UNCERTAIN,
        }:
            if self.dispatched_at is None or self.settled_at is None:
                raise ValueError("terminal command requires dispatch and settlement")

    @property
    def idempotency_digest(self) -> str:
        """Stable cross-store identity; the store enforces kernel_action_id uniqueness."""

        return _digest(
            {
                "binding_id": self.binding_id,
                "session_id": self.session_id,
                "device_id": self.device_id,
                "device_boot_id": self.device_boot_id,
                "kernel_action_id": self.kernel_action_id,
                "action_cursor": self.action_cursor,
                "action_type": self.action_type.value,
                "parameters": self.parameters,
                "expected_state": self.expected_state,
                "required_capabilities": [item.value for item in self.required_capabilities],
            }
        )

    def claim_for_dispatch(self, *, at: str) -> BodyActionCommand:
        """Claim exactly once; reconciliation must inspect later states instead."""

        _require_utc(at, "command claim timestamp")
        if self.status is not BodyCommandStatus.PREPARED:
            raise BodyCommandAlreadyClaimed(
                f"Kernel action {self.kernel_action_id} is already {self.status.value}"
            )
        return replace(self, status=BodyCommandStatus.DISPATCHING, dispatched_at=at)

    def acknowledge(self, *, at: str) -> BodyActionCommand:
        _require_utc(at, "command acknowledgement timestamp")
        if self.status is not BodyCommandStatus.DISPATCHING:
            raise ValueError("only DISPATCHING command can be acknowledged")
        return replace(self, status=BodyCommandStatus.ACKNOWLEDGED, acknowledged_at=at)

    def settle(
        self, *, at: str, terminal_status: BodyCommandStatus = BodyCommandStatus.SETTLED
    ) -> BodyActionCommand:
        _require_utc(at, "command settlement timestamp")
        if self.status not in {BodyCommandStatus.DISPATCHING, BodyCommandStatus.ACKNOWLEDGED}:
            raise ValueError("only dispatched command can be settled")
        if terminal_status not in {
            BodyCommandStatus.SETTLED,
            BodyCommandStatus.REJECTED,
            BodyCommandStatus.UNCERTAIN,
        }:
            raise ValueError("settlement requires a non-dispatch terminal status")
        return replace(self, status=terminal_status, settled_at=at)

    def settle_from_receipt(self, receipt: BodyExecutionReceipt) -> BodyActionCommand:
        """Project only the receipt's transport uncertainty into a no-replay state."""

        receipt.ensure_matches(self)
        if self.status not in {BodyCommandStatus.DISPATCHING, BodyCommandStatus.ACKNOWLEDGED}:
            raise ValueError("only dispatched command can record a receipt")
        if receipt.transport is TransportDisposition.REJECTED:
            terminal = BodyCommandStatus.REJECTED
        elif receipt.execution is ExecutionReportState.UNKNOWN:
            terminal = BodyCommandStatus.UNCERTAIN
        else:
            terminal = BodyCommandStatus.SETTLED
        acknowledged_at = (
            receipt.received_at
            if receipt.acknowledgement is ReceiptAcknowledgement.ACKNOWLEDGED
            and self.status is BodyCommandStatus.DISPATCHING
            else self.acknowledged_at
        )
        return replace(
            self,
            status=terminal,
            acknowledged_at=acknowledged_at,
            settled_at=receipt.received_at,
        )


@dataclass(frozen=True, slots=True)
class BodyExecutionReceipt:
    """Transport and device report only; Kernel owns all verification verdicts."""

    id: str
    source_receipt_id: str
    command_id: str
    kernel_action_id: str
    binding_id: str
    device_id: str
    device_boot_id: str
    adapter_id: str
    acknowledgement: ReceiptAcknowledgement
    transport: TransportDisposition
    execution: ExecutionReportState
    started_at: str
    finished_at: str
    received_at: str
    retryable: bool
    adapter_code: str | None = None
    reason_code: str | None = None
    evidence_ref: str | None = None
    companion_install_id: str | None = None
    connection_epoch: int | None = None
    protocol_revision: int = DEVICE_BODY_PROTOCOL_REVISION

    def __post_init__(self) -> None:
        _require_protocol_revision(self.protocol_revision, "receipt protocol_revision")
        for value, label in (
            (self.id, "receipt id"),
            (self.source_receipt_id, "receipt source_receipt_id"),
            (self.command_id, "receipt command_id"),
            (self.kernel_action_id, "receipt kernel_action_id"),
            (self.binding_id, "receipt binding_id"),
            (self.device_id, "receipt device_id"),
            (self.device_boot_id, "receipt device_boot_id"),
            (self.adapter_id, "receipt adapter_id"),
        ):
            _require_text(value, label)
        started = _require_utc(self.started_at, "receipt started_at")
        finished = _require_utc(self.finished_at, "receipt finished_at")
        received = _require_utc(self.received_at, "receipt received_at")
        if not started <= finished <= received:
            raise ValueError("receipt timestamps are out of order")
        if not isinstance(self.retryable, bool):
            raise ValueError("receipt retryable must be a boolean")
        _require_optional_text(self.adapter_code, "receipt adapter_code")
        _require_optional_text(self.reason_code, "receipt reason_code")
        _require_optional_text(self.evidence_ref, "receipt evidence_ref")
        _require_optional_text(
            self.companion_install_id, "receipt companion_install_id"
        )
        if (self.companion_install_id is None) != (self.connection_epoch is None):
            raise ValueError(
                "receipt companion_install_id and connection_epoch must be present together"
            )
        if self.connection_epoch is not None and self.connection_epoch < 1:
            raise ValueError("receipt connection_epoch must be at least 1")
        if self.transport is TransportDisposition.REJECTED:
            if self.execution is not ExecutionReportState.UNKNOWN:
                raise ValueError("rejected transport cannot report execution")
            _require_text(self.reason_code or "", "rejected receipt reason_code")
        if self.acknowledgement is ReceiptAcknowledgement.NOT_ACKNOWLEDGED:
            if self.transport is not TransportDisposition.REJECTED:
                raise ValueError("unacknowledged receipt must have rejected transport")

    def ensure_matches(self, command: BodyActionCommand) -> None:
        if (
            self.command_id != command.id
            or self.kernel_action_id != command.kernel_action_id
            or self.binding_id != command.binding_id
            or self.device_id != command.device_id
            or self.device_boot_id != command.device_boot_id
        ):
            raise ValueError("receipt does not match command binding/device/boot")


@dataclass(frozen=True, slots=True)
class BodyEvent:
    """A device fact awaiting EventInbox classification, never a scheduler decision."""

    id: str
    binding_id: str
    device_id: str
    device_boot_id: str
    source_event_id: str
    source_cursor: int | None
    event_type: BodyEventType
    occurred_at: str
    received_at: str
    caused_by_command_id: str | None = None
    foreground_package: str | None = None
    foreground_activity: str | None = None
    capability_revision: int | None = None
    connection_state: ConnectionState | None = None
    human_presence: HumanPresenceState | None = None
    evidence_ref: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    source_namespace: str = "device-body"
    protocol_revision: int = DEVICE_BODY_PROTOCOL_REVISION

    def __post_init__(self) -> None:
        _require_protocol_revision(self.protocol_revision, "body event protocol_revision")
        for value, label in (
            (self.id, "body event id"),
            (self.binding_id, "body event binding_id"),
            (self.device_id, "body event device_id"),
            (self.device_boot_id, "body event device_boot_id"),
            (self.source_event_id, "body event source_event_id"),
        ):
            _require_text(value, label)
        if self.source_cursor is not None and self.source_cursor < 1:
            raise ValueError("body event source_cursor must be positive")
        _require_text(self.source_namespace, "body event source_namespace")
        occurred = _require_utc(self.occurred_at, "body event occurred_at")
        received = _require_utc(self.received_at, "body event received_at")
        if received < occurred:
            raise ValueError("body event receipt precedes occurrence")
        _require_optional_text(self.caused_by_command_id, "body event caused_by_command_id")
        _require_optional_text(self.foreground_package, "body event foreground_package")
        _require_optional_text(self.foreground_activity, "body event foreground_activity")
        _require_optional_text(self.evidence_ref, "body event evidence_ref")
        facts = _canonical_mapping(self.facts, "body event facts")
        forbidden = {
            "session_id",
            "binding_id",
            "goal",
            "goal_id",
            "goal_status",
            "goal_completion",
            "attention",
            "attention_decision",
            "completion",
            "verified",
            "verification",
            "verdict",
        }
        if any(str(key).casefold().replace("-", "_") in forbidden for key in facts):
            raise ValueError("BodyEvent facts cannot publish Goal/Attention/verification authority")
        object.__setattr__(self, "facts", facts)
        if self.foreground_activity is not None and self.foreground_package is None:
            raise ValueError("body event foreground_activity requires foreground_package")
        if self.capability_revision is not None and self.capability_revision < 1:
            raise ValueError("body event capability_revision must be positive")
        if self.event_type is BodyEventType.FOREGROUND_CHANGED:
            _require_text(self.foreground_package or "", "foreground event package")
        elif self.event_type is BodyEventType.CAPABILITY_REVISION_READY:
            if self.capability_revision is None:
                raise ValueError("capability event requires capability_revision")
        elif self.event_type is BodyEventType.CONNECTION_CHANGED:
            if self.connection_state is None:
                raise ValueError("connection event requires connection_state")
        elif self.event_type is BodyEventType.HUMAN_PRESENCE_CHANGED:
            if self.human_presence is None:
                raise ValueError("human presence event requires human_presence")

    @property
    def idempotency_key(self) -> str:
        return _digest(
            {
                "device_id": self.device_id,
                "device_boot_id": self.device_boot_id,
                "source_event_id": self.source_event_id,
            }
        )


def canonical_parameters(
    action_type: BodyActionType, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and freeze the one canonical parameter spelling for each action."""

    value = _canonical_mapping(parameters, "command parameters")
    if action_type is BodyActionType.OPEN_APP:
        _only_keys(value, {"package", "component"}, "OPEN_APP parameters")
        _require_text(value.get("package", ""), "OPEN_APP package")
        if "component" in value:
            _require_text(value["component"], "OPEN_APP component")
    elif action_type in {BodyActionType.TAP, BodyActionType.LONG_PRESS}:
        required = {"x", "y"}
        allowed = required | ({"duration_ms"} if action_type is BodyActionType.LONG_PRESS else set())
        _only_keys(value, allowed, f"{action_type.value} parameters")
        _required_keys(value, required, f"{action_type.value} parameters")
        _nonnegative_int(value["x"], f"{action_type.value} x")
        _nonnegative_int(value["y"], f"{action_type.value} y")
        if action_type is BodyActionType.LONG_PRESS:
            _positive_int(value.get("duration_ms", 600), "LONG_PRESS duration_ms")
            value.setdefault("duration_ms", 600)
    elif action_type is BodyActionType.SWIPE:
        required = {"start_x", "start_y", "end_x", "end_y"}
        _only_keys(value, required | {"duration_ms"}, "SWIPE parameters")
        _required_keys(value, required, "SWIPE parameters")
        for key in required:
            _nonnegative_int(value[key], f"SWIPE {key}")
        _positive_int(value.get("duration_ms", 300), "SWIPE duration_ms")
        value.setdefault("duration_ms", 300)
    elif action_type is BodyActionType.INPUT_TEXT_UNICODE:
        _only_keys(value, {"text_ref", "text_sha256", "text_length", "target_hint"}, "INPUT_TEXT_UNICODE parameters")
        _required_keys(value, {"text_ref", "text_sha256", "text_length"}, "INPUT_TEXT_UNICODE parameters")
        _require_text(value["text_ref"], "INPUT_TEXT_UNICODE text_ref")
        _sha256(value["text_sha256"], "INPUT_TEXT_UNICODE text_sha256")
        _nonnegative_int(value["text_length"], "INPUT_TEXT_UNICODE text_length")
        if "target_hint" in value:
            _require_text(value["target_hint"], "INPUT_TEXT_UNICODE target_hint")
    elif action_type is BodyActionType.WAIT:
        _only_keys(value, {"duration_ms"}, "WAIT parameters")
        _required_keys(value, {"duration_ms"}, "WAIT parameters")
        _positive_int(value["duration_ms"], "WAIT duration_ms")
    else:
        _only_keys(value, set(), f"{action_type.value} parameters")
    return value


def canonical_expected_state(
    action_type: BodyActionType,
    parameters: Mapping[str, Any],
    expected_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze expected state without allowing Unicode plaintext duplication."""

    value = _canonical_mapping(expected_state, "command expected_state")
    if action_type is BodyActionType.OPEN_APP:
        _only_keys(value, {"foreground_package", "foreground_activity"}, "OPEN_APP expected_state")
        value.setdefault("foreground_package", parameters["package"])
        _require_text(value["foreground_package"], "OPEN_APP foreground_package")
        if value["foreground_package"] != parameters["package"]:
            raise ValueError("OPEN_APP expected package must equal requested package")
        if "foreground_activity" in value:
            _require_text(value["foreground_activity"], "OPEN_APP foreground_activity")
    elif action_type is BodyActionType.TAP:
        _only_keys(value, {"foreground_package", "foreground_activity"}, "TAP expected_state")
        _required_keys(value, {"foreground_package"}, "TAP expected_state")
        _require_text(value["foreground_package"], "TAP foreground_package")
        if "foreground_activity" in value:
            _require_text(value["foreground_activity"], "TAP foreground_activity")
    elif action_type is BodyActionType.SWIPE:
        _only_keys(
            value,
            {
                "foreground_package",
                "foreground_activity",
                "accessibility_tree_changed_from",
            },
            "SWIPE expected_state",
        )
        _required_keys(
            value,
            {"foreground_package", "accessibility_tree_changed_from"},
            "SWIPE expected_state",
        )
        _require_text(value["foreground_package"], "SWIPE foreground_package")
        if "foreground_activity" in value:
            _require_text(value["foreground_activity"], "SWIPE foreground_activity")
        _sha256(
            value["accessibility_tree_changed_from"],
            "SWIPE accessibility_tree_changed_from",
        )
    elif action_type is BodyActionType.INPUT_TEXT_UNICODE:
        _only_keys(value, {"read_back_text_sha256", "read_back_text_length"}, "INPUT_TEXT_UNICODE expected_state")
        _required_keys(value, {"read_back_text_sha256", "read_back_text_length"}, "INPUT_TEXT_UNICODE expected_state")
        _sha256(value["read_back_text_sha256"], "INPUT_TEXT_UNICODE read_back_text_sha256")
        _nonnegative_int(value["read_back_text_length"], "INPUT_TEXT_UNICODE read_back_text_length")
        if value["read_back_text_sha256"] != parameters["text_sha256"]:
            raise ValueError("Unicode read-back hash must match the Kernel text reference")
        if value["read_back_text_length"] != parameters["text_length"]:
            raise ValueError("Unicode read-back length must match the Kernel text reference")
    else:
        _only_keys(value, set(), f"{action_type.value} expected_state")
    return value


def _canonical_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(canonical)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON serializable") from error
    if not isinstance(decoded, dict):  # defensive: JSON object input must stay object
        raise ValueError(f"{label} must be an object")
    return decoded


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _only_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ValueError(f"{label} contains unsupported keys: {', '.join(unexpected)}")


def _required_keys(value: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"{label} is missing required keys: {', '.join(missing)}")


def _require_text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be blank")


def _require_protocol_revision(value: Any, label: str) -> None:
    if value != DEVICE_BODY_PROTOCOL_REVISION:
        raise ValueError(
            f"{label} must equal {DEVICE_BODY_PROTOCOL_REVISION}"
        )


def _require_optional_text(value: str | None, label: str) -> None:
    if value is not None:
        _require_text(value, label)


def _require_utc(value: str, label: str) -> datetime:
    _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be in UTC")
    return parsed


def _require_optional_utc(value: str | None, label: str) -> datetime | None:
    return _require_utc(value, label) if value is not None else None


def _nonnegative_int(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _positive_int(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _sha256(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
