from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .domain import (
    EmulatorFingerprint,
    EmulatorProfile,
    EmulatorProfileState,
    EmulatorSettingsRun,
    SettingsRunStatus,
)


class EmulatorProfileStore:
    """Small SQLite store for profiles and durable operator command projections."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_profile(self, profile: EmulatorProfile) -> EmulatorProfile:
        with self._lock, self._connection:
            existing_owner = self._connection.execute(
                "SELECT owner_principal_id, owner_controller_id FROM emulator_profiles WHERE profile_id = ?",
                (profile.profile_id,),
            ).fetchone()
            if existing_owner is not None and (
                existing_owner["owner_principal_id"] != profile.owner_principal_id
                or existing_owner["owner_controller_id"] != profile.owner_controller_id
            ):
                raise EmulatorProfileStoreOwnershipError("emulator_profile_not_found")
            if profile.is_default:
                self._connection.execute(
                    """
                    UPDATE emulator_profiles SET is_default = 0
                    WHERE owner_principal_id = ? AND owner_controller_id = ?
                    """,
                    (profile.owner_principal_id, profile.owner_controller_id),
                )
            cursor = self._connection.execute(
                """
                INSERT INTO emulator_profiles (
                    profile_id, owner_principal_id, owner_controller_id, display_name, enabled, is_default, transport_serial,
                    profile_generation, state, boot_id, fingerprint_json,
                    capabilities_json, last_error_code, last_verified_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    display_name=excluded.display_name, enabled=excluded.enabled,
                    is_default=excluded.is_default, transport_serial=excluded.transport_serial,
                    profile_generation=excluded.profile_generation, state=excluded.state,
                    boot_id=excluded.boot_id, fingerprint_json=excluded.fingerprint_json,
                    capabilities_json=excluded.capabilities_json,
                    last_error_code=excluded.last_error_code,
                    last_verified_at=excluded.last_verified_at, updated_at=excluded.updated_at
                WHERE emulator_profiles.owner_principal_id = excluded.owner_principal_id
                  AND emulator_profiles.owner_controller_id = excluded.owner_controller_id
                """,
                _profile_values(profile),
            )
            if cursor.rowcount != 1:
                raise EmulatorProfileStoreOwnershipError("emulator_profile_not_found")
        return profile

    def get_profile(
        self, *, owner_principal_id: str, owner_controller_id: str, profile_id: str
    ) -> EmulatorProfile | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM emulator_profiles
                WHERE profile_id = ? AND owner_principal_id = ? AND owner_controller_id = ?
                """,
                (profile_id, owner_principal_id, owner_controller_id),
            ).fetchone()
        return _profile_from_row(row) if row else None

    def get_profile_internal(self, *, profile_id: str) -> EmulatorProfile | None:
        """Resolve an internal canonical id without exposing another owner's row."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM emulator_profiles WHERE profile_id = ?", (profile_id,)
            ).fetchone()
        return _profile_from_row(row) if row else None

    def profile_id_claimed(self, *, profile_id: str) -> bool:
        """Internal collision guard; it never returns another profile's data."""

        with self._lock:
            return (
                self._connection.execute(
                "SELECT 1 FROM emulator_profiles WHERE profile_id = ?", (profile_id,)
                ).fetchone()
                is not None
            )

    def list_profiles(
        self, *, owner_principal_id: str, owner_controller_id: str
    ) -> tuple[EmulatorProfile, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM emulator_profiles
                WHERE owner_principal_id = ? AND owner_controller_id = ?
                ORDER BY is_default DESC, display_name, profile_id
                """,
                (owner_principal_id, owner_controller_id),
            ).fetchall()
        return tuple(_profile_from_row(row) for row in rows)

    def get_default_profile(
        self, *, owner_principal_id: str, owner_controller_id: str
    ) -> EmulatorProfile | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM emulator_profiles
                WHERE owner_principal_id = ? AND owner_controller_id = ? AND is_default = 1
                """,
                (owner_principal_id, owner_controller_id),
            ).fetchone()
        return _profile_from_row(row) if row else None

    def get_settings_run(
        self, *, owner_principal_id: str, owner_controller_id: str, task_id: str, action_id: str
    ) -> EmulatorSettingsRun | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM emulator_settings_runs
                WHERE owner_principal_id = ? AND owner_controller_id = ? AND task_id = ? AND action_id = ?
                """,
                (owner_principal_id, owner_controller_id, task_id, action_id),
            ).fetchone()
        return _run_from_row(row) if row else None

    def get_settings_run_by_idempotency(
        self,
        *,
        owner_principal_id: str,
        owner_controller_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> EmulatorSettingsRun | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM emulator_settings_runs
                WHERE owner_principal_id = ? AND owner_controller_id = ?
                  AND task_id = ? AND idempotency_key = ?
                """,
                (owner_principal_id, owner_controller_id, task_id, idempotency_key),
            ).fetchone()
        return _run_from_row(row) if row else None

    def latest_succeeded_settings_run(
        self, *, owner_principal_id: str, owner_controller_id: str, task_id: str
    ) -> EmulatorSettingsRun | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM emulator_settings_runs
                WHERE owner_principal_id = ? AND owner_controller_id = ?
                  AND task_id = ? AND status = ?
                ORDER BY rowid DESC LIMIT 1
                """,
                (
                    owner_principal_id,
                    owner_controller_id,
                    task_id,
                    SettingsRunStatus.SUCCEEDED.value,
                ),
            ).fetchone()
        return _run_from_row(row) if row else None

    def succeeded_settings_run_for_observation(
        self, *, observation_id: str
    ) -> EmulatorSettingsRun | None:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM emulator_settings_runs
                WHERE after_observation_id = ? AND status = ?
                ORDER BY rowid DESC LIMIT 2
                """,
                (observation_id, SettingsRunStatus.SUCCEEDED.value),
            ).fetchall()
        if len(rows) != 1:
            return None
        return _run_from_row(rows[0])

    def save_settings_run(self, run: EmulatorSettingsRun) -> EmulatorSettingsRun:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO emulator_settings_runs (
                    task_id, action_id, profile_id, owner_principal_id, owner_controller_id, profile_generation, boot_id, status,
                    before_observation_id, after_observation_id, command_id, frame_ref,
                    evidence_refs_json, reason_code, canonical_device_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, action_id) DO UPDATE SET
                    profile_id=excluded.profile_id, owner_principal_id=excluded.owner_principal_id,
                    owner_controller_id=excluded.owner_controller_id,
                    profile_generation=excluded.profile_generation,
                    boot_id=excluded.boot_id, status=excluded.status,
                    before_observation_id=excluded.before_observation_id,
                    after_observation_id=excluded.after_observation_id,
                    command_id=excluded.command_id, frame_ref=excluded.frame_ref,
                    evidence_refs_json=excluded.evidence_refs_json,
                    reason_code=excluded.reason_code,
                    canonical_device_id=excluded.canonical_device_id,
                    idempotency_key=excluded.idempotency_key
                WHERE emulator_settings_runs.owner_principal_id = excluded.owner_principal_id
                  AND emulator_settings_runs.owner_controller_id = excluded.owner_controller_id
                """,
                (
                    run.task_id, run.action_id, run.profile_id, run.owner_principal_id,
                    run.owner_controller_id, run.profile_generation,
                    run.boot_id, run.status.value, run.before_observation_id,
                    run.after_observation_id, run.command_id, run.frame_ref,
                    json.dumps(run.evidence_refs), run.reason_code,
                    run.canonical_device_id, run.idempotency_key,
                ),
            )
            if cursor.rowcount != 1:
                raise EmulatorProfileStoreOwnershipError("emulator_settings_run_not_found")
        return run

    def claim_settings_command(
        self,
        *,
        owner_principal_id: str,
        owner_controller_id: str,
        task_id: str,
        action_id: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Persist the no-replay fence before crossing the ADB side-effect boundary."""

        with self._lock, self._connection:
            existing = self._connection.execute(
                """
                SELECT * FROM emulator_settings_command_claims
                WHERE task_id = ? AND action_id = ?
                """,
                (task_id, action_id),
            ).fetchone()
            if existing is not None:
                return dict(existing), False
            self._connection.execute(
                """
                INSERT INTO emulator_settings_command_claims (
                    task_id, action_id, owner_principal_id, owner_controller_id,
                    idempotency_key, command_id, status, accepted, reason_code
                ) VALUES (?, ?, ?, ?, ?, ?, 'claimed', NULL, NULL)
                """,
                (
                    task_id,
                    action_id,
                    owner_principal_id,
                    owner_controller_id,
                    idempotency_key,
                    action_id,
                ),
            )
            created = self._connection.execute(
                "SELECT * FROM emulator_settings_command_claims WHERE task_id = ? AND action_id = ?",
                (task_id, action_id),
            ).fetchone()
        return dict(created), True

    def settings_command_claim(self, *, task_id: str, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM emulator_settings_command_claims WHERE task_id = ? AND action_id = ?",
                (task_id, action_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def settle_settings_command(
        self,
        *,
        task_id: str,
        action_id: str,
        accepted: bool,
        reason_code: str | None,
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE emulator_settings_command_claims
                SET status = 'settled', accepted = ?, reason_code = ?
                WHERE task_id = ? AND action_id = ?
                """,
                (int(accepted), reason_code, task_id, action_id),
            )
            if cursor.rowcount != 1:
                raise EmulatorProfileStoreOwnershipError("emulator_settings_command_not_found")
            row = self._connection.execute(
                "SELECT * FROM emulator_settings_command_claims WHERE task_id = ? AND action_id = ?",
                (task_id, action_id),
            ).fetchone()
        return dict(row)

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS emulator_profiles (
                    profile_id TEXT PRIMARY KEY,
                    owner_principal_id TEXT NOT NULL,
                    owner_controller_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    is_default INTEGER NOT NULL,
                    transport_serial TEXT NOT NULL,
                    profile_generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    boot_id TEXT,
                    fingerprint_json TEXT,
                    capabilities_json TEXT NOT NULL,
                    last_error_code TEXT,
                    last_verified_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS emulator_settings_runs (
                    task_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL REFERENCES emulator_profiles(profile_id),
                    owner_principal_id TEXT NOT NULL,
                    owner_controller_id TEXT NOT NULL,
                    profile_generation INTEGER NOT NULL,
                    boot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    before_observation_id TEXT,
                    after_observation_id TEXT,
                    command_id TEXT,
                    frame_ref TEXT,
                    evidence_refs_json TEXT NOT NULL,
                    reason_code TEXT,
                    canonical_device_id TEXT,
                    idempotency_key TEXT,
                    PRIMARY KEY(task_id, action_id)
                );
                CREATE TABLE IF NOT EXISTS emulator_settings_command_claims (
                    task_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    owner_principal_id TEXT NOT NULL,
                    owner_controller_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    accepted INTEGER,
                    reason_code TEXT,
                    PRIMARY KEY(task_id, action_id)
                );
                """
            )
            self._migrate_owner_controller_columns()
            self._migrate_settings_binding_columns()
            self._connection.execute("DROP INDEX IF EXISTS one_default_emulator_profile")
            self._connection.execute(
                "DROP INDEX IF EXISTS one_default_emulator_profile_per_principal"
            )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_default_emulator_profile_per_owner_pair
                ON emulator_profiles(owner_principal_id, owner_controller_id)
                WHERE is_default = 1
                """
            )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_settings_idempotency_per_owner_task
                ON emulator_settings_runs(
                    owner_principal_id, owner_controller_id, task_id, idempotency_key
                ) WHERE idempotency_key IS NOT NULL
                """
            )

    def _migrate_owner_controller_columns(self) -> None:
        """Safely quarantine pre-owner-pair rows behind an unreachable controller."""

        for table in ("emulator_profiles", "emulator_settings_runs"):
            columns = {
                row["name"]
                for row in self._connection.execute(f"PRAGMA table_info({table})")
            }
            if "owner_controller_id" not in columns:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner_controller_id TEXT NOT NULL DEFAULT 'legacy:unbound'"
                )

    def _migrate_settings_binding_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(emulator_settings_runs)")
        }
        if "canonical_device_id" not in columns:
            self._connection.execute(
                "ALTER TABLE emulator_settings_runs ADD COLUMN canonical_device_id TEXT"
            )
        if "idempotency_key" not in columns:
            self._connection.execute(
                "ALTER TABLE emulator_settings_runs ADD COLUMN idempotency_key TEXT"
            )
        self._connection.execute(
            """
            UPDATE emulator_settings_runs
            SET canonical_device_id = 'emulator:' || profile_id
            WHERE canonical_device_id IS NULL
            """
        )
        self._connection.execute(
            """
            UPDATE emulator_settings_runs SET idempotency_key = action_id
            WHERE idempotency_key IS NULL
            """
        )


class EmulatorProfileStoreOwnershipError(ValueError):
    """Do not reveal whether another principal owns an otherwise valid ID."""


def _profile_values(profile: EmulatorProfile) -> tuple[object, ...]:
    return (
        profile.profile_id, profile.owner_principal_id, profile.owner_controller_id,
        profile.display_name,
        int(profile.enabled), int(profile.is_default),
        profile.transport_serial, profile.profile_generation, profile.state.value,
        profile.boot_id,
        json.dumps(profile.fingerprint.as_dict()) if profile.fingerprint else None,
        json.dumps(profile.capabilities), profile.last_error_code, profile.last_verified_at,
        profile.created_at, profile.updated_at,
    )


def _profile_from_row(row: sqlite3.Row) -> EmulatorProfile:
    fingerprint_data = json.loads(row["fingerprint_json"]) if row["fingerprint_json"] else None
    return EmulatorProfile(
        profile_id=row["profile_id"], display_name=row["display_name"],
        owner_principal_id=row["owner_principal_id"],
        owner_controller_id=row["owner_controller_id"],
        enabled=bool(row["enabled"]), is_default=bool(row["is_default"]),
        transport_serial=row["transport_serial"],
        canonical_device_id=f"emulator:{row['profile_id']}",
        profile_generation=row["profile_generation"], state=EmulatorProfileState(row["state"]),
        boot_id=row["boot_id"],
        fingerprint=EmulatorFingerprint.from_dict(fingerprint_data) if fingerprint_data else None,
        capabilities=tuple(json.loads(row["capabilities_json"])),
        last_error_code=row["last_error_code"], last_verified_at=row["last_verified_at"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _run_from_row(row: sqlite3.Row) -> EmulatorSettingsRun:
    return EmulatorSettingsRun(
        task_id=row["task_id"], action_id=row["action_id"], profile_id=row["profile_id"],
        owner_principal_id=row["owner_principal_id"],
        owner_controller_id=row["owner_controller_id"],
        profile_generation=row["profile_generation"], boot_id=row["boot_id"],
        status=SettingsRunStatus(row["status"]), before_observation_id=row["before_observation_id"],
        after_observation_id=row["after_observation_id"], command_id=row["command_id"],
        frame_ref=row["frame_ref"], evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
        reason_code=row["reason_code"],
        canonical_device_id=row["canonical_device_id"],
        idempotency_key=row["idempotency_key"],
    )
