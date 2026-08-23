"""Operator-safe U7 snapshot and controlled-restore commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .legacy_cutover import exercise_snapshot_restore, snapshot_legacy_mobile_tasks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-GAME Kernel cutover data helper")
    subcommands = parser.add_subparsers(dest="command", required=True)

    snapshot = subcommands.add_parser("snapshot")
    snapshot.add_argument("--source", required=True)
    snapshot.add_argument("--backup-dir", required=True)

    restore = subcommands.add_parser("restore-check")
    restore.add_argument("--snapshot", required=True)
    restore.add_argument("--restore-dir", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.command == "snapshot":
        path = snapshot_legacy_mobile_tasks(
            Path(arguments.source), Path(arguments.backup_dir)
        )
        result = {"snapshot": str(path.resolve())}
    else:
        result = exercise_snapshot_restore(
            Path(arguments.snapshot), Path(arguments.restore_dir)
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
