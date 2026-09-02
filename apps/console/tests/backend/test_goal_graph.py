from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_game_console.agent_runtime.domain import (
    GoalCoverageDraft,
    GoalCoverageKind,
    GoalCriterionDraft,
    GoalEdgeDraft,
    GoalEdgeKind,
    GoalGraphRevisionDraft,
    GoalNodeDraft,
    GoalNodeStatus,
    SCHEMA_REVISION,
    WakeConditionDraft,
    WakeConditionKind,
)
from ai_game_console.agent_runtime.agenda import AgendaContext, AgendaGoalSnapshot
from ai_game_console.agent_runtime.scheduler import AttentionScheduler
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore, _SCHEMA


def _new_graph_input(store: SQLiteAgentRuntimeStore, session_id: str):
    directive = store.directives(session_id)[0]
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    return directive, first, second


def test_v1_database_migrates_once_and_empty_database_creates_current_schema(tmp_path: Path):
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as connection:
        connection.executescript(_SCHEMA)
        # Simulate the R1 ledger exactly; the runner applies every later
        # migration through the current AgentRuntime revision.
        connection.execute("UPDATE agent_runtime_schema SET revision=1 WHERE singleton=1")
    store = SQLiteAgentRuntimeStore(legacy)
    store.initialize()
    store.initialize()
    with sqlite3.connect(legacy) as connection:
        revision = connection.execute("SELECT revision FROM agent_runtime_schema").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert revision == SCHEMA_REVISION
    assert {"goal_graph_revisions", "goal_edges", "goal_criteria", "goal_coverage"} <= tables
    assert {"attention_decisions", "goal_continuations", "wake_conditions"} <= tables


def test_v2_database_migrates_through_current_schema_once(tmp_path: Path):
    legacy = tmp_path / "legacy-v2.db"
    with sqlite3.connect(legacy) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(_SCHEMA)
        SQLiteAgentRuntimeStore._migrate_schema_v2(connection)
        assert connection.execute("SELECT revision FROM agent_runtime_schema").fetchone()[0] == 2
    store = SQLiteAgentRuntimeStore(legacy)
    store.initialize()
    store.initialize()
    with sqlite3.connect(legacy) as connection:
        revision = connection.execute("SELECT revision FROM agent_runtime_schema").fetchone()[0]
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(session_events)")}
        goal_columns = {row[1] for row in connection.execute("PRAGMA table_info(goal_nodes)")}
    assert revision == SCHEMA_REVISION
    assert {"source_namespace", "source_event_id", "payload_digest", "affected_goal_ids_json"} <= event_columns
    assert {"scheduling_class", "last_service_at", "backoff_until"} <= goal_columns


def test_v2_multi_active_bindings_migrate_sibling_ready_and_scheduler_can_select_it(tmp_path: Path):
    legacy = tmp_path / "legacy-v2-multi-active.db"
    session_id, directive_id = "session-v2", "directive-v2"
    selected_id, sibling_id = "goal-selected", "goal-sibling"
    timestamp = "2026-08-24T00:00:00Z"
    with sqlite3.connect(legacy) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(_SCHEMA)
        SQLiteAgentRuntimeStore._migrate_schema_v2(connection)
        connection.execute(
            "INSERT INTO agent_sessions(session_id, client_request_id, original_instruction, authority_revision, "
            "session_kind, status, control_mode, active_goal_id, event_cursor, calendar_started_at, created_at, updated_at) "
            "VALUES (?, 'legacy-create', '先做当前目标，再做兄弟目标', 1, 'today', 'ACTIVE', 'AGENT_ACTIVE', ?, 0, ?, ?, ?)",
            (session_id, selected_id, timestamp, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO user_directives(directive_id, session_id, revision, content, directive_kind, source_message_id, created_at) "
            "VALUES (?, ?, 1, '先做当前目标，再做兄弟目标', 'original', 'legacy-message', ?)",
            (directive_id, session_id, timestamp),
        )
        connection.execute(
            "INSERT INTO goal_graph_revisions(graph_revision_id, session_id, revision, authority_revision, source_directive_id, reason, created_at) "
            "VALUES ('graph-v2', ?, 1, 1, ?, 'legacy R2 graph', ?)",
            (session_id, directive_id, timestamp),
        )
        for goal_id, run_id, title in (
            (selected_id, "run-selected", "当前目标"),
            (sibling_id, "run-sibling", "兄弟目标"),
        ):
            connection.execute(
                "INSERT INTO goal_nodes(goal_node_id, session_id, title, source_directive_id, status, bound_goal_run_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
                (goal_id, session_id, title, directive_id, run_id, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO session_goal_bindings(binding_id, session_id, goal_node_id, goal_run_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'BOUND', ?, ?)",
                (f"binding-{goal_id}", session_id, goal_id, run_id, timestamp, timestamp),
            )

    store = SQLiteAgentRuntimeStore(legacy)
    store.initialize()
    nodes = {item.id: item for item in store.goal_nodes(session_id)}
    assert nodes[selected_id].status is GoalNodeStatus.ACTIVE
    assert nodes[sibling_id].status is GoalNodeStatus.READY

    retry_at = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
    store.create_wake_condition(
        session_id,
        selected_id,
        WakeConditionDraft(kind=WakeConditionKind.TIME, due_at=retry_at.isoformat().replace("+00:00", "Z")),
    )
    session = store.get_session(session_id)
    goals = store.goal_nodes(session_id)
    plan = AttentionScheduler().request_attention(
        session_id=session_id,
        trigger_key="migration:selected-waiting",
        original_instruction=session.original_instruction,
        authority_revision=session.authority_revision,
        goals=tuple(
            AgendaGoalSnapshot.from_goal_node(
                item, binding_ready=True, switch_checkpoint_ready=True
            )
            for item in goals
        ),
        context=AgendaContext(active_goal_id=selected_id),
        now=retry_at - timedelta(hours=1),
    )
    assert plan.selected_goal_id == sibling_id
    decision, created, _ = store.commit_attention_decision(
        session_id,
        draft=plan.to_decision_draft(
            authority_revision=1,
            graph_revision=1,
            event_cursor=store.get_session(session_id).event_cursor,
        ),
        candidates=plan.evaluation.all_goals,
    )
    assert created is True
    assert decision.selected_goal_id == sibling_id
    assert store.get_session(session_id).active_goal_id == sibling_id


def test_apply_graph_revision_commits_nodes_criteria_edges_coverage_and_intents_together(tmp_path: Path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = store.create_unplanned_session(instruction="先做 A，再做 B", client_request_id="create-1")
    directive, first, second = _new_graph_input(store, session.id)
    applied = store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(1, directive.id, "split composite intent"),
        nodes=(
            GoalNodeDraft(first, "做 A", directive.id, "做 A", execution_goal="做 A"),
            GoalNodeDraft(second, "做 B", directive.id, "做 B", execution_goal="做 B"),
        ),
        edges=(GoalEdgeDraft(first, second, GoalEdgeKind.BLOCKS),),
        criteria=(GoalCriterionDraft(first, "A 已验证"), GoalCriterionDraft(second, "B 已验证")),
        coverage=(
            GoalCoverageDraft(directive.id, "做 A", GoalCoverageKind.GOAL, first),
            GoalCoverageDraft(directive.id, "做 B", GoalCoverageKind.GOAL, second),
        ),
    )
    assert applied.revision == 1
    assert {node.id for node in store.goal_nodes(session.id)} >= {first, second}
    assert len(store.goal_edges(session.id, revision=1)) == 1
    assert len(store.criteria(first, revision=1)) == 1
    assert len(store.coverage(session.id, revision=1)) == 2
    intents = [item for item in store.pending_activation_intents() if item.graph_revision == 1]
    assert {item.goal_node_id for item in intents} == {first, second}
    events = store.events(session.id, after=0, limit=100)
    graph_cursor = next(item.cursor for item in events if item.event_type.value == "goal_graph_revision_applied")
    activation_cursors = [
        item.cursor for item in events if item.event_type.value == "goal_activation_requested"
    ]
    assert activation_cursors and graph_cursor < min(activation_cursors)


def test_activation_intent_replay_fence_and_verified_goal_survive_later_revision(tmp_path: Path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = store.create_unplanned_session(instruction="做 A", client_request_id="create-1")
    directive, first, _ = _new_graph_input(store, session.id)
    store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(1, directive.id),
        nodes=(GoalNodeDraft(first, "做 A", directive.id, "做 A"),),
    )
    intent = next(item for item in store.pending_activation_intents() if item.goal_node_id == first)
    store.mark_outbox_processing(intent.id)
    binding = store.settle_goal_run(intent.id, goal_run_id="goal-A")
    assert binding.goal_node_id == first
    # A replay settles the same activation idempotently; it never creates a second active binding.
    assert store.settle_goal_run(intent.id, goal_run_id="goal-A").id == binding.id
    with store._connection(write=True) as connection:  # model a verified fact from a later runtime stage
        connection.execute(
            "UPDATE goal_nodes SET status=?, verified_result_ref=? WHERE goal_node_id=?",
            (GoalNodeStatus.COMPLETED.value, "fact:A", first),
        )
    later, _ = store.record_message(session.id, content="保持 A 的完成事实", client_request_id="later-1")
    store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(2, later.id, "later priority revision", base_graph_revision=1),
        nodes=(GoalNodeDraft(first, "renamed A", directive.id, "做 A", existing_goal_id=first, operation="UPDATE"),),
    )
    node = next(item for item in store.goal_nodes(session.id) if item.id == first)
    assert node.title == "做 A"
    assert node.verified_result_ref == "fact:A"
    assert len([item for item in store.bindings(session.id) if item.goal_node_id == first and item.active]) == 1


def test_wait_or_failure_cannot_terminalize_session_while_another_goal_is_runnable(tmp_path: Path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = store.create_unplanned_session(instruction="A 和 B", client_request_id="create-1")
    directive, first, second = _new_graph_input(store, session.id)
    store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(1, directive.id),
        nodes=(
            GoalNodeDraft(first, "A", directive.id, "A"),
            GoalNodeDraft(second, "B", directive.id, "B"),
        ),
    )
    first_intent = next(item for item in store.pending_activation_intents() if item.goal_node_id == first)
    store.settle_goal_run(first_intent.id, goal_run_id="goal-A")
    store.project_goal_run(session.id, {"id": "goal-A", "execution_status": "FAILED"})
    assert store.get_session(session.id).status.value == "ACTIVE"


def test_same_authority_revision_is_idempotent_and_stale_base_is_rejected(tmp_path: Path):
    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    session, _ = store.create_unplanned_session(instruction="做 A", client_request_id="create-1")
    directive = store.directives(session.id)[0]
    first = str(uuid.uuid4())
    initial = store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(1, directive.id, base_graph_revision=0),
        nodes=(GoalNodeDraft(first, "做 A", directive.id, "做 A", execution_goal="做 A"),),
    )
    replay = store.apply_graph_revision(
        session.id,
        revision=GoalGraphRevisionDraft(1, directive.id, base_graph_revision=0),
        nodes=(GoalNodeDraft(str(uuid.uuid4()), "重复 A", directive.id, "做 A"),),
    )
    assert replay.id == initial.id
    assert len(store.graph_revisions(session.id)) == 1
    assert [item.id for item in store.goal_nodes(session.id)] == [first]

    later, _ = store.record_message(session.id, content="再做 B", client_request_id="later-1")
    with pytest.raises(ValueError, match="base revision is stale"):
        store.apply_graph_revision(
            session.id,
            revision=GoalGraphRevisionDraft(2, later.id, base_graph_revision=0),
            nodes=(GoalNodeDraft(str(uuid.uuid4()), "做 B", later.id, "再做 B"),),
        )
