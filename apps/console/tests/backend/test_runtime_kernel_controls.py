"""Phase 6 Week 1: Kernel control surface (pause/resume/cancel/takeover).

Covers the frozen Gateway contract (§9) at the Kernel level:
- each command maps onto the frozen Task transition map;
- events TaskPaused/TaskResumed/TaskCancelled/UserTakeover are persisted
  with actor=gateway and carry previous_status + reason;
- pause/cancel/takeover release the Task's active Device Lease (idempotent),
  resume never re-acquires one (the runtime must Observe first);
- stale CAS on mutate_task and identity invariants.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import (
    ControlCommand,
    ControlError,
    EventActor,
    InvalidControlTransition,
    RecordNotFound,
    RuntimeEventDraft,
    RuntimeKernel,
    StoreConflict,
    TaskSource,
    TaskStatus,
)

TIMES = tuple(f"2026-08-17T15:{minute:02d}:00+00:00" for minute in range(60))


def _clock() -> Callable[[], str]:
    values = iter(TIMES * 20)
    return lambda: next(values)


def _ids() -> Callable[[], str]:
    return lambda: str(uuid4())


def _source(suffix: str) -> TaskSource:
    return TaskSource(
        client_id=f"client-{suffix}",
        conversation_id=f"conversation-{suffix}",
        initial_message_id=f"message-{suffix}",
    )


def _kernel(tmp_path: Path) -> RuntimeKernel:
    return RuntimeKernel(SQLiteRuntimeStore(tmp_path / "runtime.db"), clock=_clock(), id_factory=_ids())


def _running_task(kernel: RuntimeKernel, device_id: str, suffix: str):
    """Create a Task in RUNNING status (stage started)."""
    task = kernel.create_task(
        goal=f"control surface {suffix}",
        source=_source(suffix),
        device_id=device_id,
    )
    stage = kernel.create_stage(
        task_id=task.id,
        objective="running",
        completion_criteria=("done",),
    )
    kernel.start_stage(task_id=task.id, stage_id=stage.id)
    return task


class TestPause:
    def test_pause_from_running(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-pause", "pause")

        result = kernel.apply_control(
            task_id=task.id, command=ControlCommand.PAUSE, reason="user_requested"
        )

        assert result.task.status is TaskStatus.PAUSED
        assert result.released_lease_id is None
        assert result.event.type == "TaskPaused"
        assert result.event.actor is EventActor.GATEWAY
        assert result.event.task_id == task.id
        assert result.event.sequence > 0
        assert result.event.payload == {
            "previous_status": "RUNNING",
            "reason": "user_requested",
        }
        assert kernel.load_task(task.id).status is TaskStatus.PAUSED

    def test_pause_rejects_already_paused(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-pp", "pp")
        kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)
        events_before = kernel._store.list_events(task.id)

        with pytest.raises(InvalidControlTransition, match="cannot be paused"):
            kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)

        assert kernel.load_task(task.id).status is TaskStatus.PAUSED
        assert kernel._store.list_events(task.id) == events_before

    def test_pause_and_resume_created_task_preserve_preplanning_state(
        self, tmp_path: Path
    ) -> None:
        kernel = _kernel(tmp_path)
        task = kernel.create_task(
            goal="never started", source=_source("created"), device_id="device-c"
        )
        paused = kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)
        assert paused.task.status is TaskStatus.PAUSED

        resumed = kernel.apply_control(task_id=task.id, command=ControlCommand.RESUME)

        assert resumed.task.status is TaskStatus.CREATED
        assert kernel.load_task(task.id).status is TaskStatus.CREATED

    def test_pause_releases_active_lease(self, tmp_path: Path) -> None:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
        store.initialize()
        kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())
        task = _running_task(kernel, "device-lease", "lease")
        lease = store.acquire_lease(
            device_id="device-lease",
            task_id=task.id,
            holder_process_id="4242",
            ttl_seconds=60,
            lease_id=str(uuid4()),
            acquired_at=TIMES[0],
        )

        result = kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)

        assert result.released_lease_id == lease.id
        assert store.get_lease_for_task(task.id) is None
        assert store.get_lease_for_device("device-lease") is None

    def test_pause_without_lease_succeeds(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-nolease", "nolease")
        result = kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)
        assert result.released_lease_id is None


class TestResume:
    def test_resume_from_paused(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-resume", "resume")
        kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)

        result = kernel.apply_control(task_id=task.id, command=ControlCommand.RESUME)

        assert result.task.status is TaskStatus.RUNNING
        assert result.released_lease_id is None
        assert result.event.type == "TaskResumed"
        assert result.event.actor is EventActor.GATEWAY
        assert result.event.payload == {
            "previous_status": "PAUSED",
            "reason": None,
        }
        assert kernel.load_task(task.id).status is TaskStatus.RUNNING

    def test_resume_does_not_acquire_lease(self, tmp_path: Path) -> None:
        """Contract §9: resume only restores Task state; the runtime must Observe."""
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-robserve", "robserve")
        kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)
        kernel.apply_control(task_id=task.id, command=ControlCommand.RESUME)
        assert kernel._store.get_lease_for_task(task.id) is None

    def test_resume_rejects_running_task(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-rr", "rr")
        with pytest.raises(InvalidControlTransition, match="cannot be resumed"):
            kernel.apply_control(task_id=task.id, command=ControlCommand.RESUME)
        assert kernel.load_task(task.id).status is TaskStatus.RUNNING


class TestCancel:
    def test_cancel_from_running(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-cancel", "cancel")

        result = kernel.apply_control(
            task_id=task.id, command=ControlCommand.CANCEL, reason="user_requested"
        )

        assert result.task.status is TaskStatus.CANCELLED
        assert result.task.terminal_at is not None
        assert result.event.type == "TaskCancelled"
        assert result.event.payload == {
            "previous_status": "RUNNING",
            "reason": "user_requested",
        }

    def test_cancel_from_created(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = kernel.create_task(
            goal="cancel early", source=_source("cc"), device_id="device-cc"
        )
        result = kernel.apply_control(task_id=task.id, command=ControlCommand.CANCEL)
        assert result.task.status is TaskStatus.CANCELLED
        assert result.event.payload["previous_status"] == "CREATED"

    def test_cancel_from_paused(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-cp", "cp")
        kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)

        result = kernel.apply_control(task_id=task.id, command=ControlCommand.CANCEL)

        assert result.task.status is TaskStatus.CANCELLED
        assert result.event.payload["previous_status"] == "PAUSED"

    def test_cancel_releases_active_lease(self, tmp_path: Path) -> None:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
        store.initialize()
        kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())
        task = _running_task(kernel, "device-cl", "cl")
        store.acquire_lease(
            device_id="device-cl",
            task_id=task.id,
            holder_process_id="1",
            ttl_seconds=60,
            lease_id=str(uuid4()),
            acquired_at=TIMES[0],
        )

        result = kernel.apply_control(task_id=task.id, command=ControlCommand.CANCEL)

        assert result.released_lease_id is not None
        assert store.get_lease_for_device("device-cl") is None

    def test_cancel_rejects_terminal_task(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-ct", "ct")
        kernel.apply_control(task_id=task.id, command=ControlCommand.CANCEL)

        with pytest.raises(InvalidControlTransition, match="already terminal"):
            kernel.apply_control(task_id=task.id, command=ControlCommand.CANCEL)
        assert kernel.load_task(task.id).status is TaskStatus.CANCELLED

    def test_cancel_leaves_other_task_lease_alone(self, tmp_path: Path) -> None:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
        store.initialize()
        kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())
        other = kernel.create_task(
            goal="other holder", source=_source("other"), device_id="device-x"
        )
        other_lease = store.acquire_lease(
            device_id="device-x",
            task_id=other.id,
            holder_process_id="9",
            ttl_seconds=60,
            lease_id=str(uuid4()),
            acquired_at=TIMES[0],
        )
        task = _running_task(kernel, "device-cx", "cx")

        result = kernel.apply_control(task_id=task.id, command=ControlCommand.CANCEL)

        assert result.released_lease_id is None
        assert store.get_lease_for_device("device-x").id == other_lease.id


class TestTakeover:
    def test_takeover_from_running(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-to", "to")

        result = kernel.apply_control(
            task_id=task.id, command=ControlCommand.TAKEOVER, reason="user_takeover"
        )

        assert result.task.status is TaskStatus.PAUSED
        assert result.event.type == "UserTakeover"
        assert result.event.actor is EventActor.GATEWAY
        assert result.event.payload == {
            "previous_status": "RUNNING",
            "reason": "user_takeover",
        }

    def test_takeover_releases_active_lease(self, tmp_path: Path) -> None:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
        store.initialize()
        kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())
        task = _running_task(kernel, "device-tl", "tl")
        lease = store.acquire_lease(
            device_id="device-tl",
            task_id=task.id,
            holder_process_id="7",
            ttl_seconds=60,
            lease_id=str(uuid4()),
            acquired_at=TIMES[0],
        )

        result = kernel.apply_control(task_id=task.id, command=ControlCommand.TAKEOVER)

        assert result.released_lease_id == lease.id
        assert store.get_lease_for_device("device-tl") is None

    def test_takeover_rejects_paused_task(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-tp", "tp")
        kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)
        with pytest.raises(InvalidControlTransition, match="cannot be taken over"):
            kernel.apply_control(task_id=task.id, command=ControlCommand.TAKEOVER)

    def test_takeover_and_pause_project_differently(self, tmp_path: Path) -> None:
        """Both land on PAUSED; only the event type distinguishes them."""
        kernel = _kernel(tmp_path)
        t1 = _running_task(kernel, "device-pd1", "pd1")
        t2 = _running_task(kernel, "device-pd2", "pd2")
        paused = kernel.apply_control(task_id=t1.id, command=ControlCommand.PAUSE)
        taken = kernel.apply_control(task_id=t2.id, command=ControlCommand.TAKEOVER)
        assert paused.task.status is taken.task.status is TaskStatus.PAUSED
        assert paused.event.type == "TaskPaused"
        assert taken.event.type == "UserTakeover"


class TestControlInvariants:
    def test_unknown_task_raises_record_not_found(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        with pytest.raises(RecordNotFound):
            kernel.apply_control(
                task_id="no-such-task", command=ControlCommand.PAUSE
            )

    def test_unknown_command_raises_control_error(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-uc", "uc")
        with pytest.raises(ControlError, match="unknown control command"):
            kernel.apply_control(task_id=task.id, command="bogus")
        assert kernel.load_task(task.id).status is TaskStatus.RUNNING

    def test_command_sequence_is_strictly_ascending(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-seq", "seq")
        kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)
        kernel.apply_control(task_id=task.id, command=ControlCommand.RESUME)
        kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)
        kernel.apply_control(task_id=task.id, command=ControlCommand.CANCEL)

        events = kernel._store.list_events(task.id)
        sequences = [event.sequence for event in events]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
        types = [event.type for event in events]
        assert types[-4:] == [
            "TaskPaused",
            "TaskResumed",
            "TaskPaused",
            "TaskCancelled",
        ]
        assert all(event.actor is EventActor.GATEWAY for event in events[-4:])

    def test_reason_defaults_to_none(self, tmp_path: Path) -> None:
        kernel = _kernel(tmp_path)
        task = _running_task(kernel, "device-rd", "rd")
        result = kernel.apply_control(task_id=task.id, command=ControlCommand.PAUSE)
        assert result.event.payload["reason"] is None


class TestMutateTaskStore:
    @staticmethod
    def _draft(task_id: str) -> RuntimeEventDraft:
        return RuntimeEventDraft(
            id=str(uuid4()),
            type="TaskPaused",
            actor=EventActor.GATEWAY,
            payload={},
            correlation_id=task_id,
            created_at=TIMES[5],
        )

    def test_mutate_task_rejects_stale_before_task(self, tmp_path: Path) -> None:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
        store.initialize()
        kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())
        task = kernel.create_task(
            goal="stale cas", source=_source("stale"), device_id="device-st"
        )
        before = store.load_task(task.id)
        # Mutate the stored Task row so `before` becomes stale.
        stage = kernel.create_stage(
            task_id=task.id, objective="stale", completion_criteria=("x",)
        )
        kernel.start_stage(task_id=task.id, stage_id=stage.id)
        after = before.transition_to(TaskStatus.PLANNING, at=TIMES[5])

        with pytest.raises(StoreConflict, match="changed since it was loaded"):
            store.mutate_task(
                before_task=before,
                after_task=after,
                event=self._draft(task.id),
            )

    def test_mutate_task_rejects_identity_change(self, tmp_path: Path) -> None:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
        store.initialize()
        kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())
        task_a = kernel.create_task(
            goal="a", source=_source("a"), device_id="device-a"
        )
        task_b = kernel.create_task(
            goal="b", source=_source("b"), device_id="device-b"
        )
        with pytest.raises(StoreConflict, match="identity cannot change"):
            store.mutate_task(
                before_task=store.load_task(task_a.id),
                after_task=store.load_task(task_b.id).transition_to(
                    TaskStatus.PLANNING, at=TIMES[5]
                ),
                event=self._draft(task_a.id),
            )

    def test_mutate_task_persists_task_and_event_atomically(self, tmp_path: Path) -> None:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
        store.initialize()
        kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())
        task = kernel.create_task(
            goal="atomic", source=_source("atom"), device_id="device-atom"
        )
        before = store.load_task(task.id)
        after = before.transition_to(TaskStatus.PLANNING, at=TIMES[5])

        event = store.mutate_task(
            before_task=before, after_task=after, event=self._draft(task.id)
        )

        assert event.task_id == task.id
        # create_task already persisted TaskCreated (sequence 1).
        assert event.sequence == 2
        assert store.list_events(task.id)[-1] == event
        assert store.load_task(task.id) == after
