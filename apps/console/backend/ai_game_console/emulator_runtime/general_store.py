"""Durable no-replay claims for the generic Android UI runner.

This store is deliberately not another Task store.  Canonical Task authority,
control and revision live in ``agent-runtime.db``; this file only retains the
physical-command boundary which cannot be safely reconstructed from a model
plan after a crash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


class GenericCommandStoreError(RuntimeError):
    pass


class GenericCommandConflict(GenericCommandStoreError):
    pass


class UnresolvedEffect(GenericCommandConflict):
    """A prior effect may have happened and must be reconciled first."""


@dataclass(frozen=True, slots=True)
class GenericCommandClaim:
    claim_id: str
    dispatch_id: str
    task_id: str
    step_id: str
    subtask_id: str
    action_id: str
    command_id: str
    canonical_device_id: str
    owner_principal_id: str
    controller_id: str
    profile_id: str
    profile_generation: int
    device_boot_id: str
    command_type: str
    payload_digest: str
    state: str
    outcome: str | None
    claimed_at: str
    dispatch_started_at: str | None
    settled_at: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS general_command_claims (
    claim_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE,
    canonical_device_id TEXT NOT NULL,
    owner_principal_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_generation INTEGER NOT NULL,
    device_boot_id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'CLAIMED', 'DISPATCHING', 'OUTCOME_UNKNOWN', 'SETTLED', 'REPLAN_REQUIRED'
    )),
    outcome TEXT,
    claimed_at TEXT NOT NULL,
    dispatch_started_at TEXT,
    settled_at TEXT,
    UNIQUE(task_id, step_id),
    UNIQUE(task_id, action_id)
);
CREATE INDEX IF NOT EXISTS idx_general_claim_task
ON general_command_claims(task_id, claimed_at, claim_id);
CREATE INDEX IF NOT EXISTS idx_general_claim_device_active
ON general_command_claims(canonical_device_id, state, claimed_at, claim_id);
"""

_EFFECT_BEARING = frozenset({
    "tap", "long_press", "swipe", "input_text", "back", "home", "recents", "open_app",
})
_SUPPORTED = _EFFECT_BEARING


def command_payload_digest(command_type: str, payload: Mapping[str, Any]) -> str:
    """Hash a command without allowing typed text into any durable record."""

    command_type = _bounded(command_type, "command_type")
    sanitized = _sanitize_payload(command_type, payload)
    encoded = json.dumps(
        {"command_type": command_type, "payload": sanitized},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    # The digest is a comparison token, never a stored command payload.  HMAC
    # keeps equal-length typed text distinct without leaving either plaintext
    # or a reversible bare content hash in SQLite/events/checkpoints.
    return hmac.new(b"ai-game:generic-command-payload:v1", encoded, hashlib.sha256).hexdigest()


def is_effect_bearing(command_type: str) -> bool:
    return command_type in _EFFECT_BEARING


def require_supported_command(command_type: str) -> None:
    """Reject declared-but-unimplemented primitives before any durable claim."""

    if command_type not in _SUPPORTED:
        raise GenericCommandConflict(f"unsupported generic command type: {command_type}")


class SQLiteGenericCommandStore:
    """A small command ledger used by fake and eventual production adapters."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as connection:
            connection.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(general_command_claims)")
            }
            # K1's claim ledger is additive.  A previously-created temp or
            # developer DB must gain the crash-window timestamp before this
            # runtime can trust it for reconciliation.
            if "dispatch_started_at" not in columns:
                connection.execute(
                    "ALTER TABLE general_command_claims ADD COLUMN dispatch_started_at TEXT"
                )
                columns.add("dispatch_started_at")
            if "subtask_id" not in columns:
                connection.execute(
                    "ALTER TABLE general_command_claims "
                    "ADD COLUMN subtask_id TEXT NOT NULL DEFAULT 'legacy-unresolved'"
                )
                columns.add("subtask_id")
            required = {
                "owner_principal_id", "controller_id", "profile_id",
                "profile_generation", "device_boot_id", "dispatch_started_at",
                "subtask_id",
            }
            if missing := required - columns:
                raise GenericCommandStoreError(
                    "generic command ledger lacks required binding columns: " + ",".join(sorted(missing))
                )
        self._initialized = True

    def claim(
        self,
        *,
        dispatch_id: str,
        task_id: str,
        step_id: str,
        subtask_id: str,
        action_id: str,
        command_id: str,
        canonical_device_id: str,
        owner_principal_id: str,
        controller_id: str,
        profile_id: str,
        profile_generation: int,
        device_boot_id: str,
        command_type: str,
        payload_digest: str,
    ) -> tuple[GenericCommandClaim, bool]:
        """Persist a no-replay claim before any physical call.

        The boolean is true only for the process that created the claim.  A
        replay sees the same durable claim but never receives permission to
        dispatch it again, including the conservative crash-after-claim case.
        """

        self.initialize()
        fields = {
            "dispatch_id": dispatch_id, "task_id": task_id, "step_id": step_id,
            "subtask_id": subtask_id,
            "action_id": action_id, "command_id": command_id,
            "canonical_device_id": canonical_device_id, "owner_principal_id": owner_principal_id,
            "controller_id": controller_id, "profile_id": profile_id,
            "device_boot_id": device_boot_id, "command_type": command_type,
            "payload_digest": payload_digest,
        }
        for label, value in fields.items():
            _bounded(value, label)
        _digest(payload_digest)
        if isinstance(profile_generation, bool) or not isinstance(profile_generation, int) or profile_generation < 1:
            raise ValueError("profile_generation must be positive")
        require_supported_command(command_type)
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM general_command_claims WHERE command_id=?", (command_id,)
            ).fetchone()
            if existing is not None:
                claim = _claim(existing)
                if _claim_identity(claim) != (
                    dispatch_id, task_id, step_id, subtask_id, action_id, canonical_device_id,
                    owner_principal_id, controller_id, profile_id, profile_generation,
                    device_boot_id, command_type, payload_digest,
                ):
                    raise GenericCommandConflict("command identity was reused with a different payload")
                return claim, False
            if is_effect_bearing(command_type):
                unresolved = connection.execute(
                    "SELECT command_id FROM general_command_claims WHERE task_id=? "
                    "AND command_type IN ('tap','long_press','swipe','input_text','back','home','recents','open_app') "
                    "AND state IN ('CLAIMED','DISPATCHING','OUTCOME_UNKNOWN')",
                    (task_id,),
                ).fetchone()
                if unresolved is not None:
                    raise UnresolvedEffect("Task has an unresolved physical effect")
            writer = connection.execute(
                "SELECT command_id FROM general_command_claims WHERE canonical_device_id=? "
                "AND state IN ('CLAIMED','DISPATCHING','OUTCOME_UNKNOWN')",
                (canonical_device_id,),
            ).fetchone()
            if writer is not None:
                raise GenericCommandConflict("another command currently owns this device writer lane")
            now = _utc_now()
            claim_id = str(uuid4())
            connection.execute(
                "INSERT INTO general_command_claims("
                "claim_id,dispatch_id,task_id,step_id,subtask_id,action_id,command_id,canonical_device_id,"
                "owner_principal_id,controller_id,profile_id,profile_generation,device_boot_id,"
                "command_type,payload_digest,state,outcome,claimed_at,dispatch_started_at,settled_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'CLAIMED',NULL,?,NULL,NULL)",
                (claim_id, dispatch_id, task_id, step_id, subtask_id, action_id, command_id,
                 canonical_device_id, owner_principal_id, controller_id, profile_id,
                 profile_generation, device_boot_id, command_type, payload_digest, now),
            )
            row = connection.execute(
                "SELECT * FROM general_command_claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            return _claim(row), True

    def begin_dispatch(self, command_id: str) -> GenericCommandClaim:
        """Cross the no-replay boundary immediately before the physical call."""

        self.initialize()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM general_command_claims WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                raise GenericCommandConflict("command claim was not found")
            claim = _claim(row)
            if claim.state != "CLAIMED":
                raise GenericCommandConflict("claimed command cannot be physically dispatched again")
            connection.execute(
                "UPDATE general_command_claims SET state='DISPATCHING',dispatch_started_at=? "
                "WHERE command_id=? AND state='CLAIMED'",
                (_utc_now(), command_id,),
            )
            row = connection.execute(
                "SELECT * FROM general_command_claims WHERE command_id=?", (command_id,)
            ).fetchone()
            return _claim(row)

    def mark_outcome_unknown(self, command_id: str) -> GenericCommandClaim:
        """Record an uncertain physical result; it is never a success proof."""

        return self._settle(command_id, state="OUTCOME_UNKNOWN", outcome="unknown")

    def recover_unfinished(self, command_id: str) -> GenericCommandClaim:
        """Conservatively classify a claim recovered without a durable receipt.

        ``CLAIMED`` means a crash may have occurred immediately before the
        durable dispatch-start transition; ``DISPATCHING`` means it may have
        occurred on either side of the physical call.  Neither state carries a
        trustworthy receipt, so process recovery must never infer a replayable
        non-effect from it.
        """

        claim = self.get(command_id)
        if claim is None:
            raise GenericCommandConflict("command claim was not found")
        if claim.state in {"CLAIMED", "DISPATCHING"}:
            return self.mark_outcome_unknown(command_id)
        return claim

    def settle(self, command_id: str, *, outcome: str) -> GenericCommandClaim:
        if outcome not in {"accepted", "rejected", "reconciled_happened"}:
            raise ValueError("unsupported settled command outcome")
        return self._settle(command_id, state="SETTLED", outcome=outcome)

    def require_replan_after_non_effect(self, command_id: str) -> GenericCommandClaim:
        """Only a reliable non-effect observation can clear the no-replay fence.

        The explicit state gives K2 a durable replan boundary; it does not
        auto-create or auto-dispatch a semantic replacement action.
        """

        return self._settle(command_id, state="REPLAN_REQUIRED", outcome="not_observed")

    def get(self, command_id: str) -> GenericCommandClaim | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM general_command_claims WHERE command_id=?", (command_id,)
            ).fetchone()
            return _claim(row) if row is not None else None

    def _settle(self, command_id: str, *, state: str, outcome: str) -> GenericCommandClaim:
        self.initialize()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM general_command_claims WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                raise GenericCommandConflict("command claim was not found")
            current = _claim(row)
            if current.state == "SETTLED":
                if current.outcome != outcome:
                    raise GenericCommandConflict("settled command cannot change outcome")
                return current
            if current.state == "REPLAN_REQUIRED":
                if state != "REPLAN_REQUIRED" or current.outcome != outcome:
                    raise GenericCommandConflict("replan boundary cannot change outcome")
                return current
            now = _utc_now()
            connection.execute(
                "UPDATE general_command_claims SET state=?, outcome=?, settled_at=? WHERE command_id=?",
                (state, outcome, now, command_id),
            )
            row = connection.execute(
                "SELECT * FROM general_command_claims WHERE command_id=?", (command_id,)
            ).fetchone()
            return _claim(row)

    def _connection(self, *, write: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute("BEGIN IMMEDIATE")
        return _ConnectionContext(connection)


class _ConnectionContext:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()


def _claim(row: sqlite3.Row) -> GenericCommandClaim:
    return GenericCommandClaim(
        claim_id=str(row["claim_id"]), dispatch_id=str(row["dispatch_id"]),
        task_id=str(row["task_id"]), step_id=str(row["step_id"]),
        subtask_id=str(row["subtask_id"]),
        action_id=str(row["action_id"]), command_id=str(row["command_id"]),
        canonical_device_id=str(row["canonical_device_id"]),
        owner_principal_id=str(row["owner_principal_id"]),
        controller_id=str(row["controller_id"]), profile_id=str(row["profile_id"]),
        profile_generation=int(row["profile_generation"]), device_boot_id=str(row["device_boot_id"]),
        command_type=str(row["command_type"]), payload_digest=str(row["payload_digest"]),
        state=str(row["state"]), outcome=(str(row["outcome"]) if row["outcome"] is not None else None),
        claimed_at=str(row["claimed_at"]),
        dispatch_started_at=(str(row["dispatch_started_at"]) if row["dispatch_started_at"] is not None else None),
        settled_at=(str(row["settled_at"]) if row["settled_at"] is not None else None),
    )


def _claim_identity(claim: GenericCommandClaim) -> tuple[str, str, str, str, str, str, str, str, str, int, str, str, str]:
    return (
        claim.dispatch_id, claim.task_id, claim.step_id, claim.subtask_id, claim.action_id,
        claim.canonical_device_id, claim.owner_principal_id, claim.controller_id,
        claim.profile_id, claim.profile_generation, claim.device_boot_id,
        claim.command_type, claim.payload_digest,
    )


def _sanitize_payload(command_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("command payload must be a mapping")
    if command_type == "input_text":
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("input_text requires text")
        # This remains secret-free but has a keyed opaque tag so two values of
        # the same length cannot share a physical command identity.
        tag = hmac.new(b"ai-game:generic-typed-text:v1", text.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"text": {"redacted": True, "length": len(text), "opaque_tag": tag}}
    # Generic payloads are bounded scalar geometry/package facts.  Rejecting
    # nested/free text values keeps claim data unable to become an accidental
    # UI-text or credential store.
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or len(key) > 64:
            raise ValueError("command payload key is invalid")
        if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
            sanitized[key] = value
        elif isinstance(value, str) and len(value) <= 256:
            sanitized[key] = value
        else:
            raise ValueError("command payload contains unsupported sensitive structure")
    return sanitized


def _bounded(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("payload_digest must be lowercase SHA-256")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
