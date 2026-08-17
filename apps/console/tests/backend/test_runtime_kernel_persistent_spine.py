from __future__ import annotations

import ast
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from ai_game_console.runtime_adapters.sqlite import SQLiteRuntimeStore
from ai_game_console.runtime_kernel import (
    EventActor,
    InvalidStageTransition,
    InvalidTaskTransition,
    RuntimeEventDraft,
    RuntimeKernel,
    Stage,
    StageStatus,
    StoreConflict,
    TaskSource,
    TaskStatus,
)


TIMESTAMPS = (
    "2026-08-10T01:00:00+00:00",
    "2026-08-10T01:01:00+00:00",
    "2026-08-10T01:02:00+00:00",
    "2026-08-10T01:03:00+00:00",
    "2026-08-10T01:04:00+00:00",
    "2026-08-10T01:05:00+00:00",
    "2026-08-10T01:06:00+00:00",
    "2026-08-10T01:07:00+00:00",
)


def _source(suffix: str = "1") -> TaskSource:
    return TaskSource(
        client_id=f"client-{suffix}",
        conversation_id=f"conversation-{suffix}",
        initial_message_id=f"message-{suffix}",
    )


def _clock() -> Callable[[], str]:
    values = iter(TIMESTAMPS * 10)
    return lambda: next(values)


def _ids() -> Callable[[], str]:
    sequence = iter(f"generated-{index}" for index in range(1, 100))
    return lambda: next(sequence)


def _kernel(database_path: Path) -> RuntimeKernel:
    return RuntimeKernel(SQLiteRuntimeStore(database_path))


def _create_task(kernel: RuntimeKernel, task_id: str = "task-1"):
    return kernel.create_task(
        task_id=task_id,
        goal=f"Complete goal for {task_id}",
        source=_source(task_id),
        device_id=f"device-{task_id}",
    )


def _create_stage(
    kernel: RuntimeKernel, task_id: str = "task-1", stage_id: str = "stage-1"
):
    return kernel.create_stage(
        task_id=task_id,
        stage_id=stage_id,
        objective=f"Reach objective for {stage_id}",
        completion_criteria=("visible success evidence", "stable destination"),
        planner_call_id=f"planner-{stage_id}",
    )


def test_task_persists_and_recovers_after_store_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    process_a = _kernel(database_path)
    expected = _create_task(process_a)
    process_a.close()

    process_b = _kernel(database_path)

    assert process_b.load_task(expected.id) == expected
    assert [(event.type, event.sequence) for event in process_b.events(expected.id)] == [
        ("TaskCreated", 1)
    ]
    task_created = process_b.events(expected.id)[0]
    assert task_created.payload["source"] == {
        "client_id": "client-task-1",
        "conversation_id": "conversation-task-1",
        "initial_message_id": "message-task-1",
    }
    assert task_created.payload["device_id"] == "device-task-1"


def test_stage_and_current_stage_persist_after_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    process_a = _kernel(database_path)
    _create_task(process_a)
    planned = _create_stage(process_a)
    active = process_a.start_stage(task_id="task-1", stage_id=planned.id)
    task_after_start = process_a.load_task("task-1")
    process_a.close()

    process_b = _kernel(database_path)

    assert process_b.current_stage("task-1") == active
    assert process_b.load_task("task-1") == task_after_start
    assert active.status is StageStatus.ACTIVE
    assert active.objective == "Reach objective for stage-1"
    assert active.completion_criteria == (
        "visible success evidence",
        "stable destination",
    )


def test_single_active_stage_is_rejected_by_domain_and_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.db"
    kernel = _kernel(database_path)
    _create_task(kernel)
    first = _create_stage(kernel, stage_id="stage-1")
    second = _create_stage(kernel, stage_id="stage-2")
    kernel.start_stage(task_id="task-1", stage_id=first.id)

    with pytest.raises(InvalidTaskTransition, match="active Stage"):
        kernel.start_stage(task_id="task-1", stage_id=second.id)

    # The partial unique index is a second line of defense below the Domain.
    store = SQLiteRuntimeStore(database_path)
    illegal_second_active = second.activate(at=TIMESTAMPS[5])
    with pytest.raises(StoreConflict, match="UNIQUE"):
        store.create_stage(
            replace(illegal_second_active, id="stage-3", ordinal=3),
            RuntimeEventDraft(
                id="event-illegal-active",
                type="StageCreated",
                actor=EventActor.RUNTIME,
                created_at=TIMESTAMPS[5],
            ),
        )


def test_event_sequence_is_task_local_and_continues_after_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.db"
    process_a = _kernel(database_path)
    _create_task(process_a)
    stage = _create_stage(process_a)
    process_a.start_stage(task_id="task-1", stage_id=stage.id)
    assert [event.sequence for event in process_a.events("task-1")] == [1, 2, 3]
    process_a.close()

    process_b = _kernel(database_path)
    process_b.complete_stage(
        task_id="task-1",
        stage_id=stage.id,
        evidence_refs=("observation:test-success",),
        progress_summary="The explicit test evidence meets both criteria.",
    )

    events = process_b.events("task-1")
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [event.type for event in events] == [
        "TaskCreated",
        "StageCreated",
        "StageStarted",
        "StageCompleted",
    ]
    assert [event.sequence for event in process_b.events("task-1", after_sequence=2)] == [
        3,
        4,
    ]


def test_tasks_keep_stages_events_and_sequences_isolated(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path / "runtime.db")
    _create_task(kernel, "task-a")
    _create_task(kernel, "task-b")
    stage_a = _create_stage(kernel, "task-a", "stage-a")
    stage_b = _create_stage(kernel, "task-b", "stage-b")
    active_a = kernel.start_stage(task_id="task-a", stage_id=stage_a.id)

    assert kernel.list_stages("task-a") == (active_a,)
    assert kernel.list_stages("task-b") == (stage_b,)
    assert [event.sequence for event in kernel.events("task-a")] == [1, 2, 3]
    assert [event.sequence for event in kernel.events("task-b")] == [1, 2]
    assert kernel.current_stage("task-a").id == "stage-a"
    assert kernel.current_stage("task-b") is None


def test_task_creation_rolls_back_when_event_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(database_path)
    store.initialize()

    def fail_event(*_args, **_kwargs):
        raise sqlite3.IntegrityError("injected event failure")

    monkeypatch.setattr(store, "_insert_event", fail_event)
    kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())

    with pytest.raises(StoreConflict, match="injected event failure"):
        _create_task(kernel)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == 0


def test_stage_mutation_rolls_back_when_event_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(database_path)
    kernel = RuntimeKernel(store, clock=_clock(), id_factory=_ids())
    _create_task(kernel)
    planned = _create_stage(kernel)

    def fail_event(*_args, **_kwargs):
        raise sqlite3.IntegrityError("injected mutation event failure")

    monkeypatch.setattr(store, "_insert_event", fail_event)
    with pytest.raises(StoreConflict, match="injected mutation event failure"):
        kernel.start_stage(task_id="task-1", stage_id=planned.id)

    assert store.load_task("task-1").status is TaskStatus.CREATED
    assert store.load_task("task-1").current_stage_id is None
    assert store.load_stage("task-1", planned.id).status is StageStatus.PENDING
    assert [event.type for event in store.list_events("task-1")] == [
        "TaskCreated",
        "StageCreated",
    ]


def test_invalid_task_and_stage_transitions_are_rejected(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path / "runtime.db")
    task = _create_task(kernel)
    stage = _create_stage(kernel)

    with pytest.raises(InvalidTaskTransition, match="CREATED to COMPLETED"):
        task.transition_to(TaskStatus.COMPLETED, at=TIMESTAMPS[3])
    with pytest.raises(InvalidStageTransition, match="PENDING to COMPLETED"):
        stage.complete(
            at=TIMESTAMPS[3], evidence_refs=("observation:not-yet-active",)
        )

    active = kernel.start_stage(task_id=task.id, stage_id=stage.id)
    completed = kernel.complete_stage(
        task_id=task.id,
        stage_id=active.id,
        evidence_refs=("observation:complete",),
    )
    with pytest.raises(InvalidStageTransition, match="COMPLETED to ACTIVE"):
        completed.activate(at=TIMESTAMPS[7])


def test_restart_recovery_preserves_all_committed_facts(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    process_a = _kernel(database_path)
    _create_task(process_a)
    stage = _create_stage(process_a)
    process_a.start_stage(task_id="task-1", stage_id=stage.id)
    completed = process_a.complete_stage(
        task_id="task-1",
        stage_id=stage.id,
        evidence_refs=("observation:42", "event:verification-42"),
        progress_summary="The stage is complete.",
    )
    expected_task = process_a.load_task("task-1")
    expected_stages = process_a.list_stages("task-1")
    expected_events = process_a.events("task-1")
    process_a.close()

    process_b = _kernel(database_path)

    assert process_b.load_task("task-1") == expected_task
    assert process_b.list_stages("task-1") == expected_stages == (completed,)
    assert process_b.events("task-1") == expected_events
    assert process_b.current_stage("task-1") is None


def test_schema_contains_phase_5_lease_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(database_path)
    store.initialize()

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert tables == {
        "runtime_schema",
        "runtime_tasks",
        "runtime_stages",
        "runtime_events",
        "runtime_observations",
        "runtime_actions",
        "runtime_action_executions",
        "runtime_verifications",
        "runtime_facts",
        "runtime_checkpoints",
        "runtime_device_leases",
    }


def test_runtime_kernel_has_no_legacy_or_infrastructure_imports() -> None:
    source_root = (
        Path(__file__).parents[2]
        / "backend"
        / "ai_game_console"
        / "runtime_kernel"
    )
    imported_modules: set[str] = set()
    source_text = ""
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        source_text += text
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    assert "sqlite3" not in imported_modules
    assert "fastapi" not in imported_modules
    for forbidden_concept in (
        "TaskPlan",
        "Subgoal",
        "Reflection",
        "SkillMemory",
        "ApplicationRuntime",
        "MobileTaskRuntime",
    ):
        assert forbidden_concept not in source_text
