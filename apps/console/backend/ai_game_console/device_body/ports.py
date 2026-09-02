"""Ports for the R4 DeviceBody contract.

No port in this module exposes a Goal, Stage, Verification, or scheduling
operation.  The composition layer projects BodyEvent through EventInbox and
RuntimeKernel owns physical-action verification.
"""

from __future__ import annotations

from typing import Protocol

from .domain import (
    BodyActionCommand,
    BodyEvent,
    BodyExecutionReceipt,
    DeviceBodyBinding,
    DeviceBodyCapability,
    DeviceSnapshot,
)


class DeviceBodyStorePort(Protocol):
    """Durable agent-runtime side of binding, command and event facts."""

    def initialize(self) -> None: ...

    def create_binding(self, binding: DeviceBodyBinding) -> DeviceBodyBinding: ...

    def load_binding(self, binding_id: str) -> DeviceBodyBinding: ...

    def binding_for_session(self, session_id: str) -> DeviceBodyBinding | None: ...

    def bindings_for_device(self, device_id: str) -> tuple[DeviceBodyBinding, ...]: ...

    def update_binding_transport(
        self,
        binding_id: str,
        *,
        adapter_id: str,
        device_boot_id: str,
        connection_state: object,
        at: str,
        heartbeat: bool = False,
    ) -> DeviceBodyBinding: ...

    def advance_event_cursor(
        self, binding_id: str, *, device_boot_id: str, cursor: int, at: str
    ) -> DeviceBodyBinding: ...

    def replace_capability_revision(
        self, binding_id: str, capabilities: tuple[DeviceBodyCapability, ...]
    ) -> tuple[DeviceBodyCapability, ...]: ...

    def capabilities_at_revision(
        self, binding_id: str, revision: int
    ) -> tuple[DeviceBodyCapability, ...]: ...

    def persist_snapshot(self, snapshot: DeviceSnapshot) -> DeviceSnapshot: ...

    def load_snapshot(self, snapshot_id: str) -> DeviceSnapshot: ...

    def latest_snapshot(self, binding_id: str) -> DeviceSnapshot | None: ...

    def create_command(self, command: BodyActionCommand) -> BodyActionCommand: ...

    def load_command_for_kernel_action(self, kernel_action_id: str) -> BodyActionCommand | None: ...

    def load_command(self, command_id: str) -> BodyActionCommand: ...

    def inspect_execution(
        self, kernel_action_id: str
    ) -> tuple[BodyActionCommand, BodyExecutionReceipt | None] | None: ...

    def commands_needing_reconciliation(
        self, *, after_action_cursor: int, limit: int
    ) -> tuple[BodyActionCommand, ...]: ...

    def commands_for_binding_reconciliation(
        self, binding_id: str, *, after_action_cursor: int, limit: int
    ) -> tuple[BodyActionCommand, ...]: ...

    def claim_command(self, command_id: str, *, at: str) -> BodyActionCommand: ...

    def acknowledge_command(
        self, command_id: str, *, kernel_action_id: str, at: str
    ) -> BodyActionCommand: ...

    def claim_companion_install(
        self, command_id: str, *, companion_install_id: str
    ) -> BodyActionCommand: ...

    def record_receipt(self, receipt: BodyExecutionReceipt) -> BodyExecutionReceipt: ...

    def reconcile_receipt(self, receipt: BodyExecutionReceipt) -> BodyExecutionReceipt: ...

    def load_receipt(self, command_id: str) -> BodyExecutionReceipt | None: ...



class BodyEventInboxPort(Protocol):
    """The sole DeviceBody-to-EventInbox intake seam; it does not schedule."""

    def ingest(self, event: BodyEvent) -> tuple[BodyEvent, bool]: ...


class DeviceBodyAdapterPort(Protocol):
    """Physical adapter; results remain transport reports, not verdicts."""

    def discover_capabilities(
        self, binding: DeviceBodyBinding
    ) -> tuple[DeviceBodyCapability, ...]: ...

    def execute(self, command: BodyActionCommand) -> BodyExecutionReceipt: ...

    def capture_snapshot(
        self,
        binding: DeviceBodyBinding,
        *,
        capture_request_id: str,
        caused_by_command_id: str | None = None,
    ) -> DeviceSnapshot: ...
