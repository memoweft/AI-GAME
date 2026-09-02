from __future__ import annotations

import sqlite3
from dataclasses import fields, replace
from pathlib import Path

import pytest

from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.device_body.domain import (
    BodyActionCommand, BodyActionType, BodyCommandAlreadyClaimed, BodyCommandStatus,
    BodyExecutionReceipt, CapabilityKind, CapabilityState, ConnectionState,
    DeviceBodyBinding, DeviceBodyCapability, DeviceSnapshot, ExecutionReportState,
    HumanPresenceState, NetworkState, Orientation, ReceiptAcknowledgement,
    TransportDisposition,
)
from ai_game_console.device_body.store import (
    SQLiteDeviceBodyStore,
    _legacy_receipt_digest_without_install,
)


NOW = "2026-08-24T00:00:00Z"
DISPATCHED = "2026-08-24T00:00:01Z"
FINISHED = "2026-08-24T00:00:02Z"


def _session(database: Path) -> str:
    store = SQLiteAgentRuntimeStore(database)
    session, _ = store.create_unplanned_session(instruction="打开设置", client_request_id="session-1")
    return session.id


def _binding(session_id: str, *, binding_id: str = "binding-1", device_id: str = "device-1") -> DeviceBodyBinding:
    return DeviceBodyBinding(
        id=binding_id, session_id=session_id, device_id=device_id, adapter_id="adb",
        device_boot_id="boot-1", connection_state=ConnectionState.CONNECTED,
        capability_revision=0, event_cursor=0, action_cursor=0, bound_at=NOW, updated_at=NOW,
    )


def _snapshot(binding: DeviceBodyBinding, *, sequence: int = 1, request_id: str = "capture-1") -> DeviceSnapshot:
    return DeviceSnapshot(
        id=f"snapshot-{sequence}", binding_id=binding.id, device_id=binding.device_id,
        device_boot_id=binding.device_boot_id, capture_request_id=request_id, sequence=sequence,
        foreground_package="com.android.settings", foreground_activity=".Settings", screen_on=True,
        locked=False, network_state=NetworkState.CONNECTED, orientation=Orientation.PORTRAIT,
        human_presence=HumanPresenceState.ABSENT, requested_at=NOW, capture_started_at=NOW,
        capture_completed_at=FINISHED, received_at=FINISHED, observed_at=FINISHED,
    )


def _capabilities(binding: DeviceBodyBinding, *, revision: int = 1) -> tuple[DeviceBodyCapability, ...]:
    return tuple(
        DeviceBodyCapability(
            id=f"cap-{revision}-{kind.value}", binding_id=binding.id, device_id=binding.device_id,
            device_boot_id=binding.device_boot_id, revision=revision, kind=kind,
            state=CapabilityState.READY, reason_code=None, evidence_ref=None, observed_at=NOW,
        )
        for kind in CapabilityKind
    )


def _command(binding: DeviceBodyBinding, *, cursor: int = 1, action_id: str = "kernel-1") -> BodyActionCommand:
    return BodyActionCommand(
        id=f"command-{cursor}", binding_id=binding.id, session_id=binding.session_id,
        device_id=binding.device_id, device_boot_id=binding.device_boot_id, kernel_action_id=action_id,
        action_cursor=cursor, action_type=BodyActionType.OPEN_APP, parameters={"package": "com.android.settings"},
        expected_state={}, required_capabilities=(CapabilityKind.OPEN_APP, CapabilityKind.FOREGROUND_APPLICATION),
        status=BodyCommandStatus.PREPARED, issued_at=NOW,
    )


def _receipt(command: BodyActionCommand) -> BodyExecutionReceipt:
    return BodyExecutionReceipt(
        id="receipt-1", source_receipt_id="transport-receipt-1", command_id=command.id,
        kernel_action_id=command.kernel_action_id, binding_id=command.binding_id, device_id=command.device_id,
        device_boot_id=command.device_boot_id, adapter_id="adb",
        acknowledgement=ReceiptAcknowledgement.ACKNOWLEDGED, transport=TransportDisposition.ACCEPTED,
        execution=ExecutionReportState.REPORTED, started_at=DISPATCHED, finished_at=FINISHED,
        received_at=FINISHED, retryable=False,
    )


def test_session_binds_device_without_freezing_foreground_application(tmp_path: Path):
    database = tmp_path / "agent-runtime.db"
    session_id = _session(database)
    store = SQLiteDeviceBodyStore(database)
    binding = store.create_binding(_binding(session_id))

    restored = SQLiteAgentRuntimeStore(database).get_session(session_id)
    assert restored.device_binding_id == binding.id
    assert "foreground_package" not in {field.name for field in fields(DeviceBodyBinding)}
    store.persist_snapshot(_snapshot(binding))
    assert store.latest_snapshot(binding.id).foreground_package == "com.android.settings"


def test_binding_is_atomic_idempotent_for_same_device_and_conflicts_for_different_device(tmp_path: Path):
    database = tmp_path / "agent-runtime.db"
    session_id = _session(database)
    store = SQLiteDeviceBodyStore(database)
    first = store.create_binding(_binding(session_id))
    replay = store.create_binding(_binding(session_id, binding_id="another-id"))

    assert replay == first
    with pytest.raises(ValueError, match="different device"):
        store.create_binding(_binding(session_id, binding_id="other", device_id="device-2"))
    assert SQLiteDeviceBodyStore(database).binding_for_session(session_id) == first


def test_capability_snapshot_and_command_facts_are_idempotent_across_restart(tmp_path: Path):
    database = tmp_path / "agent-runtime.db"
    binding = SQLiteDeviceBodyStore(database).create_binding(_binding(_session(database)))
    store = SQLiteDeviceBodyStore(database)

    capabilities = _capabilities(binding)
    assert store.replace_capability_revision(binding.id, capabilities) == tuple(sorted(capabilities, key=lambda item: item.kind.value))
    assert SQLiteDeviceBodyStore(database).replace_capability_revision(binding.id, capabilities)
    snapshot = _snapshot(binding)
    assert store.persist_snapshot(snapshot) == snapshot
    assert SQLiteDeviceBodyStore(database).persist_snapshot(snapshot) == snapshot
    assert SQLiteDeviceBodyStore(database).load_snapshot(snapshot.id) == snapshot

    command = _command(binding)
    assert store.create_command(command) == command
    assert SQLiteDeviceBodyStore(database).create_command(command) == command
    assert SQLiteDeviceBodyStore(database).load_command_for_kernel_action("kernel-1") == command


def test_claim_is_compare_and_swap_and_receipt_settles_without_verification(tmp_path: Path):
    database = tmp_path / "agent-runtime.db"
    binding = SQLiteDeviceBodyStore(database).create_binding(_binding(_session(database)))
    store = SQLiteDeviceBodyStore(database)
    command = store.create_command(_command(binding))

    claimed = store.claim_command(command.id, at=DISPATCHED)
    assert claimed.status is BodyCommandStatus.DISPATCHING
    with pytest.raises(BodyCommandAlreadyClaimed):
        SQLiteDeviceBodyStore(database).claim_command(command.id, at=DISPATCHED)
    receipt = _receipt(command)
    assert store.record_receipt(receipt) == receipt
    assert SQLiteDeviceBodyStore(database).record_receipt(receipt) == receipt
    persisted, persisted_receipt = store.inspect_execution(command.kernel_action_id)
    assert persisted.status is BodyCommandStatus.SETTLED
    assert persisted_receipt == receipt


def test_companion_install_claim_requires_dispatch_and_is_stable(tmp_path: Path):
    database = tmp_path / "agent-runtime.db"
    binding = SQLiteDeviceBodyStore(database).create_binding(_binding(_session(database)))
    store = SQLiteDeviceBodyStore(database)
    command = store.create_command(_command(binding))

    with pytest.raises(ValueError, match="dispatching"):
        store.claim_companion_install(command.id, companion_install_id="install-1")

    dispatched = store.claim_command(command.id, at=DISPATCHED)
    claimed = store.claim_companion_install(
        dispatched.id, companion_install_id="install-1"
    )
    assert claimed.target_companion_install_id == "install-1"
    assert store.claim_companion_install(
        dispatched.id, companion_install_id="install-1"
    ) == claimed
    with pytest.raises(ValueError, match="another"):
        store.claim_companion_install(dispatched.id, companion_install_id="install-2")


def test_corrupt_command_and_receipt_digests_fail_closed_with_exact_legacy_read(
    tmp_path: Path,
):
    database = tmp_path / "agent-runtime.db"
    binding = SQLiteDeviceBodyStore(database).create_binding(_binding(_session(database)))
    store = SQLiteDeviceBodyStore(database)
    command = store.create_command(_command(binding))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE body_action_commands SET payload_digest=? WHERE command_id=?",
            ("b" * 64, command.id),
        )
        connection.commit()
    with pytest.raises(ValueError, match="payload digest"):
        SQLiteDeviceBodyStore(database).load_command(command.id)

    # Build a fresh valid command/receipt, then prove only the exact old
    # two-field digest is accepted for historical rows without install epoch.
    database = tmp_path / "legacy-receipt.db"
    binding = SQLiteDeviceBodyStore(database).create_binding(_binding(_session(database)))
    store = SQLiteDeviceBodyStore(database)
    command = store.create_command(_command(binding))
    store.claim_command(command.id, at=DISPATCHED)
    receipt = _receipt(command)
    store.record_receipt(receipt)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE body_execution_receipts SET payload_digest=? WHERE receipt_id=?",
            (_legacy_receipt_digest_without_install(receipt), receipt.id),
        )
        connection.commit()
    assert SQLiteDeviceBodyStore(database).inspect_execution(command.kernel_action_id)[1] == receipt
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE body_execution_receipts SET payload_digest=? WHERE receipt_id=?",
            ("b" * 64, receipt.id),
        )
        connection.commit()
    with pytest.raises(ValueError, match="payload digest"):
        SQLiteDeviceBodyStore(database).inspect_execution(command.kernel_action_id)


def test_reconciliation_cursor_returns_only_inflight_commands_after_restart(tmp_path: Path):
    database = tmp_path / "agent-runtime.db"
    binding = SQLiteDeviceBodyStore(database).create_binding(_binding(_session(database)))
    store = SQLiteDeviceBodyStore(database)
    first = store.create_command(_command(binding, cursor=1, action_id="kernel-1"))
    store.claim_command(first.id, at=DISPATCHED)
    second = store.create_command(_command(binding, cursor=2, action_id="kernel-2"))
    store.claim_command(second.id, at=DISPATCHED)
    store.record_receipt(_receipt(first))

    restarted = SQLiteDeviceBodyStore(database)
    assert restarted.commands_needing_reconciliation(after_action_cursor=0, limit=10) == (
        replace(second, status=BodyCommandStatus.DISPATCHING, dispatched_at=DISPATCHED),
    )
    assert restarted.commands_needing_reconciliation(after_action_cursor=2, limit=10) == ()


def test_v4_nonempty_device_binding_id_backfills_a_degraded_device_record(tmp_path: Path):
    database = tmp_path / "agent-runtime.db"
    session_id = _session(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE agent_sessions SET device_binding_id='legacy-binding'")
        connection.execute("DROP TABLE body_execution_receipts")
        connection.execute("DROP TABLE body_action_commands")
        connection.execute("DROP TABLE device_snapshots")
        connection.execute("DROP TABLE device_body_capabilities")
        connection.execute("DROP TABLE device_body_bindings")
        connection.execute("UPDATE agent_runtime_schema SET revision=4 WHERE singleton=1")
        connection.commit()

    migrated = SQLiteAgentRuntimeStore(database)
    migrated.initialize()
    binding = SQLiteDeviceBodyStore(database).load_binding("legacy-binding")
    assert binding.session_id == session_id
    assert binding.connection_state is ConnectionState.DEGRADED
    assert binding.device_id == "legacy-binding:legacy-binding"
