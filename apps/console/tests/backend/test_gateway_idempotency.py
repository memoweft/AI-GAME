"""Phase 6 Week 2: Gateway idempotency + store + frozen error table.

Covers the contract guarantees behind the ``Idempotency-Key`` header (§3):
- canonical payload hashing is deterministic across key order and unicode;
- the SQLite gateway store is initialized exactly once and rejects foreign
  or newer schemas;
- same (scope, key, payload) replays the stored response verbatim and runs
  the operation exactly once; same key + different payload is a
  ``IDEMPOTENCY_CONFLICT``; failed operations are not recorded;
- the frozen §14 error table: every code, its retryable flag, and the
  ``to_dict`` shape the HTTP layer renders.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_game_console.gateway import (
    GatewayStore,
    GatewayStoreConflict,
    IdempotencyConflict,
    IdempotencyRecord,
    IdempotencyService,
    ValidationError,
    canonical_payload_hash,
)
from ai_game_console.gateway.errors import (
    ConversationConflict,
    DeviceNotAvailable,
    DeviceNotFound,
    EventCursorInvalid,
    GatewayError,
    IdempotencyConflict as IdempotencyConflictError,
    InternalError,
    LegacyTaskWriteDisabled,
    TaskNotActive,
    TaskNotFound,
    ValidationError as ValidationErrorError,
)

SCOPE = "task.create"
KEY = "key-1"


def _store(tmp_path: Path) -> GatewayStore:
    store = GatewayStore(tmp_path / "gateway.db")
    store.initialize()
    return store


class TestCanonicalPayloadHash:
    def test_stable_across_key_order(self) -> None:
        assert canonical_payload_hash({"a": 1, "b": "x"}) == canonical_payload_hash(
            {"b": "x", "a": 1}
        )

    def test_sensitive_to_value_change(self) -> None:
        assert canonical_payload_hash({"a": 1}) != canonical_payload_hash({"a": 2})

    def test_unicode_payload_stable_and_distinct(self) -> None:
        a = canonical_payload_hash({"goal": "打开游戏"})
        b = canonical_payload_hash({"goal": "打开游戏"})
        c = canonical_payload_hash({"goal": "open the game"})
        assert a == b
        assert a != c


class TestGatewayStore:
    def test_roundtrip_and_scope_isolation(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.put_idempotency(
            scope=SCOPE,
            key=KEY,
            payload_hash="h1",
            response_json='{"ok": 1}',
        )
        store.put_idempotency(
            scope="task.message",
            key=KEY,
            payload_hash="h2",
            response_json='{"ok": 2}',
        )

        first = store.get_idempotency(SCOPE, KEY)
        second = store.get_idempotency("task.message", KEY)
        assert isinstance(first, IdempotencyRecord)
        assert first.scope == SCOPE
        assert first.key == KEY
        assert first.payload_hash == "h1"
        assert first.response_json == '{"ok": 1}'
        assert second is not None and second.payload_hash == "h2"

    def test_missing_record_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.get_idempotency(SCOPE, "other") is None

    def test_initialize_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.close()
        store.initialize()
        assert store.get_idempotency(SCOPE, KEY) is None

    def test_newer_schema_revision_conflicts(self, tmp_path: Path) -> None:
        path = tmp_path / "gateway.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE gateway_schema (revision TEXT PRIMARY KEY, "
                "applied_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO gateway_schema (revision, applied_at) "
                "VALUES ('2', '2026-08-18T00:00:00+00:00')"
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(GatewayStoreConflict):
            GatewayStore(path).initialize()

    def test_foreign_database_without_schema_conflicts(self, tmp_path: Path) -> None:
        path = tmp_path / "gateway.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(GatewayStoreConflict):
            GatewayStore(path).initialize()

    def test_duplicate_put_raises_integrity(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.put_idempotency(
            scope=SCOPE, key=KEY, payload_hash="h1", response_json="{}"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.put_idempotency(
                scope=SCOPE, key=KEY, payload_hash="h1", response_json="{}"
            )


class TestIdempotencyService:
    def test_operation_runs_exactly_once(self, tmp_path: Path) -> None:
        service = IdempotencyService(_store(tmp_path))
        calls: list[str] = []

        def operation() -> dict:
            calls.append(KEY)
            return {"task": {"id": "task-1"}}

        first = service.execute_once(
            scope=SCOPE, key=KEY, payload={"goal": "g"}, operation=operation
        )
        second = service.execute_once(
            scope=SCOPE, key=KEY, payload={"goal": "g"}, operation=operation
        )

        assert first == second == {"task": {"id": "task-1"}}
        assert calls == [KEY]

    def test_replay_is_verbatim_stored_response(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = IdempotencyService(store)

        def operation() -> dict:
            return {"b": 2, "a": 1}

        first = service.execute_once(
            scope=SCOPE, key=KEY, payload={"p": 1}, operation=operation
        )
        # Corrupt the in-memory expectation: the replay must come from the
        # stored JSON, not from re-running anything.
        service2 = IdempotencyService(store)
        second = service2.execute_once(
            scope=SCOPE, key=KEY, payload={"p": 1}, operation=operation
        )

        assert first == {"b": 2, "a": 1}
        assert second == {"b": 2, "a": 1}

    def test_same_key_different_payload_conflicts(self, tmp_path: Path) -> None:
        service = IdempotencyService(_store(tmp_path))
        service.execute_once(
            scope=SCOPE, key=KEY, payload={"goal": "a"}, operation=lambda: {}
        )
        with pytest.raises(IdempotencyConflict):
            service.execute_once(
                scope=SCOPE, key=KEY, payload={"goal": "b"}, operation=lambda: {}
            )

    def test_blank_key_rejected(self, tmp_path: Path) -> None:
        service = IdempotencyService(_store(tmp_path))
        with pytest.raises(ValidationError):
            service.execute_once(
                scope=SCOPE, key="  ", payload={}, operation=lambda: {}
            )

    def test_failed_operation_is_not_recorded(self, tmp_path: Path) -> None:
        service = IdempotencyService(_store(tmp_path))

        def failing() -> dict:
            raise ValidationError("nope")

        with pytest.raises(ValidationError):
            service.execute_once(
                scope=SCOPE, key=KEY, payload={"goal": "g"}, operation=failing
            )

        # A retry with the same key may run again because nothing was stored.
        result = service.execute_once(
            scope=SCOPE, key=KEY, payload={"goal": "g"}, operation=lambda: {"ok": True}
        )
        assert result == {"ok": True}


class TestFrozenErrorTable:
    """§14: exactly these codes exist, with these retryable flags."""

    @pytest.mark.parametrize(
        ("error", "code", "retryable"),
        [
            (ValidationErrorError, "VALIDATION_ERROR", False),
            (TaskNotFound, "TASK_NOT_FOUND", False),
            (TaskNotActive, "TASK_NOT_ACTIVE", False),
            (DeviceNotFound, "DEVICE_NOT_FOUND", False),
            (DeviceNotAvailable, "DEVICE_NOT_AVAILABLE", True),
            (ConversationConflict, "CONVERSATION_CONFLICT", False),
            (IdempotencyConflictError, "IDEMPOTENCY_CONFLICT", False),
            (EventCursorInvalid, "EVENT_CURSOR_INVALID", False),
            (LegacyTaskWriteDisabled, "LEGACY_TASK_WRITE_DISABLED", False),
            (InternalError, "INTERNAL_ERROR", False),
        ],
    )
    def test_code_and_retryable(
        self, error: type[GatewayError], code: str, retryable: bool
    ) -> None:
        rendered = error("boom", details={"k": "v"}).to_dict()
        assert rendered == {
            "code": code,
            "message": "boom",
            "retryable": retryable,
            "details": {"k": "v"},
        }

    def test_details_default_to_none(self) -> None:
        rendered = TaskNotFound("missing").to_dict()
        assert rendered["details"] is None
        assert rendered["retryable"] is False
