from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .store import SQLiteGoalStore


Probe = Callable[[], dict[str, Any]]
ApplyRepair = Callable[[], dict[str, Any] | None]


class RepairApplyError(RuntimeError):
    def __init__(self, detail: str, *, applied: bool) -> None:
        super().__init__(detail)
        self.detail = detail
        self.applied = applied


class GoalRepairManager:
    """Runs a bounded repair once, with durable identity and post-check evidence."""

    def __init__(self, store: SQLiteGoalStore) -> None:
        self.store = store

    def run(
        self, goal_id: str, *, repair_key: str, component: str,
        readiness_probe: Probe, identity_probe: Probe, apply_repair: ApplyRepair,
    ) -> dict[str, Any]:
        # A replay is observational only. In particular, APPLYING is not
        # retried because the previous process may have mutated external state.
        existing = next(
            (item for item in self.store.repairs(goal_id)
             if item["repair_key"] == repair_key),
            None,
        )
        if existing is not None:
            return existing

        before = self._probe(readiness_probe, "readiness probe")
        if before.get("ready") is True:
            attempt, claimed = self.store.begin_repair(
                goal_id, repair_key=repair_key, component=component,
                identity={"confirmed": False, "not_required": True}, before=before,
            )
            if not claimed:
                return attempt
            return self.store.finish_repair(
                goal_id, repair_key=repair_key, state="VERIFIED", after=before,
                detail="component already ready; no mutation applied", applied=False,
            )

        identity = self._probe(identity_probe, "identity probe")
        attempt, claimed = self.store.begin_repair(
            goal_id, repair_key=repair_key, component=component,
            identity=identity, before=before,
        )
        if not claimed:
            return attempt
        if identity.get("confirmed") is not True:
            return self.store.finish_repair(
                goal_id, repair_key=repair_key, state="SKIPPED_IDENTITY", after=before,
                detail="component ownership was not confirmed; repair refused", applied=False,
            )

        try:
            result = apply_repair() or {}
        except RepairApplyError as error:
            return self.store.finish_repair(
                goal_id, repair_key=repair_key, state="FAILED", after=before,
                detail=error.detail, applied=error.applied,
            )
        except Exception as error:
            return self.store.finish_repair(
                goal_id, repair_key=repair_key, state="FAILED", after=before,
                detail=f"repair command failed: {type(error).__name__}", applied=True,
            )

        after = self._probe(readiness_probe, "post-repair readiness probe")
        verified = after.get("ready") is True
        detail = (
            str(result.get("detail") or "repair verified by post-check")
            if verified else
            "repair ran but post-check did not confirm readiness"
        )
        return self.store.finish_repair(
            goal_id, repair_key=repair_key,
            state="VERIFIED" if verified else "FAILED",
            after=after, detail=detail, applied=True,
        )

    @staticmethod
    def _probe(probe: Probe, label: str) -> dict[str, Any]:
        try:
            value = probe()
        except Exception as error:
            return {"ready": False, "probe_error": type(error).__name__, "probe": label}
        if not isinstance(value, dict):
            return {"ready": False, "probe_error": "invalid_result", "probe": label}
        return value
