"""Idempotency-Key service (frozen contract §3 / §5 / §7 / §9).

Every create-Task, send-message, and control operation requires an
``Idempotency-Key``. Semantics:

* same (scope, key) + same payload  -> replay the stored response verbatim;
* same (scope, key) + different payload -> ``IDEMPOTENCY_CONFLICT``.

The stored response is the canonical JSON the first call produced, so a
replay is byte-for-byte identical to the original response.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from .errors import IdempotencyConflict, ValidationError
from .store import GatewayStore

# Operation scopes: the key namespace is per-operation, so the same key
# value may be reused across create / message / control safely.
SCOPE_TASK_CREATE = "task.create"
SCOPE_TASK_MESSAGE = "task.message"
SCOPE_TASK_CONTROL = "task.control"
SCOPE_CONVERSATION_MESSAGE = "conversation.message"


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """Stable SHA-256 over the canonical JSON of the request payload."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyService:
    """Executes an operation exactly once per (scope, key, payload)."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def execute_once(
        self,
        *,
        scope: str,
        key: str,
        payload: Mapping[str, Any],
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("Idempotency-Key must not be blank")
        payload_hash = canonical_payload_hash(payload)
        existing = self._store.get_idempotency(scope, key)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise IdempotencyConflict(
                    f"Idempotency-Key {key!r} was already used with a different payload"
                )
            return json.loads(existing.response_json)
        response = operation()
        response_json = json.dumps(response, sort_keys=True, ensure_ascii=False)
        try:
            self._store.put_idempotency(
                scope=scope,
                key=key,
                payload_hash=payload_hash,
                response_json=response_json,
            )
        except sqlite3.IntegrityError:
            # A concurrent writer inserted the same (scope, key) first.
            existing = self._store.get_idempotency(scope, key)
            if existing is not None and existing.payload_hash == payload_hash:
                return json.loads(existing.response_json)
            raise IdempotencyConflict(
                f"Idempotency-Key {key!r} was already used with a different payload"
            ) from None
        return response
