from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_game_console.device_lease import DeviceExecutionLease, TargetBusyError
from ai_game_console.api import create_app
from ai_game_console.discovery import AdbTargetDiscovery
from ai_game_console.device_runs import DeviceRunError, DeviceRunService, DeviceRunStore, create_device_runs_router
from ai_game_console.device_runs.ui_tree import compact_ui_tree
from ai_game_console.emulator_runtime.general_production import ProductionAndroidUiRunner
from ai_game_console.execution import ActionTransportResult
from ai_game_console.execution_contract.api import execution_contract_error_handler
from ai_game_console.execution_contract.service import ExecutionContractError
from ai_game_console.runtime_kernel.observation import (
    ChannelAvailability, ConnectionState, ConsistencyStatus, DeviceState, KeyboardState,
    ObservationConsistency, Orientation, RawObservation, RawScreenshot, RawUiTree,
)

NOW = "2026-09-09T00:00:00+00:00"
OWNER = {"principal_id": "host-a", "controller_id": "controller-a"}
HEADERS = {"X-AI-Game-Client": "weftmate-harness-v1", "Authorization": "Bearer test-token",
           "X-AI-Game-Principal-Id": OWNER["principal_id"], "X-AI-Game-Controller-Id": OWNER["controller_id"]}
ROOT = "/api/execution/v2/device-runs"


def create_request(run_id="run-a"):
    return {"run_id": run_id, "device_profile_id": "profile-a", "authorization_mode": "full-access",
            "origin": {"dsh_session_id": "session-a", "dsh_turn_id": 1, "tool_call_id": "call-a", "root_call_id": "root-a"}}


class Profiles:
    def __init__(self):
        self.verifications = 0
        self.profile = SimpleNamespace(profile_generation=1, transport_serial="emulator-5554")

    def require_profile(self, **_):
        return self.profile

    def resolve_ready(self, **_):
        self.verifications += 1
        return self.profile


class Device:
    def __init__(self, lease):
        self.lease = lease
        self.actions = []
        self.screenshots = []
        self.effect = None
        self.ui_xml = '<hierarchy><node text="测试"/></hierarchy>'.encode()

    def execute(self, action):
        assert self.lease.is_held("emulator-5554")
        self.actions.append(action)
        if self.effect:
            self.effect(action)
        return ActionTransportResult(True, "Transport accepted; verify the interface separately.")

    def capture(self, serial, *, include_screenshot):
        assert self.lease.is_held(serial)
        self.screenshots.append(include_screenshot)
        return RawObservation(
            serial, NOW, NOW,
            RawScreenshot(ChannelAvailability.AVAILABLE, b"png-test", 1080, 1920, NOW),
            RawUiTree(ChannelAvailability.AVAILABLE, self.ui_xml, NOW),
            DeviceState(ChannelAvailability.AVAILABLE, "com.android.settings", (1080, 1920), Orientation.PORTRAIT,
                        KeyboardState.HIDDEN, ConnectionState.CONNECTED, NOW),
            ObservationConsistency(ConsistencyStatus.CONSISTENT, None),
        )


@pytest.fixture
def runtime(tmp_path):
    lease, profiles = DeviceExecutionLease(), Profiles()
    device = Device(lease)
    service = DeviceRunService(store=DeviceRunStore(tmp_path / "runs.db"), profiles=profiles, device_lease=lease,
                               executor_for_serial=lambda _: device, observation_for_serial=lambda _: device,
                               clock=lambda: NOW)
    yield SimpleNamespace(service=service, lease=lease, profiles=profiles, device=device, path=tmp_path / "runs.db")
    service.close()


def prepare(runtime):
    runtime.service.create(create_request(), OWNER)
    runtime.service.observe("run-a", OWNER, include_screenshot=False)


def test_authenticated_api_delivers_observation_and_reuses_batch_without_replanning(runtime):
    app = FastAPI()
    app.add_exception_handler(ExecutionContractError, execution_contract_error_handler)
    app.include_router(create_device_runs_router(runtime.service, token="test-token"))
    with TestClient(app) as client:
        assert client.post(ROOT, json=create_request()).status_code == 403
        assert runtime.profiles.verifications == 0
        created = client.post(ROOT, headers=HEADERS, json=create_request())
        assert created.status_code == 200, created.text
        assert created.json()["requires_observation"] is True
        observed = client.post(f"{ROOT}/run-a/observe", headers=HEADERS, json={}).json()
        assert observed["screenshot"] is None
        assert observed["ui_tree"]["format"] == "xml"
        assert "测试" in observed["ui_tree"]["xml"]
        compact = client.post(f"{ROOT}/run-a/observe", headers=HEADERS, json={"ui_tree_format": "compact"}).json()
        assert compact["ui_tree"]["format"] == "compact"
        assert compact["ui_tree"]["total"] == compact["ui_tree"]["returned"] == 1
        assert observed["device_state"]["foreground_app"] == "com.android.settings"
        screenshot = client.post(f"{ROOT}/run-a/observe", headers=HEADERS, json={"include_screenshot": True}).json()
        assert base64.b64decode(screenshot["screenshot"]["base64"]) == b"png-test"
        batch = {"command_id": "batch-a", "actions": [{"action": "open_app", "package": "com.example.test"},
                 {"action": "text", "text": "private-example-text"}, {"action": "tap", "x": 10, "y": 20}]}
        first = client.post(f"{ROOT}/run-a/actions", headers=HEADERS, json=batch)
        assert first.status_code == 200, first.text
        assert first.json()["accepted"] and first.json()["completed_count"] == 3
        second = client.post(f"{ROOT}/run-a/actions", headers=HEADERS, json=batch).json()
        assert second["replayed"] is True and len(runtime.device.actions) == 3
        assert runtime.profiles.verifications == 1
        altered = {**batch, "actions": [{"action": "recents"}]}
        conflict = client.post(f"{ROOT}/run-a/actions", headers=HEADERS, json=altered)
        assert conflict.status_code == 409 and conflict.json()["error"]["detail"]
        other = {**HEADERS, "X-AI-Game-Principal-Id": "host-b"}
        assert client.get(f"{ROOT}/run-a", headers=other).status_code == 404
    assert b"private-example-text" not in runtime.path.read_bytes()


def test_compact_observation_keeps_actionable_context_and_falls_back_to_xml_on_bad_input(runtime):
    runtime.device.ui_xml = b"""<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]" enabled="true">
        """ + b"".join(
            b'<node class="android.widget.LinearLayout" bounds="[0,0][1080,1920]" enabled="true"/>'
            for _ in range(80)
        ) + b"""
        <node class="android.widget.Button" clickable="true" enabled="true" bounds="[0,0][200,100]">
          <node class="android.widget.TextView" text="Settings &amp; \xe8\xae\xbe\xe7\xbd\xae" resource-id="com.example:id/title" enabled="true" bounds="[10,10][190,90]"/>
        </node>
        <node class="android.widget.ImageView" clickable="true" checkable="false" checked="false" enabled="true" bounds="[900,20][1060,180]"/>
        <node class="android.widget.EditText" focusable="true" long-clickable="true" enabled="true" bounds="[20,340][600,420]"/>
        <node class="android.widget.Switch" content-desc="Wi-Fi \xe5\xbc\x80\xe5\x85\xb3" checkable="true" checked="false" enabled="true" bounds="[20,200][300,320]"/>
      </node>
    </hierarchy>"""
    runtime.service.create(create_request(), OWNER)
    legacy = runtime.service.observe("run-a", OWNER, include_screenshot=True)
    compact = runtime.service.observe("run-a", OWNER, include_screenshot=True, ui_tree_format="compact")
    tree = compact["ui_tree"]
    assert tree["format"] == "compact" and "xml" not in tree
    assert tree["total"] == tree["returned"] == 5 and tree["truncated"] is False
    assert all(node.get("class") != "android.widget.FrameLayout" for node in tree["nodes"])
    title = next(node for node in tree["nodes"] if node.get("resource_id") == "com.example:id/title")
    icon = next(node for node in tree["nodes"] if node.get("class") == "android.widget.ImageView")
    wifi = next(node for node in tree["nodes"] if node.get("description") == "Wi-Fi 开关")
    assert title["text"] == "Settings & 设置"
    assert title["parent_id"] is not None and title["checked"] is None
    assert icon["clickable"] is True and icon["checked"] is False and icon["bounds"] == [900, 20, 1060, 180]
    assert wifi["checkable"] is True and wifi["checked"] is False and wifi["enabled"] is True
    input_node = next(node for node in tree["nodes"] if node.get("class") == "android.widget.EditText")
    assert input_node["focusable"] is True and input_node["long_clickable"] is True and "text" not in input_node
    assert base64.b64decode(compact["screenshot"]["base64"]) == b"png-test"
    assert len(json.dumps(compact, ensure_ascii=False)) < len(json.dumps(legacy, ensure_ascii=False))

    projected = compact_ui_tree(runtime.device.ui_xml, max_nodes=2)
    assert projected["total"] == 5 and projected["returned"] == 2 and projected["truncated"] is True

    runtime.device.ui_xml = b"<hierarchy><node text='broken'>"
    fallback = runtime.service.observe("run-a", OWNER, include_screenshot=False, ui_tree_format="compact")["ui_tree"]
    assert fallback == {
        "format": "xml", "xml": "<hierarchy><node text='broken'>", "status": "AVAILABLE", "error_code": None,
        "fallback_reason": "compact_parse_failed",
    }


def test_uncertain_batch_is_not_replayed_and_requires_new_observation(runtime):
    prepare(runtime)
    def effect(action):
        if action.action == "tap":
            raise TimeoutError("uncertain transport")
    runtime.device.effect = effect
    batch = {"command_id": "uncertain", "actions": [{"action": "recents"}, {"action": "tap", "x": 1, "y": 2}, {"action": "recents"}]}
    result = runtime.service.actions("run-a", OWNER, batch)
    assert result["outcome"] == "uncertain" and result["completed_count"] == 1
    assert len(runtime.device.actions) == 2
    assert runtime.service.actions("run-a", OWNER, batch)["replayed"]
    assert len(runtime.device.actions) == 2
    with pytest.raises(DeviceRunError, match="FRESH_OBSERVATION_REQUIRED"):
        runtime.service.actions("run-a", OWNER, {"command_id": "next", "actions": [{"action": "recents"}]})
    runtime.service.observe("run-a", OWNER, include_screenshot=False)
    runtime.device.effect = None
    assert runtime.service.actions("run-a", OWNER, {"command_id": "next", "actions": [{"action": "recents"}]})["accepted"]


def test_pause_cancels_remaining_batch_but_holds_lease_until_current_input_finishes(runtime):
    prepare(runtime)
    entered, finish = Event(), Event()
    def effect(_):
        entered.set()
        assert finish.wait(5)
    runtime.device.effect = effect
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.service.actions, "run-a", OWNER,
                             {"command_id": "long", "actions": [{"action": "recents"}, {"action": "recents"}]})
        assert entered.wait(5)
        paused = runtime.service.control("run-a", OWNER, "pause")
        assert paused["status"] == "paused"
        assert runtime.lease.is_held("emulator-5554")
        with pytest.raises(DeviceRunError, match="TARGET_BUSY"):
            runtime.service.create(create_request("run-b"), OWNER)
        with pytest.raises(DeviceRunError, match="DEVICE_OPERATION_IN_PROGRESS"):
            runtime.service.control("run-a", OWNER, "resume")
        finish.set()
        assert future.result(timeout=5)["outcome"] == "interrupted"
    assert len(runtime.device.actions) == 1 and not runtime.lease.is_held("emulator-5554")
    resumed = runtime.service.control("run-a", OWNER, "resume")
    assert resumed["requires_observation"] is True
    assert runtime.service.control("run-a", OWNER, "cancel")["status"] == "cancelled"
    assert not runtime.lease.is_held("emulator-5554")


def test_recovery_pauses_run_and_records_inflight_command_as_uncertain(tmp_path):
    path = tmp_path / "runs.db"
    store = DeviceRunStore(path)
    lease, profiles = DeviceExecutionLease(), Profiles()
    service = DeviceRunService(store=store, profiles=profiles, device_lease=lease,
                               executor_for_serial=lambda _: None, observation_for_serial=lambda _: None, clock=lambda: NOW)
    service.create(create_request(), OWNER)
    store.save_command("run-a", "claimed", "digest", {"outcome": "pending", "accepted": False,
                                                       "completed_count": 0, "results": [], "status": "active"})
    # Simulate process loss: dispose handles and connection without orderly pause.
    service._live["run-a"].lease.release()
    store.close()
    recovered = DeviceRunStore(path)
    restarted = DeviceRunService(store=recovered, profiles=profiles, device_lease=lease,
                                 executor_for_serial=lambda _: None, observation_for_serial=lambda _: None, clock=lambda: NOW)
    assert restarted.inspect("run-a", OWNER)["status"] == "paused"
    assert restarted.inspect("run-a", OWNER)["requires_observation"]
    assert recovered.command("run-a", "claimed")["result"]["outcome"] == "uncertain"
    assert not lease.is_held("emulator-5554")
    restarted.close()


def test_create_replay_does_not_reprobe_and_completion_releases_device(runtime):
    first = runtime.service.create(create_request(), OWNER)
    assert runtime.service.create(create_request(), OWNER) == first
    assert runtime.profiles.verifications == 1
    completed = runtime.service.control("run-a", OWNER, "complete")
    assert completed["status"] == "completed"
    assert not runtime.lease.is_held("emulator-5554")
    with pytest.raises(DeviceRunError, match="DEVICE_RUN_TERMINAL"):
        runtime.service.control("run-a", OWNER, "resume")
    runtime.service.create(create_request("run-b"), OWNER)


@pytest.mark.parametrize("runtime_mode", ["legacy", "kernel_active"])
def test_application_composes_direct_routes_without_a_role_model(settings, runtime, runtime_mode, tmp_path):
    adb = tmp_path / "fake-adb.exe"
    adb.write_bytes(b"not executed")
    app = create_app(settings=replace(settings, harness_api_token="test-token", gui_executor_enabled=True,
                                     runtime_mode=runtime_mode, managed_runtime=True, adb_path=adb),
                     adb_discovery=AdbTargetDiscovery(adb_path=adb, env={"PATH": ""}), long_task_scheduler=None)
    assert app.state.direct_device_runs.device_lease is app.state.device_execution_lease
    assert app.state.direct_device_runs.profiles is app.state.emulator_profiles
    # Exercise the actual HTTP composition with fake physical ports only.
    service = app.state.direct_device_runs
    device = Device(app.state.device_execution_lease)
    service.profiles = runtime.profiles
    service.executor_for_serial = lambda _: device
    service.observation_for_serial = lambda _: device
    with TestClient(app) as client:
        health = client.get("/api/execution/v2/health", headers=HEADERS).json()
        assert health["capabilities"]["android_ui_agent"]["available"] is False
        assert health["capabilities"]["direct_device"] == {
            "state": "ready", "available": True, "requires_model": False, "version": "1",
        }
        assert client.post(ROOT, json=create_request(), headers=HEADERS).status_code == 200
        assert client.post(f"{ROOT}/run-a/observe", json={}, headers=HEADERS).status_code == 200
        result = client.post(f"{ROOT}/run-a/actions", headers=HEADERS,
                             json={"command_id": "action", "actions": [{"action": "recents"}]})
        assert result.status_code == 200 and result.json()["accepted"]
        response = client.get(f"{ROOT}/missing", headers=HEADERS)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DEVICE_RUN_NOT_FOUND"


def test_concurrent_duplicate_is_not_dispatched_twice(runtime):
    prepare(runtime)
    entered, finish = Event(), Event()
    def effect(_):
        entered.set()
        assert finish.wait(5)
    runtime.device.effect = effect
    batch = {"command_id": "duplicate", "actions": [{"action": "recents"}]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.service.actions, "run-a", OWNER, batch)
        assert entered.wait(5)
        with pytest.raises(DeviceRunError, match="DEVICE_OPERATION_IN_PROGRESS"):
            runtime.service.actions("run-a", OWNER, batch)
        finish.set()
        assert future.result(timeout=5)["accepted"]
    assert runtime.service.actions("run-a", OWNER, batch)["replayed"]
    assert len(runtime.device.actions) == 1


def test_formal_runner_uses_same_serial_lease_for_observe_plan_act_sequence(runtime):
    calls = []
    snapshot = SimpleNamespace(owner=SimpleNamespace(**OWNER), profile_id="profile-a")
    def step(**_):
        assert runtime.lease.is_held("emulator-5554")
        with pytest.raises(DeviceRunError, match="TARGET_BUSY"):
            runtime.service.create(create_request(), OWNER)
        calls.append("act")
        return "done"
    runner = ProductionAndroidUiRunner(
        handler=SimpleNamespace(canonical=SimpleNamespace(inspect=lambda _: snapshot), one_step=step),
        criteria=SimpleNamespace(coverage_matches_goal=lambda **_: True),
        observations=SimpleNamespace(prepare=lambda **_: calls.append("observe")),
        device_lease=runtime.lease, profiles=runtime.profiles,
    )
    assert runner.one_step(task_id="task", goal="goal", criteria=None) == "done"
    assert calls == ["observe", "act"]
    runtime.service.create(create_request(), OWNER)
    with pytest.raises(TargetBusyError):
        runner.one_step(task_id="task", goal="goal", criteria=None)
    assert calls == ["observe", "act"]
