"""Long-lived EventInbox producers that must run between launcher restarts."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any, Callable


logger = logging.getLogger(__name__)


class AgentRuntimeEventPump:
    """Poll durable time wakes and hand them to the AgentSession service."""

    def __init__(
        self,
        service: Any,
        *,
        long_task_scheduler: Any | None = None,
        interval_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("event pump interval must be positive")
        self.service = service
        # Package H may inject the C scheduler after wiring Package B's
        # CanonicalTaskService.  Keeping it optional preserves the established
        # R7 AgentSession EventInbox pump and avoids a second background loop.
        self.long_task_scheduler = long_task_scheduler
        self.interval_seconds = float(interval_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._scheduler_shutdown = False

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def poll_once(self) -> int:
        handled = int(
            self.service.poll_due_time_events(
                dispatch=True,
                now=self.clock(),
            )
        )
        if self.long_task_scheduler is not None:
            handled += int(self.long_task_scheduler.poll_once())
        return handled

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop.clear()
            self._scheduler_shutdown = False
            self._thread = threading.Thread(
                target=self._run,
                name="agent-runtime-event-pump",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join(timeout=max(1.0, self.interval_seconds * 2))
        joined = thread is None or not thread.is_alive()
        with self._lock:
            if self._thread is thread and joined:
                self._thread = None
        # Never release the process scheduler lease while the sole pump thread
        # may still be inside poll_once.  A timed-out join leaves the durable
        # lease to expire naturally so another process cannot overlap early.
        if joined and not self._scheduler_shutdown:
            shutdown = getattr(self.long_task_scheduler, "shutdown", None)
            if callable(shutdown):
                shutdown()
            self._scheduler_shutdown = True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # A durable timer event or wake remains available to the next
                # poll/restart.  One bad Session must not kill the producer.
                logger.exception("AgentRuntime time-event poll failed")
            self._stop.wait(self.interval_seconds)


__all__ = ["AgentRuntimeEventPump"]
