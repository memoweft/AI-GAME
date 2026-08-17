from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol

from ..execution import GuiAction
from ..gui_owl_client import (
    GuiOwlClientError,
    GuiOwlTransport,
    OpenAICompatibleGuiOwlClient,
    _completion_content,
    _loopback_chat_completions_endpoint,
)
from .domain import Observation, OutcomeVerification, TransportReceipt
from .profiles import CompiledProfileTask, PACKAGE_NAME, StzbTutorialProfile


VerificationStatus = Literal[
    "confirmed_success",
    "confirmed_progress",
    "unconfirmed",
    "failed",
    "unsafe",
]

_ALLOWED_SCENES = {
    "main_scene",
    "tutorial",
    "task_list",
    "general_list",
    "army_list",
    "map",
    "allowed_menu",
    "unknown",
}
_ALLOWED_MARKERS = {
    "tutorial_step_advanced",
    "tutorial_prompt_changed",
    "allowed_menu_opened",
    "task_list_visible",
    "task_entry_visible",
    "general_list_visible",
    "general_entry_visible",
    "army_list_visible",
    "army_entry_visible",
    "map_visible",
    "map_entry_visible",
    "main_scene_visible",
    "allowed_menu_closed",
    "main_scene_candidate",
}
_ALLOWED_UNSAFE_REASONS = {
    "login",
    "legal_consent",
    "identity_verification",
    "otp",
    "payment",
    "purchase",
    "loot_box",
    "permission",
    "chat",
    "alliance",
    "matchmaking",
    "human_interaction",
    "account_settings",
    "unknown_sensitive",
}
_ALLOWED_FAILURE_REASONS = {"scene_unknown", "visual_ambiguous", "obstructed"}


class LocalEvidenceVerifierError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class LocalEvidenceAssessment:
    """Sanitized local classification; raw frames never leave the assessor call."""

    package_name: str
    scene: str
    markers: tuple[str, ...]
    confidence: float
    unsafe_reason: str | None = None
    failure_reason: str | None = None


class LocalEvidenceAssessor(Protocol):
    def assess(
        self,
        observation: Observation,
        *,
        profile_id: str,
        task_id: str,
    ) -> LocalEvidenceAssessment: ...


class UnavailableLocalEvidenceAssessor:
    """Fail-closed Adapter used when no separate local verifier is configured."""

    def assess(
        self,
        observation: Observation,
        *,
        profile_id: str,
        task_id: str,
    ) -> LocalEvidenceAssessment:
        del observation, profile_id, task_id
        raise LocalEvidenceVerifierError(
            "local_evidence_verifier_not_configured",
            "本地证据验证器尚未配置，不能启动在线 LearningEpisode。",
        )


@dataclass(slots=True)
class OpenAICompatibleStzbEvidenceAssessor:
    """Separate loopback-only visual evidence call with a strict allowlist schema."""

    endpoint: str
    model: str
    api_key: str | None = None
    timeout_seconds: float = 30.0
    transport: GuiOwlTransport | None = None

    MAX_IMAGE_BYTES = 16 * 1024 * 1024

    def assess(
        self,
        observation: Observation,
        *,
        profile_id: str,
        task_id: str,
    ) -> LocalEvidenceAssessment:
        if profile_id != "stzb-tutorial-v1":
            raise LocalEvidenceVerifierError(
                "evidence_profile_mismatch",
                "本地证据验证器不支持该 GameProfile。",
            )
        image = observation.payload
        if (
            image is None
            or not image.startswith(b"\x89PNG\r\n\x1a\n")
            or len(image) > self.MAX_IMAGE_BYTES
        ):
            raise LocalEvidenceVerifierError(
                "evidence_image_invalid",
                "本地证据画面无效。",
            )
        if not self.model.strip():
            raise LocalEvidenceVerifierError(
                "local_evidence_verifier_not_configured",
                "本地证据验证模型尚未配置。",
            )
        try:
            endpoint = _loopback_chat_completions_endpoint(self.endpoint)
        except GuiOwlClientError as exc:
            raise LocalEvidenceVerifierError(exc.code, str(exc)) from None

        schema_prompt = (
            "Inspect only the current screenshot for the stzb-tutorial-v1 profile. "
            "Return exactly one JSON object and no markdown with keys package_name, "
            "scene, markers, confidence, unsafe_reason, failure_reason. "
            f"task_id={task_id}. package_name must be {PACKAGE_NAME!r}. "
            f"scene must be one of {sorted(_ALLOWED_SCENES)!r}. "
            f"markers may contain only {sorted(_ALLOWED_MARKERS)!r}. "
            f"unsafe_reason must be null or one of {sorted(_ALLOWED_UNSAFE_REASONS)!r}. "
            f"failure_reason must be null or one of {sorted(_ALLOWED_FAILURE_REASONS)!r}. "
            "Use scene='unknown' and failure_reason='visual_ambiguous' when uncertain."
        )
        payload: dict[str, Any] = {
            "model": self.model.strip(),
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": "You are a local evidence classifier, never an action planner.",
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": schema_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image).decode("ascii")
                            },
                        },
                    ],
                },
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": 256,
        }
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            decoded = (
                self.transport(endpoint, payload, headers, self.timeout_seconds)
                if self.transport is not None
                else OpenAICompatibleGuiOwlClient._request(
                    endpoint,
                    payload,
                    headers,
                    self.timeout_seconds,
                )
            )
            content = _completion_content(decoded)
            return _parse_local_assessment(content)
        except LocalEvidenceVerifierError:
            raise
        except GuiOwlClientError as exc:
            raise LocalEvidenceVerifierError(exc.code, str(exc)) from None
        except Exception:
            raise LocalEvidenceVerifierError(
                "local_evidence_verifier_failed",
                "本地证据验证失败。",
            ) from None
        finally:
            # Do not retain raw image or model response on the Adapter.
            image = b""
            if "content" in locals():
                content = ""


# Generic export name for composition code; v1 remains deliberately STZB-specific.
OpenAICompatibleLocalEvidenceAssessor = OpenAICompatibleStzbEvidenceAssessor


def _parse_local_assessment(content: str) -> LocalEvidenceAssessment:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        raise LocalEvidenceVerifierError(
            "local_evidence_invalid_response",
            "本地证据验证器返回了无效 JSON。",
        ) from None
    if not isinstance(decoded, Mapping) or set(decoded) != {
        "package_name",
        "scene",
        "markers",
        "confidence",
        "unsafe_reason",
        "failure_reason",
    }:
        raise LocalEvidenceVerifierError(
            "local_evidence_invalid_response",
            "本地证据验证器响应结构无效。",
        )
    package_name = decoded["package_name"]
    scene = decoded["scene"]
    markers = decoded["markers"]
    confidence = decoded["confidence"]
    unsafe_reason = decoded["unsafe_reason"]
    failure_reason = decoded["failure_reason"]
    if package_name != PACKAGE_NAME:
        raise LocalEvidenceVerifierError(
            "local_evidence_package_mismatch",
            "本地证据验证器未确认率土之滨前台包。",
        )
    if not isinstance(scene, str) or scene not in _ALLOWED_SCENES:
        raise LocalEvidenceVerifierError(
            "local_evidence_invalid_response",
            "本地证据验证器返回了未知场景。",
        )
    if (
        not isinstance(markers, list)
        or len(markers) > 16
        or any(not isinstance(marker, str) or marker not in _ALLOWED_MARKERS for marker in markers)
        or len(markers) != len(set(markers))
    ):
        raise LocalEvidenceVerifierError(
            "local_evidence_invalid_response",
            "本地证据验证器返回了未知标记。",
        )
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise LocalEvidenceVerifierError(
            "local_evidence_invalid_response",
            "本地证据验证器置信度无效。",
        )
    if unsafe_reason is not None and (
        not isinstance(unsafe_reason, str) or unsafe_reason not in _ALLOWED_UNSAFE_REASONS
    ):
        raise LocalEvidenceVerifierError(
            "local_evidence_invalid_response",
            "本地证据验证器安全分类无效。",
        )
    if failure_reason is not None and (
        not isinstance(failure_reason, str) or failure_reason not in _ALLOWED_FAILURE_REASONS
    ):
        raise LocalEvidenceVerifierError(
            "local_evidence_invalid_response",
            "本地证据验证器失败分类无效。",
        )
    if scene == "unknown" and unsafe_reason is None and failure_reason is None:
        raise LocalEvidenceVerifierError(
            "local_evidence_invalid_response",
            "未知场景必须明确标记为不安全或验证失败。",
        )
    return LocalEvidenceAssessment(
        package_name=package_name,
        scene=scene,
        markers=tuple(markers),
        confidence=float(confidence),
        unsafe_reason=unsafe_reason,
        failure_reason=failure_reason,
    )


class StrictStzbOutcomeVerifier:
    """Fail-closed verifier for one compiled STZB tutorial/navigation task.

    A GUI model's terminate proposal is intentionally absent from this
    interface. Only independently assessed local before/after evidence can
    produce ``confirmed_success`` or ``confirmed_progress``.
    """

    def __init__(
        self,
        *,
        task: CompiledProfileTask,
        assessor: LocalEvidenceAssessor,
        profile: StzbTutorialProfile | None = None,
    ) -> None:
        self.task = task
        self.assessor = assessor
        self.profile = profile or StzbTutorialProfile()
        self._no_progress_count = 0

    def verify(
        self,
        *,
        before: Observation,
        action: GuiAction | None,
        transport: TransportReceipt,
        after: Observation | None,
    ) -> OutcomeVerification:
        del action  # Outcome comes from evidence, never from the proposed action itself.

        if transport.status == "uncertain":
            return self._result("unsafe", "transport_uncertain")
        if transport.status == "rejected":
            return self._result("failed", "transport_rejected")
        if after is None:
            return self._result("failed", "post_action_observation_missing")

        before_state = self.assessor.assess(
            before,
            profile_id=self.profile.profile_id,
            task_id=self.task.task_id,
        )
        after_state = self.assessor.assess(
            after,
            profile_id=self.profile.profile_id,
            task_id=self.task.task_id,
        )

        if (
            before_state.package_name != PACKAGE_NAME
            or after_state.package_name != PACKAGE_NAME
        ):
            return self._result("unsafe", "foreground_package_changed")
        unsafe_reason = before_state.unsafe_reason or after_state.unsafe_reason
        if unsafe_reason:
            return self._result("unsafe", _safe_reason(unsafe_reason, "unsafe_scene"))
        failure_reason = after_state.failure_reason
        if failure_reason:
            return self._result("failed", _safe_reason(failure_reason, "verifier_failed"))
        if min(before_state.confidence, after_state.confidence) < self.profile.min_verifier_confidence:
            return self._result("unconfirmed", "evidence_low_confidence")

        after_markers = set(after_state.markers)
        if self.task.success_marker in after_markers:
            if self.task.success_marker in set(before_state.markers):
                return OutcomeVerification(
                    confirmed=True,
                    task_succeeded=True,
                    reward=0.0,
                    detail="confirmed_success:goal_already_present",
                )
            return self._result("confirmed_success", "goal_marker_observed")

        new_markers = after_markers.difference(before_state.markers)
        if new_markers.intersection(self.task.progress_markers):
            return self._result("confirmed_progress", "progress_marker_observed")
        if before_state.scene != after_state.scene and after_state.scene:
            return self._result("confirmed_progress", "allowed_scene_changed")
        return self._result("unconfirmed", "postcondition_not_observed")

    def _result(self, status: VerificationStatus, reason: str) -> OutcomeVerification:
        if status in {"confirmed_success", "confirmed_progress"}:
            self._no_progress_count = 0
        elif status == "unconfirmed":
            self._no_progress_count += 1
            if self._no_progress_count >= self.profile.no_progress_limit:
                return _verification("failed", "no_progress_limit")
        return _verification(status, reason)


def verification_status(result: OutcomeVerification) -> VerificationStatus:
    prefix = result.detail.partition(":")[0]
    if prefix not in {
        "confirmed_success",
        "confirmed_progress",
        "unconfirmed",
        "failed",
        "unsafe",
    }:
        return "unconfirmed"
    return prefix  # type: ignore[return-value]


def _verification(status: VerificationStatus, reason: str) -> OutcomeVerification:
    values: dict[VerificationStatus, tuple[bool, bool, float]] = {
        "confirmed_success": (True, True, 1.0),
        "confirmed_progress": (True, False, 0.1),
        "unconfirmed": (False, False, 0.0),
        "failed": (True, False, -0.5),
        "unsafe": (True, False, -1.0),
    }
    confirmed, task_succeeded, reward = values[status]
    return OutcomeVerification(
        confirmed=confirmed,
        task_succeeded=task_succeeded,
        reward=reward,
        detail=f"{status}:{reason}",
    )


def _safe_reason(value: str, fallback: str) -> str:
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 80:
        return fallback
    if not all(character.isascii() and (character.isalnum() or character == "_") for character in normalized):
        return fallback
    return normalized
