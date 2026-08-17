from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import time

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
)


PNG = b"\x89PNG\r\n\x1a\n" + b"mobile-task-frame"


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
