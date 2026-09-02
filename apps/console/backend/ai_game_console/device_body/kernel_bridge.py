"""Narrow R4 bridge between RuntimeKernel and the DeviceBody contract.

The two ledgers deliberately live behind different stores.  This module makes
their stable join key explicit (``kernel_action_id``) and never promotes a
receipt into a verification verdict.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Callable, Mapping
from uuid import uuid4

from ..runtime_kernel.action import Action, ActionType, ExecutionError
from ..runtime_kernel.executor import ActionExecutionResult
from .domain import (
    BodyActionCommand,
    BodyActionType,
    BodyCommandStatus,
    BodyEvent,
    BodyEventType,
    BodyExecutionReceipt,
    CapabilityKind,
    DeviceBodyBinding,
    DeviceSnapshot,
    ExecutionReportState,
    TransportDisposition,
)
from .ports import BodyEventInboxPort, DeviceBodyAdapterPort, DeviceBodyStorePort


class BodyCommandNeedsReconciliation(RuntimeError):
    """A previous attempt may have reached the device, so it must not replay."""


@dataclass(frozen=True, slots=True)
class FreshBodySnapshot:
    """A snapshot that passed correlation checks, not a success assertion."""

    command: BodyActionCommand
    receipt: BodyExecutionReceipt
    snapshot: DeviceSnapshot

    def as_kernel_correlation(self):
        """Build the opaque value passed to ``capture_observation``.

        Local import avoids making the transport-neutral DeviceBody package
        depend on RuntimeKernel at module import time.
        """

        from ..runtime_kernel.observation import BodySnapshotCorrelation

        return BodySnapshotCorrelation(
            snapshot_id=self.snapshot.id,
            binding_id=self.snapshot.binding_id,
            device_boot_id=self.snapshot.device_boot_id,
            capture_request_id=self.snapshot.capture_request_id,
            sequence=self.snapshot.sequence,
            caused_by_body_command_id=self.command.id,
        )


@dataclass(frozen=True, slots=True)
class BodyVerification:
    """A deterministic R4 verifier result for a correlated fresh snapshot."""

    matched: bool
    reason: str


class KernelDeviceBodyBridge:
    """Create/claim/reconcile exactly one Body command for each Kernel Action.

    ``binding_resolver`` deliberately receives the Kernel task id.  The bridge
    does not infer Session ownership or scheduling state from a DeviceBody
    binding; the composition layer supplies that established relationship.
    """

    def __init__(
        self,
        *,
        store: DeviceBodyStorePort,
        adapter: DeviceBodyAdapterPort,
        binding_resolver: Callable[[str, str], DeviceBodyBinding],
        clock: Callable[[], str],
        id_factory: Callable[[], str] | None = None,
        action_cursor: Callable[[DeviceBodyBinding, Action], int] | None = None,
        event_inbox: BodyEventInboxPort | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._binding_resolver = binding_resolver
        self._clock = clock
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._action_cursor = action_cursor or (
            lambda binding, _action: binding.action_cursor + 1
        )
        self._event_inbox = event_inbox

    def dispatch(
        self, *, action: Action, task_id: str, device_id: str
    ) -> ActionExecutionResult:
        """Return an ActionExecution transport result without asserting success.

        The Kernel Action has already been persisted when this method is called.
        A pre-existing command is inspected first; only ``PREPARED`` can be
        claimed, therefore a restart cannot issue a second physical command.
        """

        command = self._store.load_command_for_kernel_action(action.id)
        if command is None:
            binding = self._binding_resolver(task_id, device_id)
            if binding.device_id != device_id:
                raise ValueError("DeviceBody binding does not match Kernel Task device")
            command = self._command_for(action=action, binding=binding)
            command = self._store.create_command(command)
        if command.kernel_action_id != action.id:
            raise ValueError("DeviceBody command does not match Kernel Action")
        if command.device_id != device_id:
            raise ValueError("DeviceBody command does not match Kernel Task device")

        existing = self._store.inspect_execution(action.id)
        if existing is not None and existing[1] is not None:
            return _result_from_receipt(existing[0], existing[1])
        if command.status is not BodyCommandStatus.PREPARED:
            raise BodyCommandNeedsReconciliation(
                f"Kernel action {action.id} is already {command.status.value}; "
                "inspect or reconcile it without replay"
            )

        # A pre-existing PREPARED command has not crossed the physical
        # boundary.  Resolving the current binding is therefore still required
        # immediately before its first claim.  DISPATCHING/ACKNOWLEDGED and
        # terminal commands returned above deliberately never touch a live
        # binding: restart recovery must reconcile their durable identity first.
        binding = self._binding_resolver(task_id, device_id)
        if binding.device_id != device_id or binding.id != command.binding_id:
            raise ValueError("DeviceBody binding does not match persisted command")
        claimed = self._store.claim_command(command.id, at=self._clock())
        execute_materialized = getattr(self._adapter, "execute_materialized", None)
        if (
            claimed.action_type is BodyActionType.INPUT_TEXT_UNICODE
            and callable(execute_materialized)
        ):
            # Plaintext exists only on the already-persisted Kernel Action and
            # this in-flight call.  Body/Companion journals retain ref/hash/
            # code-point length, never another plaintext copy.
            receipt = execute_materialized(
                claimed, text_material=_text(action.params, "text")
            )
        else:
            receipt = self._adapter.execute(claimed)
        receipt.ensure_matches(claimed)
        persisted = self._store.record_receipt(receipt)
        return _result_from_receipt(claimed, persisted)

    def capture_fresh_snapshot(self, *, kernel_action_id: str) -> FreshBodySnapshot:
        """Capture a normal post-receipt snapshot for a reported command."""

        return self._capture_post_receipt_snapshot(
            kernel_action_id=kernel_action_id,
            required_execution=ExecutionReportState.REPORTED,
            require_uncertain_command=False,
        )

    def capture_reconciliation_snapshot(
        self, *, kernel_action_id: str
    ) -> FreshBodySnapshot:
        """Observe an already-UNCERTAIN command without ever replaying it.

        An accepted transport receipt with ``UNKNOWN`` execution means the
        original physical action may or may not have reached the device.  The
        sole permissible next action is an independently captured, correlated
        snapshot after that receipt.  This seam deliberately does not call
        ``dispatch`` or ``claim_command`` and cannot turn uncertainty into a
        success assertion.
        """

        return self._capture_post_receipt_snapshot(
            kernel_action_id=kernel_action_id,
            required_execution=ExecutionReportState.UNKNOWN,
            require_uncertain_command=True,
        )

    def capture_snapshot_for_observation(
        self, *, kernel_action_id: str
    ) -> FreshBodySnapshot:
        """Select the only valid post-receipt observation seam.

        RuntimeKernel uses this selector rather than inferring receipt state
        from its transport-neutral ActionExecution projection.  ``UNKNOWN``
        is routed exclusively to reconciliation capture; ``REPORTED`` retains
        the ordinary verification path.
        """

        inspected = self._store.inspect_execution(kernel_action_id)
        if inspected is None or inspected[1] is None:
            raise BodyCommandNeedsReconciliation("Body receipt is not available")
        _command, receipt = inspected
        if receipt.execution is ExecutionReportState.UNKNOWN:
            return self.capture_reconciliation_snapshot(
                kernel_action_id=kernel_action_id
            )
        return self.capture_fresh_snapshot(kernel_action_id=kernel_action_id)

    def validate_verification_verdict(
        self, *, kernel_action_id: str, verdict: str
    ) -> None:
        """Fail closed when an UNKNOWN receipt reaches Kernel verification.

        A fresh reconciliation snapshot is useful evidence of present state,
        but it cannot establish that the original action succeeded.  Its
        Kernel verification is therefore restricted to ``UNCERTAIN``.  This
        check is intentionally independent of snapshot contents and remains
        durable across a RuntimeKernel restart because it re-reads the Body
        receipt ledger.
        """

        inspected = self._store.inspect_execution(kernel_action_id)
        if inspected is None or inspected[1] is None:
            raise BodyCommandNeedsReconciliation("Body receipt is not available")
        command, receipt = inspected
        if receipt.execution is ExecutionReportState.UNKNOWN:
            if command.status is not BodyCommandStatus.UNCERTAIN:
                raise ValueError("UNKNOWN receipt must have an UNCERTAIN Body command")
            if verdict != "UNCERTAIN":
                raise ValueError(
                    "UNKNOWN Body receipt may only commit an UNCERTAIN verification"
                )

    def verify_persisted_snapshot(
        self,
        *,
        kernel_action_id: str,
        snapshot_id: str,
        binding_id: str,
        device_boot_id: str,
        capture_request_id: str,
        sequence: int,
        caused_by_body_command_id: str,
        before_snapshot_id: str | None = None,
        before_binding_id: str | None = None,
        before_device_boot_id: str | None = None,
        before_capture_request_id: str | None = None,
        before_sequence: int | None = None,
    ) -> BodyVerification:
        """Verify one already-persisted, correlated snapshot without I/O to a device.

        This is intentionally narrower than ``capture_*_snapshot``.  It never
        dispatches or captures: the RuntimeKernel first persists its after
        Observation, then asks the bridge to re-load the durable command,
        receipt, and current snapshot that exact correlation names.  Requiring
        the named snapshot to still be the binding's latest snapshot prevents a
        caller from replaying an old Observation after later device state has
        superseded it.
        """

        inspected = self._store.inspect_execution(kernel_action_id)
        if inspected is None or inspected[1] is None:
            raise BodyCommandNeedsReconciliation("Body receipt is not available")
        command, receipt = inspected
        latest = self._store.latest_snapshot(command.binding_id)
        if latest is None:
            raise ValueError("Body verification requires a persisted snapshot")
        if (
            command.kernel_action_id != kernel_action_id
            or command.id != caused_by_body_command_id
            or command.binding_id != binding_id
            or command.device_boot_id != device_boot_id
            or latest.id != snapshot_id
            or latest.binding_id != binding_id
            or latest.device_boot_id != device_boot_id
            or latest.capture_request_id != capture_request_id
            or latest.sequence != sequence
            or latest.caused_by_command_id != caused_by_body_command_id
        ):
            raise ValueError("persisted Body snapshot does not match Kernel correlation")
        receipt.ensure_matches(command)
        if receipt.transport is not TransportDisposition.ACCEPTED:
            return BodyVerification(False, "receipt_transport_not_accepted")
        if receipt.execution is ExecutionReportState.UNKNOWN:
            if command.status is not BodyCommandStatus.UNCERTAIN:
                raise ValueError("UNKNOWN receipt must have an UNCERTAIN Body command")
            # Present device contents can document reconciliation, but cannot
            # prove the original physical command ran.  Do not call any
            # content verifier here: that would make UNKNOWN promotable.
            return BodyVerification(False, "receipt_execution_unknown")
        if command.status is not BodyCommandStatus.SETTLED:
            raise ValueError("REPORTED receipt must have a SETTLED Body command")
        if latest.requested_at < receipt.finished_at:
            raise ValueError("persisted Body snapshot predates the command receipt")
        before_snapshot: DeviceSnapshot | None = None
        if command.action_type is BodyActionType.SWIPE:
            if None in {
                before_snapshot_id,
                before_binding_id,
                before_device_boot_id,
                before_capture_request_id,
                before_sequence,
            }:
                raise ValueError("SWIPE verification requires a correlated before Body snapshot")
            try:
                before_snapshot = self._store.load_snapshot(str(before_snapshot_id))
            except (KeyError, ValueError) as error:
                raise ValueError("SWIPE before Body snapshot is unavailable") from error
            if (
                before_snapshot.id != before_snapshot_id
                or before_snapshot.binding_id != before_binding_id
                or before_snapshot.device_boot_id != before_device_boot_id
                or before_snapshot.capture_request_id != before_capture_request_id
                or before_snapshot.sequence != before_sequence
                or before_snapshot.binding_id != command.binding_id
                or before_snapshot.device_id != command.device_id
                or before_snapshot.device_boot_id != command.device_boot_id
            ):
                raise ValueError("SWIPE before Body snapshot does not match the Action binding")
        return self.verify_snapshot(
            FreshBodySnapshot(command=command, receipt=receipt, snapshot=latest),
            before_snapshot=before_snapshot,
        )

    def _capture_post_receipt_snapshot(
        self,
        *,
        kernel_action_id: str,
        required_execution: ExecutionReportState,
        require_uncertain_command: bool,
    ) -> FreshBodySnapshot:
        """Capture and persist a correlated snapshot for one receipt state."""

        inspected = self._store.inspect_execution(kernel_action_id)
        if inspected is None or inspected[1] is None:
            raise BodyCommandNeedsReconciliation("Body receipt is not available")
        command, receipt = inspected
        if (
            receipt.transport is not TransportDisposition.ACCEPTED
            or receipt.execution is not required_execution
        ):
            raise ValueError(
                "Body receipt execution state is not valid for this snapshot capture"
            )
        if require_uncertain_command and command.status is not BodyCommandStatus.UNCERTAIN:
            raise ValueError("UNKNOWN receipt must have an UNCERTAIN Body command")
        before = self._store.latest_snapshot(command.binding_id)
        binding = self._store.load_binding(command.binding_id)
        # Keep the after-observation on the transport that produced the
        # receipt.  A composite adapter may change preferred availability
        # between action dispatch and capture; switching here would make a
        # different transport claim correlation for the original command.
        capture_request_id = (
            f"kernel-action:{kernel_action_id}:capture:"
            f"{required_execution.value.lower()}"
        )
        capture_for_adapter = getattr(
            self._adapter, "capture_snapshot_for_adapter", None
        )
        if callable(capture_for_adapter):
            snapshot = capture_for_adapter(
                binding,
                adapter_id=receipt.adapter_id,
                capture_request_id=capture_request_id,
                caused_by_command_id=command.id,
            )
        else:
            snapshot = self._adapter.capture_snapshot(
                binding,
                capture_request_id=capture_request_id,
                caused_by_command_id=command.id,
            )
        # The adapter process is not the durable sequence authority.  In
        # particular, an ADB adapter restarts with an empty in-memory counter
        # while the binding's snapshot journal remains in SQLite.  Continue
        # from that journal so reconciliation after launcher restart cannot
        # reuse sequence 1 or fail before observing the current foreground.
        persisted_sequence = (before.sequence if before is not None else 0) + 1
        if snapshot.sequence != persisted_sequence:
            snapshot = replace(snapshot, sequence=persisted_sequence)
        persisted = self._store.persist_snapshot(snapshot)
        latest = self._store.latest_snapshot(command.binding_id)
        if latest is None or latest.id != persisted.id:
            raise ValueError("fresh snapshot is not the binding's latest persisted snapshot")
        previous_sequence = before.sequence if before is not None else 0
        if not persisted.is_fresh_for(command, receipt, previous_sequence=previous_sequence):
            raise ValueError("snapshot is not fresh for the Body command receipt")
        self._ingest_foreground_change(before=before, current=persisted)
        return FreshBodySnapshot(command=command, receipt=receipt, snapshot=persisted)

    def _ingest_foreground_change(
        self, *, before: DeviceSnapshot | None, current: DeviceSnapshot
    ) -> None:
        """Project a changed foreground fact through the sole EventInbox seam.

        The initial snapshot deliberately emits no change event.  A snapshot
        without a known current package cannot satisfy the frozen BodyEvent
        foreground invariant, so it remains an observation-only fact.
        """

        if (
            self._event_inbox is None
            or before is None
            or before.device_boot_id != current.device_boot_id
            or current.foreground_package is None
            or (before.foreground_package, before.foreground_activity)
            == (current.foreground_package, current.foreground_activity)
        ):
            return
        self._event_inbox.ingest(
            BodyEvent(
                id=self._id_factory(),
                binding_id=current.binding_id,
                device_id=current.device_id,
                device_boot_id=current.device_boot_id,
                source_event_id=(
                    f"foreground:{current.device_boot_id}:{current.sequence}"
                ),
                source_cursor=current.sequence,
                event_type=BodyEventType.FOREGROUND_CHANGED,
                occurred_at=current.observed_at,
                received_at=current.received_at,
                caused_by_command_id=current.caused_by_command_id,
                foreground_package=current.foreground_package,
                foreground_activity=current.foreground_activity,
            )
        )

    @staticmethod
    def verify_snapshot(
        fresh: FreshBodySnapshot, *, before_snapshot: DeviceSnapshot | None = None
    ) -> BodyVerification:
        """Evaluate deterministic device facts after freshness succeeds."""

        expected = fresh.command.expected_state
        snapshot = fresh.snapshot
        if fresh.command.action_type in {BodyActionType.OPEN_APP, BodyActionType.TAP}:
            package = expected["foreground_package"]
            if snapshot.foreground_package != package:
                if _is_oplus_two_pane_settings_result(fresh):
                    return BodyVerification(
                        True, "settings_root_opened_with_oplus_two_pane_foreground"
                    )
                return BodyVerification(False, "foreground_package_mismatch")
            activity = expected.get("foreground_activity")
            if activity is not None and snapshot.foreground_activity != activity:
                return BodyVerification(False, "foreground_activity_mismatch")
            return BodyVerification(True, "foreground_exact_match")
        if fresh.command.action_type is BodyActionType.SWIPE:
            package = expected["foreground_package"]
            if snapshot.foreground_package != package:
                return BodyVerification(False, "foreground_package_mismatch")
            activity = expected.get("foreground_activity")
            if activity is not None and snapshot.foreground_activity != activity:
                return BodyVerification(False, "foreground_activity_mismatch")
            if snapshot.accessibility_tree_ref is None:
                return BodyVerification(False, "accessibility_tree_unavailable")
            if before_snapshot is None:
                return BodyVerification(False, "accessibility_tree_before_snapshot_required")
            if (
                before_snapshot.binding_id != fresh.command.binding_id
                or before_snapshot.device_id != fresh.command.device_id
                or before_snapshot.device_boot_id != fresh.command.device_boot_id
            ):
                return BodyVerification(False, "accessibility_tree_before_binding_mismatch")
            if before_snapshot.accessibility_tree_ref is None:
                return BodyVerification(False, "accessibility_tree_before_unavailable")
            if (
                before_snapshot.accessibility_tree_ref
                != expected["accessibility_tree_changed_from"]
            ):
                return BodyVerification(
                    False, "accessibility_tree_before_reference_mismatch"
                )
            if (
                snapshot.accessibility_tree_ref
                == expected["accessibility_tree_changed_from"]
            ):
                return BodyVerification(False, "accessibility_tree_unchanged")
            return BodyVerification(True, "foreground_exact_and_accessibility_tree_changed")
        if fresh.command.action_type is BodyActionType.INPUT_TEXT_UNICODE:
            return KernelDeviceBodyBridge.verify_unicode_readback(fresh)
        return BodyVerification(False, "action_requires_action_specific_verifier")

    @staticmethod
    def verify_unicode_readback(fresh: FreshBodySnapshot) -> BodyVerification:
        """Compare only the independently captured snapshot digest and length."""

        if fresh.command.action_type is not BodyActionType.INPUT_TEXT_UNICODE:
            raise ValueError("Unicode read-back verifier requires INPUT_TEXT_UNICODE")
        expected = fresh.command.expected_state
        snapshot = fresh.snapshot
        if snapshot.input_read_back_sha256 is None:
            return BodyVerification(False, "unicode_readback_unavailable")
        if snapshot.input_read_back_sha256 != expected["read_back_text_sha256"]:
            return BodyVerification(False, "unicode_readback_hash_mismatch")
        if snapshot.input_read_back_length != expected["read_back_text_length"]:
            return BodyVerification(False, "unicode_readback_length_mismatch")
        return BodyVerification(True, "unicode_readback_match")

    def _command_for(
        self, *, action: Action, binding: DeviceBodyBinding
    ) -> BodyActionCommand:
        action_type, parameters, expected_state, capabilities = _body_action(action)
        return BodyActionCommand(
            id=self._id_factory(),
            binding_id=binding.id,
            session_id=binding.session_id,
            device_id=binding.device_id,
            device_boot_id=binding.device_boot_id,
            kernel_action_id=action.id,
            action_cursor=self._action_cursor(binding, action),
            action_type=action_type,
            parameters=parameters,
            expected_state=expected_state,
            required_capabilities=capabilities,
            status=BodyCommandStatus.PREPARED,
            issued_at=self._clock(),
        )


def _body_action(
    action: Action,
) -> tuple[BodyActionType, dict[str, object], dict[str, object], tuple[CapabilityKind, ...]]:
    """The sole explicit mapping from legacy Kernel Action to Body action."""

    params: Mapping[str, object] = action.params
    if action.type is ActionType.OPEN_APP:
        package = _text(params, "package")
        values: dict[str, object] = {"package": package}
        component = params.get("component")
        if component is not None:
            values["component"] = _text(params, "component")
        expected: dict[str, object] = {"foreground_package": package}
        activity = params.get("expected_activity")
        if activity is not None:
            expected["foreground_activity"] = _text(params, "expected_activity")
        return (
            BodyActionType.OPEN_APP,
            values,
            expected,
            (CapabilityKind.OPEN_APP, CapabilityKind.DEVICE_SNAPSHOT, CapabilityKind.FOREGROUND_APPLICATION),
        )
    if action.type is ActionType.TAP:
        expected = _foreground_expected(params, action_name="TAP")
        return (
            BodyActionType.TAP,
            _coordinates(params, ("x", "y")),
            expected,
            (
                CapabilityKind.TAP,
                CapabilityKind.DEVICE_SNAPSHOT,
                CapabilityKind.FOREGROUND_APPLICATION,
            ),
        )
    if action.type is ActionType.LONG_PRESS:
        values = _coordinates(params, ("x", "y"))
        values["duration_ms"] = int(params.get("duration_ms", 600))
        return BodyActionType.LONG_PRESS, values, {}, (CapabilityKind.LONG_PRESS,)
    if action.type is ActionType.SWIPE:
        values = _coordinates(params, ("start_x", "start_y", "end_x", "end_y"))
        values["duration_ms"] = int(params.get("duration_ms", 300))
        expected = _foreground_expected(params, action_name="SWIPE")
        changed_from = params.get("expected_accessibility_tree_changed_from")
        if not isinstance(changed_from, str) or len(changed_from) != 64 or any(
            character not in "0123456789abcdef" for character in changed_from
        ):
            raise ValueError(
                "Kernel Action expected_accessibility_tree_changed_from must be lowercase SHA-256"
            )
        expected["accessibility_tree_changed_from"] = changed_from
        return (
            BodyActionType.SWIPE,
            values,
            expected,
            (
                CapabilityKind.SWIPE,
                CapabilityKind.DEVICE_SNAPSHOT,
                CapabilityKind.FOREGROUND_APPLICATION,
                CapabilityKind.ACCESSIBILITY_TREE,
            ),
        )
    if action.type in {ActionType.INPUT_TEXT, ActionType.INPUT_TEXT_UNICODE}:
        text = _text(params, "text")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        values = {
            "text_ref": f"kernel-action:{action.id}:input",
            "text_sha256": digest,
            "text_length": len(text),
        }
        if params.get("target_hint") is not None:
            values["target_hint"] = _text(params, "target_hint")
        return (
            BodyActionType.INPUT_TEXT_UNICODE,
            values,
            {"read_back_text_sha256": digest, "read_back_text_length": len(text)},
            (CapabilityKind.INPUT_TEXT_UNICODE, CapabilityKind.TEXT_READ_BACK),
        )
    mapping = {
        ActionType.BACK: (BodyActionType.BACK, CapabilityKind.BACK),
        ActionType.HOME: (BodyActionType.HOME, CapabilityKind.HOME),
        ActionType.RECENTS: (BodyActionType.RECENTS, CapabilityKind.RECENTS),
        ActionType.CAPTURE_SNAPSHOT: (BodyActionType.CAPTURE_SNAPSHOT, CapabilityKind.CAPTURE_SNAPSHOT),
    }
    try:
        body_type, capability = mapping[action.type]
    except KeyError as error:
        raise ValueError(f"Kernel Action {action.type.value} has no R4 DeviceBody mapping") from error
    return body_type, {}, {}, (capability,)


def _result_from_receipt(
    command: BodyActionCommand, receipt: BodyExecutionReceipt
) -> ActionExecutionResult:
    receipt.ensure_matches(command)
    accepted = receipt.transport is TransportDisposition.ACCEPTED
    adapter_code = None
    if receipt.adapter_code is not None:
        try:
            adapter_code = int(receipt.adapter_code)
        except ValueError:
            adapter_code = None
    error = None
    if not accepted:
        error = ExecutionError(
            code=receipt.reason_code or "body_transport_rejected",
            message="DeviceBody transport rejected the persisted command",
            retryable=receipt.retryable,
        )
    return ActionExecutionResult(
        accepted=accepted,
        adapter_code=adapter_code,
        error=error,
        started_at=receipt.started_at,
        finished_at=receipt.finished_at,
        body_command_id=command.id,
        body_receipt_id=receipt.id,
    )


def _is_oplus_two_pane_settings_result(fresh: FreshBodySnapshot) -> bool:
    """Recognize one exact OEM projection of the Android Settings root.

    On this OPPO large-screen build, ``Settings.ACTION_SETTINGS`` launches the
    trusted ``com.android.settings`` homepage and Android ActivityEmbedding
    immediately focuses its read-only WLAN detail pane.  Accessibility then
    names that pane's package instead of the task's Settings root.  This is not
    a general package alias: it is accepted only when the same Companion
    command durably reports the exact Settings-root launch, the fresh snapshot
    is correlated to that command, and an accessibility tree from the OEM pane
    is available.  ADB receipts and arbitrary OEM packages remain rejected.
    """

    command = fresh.command
    receipt = fresh.receipt
    snapshot = fresh.snapshot
    settings_package = "com.android.settings"
    return (
        command.action_type is BodyActionType.OPEN_APP
        and dict(command.parameters) == {"package": settings_package}
        and dict(command.expected_state) == {"foreground_package": settings_package}
        and command.target_companion_install_id is not None
        and receipt.adapter_id == "android-companion-v1"
        and receipt.transport is TransportDisposition.ACCEPTED
        and receipt.execution is ExecutionReportState.REPORTED
        and receipt.adapter_code == "SUCCEEDED"
        and receipt.companion_install_id == command.target_companion_install_id
        and receipt.connection_epoch is not None
        and receipt.evidence_ref
        == f"ref=package-launch:{settings_package}:{command.id}"
        and snapshot.caused_by_command_id == command.id
        and snapshot.foreground_package == "com.oplus.wirelesssettings"
        and snapshot.foreground_activity
        in {"com.android.settings.SettingsActivity", "android.widget.FrameLayout"}
        and snapshot.accessibility_tree_ref is not None
    )


def _coordinates(params: Mapping[str, object], names: tuple[str, ...]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in names:
        value = params.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Kernel Action {name} must be a non-negative integer")
        result[name] = value
    return result


def _foreground_expected(
    params: Mapping[str, object], *, action_name: str
) -> dict[str, object]:
    """Read audit-only action metadata without sending it to the device."""

    package = params.get("expected_foreground_package")
    if not isinstance(package, str) or not package.strip():
        raise ValueError(
            f"Kernel Action {action_name} expected_foreground_package must be non-blank text"
        )
    expected: dict[str, object] = {"foreground_package": package}
    activity = params.get("expected_foreground_activity")
    if activity is not None:
        if not isinstance(activity, str) or not activity.strip():
            raise ValueError(
                f"Kernel Action {action_name} expected_foreground_activity must be non-blank text"
            )
        expected["foreground_activity"] = activity
    return expected


def _text(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Kernel Action {name} must be non-blank text")
    return value
