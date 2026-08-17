from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ai_game_console.soul_application_composition as composition_module
from ai_game_console.api import create_app
from ai_game_console.application_runtime import Input, Intent, Pause, Resume, Stop
from ai_game_console.application_runtime.domain import RuntimeClosed
from ai_game_console.application_runtime.store import _SQLiteApplicationStore
from ai_game_console.applications.soul import PROFILE_ID
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.soul_application_composition import (
    APPLICATION_DATABASE_FILENAME,
    DATE_AS_YUN_PERSONA_SHA256,
    DATE_AS_YUN_PERSONA_VERSION,
    REPLY_LEARNING_DATABASE_FILENAME,
    SCHEDULER_LIFECYCLE_DATABASE_FILENAME,
    SQLiteApplicationArchive,
    SQLiteSoulSchedulerLifecycleStore,
    SoulApplicationRuntimeGateway,
    SoulApplicationUnavailable,
    compose_soul_application_runtime,
    load_date_as_yun_persona,
)

from conftest import WRITE_HEADERS, build_settings


class _MutableCloudConfiguration:
    def __init__(self) -> None:
        self.provider = None
        self.resolve_calls = 0

    def resolve_provider(self):
        self.resolve_calls += 1
        return self.provider


class _Owner:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events
        self.capability_calls = 0
        self.observation_calls = 0
        self.scheduler_calls: list[tuple[str, str | None]] = []
        self.scheduler_desired_state = "running"
        self.scheduler_effective_state = "running"
        self.scheduler_controller_ref: str | None = "ai-game-soul-reply-v1"

    def capabilities(self):
        self.capability_calls += 1
        return {
            "contract_version": "v1",
            "service": "soul_execution_owner",
            "capabilities": {
                "observations": True,
                "intent_reserve": True,
                "intent_dispatch": True,
                "intent_inspect": True,
                "owner_captured_observations": True,
                "loopback_only": True,
            },
        }

    def observe(self):
        self.observation_calls += 1
        return {
            "contract_version": "v1",
            "scope": "no_due_pending_inbound",
            "expires_in_seconds": 0,
            "transcript": [],
        }

    def scheduler(self):
        if self.events is not None:
            self.events.append("scheduler:get")
        self.scheduler_calls.append(("GET", None))
        return {
            "contract_version": "v1",
            "controller_ref": self.scheduler_controller_ref,
            "desired_state": self.scheduler_desired_state,
            "effective_state": self.scheduler_effective_state,
            "reply_owner": (
                "application_runtime"
                if self.scheduler_effective_state != "stopped"
                else "none"
            ),
            "scheduler_mode": (
                "match"
                if self.scheduler_effective_state != "stopped"
                else None
            ),
        }

    def set_scheduler_state(self, desired_state: str):
        if self.events is not None:
            self.events.append(f"scheduler:{desired_state}")
        self.scheduler_calls.append(("PUT", desired_state))
        self.scheduler_desired_state = desired_state
        self.scheduler_controller_ref = "ai-game-soul-reply-v1"
        if desired_state == "paused" and self.scheduler_effective_state == "stopped":
            self.scheduler_effective_state = "stopped"
        else:
            self.scheduler_effective_state = (
                "stopping" if desired_state == "stopped" else desired_state
            )
        return {
            "contract_version": "v1",
            "controller_ref": "ai-game-soul-reply-v1",
            "desired_state": desired_state,
            "effective_state": self.scheduler_effective_state,
            "reply_owner": (
                "none" if desired_state == "stopped" else "application_runtime"
            ),
            "scheduler_mode": None if desired_state == "stopped" else "match",
        }


class _Vision:
    def extract(self, _screenshot):  # pragma: no cover - not called by composition
        raise AssertionError("composition must not call the local model")


class _Runtime:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        stateful: bool = False,
    ) -> None:
        self.events = events
        self.stateful = stateful
        self.status = "queued"
        self.start_calls = []
        self.command_calls = []
        self.shutdown_calls = 0
        self.shutdown_timeouts: list[float | None] = []

    def start(self, *args, **kwargs):
        if self.events is not None:
            self.events.append("runtime:start")
        self.start_calls.append((args, kwargs))
        if self.stateful:
            return {
                "started": True,
                "instance_id": "instance-1",
                "profile_id": PROFILE_ID,
                "status": self.status,
                "revision": 0,
                "updated_at": "2026-08-10T01:00:00Z",
            }
        return {"started": True}

    def command(self, *args, **kwargs):
        if self.events is not None:
            self.events.append(f"runtime:{args[1].tag.lower()}")
        self.command_calls.append((args, kwargs))
        if self.stateful:
            self.status = {
                "Pause": "paused",
                "Resume": "queued",
                "Stop": "stopped",
            }.get(args[1].tag, self.status)
            return {
                "commanded": True,
                "instance_id": args[0],
                "profile_id": PROFILE_ID,
                "status": self.status,
                "revision": 0,
                "updated_at": "2026-08-10T01:00:01Z",
            }
        return {"commanded": True}

    def shutdown(self, timeout: float | None = None) -> None:
        self.shutdown_calls += 1
        self.shutdown_timeouts.append(timeout)


class _StoreBackedRuntime:
    def __init__(self, store: _SQLiteApplicationStore, *, settle_stops=True):
        self.store = store
        self.settle_stops = settle_stops
        self.start_calls: list[tuple] = []
        self.command_calls: list[tuple] = []
        self.shutdown_calls = 0

    def start(
        self,
        profile_id,
        client_request_id,
        target_id=None,
        initial_input=None,
    ):
        self.start_calls.append(
            (profile_id, client_request_id, target_id, initial_input)
        )
        proposed_id = f"store-instance-{len(self.start_calls)}"
        state, _created = self.store.accept_start(
            proposed_id,
            profile_id,
            target_id,
            initial_input,
            client_request_id,
            f"digest:start:{client_request_id}",
        )
        return state

    def command(self, instance_id, command, client_request_id):
        self.command_calls.append((instance_id, command, client_request_id))
        content = command.content if command.tag == "Input" else None
        state, created = self.store.accept_command(
            instance_id,
            command.tag,
            content,
            client_request_id,
            f"digest:command:{client_request_id}",
        )
        if created and command.tag == "Stop" and self.settle_stops:
            self.store.settle_stop_if_idle(instance_id)
            state = self.store.inspect(instance_id)
        return state

    def shutdown(self, timeout=None):
        del timeout
        self.shutdown_calls += 1


def test_packaged_date_as_yun_persona_is_exact_and_truthfully_versioned() -> None:
    persona = load_date_as_yun_persona()

    assert persona.version == DATE_AS_YUN_PERSONA_VERSION
    assert hashlib.sha256(persona.prompt.encode("utf-8")).hexdigest() == (
        DATE_AS_YUN_PERSONA_SHA256
    )
    assert ".codex" not in str(persona.source_path).lower()
    assert "每个聊天或决策轮次都完整加载本文件" in persona.prompt
    assert "诚实说明这是帮账号本人先聊天的 AI" in persona.prompt
    assert "普通谈钱、见面、联系方式或关系不因关键词触发人工接管" in persona.prompt


def test_production_composition_is_lazy_uses_runtime_data_dir_and_live_cloud_resolver(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    owner = _Owner()
    runtime = _Runtime()
    captured: dict[str, object] = {}

    def runtime_factory(database_path, **kwargs):
        captured["database_path"] = Path(database_path)
        captured["kwargs"] = kwargs
        return runtime

    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=runtime_factory,
    )

    assert composition.archive.list() == []
    assert (settings.data_dir / APPLICATION_DATABASE_FILENAME).is_file()
    assert (settings.data_dir / REPLY_LEARNING_DATABASE_FILENAME).is_file()
    assert (
        settings.data_dir / SCHEDULER_LIFECYCLE_DATABASE_FILENAME
    ).is_file()
    assert owner.capability_calls == 0
    assert captured == {}

    with pytest.raises(SoulApplicationUnavailable):
        composition.runtime.start(PROFILE_ID, "start-before-cloud")
    assert owner.capability_calls == 0
    assert captured == {}

    first_provider = object()
    cloud.provider = first_provider
    assert composition.runtime.start(PROFILE_ID, "start-after-cloud") == {
        "started": True
    }
    assert owner.capability_calls == 1
    assert captured["database_path"] == (
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["profile"] == PROFILE_ID
    assert kwargs["memory_scope"] == PROFILE_ID
    policy = kwargs["policy"]
    assert policy.persona_prompt == load_date_as_yun_persona().prompt.strip()
    assert policy.persona_version == DATE_AS_YUN_PERSONA_VERSION
    assert policy.cloud_provider_resolver() is first_provider

    second_provider = object()
    cloud.provider = second_provider
    assert policy.cloud_provider_resolver() is second_provider

    composition.runtime.shutdown()
    assert runtime.shutdown_calls == 1


def test_production_defaults_reuse_loopback_owner_and_local_gui_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://localhost:4243/v1",
        local_chat_model="configured-gui-model",
        local_chat_api_key="local-key",
    )
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    owner = _Owner()
    owner_construction: list[tuple[str, float, float]] = []
    captured: dict[str, object] = {}

    def owner_factory(
        base_url: str,
        *,
        timeout_seconds: float,
        observation_timeout_seconds: float,
    ):
        owner_construction.append(
            (base_url, timeout_seconds, observation_timeout_seconds)
        )
        return owner

    def runtime_factory(database_path, **kwargs):
        captured["database_path"] = Path(database_path)
        captured["kwargs"] = kwargs
        return _Runtime()

    monkeypatch.setattr(composition_module, "SoulOwnerClient", owner_factory)
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        runtime_factory=runtime_factory,
    )

    assert owner_construction == [
        (
            "http://127.0.0.1:5000",
            settings.soul_request_timeout_seconds,
            settings.soul_observation_timeout_seconds,
        )
    ]
    assert owner.capability_calls == 0
    assert captured == {}

    composition.runtime.start(PROFILE_ID, "production-default-start")

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    observation_port = kwargs["observation_port"]
    assert observation_port.owner_client is owner
    assert observation_port.vision.endpoint == (
        "http://localhost:4243/v1/chat/completions"
    )
    assert observation_port.vision.model == "configured-gui-model"
    assert observation_port.vision.api_key == "local-key"
    assert owner.capability_calls == 1
    composition.runtime.shutdown()


def test_command_retries_activation_after_startup_dependency_outage(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    owner = _Owner()
    runtime = _Runtime()
    runtime_factory_calls = 0

    def runtime_factory(_database_path, **_kwargs):
        nonlocal runtime_factory_calls
        runtime_factory_calls += 1
        return runtime

    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=runtime_factory,
        activation_retry_delays=(10.0,),
    )

    composition.runtime.startup()
    assert runtime_factory_calls == 0

    cloud.provider = object()
    assert composition.runtime.command(
        "historical-instance", Pause(), "pause-after-outage"
    ) == {"commanded": True}
    assert runtime_factory_calls == 1
    assert runtime.command_calls == [
        (("historical-instance", Pause(), "pause-after-outage"), {})
    ]
    composition.runtime.shutdown()


def test_existing_runtime_controls_do_not_reprobe_temporarily_offline_dependencies(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    owner = _Owner()
    runtime = _Runtime()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
    )

    composition.runtime.start(PROFILE_ID, "load-runtime")
    resolve_calls = cloud.resolve_calls
    capability_calls = owner.capability_calls
    cloud.provider = None

    assert composition.runtime.command(
        "existing-instance", Pause(), "pause-while-offline"
    ) == {"commanded": True}
    assert composition.runtime.start(PROFILE_ID, "new-while-offline") == {
        "started": True
    }
    assert cloud.resolve_calls == resolve_calls
    assert owner.capability_calls == capability_calls
    composition.runtime.shutdown()


def test_gateway_controls_scheduler_only_for_explicit_lifecycle_commands(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    events: list[str] = []
    owner = _Owner(events)
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    runtime = _Runtime(events, stateful=True)
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
    )

    composition.runtime.start(PROFILE_ID, "scheduler-start")
    composition.runtime.command(
        "instance-1", Input("new instruction"), "scheduler-input"
    )
    composition.runtime.command(
        "instance-1", Pause(), "scheduler-pause"
    )
    composition.runtime.command(
        "instance-1", Resume(), "scheduler-resume"
    )
    composition.runtime.command(
        "instance-1", Stop(), "scheduler-stop"
    )
    calls_before_shutdown = list(owner.scheduler_calls)
    composition.runtime.shutdown()

    assert calls_before_shutdown == [
        ("GET", None),
        ("PUT", "running"),
        ("GET", None),
        ("PUT", "paused"),
        ("GET", None),
        ("PUT", "running"),
        ("GET", None),
        ("PUT", "stopped"),
    ]
    assert owner.scheduler_calls == calls_before_shutdown
    assert [call[0][1].tag for call in runtime.command_calls] == [
        "Input",
        "Pause",
        "Resume",
        "Stop",
    ]
    assert events == [
        "runtime:start",
        "scheduler:get",
        "scheduler:running",
        "runtime:input",
        "runtime:pause",
        "scheduler:get",
        "scheduler:paused",
        "runtime:resume",
        "scheduler:get",
        "scheduler:running",
        "runtime:stop",
        "scheduler:get",
        "scheduler:stopped",
    ]


def test_scheduler_control_failure_is_retryable_with_same_runtime_request_id(
    tmp_path: Path,
) -> None:
    class FailingSchedulerOwner(_Owner):
        def __init__(self):
            super().__init__()
            self.failures_remaining = 1

        def set_scheduler_state(self, desired_state: str):
            self.scheduler_calls.append(("PUT", desired_state))
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise OSError("temporary scheduler outage")
            return {
                "contract_version": "v1",
                "controller_ref": "ai-game-soul-reply-v1",
                "desired_state": desired_state,
                "effective_state": desired_state,
                "reply_owner": "application_runtime",
                "scheduler_mode": "match",
            }

    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    owner = FailingSchedulerOwner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    runtime = _Runtime(stateful=True)
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
    )

    with pytest.raises(SoulApplicationUnavailable):
        composition.runtime.start(PROFILE_ID, "scheduler-failed-start")
    replayed = composition.runtime.start(
        PROFILE_ID, "scheduler-failed-start"
    )
    composition.runtime.shutdown()

    assert owner.scheduler_calls == [
        ("GET", None),
        ("PUT", "running"),
        ("GET", None),
        ("PUT", "running"),
    ]
    assert replayed["started"] is True
    assert [call[0][1] for call in runtime.start_calls] == [
        "scheduler-failed-start",
        "scheduler-failed-start",
    ]
    assert runtime.command_calls == []


def test_scheduler_status_projection_is_content_safe_and_reports_mismatch(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    owner = _Owner()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: _Runtime(),
    )

    running = composition.runtime.scheduler_status()
    owner.scheduler_controller_ref = "different-controller"
    mismatched = composition.runtime.scheduler_status()
    composition.runtime.shutdown()

    assert running["state"] == "running"
    assert running["controller_matches"] is True
    assert running["code"] == "scheduler_running"
    assert running["observed_at"].endswith("Z")
    assert mismatched["state"] == "degraded"
    assert mismatched["controller_matches"] is False
    assert mismatched["code"] == "scheduler_controller_mismatch"
    assert "controller_ref" not in running
    assert "identity" not in repr(running).lower()


def test_scheduler_status_has_a_read_only_application_profile_api(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    owner = _Owner()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: _Runtime(),
    )
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        application_runtime=composition.runtime,
        application_runtime_archive=composition.archive,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/application-profiles/soul-reply-v1/scheduler"
        )

    assert response.status_code == 200
    assert response.json()["state"] == "running"
    assert set(response.json()) == {
        "profile_id",
        "state",
        "desired_state",
        "effective_state",
        "controller_matches",
        "code",
        "observed_at",
    }


def test_partial_lifespan_startup_failure_cleans_application_activation_thread(
    tmp_path: Path,
) -> None:
    class ActivationRuntime:
        def __init__(self):
            self.stop = threading.Event()
            self.thread: threading.Thread | None = None
            self.shutdown_calls = 0

        def startup(self):
            self.thread = threading.Thread(
                target=self.stop.wait,
                name="test-soul-activation",
                daemon=True,
            )
            self.thread.start()

        def shutdown(self):
            self.shutdown_calls += 1
            self.stop.set()
            assert self.thread is not None
            self.thread.join(1.0)

        def list(self, limit=100):
            del limit
            return []

    class StartedChat:
        def __init__(self):
            self.shutdown_calls = 0

        def start(self):
            return None

        def shutdown(self):
            self.shutdown_calls += 1

    class FailingGame:
        def __init__(self):
            self.shutdown_calls = 0

        def start(self):
            raise RuntimeError("game startup failed")

        def shutdown(self):
            self.shutdown_calls += 1
            raise RuntimeError("game partial cleanup failed")

    application = ActivationRuntime()
    chat = StartedChat()
    game = FailingGame()
    settings = build_settings(tmp_path)
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
        application_runtime=application,
        application_runtime_archive=application,
        chat_coordinator=chat,
        game_learner=game,
    )

    with pytest.raises(RuntimeError, match="game startup failed"):
        with TestClient(app):
            pass

    assert application.shutdown_calls == 1
    assert application.thread is not None
    assert application.thread.is_alive() is False
    assert chat.shutdown_calls == 1
    assert game.shutdown_calls == 1


def _ready_composition(
    tmp_path: Path,
    *,
    owner: _Owner,
    runtime,
    lifecycle_poll_seconds: float = 0.01,
):
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    return compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
        lifecycle_poll_seconds=lifecycle_poll_seconds,
        lifecycle_retry_delays=(0.01,),
    )


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_lifecycle_startup_without_durable_or_active_instance_never_starts_scheduler(
    tmp_path: Path,
) -> None:
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    runtime = _Runtime()
    composition = _ready_composition(
        tmp_path,
        owner=owner,
        runtime=runtime,
    )

    composition.runtime.startup()
    time.sleep(0.05)
    composition.runtime.shutdown()

    assert runtime.start_calls == []
    assert runtime.command_calls == []
    assert owner.scheduler_calls == []


@pytest.mark.parametrize("missing_dependency", ["cloud", "local_vision"])
def test_matcher_reconciler_is_independent_from_reply_dependencies(
    tmp_path: Path,
    missing_dependency: str,
) -> None:
    base_settings = build_settings(tmp_path)
    settings = replace(
        base_settings,
        local_chat_endpoint=(
            None
            if missing_dependency == "local_vision"
            else "http://127.0.0.1:4243/v1"
        ),
        local_chat_model=(
            None if missing_dependency == "local_vision" else "gui-owl-local"
        ),
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        f"offline-reply-{missing_dependency}",
        PROFILE_ID,
        None,
        None,
        f"offline-reply-{missing_dependency}-request",
        f"offline-reply-{missing_dependency}-digest",
    )
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    cloud = _MutableCloudConfiguration()
    if missing_dependency != "cloud":
        cloud.provider = object()
    runtime_factory_calls = 0

    def runtime_factory(_database_path, **_kwargs):
        nonlocal runtime_factory_calls
        runtime_factory_calls += 1
        return _Runtime()

    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=None if missing_dependency == "local_vision" else _Vision(),
        runtime_factory=runtime_factory,
        activation_retry_delays=(10.0,),
        lifecycle_poll_seconds=0.01,
        lifecycle_retry_delays=(0.01,),
    )

    composition.runtime.startup()
    assert ("PUT", "running") in owner.scheduler_calls
    composition.runtime.shutdown()

    assert runtime_factory_calls == 0


def test_lifecycle_reconciler_repairs_dating_restart_without_new_instance(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "active-before-ai-restart",
        PROFILE_ID,
        None,
        None,
        "active-before-ai-restart-request",
        "active-before-ai-restart-digest",
    )
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    runtime = _Runtime()
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
        lifecycle_poll_seconds=0.01,
        lifecycle_retry_delays=(0.01,),
    )

    composition.runtime.startup()
    assert ("PUT", "running") in owner.scheduler_calls
    first_running_puts = owner.scheduler_calls.count(("PUT", "running"))

    # Simulate a reverse restart: dating-copilot lost its in-memory worker,
    # while the AI core and durable desired receipt remain alive.
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    assert _wait_until(
        lambda: owner.scheduler_calls.count(("PUT", "running"))
        > first_running_puts
    )
    calls_before_shutdown = list(owner.scheduler_calls)
    composition.runtime.shutdown()

    assert runtime.start_calls == []
    assert runtime.command_calls == []
    assert not any(
        call == ("PUT", "stopped")
        for call in owner.scheduler_calls[len(calls_before_shutdown) :]
    )


def test_paused_instance_recovers_cold_without_briefly_starting_matcher(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "paused-before-restart",
        PROFILE_ID,
        None,
        None,
        "paused-start-request",
        "paused-start-digest",
    )
    store.accept_command(
        "paused-before-restart",
        "Pause",
        None,
        "paused-command-request",
        "paused-command-digest",
    )
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: _Runtime(),
        lifecycle_poll_seconds=0.01,
    )

    composition.runtime.startup()
    status = composition.runtime.scheduler_status()
    composition.runtime.shutdown()

    assert ("PUT", "paused") in owner.scheduler_calls
    assert ("PUT", "running") not in owner.scheduler_calls
    assert status["state"] == "paused"
    assert status["effective_state"] == "stopped"
    assert status["code"] == "scheduler_paused_cold"


def test_shared_matcher_running_instance_overrides_newer_paused_instance(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "running-before-newer-pause",
        PROFILE_ID,
        None,
        None,
        "running-before-newer-pause-start",
        "running-before-newer-pause-digest",
    )
    store.accept_start(
        "newer-paused-instance",
        PROFILE_ID,
        None,
        None,
        "newer-paused-start",
        "newer-paused-start-digest",
    )
    store.accept_command(
        "newer-paused-instance",
        "Pause",
        None,
        "newer-paused-command",
        "newer-paused-command-digest",
    )
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    composition = _ready_composition(
        tmp_path,
        owner=owner,
        runtime=_Runtime(),
        lifecycle_poll_seconds=10.0,
    )

    composition.runtime.startup()
    composition.runtime.shutdown()

    assert ("PUT", "running") in owner.scheduler_calls
    assert ("PUT", "paused") not in owner.scheduler_calls


def test_later_failed_start_prevents_older_stop_from_stopping_matcher(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "older-explicit-stop",
        PROFILE_ID,
        None,
        None,
        "older-explicit-stop-start",
        "older-explicit-stop-start-digest",
    )
    store.accept_command(
        "older-explicit-stop",
        "Stop",
        None,
        "older-explicit-stop-command",
        "older-explicit-stop-command-digest",
    )
    assert store.settle_stop_if_idle("older-explicit-stop") is True
    store.accept_start(
        "newer-failed-start",
        PROFILE_ID,
        None,
        None,
        "newer-failed-start-request",
        "newer-failed-start-digest",
    )
    store.fail_cycle(
        "newer-failed-start",
        None,
        "simulated_reply_failure",
        "test",
    )
    owner = _Owner()
    composition = _ready_composition(
        tmp_path,
        owner=owner,
        runtime=_Runtime(),
        lifecycle_poll_seconds=10.0,
    )

    composition.runtime.startup()
    composition.runtime.shutdown()

    assert ("PUT", "stopped") not in owner.scheduler_calls
    record = SQLiteSoulSchedulerLifecycleStore(
        settings.data_dir / SCHEDULER_LIFECYCLE_DATABASE_FILENAME
    ).get()
    assert record is not None
    assert record.desired_state == "running"


def test_cold_stopping_idle_instance_settles_without_reply_dependencies(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "cold-idle-stop",
        PROFILE_ID,
        None,
        None,
        "cold-idle-stop-start",
        "cold-idle-stop-start-digest",
    )
    store.accept_command(
        "cold-idle-stop",
        "Stop",
        None,
        "cold-idle-stop-command",
        "cold-idle-stop-command-digest",
    )
    owner = _Owner()
    runtime_factory_calls = 0

    def runtime_factory(_database_path, **_kwargs):
        nonlocal runtime_factory_calls
        runtime_factory_calls += 1
        return _Runtime()

    composition = compose_soul_application_runtime(
        settings,
        _MutableCloudConfiguration(),
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=runtime_factory,
        activation_retry_delays=(10.0,),
        lifecycle_poll_seconds=10.0,
    )

    composition.runtime.startup()
    composition.runtime.shutdown()

    assert runtime_factory_calls == 0
    assert store.inspect("cold-idle-stop").status == "stopped"
    assert ("PUT", "stopped") in owner.scheduler_calls


def test_cold_stopping_open_intent_waits_for_owner_settlement(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    instance, _created = store.accept_start(
        "cold-open-intent-stop",
        PROFILE_ID,
        None,
        None,
        "cold-open-intent-start",
        "cold-open-intent-start-digest",
    )
    assert store.claim(instance.instance_id, "crashed-worker") is True
    cycle, revision = store.begin_cycle(instance.instance_id, "crashed-worker") or (
        None,
        None,
    )
    assert isinstance(cycle, int) and isinstance(revision, int)
    assert store.persist_intent(
        "cold-open-owner-intent",
        instance.instance_id,
        cycle,
        revision,
        Intent("reply", {"opaque": "test"}),
    )
    store.release(instance.instance_id, "crashed-worker")
    store.accept_command(
        instance.instance_id,
        "Stop",
        None,
        "cold-open-stop-command",
        "cold-open-stop-command-digest",
    )
    lifecycle = SQLiteSoulSchedulerLifecycleStore(
        settings.data_dir / SCHEDULER_LIFECYCLE_DATABASE_FILENAME
    )
    lifecycle.record(
        requested_state="running",
        desired_state="running",
        source_instance_id=instance.instance_id,
        transition_ref=hashlib.sha256(b"cold-open-running").hexdigest(),
    )
    owner = _Owner()
    composition = compose_soul_application_runtime(
        settings,
        _MutableCloudConfiguration(),
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: _Runtime(),
        activation_retry_delays=(10.0,),
        lifecycle_poll_seconds=10.0,
    )

    composition.runtime.startup()
    composition.runtime.shutdown()

    assert store.inspect(instance.instance_id).status == "stopping"
    assert ("PUT", "stopped") not in owner.scheduler_calls
    assert lifecycle.get() is not None
    assert lifecycle.get().desired_state == "running"


def test_cold_stopping_open_intent_holds_its_pre_stop_paused_target(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    instance, _created = store.accept_start(
        "cold-paused-open-stop",
        PROFILE_ID,
        None,
        None,
        "cold-paused-open-start",
        "cold-paused-open-start-digest",
    )
    assert store.claim(instance.instance_id, "crashed-worker") is True
    cycle, revision = store.begin_cycle(instance.instance_id, "crashed-worker") or (
        None,
        None,
    )
    assert isinstance(cycle, int) and isinstance(revision, int)
    assert store.persist_intent(
        "cold-paused-owner-intent",
        instance.instance_id,
        cycle,
        revision,
        Intent("reply", {"opaque": "test"}),
    )
    store.release(instance.instance_id, "crashed-worker")
    store.accept_command(
        instance.instance_id,
        "Pause",
        None,
        "cold-paused-command",
        "cold-paused-command-digest",
    )
    store.accept_command(
        instance.instance_id,
        "Stop",
        None,
        "cold-paused-stop-command",
        "cold-paused-stop-command-digest",
    )
    owner = _Owner()
    composition = compose_soul_application_runtime(
        settings,
        _MutableCloudConfiguration(),
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: _Runtime(),
        activation_retry_delays=(10.0,),
        lifecycle_poll_seconds=10.0,
    )

    composition.runtime.startup()
    composition.runtime.shutdown()

    assert store.inspect(instance.instance_id).status == "stopping"
    assert ("PUT", "paused") in owner.scheduler_calls
    assert ("PUT", "stopped") not in owner.scheduler_calls


def test_cold_stopping_worker_token_is_not_cleared_by_lifecycle_archive(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    instance, _created = store.accept_start(
        "cold-worker-owned-stop",
        PROFILE_ID,
        None,
        None,
        "cold-worker-owned-start",
        "cold-worker-owned-start-digest",
    )
    assert store.claim(instance.instance_id, "possibly-live-worker") is True
    store.accept_command(
        instance.instance_id,
        "Stop",
        None,
        "cold-worker-owned-stop-command",
        "cold-worker-owned-stop-command-digest",
    )
    lifecycle = SQLiteSoulSchedulerLifecycleStore(
        settings.data_dir / SCHEDULER_LIFECYCLE_DATABASE_FILENAME
    )
    lifecycle.record(
        requested_state="running",
        desired_state="running",
        source_instance_id=instance.instance_id,
        transition_ref=hashlib.sha256(b"cold-worker-running").hexdigest(),
    )
    owner = _Owner()
    composition = compose_soul_application_runtime(
        settings,
        _MutableCloudConfiguration(),
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: _Runtime(),
        activation_retry_delays=(10.0,),
        lifecycle_poll_seconds=10.0,
    )

    composition.runtime.startup()
    composition.runtime.shutdown()

    assert store.inspect(instance.instance_id).status == "stopping"
    assert ("PUT", "stopped") not in owner.scheduler_calls


def test_stop_waits_for_core_stopped_before_scheduler_stop(tmp_path: Path) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    runtime = _StoreBackedRuntime(store, settle_stops=False)
    owner = _Owner()
    owner.scheduler_desired_state = "running"
    owner.scheduler_effective_state = "running"
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
        lifecycle_poll_seconds=0.01,
    )
    composition.runtime.startup()
    started = composition.runtime.start(PROFILE_ID, "stop-gate-start")
    stopped = composition.runtime.command(
        started.instance_id,
        Stop(),
        "stop-gate-command",
    )

    assert stopped.status == "stopping"
    assert ("PUT", "stopped") not in owner.scheduler_calls
    assert store.settle_stop_if_idle(started.instance_id) is True
    assert _wait_until(lambda: ("PUT", "stopped") in owner.scheduler_calls)
    composition.runtime.shutdown()


def test_stop_that_settles_failed_does_not_stop_all_day_matcher(tmp_path: Path) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    runtime = _StoreBackedRuntime(store, settle_stops=False)
    owner = _Owner()
    owner.scheduler_desired_state = "running"
    owner.scheduler_effective_state = "running"
    composition = _ready_composition(
        tmp_path,
        owner=owner,
        runtime=runtime,
    )
    composition.runtime.startup()
    started = composition.runtime.start(PROFILE_ID, "failed-stop-start")
    composition.runtime.command(
        started.instance_id,
        Stop(),
        "failed-stop-command",
    )
    store.fail_cycle(
        started.instance_id,
        None,
        "simulated_owner_uncertain",
        "recovery",
    )
    time.sleep(0.05)
    composition.runtime.shutdown()

    assert ("PUT", "stopped") not in owner.scheduler_calls


def test_cold_failed_stop_without_lifecycle_receipt_restores_pre_stop_target(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "cold-failed-stop",
        PROFILE_ID,
        None,
        None,
        "cold-failed-stop-start",
        "cold-failed-stop-start-digest",
    )
    store.accept_command(
        "cold-failed-stop",
        "Stop",
        None,
        "cold-failed-stop-command",
        "cold-failed-stop-command-digest",
    )
    store.fail_cycle(
        "cold-failed-stop",
        None,
        "simulated_owner_uncertain",
        "recovery",
    )
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    composition = _ready_composition(
        tmp_path,
        owner=owner,
        runtime=_Runtime(),
        lifecycle_poll_seconds=10.0,
    )

    composition.runtime.startup()
    composition.runtime.shutdown()

    assert ("PUT", "running") in owner.scheduler_calls
    assert ("PUT", "stopped") not in owner.scheduler_calls


def test_stale_lifecycle_request_replays_never_roll_back_newer_state(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    runtime = _StoreBackedRuntime(store)
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
        lifecycle_poll_seconds=10.0,
    )
    first = composition.runtime.start(PROFILE_ID, "ordering-start-a")
    composition.runtime.command(first.instance_id, Pause(), "ordering-pause-a")
    composition.runtime.command(first.instance_id, Resume(), "ordering-resume-b")
    paused_puts = owner.scheduler_calls.count(("PUT", "paused"))

    composition.runtime.command(first.instance_id, Pause(), "ordering-pause-a")
    assert owner.scheduler_calls.count(("PUT", "paused")) == paused_puts
    assert owner.scheduler_desired_state == "running"

    composition.runtime.command(first.instance_id, Stop(), "ordering-stop-a")
    second = composition.runtime.start(PROFILE_ID, "ordering-start-b")
    stopped_puts = owner.scheduler_calls.count(("PUT", "stopped"))
    composition.runtime.command(first.instance_id, Stop(), "ordering-stop-a")
    composition.runtime.shutdown()

    assert second.instance_id != first.instance_id
    assert owner.scheduler_calls.count(("PUT", "stopped")) == stopped_puts
    assert owner.scheduler_desired_state == "running"


def test_lifecycle_archive_scan_does_not_lose_old_active_behind_500_history(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "old-active-paused",
        PROFILE_ID,
        None,
        None,
        "old-active-start",
        "old-active-start-digest",
    )
    store.accept_command(
        "old-active-paused",
        "Pause",
        None,
        "old-active-pause",
        "old-active-pause-digest",
    )
    for index in range(501):
        instance_id = f"newer-failed-{index:03d}"
        store.accept_start(
            instance_id,
            PROFILE_ID,
            None,
            None,
            f"newer-failed-request-{index:03d}",
            f"newer-failed-digest-{index:03d}",
        )
        store.fail_cycle(instance_id, None, "simulated", "test")
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: _Runtime(),
        lifecycle_poll_seconds=10.0,
    )

    composition.runtime.startup()
    composition.runtime.shutdown()

    assert ("PUT", "paused") in owner.scheduler_calls
    assert ("PUT", "running") not in owner.scheduler_calls


def test_lifecycle_store_closes_every_sqlite_handle(tmp_path: Path) -> None:
    database = tmp_path / "scheduler-lifecycle.db"
    store = SQLiteSoulSchedulerLifecycleStore(database)

    for index in range(100):
        store.record(
            requested_state="running",
            desired_state="running",
            source_instance_id="instance-1",
            transition_ref=hashlib.sha256(str(index).encode("ascii")).hexdigest(),
        )
        assert store.get() is not None

    moved = tmp_path / "scheduler-lifecycle-moved.db"
    database.rename(moved)
    assert moved.is_file() and not database.exists()


def test_monitor_recovers_owner_get_outage_without_replaying_core_work(
    tmp_path: Path,
) -> None:
    class FlakySchedulerOwner(_Owner):
        def __init__(self):
            super().__init__()
            self.failures_remaining = 2

        def scheduler(self):
            self.scheduler_calls.append(("GET", None))
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise OSError("temporary owner outage")
            return super().scheduler()

    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "owner-outage-active",
        PROFILE_ID,
        None,
        None,
        "owner-outage-start",
        "owner-outage-digest",
    )
    owner = FlakySchedulerOwner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    runtime = _Runtime()
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
        lifecycle_poll_seconds=0.01,
        lifecycle_retry_delays=(0.01,),
    )

    composition.runtime.startup()
    assert _wait_until(lambda: ("PUT", "running") in owner.scheduler_calls)
    composition.runtime.shutdown()

    assert runtime.start_calls == []
    assert runtime.command_calls == []
    assert owner.scheduler_calls.count(("PUT", "running")) == 1


def test_put_timeout_that_committed_is_confirmed_by_get_without_duplicate_put(
    tmp_path: Path,
) -> None:
    class CommitThenTimeoutOwner(_Owner):
        def __init__(self):
            super().__init__()
            self.timeout_once = True

        def set_scheduler_state(self, desired_state: str):
            response = super().set_scheduler_state(desired_state)
            if self.timeout_once:
                self.timeout_once = False
                raise OSError("response lost after commit")
            return response

    owner = CommitThenTimeoutOwner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    runtime = _Runtime(stateful=True)
    composition = _ready_composition(
        tmp_path,
        owner=owner,
        runtime=runtime,
    )

    with pytest.raises(SoulApplicationUnavailable):
        composition.runtime.start(PROFILE_ID, "commit-timeout-start")
    assert _wait_until(
        lambda: owner.scheduler_calls.count(("GET", None)) >= 2
    )
    composition.runtime.shutdown()

    assert owner.scheduler_calls.count(("PUT", "running")) == 1
    assert owner.scheduler_desired_state == "running"


def test_shutdown_between_scheduler_get_and_put_prevents_new_put(
    tmp_path: Path,
) -> None:
    class BlockingSchedulerOwner(_Owner):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def scheduler(self):
            self.scheduler_calls.append(("GET", None))
            self.entered.set()
            assert self.release.wait(1.0)
            return {
                "contract_version": "v1",
                "controller_ref": None,
                "desired_state": "stopped",
                "effective_state": "stopped",
                "reply_owner": "none",
                "scheduler_mode": None,
            }

    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    store.accept_start(
        "shutdown-race-active",
        PROFILE_ID,
        None,
        None,
        "shutdown-race-start",
        "shutdown-race-digest",
    )
    owner = BlockingSchedulerOwner()
    runtime = _Runtime()
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
        lifecycle_poll_seconds=10.0,
    )
    startup_thread = threading.Thread(target=composition.runtime.startup)
    startup_thread.start()
    assert owner.entered.wait(1.0)

    composition.runtime.shutdown()
    owner.release.set()
    startup_thread.join(1.0)

    assert startup_thread.is_alive() is False
    assert not any(method == "PUT" for method, _state in owner.scheduler_calls)


def test_shutdown_closes_while_dependency_probe_is_blocked_and_prevents_factory(
    tmp_path: Path,
) -> None:
    probe_entered = threading.Event()
    probe_release = threading.Event()
    shutdown_started = threading.Event()
    shutdown_finished = threading.Event()
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    archive = SQLiteApplicationArchive(tmp_path / "application-runtime.db")
    lifecycle = SQLiteSoulSchedulerLifecycleStore(
        tmp_path / "soul-scheduler-lifecycle.db"
    )
    runtime_factory_calls = 0
    start_errors: list[BaseException] = []
    shutdown_errors: list[BaseException] = []

    def dependency_probe() -> None:
        probe_entered.set()
        assert probe_release.wait(5.0)

    def runtime_factory():
        nonlocal runtime_factory_calls
        runtime_factory_calls += 1
        return _Runtime(stateful=True)

    gateway = SoulApplicationRuntimeGateway(
        archive=archive,
        dependency_probe=dependency_probe,
        runtime_factory=runtime_factory,
        scheduler_reader=owner.scheduler,
        scheduler_controller=owner.set_scheduler_state,
        scheduler_lifecycle_store=lifecycle,
        activation_retry_delays=(10.0,),
        lifecycle_poll_seconds=10.0,
        lifecycle_retry_delays=(10.0,),
    )

    def start() -> None:
        try:
            gateway.start(PROFILE_ID, "blocked-probe-start")
        except BaseException as error:
            start_errors.append(error)

    def shutdown() -> None:
        shutdown_started.set()
        try:
            gateway.shutdown()
        except BaseException as error:
            shutdown_errors.append(error)
        finally:
            shutdown_finished.set()

    start_thread = threading.Thread(target=start)
    shutdown_thread = threading.Thread(target=shutdown)
    start_thread.start()
    assert probe_entered.wait(1.0)
    shutdown_thread.start()
    assert shutdown_started.wait(1.0)
    try:
        assert shutdown_finished.wait(1.0)
    finally:
        probe_release.set()
        start_thread.join(1.0)
        shutdown_thread.join(1.0)

    assert shutdown_errors == []
    assert len(start_errors) == 1
    assert isinstance(start_errors[0], RuntimeClosed)
    assert runtime_factory_calls == 0
    assert owner.scheduler_calls == []


def test_shutdown_during_runtime_factory_discards_candidate_without_owner_put(
    tmp_path: Path,
) -> None:
    factory_entered = threading.Event()
    factory_release = threading.Event()
    shutdown_finished = threading.Event()
    owner = _Owner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    candidate = _Runtime(stateful=True)
    start_errors: list[BaseException] = []
    shutdown_errors: list[BaseException] = []
    gateway = SoulApplicationRuntimeGateway(
        archive=SQLiteApplicationArchive(tmp_path / "application-runtime.db"),
        dependency_probe=lambda: None,
        runtime_factory=lambda: _blocking_runtime_factory(
            factory_entered,
            factory_release,
            candidate,
        ),
        scheduler_reader=owner.scheduler,
        scheduler_controller=owner.set_scheduler_state,
        scheduler_lifecycle_store=SQLiteSoulSchedulerLifecycleStore(
            tmp_path / "soul-scheduler-lifecycle.db"
        ),
        activation_retry_delays=(10.0,),
        lifecycle_poll_seconds=10.0,
        lifecycle_retry_delays=(10.0,),
    )

    def start() -> None:
        try:
            gateway.start(PROFILE_ID, "blocked-factory-start")
        except BaseException as error:
            start_errors.append(error)

    def shutdown() -> None:
        try:
            gateway.shutdown()
        except BaseException as error:
            shutdown_errors.append(error)
        finally:
            shutdown_finished.set()

    start_thread = threading.Thread(target=start)
    start_thread.start()
    assert factory_entered.wait(1.0)
    shutdown_thread = threading.Thread(target=shutdown)
    shutdown_thread.start()
    try:
        assert shutdown_finished.wait(1.0)
    finally:
        factory_release.set()
        start_thread.join(1.0)
        shutdown_thread.join(1.0)

    assert len(start_errors) == 1
    assert isinstance(start_errors[0], RuntimeClosed)
    assert shutdown_errors == []
    assert candidate.shutdown_calls == 1
    assert candidate.start_calls == []
    assert owner.scheduler_calls == []


def _blocking_runtime_factory(
    entered: threading.Event,
    release: threading.Event,
    runtime: _Runtime,
) -> _Runtime:
    entered.set()
    assert release.wait(5.0)
    return runtime


def test_lifecycle_get_put_is_single_flight_with_newer_resume(
    tmp_path: Path,
) -> None:
    class BlockingOnceOwner(_Owner):
        def __init__(self):
            super().__init__()
            self.block_next_get = False
            self.entered = threading.Event()
            self.release = threading.Event()

        def scheduler(self):
            result = super().scheduler()
            if self.block_next_get:
                self.block_next_get = False
                self.entered.set()
                assert self.release.wait(1.0)
            return result

    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    store = _SQLiteApplicationStore(
        settings.data_dir / APPLICATION_DATABASE_FILENAME
    )
    runtime = _StoreBackedRuntime(store)
    owner = BlockingOnceOwner()
    owner.scheduler_desired_state = "stopped"
    owner.scheduler_effective_state = "stopped"
    owner.scheduler_controller_ref = None
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
        lifecycle_poll_seconds=10.0,
    )
    instance = composition.runtime.start(PROFILE_ID, "single-flight-start")
    owner.block_next_get = True
    pause_thread = threading.Thread(
        target=lambda: composition.runtime.command(
            instance.instance_id,
            Pause(),
            "single-flight-pause",
        )
    )
    resume_thread = threading.Thread(
        target=lambda: composition.runtime.command(
            instance.instance_id,
            Resume(),
            "single-flight-resume",
        )
    )

    pause_thread.start()
    assert owner.entered.wait(1.0)
    resume_thread.start()
    time.sleep(0.03)
    assert [call[1].tag for call in runtime.command_calls] == ["Pause"]
    owner.release.set()
    pause_thread.join(1.0)
    resume_thread.join(1.0)
    composition.runtime.shutdown()

    assert pause_thread.is_alive() is False
    assert resume_thread.is_alive() is False
    assert [call[1].tag for call in runtime.command_calls] == [
        "Pause",
        "Resume",
    ]
    assert owner.scheduler_desired_state == "running"
    last_paused = max(
        index
        for index, call in enumerate(owner.scheduler_calls)
        if call == ("PUT", "paused")
    )
    last_running = max(
        index
        for index, call in enumerate(owner.scheduler_calls)
        if call == ("PUT", "running")
    )
    assert last_running > last_paused


def test_startup_background_retry_activates_without_a_later_http_write(
    tmp_path: Path,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    owner = _Owner()
    runtime = _Runtime()
    runtime_factory_calls = 0

    def runtime_factory(_database_path, **_kwargs):
        nonlocal runtime_factory_calls
        runtime_factory_calls += 1
        return runtime

    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=owner,
        vision=_Vision(),
        runtime_factory=runtime_factory,
        activation_retry_delays=(0.01,),
    )

    composition.runtime.startup()
    assert runtime_factory_calls == 0
    cloud.provider = object()
    deadline = time.monotonic() + 1.0
    while runtime_factory_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime_factory_calls == 1
    assert owner.capability_calls == 1
    composition.runtime.shutdown()


def test_shutdown_keeps_runtime_reference_after_timeout_for_a_repeat_join(
    tmp_path: Path,
) -> None:
    class _TimeoutOnceRuntime(_Runtime):
        def shutdown(self, timeout: float | None = None) -> None:
            super().shutdown(timeout)
            if self.shutdown_calls == 1:
                raise TimeoutError("simulated in-flight model call")

    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
    )
    cloud = _MutableCloudConfiguration()
    cloud.provider = object()
    runtime = _TimeoutOnceRuntime()
    composition = compose_soul_application_runtime(
        settings,
        cloud,
        owner_client=_Owner(),
        vision=_Vision(),
        runtime_factory=lambda _database_path, **_kwargs: runtime,
    )
    composition.runtime.start(PROFILE_ID, "load-before-shutdown")

    with pytest.raises(TimeoutError, match="simulated in-flight model call"):
        composition.runtime.shutdown()
    composition.runtime.shutdown()
    composition.runtime.shutdown()

    assert runtime.shutdown_calls == 2
    assert runtime.shutdown_timeouts == [85.0, 85.0]


def test_default_app_keeps_history_visible_but_new_start_is_stable_503_offline(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)
    application_db = settings.data_dir / APPLICATION_DATABASE_FILENAME
    store = _SQLiteApplicationStore(application_db)
    store.accept_start(
        "historical-instance",
        PROFILE_ID,
        None,
        None,
        "historical-request",
        "historical-digest",
    )

    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )
    with TestClient(app) as client:
        listed = client.get("/api/v1/application-instances")
        inspected = client.get(
            "/api/v1/application-instances/historical-instance"
        )
        unavailable = client.post(
            "/api/v1/application-instances",
            headers=WRITE_HEADERS,
            json={
                "profile_id": PROFILE_ID,
                "client_request_id": "offline-new-start",
            },
        )

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [
        "historical-instance"
    ]
    assert inspected.status_code == 200
    assert inspected.json()["id"] == "historical-instance"
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "error": {
            "code": "soul_application_dependencies_unavailable",
            "message": "Soul Application 运行依赖当前不可用。",
        }
    }
    assert (settings.data_dir / REPLY_LEARNING_DATABASE_FILENAME).is_file()


def test_default_lifespan_recovers_same_active_instance_when_dependencies_are_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        build_settings(tmp_path),
        local_chat_endpoint="http://127.0.0.1:4243/v1",
        local_chat_model="gui-owl-local",
        cloud_chat_endpoint="https://cloud.example.test/v1/chat/completions",
        cloud_chat_model="cloud-model",
        cloud_chat_api_key="cloud-key",
    )
    application_db = settings.data_dir / APPLICATION_DATABASE_FILENAME
    _SQLiteApplicationStore(application_db).accept_start(
        "active-before-restart",
        PROFILE_ID,
        None,
        None,
        "active-before-restart-request",
        "active-before-restart-digest",
    )
    owner = _Owner()

    def owner_factory(
        base_url: str,
        *,
        timeout_seconds: float,
        observation_timeout_seconds: float,
    ):
        assert base_url == "http://127.0.0.1:5000"
        assert timeout_seconds == settings.soul_request_timeout_seconds
        assert observation_timeout_seconds == settings.soul_observation_timeout_seconds
        return owner

    monkeypatch.setattr(composition_module, "SoulOwnerClient", owner_factory)
    app = create_app(
        settings=settings,
        adb_discovery=AdbTargetDiscovery(env={"PATH": ""}),
    )

    with TestClient(app) as client:
        deadline = time.monotonic() + 2.0
        state = None
        while time.monotonic() < deadline:
            response = client.get(
                "/api/v1/application-instances/active-before-restart"
            )
            assert response.status_code == 200
            state = response.json()
            if state["status"] == "waiting":
                break
            time.sleep(0.01)
        listed = client.get("/api/v1/application-instances")

    assert state is not None and state["status"] == "waiting"
    assert [item["id"] for item in listed.json()["items"]] == [
        "active-before-restart"
    ]
    assert owner.capability_calls == 1
    assert owner.observation_calls >= 1
