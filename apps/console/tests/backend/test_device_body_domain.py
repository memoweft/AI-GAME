from __future__ import annotations

from dataclasses import fields
from inspect import signature

import pytest

from ai_game_console.device_body.domain import (
    BodyActionCommand,
    BodyActionType,
    BodyCommandAlreadyClaimed,
    BodyCommandStatus,
    BodyEvent,
    BodyEventType,
    BodyExecutionReceipt,
    CapabilityKind,
    CapabilityState,
    ConnectionState,
    DeviceSnapshot,
    DEVICE_BODY_PROTOCOL_REVISION,
    ExecutionReportState,
    HumanPresenceState,
    NetworkState,
    Orientation,
    ReceiptAcknowledgement,
    TransportDisposition,
)
from ai_game_console.device_body.ports import BodyEventInboxPort, DeviceBodyStorePort


TIME = "2026-08-24T12:00:00Z"
LATER = "2026-08-24T12:00:01Z"
DIGEST = "a" * 64


def _command(**overrides: object) -> BodyActionCommand:
    values: dict[str, object] = {
        "id": "body-command-1",
        "binding_id": "binding-1",
        "session_id": "session-1",
        "device_id": "adb:emulator-5554",
        "device_boot_id": "boot-1",
        "kernel_action_id": "kernel-action-1",
        "action_cursor": 1,
        "action_type": BodyActionType.OPEN_APP,
        "parameters": {"package": "com.android.settings"},
        "expected_state": {},
        "required_capabilities": (
            CapabilityKind.OPEN_APP,
            CapabilityKind.DEVICE_SNAPSHOT,
            CapabilityKind.FOREGROUND_APPLICATION,
        ),
        "status": BodyCommandStatus.PREPARED,
        "issued_at": TIME,
    }
    values.update(overrides)
    return BodyActionCommand(**values)  # type: ignore[arg-type]


def _receipt(**overrides: object) -> BodyExecutionReceipt:
    values: dict[str, object] = {
        "id": "receipt-1",
        "source_receipt_id": "adb-receipt-1",
        "command_id": "body-command-1",
        "kernel_action_id": "kernel-action-1",
        "binding_id": "binding-1",
        "device_id": "adb:emulator-5554",
        "device_boot_id": "boot-1",
        "adapter_id": "adb-compat",
        "acknowledgement": ReceiptAcknowledgement.ACKNOWLEDGED,
        "transport": TransportDisposition.ACCEPTED,
        "execution": ExecutionReportState.REPORTED,
        "started_at": LATER,
        "finished_at": "2026-08-24T12:00:02Z",
        "received_at": "2026-08-24T12:00:03Z",
        "retryable": False,
    }
    values.update(overrides)
    return BodyExecutionReceipt(**values)  # type: ignore[arg-type]


def test_action_parameters_are_canonical_and_digest_is_stable() -> None:
    first = _command(parameters={"package": "com.android.settings", "component": ".Settings"})
    second = _command(parameters={"component": ".Settings", "package": "com.android.settings"})

    assert first.parameters == second.parameters
    assert first.expected_state == {"foreground_package": "com.android.settings"}
    assert first.idempotency_digest == second.idempotency_digest

    with pytest.raises(ValueError, match="unsupported keys"):
        _command(parameters={"package": "com.android.settings", "raw_adb": "am start"})
    with pytest.raises(ValueError, match="must equal requested package"):
        _command(expected_state={"foreground_package": "other.package"})
    with pytest.raises(ValueError, match="protocol_revision"):
        _command(protocol_revision=DEVICE_BODY_PROTOCOL_REVISION + 1)
    with pytest.raises(ValueError, match="slice_id must be None"):
        _command(slice_id="r6-slice")
    with pytest.raises(ValueError, match="required_capabilities"):
        _command(required_capabilities=())


def test_tap_and_swipe_require_only_their_auditable_expected_state() -> None:
    with pytest.raises(ValueError, match="TAP expected_state is missing"):
        _command(
            action_type=BodyActionType.TAP,
            parameters={"x": 1, "y": 2},
            expected_state={},
            required_capabilities=(CapabilityKind.TAP,),
        )
    with pytest.raises(ValueError, match="unsupported keys"):
        _command(
            action_type=BodyActionType.TAP,
            parameters={"x": 1, "y": 2},
            expected_state={"foreground_package": "com.android.settings", "screenshot_changed": True},
            required_capabilities=(CapabilityKind.TAP,),
        )
    with pytest.raises(ValueError, match="SWIPE expected_state is missing"):
        _command(
            action_type=BodyActionType.SWIPE,
            parameters={"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4},
            expected_state={"foreground_package": "com.android.settings"},
            required_capabilities=(CapabilityKind.SWIPE,),
        )
    with pytest.raises(ValueError, match="expected_state"):
        _command(
            action_type=BodyActionType.BACK,
            parameters={},
            expected_state={"foreground_package": "com.android.settings"},
            required_capabilities=(CapabilityKind.BACK,),
        )


def test_capability_vocabulary_distinguishes_setup_temporary_and_unsupported() -> None:
    assert {
        CapabilityKind.DEVICE_SNAPSHOT,
        CapabilityKind.SCREEN_CAPTURE,
        CapabilityKind.ACCESSIBILITY_TREE,
        CapabilityKind.FOREGROUND_APPLICATION,
        CapabilityKind.FOREGROUND_ACTIVITY,
    }.issubset(set(CapabilityKind))
    assert set(CapabilityState) == {
        CapabilityState.UNKNOWN,
        CapabilityState.READY,
        CapabilityState.NEEDS_USER_SETUP,
        CapabilityState.TEMPORARILY_UNAVAILABLE,
        CapabilityState.UNSUPPORTED,
    }


def test_unicode_command_keeps_plaintext_out_of_command_repr_and_requires_readback() -> None:
    command = _command(
        action_type=BodyActionType.INPUT_TEXT_UNICODE,
        required_capabilities=(
            CapabilityKind.INPUT_TEXT_UNICODE,
            CapabilityKind.TEXT_READ_BACK,
        ),
        parameters={
            "text_ref": "kernel-action:kernel-action-1:input",
            "text_sha256": DIGEST,
            "text_length": 4,
            "target_hint": "focused-editable",
        },
        expected_state={"read_back_text_sha256": DIGEST, "read_back_text_length": 4},
    )

    assert "你好" not in repr(command)
    assert command.parameters["text_ref"] == "kernel-action:kernel-action-1:input"
    with pytest.raises(ValueError, match="unsupported keys"):
        _command(
            action_type=BodyActionType.INPUT_TEXT_UNICODE,
            required_capabilities=(CapabilityKind.INPUT_TEXT_UNICODE, CapabilityKind.TEXT_READ_BACK),
            parameters={"text": "你好", "text_ref": "ref", "text_sha256": DIGEST, "text_length": 2},
            expected_state={"read_back_text_sha256": DIGEST, "read_back_text_length": 2},
        )
    with pytest.raises(ValueError, match="read-back hash"):
        _command(
            action_type=BodyActionType.INPUT_TEXT_UNICODE,
            required_capabilities=(CapabilityKind.INPUT_TEXT_UNICODE, CapabilityKind.TEXT_READ_BACK),
            parameters={"text_ref": "ref", "text_sha256": DIGEST, "text_length": 2},
            expected_state={"read_back_text_sha256": "b" * 64, "read_back_text_length": 2},
        )


def test_only_prepared_command_can_claim_a_physical_dispatch() -> None:
    dispatched = _command().claim_for_dispatch(at=LATER)

    assert dispatched.status is BodyCommandStatus.DISPATCHING
    assert dispatched.dispatched_at == LATER
    with pytest.raises(BodyCommandAlreadyClaimed):
        dispatched.claim_for_dispatch(at="2026-08-24T12:00:02Z")
    assert dispatched.acknowledge(at="2026-08-24T12:00:02Z").status is BodyCommandStatus.ACKNOWLEDGED


def test_fresh_snapshot_requires_same_binding_boot_command_and_newer_sequence() -> None:
    command = _command().claim_for_dispatch(at=LATER)
    receipt = _receipt()
    snapshot = DeviceSnapshot(
        id="snapshot-2",
        binding_id="binding-1",
        device_id="adb:emulator-5554",
        device_boot_id="boot-1",
        capture_request_id="capture:kernel-action-1",
        sequence=2,
        foreground_package="com.android.settings",
        foreground_activity=".Settings",
        screen_on=True,
        locked=False,
        network_state=NetworkState.CONNECTED,
        orientation=Orientation.PORTRAIT,
        human_presence=HumanPresenceState.ABSENT,
        requested_at="2026-08-24T12:00:02Z",
        capture_started_at="2026-08-24T12:00:02Z",
        capture_completed_at="2026-08-24T12:00:03Z",
        received_at="2026-08-24T12:00:03Z",
        observed_at="2026-08-24T12:00:03Z",
        caused_by_command_id=command.id,
    )

    assert snapshot.is_fresh_for(command, receipt, previous_sequence=1)
    assert not snapshot.is_fresh_for(command, receipt, previous_sequence=2)

    stale_boot = DeviceSnapshot(
        id="snapshot-stale-boot",
        binding_id="binding-1",
        device_id="adb:emulator-5554",
        device_boot_id="boot-2",
        capture_request_id="capture:kernel-action-1",
        sequence=3,
        foreground_package="com.android.settings",
        foreground_activity=".Settings",
        screen_on=True,
        locked=False,
        network_state=NetworkState.CONNECTED,
        orientation=Orientation.PORTRAIT,
        human_presence=HumanPresenceState.ABSENT,
        requested_at="2026-08-24T12:00:03Z",
        capture_started_at="2026-08-24T12:00:03Z",
        capture_completed_at="2026-08-24T12:00:03Z",
        received_at="2026-08-24T12:00:03Z",
        observed_at="2026-08-24T12:00:03Z",
        caused_by_command_id=command.id,
    )
    assert not stale_boot.is_fresh_for(command, receipt, previous_sequence=1)

    stale_request = DeviceSnapshot(
        id="snapshot-stale-request",
        binding_id="binding-1",
        device_id="adb:emulator-5554",
        device_boot_id="boot-1",
        capture_request_id="capture:kernel-action-1",
        sequence=3,
        foreground_package="com.android.settings",
        foreground_activity=".Settings",
        screen_on=True,
        locked=False,
        network_state=NetworkState.CONNECTED,
        orientation=Orientation.PORTRAIT,
        human_presence=HumanPresenceState.ABSENT,
        requested_at=LATER,
        capture_started_at=LATER,
        capture_completed_at="2026-08-24T12:00:02Z",
        received_at="2026-08-24T12:00:02Z",
        observed_at="2026-08-24T12:00:02Z",
        caused_by_command_id=command.id,
    )
    assert not stale_request.is_fresh_for(command, receipt, previous_sequence=1)


def test_snapshot_allows_bounded_cross_host_clock_skew_after_device_receipt() -> None:
    command = _command().claim_for_dispatch(at=LATER)
    receipt = _receipt()
    snapshot = DeviceSnapshot(
        id="snapshot-clock-skew",
        binding_id="binding-1",
        device_id="adb:emulator-5554",
        device_boot_id="boot-1",
        capture_request_id="capture:kernel-action-1",
        sequence=1,
        foreground_package="com.android.settings",
        foreground_activity=".Settings",
        screen_on=True,
        locked=False,
        network_state=NetworkState.CONNECTED,
        orientation=Orientation.PORTRAIT,
        human_presence=HumanPresenceState.ABSENT,
        # PC request time may be a few milliseconds after Android's local
        # capture start even though the request causally arrived first.
        requested_at="2026-08-24T12:00:02.100Z",
        capture_started_at="2026-08-24T12:00:02.050Z",
        capture_completed_at="2026-08-24T12:00:02.200Z",
        received_at="2026-08-24T12:00:03Z",
        observed_at="2026-08-24T12:00:02.200Z",
        caused_by_command_id=command.id,
    )

    assert snapshot.is_fresh_for(command, receipt, previous_sequence=0)


def test_receipt_has_no_kernel_verification_or_goal_success_authority() -> None:
    receipt = _receipt(adapter_code="0")

    names = {item.name for item in fields(BodyExecutionReceipt)}
    assert {"verification", "verified", "goal_status", "stage_status", "success"}.isdisjoint(names)
    assert receipt.execution is ExecutionReportState.REPORTED
    with pytest.raises(ValueError, match="cannot report execution"):
        _receipt(
            id="receipt-rejected",
            source_receipt_id="adb-receipt-rejected",
            acknowledgement=ReceiptAcknowledgement.NOT_ACKNOWLEDGED,
            transport=TransportDisposition.REJECTED,
            execution=ExecutionReportState.REPORTED,
            reason_code="transport_timeout",
        )

    mismatched = _receipt(device_boot_id="boot-2")
    with pytest.raises(ValueError, match="does not match"):
        mismatched.ensure_matches(_command())


def test_receipt_projects_only_no_replay_terminal_statuses() -> None:
    dispatched = _command().claim_for_dispatch(at=LATER)

    rejected = dispatched.settle_from_receipt(
        _receipt(
            acknowledgement=ReceiptAcknowledgement.NOT_ACKNOWLEDGED,
            transport=TransportDisposition.REJECTED,
            execution=ExecutionReportState.UNKNOWN,
            retryable=True,
            reason_code="transport_timeout",
        )
    )
    assert rejected.status is BodyCommandStatus.REJECTED
    with pytest.raises(BodyCommandAlreadyClaimed):
        rejected.claim_for_dispatch(at="2026-08-24T12:00:04Z")


def test_device_body_ports_separate_reconciliation_from_event_inbox() -> None:
    store_methods = set(DeviceBodyStorePort.__dict__)
    assert {
        "binding_for_session",
        "load_snapshot",
        "inspect_execution",
        "commands_needing_reconciliation",
    }.issubset(store_methods)
    assert "ingest_event" not in store_methods
    assert set(BodyEventInboxPort.__dict__) >= {"ingest"}
    assert set(signature(DeviceBodyStorePort.commands_needing_reconciliation).parameters) == {
        "self",
        "after_action_cursor",
        "limit",
    }


def test_body_event_is_a_device_fact_without_scheduling_fields_or_unicode_text() -> None:
    event = BodyEvent(
        id="event-1",
        binding_id="binding-1",
        device_id="adb:emulator-5554",
        device_boot_id="boot-1",
        source_event_id="foreground:17",
        source_cursor=17,
        event_type=BodyEventType.FOREGROUND_CHANGED,
        occurred_at=TIME,
        received_at=LATER,
        caused_by_command_id="body-command-1",
        foreground_package="com.android.settings",
        foreground_activity=".Settings",
    )

    names = {item.name for item in fields(BodyEvent)}
    forbidden = {"session_id", "goal_id", "attention_decision_id", "priority", "schedule", "verification"}
    assert forbidden.isdisjoint(names)
    assert "你好" not in repr(event)
    assert event.idempotency_key == BodyEvent(
        id="event-retry",
        binding_id="binding-1",
        device_id="adb:emulator-5554",
        device_boot_id="boot-1",
        source_event_id="foreground:17",
        source_cursor=18,
        event_type=BodyEventType.FOREGROUND_CHANGED,
        occurred_at=TIME,
        received_at=LATER,
        foreground_package="com.android.settings",
    ).idempotency_key
