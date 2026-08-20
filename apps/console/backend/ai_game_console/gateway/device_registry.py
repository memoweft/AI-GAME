"""Device registry port for the Gateway (contract §4).

The Gateway validates ``device_id`` through this port before creating a
Task. Week 3 wires the real implementation (ADB device discovery behind
the lease layer); the service layer only sees ``DeviceSummary``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    device_id: str
    status: str  # "AVAILABLE" | "IN_USE" | "UNAVAILABLE"


class DeviceRegistry(Protocol):
    def list_devices(self) -> tuple[DeviceSummary, ...]: ...

    def get_device(self, device_id: str) -> DeviceSummary | None: ...
