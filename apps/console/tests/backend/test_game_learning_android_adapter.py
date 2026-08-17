from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ai_game_console.execution import (
    ActionTransportResult,
    AndroidScreenshot,
    ExecutorProbeResult,
    GuiAction,
)
from ai_game_console.device_lease import DeviceExecutionLease
from ai_game_console.game_learning.android_adapter import (
    StzbAndroidAdapterError,
    StzbAndroidEnvironmentFactory,
    probe_foreground_package,
)
from ai_game_console.game_learning.domain import (
    DistilledTransition,
    Observation,
    PolicyMemory,
    TransportReceipt,
)
from ai_game_console.game_learning.profiles import PACKAGE_NAME, stzb_game_profile
from ai_game_console.game_learning.verifier import (
    LocalEvidenceAssessment,
    LocalEvidenceVerifierError,
    OpenAICompatibleLocalEvidenceAssessor,
    StrictStzbOutcomeVerifier,
    verification_status,
)


def png(name: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + name.encode("ascii")


def screenshot(name: str, *, width: int = 1000, height: int = 1000) -> AndroidScreenshot:
    return AndroidScreenshot(png_bytes=png(name), width=width, height=height)


def tool(arguments: str) -> str:
    return f'<tool_call>{{"name":"mobile_use","arguments":{arguments}}}</tool_call>'


class FakeExecutor:
    serial = "127.0.0.1:16384"
    adb_path = Path("fake-adb.exe")
    COMMAND_TIMEOUT_SECONDS = 3.0

    def __init__(self, frames: list[AndroidScreenshot]) -> None:
        self.frames = iter(frames)
        self.capture_count = 0
        self.actions: list[GuiAction] = []

    def probe(self) -> ExecutorProbeResult:
        return ExecutorProbeResult(
            status="ready",
            configured=True,
            detail="ready",
            blocker=None,
        )

    def capture_screenshot(self) -> AndroidScreenshot:
        self.capture_count += 1
        return next(self.frames)

    def execute(self, action: GuiAction) -> ActionTransportResult:
        self.actions.append(action)
        return ActionTransportResult(accepted=True, detail="accepted")


class FakeModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.goals: list[str] = []
        self.history: list[tuple[str, ...]] = []

    def propose_action(self, frame, *, goal, action_history):
        self.goals.append(goal)
        self.history.append(action_history)
        return next(self.responses)


class FakeAssessor:
    def __init__(self, assessments: Mapping[bytes, LocalEvidenceAssessment]) -> None:
        self.assessments = assessments
        self.calls: list[tuple[str, str]] = []

    def assess(self, observation, *, profile_id, task_id):
        self.calls.append((profile_id, task_id))
        assert observation.payload is not None
        return self.assessments[observation.payload]


@dataclass
class ForegroundSequence:
    packages: list[str]

    def __post_init__(self) -> None:
        self.calls = 0

    def __call__(self, executor) -> str:
        del executor
        self.calls += 1
        if len(self.packages) == 1:
            return self.packages[0]
        return self.packages.pop(0)


def assessment(
    *,
    scene: str = "main_scene",
    markers: tuple[str, ...] = (),
    confidence: float = 0.99,
    unsafe_reason: str | None = None,
    failure_reason: str | None = None,
) -> LocalEvidenceAssessment:
    return LocalEvidenceAssessment(
        package_name=PACKAGE_NAME,
        scene=scene,
        markers=markers,
        confidence=confidence,
        unsafe_reason=unsafe_reason,
        failure_reason=failure_reason,
    )


def build_session(
    *,
    frames: list[AndroidScreenshot],
    responses: list[str],
    assessments: Mapping[bytes, LocalEvidenceAssessment],
    foreground: ForegroundSequence | None = None,
):
    executor = FakeExecutor(frames)
    model = FakeModel(responses)
    assessor = FakeAssessor(assessments)
    packages = foreground or ForegroundSequence([PACKAGE_NAME])
    factory = StzbAndroidEnvironmentFactory(
        executor=executor,  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
        assessor=assessor,
        foreground_probe=packages,
        post_action_settle_seconds=0.0,
    )
    session = factory.open(
        profile=stzb_game_profile(),
        target_id=executor.serial,
        is_cancelled=lambda: False,
    )
    return session, executor, model, assessor, packages


def test_android_session_uses_fresh_frame_one_action_fresh_frame_and_local_verifier() -> None:
    before = screenshot("before")
    after = screenshot("after")
    session, executor, model, _, foreground = build_session(
        frames=[before, after],
        responses=[tool('{"action":"click","coordinate":[500,500]}')],
        assessments={
            before.png_bytes: assessment(scene="main_scene"),
            after.png_bytes: assessment(
                scene="task_list",
                markers=("task_list_visible",),
            ),
        },
    )
    memory = PolicyMemory(profile_id="stzb-tutorial-v1", version=1)

    before_observation = session.observe()
    proposal = session.propose_action(
        instruction="打开任务列表",
        observation=before_observation,
        policy_memory=memory,
    )
    assert proposal.kind == "execute"
    assert proposal.action is not None
    transport = session.execute(proposal.action)
    after_observation = session.observe()
    outcome = session.verifier.verify(
        before=before_observation,
        action=proposal.action,
        transport=transport,
        after=after_observation,
    )

    assert executor.capture_count == 2
    assert executor.actions == [proposal.action]
    assert transport.status == "accepted"
    assert verification_status(outcome) == "confirmed_success"
    assert outcome.task_succeeded is True
    assert outcome.reward == 1.0
    assert "只打开并查看任务列表" in model.goals[0]
    assert foreground.calls >= 4  # open, before, execute, after


def test_gui_model_terminate_is_not_outcome_success() -> None:
    before = screenshot("before")
    after = screenshot("same")
    session, executor, _, _, _ = build_session(
        frames=[before, after],
        responses=[tool('{"action":"terminate","status":"success"}')],
        assessments={
            before.png_bytes: assessment(),
            after.png_bytes: assessment(),
        },
    )
    observation = session.observe()
    proposal = session.propose_action(
        instruction="打开地图",
        observation=observation,
        policy_memory=PolicyMemory(profile_id="stzb-tutorial-v1", version=1),
    )
    assert proposal.kind == "terminate"
    after_observation = session.observe()
    outcome = session.verifier.verify(
        before=observation,
        action=None,
        transport=TransportReceipt(status="not_sent", detail="policy_terminate"),
        after=after_observation,
    )

    assert executor.actions == []
    assert verification_status(outcome) == "unconfirmed"
    assert outcome.task_succeeded is False
    assert outcome.reward == 0.0


def test_positive_verified_policy_memory_is_a_text_hint_not_a_replayed_action() -> None:
    frame = screenshot("current")
    session, executor, model, _, _ = build_session(
        frames=[frame],
        responses=[tool('{"action":"system_button","button":"Back"}')],
        assessments={frame.png_bytes: assessment(scene="allowed_menu")},
    )
    observation = session.observe()
    memory = PolicyMemory(
        profile_id="stzb-tutorial-v1",
        version=2,
        trajectory=(
            DistilledTransition(
                action=GuiAction(
                    target_id=executor.serial,
                    action="tap",
                    x=500,
                    y=250,
                ),
                reward=1.0,
                before_sha256="secret-before-hash",
                after_sha256="secret-after-hash",
                verifier_detail="confirmed_success:goal_marker_observed",
            ),
            DistilledTransition(
                action=GuiAction(
                    target_id=executor.serial,
                    action="tap",
                    x=10,
                    y=10,
                ),
                reward=-1.0,
                before_sha256="negative-before-hash",
                after_sha256="negative-after-hash",
                verifier_detail="unsafe:payment",
            ),
        ),
    )

    proposal = session.propose_action(
        instruction="返回主界面",
        observation=observation,
        policy_memory=memory,
    )

    assert proposal.kind == "execute"
    assert proposal.action is not None and proposal.action.keycode == "KEYCODE_BACK"
    assert executor.actions == []  # memory is never directly replayed
    history = "\n".join(model.history[0])
    assert "verified policy memory hint only" in history
    assert "tap normalized [501,250]" in history
    assert "confirmed_success" in history
    assert "secret-before-hash" not in history
    assert "secret-after-hash" not in history
    assert "negative-before-hash" not in history
    assert "payment" not in history


@pytest.mark.parametrize(
    "arguments",
    [
        '{"action":"type","text":"secret"}',
        '{"action":"long_press","coordinate":[500,500]}',
        '{"action":"system_button","button":"Home"}',
        '{"action":"system_button","button":"Menu"}',
    ],
)
def test_profile_blocks_text_long_press_home_and_app_switch(arguments: str) -> None:
    frame = screenshot("frame")
    session, executor, _, _, _ = build_session(
        frames=[frame],
        responses=[tool(arguments)],
        assessments={frame.png_bytes: assessment()},
    )
    observation = session.observe()

    with pytest.raises(StzbAndroidAdapterError) as raised:
        session.propose_action(
            instruction="打开地图",
            observation=observation,
            policy_memory=PolicyMemory(profile_id="stzb-tutorial-v1", version=1),
        )

    assert raised.value.code == "profile_action_blocked"
    assert executor.actions == []


def test_out_of_scope_language_is_blocked_before_model_call() -> None:
    frame = screenshot("frame")
    session, executor, model, _, _ = build_session(
        frames=[frame],
        responses=[tool('{"action":"click","coordinate":[500,500]}')],
        assessments={frame.png_bytes: assessment()},
    )
    observation = session.observe()

    with pytest.raises(Exception) as raised:
        session.propose_action(
            instruction="去商城充值抽卡",
            observation=observation,
            policy_memory=PolicyMemory(profile_id="stzb-tutorial-v1", version=1),
        )

    assert getattr(raised.value, "code", None) == "task_scope_blocked"
    assert model.goals == []
    assert executor.actions == []


def test_foreground_package_is_checked_again_before_physical_action() -> None:
    frame = screenshot("frame")
    foreground = ForegroundSequence([PACKAGE_NAME, PACKAGE_NAME, "com.other.app"])
    session, executor, _, _, _ = build_session(
        frames=[frame],
        responses=[tool('{"action":"click","coordinate":[500,500]}')],
        assessments={frame.png_bytes: assessment()},
        foreground=foreground,
    )
    observation = session.observe()
    proposal = session.propose_action(
        instruction="打开地图",
        observation=observation,
        policy_memory=PolicyMemory(profile_id="stzb-tutorial-v1", version=1),
    )
    assert proposal.action is not None

    with pytest.raises(StzbAndroidAdapterError) as raised:
        session.execute(proposal.action)

    assert raised.value.code == "foreground_package_mismatch"
    assert executor.actions == []


def test_learning_open_returns_target_busy_before_executor_preflight() -> None:
    lease = DeviceExecutionLease()
    executor = FakeExecutor([])
    factory = StzbAndroidEnvironmentFactory(
        executor=executor,  # type: ignore[arg-type]
        model=FakeModel([]),  # type: ignore[arg-type]
        assessor=FakeAssessor({}),
        foreground_probe=ForegroundSequence([PACKAGE_NAME]),
        device_lease=lease,
    )
    owner = lease.require(executor.serial)

    with pytest.raises(StzbAndroidAdapterError) as raised:
        factory.open(
            profile=stzb_game_profile(),
            target_id=executor.serial,
            is_cancelled=lambda: False,
        )

    assert raised.value.code == "target_busy"
    assert executor.capture_count == 0
    owner.release()


def test_learning_session_close_releases_lease_and_failed_open_does_too() -> None:
    lease = DeviceExecutionLease()
    executor = FakeExecutor([])
    factory = StzbAndroidEnvironmentFactory(
        executor=executor,  # type: ignore[arg-type]
        model=FakeModel([]),  # type: ignore[arg-type]
        assessor=FakeAssessor({}),
        foreground_probe=ForegroundSequence([PACKAGE_NAME]),
        device_lease=lease,
    )
    session = factory.open(
        profile=stzb_game_profile(),
        target_id=executor.serial,
        is_cancelled=lambda: False,
    )
    assert lease.is_held(executor.serial) is True
    session.close()
    session.close()
    assert lease.is_held(executor.serial) is False

    failing_factory = StzbAndroidEnvironmentFactory(
        executor=executor,  # type: ignore[arg-type]
        model=FakeModel([]),  # type: ignore[arg-type]
        assessor=FakeAssessor({}),
        foreground_probe=ForegroundSequence(["com.other.app"]),
        device_lease=lease,
    )
    with pytest.raises(StzbAndroidAdapterError) as raised:
        failing_factory.open(
            profile=stzb_game_profile(),
            target_id=executor.serial,
            is_cancelled=lambda: False,
        )
    assert raised.value.code == "foreground_package_mismatch"
    assert lease.is_held(executor.serial) is False

    # Existing callers that have not opted into the process-wide lease retain
    # their original preflight error rather than failing while releasing a
    # nonexistent handle.
    legacy_factory = StzbAndroidEnvironmentFactory(
        executor=executor,  # type: ignore[arg-type]
        model=FakeModel([]),  # type: ignore[arg-type]
        assessor=FakeAssessor({}),
        foreground_probe=ForegroundSequence(["com.other.app"]),
    )
    with pytest.raises(StzbAndroidAdapterError) as legacy_raised:
        legacy_factory.open(
            profile=stzb_game_profile(),
            target_id=executor.serial,
            is_cancelled=lambda: False,
        )
    assert legacy_raised.value.code == "foreground_package_mismatch"


def test_strict_verifier_emits_progress_unsafe_failed_and_no_progress_limit() -> None:
    task = stzb_game_profile()  # keep the core profile construction in the focused path
    assert task.max_actions == 25
    before = Observation(payload=png("before"))
    after = Observation(payload=png("after"))

    progress_assessor = FakeAssessor(
        {
            before.payload: assessment(scene="main_scene"),  # type: ignore[dict-item]
            after.payload: assessment(
                scene="allowed_menu",
                markers=("allowed_menu_opened",),
            ),  # type: ignore[dict-item]
        }
    )
    from ai_game_console.game_learning.profiles import compile_stzb_task

    progress = StrictStzbOutcomeVerifier(
        task=compile_stzb_task("继续教程"),
        assessor=progress_assessor,
    ).verify(
        before=before,
        action=None,
        transport=TransportReceipt(status="not_sent"),
        after=after,
    )
    assert verification_status(progress) == "confirmed_progress"

    unsafe = StrictStzbOutcomeVerifier(
        task=compile_stzb_task("继续教程"),
        assessor=FakeAssessor(
            {
                before.payload: assessment(),  # type: ignore[dict-item]
                after.payload: assessment(unsafe_reason="payment"),  # type: ignore[dict-item]
            }
        ),
    ).verify(
        before=before,
        action=None,
        transport=TransportReceipt(status="not_sent"),
        after=after,
    )
    assert verification_status(unsafe) == "unsafe"

    verifier = StrictStzbOutcomeVerifier(
        task=compile_stzb_task("打开地图"),
        assessor=FakeAssessor(
            {
                before.payload: assessment(),  # type: ignore[dict-item]
                after.payload: assessment(),  # type: ignore[dict-item]
            }
        ),
    )
    outcomes = [
        verifier.verify(
            before=before,
            action=None,
            transport=TransportReceipt(status="not_sent"),
            after=after,
        )
        for _ in range(3)
    ]
    assert [verification_status(item) for item in outcomes] == [
        "unconfirmed",
        "unconfirmed",
        "failed",
    ]
    assert outcomes[-1].detail == "failed:no_progress_limit"


def test_strict_verifier_does_not_reward_action_when_goal_was_already_present() -> None:
    from ai_game_console.game_learning.profiles import compile_stzb_task

    before = Observation(payload=png("already-before"))
    after = Observation(payload=png("already-after"))
    assessor = FakeAssessor(
        {
            before.payload: assessment(
                scene="task_list",
                markers=("task_list_visible",),
            ),  # type: ignore[dict-item]
            after.payload: assessment(
                scene="task_list",
                markers=("task_list_visible",),
            ),  # type: ignore[dict-item]
        }
    )

    outcome = StrictStzbOutcomeVerifier(
        task=compile_stzb_task("打开任务列表"),
        assessor=assessor,
    ).verify(
        before=before,
        action=GuiAction(target_id="device", action="tap", x=1, y=1),
        transport=TransportReceipt(status="accepted"),
        after=after,
    )

    assert outcome.confirmed is True
    assert outcome.task_succeeded is True
    assert outcome.reward == 0.0
    assert outcome.detail == "confirmed_success:goal_already_present"


def test_loopback_local_evidence_assessor_accepts_only_strict_allowlisted_json() -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []

    def transport(endpoint, payload, headers, timeout):
        del headers, timeout
        calls.append((endpoint, payload))
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"package_name":"com.netease.stzb.netease",'
                            '"scene":"task_list","markers":["task_list_visible"],'
                            '"confidence":0.98,"unsafe_reason":null,"failure_reason":null}'
                        )
                    }
                }
            ]
        }

    assessor = OpenAICompatibleLocalEvidenceAssessor(
        endpoint="http://127.0.0.1:8000/v1",
        model="local-verifier",
        transport=transport,
    )
    result = assessor.assess(
        Observation(payload=png("frame")),
        profile_id="stzb-tutorial-v1",
        task_id="open_task_list",
    )

    assert result.scene == "task_list"
    assert result.markers == ("task_list_visible",)
    assert result.confidence == 0.98
    assert calls[0][0] == "http://127.0.0.1:8000/v1/chat/completions"
    encoded_image = calls[0][1]["messages"][1]["content"][1]["image_url"]["url"]
    assert encoded_image.startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "content",
    [
        # Wrong foreground package.
        '{"package_name":"com.other","scene":"task_list","markers":[],"confidence":1.0,"unsafe_reason":null,"failure_reason":null}',
        # Extra untrusted field.
        '{"package_name":"com.netease.stzb.netease","scene":"task_list","markers":[],"confidence":1.0,"unsafe_reason":null,"failure_reason":null,"action":"click"}',
        # Non-finite confidence is rejected by the strict decoder after JSON parsing.
        '{"package_name":"com.netease.stzb.netease","scene":"task_list","markers":[],"confidence":NaN,"unsafe_reason":null,"failure_reason":null}',
        # Unknown values cannot become training labels.
        '{"package_name":"com.netease.stzb.netease","scene":"battle","markers":["won"],"confidence":1.0,"unsafe_reason":null,"failure_reason":null}',
    ],
)
def test_local_evidence_assessor_rejects_untrusted_or_non_allowlisted_output(
    content: str,
) -> None:
    def transport(endpoint, payload, headers, timeout):
        del endpoint, payload, headers, timeout
        return {"choices": [{"message": {"content": content}}]}

    assessor = OpenAICompatibleLocalEvidenceAssessor(
        endpoint="http://localhost:8000/v1",
        model="local-verifier",
        transport=transport,
    )
    with pytest.raises(LocalEvidenceVerifierError):
        assessor.assess(
            Observation(payload=png("frame")),
            profile_id="stzb-tutorial-v1",
            task_id="open_task_list",
        )


def test_foreground_probe_uses_shell_free_explicit_adb_command() -> None:
    captured: list[Sequence[str]] = []

    def runner(command: Sequence[str], timeout: float):
        assert timeout == 3.0
        captured.append(command)
        return subprocess_completed(
            "mResumedActivity: ActivityRecord{abc u0 "
            "com.netease.stzb.netease/.MainActivity t1}"
        )

    executor = FakeExecutor([])
    package = probe_foreground_package(executor, runner=runner)  # type: ignore[arg-type]

    assert package == PACKAGE_NAME
    assert tuple(captured[0][1:]) == (
        "-s",
        executor.serial,
        "shell",
        "dumpsys",
        "activity",
        "activities",
    )


def subprocess_completed(stdout: str):
    from subprocess import CompletedProcess

    return CompletedProcess(args=(), returncode=0, stdout=stdout, stderr="")
