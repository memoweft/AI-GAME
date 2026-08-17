from __future__ import annotations

import base64
import json
import sqlite3
import time
from dataclasses import replace
from typing import Any

import pytest

from ai_game_console.application_runtime import (
    ApplicationInstance,
    ExecutionReceipt,
    Input,
    MemoryCandidate,
    Observation,
    Outcome,
    RetryableApplicationError,
)
from ai_game_console.application_runtime.domain import PolicyContext, VerificationContext
from ai_game_console.applications.soul import (
    DelayedOutcomeEvidence,
    LoopbackSoulVisionClient,
    ReplyLearningStore,
    SoulApplicationError,
    SoulExecutionOwner,
    SoulObservationPort,
    SoulOwnerClient,
    SoulPersistenceProjection,
    SoulReplyMemoryGate,
    SoulReplyPolicy,
    SoulReplyVerifier,
    SoulVisualFacts,
    TrialDraft,
    build_soul_application_ports,
)
from ai_game_console.chat import ChatCompletion, ChatProviderError
from ai_game_console.application_runtime import ApplicationRuntime, Intent, RuntimeIntent


PNG = b"\x89PNG\r\n\x1a\n" + (b"x" * 64)


def _instance(
    *,
    inputs: tuple[str, ...] = (),
    initial_input: str | None = None,
) -> ApplicationInstance:
    return ApplicationInstance(
        instance_id="instance-1",
        profile_id="soul-reply-v1",
        target_id=None,
        initial_input=initial_input,
        status="running",
        revision=0,
        degraded=False,
        hard_risk=False,
        detail=None,
        error_code=None,
        memory_version=0,
        inputs=inputs,
        intents=(),
        outcomes=(),
        events=(),
        created_at="2026-08-10T00:00:00Z",
        updated_at="2026-08-10T00:00:00Z",
        finished_at=None,
    )


class _OwnerTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, path: str, payload: dict[str, Any] | None):
        self.calls.append((method, path, payload))
        if path.endswith("/observations"):
            return {
                "contract_version": "v1",
                "scope_ref": "scope-1",
                "scope": "one_due_pending_inbound",
                "expires_in_seconds": 120,
                "conversation_revision": "revision-1",
                "conversation_ref": "1" * 64,
                "pending_generation_ref": "2" * 64,
                "transcript_revision": "3" * 64,
                "screenshot_b64": base64.b64encode(PNG).decode("ascii"),
                "screenshot_sha256": __import__("hashlib").sha256(PNG).hexdigest(),
                "transcript": [
                    {"role": "me", "content": "刚忙完，你呢？", "created_at": "t-1"},
                    {"role": "them", "content": "今天过得怎么样？", "created_at": "t-2"},
                ],
            }
        if path.endswith("/intents"):
            return {
                "contract_version": "v1",
                "status": "reserved",
                "owner_ref": "owner-1",
            }
        if path.endswith("/dispatch"):
            return {
                "contract_version": "v1",
                "status": "confirmed",
                "owner_ref": "owner-1",
            }
        if path.endswith("/owner-1"):
            return {
                "contract_version": "v1",
                "status": "confirmed",
                "owner_ref": "owner-1",
            }
        if path.endswith("/application-intents/intent-1"):
            return {
                "contract_version": "v1",
                "status": "confirmed",
                "owner_ref": "owner-1",
            }
        if path.endswith("/scheduler"):
            desired_state = (
                payload.get("desired_state")
                if isinstance(payload, dict)
                else "running"
            )
            return {
                "contract_version": "v1",
                "controller_ref": "ai-game-soul-reply-v1",
                "desired_state": desired_state,
                "effective_state": (
                    "stopping" if desired_state == "stopped" else desired_state
                ),
                "reply_owner": "application_runtime",
                "scheduler_mode": "match",
            }
        return {
            "contract_version": "v1",
            "service": "soul_execution_owner",
            "capabilities": {"loopback_only": True},
        }


class _TimeoutCapturingResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.headers: dict[str, str] = {}
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._body


class _TimeoutCapturingOpener:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def open(self, request, *, timeout: float):
        self.calls.append((request.method, request.full_url, timeout))
        path = request.full_url
        if path.endswith("/observations"):
            payload = {
                "contract_version": "v1",
                "scope_ref": "scope-1",
                "scope": "no_due_pending_inbound",
                "expires_in_seconds": 0,
                "transcript": [],
            }
        elif path.endswith("/intents"):
            payload = {
                "contract_version": "v1",
                "status": "reserved",
                "owner_ref": "owner-1",
            }
        elif path.endswith("/dispatch"):
            payload = {
                "contract_version": "v1",
                "status": "confirmed",
                "owner_ref": "owner-1",
            }
        elif path.endswith("/scheduler"):
            desired_state = "paused" if request.method == "PUT" else "running"
            payload = {
                "contract_version": "v1",
                "controller_ref": "ai-game-soul-reply-v1",
                "desired_state": desired_state,
                "effective_state": desired_state,
                "reply_owner": "application_runtime",
                "scheduler_mode": "match",
            }
        elif path.endswith("/owner-1"):
            payload = {
                "contract_version": "v1",
                "status": "confirmed",
                "owner_ref": "owner-1",
            }
        else:
            payload = {
                "contract_version": "v1",
                "service": "soul_execution_owner",
                "capabilities": {"loopback_only": True},
            }
        return _TimeoutCapturingResponse(payload)


class _Vision:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def extract(self, screenshot_png: bytes) -> SoulVisualFacts:
        self.frames.append(screenshot_png)
        return SoulVisualFacts(
            page="conversation_detail",
            pending_inbound_visible=True,
            conversation_stage="early",
            tone="warm",
            cues=("question_present",),
            confidence=0.94,
        )


class _CloudProvider:
    def __init__(self) -> None:
        self.messages = ()

    def complete(self, messages, *, json_response, is_cancelled):
        self.messages = tuple(messages)
        assert json_response is True
        assert is_cancelled() is False
        return ChatCompletion(
            assistant_text="还不错，刚忙完一阵。你今天有什么开心的小事吗？",
            provider="cloud_openai_compatible",
            model="cloud-model",
        )


def test_owner_http_adapter_is_loopback_only_and_maps_v1_routes_exactly():
    with pytest.raises(SoulApplicationError, match="soul_owner_not_loopback"):
        SoulOwnerClient("https://example.com", transport=_OwnerTransport())
    with pytest.raises(SoulApplicationError, match="soul_owner_url_invalid"):
        SoulOwnerClient("http://127.0.0.1:5000/base?secret=yes", transport=_OwnerTransport())

    transport = _OwnerTransport()
    owner = SoulOwnerClient("http://127.0.0.1:5000", transport=transport)
    observed = owner.observe(
    )
    reserved = owner.reserve(
        application_intent_id="ai-game-intent-1",
        scope_ref=observed["scope_ref"],
        draft="one draft",
    )
    dispatched = owner.dispatch(
        reserved["owner_ref"],
        scope_ref=observed["scope_ref"],
        conversation_revision="revision-1",
        draft="one draft",
    )
    inspected = owner.inspect(reserved["owner_ref"])

    assert [call[:2] for call in transport.calls] == [
        ("POST", "/api/application-owner/v1/soul/observations"),
        ("POST", "/api/application-owner/v1/soul/intents"),
        ("POST", "/api/application-owner/v1/soul/intents/owner-1/dispatch"),
        ("GET", "/api/application-owner/v1/soul/intents/owner-1"),
    ]
    observation_payload = transport.calls[0][2]
    assert observation_payload == {
        "contract_version": "v1",
    }
    assert dispatched["status"] == inspected["status"] == "confirmed"


def test_owner_http_adapter_maps_scheduler_get_and_put_exactly():
    transport = _OwnerTransport()
    owner = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=transport
    )

    status = owner.scheduler()
    paused = owner.set_scheduler_state("paused")

    assert status["effective_state"] == "running"
    assert paused["effective_state"] == "paused"
    assert transport.calls == [
        (
            "GET",
            "/api/application-owner/v1/soul/scheduler",
            None,
        ),
        (
            "PUT",
            "/api/application-owner/v1/soul/scheduler",
            {
                "contract_version": "v1",
                "desired_state": "paused",
                "controller_ref": "ai-game-soul-reply-v1",
            },
        ),
    ]


def test_owner_http_adapter_uses_long_timeout_only_for_observations():
    owner = SoulOwnerClient(
        "http://127.0.0.1:5000",
        timeout_seconds=3.0,
        observation_timeout_seconds=90.0,
    )
    opener = _TimeoutCapturingOpener()
    owner._opener = opener

    observed = owner.observe()
    owner.capabilities()
    reserved = owner.reserve(
        application_intent_id="ai-game-intent-1",
        scope_ref=observed["scope_ref"],
        draft="one draft",
    )
    owner.dispatch(
        reserved["owner_ref"],
        scope_ref=observed["scope_ref"],
        conversation_revision="revision-1",
        draft="one draft",
    )
    owner.inspect(reserved["owner_ref"])
    owner.scheduler()
    owner.set_scheduler_state("paused")

    assert [call[0] for call in opener.calls] == [
        "POST",
        "GET",
        "POST",
        "POST",
        "GET",
        "GET",
        "PUT",
    ]
    assert [call[2] for call in opener.calls] == [90.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0]

    with pytest.raises(ValueError, match="desired_state"):
        owner.set_scheduler_state("invented")


def test_local_vision_is_loopback_only_and_parses_a_bounded_allowlist_schema():
    calls: list[tuple[str, dict[str, Any], dict[str, str], float]] = []

    def transport(endpoint, payload, headers, timeout):
        calls.append((endpoint, dict(payload), dict(headers), timeout))
        content = json.dumps({
            "page": "conversation_detail",
            "pending_inbound_visible": True,
            "conversation_stage": "new",
            "tone": "playful",
            "cues": ["greeting", "question_present"],
            "confidence": 0.8,
        }, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}

    with pytest.raises(SoulApplicationError, match="gui_model_not_local"):
        LoopbackSoulVisionClient(
            endpoint="https://vision.example.com/v1",
            model="gui-owl",
            transport=transport,
        ).extract(PNG)

    facts = LoopbackSoulVisionClient(
        endpoint="http://127.0.0.1:4243/v1",
        model="gui-owl",
        transport=transport,
    ).extract(PNG)
    assert facts.cues == ("greeting", "question_present")
    assert len(calls) == 1
    encoded_request = json.dumps(calls[0][1], ensure_ascii=False)
    assert "data:image/png;base64," in encoded_request
    assert calls[0][0] == "http://127.0.0.1:4243/v1/chat/completions"


def test_before_observation_uses_owner_capture_but_cloud_sees_only_structured_text():
    transport = _OwnerTransport()
    owner_client = SoulOwnerClient("http://127.0.0.1:5000", transport=transport)
    vision = _Vision()
    cloud = _CloudProvider()
    ports = build_soul_application_ports(
        owner_client=owner_client,
        vision=vision,
        cloud_provider_resolver=lambda: cloud,
        persona_prompt="Use the configured user profile without inventing facts.",
    )
    instance = _instance(inputs=("中途补充：语气自然一点",))
    before = ports.observation_port.observe(instance)
    decision = ports.policy.decide(PolicyContext(instance, before, None))

    assert vision.frames == [PNG]
    assert before.fresh is True
    assert "今天过得怎么样" not in repr(before.data)
    assert "screenshot" not in repr(before.data).lower()
    assert decision.intent is not None
    assert decision.intent.name == "soul.reply.pending_inbound.v1"
    assert decision.intent.arguments["scope_ref"] == "scope-1"
    assert decision.intent.arguments["conversation_revision"] == "revision-1"
    cloud_payload = "\n".join(message.content for message in cloud.messages)
    assert "今天过得怎么样" in cloud_payload
    assert "中途补充：语气自然一点" in cloud_payload
    assert "data:image" not in cloud_payload
    assert base64.b64encode(PNG).decode("ascii") not in cloud_payload
    assert "directly asks" in cloud_payload and "truthfully" in cloud_payload
    assert decision.memory_candidate is not None
    assert decision.intent.arguments["draft"] not in repr(decision.memory_candidate.content)


def test_policy_uses_initial_instruction_until_a_later_input_overrides_it():
    def decide_payload(instance: ApplicationInstance) -> str:
        owner_client = SoulOwnerClient(
            "http://127.0.0.1:5000", transport=_OwnerTransport()
        )
        observation = SoulObservationPort(
            owner_client=owner_client,
            vision=_Vision(),
        )
        cloud = _CloudProvider()
        before = observation.observe(instance)
        SoulReplyPolicy(lambda: cloud, observation.exchange).decide(
            PolicyContext(instance, before, None)
        )
        return "\n".join(message.content for message in cloud.messages)

    initial = decide_payload(
        _instance(initial_input="首轮请自然一点，不要连续追问")
    )
    overridden = decide_payload(
        _instance(
            initial_input="首轮请自然一点，不要连续追问",
            inputs=("现在简短一点",),
        )
    )

    assert "首轮请自然一点，不要连续追问" in initial
    assert "现在简短一点" in overridden
    assert "首轮请自然一点，不要连续追问" not in overridden


def test_policy_passes_runtime_cancellation_to_cloud_provider():
    owner_client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=_OwnerTransport()
    )
    observation = SoulObservationPort(
        owner_client=owner_client,
        vision=_Vision(),
    )
    instance = _instance()
    before = observation.observe(instance)

    class CancellationAwareProvider:
        def complete(self, _messages, *, json_response, is_cancelled):
            assert json_response is True
            assert is_cancelled is cancellation_probe
            assert is_cancelled() is True
            raise ChatProviderError("provider_cancelled", "cancelled")

    def cancellation_probe() -> bool:
        return True

    policy = SoulReplyPolicy(
        lambda: CancellationAwareProvider(), observation.exchange
    )
    with pytest.raises(RetryableApplicationError):
        policy.decide(
            PolicyContext(
                instance,
                before,
                None,
                is_cancelled=cancellation_probe,
            )
        )


@pytest.mark.parametrize(
    "failure",
    [
        OSError("owner offline"),
        SoulApplicationError("soul_execution_runtime_unavailable"),
        SoulApplicationError("foreground_action_owned"),
    ],
)
def test_before_owner_transient_failure_requests_bounded_retry(failure):
    def unavailable(_method, _path, _payload):
        raise failure

    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=unavailable
    )
    observation = SoulObservationPort(owner_client=client, vision=_Vision())

    with pytest.raises(RetryableApplicationError) as caught:
        observation.observe(_instance())

    assert 0.2 <= caught.value.wait_seconds <= 900


def test_before_local_vision_and_learning_transients_request_retry_without_material():
    class UnavailableVision:
        def extract(self, _screenshot):
            raise SoulApplicationError("local_soul_vision_unavailable")

    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=_OwnerTransport()
    )
    with pytest.raises(RetryableApplicationError):
        SoulObservationPort(
            owner_client=client, vision=UnavailableVision()
        ).observe(_instance())

    class UnavailableLearning:
        def observe_pending_inbound(self, **_kwargs):
            raise sqlite3.OperationalError("database temporarily locked")

    observation = SoulObservationPort(
        owner_client=client,
        vision=_Vision(),
        learning=UnavailableLearning(),
    )
    with pytest.raises(RetryableApplicationError):
        observation.observe(_instance())
    assert observation.exchange._before == {}


def test_strictly_invalid_owner_and_vision_payloads_are_not_retryable():
    def invalid_owner(_method, _path, _payload):
        return {
            "contract_version": "v1",
            "scope": "invented_scope",
        }

    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=invalid_owner
    )
    with pytest.raises(SoulApplicationError, match="soul_owner_invalid_observation"):
        SoulObservationPort(owner_client=client, vision=_Vision()).observe(
            _instance()
        )

    class InvalidVision:
        def extract(self, _screenshot):
            raise SoulApplicationError("local_soul_vision_invalid_response")

    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=_OwnerTransport()
    )
    with pytest.raises(
        SoulApplicationError, match="local_soul_vision_invalid_response"
    ):
        SoulObservationPort(
            owner_client=client, vision=InvalidVision()
        ).observe(_instance())


@pytest.mark.parametrize(
    "provider_error",
    [
        None,
        ChatProviderError("provider_unavailable", "temporary"),
        ChatProviderError("provider_http_error", "temporary"),
    ],
)
def test_cloud_transient_before_intent_requests_retry(provider_error):
    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=_OwnerTransport()
    )
    observation = SoulObservationPort(owner_client=client, vision=_Vision())
    before = observation.observe(_instance())

    class Provider:
        def complete(self, *_args, **_kwargs):
            raise provider_error

    provider = None if provider_error is None else Provider()
    policy = SoulReplyPolicy(lambda: provider, observation.exchange)

    with pytest.raises(RetryableApplicationError) as caught:
        policy.decide(PolicyContext(_instance(), before, None))

    assert caught.value.wait_seconds >= 0.2


def test_cloud_invalid_response_remains_strict_failure():
    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=_OwnerTransport()
    )
    observation = SoulObservationPort(owner_client=client, vision=_Vision())
    before = observation.observe(_instance())

    class InvalidProvider:
        def complete(self, *_args, **_kwargs):
            raise ChatProviderError("provider_invalid_response", "invalid")

    policy = SoulReplyPolicy(
        lambda: InvalidProvider(), observation.exchange
    )
    with pytest.raises(ChatProviderError, match="invalid"):
        policy.decide(PolicyContext(_instance(), before, None))


def test_cloud_draft_becomes_learning_trial_only_after_durable_reserve_fence(tmp_path):
    transport = _OwnerTransport()
    learning = ReplyLearningStore(tmp_path / "fenced-learning.db")
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
        learning=learning,
    )
    instance = _instance()
    before = ports.observation_port.observe(instance)
    decision = ports.policy.decide(PolicyContext(instance, before, None))
    assert decision.intent is not None
    trial_id = decision.intent.arguments["trial_id"]
    assert learning.get_trial(trial_id) is None

    owner_ref = ports.execution_owner.reserve(instance, decision.intent)
    assert owner_ref == "owner-1"
    trial = learning.get_trial(trial_id)
    assert trial is not None and trial["send_status"] == "reserved"


def test_stale_pre_intent_cloud_draft_is_discarded_without_learning_trial(tmp_path):
    transport = _OwnerTransport()
    learning = ReplyLearningStore(tmp_path / "stale-learning.db")
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
        learning=learning,
    )
    instance = _instance()
    before = ports.observation_port.observe(instance)
    stale_decision = ports.policy.decide(PolicyContext(instance, before, None))
    assert stale_decision.intent is not None
    trial_id = stale_decision.intent.arguments["trial_id"]

    # A fresh observation represents the core re-planning after an input
    # revision fence and evicts the never-reserved process-only draft.
    ports.observation_port.observe(instance)
    with pytest.raises(SoulApplicationError, match="reply_trial_material_unavailable"):
        ports.execution_owner.reserve(instance, stale_decision.intent)
    assert learning.get_trial(trial_id) is None
    assert not any(call[1].endswith("/intents") for call in transport.calls)


def test_execution_owner_maps_reserve_dispatch_inspect_and_after_is_fresh_owner_inspect():
    transport = _OwnerTransport()
    client = SoulOwnerClient("http://127.0.0.1:5000", transport=transport)
    vision = _Vision()
    observation_port = SoulObservationPort(
        owner_client=client,
        vision=vision,
    )
    owner = SoulExecutionOwner(client, observation_port.exchange)
    instance = _instance()
    before = observation_port.observe(instance)
    intent = SoulReplyPolicy(
        lambda: _CloudProvider(), observation_port.exchange
    ).decide(
        PolicyContext(instance, before, None)
    ).intent
    assert intent is not None

    reservation_id = owner.reserve(instance, intent)
    receipt = owner.dispatch(reservation_id, instance, intent)
    independently_inspected = owner.inspect(reservation_id)
    after = observation_port.observe(instance)

    assert reservation_id == "owner-1"
    assert receipt == ExecutionReceipt("owner-1", True, "confirmed")
    assert independently_inspected["status"] == "confirmed"
    assert after.fresh is True
    assert after.data == {
        "phase": "after",
        "contract_version": "v1",
        "owner_ref": "owner-1",
        "owner_status": "confirmed",
    }
    assert after.evidence_id != before.evidence_id
    assert len(vision.frames) == 1
    assert len([call for call in transport.calls if call[1].endswith("/observations")]) == 1


def test_verifier_requires_exact_owner_confirmation_and_fresh_after():
    before = Observation(
        "soul-before:scope-1",
        fresh=True,
        data={
            "phase": "before",
            "contract_version": "v1",
            "owner_scope_ref": "scope-1",
            "conversation_revision": "revision-1",
        },
    )
    intent = __import__(
        "ai_game_console.application_runtime", fromlist=["Intent"]
    ).Intent("soul.reply.pending_inbound.v1", {
        "contract_version": "v1",
        "scope_ref": "scope-1",
        "conversation_revision": "revision-1",
        "draft": "one draft",
        "application_intent_id": "intent-1",
        "trial_id": "trial-1",
    })
    after = Observation(
        "soul-after:owner-1:confirmed",
        fresh=True,
        data={
            "phase": "after",
            "contract_version": "v1",
            "owner_ref": "owner-1",
            "owner_status": "confirmed",
        },
    )
    context = VerificationContext(
        _instance(), intent, before, after, ExecutionReceipt("owner-1", True, "confirmed")
    )
    verifier = SoulReplyVerifier()
    assert verifier.verify(context).status == "confirmed_success"
    assert verifier.verify(replace(context, after=replace(after, fresh=False))).status == "unconfirmed"
    assert verifier.verify(
        replace(context, receipt=ExecutionReceipt("owner-other", True, "confirmed"))
    ).status == "uncertain"
    assert verifier.verify(
        replace(context, after=replace(after, data={**after.data, "owner_status": "reserved"}))
    ).status == "uncertain"

    terminal_after = replace(
        after,
        data={**after.data, "owner_status": "terminal_no_replay"},
    )
    for direct_status in ("stale_preflight", "preclick_rejected"):
        definite = verifier.verify(
            replace(
                context,
                receipt=ExecutionReceipt("owner-1", False, direct_status),
                after=terminal_after,
            )
        )
        assert definite.status == "confirmed_failure"
        assert definite.hard_risk is False
        assert definite.terminal is False

    terminal = verifier.verify(
        replace(
            context,
            receipt=ExecutionReceipt("owner-1", False, "terminal_no_replay"),
            after=terminal_after,
        )
    )
    assert terminal.status == "confirmed_failure"
    assert terminal.terminal is True


def test_memory_gate_never_promotes_current_send_as_reply_learning():
    candidate = MemoryCandidate(
        "soul-reply-v1",
        {
            "schema": "soul.reply_strategy.v1",
            "strategy": {
                "reply_length": "short",
                "question_usage": "one",
                "tone": "natural",
            },
            "claim": "ai_strategy_applied",
            "user_fact": False,
        },
        ("soul-before:scope-1",),
    )
    gate = SoulReplyMemoryGate()
    assert gate.promote(candidate, Outcome("unconfirmed", "no proof")) is False
    assert gate.promote(
        candidate,
        Outcome("confirmed_success", "soul_reply_confirmed|after=after-proof"),
    ) is False


def test_reply_learning_updates_posterior_only_after_explicit_delayed_outcome(tmp_path):
    store = ReplyLearningStore(tmp_path / "soul-learning.db")
    draft = TrialDraft(
        trial_id="trial-1",
        application_intent_id="intent-1",
        instance_id="instance-1",
        before_evidence_id="soul-before:scope-1",
        conversation_ref="1" * 64,
        pending_generation_ref="2" * 64,
        transcript_revision="3" * 64,
        scope_commitment_sha256="a" * 64,
        draft_sha256="b" * 64,
        strategy={
            "reply_length": "short",
            "question_usage": "one",
            "tone": "natural",
        },
        prompt_version="prompt-v1",
        persona_version="persona-v1",
        memory_version=0,
        provider="cloud_openai_compatible",
        model="cloud-model",
    )
    store.begin_trial(draft)
    store.bind_owner("trial-1", "owner-1", "reserved")
    store.record_send_proof("trial-1", owner_ref="owner-1", status="confirmed", proof_ref="proof-1")

    before = store.recommend_strategy()
    trial_before = store.get_trial("trial-1")
    assert before.explicit_outcomes == 0
    assert trial_before is not None and trial_before["delayed_outcome"] == "pending"
    assert "今天过得怎么样" not in json.dumps(trial_before, ensure_ascii=False)
    assert "draft" not in trial_before

    updated = store.record_delayed_outcome(
        "trial-1",
        DelayedOutcomeEvidence("positive_engagement", "owner-evidence-2"),
    )
    assert updated.explicit_outcomes == 1
    assert updated.alpha == before.alpha + 1
    replay = store.record_delayed_outcome(
        "trial-1",
        DelayedOutcomeEvidence("positive_engagement", "owner-evidence-2"),
    )
    assert replay == updated


def test_reply_learning_rejects_outcome_without_confirmed_send(tmp_path):
    store = ReplyLearningStore(tmp_path / "soul-learning.db")
    store.begin_trial(TrialDraft(
        trial_id="trial-2",
        application_intent_id="intent-2",
        instance_id="instance-1",
        before_evidence_id="before-2",
        conversation_ref="4" * 64,
        pending_generation_ref="5" * 64,
        transcript_revision="6" * 64,
        scope_commitment_sha256="c" * 64,
        draft_sha256="d" * 64,
        strategy={"reply_length": "short", "question_usage": "none", "tone": "warm"},
        prompt_version="prompt-v1",
        persona_version="persona-v1",
        memory_version=0,
        provider="cloud_openai_compatible",
        model="cloud-model",
    ))
    with pytest.raises(SoulApplicationError, match="delayed_outcome_requires_confirmed_send"):
        store.record_delayed_outcome(
            "trial-2", DelayedOutcomeEvidence("negative_engagement", "proof-2")
        )


def test_new_owner_pending_generation_auto_records_positive_engagement(tmp_path):
    store = ReplyLearningStore(tmp_path / "soul-learning.db")
    strategy = {"reply_length": "short", "question_usage": "one", "tone": "natural"}
    store.begin_trial(TrialDraft(
        trial_id="trial-old",
        application_intent_id="intent-old",
        instance_id="instance-old",
        before_evidence_id="before-old",
        conversation_ref="7" * 64,
        pending_generation_ref="8" * 64,
        transcript_revision="9" * 64,
        scope_commitment_sha256="a" * 64,
        draft_sha256="b" * 64,
        strategy=strategy,
        prompt_version="prompt-v1",
        persona_version="persona-v1",
        memory_version=0,
        provider="cloud_openai_compatible",
        model="cloud-model",
    ))
    store.bind_owner("trial-old", "owner-old", "reserved")
    store.record_send_proof(
        "trial-old", owner_ref="owner-old", status="confirmed", proof_ref="send-proof"
    )
    update = store.observe_pending_inbound(
        conversation_ref="7" * 64,
        pending_generation_ref="a" * 64,
        transcript_revision="b" * 64,
        evidence_ref="new-pending-proof",
    )
    assert update is not None
    assert update.explicit_outcomes == 1
    assert store.get_trial("trial-old")["delayed_outcome"] == "positive_engagement"

    # Re-observing the same new pending generation is idempotent.
    replay = store.observe_pending_inbound(
        conversation_ref="7" * 64,
        pending_generation_ref="a" * 64,
        transcript_revision="b" * 64,
        evidence_ref="new-pending-proof",
    )
    assert replay is None


def test_no_due_pending_inbound_waits_without_visual_or_cloud_call():
    class NoDueTransport(_OwnerTransport):
        def __call__(self, method, path, payload):
            self.calls.append((method, path, payload))
            return {
                "contract_version": "v1",
                "scope": "no_due_pending_inbound",
                "expires_in_seconds": 0,
                "transcript": [],
            }

    owner_transport = NoDueTransport()
    vision = _Vision()
    cloud = _CloudProvider()
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=owner_transport
        ),
        vision=vision,
        cloud_provider_resolver=lambda: cloud,
    )
    observation = ports.observation_port.observe(_instance())
    decision = ports.policy.decide(PolicyContext(_instance(), observation, None))
    assert observation.data["scope"] == "no_due_pending_inbound"
    assert decision.intent is None and decision.wait_seconds == 20.0
    assert vision.frames == [] and cloud.messages == ()


def test_projection_redacts_draft_and_preserves_runtime_control_facts():
    projection = SoulPersistenceProjection()
    raw = Intent(
        "soul.reply.pending_inbound.v1",
        {
            "contract_version": "v1",
            "application_intent_id": "intent-1",
            "trial_id": "trial-1",
            "scope_ref": "scope-1",
            "conversation_revision": "revision-1",
            "draft": "this exact body must not be durable",
        },
        hard_risk=False,
    )
    projected = projection.project_intent(raw)
    assert projected.name == raw.name and projected.hard_risk == raw.hard_risk
    assert "draft" not in projected.arguments
    assert projected.arguments["draft_sha256"] == __import__("hashlib").sha256(
        raw.arguments["draft"].encode("utf-8")
    ).hexdigest()
    receipt = ExecutionReceipt("owner-1", True, "confirmed")
    assert projection.project_receipt(receipt) == receipt
    outcome = Outcome("confirmed_success", "proof", terminal=False)
    projected_outcome = projection.project_outcome(outcome)
    assert (projected_outcome.status, projected_outcome.hard_risk, projected_outcome.terminal) == (
        outcome.status,
        outcome.hard_risk,
        outcome.terminal,
    )


def test_reconciliation_is_inspect_only_and_never_redispatches():
    transport = _OwnerTransport()
    client = SoulOwnerClient("http://127.0.0.1:5000", transport=transport)
    owner = SoulExecutionOwner(client, SoulObservationPort(
        owner_client=client, vision=_Vision()
    ).exchange)
    runtime_intent = RuntimeIntent(
        intent_id="runtime-intent-1",
        cycle=1,
        revision=0,
        intent=Intent("soul.reply.pending_inbound.v1", {
            "contract_version": "v1",
            "application_intent_id": "intent-1",
            "trial_id": "trial-1",
            "scope_ref": "scope-1",
            "conversation_revision": "revision-1",
            "draft_sha256": "a" * 64,
            "draft_length": 10,
        }),
        phase="dispatching",
        reservation_id="owner-1",
        receipt=None,
        created_at="2026-08-10T00:00:00Z",
        finalized_at=None,
    )
    reconciliation = owner.reconcile(_instance(), runtime_intent)
    assert reconciliation.outcome.status == "confirmed_success"
    assert reconciliation.outcome.terminal is False
    assert reconciliation.receipt == ExecutionReceipt("owner-1", True, "confirmed")
    assert [call[:2] for call in transport.calls] == [
        ("GET", "/api/application-owner/v1/soul/intents/owner-1")
    ]


def test_reconciliation_defers_active_dispatch_and_only_reinspects_owner():
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def active_dispatch(method, path, payload):
        calls.append((method, path, payload))
        return {
            "contract_version": "v1",
            "status": "active_dispatch",
            "owner_ref": "owner-1",
        }

    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=active_dispatch
    )
    owner = SoulExecutionOwner(
        client,
        SoulObservationPort(owner_client=client, vision=_Vision()).exchange,
    )
    runtime_intent = RuntimeIntent(
        intent_id="runtime-intent-active",
        cycle=1,
        revision=0,
        intent=Intent("soul.reply.pending_inbound.v1", {
            "contract_version": "v1",
            "application_intent_id": "intent-active",
            "trial_id": "trial-active",
            "scope_ref": "scope-active",
            "conversation_revision": "revision-active",
            "draft_sha256": "a" * 64,
            "draft_length": 10,
        }),
        phase="reconciled",
        reservation_id="owner-1",
        receipt=ExecutionReceipt("owner-1", False, "active_dispatch"),
        created_at="2026-08-10T00:00:00Z",
        finalized_at=None,
    )

    reconciliation = owner.reconcile(_instance(), runtime_intent)

    assert reconciliation.outcome == Outcome(
        "unconfirmed",
        "soul_reply_dispatch_in_flight",
        False,
        terminal=False,
    )
    assert reconciliation.receipt == ExecutionReceipt(
        "owner-1", False, "active_dispatch"
    )
    assert reconciliation.retry_after_seconds == 1.0
    assert calls == [
        ("GET", "/api/application-owner/v1/soul/intents/owner-1", None)
    ]


def test_reconciliation_transport_outage_retries_by_get_only_until_confirmed():
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def flaky_inspect(method, path, payload):
        calls.append((method, path, payload))
        if len(calls) <= 2:
            raise OSError("temporary owner outage")
        return {
            "contract_version": "v1",
            "status": "confirmed",
            "owner_ref": "owner-1",
        }

    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=flaky_inspect
    )
    owner = SoulExecutionOwner(
        client,
        SoulObservationPort(owner_client=client, vision=_Vision()).exchange,
    )
    runtime_intent = RuntimeIntent(
        intent_id="runtime-intent-flaky-inspect",
        cycle=1,
        revision=0,
        intent=Intent("soul.reply.pending_inbound.v1", {
            "contract_version": "v1",
            "application_intent_id": "intent-flaky-inspect",
            "trial_id": "trial-flaky-inspect",
            "scope_ref": "scope-flaky-inspect",
            "conversation_revision": "revision-flaky-inspect",
            "draft_sha256": "a" * 64,
            "draft_length": 10,
        }),
        phase="dispatching",
        reservation_id="owner-1",
        receipt=None,
        created_at="2026-08-10T00:00:00Z",
        finalized_at=None,
    )

    first = owner.reconcile(_instance(), runtime_intent)
    second = owner.reconcile(_instance(), runtime_intent)
    settled = owner.reconcile(_instance(), runtime_intent)

    for retry in (first, second):
        assert retry.outcome.status == "unconfirmed"
        assert retry.outcome.terminal is False
        assert retry.retry_after_seconds is not None
    assert settled.outcome.status == "confirmed_success"
    assert settled.retry_after_seconds is None
    assert calls == [
        ("GET", "/api/application-owner/v1/soul/intents/owner-1", None),
        ("GET", "/api/application-owner/v1/soul/intents/owner-1", None),
        ("GET", "/api/application-owner/v1/soul/intents/owner-1", None),
    ]


@pytest.mark.parametrize("status", ["stale_preflight", "preclick_rejected"])
def test_reconciliation_definite_preclick_failure_is_nonterminal_replan(status):
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def definite_not_sent(method, path, payload):
        calls.append((method, path, payload))
        return {
            "contract_version": "v1",
            "status": status,
            "owner_ref": "owner-1",
        }

    client = SoulOwnerClient(
        "http://127.0.0.1:5000", transport=definite_not_sent
    )
    owner = SoulExecutionOwner(
        client,
        SoulObservationPort(owner_client=client, vision=_Vision()).exchange,
    )
    runtime_intent = RuntimeIntent(
        intent_id=f"runtime-intent-{status}",
        cycle=1,
        revision=0,
        intent=Intent("soul.reply.pending_inbound.v1", {
            "contract_version": "v1",
            "application_intent_id": f"intent-{status}",
            "trial_id": f"trial-{status}",
            "scope_ref": f"scope-{status}",
            "conversation_revision": f"revision-{status}",
            "draft_sha256": "a" * 64,
            "draft_length": 10,
        }),
        phase="dispatching",
        reservation_id="owner-1",
        receipt=None,
        created_at="2026-08-10T00:00:00Z",
        finalized_at=None,
    )

    result = owner.reconcile(_instance(), runtime_intent)

    assert result.outcome == Outcome(
        "confirmed_failure",
        "soul_reply_reconciled_terminal_no_replay",
        False,
        terminal=False,
    )
    assert result.receipt == ExecutionReceipt("owner-1", False, status)
    assert result.retry_after_seconds is None
    assert calls == [
        ("GET", "/api/application-owner/v1/soul/intents/owner-1", None)
    ]


def test_reconciliation_finds_remote_reservation_by_application_intent_without_resend():
    transport = _OwnerTransport()
    client = SoulOwnerClient("http://127.0.0.1:5000", transport=transport)
    owner = SoulExecutionOwner(client, SoulObservationPort(
        owner_client=client, vision=_Vision()
    ).exchange)
    projected_intent = SoulPersistenceProjection().project_intent(Intent(
        "soul.reply.pending_inbound.v1",
        {
            "contract_version": "v1",
            "application_intent_id": "intent-1",
            "trial_id": "trial-1",
            "scope_ref": "scope-1",
            "conversation_revision": "revision-1",
            "draft": "body is projected before recovery",
        },
    ))
    runtime_intent = RuntimeIntent(
        intent_id="runtime-intent-2",
        cycle=1,
        revision=0,
        intent=projected_intent,
        phase="open",
        reservation_id=None,
        receipt=None,
        created_at="2026-08-10T00:00:00Z",
        finalized_at=None,
    )
    reconciliation = owner.reconcile(_instance(), runtime_intent)
    assert reconciliation.outcome.status == "confirmed_success"
    assert reconciliation.receipt.receipt_id == "owner-1"
    assert [call[:2] for call in transport.calls] == [
        (
            "GET",
            "/api/application-owner/v1/soul/application-intents/intent-1",
        )
    ]


def test_reconciliation_closes_remote_reserve_local_lineage_crash_window(tmp_path):
    transport = _OwnerTransport()
    client = SoulOwnerClient("http://127.0.0.1:5000", transport=transport)
    learning = ReplyLearningStore(tmp_path / "recovery-learning.db")
    learning.begin_trial(TrialDraft(
        trial_id="trial-1",
        application_intent_id="intent-1",
        instance_id="instance-1",
        before_evidence_id="before-recovery",
        conversation_ref="1" * 64,
        pending_generation_ref="2" * 64,
        transcript_revision="3" * 64,
        scope_commitment_sha256="4" * 64,
        draft_sha256="5" * 64,
        strategy=learning.recommend_strategy().strategy,
        prompt_version="prompt-v1",
        persona_version="persona-v1",
        memory_version=0,
        provider="cloud_openai_compatible",
        model="cloud-model",
    ))
    owner = SoulExecutionOwner(
        client,
        SoulObservationPort(owner_client=client, vision=_Vision()).exchange,
        learning,
    )
    projected = SoulPersistenceProjection().project_intent(Intent(
        "soul.reply.pending_inbound.v1",
        {
            "contract_version": "v1",
            "application_intent_id": "intent-1",
            "trial_id": "trial-1",
            "scope_ref": "scope-1",
            "conversation_revision": "revision-1",
            "draft": "lost process-local body",
        },
    ))
    runtime_intent = RuntimeIntent(
        "runtime-intent-3", 1, 0, projected, "open", None, None,
        "2026-08-10T00:00:00Z", None,
    )
    result = owner.reconcile(_instance(), runtime_intent)
    trial = learning.get_trial("trial-1")
    assert result.outcome.status == "confirmed_success"
    assert trial is not None and trial["owner_ref"] == "owner-1"
    assert trial["send_status"] == "confirmed"


def test_reconciliation_treats_authoritative_missing_remote_intent_as_no_send():
    def missing(_method, path, _payload):
        assert "/application-intents/" in path
        raise SoulApplicationError("owner_intent_not_found")

    client = SoulOwnerClient("http://127.0.0.1:5000", transport=missing)
    owner = SoulExecutionOwner(
        client, SoulObservationPort(owner_client=client, vision=_Vision()).exchange
    )
    projected = SoulPersistenceProjection().project_intent(Intent(
        "soul.reply.pending_inbound.v1",
        {
            "contract_version": "v1",
            "application_intent_id": "intent-missing",
            "trial_id": "trial-missing",
            "scope_ref": "scope-missing",
            "conversation_revision": "revision-missing",
            "draft": "never reserved",
        },
    ))
    result = owner.reconcile(_instance(), RuntimeIntent(
        "runtime-intent-missing", 1, 0, projected, "open", None, None,
        "2026-08-10T00:00:00Z", None,
    ))
    assert result.outcome.status == "confirmed_failure"
    assert result.outcome.terminal is False
    assert result.outcome.hard_risk is False
    assert result.receipt == ExecutionReceipt(
        None, False, "owner_intent_not_found"
    )


def test_owner_adapter_bounds_even_injected_responses():
    def huge(_method, _path, _payload):
        return {
            "contract_version": "v1",
            "status": "confirmed",
            "padding": "x" * (24 * 1024 * 1024),
        }

    client = SoulOwnerClient("http://127.0.0.1:5000", transport=huge)
    with pytest.raises(SoulApplicationError, match="soul_owner_response_too_large"):
        client.inspect("owner-1")


def test_full_application_runtime_cycle_redacts_draft_and_waits_for_next_inbound(tmp_path):
    class StatefulOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.sent = False
            self.observations = 0

        def __call__(self, method, path, payload):
            if path.endswith("/observations") and self.sent:
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "scope": "no_due_pending_inbound",
                    "expires_in_seconds": 0,
                    "transcript": [],
                }
            result = super().__call__(method, path, payload)
            if path.endswith("/observations"):
                self.observations += 1
                result = dict(result)
                result["scope_ref"] = f"scope-{self.observations}"
                result["conversation_revision"] = (
                    f"revision-{self.observations}"
                )
            if path.endswith("/dispatch"):
                self.sent = True
            return result

    transport = StatefulOwner()
    learning = ReplyLearningStore(tmp_path / "reply-learning.db")
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
        learning=learning,
    )
    runtime = ApplicationRuntime(
        tmp_path / "application-runtime.db",
        profile="soul-reply-v1",
        memory_scope="soul-reply-v1",
        observation_port=ports.observation_port,
        policy=ports.policy,
        execution_owner=ports.execution_owner,
        verifier=ports.verifier,
        memory_gate=ports.memory_gate,
        persistence_projection=ports.persistence_projection,
    )
    started = runtime.start(
        "soul-reply-v1", "start-soul-1", initial_input="语气自然一点"
    )
    deadline = time.monotonic() + 2
    state = runtime.inspect(started.instance_id)
    while time.monotonic() < deadline:
        state = runtime.inspect(started.instance_id)
        if state.status == "waiting" and state.outcomes:
            break
        time.sleep(0.01)
    runtime.shutdown()

    assert state.status == "waiting"
    assert state.outcomes[-1].status == "confirmed_success"
    assert state.outcomes[-1].terminal is False
    assert state.memory_version == 0
    assert len([call for call in transport.calls if call[1].endswith("/dispatch")]) == 1
    persisted_args = state.intents[-1].intent.arguments
    assert "draft" not in persisted_args and "draft_sha256" in persisted_args
    trial = learning.get_trial(persisted_args["trial_id"])
    assert trial is not None and trial["send_status"] == "confirmed"
    assert learning.recommend_strategy().explicit_outcomes == 0
    durable_text = ""
    for database in (
        tmp_path / "application-runtime.db",
        tmp_path / "reply-learning.db",
    ):
        connection = sqlite3.connect(database)
        try:
            durable_text += "\n".join(connection.iterdump())
        finally:
            connection.close()
    assert "今天过得怎么样" not in durable_text
    assert "还不错，刚忙完一阵" not in durable_text
    assert base64.b64encode(PNG).decode("ascii") not in durable_text


def _wait_for_application_state(runtime, instance_id, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    state = runtime.inspect(instance_id)
    while time.monotonic() < deadline:
        state = runtime.inspect(instance_id)
        if predicate(state):
            return state
        time.sleep(0.01)
    return state


def _runtime_for_ports(tmp_path, ports, suffix):
    return ApplicationRuntime(
        tmp_path / f"application-runtime-{suffix}.db",
        profile="soul-reply-v1",
        memory_scope="soul-reply-v1",
        observation_port=ports.observation_port,
        policy=ports.policy,
        execution_owner=ports.execution_owner,
        verifier=ports.verifier,
        memory_gate=ports.memory_gate,
        persistence_projection=ports.persistence_projection,
    )


def test_owner_observation_outage_recovers_the_same_instance_without_dispatch(tmp_path):
    class RecoveringOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.observations = 0

        def __call__(self, method, path, payload):
            if path.endswith("/observations"):
                self.calls.append((method, path, payload))
                self.observations += 1
                if self.observations == 1:
                    raise OSError("temporary owner outage")
                return {
                    "contract_version": "v1",
                    "scope": "no_due_pending_inbound",
                    "expires_in_seconds": 0,
                    "transcript": [],
                }
            return super().__call__(method, path, payload)

    transport = RecoveringOwner()
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
    )
    runtime = _runtime_for_ports(tmp_path, ports, "observe-recovery")
    started = runtime.start("soul-reply-v1", "start-observe-recovery")
    waiting_after_outage = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda state: state.status == "waiting" and state.degraded,
    )
    assert waiting_after_outage.status == "waiting"

    runtime.command(
        started.instance_id,
        Input("依赖恢复后继续"),
        "wake-observe-recovery",
    )
    recovered = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda state: state.status == "waiting" and state.revision == 1,
    )
    runtime.shutdown()

    assert recovered.instance_id == started.instance_id
    assert recovered.status == "waiting"
    assert len(runtime.list()) == 1
    assert not any(call[1].endswith("/dispatch") for call in transport.calls)


def test_cloud_outage_recovers_same_instance_and_dispatches_exactly_once(tmp_path):
    class StatefulOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.sent = False
            self.observations = 0

        def __call__(self, method, path, payload):
            if path.endswith("/observations") and self.sent:
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "scope": "no_due_pending_inbound",
                    "expires_in_seconds": 0,
                    "transcript": [],
                }
            result = super().__call__(method, path, payload)
            if path.endswith("/observations"):
                self.observations += 1
                result = dict(result)
                result["scope_ref"] = f"scope-{self.observations}"
                result["conversation_revision"] = (
                    f"revision-{self.observations}"
                )
            if path.endswith("/dispatch"):
                self.sent = True
            return result

    class RecoveringProvider(_CloudProvider):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def complete(self, messages, *, json_response, is_cancelled):
            self.attempts += 1
            if self.attempts == 1:
                raise ChatProviderError("provider_unavailable", "temporary")
            return super().complete(
                messages,
                json_response=json_response,
                is_cancelled=is_cancelled,
            )

    transport = StatefulOwner()
    provider = RecoveringProvider()
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: provider,
    )
    runtime = _runtime_for_ports(tmp_path, ports, "cloud-recovery")
    started = runtime.start("soul-reply-v1", "start-cloud-recovery")
    _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda state: state.status == "waiting" and state.degraded,
    )
    runtime.command(
        started.instance_id,
        Input("恢复后继续"),
        "wake-cloud-recovery",
    )
    recovered = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda state: state.status == "waiting" and bool(state.outcomes),
    )
    runtime.shutdown()

    assert recovered.instance_id == started.instance_id
    assert recovered.outcomes[-1].status == "confirmed_success"
    assert provider.attempts == 2
    assert transport.observations == 2
    assert len(
        [call for call in transport.calls if call[1].endswith("/dispatch")]
    ) == 1
    reserve_calls = [
        call for call in transport.calls if call[1].endswith("/intents")
    ]
    assert len(reserve_calls) == 1
    assert reserve_calls[0][2]["scope_ref"] == "scope-2"


def test_after_inspect_outages_reconcile_by_get_only_without_redispatch(tmp_path):
    class RecoveringInspectOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.inspect_attempts = 0
            self.sent = False

        def __call__(self, method, path, payload):
            if method == "GET" and path.endswith("/owner-1"):
                self.calls.append((method, path, payload))
                self.inspect_attempts += 1
                if self.inspect_attempts <= 2:
                    raise OSError("temporary inspect outage")
                return {
                    "contract_version": "v1",
                    "status": "confirmed",
                    "owner_ref": "owner-1",
                }
            if path.endswith("/observations") and self.sent:
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "scope": "no_due_pending_inbound",
                    "expires_in_seconds": 0,
                    "transcript": [],
                }
            result = super().__call__(method, path, payload)
            if path.endswith("/dispatch"):
                self.sent = True
            return result

    transport = RecoveringInspectOwner()
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
    )
    runtime = _runtime_for_ports(tmp_path, ports, "inspect-recovery")
    started = runtime.start("soul-reply-v1", "start-inspect-recovery")
    for revision in (1, 2):
        _wait_for_application_state(
            runtime,
            started.instance_id,
            lambda state: state.status == "waiting" and state.wake_at is not None,
        )
        runtime.command(
            started.instance_id,
            Input(f"wake inspect {revision}"),
            f"wake-inspect-recovery-{revision}",
        )
    recovered = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda state: state.status == "waiting" and bool(state.outcomes),
    )
    runtime.shutdown()

    assert recovered.outcomes[-1].status == "confirmed_success"
    assert len(
        [call for call in transport.calls if call[1].endswith("/dispatch")]
    ) == 1
    assert transport.inspect_attempts == 3
    post_dispatch = transport.calls[
        next(
            index
            for index, call in enumerate(transport.calls)
            if call[1].endswith("/dispatch")
        )
        + 1 :
    ]
    assert all(
        method == "GET" or path.endswith("/observations")
        for method, path, _payload in post_dispatch
    )


@pytest.mark.parametrize("direct_status", ["stale_preflight", "preclick_rejected"])
def test_definite_preclick_replans_without_duplicate_dispatch(tmp_path, direct_status):
    class PreclickRejectedOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.rejected = False

        def __call__(self, method, path, payload):
            if path.endswith("/dispatch"):
                self.calls.append((method, path, payload))
                self.rejected = True
                return {
                    "contract_version": "v1",
                    "status": direct_status,
                    "owner_ref": "owner-1",
                }
            if method == "GET" and path.endswith("/owner-1"):
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "status": "terminal_no_replay",
                    "owner_ref": "owner-1",
                }
            if path.endswith("/observations") and self.rejected:
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "scope": "no_due_pending_inbound",
                    "expires_in_seconds": 0,
                    "transcript": [],
                }
            return super().__call__(method, path, payload)

    transport = PreclickRejectedOwner()
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
    )
    runtime = _runtime_for_ports(tmp_path, ports, f"replan-{direct_status}")
    started = runtime.start(
        "soul-reply-v1", f"start-replan-{direct_status}"
    )
    state = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda current: current.status == "waiting" and bool(current.outcomes),
    )
    runtime.shutdown()

    assert state.outcomes[-1].status == "confirmed_failure"
    assert state.outcomes[-1].terminal is False
    assert state.status == "waiting"
    assert len(
        [call for call in transport.calls if call[1].endswith("/dispatch")]
    ) == 1


def test_negative_delayed_outcome_changes_next_strategy_arm(tmp_path):
    store = ReplyLearningStore(tmp_path / "evolving.db")
    first = store.recommend_strategy()
    store.begin_trial(TrialDraft(
        trial_id="trial-evolve",
        application_intent_id="intent-evolve",
        instance_id="instance-evolve",
        before_evidence_id="before-evolve",
        conversation_ref="c" * 64,
        pending_generation_ref="d" * 64,
        transcript_revision="e" * 64,
        scope_commitment_sha256="f" * 64,
        draft_sha256="a" * 64,
        strategy=first.strategy,
        prompt_version="prompt-v1",
        persona_version="persona-v1",
        memory_version=first.revision,
        provider="cloud_openai_compatible",
        model="cloud-model",
    ))
    store.bind_owner("trial-evolve", "owner-evolve", "reserved")
    store.record_send_proof(
        "trial-evolve",
        owner_ref="owner-evolve",
        status="confirmed",
        proof_ref="send-evolve",
    )
    store.record_delayed_outcome(
        "trial-evolve", DelayedOutcomeEvidence("negative_engagement", "outcome-evolve")
    )
    second = store.recommend_strategy()
    assert second.strategy_key != first.strategy_key
    assert second.explicit_outcomes == 0
    assert second.revision == 1


def test_no_response_requires_explicit_timeout_and_no_new_inbound_proof(tmp_path):
    store = ReplyLearningStore(tmp_path / "timeout.db", no_response_seconds=60)
    store.begin_trial(TrialDraft(
        trial_id="trial-timeout",
        application_intent_id="intent-timeout",
        instance_id="instance-timeout",
        before_evidence_id="before-timeout",
        conversation_ref="1" * 64,
        pending_generation_ref="2" * 64,
        transcript_revision="3" * 64,
        scope_commitment_sha256="4" * 64,
        draft_sha256="5" * 64,
        strategy=store.recommend_strategy().strategy,
        prompt_version="prompt-v1",
        persona_version="persona-v1",
        memory_version=0,
        provider="cloud_openai_compatible",
        model="cloud-model",
    ))
    store.bind_owner("trial-timeout", "owner-timeout", "reserved")
    store.record_send_proof(
        "trial-timeout",
        owner_ref="owner-timeout",
        status="confirmed",
        proof_ref="send-timeout",
    )
    with pytest.raises(SoulApplicationError, match="no_response_evidence_incomplete"):
        store.record_delayed_outcome(
            "trial-timeout", DelayedOutcomeEvidence("no_response", "timeout-proof")
        )
    result = store.record_delayed_outcome(
        "trial-timeout",
        DelayedOutcomeEvidence(
            "no_response",
            "timeout-proof",
            elapsed_seconds=60,
            no_new_inbound_confirmed=True,
        ),
    )
    assert result.beta == 2.0 and result.explicit_outcomes == 1


def _dispatch_intent(*, application_intent_id: str = "intent-1") -> Intent:
    return Intent(
        "soul.reply.pending_inbound.v1",
        {
            "contract_version": "v1",
            "application_intent_id": application_intent_id,
            "trial_id": "trial-1",
            "scope_ref": "scope-1",
            "conversation_revision": "revision-1",
            "draft": "今天还挺充实的，你呢？",
        },
    )


@pytest.mark.parametrize(
    "direct_status",
    ["legacy_scheduler_active", "soul_execution_runtime_unavailable"],
)
def test_direct_transient_dispatch_then_terminal_owner_inspect_replans_without_hard_risk(
    direct_status,
):
    before = Observation(
        "before-1",
        fresh=True,
        data={
            "phase": "before",
            "contract_version": "v1",
            "owner_scope_ref": "scope-1",
            "conversation_revision": "revision-1",
        },
    )
    after = Observation(
        "after-1",
        fresh=True,
        data={
            "phase": "after",
            "contract_version": "v1",
            "owner_ref": "owner-1",
            "owner_status": "terminal_no_replay",
        },
    )

    outcome = SoulReplyVerifier().verify(
        VerificationContext(
            _instance(),
            _dispatch_intent(),
            before,
            after,
            ExecutionReceipt("owner-1", False, direct_status),
        )
    )

    assert outcome == Outcome(
        "confirmed_failure",
        "soul_reply_terminal_no_replay",
        False,
        terminal=False,
    )


def test_direct_active_dispatch_and_immediate_active_inspect_stays_get_only_deferred():
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def active_owner(method, path, payload):
        calls.append((method, path, payload))
        return {
            "contract_version": "v1",
            "status": "active_dispatch",
            "owner_ref": "owner-1",
        }

    client = SoulOwnerClient("http://127.0.0.1:5000", transport=active_owner)
    observation = SoulObservationPort(owner_client=client, vision=_Vision())
    owner = SoulExecutionOwner(client, observation.exchange)
    instance = _instance()
    intent = _dispatch_intent()

    receipt = owner.dispatch("owner-1", instance, intent)
    after = observation.observe(instance)
    reconciliation = owner.reconcile(
        instance,
        RuntimeIntent(
            "runtime-intent-active-direct",
            1,
            0,
            intent,
            "dispatching",
            "owner-1",
            receipt,
            "2026-08-10T00:00:00Z",
            None,
        ),
    )

    assert after.data["owner_status"] == "active_dispatch"
    assert reconciliation.outcome == Outcome(
        "unconfirmed",
        "soul_reply_dispatch_in_flight",
        False,
        terminal=False,
    )
    assert reconciliation.receipt == ExecutionReceipt(
        "owner-1", False, "active_dispatch"
    )
    assert reconciliation.retry_after_seconds == 1.0
    assert [call[:2] for call in calls] == [
        ("POST", "/api/application-owner/v1/soul/intents/owner-1/dispatch"),
        ("GET", "/api/application-owner/v1/soul/intents/owner-1"),
        ("GET", "/api/application-owner/v1/soul/intents/owner-1"),
    ]


def test_runtime_active_dispatch_defers_to_reconciliation_without_redispatch(tmp_path):
    class ActiveDispatchOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.inspect_attempts = 0

        def __call__(self, method, path, payload):
            if path.endswith("/dispatch"):
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "status": "active_dispatch",
                    "owner_ref": "owner-1",
                }
            if method == "GET" and path.endswith("/owner-1"):
                self.calls.append((method, path, payload))
                self.inspect_attempts += 1
                return {
                    "contract_version": "v1",
                    "status": "active_dispatch",
                    "owner_ref": "owner-1",
                }
            return super().__call__(method, path, payload)

    transport = ActiveDispatchOwner()
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
    )
    runtime = _runtime_for_ports(tmp_path, ports, "active-dispatch-runtime")
    started = runtime.start("soul-reply-v1", "start-active-dispatch-runtime")
    state = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda current: (
            current.status == "waiting"
            and current.wake_at is not None
            and transport.inspect_attempts >= 2
        ),
        timeout=3.2,
    )
    runtime.shutdown()

    assert state.status == "waiting"
    assert state.hard_risk is False
    assert state.outcomes == ()
    assert len(
        [call for call in transport.calls if call[1].endswith("/dispatch")]
    ) == 1
    assert transport.inspect_attempts >= 2
    assert all(
        method == "GET"
        for method, path, _payload in transport.calls
        if path.endswith("/owner-1")
    )


@pytest.mark.parametrize(
    ("settlement_status", "expected_status", "expected_outcome", "expected_hard", "expected_terminal"),
    [
        ("confirmed", "waiting", "confirmed_success", False, False),
        ("terminal_no_replay", "failed", "confirmed_failure", False, True),
        ("uncertain_needs_reconciliation", "failed", "uncertain", True, True),
    ],
)
def test_runtime_active_dispatch_uses_second_owner_inspection_for_settlement(
    tmp_path,
    settlement_status,
    expected_status,
    expected_outcome,
    expected_hard,
    expected_terminal,
):
    class SettlingActiveOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.inspect_attempts = 0
            self.settled = False

        def __call__(self, method, path, payload):
            if path.endswith("/dispatch"):
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "status": "active_dispatch",
                    "owner_ref": "owner-1",
                }
            if method == "GET" and path.endswith("/owner-1"):
                self.calls.append((method, path, payload))
                self.inspect_attempts += 1
                if self.inspect_attempts >= 2:
                    self.settled = True
                return {
                    "contract_version": "v1",
                    "status": settlement_status,
                    "owner_ref": "owner-1",
                }
            if path.endswith("/observations") and self.settled:
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "scope": "no_due_pending_inbound",
                    "expires_in_seconds": 0,
                    "transcript": [],
                }
            return super().__call__(method, path, payload)

    transport = SettlingActiveOwner()
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
    )
    runtime = _runtime_for_ports(
        tmp_path, ports, f"active-settlement-{settlement_status}"
    )
    started = runtime.start("soul-reply-v1", f"start-{settlement_status}")
    state = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda current: (
            transport.inspect_attempts >= 2
            and current.status == expected_status
            and bool(current.outcomes)
        ),
    )
    runtime.shutdown()

    outcome = state.outcomes[-1]
    assert state.status == expected_status
    assert (
        outcome.status,
        outcome.hard_risk,
        outcome.terminal,
    ) == (expected_outcome, expected_hard, expected_terminal)
    assert len(
        [call for call in transport.calls if call[1].endswith("/dispatch")]
    ) == 1
    assert transport.inspect_attempts >= 2
    assert all(
        method == "GET"
        for method, path, _payload in transport.calls
        if path.endswith("/owner-1")
    )


@pytest.mark.parametrize(
    "direct_status",
    ["legacy_scheduler_active", "soul_execution_runtime_unavailable"],
)
def test_runtime_direct_transient_dispatch_then_terminal_inspect_replans_once(
    tmp_path, direct_status
):
    class TerminalizingOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.terminalized = False
            self.observations = 0

        def __call__(self, method, path, payload):
            if path.endswith("/dispatch"):
                self.calls.append((method, path, payload))
                self.terminalized = True
                return {
                    "contract_version": "v1",
                    "status": direct_status,
                    "owner_ref": "owner-1",
                }
            if method == "GET" and path.endswith("/owner-1"):
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "status": "terminal_no_replay",
                    "owner_ref": "owner-1",
                }
            if path.endswith("/observations"):
                self.calls.append((method, path, payload))
                self.observations += 1
                if self.terminalized:
                    return {
                        "contract_version": "v1",
                        "scope": "no_due_pending_inbound",
                        "expires_in_seconds": 0,
                        "transcript": [],
                    }
            return super().__call__(method, path, payload)

    transport = TerminalizingOwner()
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
    )
    runtime = _runtime_for_ports(tmp_path, ports, f"direct-transient-{direct_status}")
    started = runtime.start("soul-reply-v1", f"start-{direct_status}")
    state = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda current: (
            current.status == "waiting"
            and bool(current.outcomes)
            and transport.observations >= 2
        ),
    )
    runtime.shutdown()

    assert state.hard_risk is False
    outcome = state.outcomes[-1]
    assert (
        outcome.status,
        outcome.evidence,
        outcome.hard_risk,
        outcome.terminal,
    ) == ("confirmed_failure", "soul_reply_terminal_no_replay", False, False)
    assert len(
        [call for call in transport.calls if call[1].endswith("/dispatch")]
    ) == 1
    assert transport.observations >= 2


def test_confirmed_dispatch_survives_learning_send_proof_sqlite_failure():
    class ProofFailure:
        def bind_owner(self, *args, **kwargs):
            del args, kwargs
            return {}

        def record_send_proof(self, *args, **kwargs):
            del args, kwargs
            raise sqlite3.OperationalError("learning database temporarily locked")

    client = SoulOwnerClient("http://127.0.0.1:5000", transport=_OwnerTransport())
    owner = SoulExecutionOwner(
        client,
        SoulObservationPort(owner_client=client, vision=_Vision()).exchange,
        ProofFailure(),
    )

    receipt = owner.dispatch("owner-1", _instance(), _dispatch_intent())

    assert receipt == ExecutionReceipt("owner-1", True, "confirmed")


def test_learning_begin_failure_is_retryable_before_owner_reserve():
    class BeginFailure:
        def begin_trial(self, draft):
            del draft
            raise sqlite3.OperationalError("learning database temporarily locked")

    class BindFailureOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.sent = False

        def __call__(self, method, path, payload):
            if path.endswith("/observations") and self.sent:
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "scope": "no_due_pending_inbound",
                    "expires_in_seconds": 0,
                    "transcript": [],
                }
            response = super().__call__(method, path, payload)
            if path.endswith("/dispatch"):
                self.sent = True
            return response

    transport = BindFailureOwner()
    client = SoulOwnerClient("http://127.0.0.1:5000", transport=transport)
    observation = SoulObservationPort(owner_client=client, vision=_Vision())
    owner = SoulExecutionOwner(client, observation.exchange, BeginFailure())
    trial = TrialDraft(
        trial_id="trial-1",
        application_intent_id="intent-1",
        instance_id="instance-1",
        before_evidence_id="before-1",
        conversation_ref="1" * 64,
        pending_generation_ref="2" * 64,
        transcript_revision="3" * 64,
        scope_commitment_sha256="4" * 64,
        draft_sha256="5" * 64,
        strategy={"reply_length": "short", "question_usage": "one", "tone": "natural"},
        prompt_version="prompt-v1",
        persona_version="persona-v1",
        memory_version=0,
        provider="cloud_openai_compatible",
        model="cloud-model",
    )
    observation.exchange.bind_trial("instance-1", trial)

    with pytest.raises(RetryableApplicationError):
        owner.reserve(_instance(), _dispatch_intent())

    assert transport.calls == []


def test_remote_reserve_then_learning_bind_failure_keeps_one_core_fenced_dispatch(
    tmp_path,
):
    class BindFailureStore(ReplyLearningStore):
        def bind_owner(self, *args, **kwargs):
            del args, kwargs
            raise OSError("learning storage unavailable")

        def record_send_proof(self, *args, **kwargs):
            del args, kwargs
            raise OSError("learning storage unavailable")

    class BindFailureOwner(_OwnerTransport):
        def __init__(self):
            super().__init__()
            self.sent = False

        def __call__(self, method, path, payload):
            if path.endswith("/observations") and self.sent:
                self.calls.append((method, path, payload))
                return {
                    "contract_version": "v1",
                    "scope": "no_due_pending_inbound",
                    "expires_in_seconds": 0,
                    "transcript": [],
                }
            response = super().__call__(method, path, payload)
            if path.endswith("/dispatch"):
                self.sent = True
            return response

    transport = BindFailureOwner()
    learning = BindFailureStore(tmp_path / "bind-failure-learning.db")
    ports = build_soul_application_ports(
        owner_client=SoulOwnerClient(
            "http://127.0.0.1:5000", transport=transport
        ),
        vision=_Vision(),
        cloud_provider_resolver=lambda: _CloudProvider(),
        learning=learning,
    )
    runtime = _runtime_for_ports(tmp_path, ports, "bind-failure")
    started = runtime.start("soul-reply-v1", "start-bind-failure")
    state = _wait_for_application_state(
        runtime,
        started.instance_id,
        lambda current: current.status == "waiting" and bool(current.outcomes),
    )
    runtime.shutdown()

    assert state.outcomes[-1].status == "confirmed_success"
    assert len([call for call in transport.calls if call[1].endswith("/intents")]) == 1
    assert len(
        [call for call in transport.calls if call[1].endswith("/dispatch")]
    ) == 1
