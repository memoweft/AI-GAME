"""Kernel control surface: user-directed pause/resume/cancel/takeover.

The retained Gateway compatibility surface maps control commands onto the
existing Task state machine:

- ``pause``    RUNNING -> PAUSED   (event ``TaskPaused``, actor gateway)
- ``resume``   PAUSED  -> RUNNING  (event ``TaskResumed``; the runtime must
  Observe again before the next action, so no lease is re-acquired here)
- ``cancel``   any non-terminal -> CANCELLED (event ``TaskCancelled``)
- ``takeover`` RUNNING -> PAUSED   (event ``UserTakeover``; the client
  projection distinguishes user takeover from an ordinary pause)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..event.domain import RuntimeEvent
from ..task.domain import Task


class ControlCommand(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    TAKEOVER = "takeover"


class ControlError(ValueError):
    """Base error for control-surface violations."""


class InvalidControlTransition(ControlError):
    """The Task status does not permit the requested control command."""


@dataclass(frozen=True, slots=True)
class ControlResult:
    """Outcome of an applied control command.

    ``released_lease_id`` is the id of a Device Lease this command released
    (None when the Task held no active lease). ``event`` is the persisted
    runtime event carrying the Task-scoped sequence the Gateway reports back.
    """

    task: Task
    event: RuntimeEvent
    released_lease_id: str | None
