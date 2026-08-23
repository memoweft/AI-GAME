from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from ..mobile_agent import (
    ActionAttempt,
    MobileTaskState,
    PlanDraft,
    Subgoal,
    TaskProgressDirective,
)

from ..goal_families import (
    STZB_DAILY_GOAL_FAMILY,
    is_stzb_discovery_only_goal,
    normalize_goal_family,
)


def normalized_intent(goal: str, model_intent: dict[str, Any]) -> dict[str, Any]:
    result = dict(model_intent)
    family = normalize_goal_family(goal)
    if family is not None:
        result.update(
            {
                "goal_family": family,
                "application_id": "stzb",
                "repeatable": True,
                "dynamic_checklist_required": True,
                "autonomy_mode": "development_open",
            }
        )
    return result


ChecklistStatus = Literal["completed", "incomplete", "blocked", "uncertain"]
CoveragePhase = Literal["discovery", "final"]

_MULTI_SURFACE_MODE = "multi_surface_v1"
_BASE_REQUIRED_COVERAGE = frozenset({
    "task_major",
    "task_affairs",
    "task_reputation",
    "activity_start",
    "activity_middle",
    "activity_end",
    "activity_detail",
    "patrol",
})


@dataclass(frozen=True, slots=True)
class DailyChecklistItem:
    item_id: str
    title: str
    status: ChecklistStatus
    evidence: str

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.title.strip() or not self.evidence.strip():
            raise ValueError("daily checklist item fields must not be blank")
        if self.status not in {"completed", "incomplete", "blocked", "uncertain"}:
            raise ValueError("invalid daily checklist item status")


@dataclass(frozen=True, slots=True)
class DailyChecklistSnapshot:
    evidence_id: str
    date_label: str
    coverage_complete: bool
    items: tuple[DailyChecklistItem, ...]
    observed_at: str
    surface_id: str = "single_daily_list"
    coverage_start: bool = False
    coverage_end: bool = False

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.date_label.strip():
            raise ValueError("daily checklist snapshot identity must not be blank")
        if self.coverage_complete and not self.items:
            raise ValueError("a complete checklist view must contain items")
        if not self.surface_id.strip():
            raise ValueError("daily checklist surface id must not be blank")
        identifiers = [item.item_id for item in self.items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("daily checklist item ids must be unique per snapshot")


class SQLiteDailyChecklistStore:
    """Append-only daily observations with an immutable first complete checklist."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection(write=True) as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS stzb_daily_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL
                    );
                    INSERT OR IGNORE INTO stzb_daily_schema(singleton, version) VALUES (1, 4);
                    CREATE TABLE IF NOT EXISTS stzb_daily_snapshots (
                        goal_id TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        date_label TEXT NOT NULL,
                        coverage_complete INTEGER NOT NULL,
                        items_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        surface_id TEXT NOT NULL DEFAULT 'single_daily_list',
                        coverage_start INTEGER NOT NULL DEFAULT 0,
                        coverage_end INTEGER NOT NULL DEFAULT 0,
                        phase TEXT NOT NULL DEFAULT 'legacy',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(goal_id, evidence_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_stzb_daily_goal_time
                    ON stzb_daily_snapshots(goal_id, observed_at, evidence_id);
                    CREATE TABLE IF NOT EXISTS stzb_daily_scans (
                        goal_id TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        scanned_at TEXT NOT NULL,
                        PRIMARY KEY(goal_id, evidence_id)
                    );
                    CREATE TABLE IF NOT EXISTS stzb_daily_goal_modes (
                        goal_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        registered_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stzb_daily_task_bindings (
                        task_id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        original_goal TEXT NOT NULL,
                        bound_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stzb_daily_coverage (
                        goal_id TEXT NOT NULL,
                        coverage_key TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        phase TEXT NOT NULL CHECK (phase IN ('discovery', 'final')),
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY(goal_id, coverage_key, evidence_id, phase)
                    );
                    CREATE TABLE IF NOT EXISTS stzb_daily_item_statuses (
                        goal_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('completed', 'incomplete', 'blocked', 'uncertain')
                        ),
                        evidence TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        phase TEXT NOT NULL CHECK (phase IN ('discovery', 'final')),
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY(goal_id, item_id, evidence_id, phase)
                    );
                    CREATE TABLE IF NOT EXISTS stzb_daily_manifests (
                        goal_id TEXT PRIMARY KEY,
                        manifest_json TEXT NOT NULL,
                        frozen_at TEXT NOT NULL
                    );
                    """
                )
                version = connection.execute(
                    "SELECT version FROM stzb_daily_schema WHERE singleton = 1"
                ).fetchone()
                if version is None or int(version["version"]) not in {1, 2, 3, 4}:
                    raise RuntimeError("unsupported STZB daily checklist schema")
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(stzb_daily_snapshots)"
                    ).fetchall()
                }
                additions = {
                    "surface_id": "TEXT NOT NULL DEFAULT 'single_daily_list'",
                    "coverage_start": "INTEGER NOT NULL DEFAULT 0",
                    "coverage_end": "INTEGER NOT NULL DEFAULT 0",
                    "phase": "TEXT NOT NULL DEFAULT 'legacy'",
                }
                for name, definition in additions.items():
                    if name not in columns:
                        connection.execute(
                            f"ALTER TABLE stzb_daily_snapshots ADD COLUMN {name} {definition}"
                        )
                connection.execute(
                    "UPDATE stzb_daily_schema SET version = 4 WHERE singleton = 1"
                )
            self._initialized = True

    def register_multi_surface_goal(self, goal_id: str) -> None:
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO stzb_daily_goal_modes("
                "goal_id, mode, registered_at) VALUES (?, ?, ?)",
                (goal_id, _MULTI_SURFACE_MODE, _now()),
            )

    def bind_task(self, task_id: str, goal_id: str, original_goal: str) -> None:
        self.initialize()
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO stzb_daily_task_bindings("
                "task_id, goal_id, original_goal, bound_at) VALUES (?, ?, ?, ?)",
                (task_id, goal_id, original_goal, _now()),
            )

    def task_binding(self, task_id: str) -> tuple[str, str] | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT goal_id, original_goal FROM stzb_daily_task_bindings "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return (
            (str(row["goal_id"]), str(row["original_goal"]))
            if row is not None else None
        )

    def record_coverage(
        self,
        goal_id: str,
        coverage_key: str,
        evidence_id: str,
        *,
        phase: CoveragePhase,
    ) -> None:
        self.initialize()
        if phase not in {"discovery", "final"}:
            raise ValueError("invalid STZB coverage phase")
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO stzb_daily_coverage("
                "goal_id, coverage_key, evidence_id, phase, observed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (goal_id, coverage_key, evidence_id, phase, _now()),
            )

    def record_item_status(
        self,
        goal_id: str,
        item_id: str,
        status: ChecklistStatus,
        evidence: str,
        evidence_id: str,
        *,
        phase: CoveragePhase,
    ) -> None:
        self.initialize()
        if status not in {"completed", "incomplete", "blocked", "uncertain"}:
            raise ValueError("invalid STZB item status")
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO stzb_daily_item_statuses("
                "goal_id, item_id, status, evidence, evidence_id, phase, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (goal_id, item_id, status, evidence, evidence_id, phase, _now()),
            )

    def record(
        self,
        goal_id: str,
        snapshot: DailyChecklistSnapshot,
        *,
        phase: CoveragePhase | None = None,
    ) -> None:
        self.initialize()
        items = [
            {
                "item_id": item.item_id,
                "title": item.title,
                "status": item.status,
                "evidence": item.evidence,
            }
            for item in snapshot.items
        ]
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO stzb_daily_snapshots("
                "goal_id, evidence_id, date_label, coverage_complete, items_json, "
                "observed_at, surface_id, coverage_start, coverage_end, phase, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    goal_id,
                    snapshot.evidence_id,
                    snapshot.date_label,
                    int(snapshot.coverage_complete),
                    json.dumps(items, ensure_ascii=False, separators=(",", ":")),
                    snapshot.observed_at,
                    snapshot.surface_id,
                    int(snapshot.coverage_start),
                    int(snapshot.coverage_end),
                    phase or "legacy",
                    _now(),
                ),
            )

    def evidence_ids(self, goal_id: str) -> set[str]:
        self.initialize()
        with self._connection() as connection:
            return {
                str(row["evidence_id"])
                for row in connection.execute(
                    "SELECT evidence_id FROM stzb_daily_scans WHERE goal_id = ?",
                    (goal_id,),
                ).fetchall()
            }

    def mark_scanned(self, goal_id: str, evidence_ids: tuple[str, ...]) -> None:
        self.initialize()
        now = _now()
        with self._lock, self._connection(write=True) as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO stzb_daily_scans(goal_id, evidence_id, scanned_at) "
                "VALUES (?, ?, ?)",
                ((goal_id, evidence_id, now) for evidence_id in evidence_ids),
            )

    def freeze_if_complete(self, goal_id: str) -> dict[str, Any]:
        """Persist the first fully covered multi-surface manifest exactly once."""

        current = self.state(goal_id)
        candidate = current.get("candidate_manifest")
        missing = tuple((current.get("coverage") or {}).get("missing") or ())
        if current["state"] != "NOT_DISCOVERED" or candidate is None or missing:
            return current
        with self._lock, self._connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO stzb_daily_manifests("
                "goal_id, manifest_json, frozen_at) VALUES (?, ?, ?)",
                (
                    goal_id,
                    json.dumps(candidate, ensure_ascii=False, separators=(",", ":")),
                    _now(),
                ),
            )
        return self.state(goal_id)

    def state(self, goal_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM stzb_daily_snapshots WHERE goal_id = ? "
                "ORDER BY observed_at, evidence_id",
                (goal_id,),
            ).fetchall()
            manifest_row = connection.execute(
                "SELECT manifest_json FROM stzb_daily_manifests WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        snapshots = [_snapshot_payload(row) for row in rows]
        complete_views = sorted(
            [item for item in snapshots if item["coverage_complete"]]
            + _composite_views(snapshots),
            key=lambda item: (item["observed_at"], item["evidence_id"]),
        )
        mode, coverage_rows, status_rows = self._progress_rows(goal_id)
        if mode != _MULTI_SURFACE_MODE:
            return _legacy_checklist_state(snapshots, complete_views)
        persisted_manifest = (
            json.loads(str(manifest_row["manifest_json"]))
            if manifest_row is not None else None
        )
        candidate = persisted_manifest or _multi_surface_manifest(
            snapshots, phase="discovery"
        )
        required = set(_BASE_REQUIRED_COVERAGE)
        if candidate is not None:
            required.update(
                f"activity_detail:{item_id}"
                for item_id in candidate.get("activity_item_ids", ())
            )
        discovery_keys = {
            str(row["coverage_key"])
            for row in coverage_rows if str(row["phase"]) == "discovery"
        }
        missing = sorted(required - discovery_keys)
        coverage = {
            "required": sorted(required),
            "discovered": sorted(discovery_keys),
            "missing": missing,
        }
        if persisted_manifest is None:
            return {
                "state": "NOT_DISCOVERED",
                "frozen": None,
                "latest": candidate or (snapshots[-1] if snapshots else None),
                "candidate_manifest": candidate,
                "remaining_items": [],
                "final_verified": False,
                "deviation": None,
                "coverage": coverage,
            }
        frozen = _overlay_item_statuses(candidate, status_rows, phase="discovery")
        final_candidate = _multi_surface_manifest(snapshots, phase="final")
        latest = _overlay_item_statuses(
            final_candidate or frozen, status_rows, phase="final"
        )
        frozen_by_id = {item["item_id"]: item for item in frozen["items"]}
        frozen_ids = set(frozen_by_id)
        latest_by_id = {item["item_id"]: item for item in latest["items"]}
        latest_ids = set(latest_by_id)
        deviation = None
        if final_candidate is not None and latest_ids != frozen_ids:
            deviation = "final checklist item set differs from the frozen daily checklist"
        remaining = []
        for item_id in sorted(frozen_ids):
            current = latest_by_id.get(item_id)
            if current is None:
                remaining.append({
                    **frozen_by_id[item_id],
                    "status": "uncertain",
                    "evidence": "item missing from the latest complete-view claim",
                })
            elif current["status"] != "completed":
                remaining.append(current)
        final_keys = {
            str(row["coverage_key"])
            for row in coverage_rows if str(row["phase"]) == "final"
        }
        final_missing = sorted(required - final_keys)
        coverage["final_discovered"] = sorted(final_keys)
        coverage["final_missing"] = final_missing
        final_verified = (
            final_candidate is not None
            and latest["evidence_id"] != frozen["evidence_id"]
            and latest["date_label"] == frozen["date_label"]
            and deviation is None
            and not remaining
            and not final_missing
        )
        return {
            "state": "VERIFIED_COMPLETE" if final_verified else "FROZEN",
            "frozen": frozen,
            "latest": latest,
            "remaining_items": remaining,
            "final_verified": final_verified,
            "deviation": deviation,
            "coverage": coverage,
        }

    def _progress_rows(
        self, goal_id: str,
    ) -> tuple[str | None, list[sqlite3.Row], list[sqlite3.Row]]:
        with self._connection() as connection:
            mode_row = connection.execute(
                "SELECT mode FROM stzb_daily_goal_modes WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            coverage = connection.execute(
                "SELECT * FROM stzb_daily_coverage WHERE goal_id = ? "
                "ORDER BY observed_at, evidence_id",
                (goal_id,),
            ).fetchall()
            statuses = connection.execute(
                "SELECT * FROM stzb_daily_item_statuses WHERE goal_id = ? "
                "ORDER BY observed_at, evidence_id",
                (goal_id,),
            ).fetchall()
        return (
            str(mode_row["mode"]) if mode_row is not None else None,
            list(coverage),
            list(statuses),
        )

    def _connection(self, *, write: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection


class StzbDailyProgressController:
    """Serialize checklist extraction and manifest-driven replanning with MobileTask."""

    def __init__(
        self,
        store: SQLiteDailyChecklistStore,
        *,
        inspect_daily_checklist: Callable[
            [str, tuple[Any, ...]], tuple[DailyChecklistSnapshot, ...]
        ],
        is_execution_stage: Callable[[str], bool],
    ) -> None:
        self.store = store
        self.inspect_daily_checklist = inspect_daily_checklist
        self.is_execution_stage = is_execution_stage

    def bind(self, task_id: str, goal_run_id: str, goal: str) -> None:
        if normalize_goal_family(goal) != STZB_DAILY_GOAL_FAMILY:
            return
        self.store.register_multi_surface_goal(goal_run_id)
        self.store.bind_task(task_id, goal_run_id, goal)

    def after_attempt(
        self, state: MobileTaskState, attempt: ActionAttempt,
    ) -> None:
        binding = self.store.task_binding(state.task_id)
        if binding is None or attempt.after is None or attempt.verification is None:
            return
        goal_id, original_goal = binding
        before_state = self.store.state(goal_id)
        phase: CoveragePhase = (
            "final"
            if before_state["state"] in {"FROZEN", "VERIFIED_COMPLETE"}
            else "discovery"
        )
        subgoal = _attempt_subgoal(state, attempt)
        evidence_id = attempt.after.evidence_id
        evidence = attempt.verification.evidence
        if attempt.verification.satisfied:
            for key in _verified_surface_keys(subgoal, evidence):
                self.store.record_coverage(
                    goal_id, key, evidence_id, phase=phase,
                )
        scanned = self.store.evidence_ids(goal_id)
        should_inspect = (
            evidence_id not in scanned
            and (
                looks_like_daily_checklist_evidence(evidence)
                or _is_activity_boundary_subgoal(subgoal)
                or _is_activity_middle_subgoal(subgoal)
                or _is_daily_detail_subgoal(subgoal)
            )
        )
        snapshots: tuple[DailyChecklistSnapshot, ...] = ()
        if should_inspect:
            snapshots = self.inspect_daily_checklist(original_goal, (attempt.after,))
            normalized = []
            for snapshot in snapshots:
                if _is_activity_boundary_subgoal(subgoal):
                    coverage_start, coverage_end = _activity_boundary_flags(subgoal)
                    snapshot = replace(
                        snapshot,
                        date_label="current-daily-cycle",
                        coverage_complete=False,
                        items=tuple(
                            _canonical_activity_item(item)
                            for item in snapshot.items
                            if _is_explicit_activity_daily_item(item)
                        ),
                        surface_id="activity_carousel",
                        coverage_start=(
                            coverage_start and attempt.verification.satisfied
                        ),
                        coverage_end=(
                            coverage_end and attempt.verification.satisfied
                        ),
                    )
                elif _is_activity_middle_subgoal(subgoal):
                    snapshot = replace(
                        snapshot,
                        date_label="current-daily-cycle",
                        coverage_complete=False,
                        items=tuple(
                            _canonical_activity_item(item)
                            for item in snapshot.items
                            if _is_explicit_activity_daily_item(item)
                        ),
                        surface_id="activity_carousel",
                        coverage_start=False,
                        coverage_end=False,
                    )
                elif _is_daily_detail_subgoal(subgoal):
                    snapshot = replace(
                        snapshot,
                        coverage_complete=False,
                        surface_id="other",
                        coverage_start=False,
                        coverage_end=False,
                    )
                elif (
                    snapshot.items
                    and re.search(r"(?:事务|巡察)", subgoal)
                    and looks_like_daily_checklist_evidence(evidence)
                ):
                    snapshot = replace(snapshot, surface_id="migrated_affairs")
                self.store.record(goal_id, snapshot, phase=phase)
                if snapshot.surface_id == "activity_carousel":
                    if (
                        _is_activity_middle_subgoal(subgoal)
                        and attempt.verification.satisfied
                    ):
                        self.store.record_coverage(
                            goal_id, "activity_middle", evidence_id, phase=phase,
                        )
                    if snapshot.coverage_start:
                        self.store.record_coverage(
                            goal_id, "activity_start", evidence_id, phase=phase,
                        )
                    if snapshot.coverage_end:
                        self.store.record_coverage(
                            goal_id, "activity_end", evidence_id, phase=phase,
                        )
                normalized.append(snapshot)
            snapshots = tuple(normalized)
            self.store.mark_scanned(goal_id, (evidence_id,))
        elif evidence_id not in scanned:
            self.store.mark_scanned(goal_id, (evidence_id,))

        manifest_state = self.store.state(goal_id)
        candidate = manifest_state.get("candidate_manifest") or manifest_state.get("frozen")
        matched_items = _matched_manifest_items(
            candidate,
            subgoal=subgoal,
            evidence=evidence,
            target_description=_target_description(attempt),
        )
        if _is_daily_detail_subgoal(subgoal) and looks_like_daily_checklist_evidence(evidence):
            self.store.record_coverage(
                goal_id, "activity_detail", evidence_id, phase=phase,
            )
            status, status_evidence = _snapshot_status(snapshots, evidence)
            for item in matched_items:
                item_id = str(item["item_id"])
                self.store.record_coverage(
                    goal_id, f"activity_detail:{item_id}", evidence_id, phase=phase,
                )
                self.store.record_item_status(
                    goal_id,
                    item_id,
                    status,
                    status_evidence,
                    evidence_id,
                    phase=phase,
                )
        elif matched_items and _is_manifest_item_stage(subgoal):
            status, status_evidence = _snapshot_status(snapshots, evidence)
            for item in matched_items:
                self.store.record_item_status(
                    goal_id,
                    str(item["item_id"]),
                    status,
                    status_evidence,
                    evidence_id,
                    phase=phase,
                )
        self.store.freeze_if_complete(goal_id)

    def before_subgoal(
        self, state: MobileTaskState, subgoal: Subgoal,
    ) -> TaskProgressDirective | None:
        binding = self.store.task_binding(state.task_id)
        if binding is None or state.plan is None:
            return None
        goal_id, original_goal = binding
        checklist = self.store.freeze_if_complete(goal_id)
        description = subgoal.description
        if _is_final_completion_gate(description):
            if checklist["state"] == "VERIFIED_COMPLETE":
                return None
            return TaskProgressDirective(
                "fail",
                "冻结清单尚未通过不同新鲜画面的全界面复读；MobileTask 未声明完成。",
                error_code="daily_checklist_final_verification_incomplete",
            )
        if (
            checklist["state"] == "NOT_DISCOVERED"
            and _is_daily_detail_subgoal(description)
        ):
            candidate = checklist.get("candidate_manifest")
            missing = set((checklist.get("coverage") or {}).get("missing") or ())
            detail_missing = sorted(
                key for key in missing if key.startswith("activity_detail:")
            )
            candidate_titles = tuple(
                str(item.get("title") or "")
                for item in (candidate or {}).get("items", ())
                if str(item.get("title") or "")
            )
            is_specific_detail = any(
                title in description for title in candidate_titles
            )
            if candidate is not None and detail_missing and not is_specific_detail:
                details = _candidate_detail_stages(candidate, detail_missing)
                tail_index = state.active_subgoal_index
                while (
                    tail_index < len(state.plan.subgoals)
                    and _is_daily_detail_subgoal(
                        state.plan.subgoals[tail_index].description
                    )
                ):
                    tail_index += 1
                tail = tuple(
                    item.description for item in state.plan.subgoals[tail_index:]
                )
                return TaskProgressDirective(
                    "replace_plan",
                    "活动边界已形成候选清单；用真实候选替换序号详情占位。",
                    PlanDraft((*details, *tail)),
                )
        if _is_manifest_freeze_stage(description):
            if checklist["state"] == "NOT_DISCOVERED":
                candidate = checklist.get("candidate_manifest")
                missing = set((checklist.get("coverage") or {}).get("missing") or ())
                detail_missing = sorted(
                    key for key in missing if key.startswith("activity_detail:")
                )
                base_missing = sorted(missing - set(detail_missing))
                if candidate is not None and detail_missing and not base_missing:
                    details = _candidate_detail_stages(candidate, detail_missing)
                    tail = tuple(
                        item.description
                        for item in state.plan.subgoals[state.active_subgoal_index + 1:]
                    )
                    return TaskProgressDirective(
                        "replace_plan",
                        "活动边界已形成候选清单；逐候选补齐详情后再冻结。",
                        PlanDraft((*details, description, *tail)),
                    )
                return TaskProgressDirective(
                    "fail",
                    "每日清单尚未覆盖任务三页、活动左右边界、候选详情和巡察；未进入执行。",
                    error_code="daily_checklist_coverage_incomplete",
                )
            return self._manifest_plan_directive(original_goal, checklist)
        if self.is_execution_stage(description):
            if checklist["state"] not in {"FROZEN", "VERIFIED_COMPLETE"}:
                return TaskProgressDirective(
                    "fail",
                    "每日清单尚未冻结；执行前置门阻止了清单条目动作。",
                    error_code="daily_checklist_not_frozen",
                )
            if not _is_manifest_item_stage(description):
                return self._manifest_plan_directive(original_goal, checklist)
        return None

    def _manifest_plan_directive(
        self, original_goal: str, checklist: dict[str, Any],
    ) -> TaskProgressDirective:
        frozen = checklist.get("frozen") or {}
        items = tuple(frozen.get("items") or ())
        execution: tuple[str, ...] = ()
        discovery_only = is_stzb_discovery_only_goal(original_goal)
        if not discovery_only:
            execution = tuple(
                (
                    f"完成冻结清单条目《{item['title']}》的当前可行步骤，使屏幕明确显示"
                    "已完成、已领取或具体阻塞状态。"
                )
                for item in items
                if item.get("status") != "completed"
            )
        reread = tuple(
            stage
            for item in items
            for stage in (
                f"从新鲜活动轮播中重新定位并清晰显示每日候选《{item['title']}》卡片。",
                f"从新鲜画面打开每日候选《{item['title']}》详情，独立复读其最终状态。",
            )
        ) + (
            "从新鲜画面重新打开主要事宜页签，确认其每日身份与条目最终状态。",
            "从新鲜画面重新打开事务页签，确认其每日身份与条目最终状态。",
            "从新鲜画面重新打开名望页签，确认其每日身份与条目最终状态。",
            "从新鲜画面重新打开巡察入口，确认今日次数、条目和最终状态。",
            "从新鲜画面重新打开活动轮播并到达左侧边界，记录全部可见每日候选最终状态。",
            "从活动左侧边界向右移动一个有重叠的可见卡片组，记录中间视口全部每日候选最终状态。",
            "将活动轮播移动到右侧边界，记录全部可见每日候选最终状态。",
        )
        if not discovery_only:
            reread += (
                "从新鲜画面再次打开主要事宜页签作为清单完成收尾画面，确认其每日身份与条目最终状态。",
            )
        expanded = (*execution, *reread)
        if len(expanded) > 64:
            return TaskProgressDirective(
                "fail",
                "冻结清单展开后超过运行时 64 个可观察阶段；未执行。",
                error_code="daily_checklist_too_large",
            )
        return TaskProgressDirective(
            "replace_plan",
            "持久化多表面清单已冻结；按真实条目展开执行与独立复读。",
            PlanDraft(expanded),
        )


def looks_like_daily_checklist_evidence(evidence: str) -> bool:
    text = evidence.casefold()
    if re.search(
        r"(?:(?:每日|每天|今日)(?:进入|登录|招募|刷新|重置)|"
        r"(?:每日|每天|今日).{0,24}(?:可获得|可领取|增加.{0,6}次数)|"
        r"(?:第一日.{0,80}第十四日|逐日.{0,24}(?:登录|奖励|任务|刷新)))",
        text,
        re.IGNORECASE,
    ):
        return True
    if any(
        token in text
        for token in (
            "每天登录",
            "每日招募",
            "今日可获得",
            "每日刷新",
            "每天刷新",
            "巡察次数",
            "每日相关描述",
            "明确的每日/每天文案",
            "累计登录七天",
        )
    ):
        return True
    if any(
        token in text
        for token in (
            "未弹出任务",
            "未打开任务",
            "未进入任务",
            "未见任务列表",
            "并非任务",
            "不在任务",
            "task panel did not",
            "checklist is not visible",
        )
    ):
        return False
    return any(
        token in text
        for token in (
            "已展开任务面板",
            "已打开任务面板",
            "显示每日任务",
            "显示日常任务",
            "每日任务列表显示",
            "今日任务列表显示",
            "清单的全部条目",
            "任务条目及",
            "daily task",
            "daily checklist",
        )
    )


def _is_explicit_activity_daily_item(item: DailyChecklistItem) -> bool:
    """Keep only activity cards whose own text positively proves a daily cycle."""

    text = f"{item.title}\n{item.evidence}"
    if re.search(
        r"(?:(?:未|不|无|没有|并非|不是).{0,12}"
        r"(?:显示|出现|包含|含有|标注|写有)?.{0,12}(?:每日|每天|今日|日常)|"
        r"(?:每日|每天|今日|日常).{0,16}(?:未显示|未出现|不存在|不属于))",
        text,
        re.IGNORECASE,
    ):
        return False
    return re.search(
        r"(?:(?:每日|每天|今日|日常).{0,32}"
        r"(?:登录|招募|领取|获得|刷新|重置|任务|奖励|次数|积分|进入|完成)|"
        r"(?:登录|招募|领取|获得|刷新|重置|任务|奖励|次数|积分).{0,24}"
        r"(?:每日|每天|今日|日常))",
        text,
        re.IGNORECASE,
    ) is not None


def _canonical_activity_item(item: DailyChecklistItem) -> DailyChecklistItem:
    """Collapse a model's optional card tagline into one stable card identity."""

    title = item.title.strip()
    parts = re.split(r"\s*(?:[：:｜|/]|[—–-])\s*", title, maxsplit=1)
    if len(parts) == 2 and re.search(r"(?:每日|每天|今日|日常)", parts[1]):
        title = parts[0].strip()
    else:
        spaced = re.match(r"^(.{2,40}?)\s+(?=(?:每日|每天|今日|日常))", title)
        if spaced is not None:
            title = spaced.group(1).strip()
    if title == item.title:
        return item
    return DailyChecklistItem(
        checklist_item_id(title), title, item.status, item.evidence
    )


def _attempt_subgoal(state: MobileTaskState, attempt: ActionAttempt) -> str:
    if state.plan is None:
        return state.goal
    return next(
        (
            item.description
            for item in state.plan.subgoals
            if item.index == attempt.subgoal_index
        ),
        state.goal,
    )


def _verified_surface_keys(subgoal: str, evidence: str) -> tuple[str, ...]:
    del evidence
    keys = []
    for name, key in (
        ("主要事宜", "task_major"),
        ("事务", "task_affairs"),
        ("名望", "task_reputation"),
    ):
        if name in subgoal and re.search(
            r"(?:确认|判断|查看|复读).{0,32}(?:每日|每天|今日|周期|身份|状态)",
            subgoal,
        ):
            keys.append(key)
    if "巡察" in subgoal and re.search(
        r"(?:确认|查看|复读|记录).{0,32}(?:次数|条目|状态|今日|每日)",
        subgoal,
    ):
        keys.append("patrol")
    return tuple(keys)


def _is_activity_boundary_subgoal(subgoal: str) -> bool:
    return bool(
        re.search(r"(?:活动|轮播|卡片)", subgoal)
        and re.search(
            r"(?:左侧边界|右侧边界|最左|最右|左边界|右边界|起止边界|左右边界)",
            subgoal,
        )
    )


def _is_activity_middle_subgoal(subgoal: str) -> bool:
    return bool(
        re.search(r"(?:活动|轮播|卡片)", subgoal)
        and re.search(r"(?:中间视口|中间画面|中部视口|有重叠|一个可见卡片组)", subgoal)
    )


def _activity_boundary_flags(subgoal: str) -> tuple[bool, bool]:
    start = re.search(r"(?:左侧边界|最左|左边界|起止边界|左右边界)", subgoal) is not None
    end = re.search(r"(?:右侧边界|最右|右边界|起止边界|左右边界)", subgoal) is not None
    return start, end


def _is_daily_detail_subgoal(subgoal: str) -> bool:
    return bool(
        "详情" in subgoal
        and re.search(r"(?:每日|每天|今日|每日候选|冻结清单条目)", subgoal)
    )


def _is_manifest_freeze_stage(subgoal: str) -> bool:
    return bool(
        re.search(r"(?:汇总|冻结|记录).{0,48}(?:完整|全部|全量)", subgoal)
        and re.search(r"(?:每日|日常|目标)?(?:清单|目标集|候选|条目)", subgoal)
    )


def _candidate_detail_stages(
    candidate: dict[str, Any], detail_missing: list[str],
) -> tuple[str, ...]:
    by_id = {
        str(item["item_id"]): item for item in candidate.get("items", ())
    }
    return tuple(
        stage
        for key in detail_missing
        if key.split(":", 1)[1] in by_id
        for title in (str(by_id[key.split(":", 1)[1]]["title"]),)
        for stage in (
            f"在精彩活动轮播中定位并清晰显示每日候选《{title}》卡片。",
            f"打开每日候选《{title}》的可见详情，确认其每日、每天或今日周期机制及当前条目状态。",
        )
    )


def _is_manifest_item_stage(subgoal: str) -> bool:
    return "《" in subgoal and "》" in subgoal and bool(
        re.search(r"(?:每日候选|冻结清单条目)", subgoal)
    )


def _is_final_completion_gate(subgoal: str) -> bool:
    return "作为清单完成收尾画面" in subgoal


def _target_description(attempt: ActionAttempt) -> str:
    intent = attempt.decision.intent
    if intent is None:
        return ""
    value = intent.arguments.get("target_description")
    return str(value) if isinstance(value, str) else ""


def _matched_manifest_items(
    candidate: dict[str, Any] | None,
    *,
    subgoal: str,
    evidence: str,
    target_description: str,
) -> tuple[dict[str, Any], ...]:
    if not candidate:
        return ()
    context = "\n".join((subgoal, evidence, target_description)).casefold()
    matches = tuple(
        item
        for item in candidate.get("items", ())
        if str(item.get("title") or "").casefold() in context
    )
    if matches:
        return matches
    marker = re.search(r"《([^》]+)》", subgoal)
    if marker is None:
        return ()
    title = marker.group(1).strip().casefold()
    return tuple(
        item
        for item in candidate.get("items", ())
        if str(item.get("title") or "").strip().casefold() == title
    )


def _snapshot_status(
    snapshots: tuple[DailyChecklistSnapshot, ...], evidence: str,
) -> tuple[ChecklistStatus, str]:
    items = tuple(item for snapshot in snapshots for item in snapshot.items)
    if items:
        statuses = {item.status for item in items}
        if statuses == {"completed"}:
            status: ChecklistStatus = "completed"
        elif "incomplete" in statuses:
            status = "incomplete"
        elif "blocked" in statuses:
            status = "blocked"
        else:
            status = "uncertain"
        detail = "; ".join(item.evidence for item in items)[:8_000]
        return status, detail
    if re.search(r"(?:已完成|已领取|全部完成|全部领取)", evidence):
        return "completed", evidence[:8_000]
    if re.search(r"(?:阻塞|时间未到|次数不足|条件不足|不可完成)", evidence):
        return "blocked", evidence[:8_000]
    if re.search(r"(?:未完成|未领取|\b0\s*/\s*\d+|\b1\s*/\s*\d+)", evidence):
        return "incomplete", evidence[:8_000]
    return "uncertain", evidence[:8_000]


def checklist_item_id(title: str) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.casefold())
    if not normalized:
        raise ValueError("daily checklist title has no stable content")
    return "daily_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _multi_surface_manifest(
    snapshots: list[dict[str, Any]], *, phase: CoveragePhase,
) -> dict[str, Any] | None:
    """Union the latest bounded view of each positive daily surface."""

    phase_snapshots = [item for item in snapshots if item.get("phase") == phase]
    complete_views = [
        item for item in phase_snapshots if item["coverage_complete"]
    ] + _composite_views(phase_snapshots)
    latest_by_surface: dict[str, dict[str, Any]] = {}
    for view in sorted(
        complete_views, key=lambda item: (item["observed_at"], item["evidence_id"])
    ):
        surface_id = str(view.get("surface_id") or "")
        if surface_id not in {
            "single_daily_list", "activity_carousel", "migrated_affairs",
        } or not view.get("items"):
            continue
        latest_by_surface[surface_id] = view
    if not latest_by_surface:
        return None
    items_by_id: dict[str, dict[str, Any]] = {}
    evidence_ids: list[str] = []
    activity_item_ids: set[str] = set()
    for surface_id in sorted(latest_by_surface):
        view = latest_by_surface[surface_id]
        for evidence_id in view.get("evidence_ids") or (view["evidence_id"],):
            if evidence_id not in evidence_ids:
                evidence_ids.append(str(evidence_id))
        for item in view.get("items", ()):
            item_id = str(item["item_id"])
            items_by_id[item_id] = dict(item)
            if surface_id == "activity_carousel":
                activity_item_ids.add(item_id)
    if not items_by_id:
        return None
    digest = hashlib.sha256(
        "\n".join(evidence_ids).encode("utf-8")
    ).hexdigest()[:24]
    observed_at = max(str(item["observed_at"]) for item in latest_by_surface.values())
    return {
        "evidence_id": f"multi_manifest_{digest}",
        "evidence_ids": evidence_ids,
        "date_label": "current-daily-cycle",
        "coverage_complete": True,
        "items": [items_by_id[item_id] for item_id in sorted(items_by_id)],
        "observed_at": observed_at,
        "surface_id": "multi_surface_manifest",
        "coverage_start": True,
        "coverage_end": True,
        "activity_item_ids": sorted(activity_item_ids),
    }


def _legacy_checklist_state(
    snapshots: list[dict[str, Any]],
    complete_views: list[dict[str, Any]],
) -> dict[str, Any]:
    if not complete_views:
        return {
            "state": "NOT_DISCOVERED",
            "frozen": None,
            "latest": snapshots[-1] if snapshots else None,
            "remaining_items": [],
            "final_verified": False,
            "deviation": None,
        }
    frozen = complete_views[0]
    latest = complete_views[-1]
    frozen_by_id = {item["item_id"]: item for item in frozen["items"]}
    frozen_ids = set(frozen_by_id)
    latest_by_id = {item["item_id"]: item for item in latest["items"]}
    latest_ids = set(latest_by_id)
    deviation = (
        "final checklist item set differs from the frozen daily checklist"
        if latest_ids != frozen_ids else None
    )
    remaining = []
    for item_id in sorted(frozen_ids):
        current = latest_by_id.get(item_id)
        if current is None:
            remaining.append({
                **frozen_by_id[item_id],
                "status": "uncertain",
                "evidence": "item missing from the latest complete-view claim",
            })
        elif current["status"] != "completed":
            remaining.append(current)
    final_verified = (
        len(complete_views) >= 2
        and latest["evidence_id"] != frozen["evidence_id"]
        and latest["date_label"] == frozen["date_label"]
        and deviation is None
        and not remaining
    )
    return {
        "state": "VERIFIED_COMPLETE" if final_verified else "FROZEN",
        "frozen": frozen,
        "latest": latest,
        "remaining_items": remaining,
        "final_verified": final_verified,
        "deviation": deviation,
    }


def _overlay_item_statuses(
    view: dict[str, Any],
    rows: list[sqlite3.Row],
    *,
    phase: CoveragePhase,
) -> dict[str, Any]:
    latest_by_id: dict[str, sqlite3.Row] = {}
    for row in rows:
        if str(row["phase"]) == phase:
            latest_by_id[str(row["item_id"])] = row
    items = []
    for item in view["items"]:
        row = latest_by_id.get(str(item["item_id"]))
        items.append(
            {
                **item,
                "status": str(row["status"]),
                "evidence": str(row["evidence"]),
            }
            if row is not None else dict(item)
        )
    return {**view, "items": items}


def _snapshot_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "evidence_id": str(row["evidence_id"]),
        "evidence_ids": [str(row["evidence_id"])],
        "date_label": str(row["date_label"]),
        "coverage_complete": bool(row["coverage_complete"]),
        "items": json.loads(row["items_json"]),
        "observed_at": str(row["observed_at"]),
        "surface_id": str(row["surface_id"]),
        "coverage_start": bool(row["coverage_start"]),
        "coverage_end": bool(row["coverage_end"]),
        "phase": str(row["phase"]),
    }


def _composite_views(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build complete manifests only from bounded start-to-end surface walks."""

    active: dict[tuple[str, str], list[dict[str, Any]]] = {}
    manifests: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if snapshot["coverage_complete"]:
            continue
        surface_id = str(snapshot.get("surface_id") or "")
        date_label = str(snapshot.get("date_label") or "")
        if surface_id in {"", "single_daily_list", "other"}:
            continue
        key = (surface_id, date_label)
        if snapshot.get("coverage_start"):
            active[key] = []
        walk = active.get(key)
        if walk is None:
            continue
        walk.append(snapshot)
        if not snapshot.get("coverage_end"):
            continue
        items_by_id: dict[str, dict[str, Any]] = {}
        evidence_ids: list[str] = []
        for view in walk:
            evidence_id = str(view["evidence_id"])
            evidence_ids.append(evidence_id)
            for item in view.get("items", ()):  # latest visible status wins
                items_by_id[str(item["item_id"])] = {
                    **item,
                    "evidence": f"[{evidence_id}] {item['evidence']}",
                }
        if items_by_id:
            digest = hashlib.sha256("\n".join(evidence_ids).encode("ascii")).hexdigest()[:24]
            manifests.append({
                "evidence_id": f"manifest_{digest}",
                "evidence_ids": evidence_ids,
                "date_label": date_label,
                "coverage_complete": True,
                "items": [items_by_id[item_id] for item_id in sorted(items_by_id)],
                "observed_at": str(walk[-1]["observed_at"]),
                "surface_id": surface_id,
                "coverage_start": True,
                "coverage_end": True,
            })
        active.pop(key, None)
    return manifests


def _now() -> str:
    return datetime.now(UTC).isoformat()
