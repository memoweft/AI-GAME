from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from ai_game_console.goal_runtime.preflight import GoalPreflight


@dataclass(frozen=True)
class Target:
    id: str
    name: str
    status: str
    external_id: str
    kind: str = "android"


def snapshot(model_status: str = "ready"):
    return {
        "capabilities": [
            {"id": "model", "status": model_status, "detail": f"model {model_status}"}
        ]
    }


def discovery(*targets: Target, status: str = "ready"):
    return SimpleNamespace(
        targets=targets,
        discovery=SimpleNamespace(status=status, message=f"found {len(targets)}"),
    )


def preflight(*targets: Target, busy=(), model_status="ready", runtime=True, preferred=None):
    return GoalPreflight(
        capability_snapshot=lambda: snapshot(model_status),
        discover_targets=lambda: discovery(*targets),
        lease_is_held=lambda serial: serial in busy,
        runtime_available=lambda: runtime,
        preferred_serial=preferred,
    )


def test_preflight_selects_the_sole_ready_idle_android_target():
    result = preflight(
        Target("adb:one", "Phone", "ready", "one"),
        Target("adb:offline", "Old", "offline", "offline"),
    ).assess()

    assert result.state == "READY"
    assert result.selected_target_id == "adb:one"
    assert result.selected_serial == "one"


def test_preflight_requires_plain_selection_for_multiple_valid_targets():
    result = preflight(
        Target("adb:one", "Phone", "ready", "one"),
        Target("adb:two", "Tablet", "ready", "two"),
    ).assess()

    assert result.state == "WAITING_EXTERNAL"
    assert result.waiting_reason["code"] == "TARGET_SELECTION_REQUIRED"
    assert [item["target_id"] for item in result.target_options] == ["adb:one", "adb:two"]


def test_preflight_honors_an_explicit_ready_deployment_target():
    result = preflight(
        Target("adb:one", "Phone", "ready", "one"),
        Target("adb:two", "Tablet", "ready", "two"),
        preferred="two",
    ).assess()

    assert result.state == "READY"
    assert result.selected_target_id == "adb:two"


def test_preflight_excludes_leased_target_and_never_silently_uses_it():
    result = preflight(
        Target("adb:busy", "Busy", "ready", "busy"),
        Target("adb:idle", "Idle", "ready", "idle"),
        busy={"busy"},
    ).assess()

    assert result.state == "READY"
    assert result.selected_target_id == "adb:idle"
    assert any(item["state"] == "BUSY" for item in result.facts)


def test_preflight_reports_authorization_as_external_gate():
    result = preflight(
        Target("adb:phone", "Phone", "unauthorized", "phone")
    ).assess()

    assert result.state == "WAITING_EXTERNAL"
    assert result.waiting_reason == {
        "code": "USB_DEBUG_AUTHORIZATION",
        "message": "请在手机上允许这台电脑进行 USB 调试，然后重试。",
    }


def test_preflight_waits_for_model_or_runtime_before_binding():
    target = Target("adb:one", "Phone", "ready", "one")
    assert preflight(target, model_status="stopped").assess().waiting_reason["code"] == (
        "execution_capability_not_ready"
    )
    assert preflight(target, runtime=False).assess().state == "WAITING_CONFIGURATION"


def test_preflight_uses_fresh_discovery_on_every_retry():
    states = [
        discovery(Target("adb:one", "Phone", "offline", "one")),
        discovery(Target("adb:one", "Phone", "ready", "one")),
    ]
    manager = GoalPreflight(
        capability_snapshot=lambda: snapshot(),
        discover_targets=lambda: states.pop(0),
        lease_is_held=lambda _: False,
        runtime_available=lambda: True,
    )

    assert manager.assess().state == "WAITING_CONFIGURATION"
    assert manager.assess().state == "READY"


def test_preflight_never_treats_a_non_android_target_as_a_phone():
    result = preflight(
        Target("windows-local", "Windows", "ready", "local", kind="windows")
    ).assess()

    assert result.state == "WAITING_CONFIGURATION"
    assert result.selected_target_id is None
    assert all(item["capability"] != "target:windows-local" for item in result.facts)


def test_preflight_accepts_production_runtime_capability_objects():
    manager = GoalPreflight(
        capability_snapshot=lambda: {"capabilities": [SimpleNamespace(
            id="model", status="ready", detail="served model ready",
        )]},
        discover_targets=lambda: discovery(Target("adb:one", "Phone", "ready", "one")),
        lease_is_held=lambda _: False,
        runtime_available=lambda: True,
    )

    result = manager.assess()
    assert result.state == "READY"
    assert result.facts[0] == {
        "capability": "local.visual_grounding",
        "state": "READY",
        "detail": "served model ready",
    }
