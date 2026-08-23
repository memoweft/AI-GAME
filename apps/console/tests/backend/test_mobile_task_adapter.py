from __future__ import annotations

from collections import deque
import binascii
import json
import os
from pathlib import Path
import struct
import time
from types import SimpleNamespace
import zlib

import pytest

from ai_game_console.device_lease import DeviceExecutionLease, TargetBusyError
from ai_game_console.domain import Target, TargetKind
from ai_game_console.execution import (
    ActionTransportResult,
    AndroidScreenshot,
    GuiAction,
)
from ai_game_console.mobile_agent import (
    ActionAttempt,
    ActionDecision,
    DecisionContext,
    InputRevision,
    PhysicalIntent,
    PlanContext,
    ReflectionContext,
    Subgoal,
    TransportReceipt,
    VerificationContext,
)
from ai_game_console.mobile_task_adapter import (
    LocalMobileEvidenceStore,
    MobileTaskAdapterError,
    MobileTaskAndroidDriver,
    OpenAICompatibleMobileRoleModel,
    OpenAICompatibleToolRoleModel,
    _stzb_daily_plan_issues,
    _stzb_daily_plan_scaffold,
    _stzb_daily_executor_instruction,
    _stzb_daily_verdict_guard,
    _stzb_daily_verification_instruction,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"mobile-task-frame"


def _valid_png(width: int, height: int, *, invert: bool = False, pulse: int = 0) -> bytes:
    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(
            ">I", binascii.crc32(name + data) & 0xFFFFFFFF
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            value = int(255 * x / max(1, width - 1))
            if invert:
                value = 255 - value
            if x == width - 1 and y == height - 1:
                value = (value + pulse) % 256
            rows.extend((value, (value + y * 7) % 256, 255 - value))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )


class FakeRepository:
    def __init__(self, target: Target | None) -> None:
        self.target = target

    def get_target(self, target_id: str):
        if self.target is not None and self.target.id == target_id:
            return self.target
        return None


class FakeExecutor:
    serial = "127.0.0.1:16384"

    def __init__(self) -> None:
        self.actions: list[GuiAction] = []
        self.capture_count = 0

    def capture_screenshot(self) -> AndroidScreenshot:
        self.capture_count += 1
        return AndroidScreenshot(PNG + bytes([self.capture_count]), width=100, height=200)

    def execute(self, action: GuiAction) -> ActionTransportResult:
        self.actions.append(action)
        return ActionTransportResult(True, "accepted")


def _target() -> Target:
    return Target(
        id="adb:127.0.0.1:16384",
        name="MuMu",
        kind=TargetKind.ANDROID,
        status="ready",
        source="test",
        external_id="127.0.0.1:16384",
    )


def test_android_driver_opens_the_selected_real_device_serial_instead_of_the_default(
    tmp_path: Path,
) -> None:
    tablet = Target(
        id="adb:R58M1234AB",
        name="Android tablet",
        kind=TargetKind.ANDROID,
        status="ready",
        source="adb",
        external_id="R58M1234AB",
    )

    class MultiTargetExecutor(FakeExecutor):
        def __init__(self, serial: str = "127.0.0.1:16384") -> None:
            super().__init__()
            self.serial = serial
            self.opened: dict[str, MultiTargetExecutor] = {}

        def for_serial(self, serial: str) -> "MultiTargetExecutor":
            child = MultiTargetExecutor(serial)
            self.opened[serial] = child
            return child

    executor = MultiTargetExecutor()
    lease = DeviceExecutionLease()
    driver = MobileTaskAndroidDriver(
        repository=FakeRepository(tablet),
        executor=executor,
        evidence=LocalMobileEvidenceStore(tmp_path / "evidence"),
        device_lease=lease,
    )

    session = driver.open("task-tablet", tablet.id)
    receipt = session.execute(PhysicalIntent("tap", {"x": 30, "y": 40}))

    selected = executor.opened["R58M1234AB"]
    assert receipt.status == "accepted"
    assert lease.is_held("R58M1234AB")
    assert selected.actions == [
        GuiAction(target_id=tablet.id, action="tap", x=30, y=40)
    ]
    assert executor.actions == []
    session.close()
    assert not lease.is_held("R58M1234AB")


def test_android_driver_holds_device_lease_for_the_whole_task_session_and_persists_evidence(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    lease = DeviceExecutionLease()
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    driver = MobileTaskAndroidDriver(
        repository=FakeRepository(_target()),
        executor=executor,
        evidence=evidence,
        device_lease=lease,
    )

    session = driver.open("task-1", "adb:127.0.0.1:16384")
    assert lease.is_held(executor.serial)
    with pytest.raises(TargetBusyError):
        driver.open("task-2", "adb:127.0.0.1:16384")

    observation = session.observe()
    loaded = evidence.load(observation.evidence_id)
    receipt = session.execute(PhysicalIntent("tap", {"x": 20, "y": 30}))

    assert loaded.width == 100
    assert loaded.height == 200
    assert loaded.png_bytes.startswith(b"\x89PNG")
    assert receipt.status == "accepted"
    assert executor.actions == [
        GuiAction(
            target_id="adb:127.0.0.1:16384",
            action="tap",
            x=20,
            y=30,
        )
    ]
    assert list((tmp_path / "evidence").glob("*.png"))

    session.close()
    session.close()
    assert not lease.is_held(executor.serial)


def test_android_driver_maps_text_wait_and_uncertain_transport_without_replay(
    tmp_path: Path,
) -> None:
    class UncertainExecutor(FakeExecutor):
        def execute(self, action: GuiAction) -> ActionTransportResult:
            raise RuntimeError("executor_unicode_text_uncertain")

    waits: list[float] = []
    driver = MobileTaskAndroidDriver(
        repository=FakeRepository(_target()),
        executor=UncertainExecutor(),
        evidence=LocalMobileEvidenceStore(tmp_path / "evidence"),
        waiter=waits.append,
    )
    session = driver.open("task-1", None)
    receipt = session.execute(PhysicalIntent("text", {"text": "仗剑传说"}))
    waited = session.execute(PhysicalIntent("wait", {"seconds": 3}))
    session.close()

    assert receipt == TransportReceipt(
        "uncertain",
        detail="executor_unicode_text_uncertain",
    )
    assert waited.status == "accepted"
    assert waits == [3.0]


def test_android_driver_settles_after_an_accepted_physical_action(tmp_path: Path) -> None:
    waits: list[float] = []
    driver = MobileTaskAndroidDriver(
        repository=FakeRepository(_target()),
        executor=FakeExecutor(),
        evidence=LocalMobileEvidenceStore(tmp_path / "evidence"),
        waiter=waits.append,
    )

    session = driver.open("task-1", None)
    receipt = session.execute(PhysicalIntent("tap", {"x": 30, "y": 40}))
    session.close()

    assert receipt.status == "accepted"
    assert waits == [1.0]


def test_evidence_store_prunes_age_orphans_count_and_bytes_without_losing_latest(
    tmp_path: Path,
) -> None:
    now = time.time()
    root = tmp_path / "evidence"
    store = LocalMobileEvidenceStore(
        root,
        max_frames=2,
        max_total_bytes=10_000,
        max_age_seconds=60,
        now=lambda: now,
    )
    old = store.record("task-1", AndroidScreenshot(PNG + b"old", width=100, height=200))
    old_png = root / f"{old.evidence_id}.png"
    old_json = root / f"{old.evidence_id}.json"
    os.utime(old_png, (now - 61, now - 61))
    os.utime(old_json, (now - 61, now - 61))
    orphan_id = "a" * 32
    (root / f"{orphan_id}.png").write_bytes(PNG)
    (root / "keep.txt").write_text("keep", encoding="utf-8")

    current = store.record(
        "task-1", AndroidScreenshot(PNG + b"current", width=100, height=200)
    )
    next_observation = store.record(
        "task-1", AndroidScreenshot(PNG + b"next", width=100, height=200)
    )
    latest = store.record(
        "task-1", AndroidScreenshot(PNG + b"latest", width=100, height=200)
    )

    assert not old_png.exists()
    assert not old_json.exists()
    assert not (root / f"{orphan_id}.png").exists()
    assert (root / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert len(list(root.glob("*.png"))) <= 2
    assert not (root / f"{current.evidence_id}.png").exists()
    assert (root / f"{next_observation.evidence_id}.png").exists()
    assert store.load(latest.evidence_id).png_bytes.endswith(b"latest")

    byte_root = tmp_path / "byte-evidence"
    byte_store = LocalMobileEvidenceStore(
        byte_root,
        max_frames=10,
        max_total_bytes=len(PNG) + 80,
        max_age_seconds=60 * 60,
        now=lambda: now,
    )
    byte_store.record("task-1", AndroidScreenshot(PNG + b"x" * 32, width=100, height=200))
    byte_latest = byte_store.record(
        "task-1", AndroidScreenshot(PNG + b"y" * 32, width=100, height=200)
    )
    retained_bytes = sum(path.stat().st_size for path in byte_root.glob("*"))
    assert retained_bytes <= len(PNG) + 80
    assert byte_store.load(byte_latest.evidence_id).png_bytes.endswith(b"y" * 32)


def test_same_gui_owl_endpoint_runs_planner_executor_verifier_and_reflection_roles(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record(
        "task-1", AndroidScreenshot(PNG + b"before", width=100, height=200)
    )
    after = evidence.record(
        "task-1", AndroidScreenshot(PNG + b"after", width=100, height=200)
    )
    replies = deque(
        [
            '{"subgoals":["打开每日训练","领取奖励"]}',
            (
                "Action: tap entry\n"
                '<tool_call>{"name":"mobile_use","arguments":'
                '{"action":"click","coordinate":[500,250]}}</tool_call>'
            ),
            (
                '{"visible_facts":["Daily training entry is not visible"],'
                '"uncertain":false}'
            ),
            (
                '{"visible_facts":["Daily training entry is visible"],'
                '"goal_obstructed":false,"uncertain":false}'
            ),
            (
                '{"satisfied":true,"progress":true,"uncertain":false,'
                '"evidence":"每日训练入口已显示"}'
            ),
            (
                '{"strategy":"先关闭遮挡弹窗","terminate":false,'
                '"reason":"原路线无进展","replacement_subgoals":null}'
            ),
        ]
    )
    captured: list[dict[str, object]] = []

    def transport(endpoint, payload, headers, timeout):
        captured.append(
            {"endpoint": endpoint, "payload": payload, "headers": headers, "timeout": timeout}
        )
        return {"choices": [{"message": {"content": replies.popleft()}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl-1.5-8b-instruct",
        evidence=evidence,
        transport=transport,
    )
    subgoal = Subgoal(0, "打开每日训练", "active")

    plan = model.plan(
        PlanContext("task-1", "完成星铁日常", None, 0, (), before, None)
    )
    decision = model.decide(
        DecisionContext(
            "task-1",
            "完成星铁日常",
            None,
            1,
            subgoal,
            0,
            (),
            before,
            "initial",
            0,
            (),
            None,
        )
    )
    verification = model.verify(
        VerificationContext(
            "task-1",
            "完成星铁日常",
            subgoal,
            decision,
            before,
            TransportReceipt("accepted", "receipt-1"),
            after,
        )
    )
    reflection = model.reflect(
        ReflectionContext(
            "task-1",
            "完成星铁日常",
            subgoal,
            0,
            (),
            "initial",
            3,
            (
                ActionAttempt(
                    "attempt-1",
                    1,
                    1,
                    0,
                    0,
                    decision,
                    before,
                    TransportReceipt("accepted", "receipt-1"),
                    after,
                    verification,
                    "2026-08-10T00:00:00Z",
                    "2026-08-10T00:00:01Z",
                ),
            ),
            None,
        )
    )

    assert plan.subgoals == ("打开每日训练", "领取奖励")
    assert decision == ActionDecision(
        "act", PhysicalIntent("tap", {"x": 50, "y": 50}), "tap entry"
    )
    assert verification.satisfied is True
    assert verification.evidence == "每日训练入口已显示"
    assert reflection.strategy == "先关闭遮挡弹窗"
    assert len(captured) == 6
    assert {item["endpoint"] for item in captured} == {
        "http://127.0.0.1:4243/v1/chat/completions"
    }
    role_prompts = [
        item["payload"]["messages"][0]["content"][0]["text"]  # type: ignore[index]
        for item in captured
    ]
    assert ["ROLE: Planner" in prompt for prompt in role_prompts] == [
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert "ROLE: Executor" in role_prompts[1]
    assert "ROLE: Before Evidence Summarizer" in role_prompts[2]
    assert "ROLE: After Evidence Summarizer" in role_prompts[3]
    assert "ROLE: Verifier" in role_prompts[4]
    assert "ROLE: Reflection" in role_prompts[5]
    assert all(
        len(
            [
                content_item
                for content_item in item["payload"]["messages"][1]["content"]  # type: ignore[index]
                if content_item["type"] == "image_url"
            ]
        )
        <= 1
        for item in captured
    )
    verifier_prompt = captured[4]["payload"]["messages"][1]["content"][0]["text"]  # type: ignore[index]
    assert 'BEFORE visible facts summary: ["Daily training entry is not visible"]' in verifier_prompt
    assert 'AFTER visible facts summary: ["Daily training entry is visible"]' in verifier_prompt
    assert "start/login/continue gateway" in verifier_prompt
    verifier_content = captured[4]["payload"]["messages"][1]["content"]  # type: ignore[index]
    assert not [item for item in verifier_content if item["type"] == "image_url"]


def test_planner_discards_meta_finish_subgoals_instead_of_executing_them(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    observation = evidence.record(
        "task-1", AndroidScreenshot(PNG + b"home", width=100, height=200)
    )
    replies = deque(
        [
            '{"subgoals":["打开游戏","确认进入城内地图界面","结束任务"]}',
        ]
    )
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"content": replies.popleft()}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )

    plan = model.plan(
        PlanContext("task-1", "进入游戏主界面", None, 0, (), observation, None)
    )

    assert plan.subgoals == ("打开游戏", "确认进入城内地图界面")
    assert len(prompts) == 1


def test_verifier_repairs_an_invalid_before_summary_with_single_image_requests(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record("task-1", AndroidScreenshot(PNG + b"before", 100, 200))
    after = evidence.record("task-1", AndroidScreenshot(PNG + b"after", 100, 200))
    replies = deque(
        [
            "not json",
            '{"visible_facts":["entry is absent"],"uncertain":false}',
            '{"visible_facts":["entry is visible"],"goal_obstructed":false,"uncertain":false}',
            '{"satisfied":false,"progress":true,"uncertain":false,"evidence":"opened"}',
        ]
    )
    prompts: list[str] = []
    image_counts: list[int] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        content = payload["messages"][1]["content"]
        prompts.append(content[0]["text"])
        image_counts.append(len([item for item in content if item["type"] == "image_url"]))
        return {"choices": [{"message": {"content": replies.popleft()}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )
    verification = model.verify(
        VerificationContext(
            "task-1",
            "目标",
            Subgoal(0, "打开入口", "active"),
            ActionDecision("act", PhysicalIntent("tap", {"x": 1, "y": 2})),
            before,
            TransportReceipt("accepted", "receipt-1"),
            after,
        )
    )

    assert verification.progress is True
    assert image_counts == [1, 1, 1, 0]
    assert "previous response did not match" in prompts[1]
    assert 'BEFORE visible facts summary: ["entry is absent"]' in prompts[3]
    assert 'AFTER visible facts summary: ["entry is visible"]' in prompts[3]


def test_verifier_never_reports_progress_for_byte_identical_before_and_after(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    unchanged_frame = AndroidScreenshot(PNG + b"unchanged", 100, 200)
    before = evidence.record("task-1", unchanged_frame)
    after = evidence.record("task-1", unchanged_frame)
    replies = deque(
        [
            '{"visible_facts":["率土之滨图标已在桌面可见"],"uncertain":false}',
            '{"visible_facts":["率土之滨图标已在桌面可见"],"goal_obstructed":false,"uncertain":false}',
            (
                '{"satisfied":false,"progress":true,"uncertain":false,'
                '"evidence":"率土之滨图标可见"}'
            ),
        ]
    )
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"content": replies.popleft()}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )
    verification = model.verify(
        VerificationContext(
            "task-1",
            "打开率土之滨",
            Subgoal(0, "进入率土之滨主界面", "active"),
            ActionDecision("act", PhysicalIntent("tap", {"x": 50, "y": 50})),
            before,
            TransportReceipt("accepted", "receipt-1"),
            after,
        )
    )

    assert verification.satisfied is False
    assert verification.progress is False
    assert "no material visual change" in verification.evidence
    assert "AFTER repeats only BEFORE facts" in prompts[2]


def test_verifier_can_confirm_an_already_satisfied_static_final_state(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    unchanged_frame = AndroidScreenshot(PNG + b"already-done", 100, 200)
    before = evidence.record("task-1", unchanged_frame)
    after = evidence.record("task-1", unchanged_frame)
    replies = deque(
        [
            '{"visible_facts":["requested map is unobstructed"],"uncertain":false}',
            (
                '{"visible_facts":["requested map is unobstructed"],'
                '"goal_obstructed":false,"uncertain":false}'
            ),
            (
                '{"satisfied":true,"progress":true,"uncertain":false,'
                '"evidence":"requested map is unobstructed"}'
            ),
        ]
    )

    def transport(endpoint, payload, headers, timeout):
        del endpoint, payload, headers, timeout
        return {"choices": [{"message": {"content": replies.popleft()}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )
    verification = model.verify(
        VerificationContext(
            "task-1",
            "确认已经回到无遮挡地图",
            Subgoal(0, "无遮挡地图已经显示", "active"),
            ActionDecision("finish"),
            before,
            TransportReceipt("not_sent", "verification-only"),
            after,
        )
    )

    assert verification.satisfied is True
    assert verification.progress is True
    assert verification.evidence == "requested map is unobstructed"


def test_verifier_uses_owner_refinement_to_confirm_unobstructed_normal_game_hud(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    city_map = AndroidScreenshot(PNG + b"city-map-normal-hud", 100, 200)
    before = evidence.record("task-1", city_map)
    after = evidence.record("task-1", city_map)
    owner_update = (
        "任务、武将、仓库、势力、同盟、招募按钮和侦查状态文字属于正常 HUD；"
        "不要点击招募。没有模态框或教程遮挡即完成。"
    )
    replies = deque(
        [
            (
                '{"visible_facts":["率土之滨城内地图及正常 HUD 可见"],'
                '"uncertain":false}'
            ),
            (
                '{"visible_facts":["城内地图可见；任务、武将、仓库、势力、同盟、'
                '招募和侦查文字为正常 HUD，没有模态框或教程遮挡"],'
                '"goal_obstructed":false,"uncertain":false}'
            ),
            (
                '{"satisfied":true,"progress":true,"uncertain":false,'
                '"evidence":"城内地图无遮挡，正常 HUD 不构成遮挡"}'
            ),
        ]
    )
    prompts: list[str] = []
    image_counts: list[int] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        content = payload["messages"][1]["content"]
        prompts.append(content[0]["text"])
        image_counts.append(len([item for item in content if item["type"] == "image_url"]))
        return {"choices": [{"message": {"content": replies.popleft()}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )

    verification = model.verify(
        VerificationContext(
            "task-1",
            "进入率土之滨城内地图",
            Subgoal(0, "城内地图无遮挡地显示", "active"),
            ActionDecision("finish"),
            before,
            TransportReceipt("not_sent", "verification-only"),
            after,
            input_revision=1,
            owner_inputs=(
                InputRevision(
                    1,
                    owner_update,
                    "applied",
                    "owner-update-1",
                    "2026-08-10T00:00:00Z",
                    "2026-08-10T00:00:00Z",
                ),
            ),
        )
    )

    assert verification.satisfied is True
    assert verification.evidence == "城内地图无遮挡，正常 HUD 不构成遮挡"
    assert prompts == [
        prompt for prompt in prompts if owner_update in prompt
    ]
    assert image_counts == [1, 1, 0]


def test_verifier_refuses_satisfaction_when_after_facts_mark_goal_obstructed(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record("task-1", AndroidScreenshot(PNG + b"map", 100, 200))
    after = evidence.record("task-1", AndroidScreenshot(PNG + b"tutorial", 100, 200))
    replies = deque(
        [
            '{"visible_facts":["map is visible"],"uncertain":false}',
            (
                '{"visible_facts":["tutorial dialog overlays the requested map"],'
                '"goal_obstructed":true,"uncertain":false}'
            ),
            (
                '{"satisfied":true,"progress":true,"uncertain":false,'
                '"evidence":"map is visible"}'
            ),
        ]
    )
    captured: list[dict[str, object]] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        captured.append(payload)
        return {"choices": [{"message": {"content": replies.popleft()}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )
    verification = model.verify(
        VerificationContext(
            "task-1",
            "进入无遮挡的地图",
            Subgoal(0, "确认地图无遮挡", "active"),
            ActionDecision("act", PhysicalIntent("tap", {"x": 50, "y": 50})),
            before,
            TransportReceipt("accepted", "receipt-1"),
            after,
        )
    )

    assert verification.satisfied is False
    assert verification.progress is True
    assert "visible AFTER evidence still obstructs the requested result" in verification.evidence
    assert [
        len(
            [item for item in payload["messages"][1]["content"] if item["type"] == "image_url"]  # type: ignore[index]
        )
        for payload in captured
    ] == [1, 1, 0]
    final_prompt = captured[2]["messages"][1]["content"][0]["text"]  # type: ignore[index]
    assert 'AFTER visible facts summary: ["tutorial dialog overlays the requested map"]' in final_prompt
    assert "AFTER goal obstructed: true" in final_prompt


def test_verifier_fails_closed_when_before_summary_stays_invalid(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record("task-1", AndroidScreenshot(PNG + b"before", 100, 200))
    after = evidence.record("task-1", AndroidScreenshot(PNG + b"after", 100, 200))
    image_counts: list[int] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        content = payload["messages"][1]["content"]
        image_counts.append(len([item for item in content if item["type"] == "image_url"]))
        return {"choices": [{"message": {"content": "not json"}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )

    with pytest.raises(MobileTaskAdapterError) as raised:
        model.verify(
            VerificationContext(
                "task-1",
                "目标",
                Subgoal(0, "打开入口", "active"),
                ActionDecision("act", PhysicalIntent("tap", {"x": 1, "y": 2})),
                before,
                TransportReceipt("accepted", "receipt-1"),
                after,
            )
        )

    assert raised.value.code == "mobile_role_invalid_response"
    assert image_counts == [1, 1, 1]


def test_executor_prompt_uses_redacted_recent_action_fingerprints(tmp_path: Path) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    observation = evidence.record(
        "task-1", AndroidScreenshot(PNG + b"before", width=100, height=200)
    )
    repeated_tap = ActionDecision("act", PhysicalIntent("tap", {"x": 80, "y": 160}))
    attempts = tuple(
        ActionAttempt(
            f"attempt-{index}",
            index,
            1,
            0,
            0,
            decision,
            observation,
            TransportReceipt("accepted", f"receipt-{index}"),
            observation,
            None,
            "2026-08-10T00:00:00Z",
            "2026-08-10T00:00:01Z",
        )
        for index, decision in enumerate(
            (
                repeated_tap,
                repeated_tap,
                ActionDecision("act", PhysicalIntent("text", {"text": "SENTINEL_SECRET"})),
                ActionDecision("act", PhysicalIntent("keyevent", {"keycode": "KEYCODE_BACK"})),
                ActionDecision(
                    "act",
                    PhysicalIntent(
                        "swipe", {"x": 1, "y": 2, "end_x": 90, "end_y": 2, "duration_ms": 600}
                    ),
                ),
            ),
            start=1,
        )
    )
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '<tool_call>{"name":"mobile_use","arguments":'
                            '{"action":"click","coordinate":[25,25]}}</tool_call>'
                        )
                    }
                }
            ]
        }

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )
    model.decide(
        DecisionContext(
            "task-1",
            "目标",
            None,
            1,
            Subgoal(0, "打开入口", "active"),
            0,
            (),
            observation,
            "initial",
            0,
            attempts,
            None,
        )
    )

    assert "tap@r3c3" in prompts[0]
    assert "swipe:right" in prompts[0]
    assert "text(redacted)" in prompts[0]
    assert "keyevent:KEYCODE_BACK" in prompts[0]
    assert "SENTINEL_SECRET" not in prompts[0]
    assert "x=80" not in prompts[0]
    assert "Do not blindly repeat a recent non-idempotent action fingerprint" in prompts[0]


def test_role_model_rejects_malformed_json_with_sanitized_error(tmp_path: Path) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    observation = evidence.record(
        "task-1", AndroidScreenshot(PNG, width=100, height=200)
    )
    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        api_key="SENTINEL_SECRET_KEY",
        evidence=evidence,
        transport=lambda *args: {
            "choices": [{"message": {"content": "SENTINEL_PRIVATE_FRAME invalid"}}]
        },
    )

    with pytest.raises(MobileTaskAdapterError) as raised:
        model.plan(PlanContext("task-1", "目标", None, 0, (), observation, None))

    assert raised.value.code == "mobile_role_invalid_response"
    assert "SENTINEL" not in str(raised.value)
    assert "data:image" not in str(raised.value)


def test_role_model_repairs_a_pre_action_format_error_without_executing_device(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    observation = evidence.record(
        "task-1", AndroidScreenshot(PNG, width=100, height=200)
    )
    replies = deque(["not json", '{"subgoals":["打开任务页"]}'])
    prompts: list[str] = []
    system_prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        system_prompts.append(payload["messages"][0]["content"][0]["text"])
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"content": replies.popleft()}}]}

    model = OpenAICompatibleMobileRoleModel(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        evidence=evidence,
        transport=transport,
    )

    plan = model.plan(PlanContext("task-1", "目标", None, 0, (), observation, None))

    assert plan.subgoals == ("打开任务页",)
    assert len(prompts) == 2
    assert "previous response did not match" in prompts[1]
    assert "simple current-screen confirmation" in system_prompts[0]
    assert "one subgoal" in system_prompts[0]


def test_tool_role_model_uses_forced_tools_for_all_four_roles(tmp_path: Path) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, width=100, height=200))
    calls = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]["name"]
        calls.append((tool, payload))
        arguments = {
            "record_plan": {"subgoals": ["打开设置", "查看电池", "返回桌面"]},
            "mobile_use": {"action": "system_button", "button": "Home"},
            "record_verification": {
                "verdict": "progress",
                "evidence": "model claimed progress",
            },
            "record_reflection": {
                "strategy": "改用系统 Home", "terminate": False,
                "reason": "坐标点击无进展", "replacement_subgoals": None,
            },
        }[tool]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool, "arguments": json.dumps(arguments),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    subgoal = Subgoal(2, "返回桌面", "active")
    plan = model.plan(PlanContext("task-1", "查看电池后返回桌面", None, 0, (), frame, None))
    decision = model.decide(DecisionContext(
        "task-1", "查看电池后返回桌面", None, 1, subgoal, 0, (), frame,
        "initial", 0, (), None,
    ))
    verification = model.verify(VerificationContext(
        "task-1", "查看电池后返回桌面", subgoal, decision, frame,
        TransportReceipt("accepted"), frame,
    ))
    reflection = model.reflect(ReflectionContext(
        "task-1", "查看电池后返回桌面", subgoal, 0, (), "initial", 3,
        (ActionAttempt(
            "attempt-1", 1, 1, 2, 0, decision, frame,
            TransportReceipt("accepted"), frame, verification,
            "2026-08-21T00:00:00Z", "2026-08-21T00:00:01Z",
        ),), None,
    ))

    assert plan.subgoals == ("打开设置", "查看电池", "返回桌面")
    assert decision.intent == PhysicalIntent("keyevent", {"keycode": "KEYCODE_HOME"})
    assert verification.progress is False
    assert verification.evidence == "no material visual change from BEFORE evidence"
    assert reflection.strategy == "改用系统 Home"
    assert [item[0] for item in calls] == [
        "record_plan", "mobile_use", "record_verification", "record_reflection",
    ]
    assert all(item[1]["tools"][0]["function"]["name"] == item[0] for item in calls)
    assert all(item[1]["tool_choice"] == "required" for item in calls)
    assert all(item[1]["messages"][0]["content"].endswith("/no_think") for item in calls)
    assert all(
        item[1]["messages"][1]["content"][0]["text"].endswith("/no_think")
        for item in calls
    )
    assert all(
        item[1]["messages"][1]["content"][-1]
        == {"type": "text", "text": "/no_think"}
        for item in calls
    )
    assert "一张当前截图独立验证" in calls[0][1]["messages"][0]["content"]
    assert "获得新战法" in calls[1][1]["messages"][0]["content"]
    assert "期间不要使用系统返回" in calls[1][1]["messages"][0]["content"]
    reflection_payload = calls[3][1]
    assert reflection_payload["max_tokens"] == 512
    assert reflection_payload["reasoning_effort"] == "low"
    assert reflection_payload["reasoning_budget"] == 0
    assert reflection_payload["chat_template_kwargs"] == {"enable_thinking": False}
    mobile_parameters = calls[1][1]["tools"][0]["function"]["parameters"]
    assert mobile_parameters["properties"]["target_description"]["maxLength"] == 200


def test_tool_role_model_accepts_qwen_tap_as_click_alias(tmp_path: Path) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record(
        "task-1", AndroidScreenshot(PNG, width=1280, height=720)
    )

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]["name"]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool,
            "arguments": json.dumps({
                "action": "tap",
                "coordinate": [254, 140],
                "target_description": "主要事宜页签",
            }, ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    decision = model.decide(DecisionContext(
        "task-1", "检查率土每日任务", "adb:emulator-5554", 1,
        Subgoal(0, "切换到主要事宜页签", "active"), 0, (), frame,
        "initial", 0, (), None,
    ))

    assert decision.intent == PhysicalIntent(
        "tap", {"x": 325, "y": 101, "target_description": "主要事宜页签"}
    )


def test_tool_role_model_rejects_text_fallback_without_a_tool_call(tmp_path: Path) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, width=100, height=200))
    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence,
        transport=lambda *args: {"choices": [{"message": {"content": "looks ready"}}]},
    )

    with pytest.raises(MobileTaskAdapterError) as raised:
        model.plan(PlanContext("task-1", "目标", None, 0, (), frame, None))

    assert raised.value.code == "mobile_role_invalid_response"


def test_tool_role_verifier_rejects_animation_only_progress_but_keeps_scene_change(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record(
        "task-1", AndroidScreenshot(_valid_png(18, 16), width=18, height=16)
    )
    animated = evidence.record(
        "task-1",
        AndroidScreenshot(_valid_png(18, 16, pulse=31), width=18, height=16),
    )
    changed = evidence.record(
        "task-1",
        AndroidScreenshot(_valid_png(18, 16, invert=True), width=18, height=16),
    )

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]["name"]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool,
            "arguments": json.dumps({
                "verdict": "progress", "evidence": "model claimed progress",
            }),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    subgoal = Subgoal(0, "进入每日任务清单", "active")
    decision = ActionDecision("act", PhysicalIntent("swipe", {
        "start_x": 900, "start_y": 600, "end_x": 300, "end_y": 600,
        "duration_ms": 400,
    }))

    animation_only = model.verify(VerificationContext(
        "task-1", "率土每日任务", subgoal, decision, before,
        TransportReceipt("accepted"), animated,
    ))
    scene_change = model.verify(VerificationContext(
        "task-1", "率土每日任务", subgoal, decision, before,
        TransportReceipt("accepted"), changed,
    ))
    unsupported_finish = model.verify(VerificationContext(
        "task-1", "率土每日任务", subgoal,
        ActionDecision("finish", reason="model attempted finish"), before,
        TransportReceipt("not_sent"), changed,
    ))

    assert animation_only.progress is False
    assert animation_only.evidence == "no material visual change from BEFORE evidence"
    assert scene_change.progress is True
    assert scene_change.evidence == "model claimed progress"
    assert unsupported_finish.progress is False
    assert unsupported_finish.satisfied is False


def test_tool_role_verifier_rejects_unchanged_stzb_activity_middle_success(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record(
        "task-1", AndroidScreenshot(_valid_png(18, 16), width=18, height=16)
    )
    animation_only = evidence.record(
        "task-1",
        AndroidScreenshot(_valid_png(18, 16, pulse=31), width=18, height=16),
    )

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]["name"]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool,
            "arguments": json.dumps({
                "verdict": "satisfied",
                "evidence": "model mistook the left-boundary cards for a middle viewport",
            }),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.verify(VerificationContext(
        "task-1", "帮我把率土之滨今天所有可见的每日任务做完。",
        Subgoal(1, "精彩活动轮播滑动到一个有重叠的中间视口", "active"),
        ActionDecision("act", PhysicalIntent("swipe", {
            "start_x": 900, "start_y": 300, "end_x": 400, "end_y": 300,
            "duration_ms": 600,
        })),
        before, TransportReceipt("accepted"), animation_only,
    ))

    assert result.satisfied is False
    assert result.progress is False
    assert result.uncertain is False
    assert "STZB activity-middle guard" in result.evidence


def test_tool_role_verifier_requires_a_directed_terminal_activity_boundary_probe(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record(
        "task-1", AndroidScreenshot(_valid_png(18, 16), width=18, height=16)
    )
    changed = evidence.record(
        "task-1",
        AndroidScreenshot(_valid_png(18, 16, invert=True), width=18, height=16),
    )

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]["name"]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool,
            "arguments": json.dumps({
                "verdict": "satisfied", "evidence": "model labeled a boundary",
            }),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    subgoal = Subgoal(0, "精彩活动轮播停留在最左侧边界", "active")
    rightward_probe = ActionDecision("act", PhysicalIntent("swipe", {
        "x": 300, "y": 300, "end_x": 700, "end_y": 300,
        "duration_ms": 600,
    }))
    wrong_direction = ActionDecision("act", PhysicalIntent("swipe", {
        "x": 700, "y": 300, "end_x": 300, "end_y": 300,
        "duration_ms": 600,
    }))
    goal = "帮我把率土之滨今天所有可见的每日任务做完。"

    moved = model.verify(VerificationContext(
        "task-1", goal, subgoal, rightward_probe, before,
        TransportReceipt("accepted"), changed,
    ))
    terminal = model.verify(VerificationContext(
        "task-1", goal, subgoal, rightward_probe, changed,
        TransportReceipt("accepted"), changed,
    ))
    invalid_direction = model.verify(VerificationContext(
        "task-1", goal, subgoal, wrong_direction, changed,
        TransportReceipt("accepted"), changed,
    ))
    unsupported_finish = model.verify(VerificationContext(
        "task-1", goal, subgoal, ActionDecision("finish"), changed,
        TransportReceipt("not_sent"), changed,
    ))

    assert moved.satisfied is False
    assert moved.progress is True
    assert "another terminal probe is required" in moved.evidence
    assert terminal.satisfied is True
    assert terminal.progress is True
    assert invalid_direction.satisfied is False
    assert invalid_direction.progress is False
    assert "activity-boundary guard" in invalid_direction.evidence
    assert unsupported_finish.satisfied is False
    assert unsupported_finish.progress is False


def test_tool_role_verifier_keeps_directed_boundary_motion_despite_stale_card_expectation(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record(
        "task-1", AndroidScreenshot(_valid_png(18, 16), width=18, height=16)
    )
    changed = evidence.record(
        "task-1",
        AndroidScreenshot(_valid_png(18, 16, invert=True), width=18, height=16),
    )
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_verification",
            "arguments": json.dumps({
                "verdict": "no_progress",
                "evidence": "current first card differs from a previously seen card",
            }),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.verify(VerificationContext(
        "task-1", "帮我把率土之滨今天所有可见的每日任务做完。",
        Subgoal(0, "精彩活动轮播停留在最左侧边界", "active"),
        ActionDecision("act", PhysicalIntent("swipe", {
            "x": 300, "y": 300, "end_x": 700, "end_y": 300,
            "duration_ms": 600,
        })),
        before, TransportReceipt("accepted"), changed,
    ))

    assert result.satisfied is False
    assert result.progress is True
    assert "another terminal probe is required" in result.evidence
    assert "不绑定任何固定卡片标题" in prompts[0]


def test_tool_role_verifier_makes_an_unchanged_swipe_reflectable_but_keeps_tap_uncertain(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record(
        "task-1", AndroidScreenshot(_valid_png(18, 16), width=18, height=16)
    )

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]["name"]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool,
            "arguments": json.dumps({
                "verdict": "uncertain",
                "evidence": "the visible list boundary was not established",
            }),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    subgoal = Subgoal(0, "查看列表左边界", "active")
    swipe = ActionDecision("act", PhysicalIntent("swipe", {
        "start_x": 200, "start_y": 300, "end_x": 900, "end_y": 300,
        "duration_ms": 600,
    }))
    tap = ActionDecision("act", PhysicalIntent("tap", {"x": 300, "y": 400}))

    swipe_result = model.verify(VerificationContext(
        "task-1", "查看活动列表", subgoal, swipe, frame,
        TransportReceipt("accepted"), frame,
    ))
    tap_result = model.verify(VerificationContext(
        "task-1", "查看活动列表", subgoal, tap, frame,
        TransportReceipt("accepted"), frame,
    ))

    assert swipe_result.progress is False
    assert swipe_result.uncertain is False
    assert swipe_result.evidence == "no material visual change from BEFORE evidence"
    assert tap_result.uncertain is True


def test_tool_role_verifier_keeps_changed_readable_semantic_doubt_reflectable(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record(
        "task-1", AndroidScreenshot(_valid_png(18, 16), width=18, height=16)
    )
    changed = evidence.record(
        "task-1",
        AndroidScreenshot(_valid_png(18, 16, invert=True), width=18, height=16),
    )
    replies = deque([
        "画面清晰显示已进入夏日秘境，但未见每日文案，无法判断每日身份",
        "截图画面严重遮挡，无法看清活动详情",
    ])

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        tool = payload["tools"][0]["function"]["name"]
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": tool,
            "arguments": json.dumps({
                "verdict": "uncertain", "evidence": replies.popleft(),
            }, ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    context = dict(
        task_id="task-1", goal="检查活动每日身份",
        subgoal=Subgoal(0, "进入活动详情并确认每日身份", "active"),
        decision=ActionDecision("act", PhysicalIntent("tap", {"x": 300, "y": 400})),
        before=before, transport=TransportReceipt("accepted"), after=changed,
    )

    readable = model.verify(VerificationContext(**context))
    visually_uncertain = model.verify(VerificationContext(**context))

    assert readable.satisfied is False
    assert readable.progress is False
    assert readable.uncertain is False
    assert "clear changed AFTER frame" in readable.evidence
    assert visually_uncertain.uncertain is True


def test_tool_role_verifier_rejects_progress_when_subgoal_revisits_prior_scene(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record(
        "task-1", AndroidScreenshot(_valid_png(18, 16), width=18, height=16)
    )
    revisited = evidence.record(
        "task-1",
        AndroidScreenshot(_valid_png(18, 16, invert=True), width=18, height=16),
    )

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_verification",
            "arguments": json.dumps({
                "verdict": "progress", "evidence": "different from immediate frame",
            }),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    subgoal = Subgoal(6, "覆盖所有日常表面并记录可执行条目", "active")
    decision = ActionDecision("act", PhysicalIntent("swipe", {
        "start_x": 900, "start_y": 600, "end_x": 300, "end_y": 600,
        "duration_ms": 400,
    }))
    prior_attempt = SimpleNamespace(
        plan_revision=2,
        subgoal_index=6,
        after=revisited,
    )

    result = model.verify(VerificationContext(
        "task-1", "完成率土之滨今天的每日任务", subgoal, decision, before,
        TransportReceipt("accepted"), revisited,
        plan_revision=2, recent_attempts=(prior_attempt,),
    ))

    assert result.satisfied is False
    assert result.progress is False
    assert "scene-loop guard" in result.evidence


def test_tool_role_model_repairs_one_malformed_forced_tool_response(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    before = evidence.record(
        "task-1", AndroidScreenshot(PNG + b"before", width=100, height=200)
    )
    after = evidence.record(
        "task-1", AndroidScreenshot(PNG + b"after", width=100, height=200)
    )
    replies = deque([
        {"choices": [{"message": {"content": "plain text is invalid"}}]},
        {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_verification",
            "arguments": json.dumps({
                "verdict": "progress", "evidence": "完整任务页面已打开",
            }, ensure_ascii=False),
        }}]}}]},
    ])
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return replies.popleft()

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.verify(VerificationContext(
        "task-1", "率土每日任务", Subgoal(0, "进入完整任务页面", "active"),
        ActionDecision("act", PhysicalIntent("tap", {
            "x": 42, "y": 63, "target_description": "任务入口",
        })),
        before, TransportReceipt("accepted"), after,
    ))
    assert result.progress is True
    assert result.evidence == "完整任务页面已打开"
    assert len(prompts) == 2
    assert "Call record_verification exactly once" in prompts[1]


def test_reflection_retries_a_semantically_truncated_replacement_subgoal(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record(
        "task-1", AndroidScreenshot(PNG, width=100, height=200)
    )
    replies = deque([
        {
            "strategy": "leave exhausted surface",
            "terminate": False,
            "reason": "current surface exhausted",
            "replacement_subgoals": ["在主界面定位并点击一个标注为"],
        },
        {
            "strategy": "leave exhausted surface",
            "terminate": False,
            "reason": "current surface exhausted",
            "replacement_subgoals": ["返回主界面并打开可见的活动入口"],
        },
    ])
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        arguments = replies.popleft()
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_reflection",
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    recent_attempt = SimpleNamespace(
        sequence=1,
        decision=ActionDecision("finish", reason="no progress"),
        before=frame,
        after=frame,
        verification=SimpleNamespace(evidence="same surface"),
    )
    result = model.reflect(ReflectionContext(
        "task-1", "率土每日任务", Subgoal(0, "寻找每日入口", "active"),
        0, (), "initial", 3, (recent_attempt,), None,
    ))

    assert result.replacement_subgoals == ("返回主界面并打开可见的活动入口",)
    assert len(prompts) == 2
    assert "每个恢复阶段必须是完整结果句" in prompts[1]


def test_reflection_retries_a_presumed_stzb_daily_panel_recovery(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    replies = deque([
        {
            "strategy": "open task panel",
            "terminate": False,
            "reason": "inspect task surface",
            "replacement_subgoals": [
                "已进入“任务”总览页面，页面中可见“巡察”或“每日/今日”任务分类入口"
            ],
        },
        {
            "strategy": "diagnose task panel",
            "terminate": False,
            "reason": "do not presume daily identity",
            "replacement_subgoals": [
                "主要事宜页已打开，并确认是否含有每日或今日周期标识"
            ],
        },
    ])

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        arguments = replies.popleft()
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_reflection",
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    recent_attempt = SimpleNamespace(
        sequence=1,
        decision=ActionDecision("finish", reason="no progress"),
        before=frame,
        after=frame,
        verification=SimpleNamespace(evidence="same surface"),
    )
    result = model.reflect(ReflectionContext(
        "task-1", "完成率土之滨今天的每日任务",
        Subgoal(0, "寻找每日入口", "active"),
        0, (), "initial", 3, (recent_attempt,), None,
    ))

    assert result.strategy == "diagnose task panel"
    assert result.replacement_subgoals == (
        "主要事宜页已打开，并确认是否含有每日或今日周期标识",
    )


def test_reflection_accepts_leaving_panel_for_an_independent_daily_surface(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    reply = {
        "strategy": "leave exhausted task panel",
        "terminate": False,
        "reason": "the empty affairs tab is exhausted",
        "replacement_subgoals": [
            "任务面板已关闭，画面显示主导航界面，且主导航中存在一个当前可见的每日、活动、巡察或类似入口。",
            "已打开该独立入口的详情画面，画面中可见今日、每日、每天、当前周期、重置、次数或奖励等明确文字证据。",
        ],
    }
    calls = 0

    def transport(endpoint, payload, headers, timeout):
        nonlocal calls
        del endpoint, payload, headers, timeout
        calls += 1
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_reflection",
            "arguments": json.dumps(reply, ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    recent_attempt = SimpleNamespace(
        sequence=1,
        decision=ActionDecision("finish", reason="no progress"),
        before=frame,
        after=frame,
        verification=SimpleNamespace(evidence="same empty affairs surface"),
    )

    result = model.reflect(ReflectionContext(
        "task-1", "完成率土之滨今天的每日任务",
        Subgoal(0, "确认事务页是否具有每日身份", "active"),
        0, (), "inspect task panel", 3, (recent_attempt,), None,
    ))

    assert result.replacement_subgoals == tuple(reply["replacement_subgoals"])
    assert calls == 1


def test_reflection_rewrites_conditional_then_presumed_daily_recovery(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    replies = deque([
        {
            "strategy": "try patrol then tutorial",
            "terminate": False,
            "reason": "use alternate visible routes",
            "replacement_subgoals": [
                "巡察详情页已打开，并确认页面是否具有每日或今日周期身份。",
                "若巡察没有每日内容，则打开新手指南并寻找每日任务列表。",
            ],
        },
        {
            "strategy": "reopen task panel",
            "terminate": False,
            "reason": "look for a daily tab",
            "replacement_subgoals": [
                "任务面板已打开，且其中可见每日任务页签与每日任务列表。",
            ],
        },
        {
            "strategy": "try task panel then patrol",
            "terminate": False,
            "reason": "another conditional fallback",
            "replacement_subgoals": [
                "任务面板已打开，且其中可见每日任务页签与每日任务列表。",
                "若任务面板没有每日页签，则打开巡察详情页面。",
            ],
        },
        {
            "strategy": "return to navigation and accept only explicit daily surfaces",
            "terminate": False,
            "reason": "one unconditional visible route remains",
            "replacement_subgoals": [
                "当前活动详情弹窗已关闭，画面回到可继续操作的上一级导航界面。",
                "画面回到主导航界面，并且主导航上可见的任务、每日、巡察或事务类入口清晰可辨。",
                "从主导航进入一个任务或活动表面，该表面可见明确的每日、每天、今日、每日刷新或今日进度文案，并显示至少一条任务或活动条目及其当前状态。",
                "当前画面完整显示该每日或今日表面的第一屏内容，包括可见条目、状态或进度边界。",
            ],
        },
    ])
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_reflection",
            "arguments": json.dumps(replies.popleft(), ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    recent_attempt = SimpleNamespace(
        sequence=41,
        decision=ActionDecision("finish", reason="no progress"),
        before=frame,
        after=frame,
        verification=SimpleNamespace(evidence="same main navigation"),
    )
    result = model.reflect(ReflectionContext(
        "task-1", "完成率土之滨今天的每日任务",
        Subgoal(0, "进入带每日标识的页面并显示每日任务列表或任务详情", "active"),
        0, (), "stalled route", 3, (recent_attempt,), None,
    ))

    assert result.strategy == "return to navigation and accept only explicit daily surfaces"
    assert len(prompts) == 4
    assert "不得写若、如果、否则等分支" in prompts[1]
    assert "不得预设其就是每日页" in prompts[2]
    assert "不要继续虚构该页面或标题" in prompts[3]


def test_reflection_recovery_must_restore_current_complete_manifest_result(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    replies = deque([
        {
            "strategy": "return and reopen activity",
            "terminate": False,
            "reason": "the current card is exhausted",
            "replacement_subgoals": [
                "已返回主导航并打开一个带每日标识的独立活动详情页面。",
            ],
        },
        {
            "strategy": "return, reopen, and cover the manifest",
            "terminate": False,
            "reason": "restore the replaced completeness result",
            "replacement_subgoals": [
                "已返回主导航并打开一个带每日标识的独立活动详情页面。",
                "已覆盖今日完整目标集的起止边界并记录全部可见条目及状态。",
            ],
        },
    ])
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_reflection",
            "arguments": json.dumps(replies.popleft(), ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    recent_attempt = SimpleNamespace(
        sequence=18,
        decision=ActionDecision("finish", reason="no progress"),
        before=frame,
        after=frame,
        verification=SimpleNamespace(evidence="candidate detail only"),
    )
    result = model.reflect(ReflectionContext(
        "task-1", "完成率土之滨今天的每日任务",
        Subgoal(0, "完整截图记录今日完整目标集及其起止覆盖边界", "active"),
        0, (), "incomplete discovery", 3, (recent_attempt,), None,
    ))

    assert result.replacement_subgoals == (
        "已返回主导航并打开一个带每日标识的独立活动详情页面。",
        "已覆盖今日完整目标集的起止边界并记录全部可见条目及状态。",
    )
    assert len(prompts) == 2
    assert "最后必须重新达到当前要求的完整清单或边界覆盖结果" in prompts[1]


def test_stzb_reflection_uses_one_bounded_nonexecution_fallback_after_invalid_roles(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    calls = 0

    def transport(endpoint, payload, headers, timeout):
        nonlocal calls
        del endpoint, payload, headers, timeout
        calls += 1
        return {"choices": [{"message": {"content": "plain text is invalid"}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    stalled = (
        "打开并停留在『主要事宜』页签，确认该页签是否出现每日/今日/每天刷新等"
        "当前周期身份证据"
    )
    recent_attempt = SimpleNamespace(
        sequence=4,
        decision=ActionDecision("finish", reason="no progress"),
        before=frame,
        after=frame,
        verification=SimpleNamespace(evidence="主地图导航清晰可见"),
    )

    result = model.reflect(ReflectionContext(
        "task-1", "帮我把率土之滨今天所有可见的每日任务做完。",
        Subgoal(1, stalled, "active"), 0, (), "initial", 3,
        (recent_attempt,), None,
    ))

    assert result.strategy == "bounded STZB visible-surface recovery"
    assert result.terminate is False
    assert result.replacement_subgoals == (
        "画面已回到可操作的主导航表面，主导航入口清晰可见。",
        stalled,
    )
    assert calls == 3


def test_stzb_reflection_does_not_fallback_for_execution_or_repeat_it(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))

    def transport(endpoint, payload, headers, timeout):
        del endpoint, payload, headers, timeout
        return {"choices": [{"message": {"content": "plain text is invalid"}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    recent_attempt = SimpleNamespace(
        sequence=5,
        decision=ActionDecision("finish", reason="no progress"),
        before=frame,
        after=frame,
        verification=SimpleNamespace(evidence="same visible surface"),
    )
    contexts = (
        ReflectionContext(
            "task-1", "帮我把率土之滨今天所有可见的每日任务做完。",
            Subgoal(1, "执行冻结清单第一个每日条目并确认完成状态", "active"),
            0, (), "initial", 3, (recent_attempt,), None,
        ),
        ReflectionContext(
            "task-2", "帮我把率土之滨今天所有可见的每日任务做完。",
            Subgoal(1, "确认主要事宜是否具有每日身份", "active"),
            0, (), "bounded STZB visible-surface recovery", 3,
            (recent_attempt,), None,
        ),
    )

    for context in contexts:
        with pytest.raises(MobileTaskAdapterError) as raised:
            model.reflect(context)
        assert raised.value.code == "mobile_role_invalid_response"


def test_planner_retries_a_semantically_oversized_plan(tmp_path: Path) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record(
        "task-1", AndroidScreenshot(PNG, width=1280, height=720)
    )
    replies = deque([
        {"subgoals": [f"查看页面{i}" for i in range(25)]},
        {"subgoals": ["查看事务页", "查看活动页"]},
    ])
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_plan",
            "arguments": json.dumps(replies.popleft(), ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.plan(PlanContext(
        "task-1", "完成星铁日常", "adb:emulator-5554", 0, (), frame, None,
    ))

    assert result.subgoals == ("查看事务页", "查看活动页")
    assert len(prompts) == 2
    assert "输出必须是1到24个非空阶段" in prompts[1]


def test_planner_retries_a_presumed_or_aggregated_stzb_daily_panel(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    replies = deque([
        {"subgoals": ["在任务面板中逐一检查每个可见页签并记录完整每日任务清单"]},
        {"subgoals": [
                "打开主要事宜页，确认是否含有每日或今日周期标识",
                "打开事务页，确认是否含有每日或今日周期标识",
                "打开名望页，确认是否含有每日或今日周期标识",
                "将精彩活动轮播移动到左侧边界并记录明确每日候选",
                "从活动左侧边界向右移动一个有重叠的可见卡片组并记录中间视口候选",
                "将精彩活动轮播移动到右侧边界并记录明确每日候选",
                "打开一张活动卡片详情，确认是否含每日、每天或今日机制",
                "打开巡察入口并确认今日次数、条目和状态",
                "覆盖所有明确每日表面及其边界并记录完整今日目标集",
                "执行当前可行的每日条目，并从新鲜清单画面确认该条目进度或状态变化",
                "重新打开每日目标集并独立复读全部条目的最终状态与阻塞项",
        ]},
    ])

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_plan",
            "arguments": json.dumps(replies.popleft(), ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.plan(PlanContext(
        "task-1", "完成率土之滨今天的每日任务", "adb:emulator-5554",
        0, (), frame, None,
    ))

    assert len(result.subgoals) == 11
    assert all("确认是否" in item for item in result.subgoals[:3])


def test_planner_rejects_a_single_identity_check_for_full_stzb_goal(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    replies = deque([
        {"subgoals": [
            "查看主要事宜页，确认是否出现每日或今日标识与任务状态",
        ]},
        {"subgoals": [
            "分别查看所有任务页签并记录每日清单",
        ]},
        {"subgoals": [
            "查看主要事宜页并确认是否含每日或今日周期标识",
            "查看事务页并确认是否含每日或今日周期标识",
            "查看名望页并确认是否含每日或今日周期标识",
            "将精彩活动轮播移动到左侧边界并记录明确标注每日或每天的候选条目",
            "从活动左侧边界向右移动一个有重叠的可见卡片组并记录中间视口候选",
            "将精彩活动轮播移动到右侧边界并记录明确标注每日或每天的候选条目",
            "打开一张活动卡片详情并确认是否含每日、每天或今日机制",
            "打开巡察入口并确认今日次数、条目和状态",
            "汇总以上各独立表面，冻结并记录今天完整每日目标清单及覆盖边界",
            "执行一个当前可行条目并从新鲜画面确认其进度或完成状态变化",
            "重新打开每日目标集并独立复读全部条目的最终状态与阻塞项",
        ]},
    ])
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_plan",
            "arguments": json.dumps(replies.popleft(), ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.plan(PlanContext(
        "task-1", "帮我把率土之滨今天的每日任务做完", "adb:emulator-5554",
        0, (), frame, None,
    ))

    assert len(result.subgoals) == 11
    assert len(prompts) == 3
    assert "缺少执行当前可行条目" in prompts[1]
    assert "缺少从新鲜画面重新打开并独立复读" in prompts[1]
    assert "不同任务页签或列表边界必须拆成不同" in prompts[2]


def test_planner_requires_complete_multisurface_discovery_before_execution(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    replies = deque([
        {"subgoals": [
            "记录心愿征程每日前四次招募的当前状态",
            "查看主要事宜页并确认是否含每日或今日周期标识",
            "进入活动入口并确认一个每日目标表面",
            "执行当前可行的每日条目并确认状态变化",
            "重新打开每日目标表面并独立复读最终状态",
        ]},
        {"subgoals": [
            "进入心愿征程活动卡片详情并确认每日前四次招募的当前状态",
            "查看主要事宜页并确认是否含每日或今日周期标识",
            "查看事务页并确认是否含每日或今日周期标识",
            "查看名望页并确认是否含每日或今日周期标识",
            "将精彩活动轮播移动到左侧边界并记录新出现的候选",
            "从活动左侧边界向右移动一个有重叠的可见卡片组并记录中间视口候选",
            "将精彩活动轮播移动到右侧边界并记录新出现的候选",
            "打开巡察入口并确认今日次数、条目和状态",
            "覆盖所有带明确每日、每天或今日机制的独立表面及其起止边界，记录完整今日目标集",
            "执行当前可行的每日条目并确认状态变化或明确保留阻塞证据",
            "重新打开完整今日目标集并独立复读全部条目的最终状态",
        ]},
    ])
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_plan",
            "arguments": json.dumps(replies.popleft(), ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.plan(PlanContext(
        "task-1",
        "完成率土之滨今天的每日任务；如果没有单一每日清单页，就覆盖所有带明确每日机制的独立表面及其边界",
        "adb:emulator-5554", 0, (), frame, None,
    ))

    assert len(result.subgoals) == 11
    assert len(prompts) == 2
    assert "严格按以下四段顺序生成计划" in prompts[0]
    assert "汇总以上各独立表面" in prompts[0]
    assert "必须在执行前新增一个单独阶段" in prompts[1]
    assert "冻结所有显式日常表面" in prompts[1]
    assert result.subgoals.index("覆盖所有带明确每日、每天或今日机制的独立表面及其起止边界，记录完整今日目标集") < result.subgoals.index(
        "执行当前可行的每日条目并确认状态变化或明确保留阻塞证据"
    )


def test_planner_accepts_complete_manifest_aggregated_from_each_surface(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    reply = {"subgoals": [
        "进入心愿征程详情，确认是否具有每日招募机制及当前可执行状态",
        "将精彩活动轮播移动到左侧边界并记录明确每日候选",
        "从活动左侧边界向右移动一个有重叠的可见卡片组并记录中间视口候选",
        "将精彩活动轮播移动到右侧边界并记录明确每日候选",
        "查看主要事宜页并确认是否含每日或今日周期标识",
        "查看事务页并确认是否含每日或今日周期标识",
        "查看名望页并确认是否含每日或今日周期标识",
        "打开巡察入口并确认今日次数、条目和状态",
        "汇总以上各独立表面（精彩活动卡片、主要事宜、事务、名望），冻结并记录今天完整每日目标清单及覆盖边界",
        "执行当前可行的每日条目并确认状态变化或明确保留阻塞证据",
        "从新鲜画面重新打开已确认为每日的任务页签，独立复读完整目标清单的最终状态",
    ]}

    def transport(endpoint, payload, headers, timeout):
        del endpoint, payload, headers, timeout
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_plan",
            "arguments": json.dumps(reply, ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.plan(PlanContext(
        "task-1",
        "完成率土之滨今天的每日任务；如果没有单一每日清单页，就覆盖所有带明确每日机制的独立表面及其边界",
        "adb:emulator-5554", 0, (), frame, None,
    ))

    assert result.subgoals == tuple(reply["subgoals"])


def test_planner_rewrites_a_conditional_linear_stzb_stage(tmp_path: Path) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    valid = [
        "查看主要事宜页并确认是否含每日或今日周期标识",
        "查看事务页并确认是否含每日或今日周期标识",
        "查看名望页并确认是否含每日或今日周期标识",
        "将精彩活动轮播移动到左侧边界并记录明确每日候选",
        "从活动左侧边界向右移动一个有重叠的可见卡片组并记录中间视口候选",
        "将精彩活动轮播移动到右侧边界并记录明确每日候选",
        "打开一张活动卡片详情并确认是否含每日、每天或今日机制",
        "返回主导航并打开巡察入口，确认今日次数、条目和状态",
        "汇总以上各独立表面，冻结并记录今天完整目标清单及覆盖边界",
        "执行当前可行的每日条目并确认状态变化或明确保留阻塞证据",
        "重新打开已确认的每日表面并独立复读完整目标清单的最终状态",
    ]
    replies = deque([
        {"subgoals": [
            *valid[:3],
            "进入巡察页签（如存在），确认是否含每日身份",
            *valid[4:],
        ]},
        {"subgoals": valid},
    ])
    prompts: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del endpoint, headers, timeout
        prompts.append(payload["messages"][1]["content"][0]["text"])
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_plan",
            "arguments": json.dumps(replies.popleft(), ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    result = model.plan(PlanContext(
        "task-1",
        "完成率土之滨今天的每日任务；如果没有单一每日清单页，就覆盖所有独立表面",
        "adb:emulator-5554", 0, (), frame, None,
    ))

    assert result.subgoals == tuple(valid)
    assert len(prompts) == 2
    assert "线性计划不得写若、如果、否则" in prompts[1]
    assert "上一份被拒绝计划的完整 JSON" in prompts[1]
    assert "进入巡察页签（如存在），确认是否含每日身份" in prompts[1]
    assert "必须复制其中仍有效的阶段及其顺序，只修复上面指出的问题" in prompts[1]


def test_stzb_plan_rejects_misplaced_tabs_and_aggregated_dynamic_results() -> None:
    issues = _stzb_daily_plan_issues(
        "完成率土之滨今天的每日任务",
        (
            "查看主要事宜页并确认是否含每日或今日周期标识",
            "查看事务页并确认是否含每日或今日周期标识",
            "查看名望页并确认是否含每日或今日周期标识",
            "覆盖精彩活动轮播左右边界并记录明确每日候选",
            "打开一张活动卡片详情并确认是否含每日、每天或今日机制",
            "汇总以上各独立表面，冻结并记录今天完整目标清单及覆盖边界",
            "从主界面打开事务入口并显示当前事务列表",
            "对冻结清单中其余未完成条目逐一执行并显示各自完成状态",
            "对冻结清单中第四个当前可行条目执行操作并显示完成状态",
            "重新打开先前为否定结果的主要事宜/事务/名望页签并复读其仍无每日身份",
            "执行当前可行的每日条目并确认状态变化或明确保留阻塞证据",
            "重新打开已确认的每日表面并独立复读完整目标清单的最终状态",
        ),
    )

    assert "misplaced_task_tab_navigation" in issues
    assert "aggregated_execution" in issues
    assert "invented_execution_slot" in issues
    assert "aggregated_negative_reread" in issues


def test_stzb_plan_rejects_task_tabs_inside_the_activity_panel() -> None:
    issues = _stzb_daily_plan_issues(
        "完成率土之滨今天的每日任务",
        (
            "查看主要事宜页并确认是否含每日或今日周期标识",
            "查看事务页并确认是否含每日或今日周期标识",
            "查看名望页并确认是否含每日或今日周期标识",
            "覆盖精彩活动轮播左右边界并记录明确每日候选",
            "打开一张活动卡片详情并确认是否含每日、每天或今日机制",
            "返回精彩活动面板并切换到事务页签，确认其是否具有每日身份",
            "汇总以上各独立表面，冻结并记录今天完整目标清单及覆盖边界",
            "执行当前可行的每日条目并确认状态变化或明确保留阻塞证据",
            "重新打开已确认的每日表面并独立复读完整目标清单的最终状态",
        ),
    )

    assert "misplaced_task_tab_navigation" in issues


def test_stzb_plan_accepts_both_activity_side_boundary_synonyms() -> None:
    plan = (
        "查看主要事宜页并确认是否含每日或今日周期标识",
        "查看事务页并确认是否含每日或今日周期标识",
        "查看名望页并确认是否含每日或今日周期标识",
        "当前精彩活动轮播显示左侧边界并记录可见候选",
        "从活动左侧边界向右移动一个有重叠的可见卡片组并记录中间视口候选",
        "向右翻页直至活动轮播显示右侧边界并记录可见候选",
        "打开一张活动卡片详情并确认是否含每日、每天或今日机制",
        "汇总以上各独立表面，冻结并记录今天完整目标清单及覆盖边界",
        "执行当前可行的每日条目并确认状态变化或明确保留阻塞证据",
        "重新打开已确认的每日表面并独立复读完整目标清单的最终状态",
    )

    assert "missing_activity_boundary" not in _stzb_daily_plan_issues(
        "完成率土之滨今天的每日任务", plan,
    )
    assert "missing_activity_boundary" in _stzb_daily_plan_issues(
        "完成率土之滨今天的每日任务", tuple(
            item for item in plan if "右侧边界" not in item
        ),
    )


def test_stzb_plan_requires_patrol_and_independent_boundaries_before_freeze() -> None:
    goal = "完成率土之滨今天的每日任务"
    scaffold = _stzb_daily_plan_scaffold(goal)
    assert _stzb_daily_plan_issues(goal, scaffold) == ()

    without_patrol = tuple(item for item in scaffold if "巡察入口" not in item)
    assert "missing_patrol_surface" in _stzb_daily_plan_issues(goal, without_patrol)

    without_middle = tuple(item for item in scaffold if "中间视口" not in item)
    assert "missing_activity_middle" in _stzb_daily_plan_issues(
        goal, without_middle
    )

    premature = list(scaffold)
    manifest = premature.pop(next(
        index for index, item in enumerate(premature) if "冻结并记录" in item
    ))
    premature.insert(3, manifest)
    assert "manifest_before_discovery_complete" in _stzb_daily_plan_issues(
        goal, tuple(premature)
    )

    combined = tuple(
        "覆盖精彩活动轮播左右边界并记录明确每日候选"
        if "左侧边界" in item
        else None
        if "右侧边界" in item
        else item
        for item in scaffold
    )
    combined = tuple(item for item in combined if item is not None)
    assert "aggregated_surface" in _stzb_daily_plan_issues(goal, combined)


def test_planner_falls_back_after_repeated_identical_invalid_stzb_plan(
    tmp_path: Path,
) -> None:
    evidence = LocalMobileEvidenceStore(tmp_path / "evidence")
    frame = evidence.record("task-1", AndroidScreenshot(PNG, 1280, 720))
    invalid = {"subgoals": ["打开主要事宜页面并把它作为完整每日清单"]}
    call_count = 0

    def transport(endpoint, payload, headers, timeout):
        nonlocal call_count
        del endpoint, payload, headers, timeout
        call_count += 1
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "record_plan",
            "arguments": json.dumps(invalid, ensure_ascii=False),
        }}]}}]}

    model = OpenAICompatibleToolRoleModel(
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="qwen-role", evidence=evidence, transport=transport,
    )
    goal = "帮我把率土之滨今天所有可见的每日任务做完。"
    result = model.plan(PlanContext(
        "task-1", goal, "adb:emulator-5554", 0, (), frame, None,
    ))

    assert call_count == 2
    assert result.subgoals == _stzb_daily_plan_scaffold(goal)
    assert _stzb_daily_plan_issues(goal, result.subgoals) == ()
    assert all("坐标" not in item and "点击(" not in item for item in result.subgoals)


def test_discovery_only_stzb_scaffold_never_contains_execution() -> None:
    goal = "在率土之滨只发现并冻结今天的每日任务清单，先不要执行清单条目"
    scaffold = _stzb_daily_plan_scaffold(goal)

    assert len(scaffold) == 14
    assert _stzb_daily_plan_issues(goal, scaffold) == ()
    assert not any(
        token in item for item in scaffold
        for token in ("执行冻结清单", "已领取")
    )


def test_stzb_navigation_subgoal_is_not_held_to_whole_checklist_gate() -> None:
    navigation = _stzb_daily_verification_instruction(
        "发现并冻结率土之滨完整每日任务清单",
        "关闭任务面板并回到主导航界面",
    )
    exhaustive = _stzb_daily_verification_instruction(
        "发现并冻结率土之滨完整每日任务清单",
        "记录所有可见条目与状态并确认列表上下边界",
    )

    assert "只按该当前子目标判断" in navigation
    assert "不得因整体目标尚未完成而降级" in navigation
    assert "当前子目标明确要求完整清单" in exhaustive


def test_stzb_middle_executor_targets_visible_card_body_and_direction() -> None:
    instruction = _stzb_daily_executor_instruction(
        "完成率土之滨今天的每日任务",
        "精彩活动轮播滑动到一个有重叠的中间视口",
    )

    assert "starts on the body of a visible activity card" in instruction
    assert "not on empty background" in instruction
    assert "drags the card content toward the left" in instruction
    assert "reveal cards farther to the right" in instruction

    left_boundary = _stzb_daily_executor_instruction(
        "完成率土之滨今天的每日任务",
        "精彩活动轮播停留在最左侧边界",
    )
    right_boundary = _stzb_daily_executor_instruction(
        "完成率土之滨今天的每日任务",
        "精彩活动轮播停留在最右侧边界",
    )
    assert "drag the card content toward the right" in left_boundary
    assert "unchanged terminal probe" in left_boundary
    assert "drag the card content toward the left" in right_boundary
    assert "unchanged terminal probe" in right_boundary


def test_stzb_guard_allows_a_negative_daily_identity_discovery_branch() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "发现并冻结率土之滨完整每日任务清单",
        "观察任务面板并确认其是否为每日清单还是主线任务",
        "satisfied",
        "画面属于第二章主线任务，并非每日清单，已满足当前诊断子目标。",
    )

    assert verdict == "satisfied"
    assert evidence.startswith("画面属于第二章主线任务")

    no_label_verdict, no_label_evidence = _stzb_daily_verdict_guard(
        "完成率土之滨今天的每日任务",
        "查看事务页并确认其是否含今日或每日标识",
        "satisfied",
        "事务 0/15，暂无事务，无可见每日标识，已完成当前检查。",
    )
    assert no_label_verdict == "satisfied"
    assert no_label_evidence.startswith("事务 0/15")


def test_stzb_guard_rejects_weak_empty_affairs_wording_as_identity_result() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天所有可见的每日任务做完。",
        (
            "在任务面板中切换到事务页签，确认该页签是否出现每日/今日/每天刷新等"
            "当前周期身份标识及具体条目状态。"
        ),
        "satisfied",
        (
            "任务面板已切换到“事务”页签，左侧显示“事务 0/15”并标注“暂无事务”，"
            "说明该页签当前周期条目状态可见（0/15，暂无事务）。"
        ),
    )

    assert verdict == "progress"
    assert "STZB daily guard" in evidence


def test_stzb_guard_carries_daily_card_identity_into_renamed_detail_page() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "发现并冻结率土之滨完整每日任务清单",
        "已点入带有'每天登录领取丰厚奖励'文案的'登录奖励'活动卡片，完整读取内部条目",
        "satisfied",
        "画面已进入登录奖励/俸禄活动内部页面，完整展示第一日至第十四日全部条目。",
    )

    assert verdict == "satisfied"
    assert evidence.startswith("画面已进入登录奖励/俸禄")


def test_stzb_guard_accepts_explicit_daily_mechanic_in_activity_detail_body() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "完成率土之滨今天的每日任务",
        "进入桃源合合详情，确认是否属于今日每日任务",
        "satisfied",
        (
            "已进入桃源合合详情；活动时间为2026/07/15至2026/09/06，"
            "正文明确写有每日进入活动和提升繁荣度可获得积分，当前0级0/100。"
        ),
    )

    assert verdict == "satisfied"
    assert evidence.startswith("已进入桃源合合详情")


def test_stzb_guard_accepts_exact_manifest_candidate_location() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "完成率土之滨今天的每日任务",
        "在精彩活动轮播中定位并清晰显示每日候选《心愿征程》卡片。",
        "satisfied",
        "右下角清晰显示《心愿征程》卡片，文案为每日招募可获额外心愿积分。",
    )

    assert verdict == "satisfied"
    assert "STZB daily guard" not in evidence

    wrong_title, _ = _stzb_daily_verdict_guard(
        "完成率土之滨今天的每日任务",
        "在精彩活动轮播中定位并清晰显示每日候选《登录奖励》卡片。",
        "satisfied",
        "右下角清晰显示《心愿征程》卡片，文案为每日招募可获额外心愿积分。",
    )
    assert wrong_title == "progress"


def test_stzb_guard_accepts_a_numbered_day_by_day_login_detail() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天所有可见的每日任务做完。",
        (
            "在活动列表页中，找到并进入一个正文或卡片明确写有“每日”“每天”或“今日”"
            "机制的活动详情。"
        ),
        "satisfied",
        (
            "画面已进入“俸禄”活动详情页，卡片明确按“第一日”至“第十四日”逐日排列，"
            "右侧显示“累计登录七天即送五星灵帝”“您已累计登录5天”。"
        ),
    )

    assert verdict == "satisfied"
    assert evidence.startswith("画面已进入“俸禄”活动详情页")

    exact_verdict, exact_evidence = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天所有可见的每日任务做完。",
        "打开每日候选《登录奖励》的可见详情，确认其每日、每天或今日周期机制及当前条目状态。",
        "satisfied",
        (
            "画面已打开登录奖励详情，显示第一日至第十四日的累计登录奖励列表，"
            "您已累计登录5天，前5日已领取。"
        ),
    )
    assert exact_verdict == "satisfied"
    assert "STZB daily guard" not in exact_evidence


def test_stzb_guard_does_not_treat_an_unqualified_cumulative_login_as_daily() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天所有可见的每日任务做完。",
        "进入一个明确写有每日机制的活动详情。",
        "satisfied",
        "画面只显示您已累计登录5天，没有逐日条目或每日身份标题。",
    )

    assert verdict == "progress"
    assert "STZB daily guard" in evidence


def test_stzb_guard_allows_an_explicit_activity_navigation_alternative() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "完成率土之滨今天的每日任务",
        "已进入主导航上当前可见的每日、每天、巡察或活动入口之一，并记录可见卡片",
        "satisfied",
        "画面已进入精彩活动入口，当前活动页展示六张活动卡片。",
    )

    assert verdict == "satisfied"
    assert evidence.startswith("画面已进入精彩活动入口")


def test_stzb_guard_allows_click_then_enter_activity_navigation_wording() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "帮我把率土之滨今天所有可见的每日任务做完。",
        (
            "在主导航画面中点击一个当前可见且明确带有每日、今日、每天、活动或巡察"
            "字样的入口，进入对应详情画面。"
        ),
        "satisfied",
        (
            "画面已进入“精彩活动”详情，当前显示登录奖励页签，可见第一日至第十四日"
            "的登录奖励列表。"
        ),
    )

    assert verdict == "satisfied"
    assert evidence.startswith("画面已进入“精彩活动”详情")


def test_stzb_guard_rejects_satisfaction_when_visible_ratio_misses_target() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "完成率土之滨今天的每日任务",
        "主要事宜页面中占领Lv.2土地条目清晰显示为2/4",
        "satisfied",
        "画面已回到主要事宜，条目仍显示占领Lv.2土地1/4。",
    )

    assert verdict == "no_progress"
    assert "visible ratio contradicts requested target" in evidence


def test_stzb_guard_rejects_explicit_negative_target_even_if_named_in_evidence() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "完成率土之滨今天的每日任务",
        "确认占领Lv.2土地由1/4变为2/4",
        "satisfied",
        "当前进度仍为1/4，未达到2/4，不能判定占领完成。",
    )

    assert verdict == "no_progress"
    assert "visible ratio contradicts requested target" in evidence


def test_stzb_guard_keeps_satisfaction_when_visible_ratio_matches_target() -> None:
    verdict, evidence = _stzb_daily_verdict_guard(
        "完成率土之滨今天的每日任务",
        "确认巡察次数为2/5",
        "satisfied",
        "画面顶部显示巡察次数2/5，当前事件24。",
    )

    assert verdict == "satisfied"
    assert evidence.startswith("画面顶部显示")
