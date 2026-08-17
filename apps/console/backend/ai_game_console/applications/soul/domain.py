from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .errors import SoulApplicationError


_HEX64 = re.compile(r"[0-9a-f]{64}")
_SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")
_PAGES = {"conversation_detail", "conversation_list", "other", "unknown"}
_STAGES = {"new", "early", "ongoing", "unknown"}
_TONES = {"warm", "neutral", "playful", "reserved", "unknown"}
_CUES = {
    "greeting",
    "question_present",
    "emoji_present",
    "long_message",
    "short_message",
    "composer_empty",
    "send_control_visible",
    "visual_obstruction",
}


def require_hex64(value: object, code: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise SoulApplicationError(code)
    return value


def require_safe_ref(value: object, code: str, *, maximum: int = 192) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not _SAFE_REF.fullmatch(value)
    ):
        raise SoulApplicationError(code)
    return value


@dataclass(frozen=True, slots=True)
class SoulTranscriptItem:
    role: Literal["me", "them"]
    content: str = field(repr=False)
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.role not in {"me", "them"}:
            raise SoulApplicationError("soul_owner_transcript_invalid")
        if not isinstance(self.content, str) or not self.content.strip() or len(self.content) > 4000:
            raise SoulApplicationError("soul_owner_transcript_invalid")
        if not isinstance(self.created_at, str) or len(self.created_at) > 80:
            raise SoulApplicationError("soul_owner_transcript_invalid")

    def as_cloud_data(self) -> dict[str, str]:
        # created_at is deliberately omitted. Ordering is enough for the reply
        # model and avoids turning display metadata into a behavioral feature.
        return {"role": self.role, "content": self.content.strip()}


@dataclass(frozen=True, slots=True)
class SoulVisualFacts:
    """Bounded local visual classification; it contains no identity field."""

    page: str
    pending_inbound_visible: bool
    conversation_stage: str
    tone: str
    cues: tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        if self.page not in _PAGES or self.conversation_stage not in _STAGES or self.tone not in _TONES:
            raise SoulApplicationError("local_soul_vision_invalid_response")
        if not isinstance(self.pending_inbound_visible, bool):
            raise SoulApplicationError("local_soul_vision_invalid_response")
        if (
            not isinstance(self.cues, tuple)
            or len(self.cues) > 8
            or len(set(self.cues)) != len(self.cues)
            or any(item not in _CUES for item in self.cues)
        ):
            raise SoulApplicationError("local_soul_vision_invalid_response")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise SoulApplicationError("local_soul_vision_invalid_response")

    def as_cloud_data(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "pending_inbound_visible": self.pending_inbound_visible,
            "conversation_stage": self.conversation_stage,
            "tone": self.tone,
            "cues": list(self.cues),
            "confidence": round(float(self.confidence), 3),
        }


@dataclass(frozen=True, slots=True)
class SoulOwnerObservation:
    scope: Literal["one_due_pending_inbound", "no_due_pending_inbound"]
    expires_in_seconds: int
    scope_ref: str | None = None
    conversation_revision: str | None = None
    conversation_ref: str | None = None
    pending_generation_ref: str | None = None
    transcript_revision: str | None = None
    screenshot_png: bytes | None = field(default=None, repr=False)
    screenshot_sha256: str | None = None
    transcript: tuple[SoulTranscriptItem, ...] = field(default=(), repr=False)


class SoulVision(Protocol):
    def extract(self, screenshot_png: bytes) -> SoulVisualFacts: ...
