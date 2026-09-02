"""R4 DeviceBody contracts.

This package deliberately contains only transport-neutral domain objects and
ports.  RuntimeKernel remains the authority for Action persistence and
verification; a DeviceBody receipt only describes what the device transport
reported.
"""

from .adb_adapter import (
    AdbDeviceBodyAdapter,
    AdbDeviceBodyError,
    AdbPackageCatalog,
    ResolvedAndroidPackage,
)
from .domain import (
    BodyActionCommand,
    BodyActionType,
    BodyCommandStatus,
    BodyEvent,
    BodyEventType,
    BodyExecutionReceipt,
    CapabilityKind,
    CapabilityState,
    ConnectionState,
    DeviceBodyBinding,
    DeviceBodyCapability,
    DeviceSnapshot,
    DEVICE_BODY_PROTOCOL_REVISION,
    ExecutionReportState,
    HumanPresenceState,
    NetworkState,
    Orientation,
    ReceiptAcknowledgement,
    TransportDisposition,
)

__all__ = [
    "AdbDeviceBodyAdapter",
    "AdbDeviceBodyError",
    "AdbPackageCatalog",
    "BodyActionCommand",
    "BodyActionType",
    "BodyCommandStatus",
    "BodyEvent",
    "BodyEventType",
    "BodyExecutionReceipt",
    "CapabilityKind",
    "CapabilityState",
    "ConnectionState",
    "DeviceBodyBinding",
    "DeviceBodyCapability",
    "DeviceSnapshot",
    "DEVICE_BODY_PROTOCOL_REVISION",
    "ExecutionReportState",
    "HumanPresenceState",
    "NetworkState",
    "Orientation",
    "ReceiptAcknowledgement",
    "ResolvedAndroidPackage",
    "TransportDisposition",
]
