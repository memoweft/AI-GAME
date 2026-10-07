from __future__ import annotations

import sqlite3
from dataclasses import replace

from ai_game_console.discovery import AdbDevice
from ai_game_console.domain import TargetStatus
from ai_game_console.emulator_runtime.domain import (
    EmulatorFingerprint,
    EmulatorProbe,
    EmulatorProfileState,
)
from ai_game_console.emulator_runtime.service import (
    EmulatorProfileService,
    EmulatorProfileServiceError,
)
from ai_game_console.emulator_runtime.store import EmulatorProfileStore


NOW = "2026-08-30T00:00:00+00:00"
FINGERPRINT = EmulatorFingerprint(True, 35, "x86_64", "1080x2400", 440)
OWNER_A = {"principal_id": "principal-a", "controller_id": "controller-a"}
OWNER_A_OTHER_CONTROLLER = {
    "principal_id": "principal-a",
    "controller_id": "controller-b",
}
OWNER_B = {"principal_id": "principal-b", "controller_id": "controller-a"}


def _device(serial: str = "emulator-5554", **properties: str) -> AdbDevice:
    return AdbDevice(serial, "device", TargetStatus.READY, properties)


class _Probes:
    def __init__(self) -> None:
        self.value = EmulatorProbe("emulator-5554", EmulatorProfileState.READY, "boot-a", FINGERPRINT, ("screen", "ui_tree", "open_app"))
        self.calls = 0

    def __call__(self, _serial: str) -> EmulatorProbe:
        self.calls += 1
        return self.value


def _service(probes: _Probes) -> tuple[EmulatorProfileStore, EmulatorProfileService]:
    store = EmulatorProfileStore()
    return store, EmulatorProfileService(store=store, probe=probes, clock=lambda: NOW)


def test_profile_filters_non_emulators_and_projects_no_transport_locator() -> None:
    probes = _Probes()
    store, service = _service(probes)
    physical = _device("R58M1234", model="Pixel_9")
    emulator = _device("emulator-5554", model="sdk_gphone64_x86_64")

    assert service.discover_candidates(**OWNER_A, devices=[physical, emulator]) == (emulator,)
    try:
        service.save_selected(**OWNER_A, candidate=physical, display_name="physical")
    except EmulatorProfileServiceError as error:
        assert error.code == "emulator_target_required"
    else:  # pragma: no cover - test guard
        raise AssertionError("physical target was accepted")

    profile = service.save_selected(**OWNER_A, candidate=emulator, display_name="Pixel API 35", is_default=True)
    projection = profile.public_projection()

    assert projection["is_default"] is True
    assert "serial" not in repr(projection).casefold()
    assert all(forbidden not in repr(projection).casefold() for forbidden in ("token", "adb_path", "path", "principal", "canonical"))
    store.close()


def test_profile_serial_rotation_increments_generation_but_boot_or_fingerprint_drift_does_not_migrate() -> None:
    probes = _Probes()
    store, service = _service(probes)
    profile = service.save_selected(**OWNER_A, candidate=_device(), display_name="API 35")

    probes.value = EmulatorProbe("emulator-5556", EmulatorProfileState.READY, "boot-a", FINGERPRINT, ("screen",))
    rotated = service.verify(**OWNER_A, profile_id=profile.profile_id)
    assert rotated.state is EmulatorProfileState.READY
    assert rotated.transport_serial == "emulator-5556"
    assert rotated.profile_generation == 2

    probes.value = EmulatorProbe("emulator-5556", EmulatorProfileState.READY, "boot-b", FINGERPRINT, ("screen",))
    drifted = service.verify(**OWNER_A, profile_id=profile.profile_id)
    assert drifted.state is EmulatorProfileState.DRIFTED
    assert drifted.last_error_code == "emulator_identity_drift"
    try:
        service.resolve_ready(**OWNER_A, profile_id=profile.profile_id)
    except EmulatorProfileServiceError as error:
        assert error.code == "emulator_identity_drift"
    else:  # pragma: no cover - test guard
        raise AssertionError("drifted profile was resolved")
    store.close()


def test_repeated_discovery_and_save_reuses_one_profile_identity() -> None:
    probes = _Probes()
    store, service = _service(probes)
    candidate = _device(model="sdk_gphone64_x86_64")

    first = service.save_selected(
        **OWNER_A, candidate=candidate, display_name="first", is_default=True
    )
    assert service.discover_candidates(**OWNER_A, devices=[candidate]) == ()
    repeated = service.save_selected(
        **OWNER_A, candidate=candidate, display_name="duplicate", is_default=True
    )

    assert repeated.profile_id == first.profile_id
    assert repeated.display_name == "first"
    assert len(store.list_profiles(
        owner_principal_id=OWNER_A["principal_id"],
        owner_controller_id=OWNER_A["controller_id"],
    )) == 1
    store.close()


def test_same_transport_and_fingerprint_reuses_profile_after_boot_change() -> None:
    probes = _Probes()
    store, service = _service(probes)
    candidate = _device(model="sdk_gphone64_x86_64")
    original = service.save_selected(
        **OWNER_A, candidate=candidate, display_name="original", is_default=True
    )
    probes.value = EmulatorProbe(
        original.transport_serial,
        EmulatorProfileState.READY,
        "boot-replaced",
        FINGERPRINT,
        ("screen",),
    )

    assert service.discover_candidates(**OWNER_A, devices=[candidate]) == ()
    restarted = service.save_selected(
        **OWNER_A, candidate=candidate, display_name="replacement", is_default=True
    )
    assert restarted.profile_id == original.profile_id
    assert restarted.boot_id == "boot-replaced"
    store.close()


def test_duplicate_legacy_profiles_coalesce_without_breaking_old_ids() -> None:
    probes = _Probes()
    store, service = _service(probes)
    original = service.save_selected(
        **OWNER_A,
        candidate=_device(model="sdk_gphone64_x86_64"),
        display_name="V2241A",
        is_default=True,
    )
    duplicate = replace(
        original,
        profile_id="profile_legacy_duplicate",
        canonical_device_id="emulator:profile_legacy_duplicate",
        display_name="V2241A copy",
        is_default=False,
    )
    store.save_profile(duplicate)

    calls_before_projection = probes.calls
    projected = service.projections(**OWNER_A)
    assert probes.calls == calls_before_projection + 1
    assert len(projected) == 1
    assert projected[0]["profile_id"] == original.profile_id
    assert service.require_profile(
        **OWNER_A, profile_id=original.profile_id
    ).profile_id == original.profile_id
    assert service.require_profile(
        **OWNER_A, profile_id=duplicate.profile_id
    ).profile_id == duplicate.profile_id
    store.close()


def test_three_duplicates_remain_coalesced_across_boot_change_and_discovery() -> None:
    probes = _Probes()
    store, service = _service(probes)
    original = service.save_selected(
        **OWNER_A,
        candidate=_device(model="V2241A"),
        display_name="V2241A",
        is_default=True,
    )
    duplicates = []
    for suffix in ("two", "three"):
        duplicate = replace(
            original,
            profile_id=f"profile_legacy_{suffix}",
            canonical_device_id=f"emulator:profile_legacy_{suffix}",
            display_name=f"V2241A {suffix}",
            is_default=False,
        )
        store.save_profile(duplicate)
        duplicates.append(duplicate)

    probes.value = EmulatorProbe(
        original.transport_serial,
        EmulatorProfileState.READY,
        "boot-after-restart",
        FINGERPRINT,
        ("screen",),
    )
    first_projection = service.projections(**OWNER_A)
    assert len(first_projection) == 1
    assert first_projection[0]["profile_id"] == original.profile_id
    assert service.discover_candidates(
        **OWNER_A, devices=[_device(model="V2241A")]
    ) == ()
    second_projection = service.projections(**OWNER_A)
    assert len(second_projection) == 1
    assert second_projection[0]["profile_id"] == original.profile_id
    assert second_projection[0]["state"] == "ready"
    assert service.require_profile(
        **OWNER_A, profile_id=duplicates[1].profile_id
    ).profile_id == duplicates[1].profile_id
    store.close()


def test_profile_projection_reports_fresh_connection_state() -> None:
    probes = _Probes()
    store, service = _service(probes)
    profile = service.save_selected(
        **OWNER_A, candidate=_device(), display_name="V2241A", is_default=True
    )
    probes.value = EmulatorProbe(
        profile.transport_serial,
        EmulatorProfileState.OFFLINE,
        None,
        None,
        error_code="emulator_offline",
    )
    disconnected = service.projections(**OWNER_A)
    assert disconnected[0]["state"] == "offline"
    assert disconnected[0]["is_default"] is True

    probes.value = EmulatorProbe(
        profile.transport_serial,
        EmulatorProfileState.READY,
        "boot-a",
        FINGERPRINT,
        ("screen",),
    )
    connected = service.projections(**OWNER_A)
    assert connected[0]["state"] == "ready"
    assert connected[0]["is_default"] is True
    store.close()


def test_default_is_unique_and_offline_profile_is_explainable() -> None:
    probes = _Probes()
    store, service = _service(probes)
    first = service.save_selected(**OWNER_A, candidate=_device(), display_name="first", is_default=True)
    probes.value = EmulatorProbe("emulator-5556", EmulatorProfileState.READY, "boot-b", FINGERPRINT, ("screen",))
    second = service.save_selected(**OWNER_A, candidate=_device("emulator-5556"), display_name="second", is_default=True)

    profiles = store.list_profiles(
        owner_principal_id=OWNER_A["principal_id"],
        owner_controller_id=OWNER_A["controller_id"],
    )
    assert [profile.profile_id for profile in profiles if profile.is_default] == [second.profile_id]
    assert store.get_profile(
        owner_principal_id=OWNER_A["principal_id"],
        owner_controller_id=OWNER_A["controller_id"],
        profile_id=first.profile_id,
    ).is_default is False  # type: ignore[union-attr]

    probes.value = EmulatorProbe("emulator-5556", EmulatorProfileState.OFFLINE, None, None, error_code="emulator_offline")
    offline = service.verify(**OWNER_A, profile_id=second.profile_id)
    assert offline.state is EmulatorProfileState.OFFLINE
    assert offline.last_error_code == "emulator_offline"
    store.close()


def test_resolve_ready_preserves_only_safe_probe_reason_after_offline_write() -> None:
    probes = _Probes()
    store, service = _service(probes)
    profile = service.save_selected(**OWNER_A, candidate=_device(), display_name="API 35")

    probes.value = EmulatorProbe(
        "emulator-5554",
        EmulatorProfileState.OFFLINE,
        None,
        None,
        error_code="emulator_probe_timeout",
    )
    offline = service.verify(**OWNER_A, profile_id=profile.profile_id)
    assert offline.state is EmulatorProfileState.OFFLINE
    assert offline.last_error_code == "emulator_probe_timeout"
    # The persisted state remains offline, while the caller receives the
    # bounded reason needed to distinguish a retryable probe failure from a
    # durable identity mismatch.
    try:
        service.resolve_ready(**OWNER_A, profile_id=profile.profile_id)
    except EmulatorProfileServiceError as error:
        assert error.code == "emulator_probe_timeout"
    else:  # pragma: no cover - test guard
        raise AssertionError("offline profile resolved as ready")

    probes.value = EmulatorProbe(
        "emulator-5554",
        EmulatorProfileState.OFFLINE,
        None,
        None,
        error_code="untrusted_probe_detail",
    )
    untrusted = service.verify(**OWNER_A, profile_id=profile.profile_id)
    assert untrusted.last_error_code == "emulator_probe_failed"
    store.close()


def test_profile_is_owned_by_principal_controller_pair_and_wrong_owner_never_probes() -> None:
    probes = _Probes()
    store, service = _service(probes)
    profile = service.save_selected(
        **OWNER_A,
        candidate=_device(),
        display_name="principal A default",
        is_default=True,
    )
    calls_after_save = probes.calls

    # DSH session is intentionally absent: a new session from the same owner
    # pair receives the same saved/default profile.
    assert service.default_profile(**OWNER_A).profile_id == profile.profile_id
    assert service.projections(**OWNER_A)[0]["profile_id"] == profile.profile_id
    calls_after_projection = probes.calls
    for foreign_owner in (OWNER_A_OTHER_CONTROLLER, OWNER_B):
        assert service.projections(**foreign_owner) == ()
        try:
            service.resolve_ready(**foreign_owner, profile_id=profile.profile_id)
        except EmulatorProfileServiceError as error:
            assert error.code == "emulator_profile_not_found"
        else:  # pragma: no cover - test guard
            raise AssertionError("foreign owner resolved another owner's profile")
        for mutation in (service.set_default, service.disable):
            try:
                mutation(**foreign_owner, profile_id=profile.profile_id)
            except EmulatorProfileServiceError as error:
                assert error.code == "emulator_profile_not_found"
            else:  # pragma: no cover - test guard
                raise AssertionError("foreign owner mutated another owner's profile")
    assert calls_after_projection == calls_after_save + 1
    assert probes.calls == calls_after_projection

    other = service.save_selected(
        **OWNER_A_OTHER_CONTROLLER,
        candidate=_device(),
        display_name="same principal, other controller default",
        is_default=True,
    )
    calls_after_b_save = probes.calls
    try:
        service.save_selected(
            **OWNER_A_OTHER_CONTROLLER,
            candidate=_device(),
            display_name="attempted overwrite",
            is_default=True,
            profile_id=profile.profile_id,
        )
    except EmulatorProfileServiceError as error:
        assert error.code == "emulator_profile_not_found"
    else:  # pragma: no cover - test guard
        raise AssertionError("foreign owner overwrote another owner's profile")
    assert probes.calls == calls_after_b_save
    assert service.default_profile(**OWNER_A).profile_id == profile.profile_id
    assert service.default_profile(**OWNER_A_OTHER_CONTROLLER).profile_id == other.profile_id
    store.close()


def test_owner_controller_migration_quarantines_legacy_rows_and_rebuilds_default_index(tmp_path) -> None:
    database_path = tmp_path / "legacy-owner.sqlite"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE emulator_profiles (
            profile_id TEXT PRIMARY KEY,
            owner_principal_id TEXT NOT NULL,
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
        CREATE UNIQUE INDEX one_default_emulator_profile_per_principal
        ON emulator_profiles(owner_principal_id) WHERE is_default = 1;
        CREATE TABLE emulator_settings_runs (
            task_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            profile_id TEXT NOT NULL REFERENCES emulator_profiles(profile_id),
            owner_principal_id TEXT NOT NULL,
            profile_generation INTEGER NOT NULL,
            boot_id TEXT NOT NULL,
            status TEXT NOT NULL,
            before_observation_id TEXT,
            after_observation_id TEXT,
            command_id TEXT,
            frame_ref TEXT,
            evidence_refs_json TEXT NOT NULL,
            reason_code TEXT,
            PRIMARY KEY(task_id, action_id)
        );
        """
    )
    connection.execute(
        """
        INSERT INTO emulator_profiles VALUES (
            'legacy-profile', 'principal-a', 'legacy', 1, 1, 'emulator-5554', 1,
            'ready', 'boot-a', '{"boot_completed": true, "api_level": 35, "abi": "x86_64", "resolution": "1080x2400", "density": 440}',
            '["screen"]', NULL, NULL, '2026-08-30T00:00:00+00:00', '2026-08-30T00:00:00+00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO emulator_settings_runs VALUES (
            'task-1', 'action-1', 'legacy-profile', 'principal-a', 1, 'boot-a', 'recovering',
            'obs-1', NULL, 'command-1', 'frame-1', '["evidence-1"]', NULL
        )
        """
    )
    connection.commit()
    connection.close()

    store = EmulatorProfileStore(database_path)
    migrated = sqlite3.connect(database_path)
    profile_columns = {row[1] for row in migrated.execute("PRAGMA table_info(emulator_profiles)")}
    run_columns = {row[1] for row in migrated.execute("PRAGMA table_info(emulator_settings_runs)")}
    profile_controller = migrated.execute(
        "SELECT owner_controller_id FROM emulator_profiles WHERE profile_id = 'legacy-profile'"
    ).fetchone()[0]
    run_controller = migrated.execute(
        "SELECT owner_controller_id FROM emulator_settings_runs WHERE task_id = 'task-1'"
    ).fetchone()[0]
    default_index = migrated.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'one_default_emulator_profile_per_owner_pair'"
    ).fetchone()[0]
    migrated.close()

    assert "owner_controller_id" in profile_columns
    assert "owner_controller_id" in run_columns
    assert profile_controller == run_controller == "legacy:unbound"
    assert "owner_principal_id, owner_controller_id" in default_index
    service = EmulatorProfileService(store=store, probe=_Probes(), clock=lambda: NOW)
    assert service.projections(**OWNER_A) == ()
    try:
        service.projections(principal_id="principal-a", controller_id="legacy:unbound")
    except EmulatorProfileServiceError as error:
        assert error.code == "capability_controller_invalid"
    else:  # pragma: no cover - test guard
        raise AssertionError("legacy controller sentinel became callable")
    store.close()
