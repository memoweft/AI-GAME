from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..goal_families import STZB_DAILY_GOAL_FAMILY, normalize_goal_family


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

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.date_label.strip():
            raise ValueError("daily checklist snapshot identity must not be blank")
        if self.coverage_complete and not self.items:
            raise ValueError("a complete checklist view must contain items")
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
                    INSERT OR IGNORE INTO stzb_daily_schema(singleton, version) VALUES (1, 1);
                    CREATE TABLE IF NOT EXISTS stzb_daily_snapshots (
                        goal_id TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        date_label TEXT NOT NULL,
                        coverage_complete INTEGER NOT NULL,
                        items_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
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
                    """
                )
                version = connection.execute(
                    "SELECT version FROM stzb_daily_schema WHERE singleton = 1"
                ).fetchone()
                if version is None or int(version["version"]) != 1:
                    raise RuntimeError("unsupported STZB daily checklist schema")
            self._initialized = True

    def record(self, goal_id: str, snapshot: DailyChecklistSnapshot) -> None:
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
                "observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    goal_id,
                    snapshot.evidence_id,
                    snapshot.date_label,
                    int(snapshot.coverage_complete),
                    json.dumps(items, ensure_ascii=False, separators=(",", ":")),
                    snapshot.observed_at,
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

    def state(self, goal_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM stzb_daily_snapshots WHERE goal_id = ? "
                "ORDER BY observed_at, evidence_id",
                (goal_id,),
            ).fetchall()
        snapshots = [_snapshot_payload(row) for row in rows]
        complete_views = [item for item in snapshots if item["coverage_complete"]]
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
        deviation = None
        if latest_ids != frozen_ids:
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

    def _connection(self, *, write: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection


def checklist_item_id(title: str) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.casefold())
    if not normalized:
        raise ValueError("daily checklist title has no stable content")
    return "daily_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _snapshot_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "evidence_id": str(row["evidence_id"]),
        "date_label": str(row["date_label"]),
        "coverage_complete": bool(row["coverage_complete"]),
        "items": json.loads(row["items_json"]),
        "observed_at": str(row["observed_at"]),
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()
