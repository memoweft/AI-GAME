"""Phase 7 cutover drill: read-only report of the legacy mobile-tasks archive.

Usage: python cutover_drill_db_report.py [db_path]

Defaults to the live archive (runtime/console/mobile-tasks.db). Pass the
snapshot path to verify data integrity against the pre-cutover snapshot.
Opens the database read-only (sqlite URI mode=ro); never writes.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

DEFAULT_DB = Path(r"F:\AI-GAME\runtime\console\mobile-tasks.db")


def report(db: Path) -> int:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        total = cur.execute("select count(*) from mobile_tasks").fetchone()[0]
        statuses = Counter(
            row[0] for row in cur.execute("select status from mobile_tasks")
        )
        inflight = [
            row[0]
            for row in cur.execute(
                "select task_id from mobile_tasks "
                "where status in ('queued','planning','running','stopping') "
                "order by task_id"
            )
        ]
        print(f"db={db}")
        print(f"total_tasks={total}")
        print(f"status_counts={dict(statuses)}")
        print(f"in_flight_count={len(inflight)}")
        for task_id in inflight:
            print(f"in_flight_task={task_id}")
    finally:
        con.close()
    return 0


def main() -> int:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    return report(db)


if __name__ == "__main__":
    sys.exit(main())
