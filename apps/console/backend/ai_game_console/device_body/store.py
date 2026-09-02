"""SQLite persistence for R4 DeviceBody transport facts.

It shares agent-runtime.db and its schema ledger.  This module does not
create an event or verification ledger: EventInbox and RuntimeKernel remain
their respective authorities.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore

from .domain import (
    BodyActionCommand, BodyActionType, BodyCommandAlreadyClaimed, BodyCommandStatus,
    BodyExecutionReceipt, CapabilityKind, CapabilityState, ConnectionState,
    DeviceBodyBinding, DeviceBodyCapability, DeviceSnapshot, ExecutionReportState,
    HumanPresenceState, NetworkState, Orientation, ReceiptAcknowledgement,
    TransportDisposition,
)


class SQLiteDeviceBodyStore:
    """Durable implementation of DeviceBodyStorePort on the canonical DB."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._runtime = SQLiteAgentRuntimeStore(self.database_path)
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            # SQLiteAgentRuntimeStore is the single v4 -> v5 migration owner.
            self._runtime.initialize()
            with self._connection(write=True) as connection:
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(body_action_commands)"
                    ).fetchall()
                }
                if "target_companion_install_id" not in columns:
                    connection.execute(
                        "ALTER TABLE body_action_commands ADD COLUMN "
                        "target_companion_install_id TEXT"
                    )
                receipt_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(body_execution_receipts)"
                    ).fetchall()
                }
                for name, definition in (
                    ("companion_install_id", "TEXT"),
                    ("connection_epoch", "INTEGER"),
                ):
                    if name not in receipt_columns:
                        connection.execute(
                            f"ALTER TABLE body_execution_receipts ADD COLUMN {name} {definition}"
                        )
                snapshot_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(device_snapshots)"
                    ).fetchall()
                }
                for name, definition in (
                    ("input_read_back_sha256", "TEXT"),
                    ("input_read_back_length", "INTEGER"),
                    ("input_method", "TEXT"),
                ):
                    if name not in snapshot_columns:
                        connection.execute(
                            f"ALTER TABLE device_snapshots ADD COLUMN {name} {definition}"
                        )
            self._initialized = True

    def create_binding(self, binding: DeviceBodyBinding) -> DeviceBodyBinding:
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            session = connection.execute(
                "SELECT device_binding_id FROM agent_sessions WHERE session_id=?", (binding.session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"unknown AgentSession: {binding.session_id}")
            existing = connection.execute(
                "SELECT * FROM device_body_bindings WHERE session_id=?", (binding.session_id,)
            ).fetchone()
            if existing is not None:
                stored = _binding(existing)
                if stored.device_id == binding.device_id:
                    return stored
                raise ValueError("AgentSession is already bound to a different device")
            current_id = session["device_binding_id"]
            if current_id is not None and str(current_id).strip():
                raise RuntimeError("Session device binding reference is not backfilled")
            connection.execute(
                "INSERT INTO device_body_bindings("
                "binding_id, session_id, device_id, adapter_id, device_boot_id, connection_state, "
                "capability_revision, event_cursor, action_cursor, bound_at, updated_at, "
                "last_heartbeat_at, protocol_revision"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _binding_values(binding),
            )
            updated = connection.execute(
                "UPDATE agent_sessions SET device_binding_id=?, updated_at=? "
                "WHERE session_id=? AND device_binding_id IS NULL",
                (binding.id, binding.updated_at, binding.session_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("AgentSession device binding changed concurrently")
            return binding

    def load_binding(self, binding_id: str) -> DeviceBodyBinding:
        self.initialize()
        with self._connection() as connection:
            return self._require_binding(connection, binding_id)

    def binding_for_session(self, session_id: str) -> DeviceBodyBinding | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_body_bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            return _binding(row) if row is not None else None

    def bindings_for_device(self, device_id: str) -> tuple[DeviceBodyBinding, ...]:
        self.initialize()
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must not be blank")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM device_body_bindings WHERE device_id=? "
                "ORDER BY bound_at, binding_id",
                (device_id,),
            ).fetchall()
            return tuple(_binding(row) for row in rows)

    def update_binding_transport(
        self,
        binding_id: str,
        *,
        adapter_id: str,
        device_boot_id: str,
        connection_state: ConnectionState,
        at: str,
        heartbeat: bool = False,
    ) -> DeviceBodyBinding:
        """Refresh transport facts without changing Session ownership.

        A boot change starts a new device event cursor domain.  Action cursor
        remains binding-scoped so command identities cannot be reused after a
        reconnect or reboot.
        """

        if not isinstance(adapter_id, str) or not adapter_id.strip():
            raise ValueError("adapter_id must not be blank")
        if not isinstance(device_boot_id, str) or not device_boot_id.strip():
            raise ValueError("device_boot_id must not be blank")
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            current = self._require_binding(connection, binding_id)
            event_cursor = (
                current.event_cursor
                if current.device_boot_id == device_boot_id
                else 0
            )
            connection.execute(
                "UPDATE device_body_bindings SET adapter_id=?, device_boot_id=?, "
                "connection_state=?, event_cursor=?, updated_at=?, last_heartbeat_at=? "
                "WHERE binding_id=?",
                (
                    adapter_id,
                    device_boot_id,
                    connection_state.value,
                    event_cursor,
                    at,
                    at if heartbeat else current.last_heartbeat_at,
                    binding_id,
                ),
            )
            return self._require_binding(connection, binding_id)

    def advance_event_cursor(
        self, binding_id: str, *, device_boot_id: str, cursor: int, at: str
    ) -> DeviceBodyBinding:
        self.initialize()
        if cursor < 0:
            raise ValueError("event cursor must not be negative")
        with self._lock, self._connection(write=True) as connection:
            current = self._require_binding(connection, binding_id)
            if current.device_boot_id != device_boot_id:
                raise ValueError("event cursor boot does not match binding")
            if cursor < current.event_cursor:
                return current
            connection.execute(
                "UPDATE device_body_bindings SET event_cursor=?, updated_at=? "
                "WHERE binding_id=?",
                (cursor, at, binding_id),
            )
            return self._require_binding(connection, binding_id)

    def replace_capability_revision(
        self, binding_id: str, capabilities: tuple[DeviceBodyCapability, ...]
    ) -> tuple[DeviceBodyCapability, ...]:
        self.initialize()
        if not isinstance(capabilities, tuple) or not capabilities:
            raise ValueError("capability revision must be a non-empty tuple")
        with self._lock, self._connection(write=True) as connection:
            binding = self._require_binding(connection, binding_id)
            revisions = {item.revision for item in capabilities}
            if len(revisions) != 1:
                raise ValueError("capability replacement requires one revision")
            revision = revisions.pop()
            identity = (binding.id, binding.device_id, binding.device_boot_id)
            if any((item.binding_id, item.device_id, item.device_boot_id) != identity for item in capabilities):
                raise ValueError("capability identity does not match binding")
            kinds = tuple(item.kind for item in capabilities)
            if len(set(kinds)) != len(kinds) or set(kinds) != set(CapabilityKind):
                raise ValueError("capability revision must contain every capability exactly once")
            existing_rows = connection.execute(
                "SELECT * FROM device_body_capabilities WHERE binding_id=? AND revision=? "
                "ORDER BY capability_kind", (binding_id, revision),
            ).fetchall()
            if existing_rows:
                existing = tuple(_capability(row) for row in existing_rows)
                if _capability_set_digest(existing) == _capability_set_digest(capabilities):
                    return existing
                raise ValueError("capability revision already exists with different payload")
            if revision != binding.capability_revision + 1:
                raise ValueError("capability revision must advance by one")
            for item in capabilities:
                connection.execute(
                    "INSERT INTO device_body_capabilities("
                    "capability_id, binding_id, device_id, device_boot_id, revision, capability_kind, "
                    "capability_state, reason_code, evidence_ref, observed_at, protocol_revision, payload_digest"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _capability_values(item),
                )
            updated_at = max(item.observed_at for item in capabilities)
            connection.execute(
                "UPDATE device_body_bindings SET capability_revision=?, updated_at=? WHERE binding_id=?",
                (revision, updated_at, binding_id),
            )
            return tuple(sorted(capabilities, key=lambda item: item.kind.value))

    def capabilities_at_revision(self, binding_id: str, revision: int) -> tuple[DeviceBodyCapability, ...]:
        self.initialize()
        with self._connection() as connection:
            self._require_binding(connection, binding_id)
            rows = connection.execute(
                "SELECT * FROM device_body_capabilities WHERE binding_id=? AND revision=? "
                "ORDER BY capability_kind", (binding_id, revision),
            ).fetchall()
            return tuple(_capability(row) for row in rows)

    def persist_snapshot(self, snapshot: DeviceSnapshot) -> DeviceSnapshot:
        self.initialize()
        digest = _snapshot_digest(snapshot)
        with self._lock, self._connection(write=True) as connection:
            binding = self._require_binding(connection, snapshot.binding_id)
            _ensure_binding_identity(binding, snapshot.device_id, snapshot.device_boot_id)
            existing = connection.execute(
                "SELECT * FROM device_snapshots WHERE snapshot_id=? OR "
                "(binding_id=? AND capture_request_id=?)",
                (snapshot.id, snapshot.binding_id, snapshot.capture_request_id),
            ).fetchone()
            if existing is not None:
                stored = _snapshot(existing)
                if _snapshot_digest(stored) == digest:
                    return stored
                raise ValueError("snapshot idempotency identity has different payload")
            previous = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM device_snapshots WHERE binding_id=?",
                (snapshot.binding_id,),
            ).fetchone()
            if snapshot.sequence != int(previous["sequence"]) + 1:
                raise ValueError("snapshot sequence must advance by one")
            connection.execute(
                "INSERT INTO device_snapshots("
                "snapshot_id, binding_id, device_id, device_boot_id, capture_request_id, sequence, "
                "foreground_package, foreground_activity, screen_on, locked, network_state, orientation, "
                "human_presence, requested_at, capture_started_at, capture_completed_at, received_at, observed_at, "
                "caused_by_command_id, screenshot_ref, accessibility_tree_ref, input_read_back_sha256, "
                "input_read_back_length, input_method, protocol_revision, payload_digest"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _snapshot_values(snapshot, digest),
            )
            return snapshot

    def load_snapshot(self, snapshot_id: str) -> DeviceSnapshot:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown DeviceSnapshot: {snapshot_id}")
            return _snapshot(row)

    def latest_snapshot(self, binding_id: str) -> DeviceSnapshot | None:
        self.initialize()
        with self._connection() as connection:
            self._require_binding(connection, binding_id)
            row = connection.execute(
                "SELECT * FROM device_snapshots WHERE binding_id=? ORDER BY sequence DESC LIMIT 1",
                (binding_id,),
            ).fetchone()
            return _snapshot(row) if row is not None else None

    def create_command(self, command: BodyActionCommand) -> BodyActionCommand:
        self.initialize()
        digest = command.idempotency_digest
        with self._lock, self._connection(write=True) as connection:
            binding = self._require_binding(connection, command.binding_id)
            if binding.session_id != command.session_id:
                raise ValueError("command Session does not own DeviceBody binding")
            _ensure_binding_identity(binding, command.device_id, command.device_boot_id)
            existing = connection.execute(
                "SELECT * FROM body_action_commands WHERE command_id=? OR kernel_action_id=? OR payload_digest=?",
                (command.id, command.kernel_action_id, digest),
            ).fetchone()
            if existing is not None:
                stored = _command(existing)
                if stored.idempotency_digest == digest:
                    return stored
                raise ValueError("Kernel action command identity has different payload")
            if command.status is not BodyCommandStatus.PREPARED:
                raise ValueError("new BodyActionCommand must start PREPARED")
            if command.action_cursor != binding.action_cursor + 1:
                raise ValueError("command action_cursor must advance binding cursor by one")
            connection.execute(
                "INSERT INTO body_action_commands("
                "command_id, binding_id, session_id, device_id, device_boot_id, kernel_action_id, action_cursor, "
                "action_type, parameters_json, expected_state_json, required_capabilities_json, status, issued_at, "
                "target_companion_install_id, slice_id, dispatched_at, acknowledged_at, settled_at, "
                "protocol_revision, payload_digest"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _command_values(command, digest),
            )
            connection.execute(
                "UPDATE device_body_bindings SET action_cursor=?, updated_at=? WHERE binding_id=?",
                (command.action_cursor, command.issued_at, command.binding_id),
            )
            return command

    def load_command_for_kernel_action(self, kernel_action_id: str) -> BodyActionCommand | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM body_action_commands WHERE kernel_action_id=?", (kernel_action_id,)
            ).fetchone()
            return _command(row) if row is not None else None

    def load_command(self, command_id: str) -> BodyActionCommand:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM body_action_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown BodyActionCommand: {command_id}")
            return _command(row)

    def inspect_execution(self, kernel_action_id: str) -> tuple[BodyActionCommand, BodyExecutionReceipt | None] | None:
        self.initialize()
        with self._connection() as connection:
            command_row = connection.execute(
                "SELECT * FROM body_action_commands WHERE kernel_action_id=?", (kernel_action_id,)
            ).fetchone()
            if command_row is None:
                return None
            receipt_row = connection.execute(
                "SELECT * FROM body_execution_receipts WHERE command_id=?", (command_row["command_id"],)
            ).fetchone()
            return _command(command_row), _receipt(receipt_row) if receipt_row is not None else None

    def commands_needing_reconciliation(self, *, after_action_cursor: int, limit: int) -> tuple[BodyActionCommand, ...]:
        self.initialize()
        if after_action_cursor < 0 or limit < 1:
            raise ValueError("reconciliation cursor must be non-negative and limit positive")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM body_action_commands "
                "WHERE action_cursor>? AND status IN ('DISPATCHING', 'ACKNOWLEDGED') "
                "ORDER BY action_cursor, command_id LIMIT ?",
                (after_action_cursor, limit),
            ).fetchall()
            return tuple(_command(row) for row in rows)

    def commands_for_binding_reconciliation(
        self, binding_id: str, *, after_action_cursor: int, limit: int
    ) -> tuple[BodyActionCommand, ...]:
        """Return claims for exactly one binding; cursors are not global."""

        self.initialize()
        if after_action_cursor < 0 or limit < 1:
            raise ValueError("reconciliation cursor must be non-negative and limit positive")
        with self._connection() as connection:
            self._require_binding(connection, binding_id)
            rows = connection.execute(
                "SELECT * FROM body_action_commands WHERE binding_id=? "
                "AND action_cursor>? AND status IN ('DISPATCHING', 'ACKNOWLEDGED') "
                "ORDER BY action_cursor, command_id LIMIT ?",
                (binding_id, after_action_cursor, limit),
            ).fetchall()
            return tuple(_command(row) for row in rows)

    def claim_command(self, command_id: str, *, at: str) -> BodyActionCommand:
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM body_action_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown BodyActionCommand: {command_id}")
            command = _command(row)
            claimed = command.claim_for_dispatch(at=at)
            updated = connection.execute(
                "UPDATE body_action_commands SET status=?, dispatched_at=? "
                "WHERE command_id=? AND status='PREPARED'",
                (claimed.status.value, claimed.dispatched_at, command_id),
            )
            if updated.rowcount != 1:
                raise BodyCommandAlreadyClaimed(
                    f"Kernel action {command.kernel_action_id} is already claimed"
                )
            return claimed

    def acknowledge_command(
        self, command_id: str, *, kernel_action_id: str, at: str
    ) -> BodyActionCommand:
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM body_action_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown BodyActionCommand: {command_id}")
            command = _command(row)
            if command.kernel_action_id != kernel_action_id:
                raise ValueError("command ACK kernel_action_id does not match")
            if command.status is BodyCommandStatus.DISPATCHING:
                acknowledged = command.acknowledge(at=at)
                connection.execute(
                    "UPDATE body_action_commands SET status=?, acknowledged_at=? "
                    "WHERE command_id=? AND status='DISPATCHING'",
                    (acknowledged.status.value, acknowledged.acknowledged_at, command_id),
                )
                return acknowledged
            if command.status in {
                BodyCommandStatus.ACKNOWLEDGED,
                BodyCommandStatus.SETTLED,
                BodyCommandStatus.REJECTED,
                BodyCommandStatus.UNCERTAIN,
            }:
                return command
            raise ValueError("only a dispatched Body command can be acknowledged")

    def claim_companion_install(
        self, command_id: str, *, companion_install_id: str
    ) -> BodyActionCommand:
        """Bind the first command offer to one Android installation journal."""

        if not isinstance(companion_install_id, str) or not companion_install_id.strip():
            raise ValueError("companion_install_id must not be blank")
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM body_action_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown BodyActionCommand: {command_id}")
            command = _command(row)
            if command.target_companion_install_id is not None:
                if command.target_companion_install_id != companion_install_id:
                    raise ValueError(
                        "Body command is already bound to another Companion installation"
                )
                return command
            if command.status is not BodyCommandStatus.DISPATCHING:
                raise ValueError(
                    "only a dispatching Body command can claim a Companion installation"
                )
            claimed = connection.execute(
                "UPDATE body_action_commands SET target_companion_install_id=? "
                "WHERE command_id=? AND target_companion_install_id IS NULL "
                "AND status='DISPATCHING'",
                (companion_install_id, command_id),
            )
            if claimed.rowcount != 1:
                raise ValueError("Body command installation claim changed concurrently")
            refreshed = connection.execute(
                "SELECT * FROM body_action_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            return _command(refreshed)

    def record_receipt(self, receipt: BodyExecutionReceipt) -> BodyExecutionReceipt:
        self.initialize()
        digest = _receipt_digest(receipt)
        with self._lock, self._connection(write=True) as connection:
            command_row = connection.execute(
                "SELECT * FROM body_action_commands WHERE command_id=?", (receipt.command_id,)
            ).fetchone()
            if command_row is None:
                raise KeyError(f"unknown BodyActionCommand: {receipt.command_id}")
            command = _command(command_row)
            receipt.ensure_matches(command)
            existing = connection.execute(
                "SELECT * FROM body_execution_receipts WHERE receipt_id=? OR command_id=? "
                "OR (adapter_id=? AND source_receipt_id=?) OR payload_digest=?",
                (receipt.id, receipt.command_id, receipt.adapter_id, receipt.source_receipt_id, digest),
            ).fetchone()
            if existing is not None:
                stored = _receipt(existing)
                if _receipt_digest(stored) == digest:
                    return stored
                raise ValueError("execution receipt idempotency identity has different payload")
            settled = command.settle_from_receipt(receipt)
            connection.execute(
                "INSERT INTO body_execution_receipts("
                "receipt_id, source_receipt_id, command_id, kernel_action_id, binding_id, device_id, device_boot_id, "
                "adapter_id, acknowledgement, transport, execution, started_at, finished_at, received_at, retryable, "
                "adapter_code, reason_code, evidence_ref, companion_install_id, connection_epoch, "
                "protocol_revision, payload_digest"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _receipt_values(receipt, digest),
            )
            connection.execute(
                "UPDATE body_action_commands SET status=?, acknowledged_at=?, settled_at=? WHERE command_id=?",
                (settled.status.value, settled.acknowledged_at, settled.settled_at, command.id),
            )
            return receipt

    def reconcile_receipt(self, receipt: BodyExecutionReceipt) -> BodyExecutionReceipt:
        """Idempotently reconcile a late Companion result into one receipt row.

        Only an UNKNOWN execution report may be clarified.  A reported or
        transport-rejected receipt is terminal and immutable.  This preserves
        the one-receipt authority while allowing reconnect to supply a result
        that was durably retained by Android after the PC timed out.
        """

        self.initialize()
        digest = _receipt_digest(receipt)
        with self._lock, self._connection(write=True) as connection:
            command_row = connection.execute(
                "SELECT * FROM body_action_commands WHERE command_id=?",
                (receipt.command_id,),
            ).fetchone()
            if command_row is None:
                raise KeyError(f"unknown BodyActionCommand: {receipt.command_id}")
            command = _command(command_row)
            receipt.ensure_matches(command)
            existing_row = connection.execute(
                "SELECT * FROM body_execution_receipts WHERE command_id=?",
                (receipt.command_id,),
            ).fetchone()
            if existing_row is None:
                # Keep insertion and command settlement in this implementation's
                # existing, fully validated path.
                connection.rollback()
                return self.record_receipt(receipt)
            existing = _receipt(existing_row)
            if _receipt_digest(existing) == digest:
                return existing
            if (
                existing.execution is not ExecutionReportState.UNKNOWN
                or existing.transport is TransportDisposition.REJECTED
            ):
                raise ValueError("terminal execution receipt cannot be replaced")
            if (
                existing.id != receipt.id
                or existing.source_receipt_id != receipt.source_receipt_id
                or existing.adapter_id != receipt.adapter_id
                or (
                    existing.companion_install_id is not None
                    and existing.companion_install_id != receipt.companion_install_id
                )
                or (
                    existing.connection_epoch is not None
                    and existing.connection_epoch != receipt.connection_epoch
                )
            ):
                raise ValueError("receipt reconciliation identity has changed")
            if receipt.transport is TransportDisposition.REJECTED:
                status = BodyCommandStatus.REJECTED
            elif receipt.execution is ExecutionReportState.UNKNOWN:
                status = BodyCommandStatus.UNCERTAIN
            else:
                status = BodyCommandStatus.SETTLED
            connection.execute(
                "UPDATE body_execution_receipts SET acknowledgement=?, transport=?, execution=?, "
                "started_at=?, finished_at=?, received_at=?, retryable=?, adapter_code=?, "
                "reason_code=?, evidence_ref=?, companion_install_id=?, connection_epoch=?, "
                "protocol_revision=?, payload_digest=? "
                "WHERE command_id=?",
                (
                    receipt.acknowledgement.value,
                    receipt.transport.value,
                    receipt.execution.value,
                    receipt.started_at,
                    receipt.finished_at,
                    receipt.received_at,
                    int(receipt.retryable),
                    receipt.adapter_code,
                    receipt.reason_code,
                    receipt.evidence_ref,
                    receipt.companion_install_id,
                    receipt.connection_epoch,
                    receipt.protocol_revision,
                    digest,
                    receipt.command_id,
                ),
            )
            connection.execute(
                "UPDATE body_action_commands SET status=?, acknowledged_at=?, settled_at=? "
                "WHERE command_id=?",
                (
                    status.value,
                    receipt.received_at
                    if receipt.acknowledgement is ReceiptAcknowledgement.ACKNOWLEDGED
                    else command.acknowledged_at,
                    receipt.received_at,
                    receipt.command_id,
                ),
            )
            return receipt

    def load_receipt(self, command_id: str) -> BodyExecutionReceipt | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM body_execution_receipts WHERE command_id=?", (command_id,)
            ).fetchone()
            return _receipt(row) if row is not None else None

    def _require_binding(self, connection: sqlite3.Connection, binding_id: str) -> DeviceBodyBinding:
        row = connection.execute(
            "SELECT * FROM device_body_bindings WHERE binding_id=?", (binding_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown DeviceBodyBinding: {binding_id}")
        return _binding(row)

    def _connection(self, *, write: bool = False) -> _ConnectionContext:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        return _ConnectionContext(connection)


class _ConnectionContext:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        if kind is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()


def _binding_values(value: DeviceBodyBinding) -> tuple[Any, ...]:
    return (value.id, value.session_id, value.device_id, value.adapter_id, value.device_boot_id,
            value.connection_state.value, value.capability_revision, value.event_cursor, value.action_cursor,
            value.bound_at, value.updated_at, value.last_heartbeat_at, value.protocol_revision)


def _binding(row: sqlite3.Row) -> DeviceBodyBinding:
    return DeviceBodyBinding(id=str(row["binding_id"]), session_id=str(row["session_id"]),
        device_id=str(row["device_id"]), adapter_id=str(row["adapter_id"]),
        device_boot_id=str(row["device_boot_id"]), connection_state=ConnectionState(str(row["connection_state"])),
        capability_revision=int(row["capability_revision"]), event_cursor=int(row["event_cursor"]),
        action_cursor=int(row["action_cursor"]), bound_at=str(row["bound_at"]), updated_at=str(row["updated_at"]),
        last_heartbeat_at=row["last_heartbeat_at"], protocol_revision=int(row["protocol_revision"]))


def _capability_values(value: DeviceBodyCapability) -> tuple[Any, ...]:
    return (value.id, value.binding_id, value.device_id, value.device_boot_id, value.revision, value.kind.value,
            value.state.value, value.reason_code, value.evidence_ref, value.observed_at, value.protocol_revision,
            _capability_digest(value))


def _capability(row: sqlite3.Row) -> DeviceBodyCapability:
    return DeviceBodyCapability(id=str(row["capability_id"]), binding_id=str(row["binding_id"]),
        device_id=str(row["device_id"]), device_boot_id=str(row["device_boot_id"]), revision=int(row["revision"]),
        kind=CapabilityKind(str(row["capability_kind"])), state=CapabilityState(str(row["capability_state"])),
        reason_code=row["reason_code"], evidence_ref=row["evidence_ref"], observed_at=str(row["observed_at"]),
        protocol_revision=int(row["protocol_revision"]))


def _snapshot_values(value: DeviceSnapshot, digest: str) -> tuple[Any, ...]:
    return (value.id, value.binding_id, value.device_id, value.device_boot_id, value.capture_request_id, value.sequence,
            value.foreground_package, value.foreground_activity, int(value.screen_on), int(value.locked),
            value.network_state.value, value.orientation.value, value.human_presence.value, value.requested_at,
            value.capture_started_at, value.capture_completed_at, value.received_at, value.observed_at,
            value.caused_by_command_id, value.screenshot_ref, value.accessibility_tree_ref,
            value.input_read_back_sha256, value.input_read_back_length, value.input_method,
            value.protocol_revision, digest)


def _snapshot(row: sqlite3.Row) -> DeviceSnapshot:
    return DeviceSnapshot(id=str(row["snapshot_id"]), binding_id=str(row["binding_id"]),
        device_id=str(row["device_id"]), device_boot_id=str(row["device_boot_id"]),
        capture_request_id=str(row["capture_request_id"]), sequence=int(row["sequence"]),
        foreground_package=row["foreground_package"], foreground_activity=row["foreground_activity"],
        screen_on=bool(row["screen_on"]), locked=bool(row["locked"]),
        network_state=NetworkState(str(row["network_state"])), orientation=Orientation(str(row["orientation"])),
        human_presence=HumanPresenceState(str(row["human_presence"])), requested_at=str(row["requested_at"]),
        capture_started_at=str(row["capture_started_at"]), capture_completed_at=str(row["capture_completed_at"]),
        received_at=str(row["received_at"]), observed_at=str(row["observed_at"]),
        caused_by_command_id=row["caused_by_command_id"], screenshot_ref=row["screenshot_ref"],
        accessibility_tree_ref=row["accessibility_tree_ref"],
        input_read_back_sha256=row["input_read_back_sha256"],
        input_read_back_length=(
            int(row["input_read_back_length"])
            if row["input_read_back_length"] is not None
            else None
        ),
        input_method=row["input_method"], protocol_revision=int(row["protocol_revision"]))


def _command_values(value: BodyActionCommand, digest: str) -> tuple[Any, ...]:
    return (value.id, value.binding_id, value.session_id, value.device_id, value.device_boot_id, value.kernel_action_id,
            value.action_cursor, value.action_type.value, _json(value.parameters), _json(value.expected_state),
            _json([item.value for item in value.required_capabilities]), value.status.value, value.issued_at,
            value.target_companion_install_id, value.slice_id, value.dispatched_at, value.acknowledged_at,
            value.settled_at, value.protocol_revision, digest)


def _command(row: sqlite3.Row) -> BodyActionCommand:
    command = BodyActionCommand(id=str(row["command_id"]), binding_id=str(row["binding_id"]),
        session_id=str(row["session_id"]), device_id=str(row["device_id"]), device_boot_id=str(row["device_boot_id"]),
        kernel_action_id=str(row["kernel_action_id"]), action_cursor=int(row["action_cursor"]),
        action_type=BodyActionType(str(row["action_type"])), parameters=json.loads(str(row["parameters_json"])),
        expected_state=json.loads(str(row["expected_state_json"])),
        required_capabilities=tuple(CapabilityKind(item) for item in json.loads(str(row["required_capabilities_json"]))),
        status=BodyCommandStatus(str(row["status"])), issued_at=str(row["issued_at"]),
        target_companion_install_id=row["target_companion_install_id"], slice_id=row["slice_id"],
        dispatched_at=row["dispatched_at"], acknowledged_at=row["acknowledged_at"], settled_at=row["settled_at"],
        protocol_revision=int(row["protocol_revision"]))
    if str(row["payload_digest"]) != command.idempotency_digest:
        raise ValueError("BodyActionCommand payload digest does not match row content")
    return command


def _receipt_values(value: BodyExecutionReceipt, digest: str) -> tuple[Any, ...]:
    return (value.id, value.source_receipt_id, value.command_id, value.kernel_action_id, value.binding_id, value.device_id,
            value.device_boot_id, value.adapter_id, value.acknowledgement.value, value.transport.value, value.execution.value,
            value.started_at, value.finished_at, value.received_at, int(value.retryable), value.adapter_code, value.reason_code,
            value.evidence_ref, value.companion_install_id, value.connection_epoch,
            value.protocol_revision, digest)


def _receipt(row: sqlite3.Row) -> BodyExecutionReceipt:
    receipt = BodyExecutionReceipt(id=str(row["receipt_id"]), source_receipt_id=str(row["source_receipt_id"]),
        command_id=str(row["command_id"]), kernel_action_id=str(row["kernel_action_id"]),
        binding_id=str(row["binding_id"]), device_id=str(row["device_id"]), device_boot_id=str(row["device_boot_id"]),
        adapter_id=str(row["adapter_id"]), acknowledgement=ReceiptAcknowledgement(str(row["acknowledgement"])),
        transport=TransportDisposition(str(row["transport"])), execution=ExecutionReportState(str(row["execution"])),
        started_at=str(row["started_at"]), finished_at=str(row["finished_at"]), received_at=str(row["received_at"]),
        retryable=bool(row["retryable"]), adapter_code=row["adapter_code"], reason_code=row["reason_code"],
        evidence_ref=row["evidence_ref"], companion_install_id=row["companion_install_id"],
        connection_epoch=(
            int(row["connection_epoch"])
            if row["connection_epoch"] is not None
            else None
        ), protocol_revision=int(row["protocol_revision"]))
    stored_digest = str(row["payload_digest"])
    if stored_digest == _receipt_digest(receipt):
        return receipt
    if (
        receipt.companion_install_id is None
        and receipt.connection_epoch is None
        and stored_digest == _legacy_receipt_digest_without_install(receipt)
    ):
        return receipt
    raise ValueError("BodyExecutionReceipt payload digest does not match row content")


def _ensure_binding_identity(binding: DeviceBodyBinding, device_id: str, device_boot_id: str) -> None:
    if (binding.device_id, binding.device_boot_id) != (device_id, device_boot_id):
        raise ValueError("DeviceBody fact does not match bound device identity")


def _capability_digest(value: DeviceBodyCapability) -> str:
    return _digest(asdict(value))


def _capability_set_digest(values: tuple[DeviceBodyCapability, ...]) -> str:
    return _digest([asdict(value) for value in sorted(values, key=lambda item: item.kind.value)])


def _snapshot_digest(value: DeviceSnapshot) -> str:
    return _digest(asdict(value))


def _receipt_digest(value: BodyExecutionReceipt) -> str:
    return _digest(asdict(value))


def _legacy_receipt_digest_without_install(value: BodyExecutionReceipt) -> str:
    payload = asdict(value)
    payload.pop("companion_install_id")
    payload.pop("connection_epoch")
    return _digest(payload)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value)!r}")
