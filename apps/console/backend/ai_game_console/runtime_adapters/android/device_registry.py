"""ADB-backed implementation of the Gateway DeviceRegistry port (contract §4).

Serves ``list_devices`` / ``get_device`` from the read-only
``AdbTargetDiscovery`` (``adb devices -l`` only). Availability rules:

* ``IN_USE`` — the device id appears in the active-device overlay. The
  overlay hook is reserved for the lease wiring at cutover: the Kernel has
  no public lease-listing service, so the composition (not this adapter)
  decides which devices are actively leased. Default: no overlay.
* ``AVAILABLE`` — ADB reports the device as ``ready``.
* ``UNAVAILABLE`` — anything else (offline, unauthorized, unknown), and
  also the safe fallback for an unrecognized status string.

Contract §14 discipline: discovery failures yield an empty device list;
no vendor, ADB, or traceback detail may cross the API boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ...discovery import AdbTargetDiscovery
from ...domain import TargetStatus
from ...gateway.device_registry import DeviceRegistry, DeviceSummary

__all__ = ["AdbDeviceRegistry"]

# Callable returning the device ids (``adb:{serial}`` or bare serial) that a
# lease or other non-migrated owner is currently holding.
ActiveDeviceIds = Callable[[], Iterable[str]]


class AdbDeviceRegistry:
    """Gateway ``DeviceRegistry`` served by one ``AdbTargetDiscovery``."""

    def __init__(
        self,
        discovery: AdbTargetDiscovery,
        *,
        active_device_ids: ActiveDeviceIds | None = None,
    ) -> None:
        self._discovery = discovery
        self._active_device_ids = active_device_ids

    def list_devices(self) -> tuple[DeviceSummary, ...]:
        try:
            result = self._discovery.discover()
        except Exception:
            # The contract never exposes vendor or ADB failure detail.
            return ()
        active = self._active_ids()
        summaries: list[DeviceSummary] = []
        for device in result.devices:
            device_id = f"adb:{device.serial}"
            if device_id in active or device.serial in active:
                status = "IN_USE"
            elif device.status is TargetStatus.READY:
                status = "AVAILABLE"
            else:
                status = "UNAVAILABLE"
            summaries.append(DeviceSummary(device_id=device_id, status=status))
        return tuple(summaries)

    def get_device(self, device_id: str) -> DeviceSummary | None:
        """Look up by full ``adb:{serial}`` id or bare serial."""
        if not isinstance(device_id, str):
            return None
        wanted = device_id.strip()
        if not wanted:
            return None
        bare_serial = wanted.removeprefix("adb:")
        for summary in self.list_devices():
            if summary.device_id == wanted:
                return summary
            if bare_serial and summary.device_id == f"adb:{bare_serial}":
                return summary
        return None

    def _active_ids(self) -> set[str]:
        if self._active_device_ids is None:
            return set()
        try:
            return {
                value
                for value in self._active_device_ids()
                if isinstance(value, str) and value.strip()
            }
        except Exception:
            # A failing overlay must degrade to "no overlay", not leak.
            return set()
