from __future__ import annotations


_RESTRICTED_ASCII = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-,"
)


def is_valid_text_input(value: str | None) -> bool:
    """Accept bounded printable text while excluding control/surrogate input."""

    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        return False
    if any(not character.isprintable() or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        return False
    return not value.isascii() or is_restricted_ascii_text(value)


def is_restricted_ascii_text(value: str | None) -> bool:
    """Return whether text is safe for the existing ``adb shell input`` path."""

    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        return False
    chunks = value.split(" ")
    if any(not chunk or not chunk[0].isalnum() for chunk in chunks):
        return False
    return all(character in _RESTRICTED_ASCII for chunk in chunks for character in chunk)


def requires_unicode_text_transport(value: str) -> bool:
    """Unicode transport is required exactly when the input is not pure ASCII."""

    return not value.isascii()
