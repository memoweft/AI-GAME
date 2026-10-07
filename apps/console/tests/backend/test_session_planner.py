from __future__ import annotations

import threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_game_console.agent_runtime.coverage import validate_complete_coverage
from ai_game_console.agent_runtime.domain import (
    DirectiveKind,
    GoalCoverageDraft,
    GoalCoverageKind,
    GoalNode,
    GoalNodeDraft,
    GoalNodeStatus,
    GoalSchedulingClass,
    UserDirective,
    WakeConditionKind,
    utc_now,
)
from ai_game_console.agent_runtime.planner import DeterministicSessionPlanner, FallbackSessionPlanner
from ai_game_console.agent_runtime.service import AgentSessionService, CanonicalTaskService
from ai_game_console.agent_runtime.store import SQLiteAgentRuntimeStore
from ai_game_console.goal_runtime.qwen import StructuredSessionPlanner, _session_plan_from_decoded


class PreparedGoalService:
    def __init__(self) -> None:
        self.by_key: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, str]] = []

    def prepare_goal(self, goal: str, key: str) -> dict[str, str]:
        self.calls.append(("prepare", key))
        return self.by_key.setdefault(key, {"id": f"goal-{len(self.by_key) + 1}", "original_goal": goal, "execution_status": "ACCEPTED", "control_state": "AUTOMATED"})

    def activate_goal(self, goal_id: str) -> dict[str, str]:
        self.calls.append(("activate", goal_id))
        return self.inspect(goal_id)

    def inspect(self, goal_id: str) -> dict[str, str]:
        return next(value for value in self.by_key.values() if value["id"] == goal_id)

    def control(self, goal_id: str, action: str, key: str):
        del action, key
        return self.inspect(goal_id)


def _service(tmp_path: Path, goals: PreparedGoalService, **kwargs: object) -> AgentSessionService:
    return AgentSessionService(SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db"), goals, **kwargs)


def test_composite_instruction_creates_multiple_independent_goal_nodes(tmp_path: Path) -> None:
    goals = PreparedGoalService()
    created = _service(tmp_path, goals).create(
        "今天去 Soul 和探探认识些人，合适的就聊；顺便帮我投简历；微信有人找我就维护一下；空闲的时候玩游戏。",
        "composite-1",
    )

    assert [item["title"] for item in created["goal_nodes"]] == [
        "Soul 认识人", "探探认识人", "找工作", "微信维护", "空闲游戏",
    ]
    assert len(created["goal_criteria"]) == 5
    assert len(created["activation_intents"]) == 5
    assert len(goals.calls) == 10
    assert created["goal_graph"]["revision"] == 1
    assert {item["original_goal"] for item in goals.by_key.values()} == {
        "今天去 Soul 和探探认识些人，合适的就聊",
        "顺便帮我投简历",
        "微信有人找我就维护一下",
        "空闲的时候玩游戏",
    }
    scheduling_by_title = {
        item["title"]: item["scheduling_class"] for item in created["goal_nodes"]
    }
    assert scheduling_by_title["空闲游戏"] == GoalSchedulingClass.IDLE_ONLY.value
    assert all(
        value == GoalSchedulingClass.NORMAL.value
        for title, value in scheduling_by_title.items()
        if title != "空闲游戏"
    )


def test_every_original_clause_has_goal_or_session_policy_coverage() -> None:
    directive = UserDirective("d-1", "s-1", 1, "投简历；微信有人找我就维护一下", DirectiveKind.ORIGINAL, "source", utc_now())
    plan = DeterministicSessionPlanner().plan(
        session_id="s-1", directives=[directive], authority_revision=1, graph_revision=0
    )
    assert {item.original_fragment for item in plan.coverage} == {"投简历", "微信有人找我就维护一下"}


def test_split_model_fragments_jointly_cover_one_natural_clause() -> None:
    nodes = (
        GoalNodeDraft("soul", "Soul 认识人", "d-1", "今天去 Soul 和探探认识些人"),
        GoalNodeDraft("chat", "合适就聊", "d-1", "合适的就聊"),
    )
    coverage = (
        GoalCoverageDraft("d-1", "今天去 Soul 和探探认识些人", GoalCoverageKind.GOAL, "soul"),
        GoalCoverageDraft("d-1", "合适的就聊", GoalCoverageKind.GOAL, "chat"),
    )
    validate_complete_coverage(
        ["今天去 Soul 和探探认识些人，合适的就聊"], coverage, nodes
    )


def test_structured_planner_can_retain_a_digit_prefixed_migrated_uuid() -> None:
    now = utc_now()
    directive = UserDirective("d-1", "s-1", 1, "投简历", DirectiveKind.ORIGINAL, "source", now)
    goal_id = "0abc0000-0000-0000-0000-000000000000"
    existing = GoalNode(
        goal_id, "s-1", "投简历", directive.id, GoalNodeStatus.ACTIVE,
        None, now, now, original_fragment="投简历",
    )
    decoded = {
        "schema_version": StructuredSessionPlanner.SCHEMA_VERSION,
        "session_interpretation": "保留原目标",
        "revision_reason": "保留迁移目标",
        "goal_drafts": [{
            "goal_ref": goal_id, "title": "投简历", "execution_goal": "投简历",
            "source_directive_id": directive.id, "source_fragment": "投简历",
            "operation": "RETAIN", "goal_family": None, "application_hint": None,
            "account_hint": None, "priority": None,
            "activation_mode": "IMMEDIATE", "event_type": None,
            "event_application_package": None, "event_person_hint": None,
            "event_conversation_hint": None,
        }],
        "success_criteria": [{
            "goal_ref": goal_id, "description": "完成投递",
            "evidence_requirement": "保留投递结果", "required": True,
        }],
        "coverage_map": [{
            "source_directive_id": directive.id, "source_fragment": "投简历",
            "target_kind": "GOAL", "target_ref": goal_id, "session_policy": None,
        }],
        "goal_edges": [],
    }
    plan = _session_plan_from_decoded(
        decoded, directive_index={directive.id: directive}, authority_revision=2,
        source_directive_id=directive.id, existing_nodes={goal_id: existing},
        base_graph_revision=1,
    )
    assert plan.nodes[0].existing_goal_id == goal_id
    assert plan.revision.base_graph_revision == 1


def test_structured_planner_scopes_new_model_refs_to_the_session() -> None:
    directive = UserDirective(
        "d-1", "s-1", 1, "投简历", DirectiveKind.ORIGINAL, "source", utc_now()
    )
    decoded = {
        "schema_version": StructuredSessionPlanner.SCHEMA_VERSION,
        "session_interpretation": "建立投递目标",
        "revision_reason": "首次规划",
        "goal_drafts": [{
            "goal_ref": "G1", "title": "投简历", "execution_goal": "投简历",
            "source_directive_id": directive.id, "source_fragment": "投简历",
            "operation": "ADD", "goal_family": None, "application_hint": None,
            "account_hint": None, "priority": None,
            "activation_mode": "IMMEDIATE", "event_type": None,
            "event_application_package": None, "event_person_hint": None,
            "event_conversation_hint": None,
        }],
        "success_criteria": [{
            "goal_ref": "G1", "description": "完成投递",
            "evidence_requirement": "保留投递结果", "required": True,
        }],
        "coverage_map": [{
            "source_directive_id": directive.id, "source_fragment": "投简历",
            "target_kind": "GOAL", "target_ref": "G1", "session_policy": None,
        }],
        "goal_edges": [],
    }

    def plan_for(session_id: str):
        return _session_plan_from_decoded(
            decoded, directive_index={directive.id: directive}, authority_revision=1,
            source_directive_id=directive.id, existing_nodes={}, session_id=session_id,
        )

    first = plan_for("session-A")
    retry = plan_for("session-A")
    sibling = plan_for("session-B")

    assert first.nodes[0].id == retry.nodes[0].id
    assert first.nodes[0].id != sibling.nodes[0].id
    assert first.criteria[0].goal_id == first.nodes[0].id
    assert first.coverage[0].target_goal_id == first.nodes[0].id
    assert first.goal_text(first.nodes[0].id) == "投简历"


def test_structured_planner_persists_conditional_notification_as_initial_wait(
    tmp_path: Path,
) -> None:
    directive = UserDirective(
        "d-wechat", "s-wechat", 1, "有微信新消息时查看微信但不发送",
        DirectiveKind.ORIGINAL, "source", utc_now(),
    )
    decoded = {
        "schema_version": StructuredSessionPlanner.SCHEMA_VERSION,
        "session_interpretation": "等待微信通知后只读查看",
        "revision_reason": "条件目标必须等待通知",
        "goal_drafts": [{
            "goal_ref": "wechat", "title": "查看微信新消息",
            "execution_goal": "有微信新消息时查看微信但不发送",
            "source_directive_id": directive.id,
            "source_fragment": "有微信新消息时查看微信但不发送",
            "operation": "ADD", "goal_family": None,
            "application_hint": "com.tencent.mm", "account_hint": None,
            "priority": None, "activation_mode": "EVENT",
            "event_type": "NotificationPostedEvent",
            "event_application_package": "com.tencent.mm",
            "event_person_hint": None, "event_conversation_hint": None,
        }],
        "success_criteria": [{
            "goal_ref": "wechat", "description": "查看新消息且不发送",
            "evidence_requirement": "保留只读查看证据", "required": True,
        }],
        "coverage_map": [{
            "source_directive_id": directive.id,
            "source_fragment": "有微信新消息时查看微信但不发送",
            "target_kind": "GOAL", "target_ref": "wechat",
            "session_policy": None,
        }],
        "goal_edges": [],
    }
    plan = _session_plan_from_decoded(
        decoded,
        directive_index={directive.id: directive},
        authority_revision=1,
        source_directive_id=directive.id,
        existing_nodes={},
        session_id="s-wechat",
    )
    node = plan.nodes[0]
    assert node.initial_wake is not None
    assert node.initial_wake.kind is WakeConditionKind.EVENT
    assert node.initial_wake.matcher == {
        "event_type": "NotificationPostedEvent",
        "application_package": "com.tencent.mm",
    }

    goals = PreparedGoalService()

    class FixedPlanner:
        def plan(self, **kwargs):
            current = kwargs["directives"][-1]
            current_decoded = deepcopy(decoded)
            current_decoded["goal_drafts"][0]["source_directive_id"] = current.id
            current_decoded["coverage_map"][0]["source_directive_id"] = current.id
            return _session_plan_from_decoded(
                current_decoded,
                directive_index={current.id: current},
                authority_revision=kwargs["authority_revision"],
                source_directive_id=current.id,
                existing_nodes={},
                base_graph_revision=kwargs["graph_revision"],
                session_id=kwargs["session_id"],
            )

    service = _service(tmp_path, goals, planner=FixedPlanner())
    session, _created = service.store.create_unplanned_session(
        instruction=directive.content,
        client_request_id="conditional-wechat-initial-wait",
    )
    service._plan_and_commit(session.id)
    service.recover_pending(session_id=session.id)
    projected = service.store.goal_nodes(session.id)[0]
    wakes = service.store.wake_conditions(session.id)
    assert projected.status is GoalNodeStatus.WAITING_EVENT
    assert projected.bound_goal_run_id is not None
    assert wakes[0].status.value == "PENDING"
    assert wakes[0].matcher == {
        "event_type": "NotificationPostedEvent",
        "application_package": "com.tencent.mm",
    }


def test_model_transport_outage_uses_deterministic_fallback_without_hiding_other_errors() -> None:
    class ModelUnavailable(RuntimeError):
        pass

    class FailingPlanner:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def plan(self, **kwargs):
            del kwargs
            raise self.error

    directive = UserDirective(
        "d-1", "s-1", 1, "投简历", DirectiveKind.ORIGINAL, "source", utc_now()
    )
    fallback = FallbackSessionPlanner(
        FailingPlanner(ModelUnavailable("offline")),
        recoverable_exceptions=(ModelUnavailable,),
    )
    plan = fallback.plan(
        session_id="s-1", directives=[directive], authority_revision=1,
        graph_revision=0,
    )
    assert plan.nodes[0].original_fragment == "投简历"

    strict = FallbackSessionPlanner(
        FailingPlanner(ValueError("invalid schema")),
        recoverable_exceptions=(ModelUnavailable,),
    )
    import pytest
    with pytest.raises(ValueError, match="invalid schema"):
        strict.plan(
            session_id="s-1", directives=[directive], authority_revision=1,
            graph_revision=0,
        )


def test_completed_goal_survives_a_later_service_revision(tmp_path: Path) -> None:
    goals = PreparedGoalService()
    service = _service(tmp_path, goals)
    first = service.create("投简历", "completed-1")
    completed_id = first["goal_nodes"][0]["id"]
    with service.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE goal_nodes SET status='COMPLETED', verified_result_ref='fact:resume' "
            "WHERE goal_node_id=?",
            (completed_id,),
        )

    revised = service.send_message(first["id"], "再玩一会游戏", "completed-2", "add")

    completed = next(item for item in revised["goal_nodes"] if item["id"] == completed_id)
    assert completed["status"] == "COMPLETED"
    assert completed["verified_result_ref"] == "fact:resume"
    assert revised["goal_graph"]["revision"] == 2
    assert len(revised["goal_nodes"]) == 2
    assert {item["original_goal"] for item in goals.by_key.values()} == {"投简历", "再玩一会游戏"}


def test_v1_migration_keeps_later_directive_pending_for_startup_planning(tmp_path: Path) -> None:
    database = tmp_path / "agent-runtime.db"
    legacy = SQLiteAgentRuntimeStore(database)
    session, _ = legacy.create_session(instruction="做 A", client_request_id="legacy-1")
    legacy.record_message(session.id, content="再做 B", client_request_id="legacy-2")
    import sqlite3
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM goal_graph_revisions")
        connection.execute("UPDATE agent_runtime_schema SET revision=1 WHERE singleton=1")

    goals = PreparedGoalService()
    recovered = AgentSessionService(SQLiteAgentRuntimeStore(database), goals)
    snapshot = recovered.inspect(session.id)

    assert snapshot["authority_revision"] == 2
    assert snapshot["goal_graph"]["authority_revision"] == 2
    assert snapshot["goal_graph"]["revision"] == 2
    assert {item["original_fragment"] for item in snapshot["goal_nodes"]} == {"做 A", "再做 B"}
    assert len(snapshot["goal_coverage"]) == 2


@pytest.mark.parametrize("task_status", ["scheduled", "running", "paused"])
def test_startup_planning_never_claims_runner_owned_canonical_phone_task(
    tmp_path: Path, task_status: str,
) -> None:
    class ExplodingPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def plan(self, **_: object):
            self.calls += 1
            raise AssertionError("paused Task must not call the planner")

    store = SQLiteAgentRuntimeStore(tmp_path / "agent-runtime.db")
    canonical = CanonicalTaskService(
        store, principal_id="owner", controller_id="controller"
    )
    created = canonical.create_task(
        "inspect one Android screen",
        "phone-create",
        origin={"runner_kind": "android_ui_agent", "runner_version": "1"},
    )
    task_id = created["task_id"]
    if task_status == "running":
        canonical.transition_task(
            task_id,
            status="running",
            reason_code="runner_started",
            summary="The assigned runner owns this Task.",
            recoverable=True,
            idempotency_key="phone-running",
            expected_current_status="scheduled",
        )
    elif task_status == "paused":
        canonical.control_task(
            task_id,
            action="pause",
            idempotency_key="phone-pause",
            expected_revision=created["current_revision"],
            requested_by={"source": "test"},
        )
    planner = ExplodingPlanner()
    service = AgentSessionService(
        store,
        PreparedGoalService(),
        planner=planner,
        recover_on_start=False,
    )

    session = store.get_session(task_id)
    assert session.status.value == "PLANNING"
    assert session.control_mode.value == "AGENT_ACTIVE"
    assert store.get_task(task_id).status.value == task_status
    assert store.graph_revision(task_id) is None
    assert service.recover_planning() == 0
    assert planner.calls == 0
    assert store.graph_revision(task_id) is None


def test_startup_planning_still_recovers_an_ordinary_unplanned_session(
    tmp_path: Path,
) -> None:
    class RecordingPlanner:
        def __init__(self) -> None:
            self.calls = 0
            self.inner = DeterministicSessionPlanner()

        def plan(self, **kwargs):
            self.calls += 1
            return self.inner.plan(**kwargs)

    store = SQLiteAgentRuntimeStore(tmp_path / "ordinary-agent-runtime.db")
    session, created = store.create_unplanned_session(
        instruction="整理普通会话目标",
        client_request_id="ordinary-session-create",
    )
    assert created is True
    assert store.get_task(session.id).origin == {}
    planner = RecordingPlanner()
    service = AgentSessionService(
        store,
        PreparedGoalService(),
        planner=planner,
        recover_on_start=False,
    )

    assert service.recover_planning() == 1
    assert planner.calls == 1
    assert store.graph_revision(session.id) is not None


def test_concurrent_directives_converge_on_one_latest_authority_snapshot(tmp_path: Path) -> None:
    class RacingPlanner:
        def __init__(self) -> None:
            self.inner = DeterministicSessionPlanner()
            self.authority_two_started = threading.Event()
            self.authority_three_started = threading.Event()

        def plan(self, **kwargs):
            authority = kwargs["authority_revision"]
            if authority == 2 and not self.authority_two_started.is_set():
                self.authority_two_started.set()
                assert self.authority_three_started.wait(5)
            elif authority >= 3:
                self.authority_three_started.set()
            return self.inner.plan(**kwargs)

    goals = PreparedGoalService()
    planner = RacingPlanner()
    service = _service(tmp_path, goals, planner=planner)
    created = service.create("做 A", "race-1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.send_message, created["id"], "再做 B", "race-2", "add")
        assert planner.authority_two_started.wait(5)
        second = pool.submit(service.send_message, created["id"], "再做 C", "race-3", "add")
        snapshots = (first.result(timeout=10), second.result(timeout=10))

    final = service.inspect(created["id"])
    assert all(item["authority_revision"] == 3 for item in snapshots)
    assert final["goal_graph"]["authority_revision"] == 3
    assert [item["authority_revision"] for item in final["goal_graph_revisions"]] == [1, 3]
    assert {item["original_fragment"] for item in final["goal_nodes"]} == {"做 A", "再做 B", "再做 C"}
    assert len(final["bindings"]) == 3
