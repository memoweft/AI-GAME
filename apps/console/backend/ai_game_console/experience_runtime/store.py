from __future__ import annotations

import json
import sqlite3
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import (
    ActionTransition,
    AndroidExperienceCandidate,
    AuthenticatedAndroidScope,
    ExperienceCandidate,
    ExperienceEpisode,
    OutcomeSignal,
    PolicyRevision,
    SceneState,
    ScopeKey,
)


_SCHEMA_VERSION = 6


class AndroidExperienceScopeConflict(ValueError):
    """A task id is already bound to a different exact Android scope."""


class SQLiteExperienceStore:
    """Additive canonical experience ledger; old learning databases stay untouched."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        self.initialize()

    def initialize(self) -> None:
        with self._lock, self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experience_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO experience_schema(singleton, version) VALUES (1, 6);

                CREATE TABLE IF NOT EXISTS experience_episodes (
                    episode_id TEXT PRIMARY KEY,
                    goal_run_id TEXT NOT NULL,
                    source_task_id TEXT NOT NULL UNIQUE,
                    goal_spec_revision INTEGER NOT NULL,
                    scope_json TEXT NOT NULL,
                    frozen_criteria_json TEXT NOT NULL,
                    terminal_outcome TEXT,
                    action_count INTEGER NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    intervention_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_experience_episode_goal
                ON experience_episodes(goal_run_id, created_at);

                CREATE TABLE IF NOT EXISTS experience_scenes (
                    scene_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES experience_episodes(episode_id),
                    observation_ref TEXT NOT NULL,
                    integrity_hash TEXT NOT NULL,
                    visual_fingerprint TEXT NOT NULL DEFAULT '',
                    semantic_label TEXT NOT NULL,
                    anchors_json TEXT NOT NULL,
                    compatibility_key TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    created_at TEXT NOT NULL,
                    UNIQUE(episode_id, observation_ref)
                );
                CREATE INDEX IF NOT EXISTS idx_experience_scene_match
                ON experience_scenes(semantic_label, compatibility_key);

                CREATE TABLE IF NOT EXISTS experience_transitions (
                    transition_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES experience_episodes(episode_id),
                    source_attempt_id TEXT NOT NULL UNIQUE,
                    source_attempt_sequence INTEGER NOT NULL,
                    objective TEXT NOT NULL,
                    before_scene_id TEXT NOT NULL REFERENCES experience_scenes(scene_id),
                    semantic_action TEXT NOT NULL,
                    grounded_region TEXT,
                    coordinates_json TEXT,
                    expected_outcome TEXT NOT NULL,
                    transport_status TEXT NOT NULL,
                    after_scene_id TEXT REFERENCES experience_scenes(scene_id),
                    immediate_outcome TEXT NOT NULL,
                    failure_class TEXT,
                    recovery_of_transition_id TEXT REFERENCES experience_transitions(transition_id),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_experience_transition_episode
                ON experience_transitions(episode_id, source_attempt_sequence);

                CREATE TABLE IF NOT EXISTS experience_outcomes (
                    signal_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES experience_episodes(episode_id),
                    transition_id TEXT REFERENCES experience_transitions(transition_id),
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    reward_vector_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_experience_outcome_transition
                ON experience_outcomes(transition_id, created_at);

                CREATE TABLE IF NOT EXISTS experience_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    scope_json TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('positive', 'negative', 'recovery')),
                    objective_matcher TEXT NOT NULL,
                    scene_matcher TEXT NOT NULL,
                    semantic_action TEXT NOT NULL,
                    expected_next_scene TEXT,
                    recovery_action TEXT,
                    support_count INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    provenance_json TEXT NOT NULL,
                    compatibility_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('candidate','promoted','rejected','deprecated')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope_key, kind, objective_matcher, scene_matcher, semantic_action, compatibility_key)
                );
                CREATE INDEX IF NOT EXISTS idx_experience_candidate_retrieve
                ON experience_candidates(scope_key, objective_matcher, scene_matcher, status);

                CREATE TABLE IF NOT EXISTS experience_policies (
                    policy_id TEXT PRIMARY KEY,
                    scope_json TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    candidate_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active','rolled_back','superseded')),
                    rollback_of_revision INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(scope_key, revision)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_policy_active
                ON experience_policies(scope_key) WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS experience_retrievals (
                    retrieval_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES experience_episodes(episode_id),
                    objective TEXT NOT NULL,
                    scene_id TEXT NOT NULL REFERENCES experience_scenes(scene_id),
                    candidate_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experience_usage (
                    usage_id TEXT PRIMARY KEY,
                    retrieval_id TEXT NOT NULL REFERENCES experience_retrievals(retrieval_id),
                    candidate_id TEXT NOT NULL REFERENCES experience_candidates(candidate_id),
                    source_attempt_id TEXT NOT NULL,
                    result_signal_id TEXT REFERENCES experience_outcomes(signal_id),
                    created_at TEXT NOT NULL,
                    UNIQUE(retrieval_id, candidate_id, source_attempt_id)
                );
                CREATE TABLE IF NOT EXISTS experience_candidate_trials (
                    trial_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES experience_candidates(candidate_id),
                    usage_id TEXT NOT NULL REFERENCES experience_usage(usage_id),
                    adhered INTEGER NOT NULL CHECK (adhered IN (0, 1)),
                    result TEXT NOT NULL CHECK (result IN ('support','failure','uncertain')),
                    signal_id TEXT REFERENCES experience_outcomes(signal_id),
                    created_at TEXT NOT NULL,
                    UNIQUE(candidate_id, usage_id)
                );

                CREATE TABLE IF NOT EXISTS experience_legacy_imports (
                    import_id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    provenance_valid INTEGER NOT NULL CHECK (provenance_valid IN (0, 1)),
                    goal_coverage_valid INTEGER NOT NULL CHECK (goal_coverage_valid IN (0, 1)),
                    status TEXT NOT NULL CHECK (status IN ('untrusted','eligible','rejected')),
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_kind, source_id, source_version)
                );

                -- v4 is deliberately separate from legacy experience rows.
                -- It never reads legacy local-user/unknown scopes as Android UI
                -- hints, so old rows cannot become reusable by migration.
                CREATE TABLE IF NOT EXISTS experience_android_episodes (
                    episode_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    scope_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    terminal_verification_json TEXT
                );
                CREATE TABLE IF NOT EXISTS experience_android_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    source_episode_id TEXT NOT NULL REFERENCES experience_android_episodes(episode_id),
                    scope_json TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    reusable INTEGER NOT NULL CHECK (reusable IN (0, 1)),
                    kind TEXT NOT NULL CHECK (kind IN ('progress','positive','negative','recovery')),
                    action_kind TEXT NOT NULL,
                    semantic_anchor TEXT NOT NULL,
                    expected_scene_marker TEXT NOT NULL,
                    source_checkpoint_index INTEGER NOT NULL,
                    source_observation_digest TEXT NOT NULL,
                    support_count INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    provenance_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('task_local','active','deprecated')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_experience_android_candidate_scope
                ON experience_android_candidates(scope_key, reusable, status);
                CREATE TABLE IF NOT EXISTS experience_android_retrievals (
                    retrieval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    observation_token TEXT NOT NULL,
                    observation_digest TEXT NOT NULL,
                    checkpoint_index INTEGER NOT NULL,
                    candidate_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experience_android_usage (
                    usage_id TEXT PRIMARY KEY,
                    retrieval_id TEXT NOT NULL REFERENCES experience_android_retrievals(retrieval_id),
                    candidate_id TEXT NOT NULL REFERENCES experience_android_candidates(candidate_id),
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    result TEXT NOT NULL CHECK (result IN ('support','contradiction','unknown')),
                    verification_digest TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(retrieval_id, candidate_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS experience_android_policy_revisions (
                    policy_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    candidate_id TEXT NOT NULL REFERENCES experience_android_candidates(candidate_id),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scope_key, revision)
                );
                """
            )
            row = connection.execute(
                "SELECT version FROM experience_schema WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("unsupported experience database schema")
            stored_version = int(row["version"])
            if stored_version == 1:
                columns = {
                    str(item["name"])
                    for item in connection.execute("PRAGMA table_info(experience_scenes)")
                }
                if "visual_fingerprint" not in columns:
                    connection.execute(
                        "ALTER TABLE experience_scenes ADD COLUMN visual_fingerprint "
                        "TEXT NOT NULL DEFAULT ''"
                    )
                connection.execute(
                    "UPDATE experience_schema SET version = 2 WHERE singleton = 1"
                )
                stored_version = 2
            if stored_version == 2:
                self._migrate_scope_keys_to_v3(connection)
                connection.execute(
                    "UPDATE experience_schema SET version = 3 WHERE singleton = 1"
                )
                stored_version = 3
            if stored_version == 3:
                connection.execute(
                    "UPDATE experience_schema SET version = 4 WHERE singleton = 1"
                )
                stored_version = 4
            if stored_version == 4:
                connection.execute(
                    "UPDATE experience_schema SET version = 5 WHERE singleton = 1"
                )
                stored_version = 5
            if stored_version == 5:
                candidate_columns = {
                    str(item["name"])
                    for item in connection.execute("PRAGMA table_info(experience_android_candidates)")
                }
                retrieval_columns = {
                    str(item["name"])
                    for item in connection.execute("PRAGMA table_info(experience_android_retrievals)")
                }
                if "source_checkpoint_index" not in candidate_columns:
                    connection.execute(
                        "ALTER TABLE experience_android_candidates ADD COLUMN source_checkpoint_index INTEGER NOT NULL DEFAULT 0"
                    )
                if "source_observation_digest" not in candidate_columns:
                    connection.execute(
                        "ALTER TABLE experience_android_candidates ADD COLUMN source_observation_digest TEXT NOT NULL DEFAULT ''"
                    )
                if "checkpoint_index" not in retrieval_columns:
                    connection.execute(
                        "ALTER TABLE experience_android_retrievals ADD COLUMN checkpoint_index INTEGER NOT NULL DEFAULT 0"
                    )
                connection.execute(
                    "UPDATE experience_schema SET version = 6 WHERE singleton = 1"
                )
                stored_version = 6
            if stored_version != _SCHEMA_VERSION:
                raise RuntimeError("unsupported experience database schema")

    @staticmethod
    def _migrate_scope_keys_to_v3(connection: sqlite3.Connection) -> None:
        """Add strict scope dimensions without abandoning a v1/v2 ledger.

        ScopeKey is stored as JSON.  Rewriting both JSON and derived keys keeps
        candidate/policy retrieval compatible after the additive dataclass
        fields were introduced.
        """

        for table, key_column in (
            ("experience_episodes", None),
            ("experience_candidates", "scope_key"),
            ("experience_policies", "scope_key"),
        ):
            rows = connection.execute(
                f"SELECT rowid, scope_json FROM {table}"
            ).fetchall()
            for row in rows:
                scope = _scope(str(row["scope_json"]))
                scope_json = _json(_scope_dict(scope))
                if key_column is None:
                    connection.execute(
                        f"UPDATE {table} SET scope_json = ? WHERE rowid = ?",
                        (scope_json, row["rowid"]),
                    )
                else:
                    connection.execute(
                        f"UPDATE {table} SET scope_json = ?, {key_column} = ? WHERE rowid = ?",
                        (scope_json, _scope_key(scope), row["rowid"]),
                    )

    def put_episode(self, episode: ExperienceEpisode) -> ExperienceEpisode:
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO experience_episodes(
                    episode_id, goal_run_id, source_task_id, goal_spec_revision,
                    scope_json, frozen_criteria_json, terminal_outcome, action_count,
                    recovery_count, intervention_count, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.episode_id, episode.goal_run_id, episode.source_task_id,
                    episode.goal_spec_revision, _json(_scope_dict(episode.scope)),
                    _json(episode.frozen_criteria_ids), episode.terminal_outcome,
                    episode.action_count, episode.recovery_count,
                    episode.intervention_count, episode.created_at, episode.finished_at,
                ),
            )
            return self.episode_for_task(episode.source_task_id, connection=connection)

    def episode_for_task(
        self, source_task_id: str, *, connection: sqlite3.Connection | None = None
    ) -> ExperienceEpisode:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM experience_episodes WHERE source_task_id = ?",
                (source_task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(source_task_id)
            return _episode(row)
        with self._connection() as owned:
            return self.episode_for_task(source_task_id, connection=owned)

    def finish_episode(self, source_task_id: str, outcome: str) -> ExperienceEpisode:
        now = _utc_now()
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "UPDATE experience_episodes SET terminal_outcome = ?, finished_at = ? "
                "WHERE source_task_id = ? AND terminal_outcome IS NULL",
                (outcome, now, source_task_id),
            )
            return self.episode_for_task(source_task_id, connection=connection)

    def put_scene(self, scene: SceneState) -> SceneState:
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO experience_scenes(
                    scene_id, episode_id, observation_ref, integrity_hash,
                    visual_fingerprint, semantic_label, anchors_json,
                    compatibility_key, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scene.scene_id, scene.episode_id, scene.observation_ref,
                    scene.integrity_hash, scene.visual_fingerprint,
                    scene.semantic_label, _json(scene.anchors),
                    scene.compatibility_key, scene.confidence, scene.created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM experience_scenes WHERE episode_id = ? AND observation_ref = ?",
                (scene.episode_id, scene.observation_ref),
            ).fetchone()
            if row is None:
                raise RuntimeError("scene write failed")
            return _scene(row)

    def put_transition(self, transition: ActionTransition) -> ActionTransition:
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO experience_transitions(
                    transition_id, episode_id, source_attempt_id, source_attempt_sequence,
                    objective, before_scene_id, semantic_action, grounded_region,
                    coordinates_json, expected_outcome, transport_status, after_scene_id,
                    immediate_outcome, failure_class, recovery_of_transition_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition.transition_id, transition.episode_id,
                    transition.source_attempt_id, transition.source_attempt_sequence,
                    transition.objective, transition.before_scene_id,
                    transition.semantic_action, transition.grounded_region,
                    transition.coordinates_json, transition.expected_outcome,
                    transition.transport_status, transition.after_scene_id,
                    transition.immediate_outcome, transition.failure_class,
                    transition.recovery_of_transition_id, transition.created_at,
                ),
            )
            connection.execute(
                "UPDATE experience_episodes SET action_count = "
                "(SELECT COUNT(*) FROM experience_transitions WHERE episode_id = ?) "
                "WHERE episode_id = ?",
                (transition.episode_id, transition.episode_id),
            )
            row = connection.execute(
                "SELECT * FROM experience_transitions WHERE source_attempt_id = ?",
                (transition.source_attempt_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("transition write failed")
            return _transition(row)

    def put_signal(self, signal: OutcomeSignal) -> OutcomeSignal:
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO experience_outcomes(
                    signal_id, episode_id, transition_id, kind, source,
                    evidence_refs_json, confidence, reward_vector_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signal_id, signal.episode_id, signal.transition_id,
                    signal.kind, signal.source, _json(signal.evidence_refs),
                    signal.confidence, signal.reward_vector_json, signal.created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM experience_outcomes WHERE signal_id = ?",
                (signal.signal_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("signal write failed")
            return _signal(row)

    def signals_for_episode(self, episode_id: str) -> list[OutcomeSignal]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM experience_outcomes WHERE episode_id = ? "
                "ORDER BY created_at, signal_id",
                (episode_id,),
            ).fetchall()
            return [_signal(row) for row in rows]

    def scenes_for_episode(self, episode_id: str) -> list[SceneState]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM experience_scenes WHERE episode_id = ? ORDER BY created_at",
                (episode_id,),
            ).fetchall()
            return [_scene(row) for row in rows]

    def similar_scene(
        self, scope: ScopeKey, visual_fingerprint: str, *, max_distance: int = 8
    ) -> SceneState | None:
        if not visual_fingerprint:
            return None
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT s.* FROM experience_scenes s
                JOIN experience_episodes e ON e.episode_id = s.episode_id
                WHERE e.scope_json = ? AND s.visual_fingerprint != ''
                ORDER BY s.confidence DESC, s.created_at DESC LIMIT 200""",
                (_json(_scope_dict(scope)),),
            ).fetchall()
        ranked = sorted(
            ((_hamming(visual_fingerprint, str(row["visual_fingerprint"])), row)
             for row in rows),
            key=lambda item: (item[0], -float(item[1]["confidence"])),
        )
        return _scene(ranked[0][1]) if ranked and ranked[0][0] <= max_distance else None

    def transitions_for_episode(self, episode_id: str) -> list[ActionTransition]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM experience_transitions WHERE episode_id = ? "
                "ORDER BY source_attempt_sequence",
                (episode_id,),
            ).fetchall()
            return [_transition(row) for row in rows]

    def transition_for_attempt(self, source_attempt_id: str) -> ActionTransition | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM experience_transitions WHERE source_attempt_id = ?",
                (source_attempt_id,),
            ).fetchone()
            return _transition(row) if row is not None else None

    def transition(self, transition_id: str) -> ActionTransition:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM experience_transitions WHERE transition_id = ?",
                (transition_id,),
            ).fetchone()
            if row is None:
                raise KeyError(transition_id)
            return _transition(row)

    def episode(self, episode_id: str) -> ExperienceEpisode:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM experience_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if row is None:
                raise KeyError(episode_id)
            return _episode(row)

    def latest_signal_for_transition(self, transition_id: str) -> OutcomeSignal | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM experience_outcomes WHERE transition_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (transition_id,),
            ).fetchone()
            return _signal(row) if row is not None else None

    def transition_after_matches(
        self, source_transition_id: str, observed_transition_id: str,
        *, max_distance: int = 8,
    ) -> bool:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT t.transition_id, s.visual_fingerprint
                FROM experience_transitions t
                JOIN experience_scenes s ON s.scene_id = t.after_scene_id
                WHERE t.transition_id IN (?, ?)""",
                (source_transition_id, observed_transition_id),
            ).fetchall()
        values = {str(row["transition_id"]): str(row["visual_fingerprint"]) for row in rows}
        left = values.get(source_transition_id, "")
        right = values.get(observed_transition_id, "")
        return bool(left and right and _hamming(left, right) <= max_distance)

    def candidates_for_transition(self, transition_id: str) -> list[ExperienceCandidate]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM experience_candidates WHERE instr(provenance_json, ?) > 0",
                (transition_id,),
            ).fetchall()
            return [_candidate(row) for row in rows]

    def upsert_candidate(self, candidate: ExperienceCandidate) -> ExperienceCandidate:
        key = _scope_key(candidate.scope)
        with self._lock, self._connection(write=True) as connection:
            existing = connection.execute(
                """SELECT * FROM experience_candidates WHERE scope_key = ? AND kind = ?
                AND objective_matcher = ? AND scene_matcher = ? AND semantic_action = ?
                AND compatibility_key = ?""",
                (key, candidate.kind, candidate.objective_matcher, candidate.scene_matcher,
                 candidate.semantic_action, candidate.compatibility_key),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO experience_candidates(
                    candidate_id, scope_json, scope_key, kind, objective_matcher,
                    scene_matcher, semantic_action, expected_next_scene, recovery_action,
                    support_count, failure_count, confidence, provenance_json,
                    compatibility_key, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.candidate_id, _json(_scope_dict(candidate.scope)), key,
                        candidate.kind, candidate.objective_matcher, candidate.scene_matcher,
                        candidate.semantic_action, candidate.expected_next_scene,
                        candidate.recovery_action, candidate.support_count,
                        candidate.failure_count, candidate.confidence,
                        _json(candidate.provenance_transition_ids),
                        candidate.compatibility_key, candidate.status,
                        candidate.created_at, candidate.updated_at,
                    ),
                )
                candidate_id = candidate.candidate_id
            else:
                if str(existing["status"]) != "candidate":
                    return _candidate(existing)
                provenance = tuple(dict.fromkeys(
                    (*json.loads(existing["provenance_json"]),
                     *candidate.provenance_transition_ids)
                ))
                support = int(existing["support_count"]) + candidate.support_count
                failures = int(existing["failure_count"]) + candidate.failure_count
                confidence = _candidate_confidence(support, failures)
                connection.execute(
                    """UPDATE experience_candidates SET support_count = ?, failure_count = ?,
                    confidence = ?, provenance_json = ?, expected_next_scene = COALESCE(?, expected_next_scene),
                    recovery_action = COALESCE(?, recovery_action), updated_at = ?
                    WHERE candidate_id = ?""",
                    (support, failures, confidence, _json(provenance),
                     candidate.expected_next_scene, candidate.recovery_action,
                     candidate.updated_at, existing["candidate_id"]),
                )
                candidate_id = str(existing["candidate_id"])
            return self.candidate(candidate_id, connection=connection)

    def candidate(
        self, candidate_id: str, *, connection: sqlite3.Connection | None = None
    ) -> ExperienceCandidate:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM experience_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            return _candidate(row)
        with self._connection() as owned:
            return self.candidate(candidate_id, connection=owned)

    def candidates(
        self, scope: ScopeKey, *, status: str | None = None
    ) -> list[ExperienceCandidate]:
        with self._connection() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM experience_candidates WHERE scope_key = ? "
                    "ORDER BY created_at, candidate_id",
                    (_scope_key(scope),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM experience_candidates WHERE scope_key = ? AND status = ? "
                    "ORDER BY created_at, candidate_id",
                    (_scope_key(scope), status),
                ).fetchall()
            return [_candidate(row) for row in rows]

    def set_candidate_status(self, candidate_id: str, status: str) -> ExperienceCandidate:
        if status not in {"promoted", "rejected", "deprecated"}:
            raise ValueError("invalid candidate status")
        with self._lock, self._connection(write=True) as connection:
            current = self.candidate(candidate_id, connection=connection)
            if current.status in {"rejected", "deprecated"} and status == "promoted":
                raise ValueError("rejected or deprecated candidate cannot be promoted")
            connection.execute(
                "UPDATE experience_candidates SET status = ?, updated_at = ? WHERE candidate_id = ?",
                (status, _utc_now(), candidate_id),
            )
            return self.candidate(candidate_id, connection=connection)

    def active_candidates(
        self, scope: ScopeKey, *, objective: str, scene_matcher: str,
        visual_fingerprint: str = "", compatibility_key: str = "",
    ) -> list[ExperienceCandidate]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT c.* FROM experience_candidates c
                JOIN experience_policies p ON p.scope_key = c.scope_key AND p.status = 'active'
                WHERE c.scope_key = ? AND c.status = 'promoted'
                  AND instr(p.candidate_ids_json, c.candidate_id) > 0
                ORDER BY c.confidence DESC, c.updated_at DESC""",
                (_scope_key(scope),),
            ).fetchall()
            matched: list[tuple[int, float, ExperienceCandidate]] = []
            for row in rows:
                candidate = _candidate(row)
                objective_score = _text_similarity(objective, candidate.objective_matcher)
                if objective_score < 0.18:
                    continue
                if compatibility_key and candidate.compatibility_key != compatibility_key:
                    continue
                distance = 0 if candidate.scene_matcher == scene_matcher else 10_000
                if distance and visual_fingerprint:
                    provenance = candidate.provenance_transition_ids
                    if provenance:
                        scene_row = connection.execute(
                            """SELECT s.visual_fingerprint FROM experience_scenes s
                            JOIN experience_transitions t ON t.before_scene_id = s.scene_id
                            WHERE t.transition_id = ?""",
                            (provenance[0],),
                        ).fetchone()
                        if scene_row is not None:
                            distance = _hamming(
                                visual_fingerprint, str(scene_row["visual_fingerprint"])
                            )
                if distance <= 8:
                    matched.append((distance, objective_score, candidate))
            matched.sort(key=lambda item: (item[0], -item[1], -item[2].confidence))
            return [item[2] for item in matched]

    def put_policy(self, policy: PolicyRevision) -> PolicyRevision:
        key = _scope_key(policy.scope)
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "UPDATE experience_policies SET status = 'superseded' "
                "WHERE scope_key = ? AND status = 'active'",
                (key,),
            )
            connection.execute(
                """INSERT INTO experience_policies(
                policy_id, scope_json, scope_key, revision, candidate_ids_json,
                status, rollback_of_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (policy.policy_id, _json(_scope_dict(policy.scope)), key,
                 policy.revision, _json(policy.candidate_ids), policy.status,
                 policy.rollback_of_revision, policy.created_at),
            )
            return policy

    def policies(self, scope: ScopeKey) -> list[PolicyRevision]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM experience_policies WHERE scope_key = ? ORDER BY revision",
                (_scope_key(scope),),
            ).fetchall()
            return [_policy(row) for row in rows]

    def record_retrieval(
        self, retrieval_id: str, episode_id: str, objective: str,
        scene_id: str, candidate_ids: tuple[str, ...]
    ) -> None:
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO experience_retrievals VALUES (?, ?, ?, ?, ?, ?)",
                (retrieval_id, episode_id, objective, scene_id,
                 _json(candidate_ids), _utc_now()),
            )

    def record_usage(
        self, usage_id: str, retrieval_id: str, candidate_id: str,
        source_attempt_id: str, result_signal_id: str | None
    ) -> str:
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO experience_usage VALUES (?, ?, ?, ?, ?, ?)",
                (usage_id, retrieval_id, candidate_id, source_attempt_id,
                 result_signal_id, _utc_now()),
            )
            row = connection.execute(
                """SELECT usage_id FROM experience_usage WHERE retrieval_id = ?
                AND candidate_id = ? AND source_attempt_id = ?""",
                (retrieval_id, candidate_id, source_attempt_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("experience usage write failed")
            return str(row["usage_id"])

    def record_candidate_trial(
        self, *, trial_id: str, candidate_id: str, usage_id: str,
        adhered: bool, result: str, signal_id: str | None,
    ) -> None:
        if result not in {"support", "failure", "uncertain"}:
            raise ValueError("invalid candidate trial result")
        with self._lock, self._connection(write=True) as connection:
            inserted = connection.execute(
                """INSERT OR IGNORE INTO experience_candidate_trials(
                trial_id, candidate_id, usage_id, adhered, result, signal_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (trial_id, candidate_id, usage_id, int(adhered), result,
                 signal_id, _utc_now()),
            ).rowcount
            if not inserted or result == "uncertain":
                return
            row = connection.execute(
                "SELECT support_count, failure_count FROM experience_candidates "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            support = int(row["support_count"]) + int(result == "support")
            failures = int(row["failure_count"]) + int(result == "failure")
            connection.execute(
                """UPDATE experience_candidates SET support_count = ?, failure_count = ?,
                confidence = ?, updated_at = ? WHERE candidate_id = ?""",
                (support, failures, _candidate_confidence(support, failures),
                 _utc_now(), candidate_id),
            )

    def record_legacy_import(
        self, *, import_id: str, source_kind: str, source_id: str,
        source_version: int, provenance_valid: bool, goal_coverage_valid: bool,
        detail: str
    ) -> str:
        status = "eligible" if provenance_valid and goal_coverage_valid else (
            "rejected" if not provenance_valid else "untrusted"
        )
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO experience_legacy_imports VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (import_id, source_kind, source_id, source_version,
                 int(provenance_valid), int(goal_coverage_valid), status,
                 detail, _utc_now()),
            )
        return status

    # --- v4 authenticated Android UI experience ---------------------------------

    def put_android_episode(
        self, *, episode_id: str, scope: AuthenticatedAndroidScope, created_at: str,
    ) -> str:
        """Persist a task-local ledger root without any raw owner values."""
        scope_json = _json(_android_scope_dict(scope))
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT episode_id, scope_json FROM experience_android_episodes WHERE task_id = ?",
                (scope.task_id,),
            ).fetchone()
            if row is not None:
                if str(row["scope_json"]) != scope_json:
                    raise AndroidExperienceScopeConflict("task scope is immutable")
                return str(row["episode_id"])
            connection.execute(
                """INSERT INTO experience_android_episodes(
                episode_id, task_id, scope_json, created_at, terminal_verification_json
                ) VALUES (?, ?, ?, ?, NULL)""",
                (episode_id, scope.task_id, scope_json, created_at),
            )
            return episode_id

    def android_episode_scope(self, task_id: str, *, expected_scope: AuthenticatedAndroidScope | None = None) -> AuthenticatedAndroidScope:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT scope_json FROM experience_android_episodes WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            scope = _android_scope(str(row["scope_json"]))
            if expected_scope is not None and _android_scope_dict(scope) != _android_scope_dict(expected_scope):
                raise AndroidExperienceScopeConflict("task scope is immutable")
            return scope

    def set_android_terminal_verification(self, task_id: str, verification: dict[str, Any]) -> None:
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "UPDATE experience_android_episodes SET terminal_verification_json = ? WHERE task_id = ?",
                (_json(verification), task_id),
            )

    def put_android_candidate(self, candidate: AndroidExperienceCandidate, *, reusable: bool) -> AndroidExperienceCandidate:
        _assert_android_candidate_safe(candidate, reusable=reusable)
        scope_json = _json(_android_scope_dict(candidate.scope))
        scope_key = _android_scope_key(candidate.scope)
        with self._lock, self._connection(write=True) as connection:
            if not reusable:
                episode = _require_android_episode_scope(connection, candidate.scope)
                if str(episode["episode_id"]) != candidate.source_episode_id:
                    raise ValueError("candidate source episode does not own this scope")
            existing_row = connection.execute(
                "SELECT * FROM experience_android_candidates WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            if existing_row is not None:
                existing = _android_candidate(existing_row)
                if (
                    bool(existing_row["reusable"]) != reusable
                    or not _same_android_candidate_fact(existing, candidate)
                ):
                    raise ValueError("candidate idempotency identity conflict")
                return existing
            connection.execute(
                """INSERT INTO experience_android_candidates(
                candidate_id, source_episode_id, scope_json, scope_key, reusable, kind,
                action_kind, semantic_anchor, expected_scene_marker, source_checkpoint_index,
                source_observation_digest, support_count,
                failure_count, confidence, provenance_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate.candidate_id, candidate.source_episode_id, scope_json, scope_key,
                    int(reusable), candidate.kind, candidate.action_kind, candidate.semantic_anchor,
                    candidate.expected_scene_marker, candidate.source_checkpoint_index,
                    candidate.source_observation_digest, candidate.support_count, candidate.failure_count,
                    candidate.confidence, _json(candidate.provenance_ids), candidate.status,
                    candidate.created_at, candidate.updated_at,
                ),
            )
            return self.android_candidate(candidate.candidate_id, connection=connection)

    def android_candidate(
        self, candidate_id: str, *, connection: sqlite3.Connection | None = None,
    ) -> AndroidExperienceCandidate:
        if connection is None:
            with self._connection() as owned:
                return self.android_candidate(candidate_id, connection=owned)
        row = connection.execute(
            "SELECT * FROM experience_android_candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return _android_candidate(row)

    def android_candidates(self) -> list[AndroidExperienceCandidate]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM experience_android_candidates ORDER BY created_at, candidate_id"
            ).fetchall()
            return [_android_candidate(row) for row in rows]

    def android_candidates_for_scope(
        self, scope: AuthenticatedAndroidScope,
    ) -> list[tuple[AndroidExperienceCandidate, bool]]:
        """Return only exact task-local or exact reusable scope candidates."""
        self.android_episode_scope(scope.task_id, expected_scope=scope)
        reusable_key = _android_scope_key(scope.reusable_scope()) if scope.cross_task_eligible else None
        task_key = _android_scope_key(scope)
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM experience_android_candidates
                WHERE status IN ('task_local', 'active') AND scope_key IN (?, COALESCE(?, ''))
                ORDER BY confidence DESC, updated_at DESC""",
                (task_key, reusable_key),
            ).fetchall()
        result: list[tuple[AndroidExperienceCandidate, bool]] = []
        for row in rows:
            if not _row_candidate_is_safe(row):
                continue
            candidate = _android_candidate(row)
            is_reusable = bool(row["reusable"])
            # JSON scope comparison, not a partial SQL predicate: any hard
            # dimension drift returns cold planning rather than a Task error.
            expected = scope.reusable_scope() if is_reusable and scope.cross_task_eligible else scope
            if _android_scope_dict(candidate.scope) == _android_scope_dict(expected):
                result.append((candidate, is_reusable))
        return result

    def record_android_retrieval(
        self, *, retrieval_id: str, task_id: str, step_id: str,
        scope: AuthenticatedAndroidScope, observation_token: str,
        observation_digest: str, checkpoint_index: int, candidate_ids: tuple[str, ...], created_at: str,
    ) -> tuple[str, ...]:
        with self._lock, self._connection(write=True) as connection:
            _require_android_episode_scope(connection, scope)
            if task_id != scope.task_id or checkpoint_index < 1:
                raise ValueError("retrieval task or checkpoint is invalid")
            for candidate_id in candidate_ids:
                candidate = connection.execute(
                    "SELECT * FROM experience_android_candidates WHERE candidate_id = ?", (candidate_id,)
                ).fetchone()
                if candidate is None or not _candidate_is_exact_for_scope(candidate, scope):
                    raise ValueError("retrieval candidate scope mismatch")
            existing = connection.execute(
                "SELECT * FROM experience_android_retrievals WHERE retrieval_id = ?",
                (retrieval_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    task_id, step_id, _json(_android_scope_dict(scope)),
                    observation_token, observation_digest, checkpoint_index,
                )
                actual = (
                    str(existing["task_id"]), str(existing["step_id"]),
                    str(existing["scope_json"]), str(existing["observation_token"]),
                    str(existing["observation_digest"]), int(existing["checkpoint_index"]),
                )
                if actual != expected:
                    raise ValueError("retrieval idempotency identity conflict")
                return tuple(json.loads(str(existing["candidate_ids_json"])))
            connection.execute(
                """INSERT INTO experience_android_retrievals(
                retrieval_id, task_id, step_id, scope_json, observation_token,
                observation_digest, checkpoint_index, candidate_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (retrieval_id, task_id, step_id, _json(_android_scope_dict(scope)),
                 observation_token, observation_digest, checkpoint_index, _json(candidate_ids), created_at),
            )
            return candidate_ids

    def record_android_usage(
        self, *, usage_id: str, retrieval_id: str, candidate_id: str, task_id: str,
        step_id: str, scope: AuthenticatedAndroidScope, result: str,
        verification_digest: str | None, verification_token: str | None,
        verification_observation_digest: str | None, created_at: str,
        verification_checkpoint_index: int | None = None,
    ) -> bool:
        if result not in {"support", "contradiction", "unknown"}:
            raise ValueError("invalid android experience result")
        with self._lock, self._connection(write=True) as connection:
            _require_android_episode_scope(connection, scope)
            if task_id != scope.task_id:
                raise ValueError("usage task scope mismatch")
            retrieval = connection.execute(
                """SELECT task_id, step_id, scope_json, observation_token, observation_digest, checkpoint_index,
                candidate_ids_json FROM experience_android_retrievals WHERE retrieval_id = ?""",
                (retrieval_id,),
            ).fetchone()
            if (
                retrieval is None or str(retrieval["task_id"]) != task_id
                or str(retrieval["step_id"]) != step_id
                or str(retrieval["scope_json"]) != _json(_android_scope_dict(scope))
            ):
                raise ValueError("retrieval attribution does not belong to task")
            if candidate_id not in tuple(json.loads(retrieval["candidate_ids_json"])):
                raise ValueError("candidate was not retrieved")
            candidate_row = connection.execute(
                "SELECT * FROM experience_android_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None or not _candidate_is_exact_for_scope(candidate_row, scope):
                raise ValueError("candidate attribution scope mismatch")
            if verification_digest is not None and not _OPAQUE_DIGEST.fullmatch(verification_digest):
                raise ValueError("verification digest must be opaque")
            if result != "unknown":
                if not verification_token or not verification_observation_digest:
                    raise ValueError("certain outcome requires fresh verification evidence")
                if verification_token == str(retrieval["observation_token"]):
                    raise ValueError("verification evidence is not fresh after retrieval")
                if verification_checkpoint_index is None or verification_checkpoint_index <= int(retrieval["checkpoint_index"]):
                    raise ValueError("verification checkpoint is not after retrieval")
                # The transaction is already BEGIN IMMEDIATE.  Checking the
                # durable ledger here works for old v4 databases too, without
                # risking an index-creation migration failure if a historic
                # bug already wrote duplicate rows.
                prior = connection.execute(
                    """SELECT 1 FROM experience_android_usage
                    WHERE task_id = ? AND candidate_id = ? AND verification_digest = ? LIMIT 1""",
                    (task_id, candidate_id, verification_digest),
                ).fetchone()
                if prior is not None:
                    return False
            inserted = connection.execute(
                """INSERT OR IGNORE INTO experience_android_usage(
                usage_id, retrieval_id, candidate_id, task_id, step_id, result,
                verification_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (usage_id, retrieval_id, candidate_id, task_id, step_id, result,
                 verification_digest, created_at),
            ).rowcount
            if inserted and result != "unknown":
                support = int(candidate_row["support_count"]) + int(result == "support")
                failure = int(candidate_row["failure_count"]) + int(result == "contradiction")
                connection.execute(
                    """UPDATE experience_android_candidates SET support_count = ?, failure_count = ?,
                    confidence = ?, updated_at = ? WHERE candidate_id = ?""",
                    (support, failure, _candidate_confidence(support, failure), created_at, candidate_id),
                )
                if result == "contradiction" and failure >= 2 and str(candidate_row["status"]) == "active":
                    scope_key = _android_scope_key(_android_scope(str(candidate_row["scope_json"])))
                    connection.execute(
                        "UPDATE experience_android_candidates SET status = 'deprecated', updated_at = ? WHERE candidate_id = ?",
                        (created_at, candidate_id),
                    )
                    next_revision = int(connection.execute(
                        "SELECT COALESCE(MAX(revision), 0) + 1 FROM experience_android_policy_revisions WHERE scope_key = ?",
                        (scope_key,),
                    ).fetchone()[0])
                    connection.execute(
                        """INSERT INTO experience_android_policy_revisions(
                        policy_id, scope_key, revision, candidate_id, reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (f"deprecate:{candidate_id}:{next_revision}", scope_key, next_revision,
                         candidate_id, "verified_certain_contradiction", created_at),
                    )
            return bool(inserted)

    def promote_android_candidate(
        self, *, source_candidate_id: str, scope: AuthenticatedAndroidScope,
        terminal_verification: dict[str, Any], promoted: AndroidExperienceCandidate,
    ) -> AndroidExperienceCandidate:
        """Atomically bind a trusted terminal record and promote one safe edge."""
        _assert_android_candidate_safe(promoted, reusable=True)
        with self._lock, self._connection(write=True) as connection:
            _require_android_episode_scope(connection, scope)
            source = connection.execute(
                "SELECT * FROM experience_android_candidates WHERE candidate_id = ?",
                (source_candidate_id,),
            ).fetchone()
            if source is None or _android_scope(str(source["scope_json"])) != scope:
                raise ValueError("source candidate scope mismatch")
            if str(source["status"]) != "task_local" or str(source["kind"]) == "negative":
                raise ValueError("only safe task-local evidence can be promoted")
            episode = _require_android_episode_scope(connection, scope)
            if str(source["source_episode_id"]) != str(episode["episode_id"]):
                raise ValueError("source candidate episode mismatch")
            if promoted.source_episode_id != str(source["source_episode_id"]):
                raise ValueError("promoted candidate must retain authoritative source episode")
            if (
                promoted.source_checkpoint_index != int(source["source_checkpoint_index"])
                or promoted.source_observation_digest != str(source["source_observation_digest"])
            ):
                raise ValueError("promoted candidate must retain authoritative source checkpoint")
            verification_json = _json(terminal_verification)
            existing_verification = connection.execute(
                "SELECT terminal_verification_json FROM experience_android_episodes WHERE task_id = ?",
                (scope.task_id,),
            ).fetchone()
            if existing_verification is None:
                raise KeyError(scope.task_id)
            durable_verification = existing_verification["terminal_verification_json"]
            if durable_verification not in (None, verification_json):
                raise ValueError("terminal verification provenance is immutable")
            connection.execute(
                """UPDATE experience_android_episodes
                SET terminal_verification_json = COALESCE(terminal_verification_json, ?)
                WHERE task_id = ?""",
                (verification_json, scope.task_id),
            )
            existing_candidate = connection.execute(
                "SELECT * FROM experience_android_candidates WHERE candidate_id = ?",
                (promoted.candidate_id,),
            ).fetchone()
            if existing_candidate is not None:
                current = _android_candidate(existing_candidate)
                if (
                    not bool(existing_candidate["reusable"])
                    or not _same_android_candidate_fact(current, promoted)
                ):
                    raise ValueError("promotion idempotency identity conflict")
                return current
            connection.execute(
                """INSERT INTO experience_android_candidates(
                candidate_id, source_episode_id, scope_json, scope_key, reusable, kind,
                action_kind, semantic_anchor, expected_scene_marker, source_checkpoint_index,
                source_observation_digest, support_count,
                failure_count, confidence, provenance_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    promoted.candidate_id, promoted.source_episode_id,
                    _json(_android_scope_dict(promoted.scope)), _android_scope_key(promoted.scope),
                    1, promoted.kind, promoted.action_kind, promoted.semantic_anchor,
                    promoted.expected_scene_marker, promoted.source_checkpoint_index,
                    promoted.source_observation_digest, promoted.support_count, promoted.failure_count,
                    promoted.confidence, _json(promoted.provenance_ids), promoted.status,
                    promoted.created_at, promoted.updated_at,
                ),
            )
            return _android_candidate(connection.execute(
                "SELECT * FROM experience_android_candidates WHERE candidate_id = ?", (promoted.candidate_id,)
            ).fetchone())

    def deprecate_android_candidate(self, candidate_id: str, *, reason: str, created_at: str) -> None:
        with self._lock, self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT scope_key FROM experience_android_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            connection.execute(
                "UPDATE experience_android_candidates SET status = 'deprecated', updated_at = ? WHERE candidate_id = ?",
                (created_at, candidate_id),
            )
            next_revision = int(connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM experience_android_policy_revisions WHERE scope_key = ?",
                (str(row["scope_key"]),),
            ).fetchone()[0])
            connection.execute(
                """INSERT INTO experience_android_policy_revisions(
                policy_id, scope_key, revision, candidate_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (f"deprecate:{candidate_id}:{next_revision}", str(row["scope_key"]), next_revision,
                 candidate_id, reason, created_at),
            )

    def counts(self) -> dict[str, int]:
        names = {
            "episodes": "experience_episodes", "scenes": "experience_scenes",
            "transitions": "experience_transitions", "outcomes": "experience_outcomes",
            "candidates": "experience_candidates", "policies": "experience_policies",
            "retrievals": "experience_retrievals", "usage": "experience_usage",
        }
        with self._connection() as connection:
            return {key: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for key, table in names.items()}

    def android_counts(self) -> dict[str, int]:
        names = {
            "episodes": "experience_android_episodes",
            "candidates": "experience_android_candidates",
            "retrievals": "experience_android_retrievals",
            "usage": "experience_android_usage",
            "policies": "experience_android_policy_revisions",
        }
        with self._connection() as connection:
            return {key: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for key, table in names.items()}

    def _connection(self, *, write: bool = False):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        return _ConnectionContext(connection, write)


class _ConnectionContext:
    def __init__(self, connection: sqlite3.Connection, write: bool) -> None:
        self.connection = connection
        self.write = write
    def __enter__(self) -> sqlite3.Connection:
        return self.connection
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.write:
            self.connection.rollback() if exc_type else self.connection.commit()
        self.connection.close()


def _scope_dict(scope: ScopeKey) -> dict[str, str]:
    return {name: getattr(scope, name) for name in ScopeKey.__dataclass_fields__}


def _scope(value: str) -> ScopeKey:
    return ScopeKey(**json.loads(value))


def _scope_key(scope: ScopeKey) -> str:
    return _json(_scope_dict(scope))


def _android_scope_dict(scope: AuthenticatedAndroidScope) -> dict[str, Any]:
    return {name: getattr(scope, name) for name in AuthenticatedAndroidScope.__dataclass_fields__}


def _android_scope(value: str) -> AuthenticatedAndroidScope:
    return AuthenticatedAndroidScope(**json.loads(value))


def _android_scope_key(scope: AuthenticatedAndroidScope) -> str:
    return _json(_android_scope_dict(scope))


_OPAQUE_DIGEST = re.compile(r"[a-f0-9]{64}")
_OPAQUE_PROVENANCE = re.compile(r"(?:step|observation|verification):[a-f0-9]{64}")


def _require_android_episode_scope(
    connection: sqlite3.Connection, scope: AuthenticatedAndroidScope,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT episode_id, scope_json FROM experience_android_episodes WHERE task_id = ?",
        (scope.task_id,),
    ).fetchone()
    if row is None:
        raise KeyError(scope.task_id)
    if str(row["scope_json"]) != _json(_android_scope_dict(scope)):
        raise AndroidExperienceScopeConflict("task scope is immutable")
    return row


def _candidate_is_exact_for_scope(
    row: sqlite3.Row, scope: AuthenticatedAndroidScope,
) -> bool:
    candidate_scope = _android_scope(str(row["scope_json"]))
    if not _row_candidate_is_safe(row):
        return False
    if bool(row["reusable"]):
        return (
            candidate_scope == scope.reusable_scope()
            and str(row["status"]) == "active"
            and str(row["kind"]) != "negative"
        )
    return candidate_scope == scope and str(row["status"]) == "task_local"


def _assert_android_candidate_safe(
    candidate: AndroidExperienceCandidate, *, reusable: bool,
) -> None:
    """Reject direct-store injection of raw or semantically unsafe samples."""
    if reusable:
        if not candidate.scope.cross_task_eligible or candidate.kind == "negative":
            raise ValueError("only fully scoped non-negative evidence is reusable")
        if candidate.status != "active":
            raise ValueError("reusable Android candidate must be active")
    elif candidate.status != "task_local":
        raise ValueError("local Android candidate must remain task-local")
    from ..android_ui_runtime.sanitizer import sanitize_text
    sanitized = sanitize_text(candidate.semantic_anchor, maximum=240)
    if (
        not candidate.semantic_anchor or len(candidate.semantic_anchor) > 240
        or sanitized.do_not_learn or sanitized.text != candidate.semantic_anchor
        or candidate.semantic_anchor in {"[redacted]", "[redacted-secret]"}
    ):
        raise ValueError("Android semantic anchor is invalid")
    if not re.fullmatch(r"scene:[a-f0-9]{32}", candidate.expected_scene_marker):
        raise ValueError("Android scene marker must be opaque")
    if any(not _OPAQUE_PROVENANCE.fullmatch(item) for item in candidate.provenance_ids):
        raise ValueError("Android candidate provenance must be opaque")
    if candidate.source_checkpoint_index < 1 or not _OPAQUE_DIGEST.fullmatch(candidate.source_observation_digest):
        raise ValueError("Android candidate source checkpoint is invalid")


def _row_candidate_is_safe(row: sqlite3.Row) -> bool:
    try:
        candidate = _android_candidate(row)
        _assert_android_candidate_safe(candidate, reusable=bool(row["reusable"]))
        return True
    except (TypeError, ValueError, KeyError):
        return False


def _episode(row: sqlite3.Row) -> ExperienceEpisode:
    return ExperienceEpisode(
        str(row["episode_id"]), str(row["goal_run_id"]), str(row["source_task_id"]),
        int(row["goal_spec_revision"]), _scope(row["scope_json"]),
        tuple(json.loads(row["frozen_criteria_json"])), row["terminal_outcome"],
        int(row["action_count"]), int(row["recovery_count"]),
        int(row["intervention_count"]), str(row["created_at"]), row["finished_at"],
    )


def _scene(row: sqlite3.Row) -> SceneState:
    return SceneState(
        str(row["scene_id"]), str(row["episode_id"]), str(row["observation_ref"]),
        str(row["integrity_hash"]), str(row["visual_fingerprint"]),
        str(row["semantic_label"]),
        tuple(json.loads(row["anchors_json"])), str(row["compatibility_key"]),
        float(row["confidence"]), str(row["created_at"]),
    )


def _transition(row: sqlite3.Row) -> ActionTransition:
    return ActionTransition(
        str(row["transition_id"]), str(row["episode_id"]), str(row["source_attempt_id"]),
        int(row["source_attempt_sequence"]), str(row["objective"]),
        str(row["before_scene_id"]), str(row["semantic_action"]), row["grounded_region"],
        row["coordinates_json"], str(row["expected_outcome"]),
        str(row["transport_status"]), row["after_scene_id"],
        str(row["immediate_outcome"]), row["failure_class"],
        row["recovery_of_transition_id"], str(row["created_at"]),
    )


def _signal(row: sqlite3.Row) -> OutcomeSignal:
    return OutcomeSignal(
        str(row["signal_id"]), str(row["episode_id"]), row["transition_id"],
        str(row["kind"]), str(row["source"]),
        tuple(json.loads(row["evidence_refs_json"])), float(row["confidence"]),
        str(row["reward_vector_json"]), str(row["created_at"]),
    )


def _candidate(row: sqlite3.Row) -> ExperienceCandidate:
    return ExperienceCandidate(
        str(row["candidate_id"]), _scope(row["scope_json"]), str(row["kind"]),
        str(row["objective_matcher"]), str(row["scene_matcher"]),
        str(row["semantic_action"]), row["expected_next_scene"], row["recovery_action"],
        int(row["support_count"]), int(row["failure_count"]), float(row["confidence"]),
        tuple(json.loads(row["provenance_json"])), str(row["compatibility_key"]),
        str(row["status"]), str(row["created_at"]), str(row["updated_at"]),
    )


def _android_candidate(row: sqlite3.Row) -> AndroidExperienceCandidate:
    return AndroidExperienceCandidate(
        candidate_id=str(row["candidate_id"]), source_episode_id=str(row["source_episode_id"]),
        scope=_android_scope(str(row["scope_json"])),
        kind=str(row["kind"]), action_kind=str(row["action_kind"]),
        semantic_anchor=str(row["semantic_anchor"]),
        expected_scene_marker=str(row["expected_scene_marker"]),
        source_checkpoint_index=int(row["source_checkpoint_index"]),
        source_observation_digest=str(row["source_observation_digest"]),
        support_count=int(row["support_count"]), failure_count=int(row["failure_count"]),
        confidence=float(row["confidence"]), provenance_ids=tuple(json.loads(row["provenance_json"])),
        status=str(row["status"]), created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _same_android_candidate_fact(
    left: AndroidExperienceCandidate,
    right: AndroidExperienceCandidate,
) -> bool:
    """Compare immutable candidate facts while ignoring write timestamps."""

    return (
        left.candidate_id == right.candidate_id
        and left.source_episode_id == right.source_episode_id
        and left.scope == right.scope
        and left.kind == right.kind
        and left.action_kind == right.action_kind
        and left.semantic_anchor == right.semantic_anchor
        and left.expected_scene_marker == right.expected_scene_marker
        and left.source_checkpoint_index == right.source_checkpoint_index
        and left.source_observation_digest == right.source_observation_digest
        and left.provenance_ids == right.provenance_ids
    )


def _policy(row: sqlite3.Row) -> PolicyRevision:
    return PolicyRevision(
        str(row["policy_id"]), _scope(row["scope_json"]), int(row["revision"]),
        tuple(json.loads(row["candidate_ids_json"])), str(row["status"]),
        str(row["created_at"]), row["rollback_of_revision"],
    )


def _candidate_confidence(support: int, failures: int) -> float:
    return round((support + 1) / (support + failures + 2), 6)


def _hamming(left: str, right: str) -> int:
    if len(left) != len(right):
        return 10_000
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 10_000


def _text_similarity(left: str, right: str) -> float:
    left = "".join(left.casefold().split())
    right = "".join(right.casefold().split())
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    left_pairs = {left[index:index + 2] for index in range(max(1, len(left) - 1))}
    right_pairs = {right[index:index + 2] for index in range(max(1, len(right) - 1))}
    union = left_pairs | right_pairs
    return len(left_pairs & right_pairs) / len(union) if union else 0.0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
