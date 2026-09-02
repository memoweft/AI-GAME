"""One bounded sanitizer shared by Android UI planning and experience.

Nothing in this module knows about a device.  It intentionally favours
withholding text over attempting to preserve potentially private UI content.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping

_SECRET_LABEL = (
    r"(?:password|passcode|pin|密码|验证码|verification\s*code|otp|token|cookie|"
    r"authorization|auth|bearer|api[_ -]?key|secret)"
)
_SECRET = re.compile(
    rf"{_SECRET_LABEL}\s*[:=：]?\s*[^\s,;，；]{{1,256}}",
    re.IGNORECASE,
)
# An explicit credential assignment can contain spaces.  The former token-only
# expression above would turn ``password: correct horse battery staple`` into
# ``[redacted-secret] horse battery staple``.  Stop only at a deliberately
# narrow structural boundary; where one is absent, withholding the rest of the
# field is preferable to guessing which words are private.
_EXPLICIT_SECRET_VALUE = re.compile(
    rf"{_SECRET_LABEL}\s*[:=：]\s*.*?(?=(?:\s+(?:and|then|并且|然后)\s+{_SECRET_LABEL}\s*[:=：])|[,;，；\n]|$)",
    re.IGNORECASE,
)
_CHAT = re.compile(
    r"(?:message|聊天|对话|联系人|contact|account|用户名|昵称|display\s*name)"
    r"\s*[:=：]?\s*[^\n]{1,512}",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"(?<!\d)\d{4,}(?!\d)")
_UNSAFE_REFERENCE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s]{0,512}|(?:^|\s)/[^\s]{1,512}|(?:https?|file|content)://[^\s]{1,512}|(?:/)?(?:/|\.//)[A-Za-z*][^\s]{0,512}|(?:resource-id|xpath|selector)\s*[:=]\s*[^\s]{1,512})",
    re.IGNORECASE,
)
_SENSITIVE_KEY = re.compile(
    r"(?:password|passcode|pin|密码|验证码|verificationcode|otp|token|cookie|"
    r"authorization|auth|bearer|apikey|secret)",
    re.IGNORECASE,
)
_SAFE_REDACTION = re.compile(r"\[redacted-(?:ref:[a-f0-9]{16}|secret)\]")


@dataclass(frozen=True, slots=True)
class SanitizedText:
    text: str
    redacted: bool
    do_not_learn: bool
    digest: str


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sanitize_text(value: object, *, maximum: int = 512) -> SanitizedText:
    """Return safe free text; private content becomes a generic marker.

    The digest is deliberately over the original value so callers can correlate
    a fact without persisting it.  It must never be rendered in a user-facing
    projection together with an owner identifier.
    """
    raw = value if isinstance(value, str) else ""
    raw = raw.strip()
    if _SAFE_REDACTION.fullmatch(raw):
        return SanitizedText(raw, True, True, digest_text(raw))
    if _UNSAFE_REFERENCE.search(raw):
        return SanitizedText(f"[redacted-ref:{digest_text(raw)[:16]}]", True, True, digest_text(raw))
    sensitive = bool(_SECRET.search(raw) or _CHAT.search(raw) or _LONG_DIGITS.search(raw))
    if sensitive:
        return SanitizedText("[redacted]", True, True, digest_text(raw))
    collapsed = re.sub(r"\s+", " ", raw)
    if len(collapsed) > maximum:
        collapsed = collapsed[:maximum].rstrip() + "…"
    return SanitizedText(collapsed, False, False, digest_text(raw))


def sanitize_summary(value: object, *, maximum: int = 1_200) -> str:
    return sanitize_text(value, maximum=maximum).text


def sanitize_task_goal(value: object, *, maximum: int = 2_000) -> str:
    """Keep ordinary task semantics while redacting only explicit secrets.

    Unlike ``sanitize_text`` this is suitable for the user goal carried in the
    canonical create envelope: it does not erase normal app, contact, or task
    wording merely because it mentions a person or a message.
    """
    raw = value if isinstance(value, str) else ""
    # Redact complete explicit assignments first, then retain the compact
    # token-level guard for forms such as ``PIN 1234`` that omit punctuation.
    text = _EXPLICIT_SECRET_VALUE.sub("[redacted-secret]", raw.strip())
    text = _SECRET.sub("[redacted-secret]", text)
    # Preserve ordinary app/person/conversation language, but references which
    # could become a local path, URL, selector, or opaque credential channel
    # are never durable planner input.
    text = _UNSAFE_REFERENCE.sub(lambda match: f"[redacted-ref:{digest_text(match.group(0))[:16]}]", text)
    text = re.sub(r"\s+", " ", text)
    return text[:maximum].rstrip() + ("…" if len(text) > maximum else "")


def sanitize_task_payload(value: object, *, maximum_string: int = 2_000, maximum_items: int = 64, _depth: int = 0) -> object:
    """Bound nested create-envelope input without losing ordinary goal shape."""
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, str):
        return sanitize_task_goal(value, maximum=maximum_string)
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        sensitive_key_index = 0
        for key, item in list(value.items())[:maximum_items]:
            raw_key = str(key)
            rendered_key = raw_key[:128]
            normalized_key = re.sub(r"[\s_.-]+", "", rendered_key).casefold()
            # Key context is authoritative: values under e.g. `password`,
            # `api_key`, or `nested-auth` never survive even if they are a
            # dict/list rather than a recognizable secret-looking string.
            sensitive_key = bool(_SENSITIVE_KEY.search(normalized_key) or _SECRET.search(raw_key))
            if sensitive_key:
                # The key can itself be the credential (for example
                # ``password: correct horse battery staple``).  Never retain
                # it; ordered placeholders stay deterministic for the same
                # input shape without becoming a new persistence channel.
                sensitive_key_index += 1
                rendered_key = f"[redacted-secret-key-{sensitive_key_index}]"
            sanitized[rendered_key] = (
                "[redacted-secret]"
                if sensitive_key
                else sanitize_task_payload(item, maximum_string=maximum_string, maximum_items=maximum_items, _depth=_depth + 1)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_task_payload(item, maximum_string=maximum_string, maximum_items=maximum_items, _depth=_depth + 1) for item in value[:maximum_items]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return "[unsupported]"


def safe_action_payload(action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a bounded action shape without typed input or opaque paths."""
    result: dict[str, Any] = {"action": action}
    if action == "input_text":
        raw = arguments.get("text")
        text = raw if isinstance(raw, str) else ""
        result.update({
            "text_length": len(text),
            "text_digest": digest_text(text),
            "redacted": True,
        })
    for key in ("region", "x", "y", "end_x", "end_y", "seconds", "package"):
        value = arguments.get(key)
        if key == "package" and isinstance(value, str):
            # Package IDs are execution metadata, not UI content.
            result[key] = value[:200]
        elif key != "package" and isinstance(value, (int, float, str)):
            result[key] = value
    return result
