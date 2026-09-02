from __future__ import annotations

from pathlib import Path
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_game_console.experience_runtime import ExperienceService, SQLiteExperienceStore
from ai_game_console.application_runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
    Decision,
    ExecutionReceipt,
    ExternalOwnerEvent,
    Intent,
    Observation,
    Outcome,
    OwnerEventFenced,
)
from ai_game_console.goal_runtime import (
    GoalService,
    GoalSpecificationDraft,
    SQLiteGoalStore,
    SuccessCriterion,
    create_goal_router,
    goal_error_handler,
)
from ai_game_console.goal_runtime.domain import GoalError


GOAL = "在社交软件里持续认识适合长期相处的人，直到我停止"


def _specification(classification: str) -> GoalSpecificationDraft:
    return GoalSpecificationDraft(
        {
            "classification": classification,
            "goal_family": "social/continuous",
        },
        (
            SuccessCriterion(
                "continuous_social_goal",
                "持续处理匹配、消息和候选里程碑，直到用户明确停止。",
                "长期实例、外部事件、通知和控制证据。",
                GOAL,
            ),
        ),
    )


class FakeKernel:
    def __init__(self, store: SQLiteGoalStore) -> None:
        self.store = store
        self.start_calls = 0
        self.status = "running"

    def start(self, goal, client_request_id, **options):
        del goal, options
        goal_id = client_request_id.split(":")[1]
        plan = self.store.binding_plan(goal_id)
        assert plan is not None and plan.binding_kind == "runtime_kernel"
        self.start_calls += 1
        return self.inspect("kernel-1")

    def inspect(self, task_id):
        return {
            "task_id": task_id,
            "status": self.status,
            "events": (),
            "detail": None,
            "error_code": None,
        }

    def control(self, task_id, action):
        del task_id
        self.status = {"pause": "paused", "resume": "running", "stop": "stopped"}.get(
            action, self.status
        )
        return self.inspect("kernel-1")

    def send(self, task_id, content, client_request_id):
        del content, client_request_id
        return self.inspect(task_id)


class FakeApplicationRuntime:
    def __init__(self, store: SQLiteGoalStore) -> None:
        self.store = store
        self.start_calls = 0
        self.command_calls: list[str] = []
        self.status = "waiting"
        self.events: list[dict] = []
        self.stopped = False

    def start(
        self,
        profile_id,
        client_request_id,
        target_id=None,
        initial_input=None,
    ):
        del initial_input
        goal_id = client_request_id.split(":")[1]
        plan = self.store.binding_plan(goal_id)
        assert plan is not None
        assert plan.binding_kind == "application_runtime"
        if plan.owner_kind == "external_owner":
            assert plan.profile_id == "soul-reply-v1"
            assert plan.owner_binding_ref is not None
            assert target_id == plan.owner_binding_ref
            assert profile_id == "soul-reply-v1"
        else:
            assert plan.owner_kind == "local_runtime"
            assert plan.profile_id == "local-managed-v1"
            assert plan.owner_binding_ref is None
            assert target_id == "local-managed-runtime"
            assert profile_id == "local-managed-v1"
        self.start_calls += 1
        return self.inspect("application-1")

    def inspect(self, instance_id):
        assert instance_id == "application-1"
        return {
            "instance_id": instance_id,
            "status": self.status,
            "events": tuple(self.events),
            "detail": "waiting for next bounded event cycle",
            "error_code": None,
        }

    def command(self, instance_id, command, client_request_id):
        del client_request_id
        assert instance_id == "application-1"
        self.command_calls.append(command.tag)
        if command.tag == "Stop":
            self.status = "stopped"
            self.stopped = True
        elif command.tag == "Pause":
            self.status = "paused"
        elif command.tag in {"Resume", "Input"}:
            if self.stopped:
                raise RuntimeError("stopped instance cannot resume")
            self.status = "running"
        return self.inspect(instance_id)


def _service(
    tmp_path: Path,
    *,
    classification: str,
    store: SQLiteGoalStore | None = None,
    kernel: FakeKernel | None = None,
    application: FakeApplicationRuntime | None = None,
    experience: ExperienceService | None = None,
    answer_language=None,
) -> tuple[GoalService, SQLiteGoalStore, FakeKernel, FakeApplicationRuntime]:
    resolved_store = store or SQLiteGoalStore(tmp_path / "goals.db")
    resolved_kernel = kernel or FakeKernel(resolved_store)
    resolved_application = application or FakeApplicationRuntime(resolved_store)
    service = GoalService(
        resolved_store,
        mobile_runtime=None,
        mobile_archive=None,
        kernel_runtime=resolved_kernel,
        kernel_binding_kind="runtime_kernel",
        configured_serial="device-1",
        specify_goal=lambda _: _specification(classification),
        application_runtime=resolved_application,
        application_archive=resolved_application,
        experience=experience,
        answer_language=answer_language,
    )
    return service, resolved_store, resolved_kernel, resolved_application


def _api(service: GoalService) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(GoalError, goal_error_handler)
    app.include_router(create_goal_router(service))
    return app


def test_u8_finite_and_long_lived_routes_freeze_plan_before_executor_start(
    tmp_path: Path,
) -> None:
    finite, finite_store, kernel, application = _service(
        tmp_path / "finite", classification="finite_phone_goal"
    )
    finite_goal = finite.create(GOAL, "finite")
    assert finite_goal.binding_kind == "runtime_kernel"
    assert finite_store.binding_plan(finite_goal.id).route_kind == "finite_phone"
    assert kernel.start_calls == 1
    assert application.start_calls == 0

    long_lived, long_store, kernel, application = _service(
        tmp_path / "long", classification="long_lived_application_goal"
    )
    long_goal = long_lived.create(GOAL, "long")
    plan = long_store.binding_plan(long_goal.id)
    assert plan is not None
    assert plan.route_kind == "long_lived_mobile_application"
    assert plan.binding_kind == "long_lived_mobile_composition"
    assert plan.capability_ids == (
        "long_lived.wait",
        "android.observe",
        "android.action",
        "local.goal_verification",
        "experience.record",
    )
    assert plan.owner_kind is None
    assert plan.owner_binding_ref is None
    assert plan.profile_id is None
    assert long_goal.binding_kind == "long_lived_mobile_composition"
    assert long_goal.execution_status == "WAITING_CONFIGURATION"
    assert long_goal.waiting_reason["code"] == "long_lived_mobile_runtime_not_composed"
    environment = long_store.environment(long_goal.id)
    assert environment["state"] == "WAITING_CONFIGURATION"
    assert environment["facts"] == [
        {
            "capability": "long_lived.mobile_application",
            "state": "WAITING_CONFIGURATION",
            "detail": long_goal.waiting_reason["message"],
        }
    ]
    assert application.start_calls == 0
    assert kernel.start_calls == 0


def test_long_lived_mobile_binding_persists_stable_identity_for_rotated_transport(
    tmp_path: Path,
) -> None:
    transport_id = "adb:192.168.31.232:46387"
    canonical_id = "adb:adb-eb646d2b-vJhY31._adb-tls-connect._tcp"

    class LongLivedRuntime:
        def __init__(self) -> None:
            self.prepared_target_id = None

        def prepare(self, **options):
            self.prepared_target_id = options["target_id"]
            return self.inspect("long-lived-1")

        def activate(self, instance_id):
            return self.inspect(instance_id)

        def inspect(self, instance_id):
            return {
                "instance_id": instance_id,
                "status": "running",
                "events": (),
                "detail": "active",
                "error_code": None,
            }

    runtime = LongLivedRuntime()
    observed_targets: list[str] = []
    store = SQLiteGoalStore(tmp_path / "goals.db")
    service = GoalService(
        store,
        mobile_runtime=None,
        mobile_archive=None,
        kernel_runtime=FakeKernel(store),
        kernel_binding_kind="runtime_kernel",
        configured_serial="192.168.31.232:46387",
        specify_goal=lambda _: _specification("long_lived_application_goal"),
        long_lived_mobile_runtime=runtime,
        long_lived_mobile_archive=runtime,
        discover_mobile_application=lambda target_id: (
            observed_targets.append(target_id) or "com.android.settings"
        ),
        target_id_resolver=lambda target_id: (
            canonical_id if target_id == transport_id else target_id
        ),
    )

    created = service.create(GOAL, "rotated-transport")

    assert created.target_id == canonical_id
    assert runtime.prepared_target_id == canonical_id
    assert observed_targets == [canonical_id]


def test_u8_unknown_classification_has_no_executor_side_effect(tmp_path: Path) -> None:
    service, store, kernel, application = _service(
        tmp_path, classification="unsupported_future_route"
    )
    record = service.create(GOAL, "unknown")
    assert record.execution_status == "WAITING_CONFIGURATION"
    assert record.waiting_reason["code"] == "goal_route_unavailable"
    assert record.bound_task_id is None
    assert store.binding_plan(record.id) is None
    assert kernel.start_calls == 0
    assert application.start_calls == 0


@pytest.mark.parametrize("resume_path", ("idempotent_create", "retry_preflight"))
def test_u8_stopped_unbound_mobile_composition_cannot_bind_after_stop(
    tmp_path: Path,
    resume_path: str,
) -> None:
    service, _store, kernel, application = _service(
        tmp_path, classification="long_lived_application_goal"
    )
    waiting = service.create(GOAL, "mobile-composition-wait")
    assert waiting.execution_status == "WAITING_CONFIGURATION"
    assert waiting.bound_task_id is None
    assert waiting.waiting_reason["code"] == "long_lived_mobile_runtime_not_composed"
    assert application.start_calls == 0

    stopped = service.control(waiting.id, "stop", "stop-unbound")
    assert stopped.execution_status == "CANCELLED"
    assert stopped.binding_state == "CANCELLED"
    assert stopped.bound_task_id is None
    assert stopped.terminal_at is not None
    assert kernel.start_calls == 0
    assert application.start_calls == 0
    assert application.command_calls == []

    resumed = (
        service.create(GOAL, "mobile-composition-wait")
        if resume_path == "idempotent_create"
        else service.retry_preflight(waiting.id)
    )

    assert resumed.id == waiting.id
    assert resumed.execution_status == "CANCELLED"
    assert resumed.binding_state == "CANCELLED"
    assert resumed.bound_task_id is None
    assert application.start_calls == 0
    assert kernel.start_calls == 0
    assert application.command_calls == []


def test_u8_legacy_default_soul_plan_is_preserved_but_not_auto_started(
    tmp_path: Path,
) -> None:
    store = SQLiteGoalStore(tmp_path / "goals.db")
    legacy, _ = store.create(goal=GOAL, idempotency_key="legacy-default-soul")
    store.record_specification(
        legacy.id, _specification("long_lived_application_goal")
    )
    frozen = store.record_binding_plan(
        legacy.id,
        route_kind="long_lived_application",
        binding_kind="application_runtime",
        capability_ids=(
            "long_lived.wait",
            "external_owner.soul",
            "experience.record",
        ),
        owner_kind="external_owner",
        profile_id="soul-reply-v1",
        classification="long_lived_application_goal",
        rationale="historical generic-to-Soul default",
    )
    assert frozen.owner_binding_ref is not None
    store.mark_waiting_external(
        legacy.id,
        code="owner_unavailable",
        message="historical owner was unavailable",
    )
    service, _store, kernel, application = _service(
        tmp_path,
        classification="long_lived_application_goal",
        store=store,
    )

    waiting = service.retry_preflight(legacy.id)

    assert waiting.execution_status == "WAITING_CONFIGURATION"
    assert waiting.waiting_reason["code"] == (
        "legacy_default_soul_route_requires_migration"
    )
    assert store.binding_plan(legacy.id) == frozen
    assert waiting.bound_task_id is None
    assert kernel.start_calls == 0
    assert application.start_calls == 0


def test_u8_unexpected_application_start_error_is_not_disguised_as_wait(
    tmp_path: Path,
) -> None:
    class BrokenApplication(FakeApplicationRuntime):
        def start(self, *args, **kwargs):
            del args, kwargs
            raise ValueError("programming error")

    store = SQLiteGoalStore(tmp_path / "goals.db")
    application = BrokenApplication(store)
    service, _store, kernel, _application = _service(
        tmp_path,
        classification="long_lived_local_goal",
        store=store,
        application=application,
    )
    with pytest.raises(ValueError, match="programming error"):
        service.create(GOAL, "unexpected-start-error")
    assert kernel.start_calls == 0
    assert application.start_calls == 0


def test_u8_additive_experience_failure_is_visible_without_failing_owner(
    tmp_path: Path,
) -> None:
    class UnavailableExperience:
        def begin_application_episode(self, **kwargs):
            del kwargs
            raise RuntimeError("experience unavailable")

    service, store, _kernel, _application = _service(
        tmp_path,
        classification="long_lived_local_goal",
        experience=UnavailableExperience(),
    )
    created = service.create(GOAL, "experience-unavailable")
    assert created.binding_kind == "application_runtime"
    assert created.bound_task_id == "application-1"
    assert created.execution_status == "WAITING_EXTERNAL"
    events = store.events(created.id, after=0, limit=100)
    failure = [event for event in events if event.event_type == "goal_experience_unavailable"]
    assert len(failure) == 1
    assert failure[0].data == {"error_type": "RuntimeError"}


def test_u8_candidate_notification_continues_and_delayed_outcome_is_isolated(
    tmp_path: Path,
) -> None:
    experience = ExperienceService(SQLiteExperienceStore(tmp_path / "experience.db"))
    service, store, _kernel, application = _service(
        tmp_path,
        classification="long_lived_local_goal",
        experience=experience,
    )
    created = service.create(GOAL, "continuous")
    application.events.extend(
        [
            {
                "sequence": 1,
                "event_type": "candidate_notified",
                "data": {
                    "summary": "发现一个可继续了解的候选；目标继续运行。",
                    "evidence_refs": ["candidate-evidence-1"],
                },
                "created_at": "2026-08-23T08:00:00Z",
            },
            {
                "sequence": 2,
                "event_type": "delayed_positive",
                "data": {
                    "attribution_scope": "conversation-ref-sha256",
                    "evidence_refs": ["inbound-evidence-1"],
                    "confidence": 0.9,
                },
                "created_at": "2026-08-23T08:01:00Z",
            },
        ]
    )

    projected = service.inspect(created.id)
    assert projected.execution_status == "WAITING_EXTERNAL"
    assert projected.control_state == "AUTOMATED"
    notifications = store.notifications(created.id)
    assert len(notifications) == 1
    assert notifications[0].kind == "candidate"
    episode = experience.store.episode_for_task("application-1")
    signals = experience.store.signals_for_episode(episode.episode_id)
    assert len(signals) == 1
    assert signals[0].kind == "delayed_positive"
    assert signals[0].evidence_refs == (
        "attribution:conversation-ref-sha256",
        "inbound-evidence-1",
    )

    # Restart/reprojection neither starts another owner nor duplicates the
    # candidate notification or delayed outcome.
    restarted, _store, _kernel, _application = _service(
        tmp_path,
        classification="long_lived_local_goal",
        store=store,
        application=application,
        experience=experience,
    )
    assert restarted.inspect(created.id).execution_status == "WAITING_EXTERNAL"
    assert application.start_calls == 1
    assert len(store.notifications(created.id)) == 1
    assert len(experience.store.signals_for_episode(episode.episode_id)) == 1


def test_u8_api_projects_generic_mobile_plan_without_soul_or_owner(
    tmp_path: Path,
) -> None:
    service, _store, kernel, application = _service(
        tmp_path, classification="long_lived_application_goal"
    )
    with TestClient(_api(service)) as client:
        created = client.post(
            "/api/v2/goals",
            json={"goal": GOAL, "idempotency_key": "api-mobile-wait"},
        )

    payload = created.json()
    assert payload["execution_status"] == "WAITING_CONFIGURATION"
    assert payload["binding"]["kind"] == "long_lived_mobile_composition"
    assert payload["binding"]["completion_gate"] == (
        "continuous_mobile_goal_pending_runtime"
    )
    assert payload["binding_plan"]["route_kind"] == (
        "long_lived_mobile_application"
    )
    assert payload["binding_plan"]["owner_kind"] is None
    assert payload["binding_plan"]["owner_binding_ref"] is None
    assert payload["binding_plan"]["profile_id"] is None
    assert "external_owner.soul" not in payload["binding_plan"]["capability_ids"]
    assert payload["environment_state"]["facts"][0]["capability"] == (
        "long_lived.mobile_application"
    )
    assert kernel.start_calls == 0
    assert application.start_calls == 0


def test_u8_api_projects_plan_candidate_and_explicit_stop_fences_later_work(
    tmp_path: Path,
) -> None:
    service, _store, _kernel, application = _service(
        tmp_path, classification="long_lived_local_goal"
    )
    with TestClient(_api(service)) as client:
        created = client.post(
            "/api/v2/goals",
            json={"goal": GOAL, "idempotency_key": "api-long"},
        )
        goal_id = created.json()["id"]
        application.events.append(
            {
                "sequence": 1,
                "event_type": "candidate_notified",
                "data": {"evidence_refs": ["candidate-1"]},
                "created_at": "2026-08-23T08:00:00Z",
            }
        )
        projected = client.get(f"/api/v2/goals/{goal_id}")
        followed_up = client.post(
            f"/api/v2/goals/{goal_id}/messages",
            json={"content": "把交流节奏放慢一点", "idempotency_key": "input-long"},
        )
        stopped = client.post(
            f"/api/v2/goals/{goal_id}/controls",
            json={"action": "stop", "idempotency_key": "stop-long"},
        )

    payload = projected.json()
    assert payload["binding_plan"]["route_kind"] == "local_managed_application"
    assert payload["binding"]["completion_gate"] == "continuous_managed_goal"
    assert payload["notifications"][0]["kind"] == "candidate"
    assert payload["control_state"] == "AUTOMATED"
    assert followed_up.json()["id"] == goal_id
    assert stopped.json()["execution_status"] == "CANCELLED"
    assert application.command_calls == ["Input", "Stop"]
    assert application.stopped is True


def test_u8_language_only_route_uses_local_language_without_device(tmp_path: Path) -> None:
    observed_plans = []
    service, store, kernel, application = _service(
        tmp_path,
        classification="language_only_goal",
        answer_language=lambda _: observed_plans.append(
            store.binding_plan(store.list(1)[0].id)
        )
        or "这是本地语言能力生成的结果。",
    )
    record = service.create(GOAL, "language")
    assert record.execution_status == "COMPLETED"
    assert record.binding_kind == "local_language"
    assert record.result_summary == "这是本地语言能力生成的结果。"
    assert observed_plans[0] is not None
    assert observed_plans[0].route_kind == "language_only"
    assert kernel.start_calls == 0
    assert application.start_calls == 0


def test_u8_explicit_external_application_binding_sends_once_recovers_and_stops(
    tmp_path: Path,
) -> None:
    class ObservationPort:
        def __init__(self) -> None:
            self.sequence = 0

        def observe(self, instance):
            del instance
            self.sequence += 1
            return Observation(f"observation-{self.sequence}", fresh=True)

    class Policy:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            if context.instance.intents:
                return Decision(wait_seconds=0.2, detail="wait for inbound")
            return Decision(
                intent=Intent("external.send.once", {"opaque_ref": "message-1"}),
                detail="send one bounded reply",
            )

    class Owner:
        def __init__(self) -> None:
            self.dispatches = 0

        def reserve(self, instance, intent):
            del instance, intent
            return "reservation-1"

        def dispatch(self, reservation_id, instance, intent):
            del reservation_id, instance, intent
            self.dispatches += 1
            return ExecutionReceipt("receipt-1", True, "accepted once")

    class Verifier:
        def verify(self, context):
            del context
            return Outcome(
                "confirmed_success",
                "fresh owner evidence confirms exactly one send",
                terminal=False,
            )

    database = tmp_path / "application-runtime.db"
    goal_store = SQLiteGoalStore(tmp_path / "goals.db")
    experience = ExperienceService(SQLiteExperienceStore(tmp_path / "experience.db"))
    owner = Owner()
    policy = Policy()

    def build_runtime():
        return ApplicationRuntime(
            database,
            profile="soul-reply-v1",
            observation_port=ObservationPort(),
            policy=policy,
            execution_owner=owner,
            verifier=Verifier(),
        )

    runtime = build_runtime()
    service = GoalService(
        goal_store,
        mobile_runtime=None,
        mobile_archive=None,
        kernel_runtime=FakeKernel(goal_store),
        kernel_binding_kind="runtime_kernel",
        configured_serial="device-1",
        application_runtime=runtime,
        application_archive=runtime,
        experience=experience,
    )
    pending, _ = goal_store.create(goal=GOAL, idempotency_key="explicit-owner")
    goal_store.record_specification(
        pending.id, _specification("long_lived_application_goal")
    )
    plan = goal_store.record_binding_plan(
        pending.id,
        route_kind="explicit_external_application",
        binding_kind="application_runtime",
        capability_ids=(
            "long_lived.wait",
            "application_profile.soul-reply-v1",
            "experience.record",
        ),
        owner_kind="external_owner",
        profile_id="soul-reply-v1",
        classification="long_lived_application_goal",
        rationale="explicit specialized adapter selected outside generic classification",
    )
    created = service._bind_application(
        pending,
        specification=goal_store.specification(pending.id),
        binding_plan=plan,
    )
    deadline = time.monotonic() + 3
    state = runtime.inspect(created.bound_task_id)
    while time.monotonic() < deadline:
        state = runtime.inspect(created.bound_task_id)
        if state.status == "waiting" and state.outcomes:
            break
        time.sleep(0.01)
    assert state.status == "waiting"
    assert owner.dispatches == 1
    assert service.inspect(created.id).execution_status == "WAITING_EXTERNAL"

    plan = goal_store.binding_plan(created.id)
    assert plan is not None and plan.owner_binding_ref is not None
    assert state.target_id == plan.owner_binding_ref

    candidate = ExternalOwnerEvent(
        owner_binding_ref=plan.owner_binding_ref,
        owner_event_id="candidate-owner-event-1",
        event_type="candidate_notified",
        evidence_refs=("candidate-evidence-1",),
    )
    runtime.report_owner_event(created.bound_task_id, candidate)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = runtime.inspect(created.bound_task_id)
        if state.status == "waiting" and policy.calls >= 2:
            break
        time.sleep(0.01)
    assert state.status == "waiting"
    assert policy.calls >= 2
    candidate_projected = service.inspect(created.id)
    assert candidate_projected.execution_status == "WAITING_EXTERNAL"
    assert len(goal_store.notifications(created.id)) == 1

    delayed = ExternalOwnerEvent(
        owner_binding_ref=plan.owner_binding_ref,
        owner_event_id="delayed-owner-event-1",
        event_type="delayed_positive",
        evidence_refs=("inbound-evidence-1",),
        attribution_scope="conversation-ref-sha256",
        confidence=0.9,
    )
    runtime.report_owner_event(created.bound_task_id, delayed)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = runtime.inspect(created.bound_task_id)
        if state.status == "waiting" and policy.calls >= 3:
            break
        time.sleep(0.01)
    assert state.status == "waiting"
    projected = service.inspect(created.id)
    episode = experience.store.episode_for_task(created.bound_task_id)
    signals = experience.store.signals_for_episode(episode.episode_id)
    assert projected.execution_status == "WAITING_EXTERNAL"
    assert len(signals) == 1
    assert signals[0].kind == "delayed_positive"
    assert owner.dispatches == 1
    runtime.shutdown()

    recovered_runtime = build_runtime()
    restarted = GoalService(
        goal_store,
        mobile_runtime=None,
        mobile_archive=None,
        kernel_runtime=FakeKernel(goal_store),
        kernel_binding_kind="runtime_kernel",
        configured_serial="device-1",
        application_runtime=recovered_runtime,
        application_archive=recovered_runtime,
        experience=experience,
    )
    time.sleep(0.3)
    restored = restarted.inspect(created.id)
    assert restored.bound_task_id == created.bound_task_id
    assert restored.execution_status == "WAITING_EXTERNAL"
    assert owner.dispatches == 1
    event_count = len(recovered_runtime.inspect(created.bound_task_id).events)
    replayed = recovered_runtime.report_owner_event(created.bound_task_id, candidate)
    assert len(replayed.events) == event_count
    assert owner.dispatches == 1

    stopped = restarted.control(created.id, "stop", "stop-after-restart")
    assert stopped.execution_status == "CANCELLED"
    event_count = len(recovered_runtime.inspect(created.bound_task_id).events)
    with pytest.raises(OwnerEventFenced):
        recovered_runtime.report_owner_event(
            created.bound_task_id,
            ExternalOwnerEvent(
                owner_binding_ref=plan.owner_binding_ref,
                owner_event_id="after-stop-owner-event-1",
                event_type="candidate_notified",
                evidence_refs=("after-stop-evidence",),
            ),
        )
    assert len(recovered_runtime.inspect(created.bound_task_id).events) == event_count
    assert owner.dispatches == 1
    recovered_runtime.shutdown()
