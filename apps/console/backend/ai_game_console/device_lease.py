"""Small in-process lease for serializing physical Android device use.

The console has more than one high-level orchestrator.  They may plan in
parallel, but screenshots and inputs for one Android serial must never
interleave: an action from one loop can make the other loop's latest frame
stale.  This module intentionally owns only that one concurrency invariant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol


class TargetBusyError(RuntimeError):
    """Raised when another in-process operation owns the same Android serial."""

    code = "target_busy"

    def __init__(self, target_key: str) -> None:
        self.target_key = target_key
        super().__init__(self.code)


class DeviceLease(Protocol):
    """Non-blocking ownership boundary for one physical device target."""

    def acquire(self, target_key: str) -> "DeviceLeaseHandle | None": ...


@dataclass(slots=True)
class DeviceLeaseHandle:
    """Idempotent ownership handle returned by :class:`DeviceExecutionLease`."""

    _manager: "DeviceExecutionLease" = field(repr=False)
    target_key: str
    _released: bool = field(default=False, init=False, repr=False)

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Return the target to the pool; repeated calls are deliberately safe."""

        self._manager._release(self)

    close = release

    def __enter__(self) -> "DeviceLeaseHandle":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()


class DeviceExecutionLease:
    """A process-local, non-blocking lease keyed by canonical ADB serial."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._holders: dict[str, DeviceLeaseHandle] = {}

    def acquire(self, target_key: str) -> DeviceLeaseHandle | None:
        """Try to reserve ``target_key`` without waiting for its current owner."""

        normalized_key = _normalized_target_key(target_key)
        with self._lock:
            if normalized_key in self._holders:
                return None
            handle = DeviceLeaseHandle(self, normalized_key)
            self._holders[normalized_key] = handle
            return handle

    try_acquire = acquire

    def require(self, target_key: str) -> DeviceLeaseHandle:
        """Acquire or raise the stable public ``target_busy`` failure."""

        handle = self.acquire(target_key)
        if handle is None:
            raise TargetBusyError(_normalized_target_key(target_key))
        return handle

    def is_held(self, target_key: str) -> bool:
        """Read-only test/diagnostic helper; it grants no ownership."""

        normalized_key = _normalized_target_key(target_key)
        with self._lock:
            return normalized_key in self._holders

    def _release(self, handle: DeviceLeaseHandle) -> None:
        with self._lock:
            if handle._released:
                return
            # A stale or forged handle cannot release a newer owner.
            if self._holders.get(handle.target_key) is handle:
                del self._holders[handle.target_key]
            handle._released = True


def _normalized_target_key(target_key: str) -> str:
    if not isinstance(target_key, str) or not target_key.strip():
        raise ValueError("target_key must not be blank")
    return target_key.strip()
