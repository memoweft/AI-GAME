"""Private SQLite step ledger; canonical Task truth remains outside it."""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .domain import (
    AndroidUiAction, AndroidUiActionIntent, CanonicalSnapshot, CriteriaRevision, Criterion,
    GoalVerificationRecord, ObservationArtifactGroundingPort,
    ObservationEnvelope, ObservationGroundingQuery, OwnerBinding, RoleDecision,
    TrustedObservationGroundingManifest, criteria_coverage_digest, criteria_digest,
    observation_artifact_digest, opaque_digest, owner_scope_digest, OrderedWaypoint,
    criterion_description_digest,
    CRITERIA_IDENTITY_CURRENT, CRITERIA_IDENTITY_LEGACY_HASHED,
    CRITERIA_IDENTITY_LEGACY_RAW,
)
from .sanitizer import safe_action_payload, sanitize_summary, sanitize_task_goal
from .semantic_verification import _is_verifier_issued

_OPAQUE = re.compile(r"(?:[a-f0-9]{32,128}|[A-Za-z0-9][A-Za-z0-9_-]{0,255})")
_BOUND_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_DIGEST = re.compile(r"[a-f0-9]{32,128}")


@dataclass(frozen=True, slots=True)
class StepReservation:
    task_id: str
    step_index: int
    revision: int
    action_intent_id: str | None
    action: AndroidUiAction | None
    decision_kind: str | None
    selected_hint_id: str | None = None
    claim_id: str | None = None
    has_after: bool = False


@dataclass(frozen=True, slots=True)
class TrustedTerminalVerification:
    """Read-only exact terminal fact for K3/K4; this is not a write surface."""
    verification_ref: str
    overall: str
    task_id: str
    revision: int
    step_index: int
    criteria_digest: str
    owner_scope_digest: str
    profile_id: str
    profile_generation: int
    boot_id: str
    canonical_device_id: str
    runner_kind: str
    runner_version: int
    runner_binding_digest: str
    goal_digest: str | None
    coverage_digest: str | None
    before_observation_digest: str | None
    after_observation_digest: str
    artifact_digest: str
    grounding_digest: str
    causal_command_id: str | None
    primitive_outcome: str
    model_version_digest: str
    prompt_version_digest: str


@dataclass(frozen=True, slots=True)
class CheckpointBaselineQuery:
    """Typed K3 request; a caller cannot submit a checkpoint integer."""

    snapshot: CanonicalSnapshot
    criteria: CriteriaRevision
    observation: ObservationEnvelope
    runner_kind: str
    runner_version: int


@dataclass(frozen=True, slots=True)
class TrustedCheckpointBaselineAttestation:
    task_id: str
    owner_scope_digest: str
    runner_kind: str
    runner_version: int
    runner_binding_digest: str
    revision: int
    criteria_digest: str
    checkpoint_index: int
    observation_digest: str
    causal_command_id: str | None
    _seal: object


class CheckpointBaselineAttestationPort(Protocol):
    def attest(
        self, query: CheckpointBaselineQuery,
    ) -> TrustedCheckpointBaselineAttestation | None: ...

    def validates(self, attestation: TrustedCheckpointBaselineAttestation) -> bool: ...

    def validates_for(
        self,
        query: CheckpointBaselineQuery,
        attestation: TrustedCheckpointBaselineAttestation,
    ) -> bool: ...


class SQLiteAndroidUiStepStore:
    def __init__(
        self, path: str | Path, *,
        grounding: ObservationArtifactGroundingPort | None = None,
    ) -> None:
        self._path = str(path)
        self._grounding = grounding
        self._checkpoint_attestation_seal = object()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS android_ui_criteria (
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
                    criteria_json TEXT NOT NULL, goal_digest TEXT, coverage_digest TEXT,
                    coverage_json TEXT NOT NULL DEFAULT '[]',
                    waypoints_json TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(task_id, revision)
                );
                CREATE TABLE IF NOT EXISTS android_ui_steps (
                    task_id TEXT NOT NULL, step_index INTEGER NOT NULL, revision INTEGER NOT NULL,
                    action_intent_id TEXT, intent_json TEXT, claim_json TEXT, before_json TEXT, after_json TEXT,
                    decision_json TEXT, binding_json TEXT, primitive_json TEXT,
                    recovery_generation INTEGER NOT NULL DEFAULT 0,
                    retrieval_json TEXT NOT NULL DEFAULT '[]', completed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(task_id, step_index)
                );
                CREATE TABLE IF NOT EXISTS android_ui_goal_verifications (
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, step_index INTEGER NOT NULL,
                    record_json TEXT NOT NULL, PRIMARY KEY(task_id, revision, step_index)
                );
                CREATE TABLE IF NOT EXISTS android_ui_waypoint_proofs (
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    waypoint_ordinal INTEGER NOT NULL, criteria_digest TEXT NOT NULL,
                    step_index INTEGER NOT NULL, record_json TEXT NOT NULL,
                    PRIMARY KEY(task_id, revision, waypoint_ordinal)
                );
            """)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(android_ui_criteria)")
            }
            if "goal_digest" not in columns:
                connection.execute(
                    "ALTER TABLE android_ui_criteria ADD COLUMN goal_digest TEXT"
                )
            if "coverage_digest" not in columns:
                connection.execute(
                    "ALTER TABLE android_ui_criteria ADD COLUMN coverage_digest TEXT"
                )
            if "coverage_json" not in columns:
                connection.execute(
                    "ALTER TABLE android_ui_criteria ADD COLUMN coverage_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "waypoints_json" not in columns:
                connection.execute(
                    "ALTER TABLE android_ui_criteria ADD COLUMN waypoints_json TEXT NOT NULL DEFAULT '[]'"
                )
            step_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(android_ui_steps)")
            }
            if "binding_json" not in step_columns:
                connection.execute(
                    "ALTER TABLE android_ui_steps ADD COLUMN binding_json TEXT"
                )
            if "primitive_json" not in step_columns:
                connection.execute(
                    "ALTER TABLE android_ui_steps ADD COLUMN primitive_json TEXT"
                )

    def freeze_criteria(
        self, task_id: str, criteria: CriteriaRevision, *,
        coverage: tuple[Mapping[str, Any], ...] = (),
    ) -> CriteriaRevision:
        if criteria.goal_digest is not None and not coverage:
            # Production freezes the proof exactly once in its criteria
            # provider.  The generic handler may subsequently assert the same
            # immutable revision, but it may not create a covered row without
            # the proof or replace it after restart.
            loaded = self.load_criteria(task_id, criteria.revision)
            if loaded is not None and _criteria_identity_matches(loaded, criteria):
                return criteria
            raise ValueError("covered criteria require their durable proof")
        existing = self.load_criteria(task_id, criteria.revision)
        if existing is not None and _criteria_identity_matches(existing, criteria):
            # Legacy rows are immutable read-only identities.  Returning here
            # lets handler restart and semantic verification continue without
            # rewriting their raw historical payload into a new format.
            return criteria
        if existing is None and criteria.identity_scheme != CRITERIA_IDENTITY_CURRENT:
            raise ValueError("legacy criteria identity requires an existing revision")
        normalized = tuple(type(item)(
            item.criterion_id, sanitize_task_goal(item.description, maximum=400),
            item.required_evidence_markers, item.description_digest,
        ) for item in criteria.criteria)
        coverage_payload = _coverage_payload(coverage)
        waypoints_payload = _waypoints_payload(criteria.waypoints)
        if criteria.goal_digest is None:
            if coverage:
                raise ValueError("legacy criteria cannot carry goal coverage")
            coverage_json = "[]"
        else:
            if not coverage or criteria_coverage_digest(coverage_payload) != criteria.coverage_digest:
                raise ValueError("criteria coverage proof does not match its digest")
            coverage_json = json.dumps(
                coverage_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        recomputed = criteria_digest(
            normalized,
            goal_digest=criteria.goal_digest,
            coverage_digest=criteria.coverage_digest,
            waypoints=criteria.waypoints,
            identity_scheme=criteria.identity_scheme,
        )
        if normalized != criteria.criteria or recomputed != criteria.digest:
            raise ValueError("criteria must be sanitized with a matching digest")
        payload = json.dumps([{
            "criterion_id": item.criterion_id,
            "description": "[redacted]",
            "description_digest": criterion_description_digest(item),
            "required_evidence_markers": item.required_evidence_markers,
        } for item in normalized], ensure_ascii=False, sort_keys=True)
        waypoints_json = json.dumps(
            waypoints_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            prior = connection.execute(
                """SELECT digest, criteria_json, goal_digest, coverage_digest,
                coverage_json, waypoints_json FROM android_ui_criteria WHERE task_id=? AND revision=?""",
                (task_id, criteria.revision),
            ).fetchone()
            if prior is not None:
                if (
                    prior["digest"] != criteria.digest
                    or prior["criteria_json"] != payload
                    or prior["goal_digest"] != criteria.goal_digest
                    or prior["coverage_digest"] != criteria.coverage_digest
                    or prior["coverage_json"] != coverage_json
                    or prior["waypoints_json"] != waypoints_json
                ):
                    raise ValueError("criteria revision is immutable")
            else:
                connection.execute(
                    """INSERT INTO android_ui_criteria(
                    task_id, revision, digest, criteria_json, goal_digest,
                    coverage_digest, coverage_json, waypoints_json) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        task_id, criteria.revision, criteria.digest, payload,
                        criteria.goal_digest, criteria.coverage_digest,
                        coverage_json, waypoints_json,
                    ),
                )
        return criteria

    def load_criteria(self, task_id: str, revision: int) -> CriteriaRevision | None:
        """Reload one immutable criteria revision without exposing another Task.

        Production composition calls this before invoking a compiler so a
        process restart cannot silently reinterpret a goal or issue a second
        set of terminal conditions for the same canonical revision.
        """

        with self._connect() as connection:
            row = connection.execute(
                """SELECT digest, criteria_json, goal_digest, coverage_digest,
                coverage_json, waypoints_json FROM android_ui_criteria """
                "WHERE task_id=? AND revision=?",
                (task_id, revision),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = _strict_json_loads(str(row["criteria_json"]))
            waypoints = _waypoints_from_payload(
                _strict_json_loads(str(row["waypoints_json"])),
            )
            identity_scheme = _criteria_identity_scheme_from_payload(
                payload,
                stored_digest=str(row["digest"]),
                goal_digest=(
                    str(row["goal_digest"])
                    if row["goal_digest"] is not None else None
                ),
                coverage_digest=(
                    str(row["coverage_digest"])
                    if row["coverage_digest"] is not None else None
                ),
                waypoints=waypoints,
            )
            criteria = _criteria_from_payload_items(payload, identity_scheme)
            loaded = CriteriaRevision(
                int(revision), criteria, str(row["digest"]),
                (str(row["goal_digest"]) if row["goal_digest"] is not None else None),
                (
                    str(row["coverage_digest"])
                    if row["coverage_digest"] is not None else None
                ),
                waypoints,
                identity_scheme,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("stored criteria revision is invalid") from error
        if (
            not criteria
            or loaded.digest != _criteria_digest_from_payload(
                payload,
                goal_digest=loaded.goal_digest,
                coverage_digest=loaded.coverage_digest,
                waypoints=loaded.waypoints,
                identity_scheme=loaded.identity_scheme,
            )
        ):
            raise ValueError("stored criteria revision digest is invalid")
        if loaded.goal_digest is not None:
            try:
                coverage = tuple(json.loads(str(row["coverage_json"])))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("stored criteria coverage proof is invalid") from error
            if (
                not coverage
                or criteria_coverage_digest(coverage) != loaded.coverage_digest
            ):
                raise ValueError("stored criteria coverage digest is invalid")
        return loaded

    def coverage_is_frozen(
        self, task_id: str, criteria: CriteriaRevision,
    ) -> bool:
        """Whether the exact production goal-to-marker proof remains durable."""

        if criteria.goal_digest is None or criteria.coverage_digest is None:
            return False
        try:
            loaded = self.load_criteria(task_id, criteria.revision)
        except (OSError, sqlite3.Error, ValueError):
            return False
        return loaded is not None and _criteria_identity_matches(loaded, criteria)

    def next_unmet_waypoint(
        self, snapshot: CanonicalSnapshot, criteria: CriteriaRevision,
    ) -> int | None:
        """Return the next ordered route obligation or fail cold on drift.

        A terminal K2 record is insufficient while any waypoint proof is
        absent.  Proofs are bound to the same canonical identity as an action
        step, so revision/profile/boot/device changes cannot inherit an old
        route history.
        """

        if not criteria.waypoints:
            return None
        with self._connect() as connection:
            parent = connection.execute(
                "SELECT * FROM android_ui_criteria WHERE task_id=? AND revision=?",
                (snapshot.task_id, snapshot.revision),
            ).fetchone()
            if parent is None or parent["digest"] != criteria.digest:
                raise ValueError("ordered waypoint criteria are unavailable")
            for waypoint in criteria.waypoints:
                proof = connection.execute(
                    """SELECT * FROM android_ui_waypoint_proofs
                    WHERE task_id=? AND revision=? AND waypoint_ordinal=?""",
                    (snapshot.task_id, snapshot.revision, waypoint.ordinal),
                ).fetchone()
                if proof is None:
                    return waypoint.ordinal
                if not self._waypoint_proof_matches(
                    connection, snapshot, waypoint, proof,
                ):
                    raise ValueError("ordered waypoint proof is invalid")
        return None

    def record_waypoint_verification(
        self, *, parent: CriteriaRevision, waypoint_ordinal: int,
        record: GoalVerificationRecord,
    ) -> None:
        """Commit one satisfied prerequisite together with its K1/K2 proof."""

        if not _is_verifier_issued(record):
            raise ValueError("waypoint verification requires an exact verifier witness")
        waypoint = next(
            (item for item in parent.waypoints if item.ordinal == waypoint_ordinal),
            None,
        )
        if waypoint is None:
            raise ValueError("ordered waypoint is not frozen")
        if record.criteria_digest != waypoint.digest or record.overall != "satisfied" or record.already_satisfied:
            raise ValueError("ordered waypoint requires a causal satisfied verification")
        payload = json.dumps(_waypoint_proof_record(record), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            criteria_row = connection.execute(
                "SELECT * FROM android_ui_criteria WHERE task_id=? AND revision=?",
                (record.task_id, record.revision),
            ).fetchone()
            step = connection.execute(
                """SELECT * FROM android_ui_steps
                WHERE task_id=? AND step_index=? AND revision=?""",
                (record.task_id, record.latest_step_index, record.revision),
            ).fetchone()
            latest = connection.execute(
                "SELECT MAX(step_index) AS latest FROM android_ui_steps WHERE task_id=? AND revision=?",
                (record.task_id, record.revision),
            ).fetchone()
            if (
                criteria_row is None or step is None
                or criteria_row["digest"] != parent.digest
                or int(latest["latest"]) != record.latest_step_index
            ):
                raise ValueError("ordered waypoint ledger binding is unavailable")
            expected = connection.execute(
                """SELECT MAX(waypoint_ordinal) AS latest
                FROM android_ui_waypoint_proofs WHERE task_id=? AND revision=?""",
                (record.task_id, record.revision),
            ).fetchone()
            if int(expected["latest"] or 0) + 1 != waypoint_ordinal:
                raise ValueError("ordered waypoint proof is out of sequence")
            evidence_row = {
                "digest": waypoint.digest,
                "criteria_json": _criteria_payload(waypoint.criteria),
                "goal_digest": None,
                "coverage_digest": None,
                "waypoints_json": "[]",
            }
            grounding = _resolve_trusted_grounding(
                self._grounding, _step_grounding_query(step),
            )
            prior_effect = _latest_prior_effect(connection, step)
            _validate_verification_against_ledger(
                record=record,
                criteria_row=evidence_row,
                step_row=step,
                latest_step_index=int(latest["latest"]),
                grounding=grounding,
                has_prior_effect=prior_effect is not None,
                causal_predecessor=(
                    prior_effect is not None
                    and _prior_effect_causes_observation(
                        prior_effect, step, record.before,
                    )
                ),
            )
            prior = connection.execute(
                """SELECT record_json FROM android_ui_waypoint_proofs
                WHERE task_id=? AND revision=? AND waypoint_ordinal=?""",
                (record.task_id, record.revision, waypoint_ordinal),
            ).fetchone()
            if prior is not None and prior["record_json"] != payload:
                raise ValueError("ordered waypoint proof is immutable")
            connection.execute(
                """INSERT OR IGNORE INTO android_ui_waypoint_proofs(
                task_id,revision,waypoint_ordinal,criteria_digest,step_index,record_json
                ) VALUES(?,?,?,?,?,?)""",
                (
                    record.task_id, record.revision, waypoint_ordinal,
                    waypoint.digest, record.latest_step_index, payload,
                ),
            )
            connection.execute(
                "UPDATE android_ui_steps SET completed=1 WHERE task_id=? AND step_index=?",
                (record.task_id, record.latest_step_index),
            )

    def _waypoint_proof_matches(
        self, connection: sqlite3.Connection, snapshot: CanonicalSnapshot,
        waypoint: OrderedWaypoint, proof: sqlite3.Row,
    ) -> bool:
        try:
            if (
                proof["criteria_digest"] != waypoint.digest
                or int(proof["waypoint_ordinal"]) != waypoint.ordinal
                or int(proof["step_index"]) < 0
            ):
                return False
            record = json.loads(str(proof["record_json"]))
            step = connection.execute(
                "SELECT * FROM android_ui_steps WHERE task_id=? AND step_index=? AND revision=?",
                (snapshot.task_id, int(proof["step_index"]), snapshot.revision),
            ).fetchone()
            if step is None or step["binding_json"] != _binding_json(snapshot):
                return False
            after = record.get("after") if isinstance(record, Mapping) else None
            expected = {
                "task_id": snapshot.task_id,
                "revision": snapshot.revision,
                "criteria_digest": waypoint.digest,
                "owner_scope_digest": owner_scope_digest(snapshot.owner),
                "runner_kind": snapshot.runner_kind,
                "runner_version": snapshot.runner_version,
                "runner_binding_digest": opaque_digest(
                    "runner-binding", snapshot.runner_binding_id,
                ),
            }
            if (
                not isinstance(after, Mapping)
                or any(record.get(key) != value for key, value in expected.items())
                or record.get("overall") != "satisfied"
                or record.get("already_satisfied") is not False
                or int(record.get("latest_step_index", -1)) != int(proof["step_index"])
            ):
                return False
            criteria_row = {
                "digest": waypoint.digest,
                "criteria_json": _criteria_payload(waypoint.criteria),
                "goal_digest": None,
                "coverage_digest": None,
                "waypoints_json": "[]",
            }
            grounding = _resolve_trusted_grounding(
                self._grounding, _step_grounding_query(step),
            )
            return grounding is not None and _durable_terminal_matches_ledger(
                value=record,
                criteria_row=criteria_row,
                step_row=step,
                latest_step_index=int(proof["step_index"]),
                grounding=grounding,
            )
        except (KeyError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            return False

    def complete_waypoint_attempt(
        self, reservation: StepReservation, record: GoalVerificationRecord,
    ) -> None:
        """Close a causally settled but unsatisfied waypoint attempt.

        The step remains an immutable K1/effect audit record, while absence of
        a matching waypoint proof prevents it from advancing route history.
        """

        if record.overall == "satisfied" or record.already_satisfied:
            raise ValueError("satisfied waypoint attempts require a proof")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            step = _require_step(connection, reservation)
            if (
                int(step["revision"]) != record.revision
                or int(step["step_index"]) != record.latest_step_index
                or step["after_json"] != _observation_json(record.after)
                or step["claim_json"] is None
                or step["primitive_json"] is None
                or step["binding_json"] != _record_binding_json(record)
            ):
                raise ValueError("waypoint attempt is not causally settled")
            connection.execute(
                "UPDATE android_ui_steps SET completed=1 WHERE task_id=? AND step_index=?",
                (reservation.task_id, reservation.step_index),
            )

    def reserve_step(
        self, *, task_id: str, revision: int, before: ObservationEnvelope,
        snapshot: CanonicalSnapshot | None = None,
    ) -> StepReservation:
        """Atomically recover one unfinished step or allocate one new index."""
        if before.task_id != task_id:
            raise ValueError("before observation belongs to another task")
        binding = _binding_json(snapshot) if snapshot is not None else None
        if snapshot is not None and (
            snapshot.task_id != task_id or snapshot.revision != revision
        ):
            raise ValueError("step binding does not match the canonical Task")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM android_ui_steps WHERE task_id=? AND completed=0 ORDER BY step_index DESC LIMIT 1", (task_id,)).fetchone()
            if row is None:
                maximum = connection.execute("SELECT COALESCE(MAX(step_index), -1) AS maximum FROM android_ui_steps WHERE task_id=?", (task_id,)).fetchone()
                index = int(maximum["maximum"]) + 1
                connection.execute(
                    """INSERT INTO android_ui_steps(
                    task_id,step_index,revision,before_json,binding_json,retrieval_json
                    ) VALUES(?,?,?,?,?,?)""",
                    (task_id, index, revision, _observation_json(before), binding, "[]"),
                )
                return StepReservation(task_id, index, revision, None, None, None)
            if int(row["revision"]) != revision:
                raise ValueError("unfinished step belongs to a stale revision")
            if binding is not None and row["binding_json"] != binding:
                raise ValueError("unfinished step belongs to another canonical binding")
            return _reservation_from_row(row)

    def record_decision(self, reservation: StepReservation, decision: RoleDecision, *, retrieval_attribution: tuple[Mapping[str, Any], ...] = ()) -> StepReservation:
        payload = _decision_json(decision)
        retrieval = _safe_retrieval(retrieval_attribution)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _require_step(connection, reservation)
            if row["decision_json"] not in (None, payload) or row["retrieval_json"] not in ("[]", retrieval):
                raise ValueError("step decision is immutable")
            connection.execute("UPDATE android_ui_steps SET decision_json=COALESCE(decision_json, ?), retrieval_json=CASE WHEN retrieval_json='[]' THEN ? ELSE retrieval_json END WHERE task_id=? AND step_index=?", (payload, retrieval, reservation.task_id, reservation.step_index))
            return _reservation_from_row(_require_step(connection, reservation))

    def before_matches(self, reservation: StepReservation, observation: ObservationEnvelope) -> bool:
        """Whether recovery still has the exact pre-decision observation."""
        with self._connect() as connection:
            row = _require_step(connection, reservation)
        return row["before_json"] == _observation_json(observation)

    def selected_hint_attribution(
        self, reservation: StepReservation,
    ) -> tuple[str, str] | None:
        """Recover the one durable K3 selection bound to this exact step.

        The durable retrieval projection contains opaque identifiers only. A
        recovery must never retrieve again or infer a selection from a changed
        sidecar result: malformed, missing, duplicate, or mismatched evidence
        is deliberately cold rather than being attributed to a hint.
        """

        if reservation.selected_hint_id is None:
            return None
        with self._connect() as connection:
            row = _require_step(connection, reservation)
        try:
            items = json.loads(str(row["retrieval_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("stored retrieval attribution is invalid") from error
        if not isinstance(items, list):
            raise ValueError("stored retrieval attribution is invalid")
        selected: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("stored retrieval attribution is invalid")
            candidate = item.get("candidate_id")
            provenance = item.get("provenance")
            marker = item.get("selected")
            if (
                not isinstance(candidate, str)
                or not isinstance(provenance, str)
                or not isinstance(marker, bool)
                or not _OPAQUE.fullmatch(candidate)
                or not _OPAQUE.fullmatch(provenance)
            ):
                raise ValueError("stored retrieval attribution is invalid")
            if marker:
                selected.append((candidate, provenance))
        if len(selected) != 1 or selected[0][0] != reservation.selected_hint_id:
            raise ValueError("stored selected hint attribution is invalid")
        return selected[0]

    def record_intent(self, reservation: StepReservation, intent: AndroidUiActionIntent) -> AndroidUiActionIntent:
        if intent.task_id != reservation.task_id or intent.step_index != reservation.step_index or intent.criteria_revision != reservation.revision:
            raise ValueError("intent does not belong to reserved step")
        payload = json.dumps(safe_action_payload(intent.action.kind, intent.action.arguments), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _require_step(connection, reservation)
            if row["action_intent_id"] not in (None, intent.action_intent_id) or row["intent_json"] not in (None, payload):
                raise ValueError("step intent is immutable")
            if row["decision_json"] is None:
                raise ValueError("intent requires a recorded decision")
            connection.execute("UPDATE android_ui_steps SET action_intent_id=COALESCE(action_intent_id, ?), intent_json=COALESCE(intent_json, ?) WHERE task_id=? AND step_index=?", (intent.action_intent_id, payload, intent.task_id, intent.step_index))
        return intent

    def record_claim(self, reservation: StepReservation, *, claim_id: str, action_intent_id: str) -> StepReservation:
        """Persist K1's accepted command identity before an after-observation.

        The claim does not say a physical effect happened.  It is solely the
        durable reconciliation boundary after a response drop.  A restart with
        an intent never issues a new action; it must reconcile this same ID.
        """
        if not _OPAQUE.fullmatch(claim_id):
            raise ValueError("command claim does not match the reserved action")
        payload = json.dumps({"claim_id": claim_id, "action_intent_id": action_intent_id}, sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _require_step(connection, reservation)
            if row["action_intent_id"] != action_intent_id:
                raise ValueError("command claim does not match the reserved action")
            if row["claim_json"] not in (None, payload):
                raise ValueError("command claim is immutable")
            connection.execute("UPDATE android_ui_steps SET claim_json=COALESCE(claim_json, ?) WHERE task_id=? AND step_index=?", (payload, reservation.task_id, reservation.step_index))
            return _reservation_from_row(_require_step(connection, reservation))

    def record_after(self, reservation: StepReservation, after: ObservationEnvelope, *, recovery_generation: int = 0) -> None:
        if after.task_id != reservation.task_id:
            raise ValueError("after observation belongs to another task")
        payload = _observation_json(after)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _require_step(connection, reservation)
            claim = json.loads(str(row["claim_json"])) if row["claim_json"] else None
            if (
                row["action_intent_id"] is None
                or not isinstance(claim, dict)
                or claim.get("claim_id") != after.causality_command_id
            ):
                raise ValueError(
                    "after observation requires the exact causal command claim",
                )
            if row["after_json"] not in (None, payload):
                raise ValueError("after observation is immutable")
            connection.execute("UPDATE android_ui_steps SET after_json=COALESCE(after_json, ?), recovery_generation=? WHERE task_id=? AND step_index=?", (payload, recovery_generation, reservation.task_id, reservation.step_index))

    def record_primitive_outcome(
        self, reservation: StepReservation, *, command_id: str, outcome: str,
    ) -> None:
        if not _OPAQUE.fullmatch(command_id) or outcome not in {
            "progress", "no_progress", "uncertain",
        }:
            raise ValueError("primitive outcome is not bounded")
        payload = json.dumps(
            {"command_id": command_id, "outcome": outcome},
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _require_step(connection, reservation)
            claim = json.loads(str(row["claim_json"])) if row["claim_json"] else None
            if (
                row["after_json"] is None
                or not isinstance(claim, dict)
                or claim.get("claim_id") != command_id
                or row["primitive_json"] not in (None, payload)
            ):
                raise ValueError("primitive outcome does not match the causal step")
            before_value = json.loads(str(row["before_json"]))
            after_value = json.loads(str(row["after_json"]))
            changed = any(
                before_value.get(key) != after_value.get(key)
                for key in (
                    "screenshot_digest", "ui_tree_digest", "device_state_digest",
                )
            )
            if (outcome == "progress" and not changed) or (
                outcome == "no_progress" and changed
            ):
                raise ValueError(
                    "primitive outcome contradicts durable observations",
                )
            connection.execute(
                """UPDATE android_ui_steps SET primitive_json=COALESCE(primitive_json, ?)
                WHERE task_id=? AND step_index=?""",
                (payload, reservation.task_id, reservation.step_index),
            )

    def pending_after_recovery(
        self, reservation: StepReservation,
    ) -> dict[str, Any] | None:
        """Return only immutable, text-free facts needed to settle one after.

        This is not a command-replay surface. The step must already own an
        exact claim, after observation, and primitive outcome, and must still
        be the current incomplete reservation.
        """

        try:
            with self._connect() as connection:
                row = _require_step(connection, reservation)
                before = json.loads(str(row["before_json"]))
                after = json.loads(str(row["after_json"]))
                claim = json.loads(str(row["claim_json"]))
                primitive = json.loads(str(row["primitive_json"]))
            if (
                row["action_intent_id"] is None
                or not isinstance(before, dict)
                or not isinstance(after, dict)
                or not isinstance(claim, dict)
                or not isinstance(primitive, dict)
                or claim.get("claim_id") != after.get("causality_command_id")
                or primitive.get("command_id") != claim.get("claim_id")
                or primitive.get("outcome") not in {
                    "progress", "no_progress", "uncertain",
                }
            ):
                return None
            return {
                "before": before,
                "after": after,
                "command_id": str(claim["claim_id"]),
                "primitive_outcome": str(primitive["outcome"]),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def pending_terminal_recovery(
        self, reservation: StepReservation,
    ) -> dict[str, Any] | None:
        """Return the immutable before facts for a zero-effect terminal decision."""

        try:
            with self._connect() as connection:
                row = _require_step(connection, reservation)
                decision = json.loads(str(row["decision_json"]))
                before = json.loads(str(row["before_json"]))
            if (
                not isinstance(decision, dict)
                or decision.get("kind") != "terminal_candidate"
                or not isinstance(before, dict)
                or row["action_intent_id"] is not None
                or row["claim_json"] is not None
                or row["after_json"] is not None
                or row["primitive_json"] is not None
            ):
                return None
            return before
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def terminal_candidate_is_causally_grounded(
        self, reservation: StepReservation, observation: ObservationEnvelope,
    ) -> bool:
        """Require the latest prior effect when a terminal state follows actions."""

        try:
            with self._connect() as connection:
                row = _require_step(connection, reservation)
                if row["before_json"] != _observation_json(observation):
                    return False
                prior = _latest_prior_effect(connection, row)
                if prior is None:
                    return observation.causality_command_id is None
                return _prior_effect_causes_observation(prior, row, observation)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def supersede_terminal_candidate(
        self, reservation: StepReservation, observation: ObservationEnvelope,
    ) -> bool:
        """Complete a stale zero-effect terminal decision at a newer causal fence."""

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = _require_step(connection, reservation)
                decision = json.loads(str(row["decision_json"]))
                prior = _latest_prior_effect(connection, row)
                if (
                    not isinstance(decision, dict)
                    or decision.get("kind") != "terminal_candidate"
                    or row["before_json"] == _observation_json(observation)
                    or row["action_intent_id"] is not None
                    or row["claim_json"] is not None
                    or row["after_json"] is not None
                    or row["primitive_json"] is not None
                    or prior is None
                    or not _prior_effect_causes_observation(
                        prior, row, observation,
                    )
                ):
                    return False
                connection.execute(
                    """UPDATE android_ui_steps SET completed=1
                    WHERE task_id=? AND step_index=?""",
                    (reservation.task_id, reservation.step_index),
                )
                return True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def record_verification(self, record: GoalVerificationRecord) -> None:
        if not _is_verifier_issued(record):
            raise ValueError("goal verification requires an exact verifier-issued witness")
        payload = json.dumps(record.private_durable_record(), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            criteria = connection.execute(
                """SELECT digest, criteria_json, goal_digest, coverage_digest, waypoints_json
                FROM android_ui_criteria WHERE task_id=? AND revision=?""",
                (record.task_id, record.revision),
            ).fetchone()
            if criteria is None:
                raise ValueError("verification criteria are not frozen for this Task revision")
            step = connection.execute(
                """SELECT * FROM android_ui_steps
                WHERE task_id=? AND step_index=? AND revision=?""",
                (record.task_id, record.latest_step_index, record.revision),
            ).fetchone()
            if step is None:
                raise KeyError("verification references no reserved step")
            latest = connection.execute(
                """SELECT MAX(step_index) AS latest FROM android_ui_steps
                WHERE task_id=? AND revision=?""",
                (record.task_id, record.revision),
            ).fetchone()
            grounding = (
                _resolve_trusted_grounding(
                    self._grounding,
                    _step_grounding_query(step),
                )
                if record.overall == "satisfied"
                else None
            )
            prior_effect = _latest_prior_effect(connection, step)
            _validate_verification_against_ledger(
                record=record,
                criteria_row=criteria,
                step_row=step,
                latest_step_index=int(latest["latest"]),
                grounding=grounding,
                has_prior_effect=prior_effect is not None,
                causal_predecessor=(
                    prior_effect is not None
                    and _prior_effect_causes_observation(
                        prior_effect, step, record.before,
                    )
                ),
            )
            prior = connection.execute("SELECT record_json FROM android_ui_goal_verifications WHERE task_id=? AND revision=? AND step_index=?", (record.task_id, record.revision, record.latest_step_index)).fetchone()
            if prior is not None and prior["record_json"] != payload:
                raise ValueError("goal verification is immutable")
            connection.execute("INSERT OR IGNORE INTO android_ui_goal_verifications(task_id,revision,step_index,record_json) VALUES(?,?,?,?)", (record.task_id, record.revision, record.latest_step_index, payload))
            if connection.execute("UPDATE android_ui_steps SET completed=1 WHERE task_id=? AND step_index=?", (record.task_id, record.latest_step_index)).rowcount != 1:
                raise KeyError("verification references no reserved step")

    def complete_replan(self, reservation: StepReservation) -> None:
        """A replan is a completed zero-effect checkpoint, never terminal success."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_step(connection, reservation)
            connection.execute("UPDATE android_ui_steps SET completed=1 WHERE task_id=? AND step_index=?", (reservation.task_id, reservation.step_index))

    def complete_wait(self, reservation: StepReservation) -> None:
        """Complete one durable short wait without creating an effect intent."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _require_step(connection, reservation)
            decision = json.loads(str(row["decision_json"] or "null"))
            action = decision.get("action") if isinstance(decision, dict) else None
            if (
                not isinstance(decision, dict)
                or decision.get("kind") != "action"
                or not isinstance(action, dict)
                or action.get("action") != "wait"
                or row["action_intent_id"] is not None
                or row["claim_json"] is not None
                or row["after_json"] is not None
            ):
                raise ValueError("wait checkpoint must remain zero-effect")
            connection.execute(
                "UPDATE android_ui_steps SET completed=1 WHERE task_id=? AND step_index=?",
                (reservation.task_id, reservation.step_index),
            )

    def has_effect_bearing_step(self, task_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM android_ui_steps WHERE task_id=? AND action_intent_id IS NOT NULL LIMIT 1", (task_id,)).fetchone() is not None

    def safe_events(self, task_id: str) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT step_index,revision,action_intent_id,intent_json,decision_json,primitive_json,recovery_generation,retrieval_json FROM android_ui_steps WHERE task_id=? ORDER BY step_index", (task_id,)).fetchall()
        return tuple({"step_index": int(row["step_index"]), "revision": int(row["revision"]), "action_intent_id": row["action_intent_id"], "intent": json.loads(row["intent_json"]) if row["intent_json"] else None, "decision": _safe_decision(row["decision_json"]), "primitive_outcome": json.loads(row["primitive_json"])["outcome"] if row["primitive_json"] else None, "recovery_generation": int(row["recovery_generation"]), "retrieval": json.loads(row["retrieval_json"])} for row in rows)

    def safe_goal_verifications(self, task_id: str) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT record_json FROM android_ui_goal_verifications WHERE task_id=? ORDER BY revision,step_index", (task_id,)).fetchall()
        return tuple(_safe_verification(json.loads(row["record_json"])) for row in rows)

    def structural_goal_verifications(self, task_id: str) -> tuple[dict[str, Any], ...]:
        """Expose only K2 step indexes and structural UI-tree digests for K3 migration."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM android_ui_goal_verifications WHERE task_id=? ORDER BY revision,step_index",
                (task_id,),
            ).fetchall()
        return tuple(_structural_verification(json.loads(row["record_json"])) for row in rows)

    def immutable_goal_verification(
        self, *, task_id: str, revision: int, step_index: int,
    ) -> dict[str, Any] | None:
        """Return one exact private K2 row to the in-process K3 attestor.

        This is intentionally not a renderer/public projection.  The durable
        record already excludes owner ids, raw UI text, artifact paths and
        typed input; callers must still revalidate its canonical binding and
        record identity before issuing an attestation.
        """

        if revision < 1 or step_index < 0:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """SELECT record_json FROM android_ui_goal_verifications
                WHERE task_id=? AND revision=? AND step_index=?""",
                (task_id, revision, step_index),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["record_json"]))
        return value if isinstance(value, dict) else None

    def trusted_terminal_for(
        self, snapshot: CanonicalSnapshot, criteria: CriteriaRevision, *,
        runner_kind: str, runner_version: int,
        model_version: str | None = None,
        prompt_version: str | None = None,
    ) -> TrustedTerminalVerification | None:
        """Exact, private, read-only terminal replay lookup for K3/K4.

        It validates all durable scope/binding fields against the caller's
        canonical snapshot.  A mismatch returns ``None`` rather than leaking a
        neighbouring owner/task record or inventing a terminal result.
        """
        if (
            snapshot.runner_kind != runner_kind or snapshot.runner_version != runner_version
            or snapshot.profile_id is None or snapshot.profile_generation is None
            or snapshot.boot_id is None or snapshot.canonical_device_id is None
            or snapshot.runner_binding_id is None
        ):
            return None
        try:
            if self.next_unmet_waypoint(snapshot, criteria) is not None:
                return None
        except (OSError, sqlite3.Error, ValueError):
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT record_json, step_index FROM android_ui_goal_verifications WHERE task_id=? AND revision=? ORDER BY step_index DESC LIMIT 1", (snapshot.task_id, snapshot.revision)).fetchone()
            criteria_row = connection.execute(
                """SELECT digest, criteria_json, goal_digest, coverage_digest, waypoints_json
                FROM android_ui_criteria WHERE task_id=? AND revision=?""",
                (snapshot.task_id, snapshot.revision),
            ).fetchone()
            step = (
                connection.execute(
                    "SELECT * FROM android_ui_steps WHERE task_id=? AND step_index=?",
                    (snapshot.task_id, int(row["step_index"])),
                ).fetchone()
                if row is not None else None
            )
            latest = connection.execute(
                """SELECT MAX(step_index) AS latest FROM android_ui_steps
                WHERE task_id=? AND revision=?""",
                (snapshot.task_id, snapshot.revision),
            ).fetchone()
        if row is None or criteria_row is None or step is None:
            return None
        value = json.loads(row["record_json"])
        # Only a fully evidenced semantic success is terminal.  Unknown and
        # unsatisfied per-step records are durable audit checkpoints, but the
        # next wake must be free to allocate a later step instead of replaying
        # them as top-level Task completion.
        if value.get("overall") != "satisfied":
            return None
        after = value.get("after") or {}
        grounding = _resolve_trusted_grounding(
            self._grounding,
            _step_grounding_query(step),
        )
        expected = {
            "task_id": snapshot.task_id, "revision": snapshot.revision,
            "criteria_digest": criteria.digest, "owner_scope_digest": owner_scope_digest(snapshot.owner),
            "goal_digest": criteria.goal_digest, "coverage_digest": criteria.coverage_digest,
            "runner_kind": runner_kind, "runner_version": runner_version,
            "runner_binding_digest": opaque_digest("runner-binding", snapshot.runner_binding_id),
            "profile_id_digest": opaque_digest("profile-id", snapshot.profile_id),
            "profile_generation": snapshot.profile_generation,
            "boot_id_digest": opaque_digest("boot-id", snapshot.boot_id),
            "canonical_device_id_digest": opaque_digest(
                "canonical-device-id", snapshot.canonical_device_id,
            ),
        }
        actual = {
            "task_id": value.get("task_id"), "revision": value.get("revision"),
            "criteria_digest": value.get("criteria_digest"), "owner_scope_digest": value.get("owner_scope_digest"),
            "goal_digest": value.get("goal_digest"), "coverage_digest": value.get("coverage_digest"),
            "runner_kind": value.get("runner_kind"), "runner_version": value.get("runner_version"),
            "runner_binding_digest": value.get("runner_binding_digest"),
            "profile_id_digest": after.get("profile_id_digest"),
            "profile_generation": after.get("profile_generation"),
            "boot_id_digest": after.get("boot_id_digest"),
            "canonical_device_id_digest": after.get("canonical_device_id_digest"),
        }
        if (
            model_version is not None
            and value.get("model_version") != opaque_digest("model-version", model_version)
        ) or (
            prompt_version is not None
            and value.get("prompt_version") != opaque_digest("prompt-version", prompt_version)
        ):
            return None
        if (
            actual != expected
            or step["binding_json"] != _binding_json(snapshot)
            or grounding is None
            or value.get("grounding_digest") != grounding.grounding_digest
            or not _durable_terminal_matches_ledger(
                value=value,
                criteria_row=criteria_row,
                step_row=step,
                latest_step_index=int(latest["latest"]),
                grounding=grounding,
            )
        ):
            return None
        return TrustedTerminalVerification(
            verification_ref=str(value.get("verification_ref", "")), overall=str(value.get("overall", "unknown")),
            task_id=snapshot.task_id, revision=snapshot.revision, step_index=int(value.get("latest_step_index", -1)),
            criteria_digest=criteria.digest, owner_scope_digest=owner_scope_digest(snapshot.owner),
            profile_id=snapshot.profile_id, profile_generation=snapshot.profile_generation,
            boot_id=snapshot.boot_id, canonical_device_id=snapshot.canonical_device_id,
            runner_kind=runner_kind, runner_version=runner_version,
            runner_binding_digest=opaque_digest("runner-binding", snapshot.runner_binding_id),
            goal_digest=criteria.goal_digest, coverage_digest=criteria.coverage_digest,
            before_observation_digest=_durable_observation_digest(value.get("before")),
            after_observation_digest=_durable_observation_digest(value.get("after")) or "",
            artifact_digest=str(value.get("artifact_digest", "")),
            grounding_digest=str(value.get("grounding_digest", "")),
            causal_command_id=(
                str(value["causal_command_id"])
                if value.get("causal_command_id") is not None else None
            ),
            primitive_outcome=str(value.get("primitive_outcome", "")),
            model_version_digest=str(value.get("model_version", "")),
            prompt_version_digest=str(value.get("prompt_version", "")),
        )

    def checkpoint_attestation_port(self) -> CheckpointBaselineAttestationPort:
        """Return the K2-owned, read-only baseline authority."""

        return self

    def grounding_facts(
        self, query: ObservationGroundingQuery,
    ) -> dict[str, Any] | None:
        """Return the exact text-free durable facts behind a grounding query.

        This is the narrow K4 composition seam.  It does not accept a caller
        observation or expose UI content: the query must be re-derived from
        the latest K2 step for the current revision, and only the digest/binding
        fields already persisted by K2 are returned.
        """

        if not isinstance(query, ObservationGroundingQuery):
            return None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT * FROM android_ui_steps
                    WHERE task_id=? AND revision=? AND step_index=?""",
                    (query.task_id, query.revision, query.step_index),
                ).fetchone()
                latest = connection.execute(
                    """SELECT MAX(step_index) AS latest FROM android_ui_steps
                    WHERE task_id=? AND revision=?""",
                    (query.task_id, query.revision),
                ).fetchone()
            if (
                row is None
                or latest is None
                or latest["latest"] is None
                or int(latest["latest"]) != query.step_index
                or _step_grounding_query(row) != query
            ):
                return None
            observation_json = (
                row["after_json"]
                if row["after_json"] is not None
                else row["before_json"]
            )
            value = json.loads(str(observation_json))
            if not isinstance(value, dict):
                return None
            return dict(value)
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            return None

    def attest(
        self, query: CheckpointBaselineQuery,
    ) -> TrustedCheckpointBaselineAttestation | None:
        """Derive a checkpoint from K2 rows; never accept a caller baseline."""

        if not isinstance(query, CheckpointBaselineQuery):
            return None
        snapshot = query.snapshot
        observation = query.observation
        if (
            snapshot.runner_kind != query.runner_kind
            or snapshot.runner_version != query.runner_version
            or snapshot.runner_binding_id is None
            or snapshot.profile_id is None
            or snapshot.profile_generation is None
            or snapshot.boot_id is None
            or snapshot.canonical_device_id is None
            or query.criteria.revision != snapshot.revision
            or observation.task_id != snapshot.task_id
            or observation.profile_id != snapshot.profile_id
            or observation.profile_generation != snapshot.profile_generation
            or observation.boot_id != snapshot.boot_id
            or observation.canonical_device_id != snapshot.canonical_device_id
        ):
            return None
        encoded_observation = _observation_json(observation)
        try:
            loaded = self.load_criteria(snapshot.task_id, snapshot.revision)
        except (OSError, sqlite3.Error, ValueError):
            return None
        if loaded != query.criteria:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM android_ui_steps
                WHERE task_id=? AND revision=?
                  AND (after_json=? OR before_json=?)
                ORDER BY CASE WHEN after_json=? THEN 0 ELSE 1 END, step_index DESC
                LIMIT 1""",
                (
                    snapshot.task_id, snapshot.revision,
                    encoded_observation, encoded_observation,
                    encoded_observation,
                ),
            ).fetchone()
            if row is None or row["binding_json"] != _binding_json(snapshot):
                return None
            causal_command_id = observation.causality_command_id
            if row["after_json"] == encoded_observation:
                claim = json.loads(str(row["claim_json"])) if row["claim_json"] else None
                if (
                    not isinstance(claim, dict)
                    or claim.get("claim_id") != causal_command_id
                    or row["primitive_json"] is None
                ):
                    return None
            elif causal_command_id is not None:
                prior = connection.execute(
                    """SELECT claim_json, primitive_json FROM android_ui_steps
                    WHERE task_id=? AND revision=? AND step_index<? AND after_json=?
                    ORDER BY step_index DESC LIMIT 1""",
                    (
                        snapshot.task_id, snapshot.revision,
                        int(row["step_index"]), encoded_observation,
                    ),
                ).fetchone()
                claim = (
                    json.loads(str(prior["claim_json"]))
                    if prior is not None and prior["claim_json"] else None
                )
                if (
                    prior is None
                    or prior["primitive_json"] is None
                    or not isinstance(claim, dict)
                    or claim.get("claim_id") != causal_command_id
                ):
                    return None
        return TrustedCheckpointBaselineAttestation(
            task_id=snapshot.task_id,
            owner_scope_digest=owner_scope_digest(snapshot.owner),
            runner_kind=query.runner_kind,
            runner_version=query.runner_version,
            runner_binding_digest=opaque_digest(
                "runner-binding", snapshot.runner_binding_id,
            ),
            revision=snapshot.revision,
            criteria_digest=query.criteria.digest,
            checkpoint_index=int(row["step_index"]) + 1,
            observation_digest=opaque_digest(
                "checkpoint-observation", encoded_observation,
            ),
            causal_command_id=causal_command_id,
            _seal=self._checkpoint_attestation_seal,
        )

    def validates(
        self, attestation: TrustedCheckpointBaselineAttestation,
    ) -> bool:
        return (
            isinstance(attestation, TrustedCheckpointBaselineAttestation)
            and attestation._seal is self._checkpoint_attestation_seal
            and attestation.checkpoint_index >= 1
            and bool(attestation.task_id)
            and bool(attestation.owner_scope_digest)
            and bool(attestation.runner_binding_digest)
            and bool(attestation.criteria_digest)
            and bool(attestation.observation_digest)
        )

    def validates_for(
        self,
        query: CheckpointBaselineQuery,
        attestation: TrustedCheckpointBaselineAttestation,
    ) -> bool:
        """Validate one sealed baseline against the exact K2-owned query rows."""

        try:
            if (
                not _checkpoint_query_is_well_formed(query)
                or not _checkpoint_attestation_is_well_formed(attestation)
                or not self.validates(attestation)
            ):
                return False
            expected = self.attest(query)
            return expected is not None and attestation == expected
        except Exception:
            # This is a cross-package trust boundary.  Malformed values and an
            # unavailable/corrupt store are cold failures, never caller errors.
            return False


def _checkpoint_query_is_well_formed(query: object) -> bool:
    if type(query) is not CheckpointBaselineQuery:
        return False
    snapshot = query.snapshot
    criteria = query.criteria
    observation = query.observation
    if (
        type(snapshot) is not CanonicalSnapshot
        or type(criteria) is not CriteriaRevision
        or type(observation) is not ObservationEnvelope
        or type(snapshot.owner) is not OwnerBinding
        or type(snapshot.revision) is not int
        or snapshot.revision < 1
        or type(snapshot.profile_generation) is not int
        or snapshot.profile_generation < 1
        or type(snapshot.runner_version) is not int
        or snapshot.runner_version < 1
        or type(criteria.revision) is not int
        or criteria.revision < 1
        or type(observation.profile_generation) is not int
        or observation.profile_generation < 1
        or type(query.runner_version) is not int
        or query.runner_version < 1
    ):
        return False
    opaque_values = (
        snapshot.task_id,
        snapshot.owner.principal_id,
        snapshot.owner.controller_id,
        snapshot.profile_id,
        snapshot.boot_id,
        snapshot.runner_kind,
        snapshot.runner_binding_id,
        query.runner_kind,
        observation.task_id,
        observation.profile_id,
        observation.boot_id,
        observation.freshness_token,
    )
    digest_values = (
        criteria.digest,
        observation.screenshot_digest,
        observation.device_state_digest,
    )
    return (
        all(type(value) is str and _OPAQUE.fullmatch(value) for value in opaque_values)
        and all(
            type(value) is str and _BOUND_IDENTIFIER.fullmatch(value)
            for value in (
                snapshot.canonical_device_id,
                observation.canonical_device_id,
            )
        )
        and all(type(value) is str and _DIGEST.fullmatch(value) for value in digest_values)
        and (
            observation.ui_tree_digest is None
            or (
                type(observation.ui_tree_digest) is str
                and _DIGEST.fullmatch(observation.ui_tree_digest)
            )
        )
        and (
            observation.causality_command_id is None
            or (
                type(observation.causality_command_id) is str
                and _OPAQUE.fullmatch(observation.causality_command_id)
            )
        )
    )


def _checkpoint_attestation_is_well_formed(attestation: object) -> bool:
    if type(attestation) is not TrustedCheckpointBaselineAttestation:
        return False
    return (
        type(attestation.task_id) is str
        and bool(_OPAQUE.fullmatch(attestation.task_id))
        and type(attestation.runner_kind) is str
        and bool(_OPAQUE.fullmatch(attestation.runner_kind))
        and type(attestation.runner_version) is int
        and attestation.runner_version >= 1
        and type(attestation.revision) is int
        and attestation.revision >= 1
        and type(attestation.checkpoint_index) is int
        and attestation.checkpoint_index >= 1
        and all(
            type(value) is str and bool(_DIGEST.fullmatch(value))
            for value in (
                attestation.owner_scope_digest,
                attestation.runner_binding_digest,
                attestation.criteria_digest,
                attestation.observation_digest,
            )
        )
        and (
            attestation.causal_command_id is None
            or (
                type(attestation.causal_command_id) is str
                and bool(_OPAQUE.fullmatch(attestation.causal_command_id))
            )
        )
    )


def _binding_json(snapshot: CanonicalSnapshot) -> str:
    if (
        snapshot.runner_kind is None
        or snapshot.runner_version is None
        or snapshot.runner_binding_id is None
        or snapshot.profile_id is None
        or snapshot.profile_generation is None
        or snapshot.boot_id is None
        or snapshot.canonical_device_id is None
    ):
        raise ValueError("canonical step binding is incomplete")
    return json.dumps({
        "task_id": snapshot.task_id,
        "owner_scope_digest": owner_scope_digest(snapshot.owner),
        "revision": snapshot.revision,
        "runner_kind": snapshot.runner_kind,
        "runner_version": snapshot.runner_version,
        "runner_binding_digest": opaque_digest(
            "runner-binding", snapshot.runner_binding_id,
        ),
        "profile_id_digest": opaque_digest("profile-id", snapshot.profile_id),
        "profile_generation": snapshot.profile_generation,
        "boot_id_digest": opaque_digest("boot-id", snapshot.boot_id),
        "canonical_device_id_digest": opaque_digest(
            "canonical-device-id", snapshot.canonical_device_id,
        ),
    }, ensure_ascii=False, sort_keys=True)


def _record_binding_json(record: GoalVerificationRecord) -> str:
    if record.runner_binding_id is None:
        raise ValueError("verification runner binding is incomplete")
    return json.dumps({
        "task_id": record.task_id,
        "owner_scope_digest": owner_scope_digest(record.owner),
        "revision": record.revision,
        "runner_kind": record.runner_kind,
        "runner_version": record.runner_version,
        "runner_binding_digest": opaque_digest(
            "runner-binding", record.runner_binding_id,
        ),
        "profile_id_digest": opaque_digest("profile-id", record.after.profile_id),
        "profile_generation": record.after.profile_generation,
        "boot_id_digest": opaque_digest("boot-id", record.after.boot_id),
        "canonical_device_id_digest": opaque_digest(
            "canonical-device-id", record.after.canonical_device_id,
        ),
    }, ensure_ascii=False, sort_keys=True)


def _criteria_from_row(row: sqlite3.Row) -> tuple[Criterion, ...]:
    try:
        payload = _strict_json_loads(str(row["criteria_json"]))
        waypoints = _waypoints_from_payload(
            _strict_json_loads(str(row["waypoints_json"]))
            if "waypoints_json" in row.keys() else [],
        )
        identity_scheme = _criteria_identity_scheme_from_payload(
            payload,
            stored_digest=str(row["digest"]),
            goal_digest=row["goal_digest"],
            coverage_digest=row["coverage_digest"],
            waypoints=waypoints,
        )
        criteria = _criteria_from_payload_items(payload, identity_scheme)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("stored verification criteria are invalid") from error
    if (
        not criteria
        or len({item.criterion_id for item in criteria}) != len(criteria)
        or _criteria_digest_from_payload(
            payload,
            goal_digest=row["goal_digest"],
            coverage_digest=row["coverage_digest"],
            waypoints=waypoints,
            identity_scheme=identity_scheme,
        ) != row["digest"]
    ):
        raise ValueError("stored verification criteria digest is invalid")
    return criteria


def _criteria_payload(criteria: tuple[Criterion, ...]) -> str:
    return json.dumps([
        {
            "criterion_id": item.criterion_id,
            "description": "[redacted]",
            "description_digest": criterion_description_digest(item),
            "required_evidence_markers": item.required_evidence_markers,
        }
        for item in criteria
    ], ensure_ascii=False, sort_keys=True)


def _waypoint_proof_record(record: GoalVerificationRecord) -> dict[str, Any]:
    """Text-free subset needed to revalidate an ordered prerequisite."""

    durable = record.private_durable_record()
    return {
        key: durable.get(key)
        for key in (
            "task_id", "runner_kind", "runner_version", "revision",
            "criteria_digest", "latest_step_index", "goal_digest",
            "coverage_digest", "overall", "already_satisfied", "verdicts",
            "verification_ref", "owner_scope_digest", "runner_binding_digest",
            "model_version", "prompt_version", "artifact_digest",
            "grounding_digest", "causal_command_id", "primitive_outcome",
            "before", "after",
        )
    }


_CURRENT_CRITERION_KEYS = frozenset({
    "criterion_id", "description", "description_digest",
    "required_evidence_markers",
})
_LEGACY_CRITERION_KEYS = frozenset({
    "criterion_id", "description", "required_evidence_markers",
})
_WAYPOINT_KEYS = frozenset({
    "waypoint_id", "ordinal", "source_digest", "digest", "criteria",
})


def _strict_json_loads(encoded: str) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("stored JSON object has duplicate keys")
            result[key] = value
        return result
    return json.loads(encoded, object_pairs_hook=unique_object)


def _criteria_from_payload_items(
    payload: object, identity_scheme: str,
) -> tuple[Criterion, ...]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("stored criteria payload is invalid")
    expected = (
        _CURRENT_CRITERION_KEYS
        if identity_scheme == CRITERIA_IDENTITY_CURRENT
        else _LEGACY_CRITERION_KEYS
    )
    criteria: list[Criterion] = []
    for item in payload:
        if not isinstance(item, Mapping) or set(item) != expected:
            raise ValueError("stored criterion key set is invalid")
        criterion_id = item.get("criterion_id")
        description = item.get("description")
        markers = item.get("required_evidence_markers")
        description_digest = item.get("description_digest")
        if (
            not isinstance(criterion_id, str)
            or not isinstance(description, str)
            or not isinstance(markers, list)
            or any(not isinstance(marker, str) for marker in markers)
            or (
                identity_scheme == CRITERIA_IDENTITY_CURRENT
                and (
                    description != "[redacted]"
                    or not isinstance(description_digest, str)
                )
            )
            or (
                identity_scheme != CRITERIA_IDENTITY_CURRENT
                and description_digest is not None
            )
        ):
            raise ValueError("stored criterion identity is invalid")
        try:
            criteria.append(Criterion(
                criterion_id, description, tuple(markers), description_digest,
            ))
        except ValueError as error:
            raise ValueError("stored criterion identity is invalid") from error
    return tuple(criteria)


def _criteria_digest_from_payload(
    payload: object, *, goal_digest: str | None, coverage_digest: str | None,
    waypoints: tuple[OrderedWaypoint, ...] = (),
    identity_scheme: str = CRITERIA_IDENTITY_CURRENT,
) -> str:
    criteria = _criteria_from_payload_items(payload, identity_scheme)
    return criteria_digest(
        criteria,
        goal_digest=goal_digest,
        coverage_digest=coverage_digest,
        waypoints=waypoints,
        identity_scheme=identity_scheme,
    )


def _criteria_identity_scheme_from_payload(
    payload: object, *, stored_digest: str, goal_digest: str | None,
    coverage_digest: str | None, waypoints: tuple[OrderedWaypoint, ...] = (),
) -> str:
    """Select exactly one historical identity contract for an immutable row."""

    if not isinstance(payload, list) or not payload:
        raise ValueError("stored criteria payload is invalid")
    key_sets = tuple(
        frozenset(item) if isinstance(item, Mapping) else frozenset()
        for item in payload
    )
    if all(keys == _CURRENT_CRITERION_KEYS for keys in key_sets):
        candidates = (CRITERIA_IDENTITY_CURRENT,)
    elif all(keys == _LEGACY_CRITERION_KEYS for keys in key_sets):
        candidates = (
            CRITERIA_IDENTITY_LEGACY_HASHED,
            CRITERIA_IDENTITY_LEGACY_RAW,
        )
    else:
        raise ValueError("stored criterion key set is invalid")
    matches = tuple(
        scheme for scheme in candidates
        if _criteria_digest_from_payload(
            payload,
            goal_digest=goal_digest,
            coverage_digest=coverage_digest,
            waypoints=waypoints,
            identity_scheme=scheme,
        ) == stored_digest
    )
    if len(matches) != 1:
        raise ValueError("stored criteria digest scheme is unknown")
    return matches[0]


def _waypoint_digest_projection(
    waypoints: tuple[OrderedWaypoint, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "waypoint_id": item.waypoint_id,
            "ordinal": item.ordinal,
            "source_digest": item.source_digest,
            "criteria_digest": item.digest,
        }
        for item in waypoints
    ]


def _waypoints_payload(
    waypoints: tuple[OrderedWaypoint, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "waypoint_id": item.waypoint_id,
            "ordinal": item.ordinal,
            "source_digest": item.source_digest,
            "digest": item.digest,
            "criteria": [
                {
                    "criterion_id": criterion.criterion_id,
                    "description": "[redacted]",
                    "description_digest": criterion_description_digest(criterion),
                    "required_evidence_markers": criterion.required_evidence_markers,
                }
                for criterion in item.criteria
            ],
        }
        for item in waypoints
    ]


def _waypoints_from_payload(value: object) -> tuple[OrderedWaypoint, ...]:
    if not isinstance(value, list):
        raise ValueError("stored waypoint payload is invalid")
    values: list[OrderedWaypoint] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _WAYPOINT_KEYS:
            raise ValueError("stored waypoint is invalid")
        raw_criteria = item.get("criteria")
        if not isinstance(raw_criteria, list):
            raise ValueError("stored waypoint criteria are invalid")
        criteria = _criteria_from_payload_items(
            raw_criteria, CRITERIA_IDENTITY_CURRENT,
        )
        if (
            not isinstance(item.get("waypoint_id"), str)
            or isinstance(item.get("ordinal"), bool)
            or not isinstance(item.get("ordinal"), int)
            or not isinstance(item.get("source_digest"), str)
            or not isinstance(item.get("digest"), str)
        ):
            raise ValueError("stored waypoint identity is invalid")
        values.append(OrderedWaypoint(
            item["waypoint_id"], item["ordinal"], item["source_digest"],
            criteria, item["digest"],
        ))
    return tuple(values)


def _criteria_identity_matches(
    left: CriteriaRevision, right: CriteriaRevision,
) -> bool:
    return (
        left.revision == right.revision
        and left.digest == right.digest
        and left.goal_digest == right.goal_digest
        and left.coverage_digest == right.coverage_digest
        and left.waypoints == right.waypoints
        and left.identity_scheme == right.identity_scheme
        and tuple(
            (item.criterion_id, item.required_evidence_markers)
            for item in left.criteria
        ) == tuple(
            (item.criterion_id, item.required_evidence_markers)
            for item in right.criteria
        )
    )


def _expected_overall(states: tuple[str, ...]) -> str:
    if states and all(state == "satisfied" for state in states):
        return "satisfied"
    if any(state == "unknown" for state in states):
        return "unknown"
    return "unsatisfied"


def _validate_verification_against_ledger(
    *, record: GoalVerificationRecord, criteria_row: sqlite3.Row,
    step_row: sqlite3.Row, latest_step_index: int,
    grounding: TrustedObservationGroundingManifest | None,
    has_prior_effect: bool,
    causal_predecessor: bool,
) -> None:
    criteria = _criteria_from_row(criteria_row)
    try:
        decision = json.loads(str(step_row["decision_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("verification step has no bounded decision") from error
    if record.latest_step_index != latest_step_index:
        raise ValueError("android_ui_verification_latest_step_mismatch")
    if criteria_row["digest"] != record.criteria_digest:
        raise ValueError("android_ui_verification_criteria_digest_mismatch")
    if criteria_row["goal_digest"] != record.goal_digest:
        raise ValueError("android_ui_verification_goal_digest_mismatch")
    if criteria_row["coverage_digest"] != record.coverage_digest:
        raise ValueError("android_ui_verification_coverage_digest_mismatch")
    if step_row["binding_json"] != _record_binding_json(record):
        raise ValueError("android_ui_verification_binding_mismatch")
    if record.artifact_digest != observation_artifact_digest(record.after):
        raise ValueError("android_ui_verification_artifact_mismatch")
    if not record.model_version or not record.prompt_version:
        raise ValueError("android_ui_verification_model_binding_missing")
    if step_row["before_json"] != _observation_json(record.before):
        raise ValueError("verification before-observation is not durable")
    if record.already_satisfied:
        if (
            not isinstance(decision, dict)
            or decision.get("kind") != "terminal_candidate"
            or record.before is None
            or record.before != record.after
            or step_row["after_json"] is not None
            or step_row["action_intent_id"] is not None
            or step_row["claim_json"] is not None
            or step_row["primitive_json"] is not None
            or record.causal_command_id is not None
            or record.primitive_outcome != "already_satisfied"
            or (has_prior_effect and not causal_predecessor)
            or (
                not has_prior_effect
                and record.before.causality_command_id is not None
            )
        ):
            raise ValueError("already-satisfied verification has effect evidence")
    else:
        claim = (
            json.loads(str(step_row["claim_json"]))
            if step_row["claim_json"] else None
        )
        primitive = (
            json.loads(str(step_row["primitive_json"]))
            if step_row["primitive_json"] else None
        )
        if (
            not isinstance(decision, dict)
            or decision.get("kind") != "action"
            or record.before is None
            or record.before.freshness_token == record.after.freshness_token
            or step_row["after_json"] != _observation_json(record.after)
            or step_row["action_intent_id"] is None
            or not isinstance(claim, dict)
            or not isinstance(primitive, dict)
            or claim.get("claim_id") != record.causal_command_id
            or record.after.causality_command_id != record.causal_command_id
            or primitive.get("command_id") != record.causal_command_id
            or primitive.get("outcome") != record.primitive_outcome
            or (record.overall == "satisfied" and record.primitive_outcome == "uncertain")
        ):
            raise ValueError("verification lacks causal primitive evidence")
    criterion_by_id = {item.criterion_id: item for item in criteria}
    verdict_ids = tuple(item.criterion_id for item in record.verdicts)
    if (
        len(verdict_ids) != len(set(verdict_ids))
        or set(verdict_ids) != set(criterion_by_id)
        or any(item.state not in {"satisfied", "unsatisfied", "unknown"} for item in record.verdicts)
        or record.overall != _expected_overall(
            tuple(item.state for item in record.verdicts),
        )
    ):
        raise ValueError("verification verdicts do not cover frozen criteria")
    if record.overall == "satisfied" and (
        grounding is None
        or record.grounding_digest != grounding.grounding_digest
    ):
        raise ValueError("satisfied verification lacks trusted artifact grounding")
    nodes = {
        item.node_id: item for item in (grounding.nodes if grounding is not None else ())
    }
    for verdict in record.verdicts:
        criterion = criterion_by_id[verdict.criterion_id]
        if verdict.state == "satisfied" and (
            not criterion.required_evidence_markers or not verdict.anchors
        ):
            raise ValueError("satisfied criterion has no durable evidence contract")
        for anchor in verdict.anchors:
            if anchor.observation_freshness_token != record.after.freshness_token:
                raise ValueError("verification anchor is stale")
            if verdict.state != "satisfied":
                continue
            node = nodes.get(anchor.node_id or "")
            if (
                anchor.kind != "ui_node"
                or node is None
                or node.semantic_kind not in {"page_title", "container", "state"}
                or (node.clickable and node.semantic_kind == "navigation")
                or anchor.semantic_marker != node.semantic_marker
                or anchor.semantic_marker not in criterion.required_evidence_markers
                or (anchor.bounds is not None and anchor.bounds != node.bounds)
            ):
                raise ValueError("verification anchor is not independently grounded")


def _latest_prior_effect(
    connection: sqlite3.Connection, step_row: sqlite3.Row,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM android_ui_steps
        WHERE task_id=? AND revision=? AND step_index<?
        AND action_intent_id IS NOT NULL
        ORDER BY step_index DESC LIMIT 1""",
        (
            step_row["task_id"], step_row["revision"],
            step_row["step_index"],
        ),
    ).fetchone()


def _prior_effect_causes_observation(
    prior: sqlite3.Row, current: sqlite3.Row,
    observation: ObservationEnvelope | None,
) -> bool:
    if observation is None or observation.causality_command_id is None:
        return False
    try:
        claim = json.loads(str(prior["claim_json"]))
        after = json.loads(str(prior["after_json"]))
        primitive = json.loads(str(prior["primitive_json"]))
        command_id = observation.causality_command_id
        return (
            int(prior["completed"]) == 1
            and prior["binding_json"] == current["binding_json"]
            and isinstance(claim, dict)
            and isinstance(after, dict)
            and isinstance(primitive, dict)
            and claim.get("claim_id") == command_id
            and after.get("causality_command_id") == command_id
            and primitive.get("command_id") == command_id
            and primitive.get("outcome") in {"progress", "no_progress"}
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _artifact_digest_from_durable(observation: Mapping[str, Any]) -> str:
    payload = json.dumps([
        "observation-artifacts",
        str(observation.get("screenshot_digest")),
        str(observation.get("ui_tree_digest") or "unavailable"),
        str(observation.get("device_state_digest")),
    ], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _durable_observation_digest(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return opaque_digest("durable-observation", encoded)


def _durable_terminal_matches_ledger(
    *, value: Mapping[str, Any], criteria_row: sqlite3.Row,
    step_row: sqlite3.Row, latest_step_index: int,
    grounding: TrustedObservationGroundingManifest,
) -> bool:
    try:
        criteria = _criteria_from_row(criteria_row)
        verdicts = value.get("verdicts")
        before = value.get("before")
        after = value.get("after")
        if not isinstance(verdicts, list) or not isinstance(after, Mapping):
            return False
        expected_ids = {item.criterion_id for item in criteria}
        criteria_by_id = {item.criterion_id: item for item in criteria}
        actual_ids = {
            item.get("criterion_id") for item in verdicts if isinstance(item, Mapping)
        }
        grounded_nodes = {item.node_id: item for item in grounding.nodes}
        if (
            int(value.get("latest_step_index", -1)) != latest_step_index
            or int(step_row["step_index"]) != latest_step_index
            or value.get("criteria_digest") != criteria_row["digest"]
            or value.get("goal_digest") != criteria_row["goal_digest"]
            or value.get("coverage_digest") != criteria_row["coverage_digest"]
            or actual_ids != expected_ids
            or len(verdicts) != len(expected_ids)
            or any(
                not isinstance(item, Mapping) or item.get("state") != "satisfied"
                or not item.get("anchors")
                for item in verdicts
            )
            or value.get("artifact_digest") != _artifact_digest_from_durable(after)
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("grounding_digest", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("model_version", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("prompt_version", "")))
            or step_row["binding_json"] is None
        ):
            return False
        step_before = json.loads(str(step_row["before_json"]))
        decision = json.loads(str(step_row["decision_json"]))
        if step_before != before:
            return False
        for item in verdicts:
            criterion = criteria_by_id[str(item["criterion_id"])]
            anchors = item.get("anchors")
            if not isinstance(anchors, list) or any(
                not isinstance(anchor, Mapping)
                or anchor.get("freshness") != after.get("freshness_digest")
                or anchor.get("semantic_marker") not in criterion.required_evidence_markers
                or anchor.get("kind") != "ui_node"
                or anchor.get("node_id") not in grounded_nodes
                or grounded_nodes[str(anchor.get("node_id"))].semantic_kind
                not in {"page_title", "container", "state"}
                or anchor.get("semantic_marker")
                != grounded_nodes[str(anchor.get("node_id"))].semantic_marker
                or (
                    anchor.get("bounds") is not None
                    and tuple(anchor.get("bounds"))
                    != grounded_nodes[str(anchor.get("node_id"))].bounds
                )
                for anchor in anchors
            ):
                return False
        if value.get("already_satisfied") is True:
            return (
                isinstance(decision, dict)
                and decision.get("kind") == "terminal_candidate"
                and before == after
                and step_row["after_json"] is None
                and step_row["action_intent_id"] is None
                and step_row["claim_json"] is None
                and step_row["primitive_json"] is None
                and value.get("causal_command_id") is None
                and value.get("primitive_outcome") == "already_satisfied"
            )
        claim = json.loads(str(step_row["claim_json"]))
        primitive = json.loads(str(step_row["primitive_json"]))
        changed = any(
            before.get(key) != after.get(key)
            for key in (
                "screenshot_digest", "ui_tree_digest", "device_state_digest",
            )
        )
        return (
            isinstance(decision, dict)
            and decision.get("kind") == "action"
            and json.loads(str(step_row["after_json"])) == after
            and step_row["action_intent_id"] is not None
            and claim.get("claim_id") == value.get("causal_command_id")
            and after.get("causality_command_id") == value.get("causal_command_id")
            and primitive.get("command_id") == value.get("causal_command_id")
            and primitive.get("outcome") == value.get("primitive_outcome")
            and primitive.get("outcome") != "uncertain"
            and (
                (primitive.get("outcome") == "progress" and changed)
                or (primitive.get("outcome") == "no_progress" and not changed)
            )
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _resolve_trusted_grounding(
    port: ObservationArtifactGroundingPort | None,
    query: ObservationGroundingQuery | None,
) -> TrustedObservationGroundingManifest | None:
    if port is None or query is None:
        return None
    manifest = port.resolve(query)
    if manifest is None or manifest.query != query or not port.validates(manifest):
        return None
    return manifest


def _step_grounding_query(step_row: sqlite3.Row) -> ObservationGroundingQuery | None:
    """Derive grounding identity only from the immutable durable step ledger.

    The verifier's in-memory ``record.after`` and UI-node projection are not
    authority at the store boundary.  An effect step uses its durable after
    observation; an already-satisfied zero-effect step uses its durable before
    observation.  The exact Task/owner/runner binding comes from the binding
    row written when the step was reserved.
    """

    try:
        binding = json.loads(str(step_row["binding_json"]))
        observation_json = (
            step_row["after_json"]
            if step_row["after_json"] is not None
            else step_row["before_json"]
        )
        observation = json.loads(str(observation_json))
        if not isinstance(binding, Mapping) or not isinstance(observation, Mapping):
            return None
        ui_tree_digest = observation.get("ui_tree_digest")
        freshness_digest = observation.get("freshness_digest")
        if not isinstance(ui_tree_digest, str) or not isinstance(freshness_digest, str):
            return None
        after_digest = _durable_observation_digest(observation)
        if after_digest is None:
            return None
        return ObservationGroundingQuery(
            task_id=str(binding["task_id"]),
            owner_scope_digest=str(binding["owner_scope_digest"]),
            runner_kind=str(binding["runner_kind"]),
            runner_version=int(binding["runner_version"]),
            runner_binding_digest=str(binding["runner_binding_digest"]),
            revision=int(binding["revision"]),
            step_index=int(step_row["step_index"]),
            after_observation_digest=after_digest,
            artifact_digest=_artifact_digest_from_durable(observation),
            ui_tree_artifact_digest=ui_tree_digest,
            freshness_digest=freshness_digest,
        )
    except (
        KeyError, TypeError, ValueError, json.JSONDecodeError,
    ):
        return None


def _require_step(connection: sqlite3.Connection, reservation: StepReservation) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM android_ui_steps WHERE task_id=? AND step_index=?", (reservation.task_id, reservation.step_index)).fetchone()
    if row is None or int(row["revision"]) != reservation.revision or int(row["completed"]):
        raise ValueError("reserved step is no longer current")
    return row


def _reservation_from_row(row: sqlite3.Row) -> StepReservation:
    action = None
    payload_source = row["intent_json"]
    if payload_source is None and row["decision_json"]:
        decision = json.loads(row["decision_json"])
        payload_source = json.dumps(decision["action"]) if decision.get("action") else None
    if payload_source:
        payload = json.loads(payload_source)
        if payload.get("action") != "input_text":
            action = AndroidUiAction(payload["action"], {key: value for key, value in payload.items() if key not in {"action", "redacted", "text_length", "text_digest"}})
    decision = json.loads(row["decision_json"]) if row["decision_json"] else {}
    decision_kind = decision.get("kind")
    selected_hint_id = decision.get("selected_hint_id")
    if selected_hint_id is not None and not _OPAQUE.fullmatch(selected_hint_id):
        raise ValueError("stored selected hint is invalid")
    claim = json.loads(row["claim_json"])["claim_id"] if row["claim_json"] else None
    return StepReservation(row["task_id"], int(row["step_index"]), int(row["revision"]), row["action_intent_id"], action, decision_kind, selected_hint_id, claim, row["after_json"] is not None)


def _observation_json(observation: ObservationEnvelope | None) -> str | None:
    if observation is None:
        return None
    return json.dumps({
        "screenshot_digest": observation.screenshot_digest,
        "ui_tree_digest": observation.ui_tree_digest,
        "device_state_digest": observation.device_state_digest,
        "freshness_digest": opaque_digest(
            "observation-freshness", observation.freshness_token,
        ),
        "causality_command_id": observation.causality_command_id,
        "profile_generation": observation.profile_generation,
        "boot_id_digest": opaque_digest("boot-id", observation.boot_id),
        "canonical_device_id_digest": opaque_digest(
            "canonical-device-id", observation.canonical_device_id,
        ),
        "profile_id_digest": opaque_digest("profile-id", observation.profile_id),
    }, ensure_ascii=False, sort_keys=True)


def _decision_json(decision: RoleDecision) -> str:
    return json.dumps({"kind": decision.kind, "reason": sanitize_summary(decision.reason, maximum=500), "terminal_summary": sanitize_summary(decision.terminal_summary, maximum=500), "action": safe_action_payload(decision.action.kind, decision.action.arguments) if decision.action else None, "selected_hint_id": decision.selected_hint_id}, ensure_ascii=False, sort_keys=True)


def _safe_retrieval(items: tuple[Mapping[str, Any], ...]) -> str:
    safe: list[dict[str, str | bool]] = []
    for item in items:
        candidate, provenance = str(item.get("candidate_id", "")), str(item.get("provenance", ""))
        if not _OPAQUE.fullmatch(candidate) or not _OPAQUE.fullmatch(provenance):
            raise ValueError("retrieval attribution must use opaque identifiers")
        selected = item.get("selected", False)
        if not isinstance(selected, bool):
            raise ValueError("retrieval selection must be boolean")
        safe.append({"candidate_id": candidate, "provenance": provenance, "selected": selected})
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)


def _coverage_payload(
    items: tuple[Mapping[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    values = tuple({
        "criterion_id": item.get("criterion_id"),
        "clause_index": item.get("clause_index"),
        "source_start": item.get("source_start"),
        "source_end": item.get("source_end"),
        "source_digest": item.get("source_digest"),
        "semantic_marker": item.get("semantic_marker"),
    } for item in items)
    if values:
        criteria_coverage_digest(values)
    return values


def _safe_decision(encoded: str | None) -> dict[str, Any] | None:
    if encoded is None:
        return None
    value = json.loads(encoded)
    return {"kind": value["kind"], "reason": value["reason"], "terminal_summary": value["terminal_summary"], "selected_hint_id": value.get("selected_hint_id")}


def _safe_verification(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in (
            "task_id", "runner_kind", "runner_version", "revision",
            "criteria_digest", "latest_step_index", "overall",
            "already_satisfied",
        )
    }
    verdicts = value.get("verdicts")
    result["verdicts"] = [
        {"criterion_id": item.get("criterion_id"), "state": item.get("state")}
        for item in verdicts
        if isinstance(item, Mapping)
    ] if isinstance(verdicts, list) else []
    return result


def _structural_verification(value: dict[str, Any]) -> dict[str, Any]:
    """Redacted migration view: no screenshots, text, bounds, or device IDs."""
    after = value.get("after") if isinstance(value, Mapping) else None
    tree = after.get("ui_tree_digest") if isinstance(after, Mapping) else None
    return {
        "latest_step_index": value.get("latest_step_index"),
        "after": {"ui_tree_digest": tree} if isinstance(tree, str) and _DIGEST.fullmatch(tree) else {},
    }
