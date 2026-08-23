"""Universal GoalRun compatibility facade."""

from .api import create_goal_router, goal_error_handler
from .domain import (
    CriterionAssessment,
    GoalCompletionAssessment,
    GoalSpecificationDraft,
    SuccessCriterion,
)
from .preflight import GoalPreflight, PreflightResult
from .production_repair import ProductionGoalRepairs
from .qwen import StructuredGoalModel
from .repair import GoalRepairManager, RepairApplyError
from .service import GoalService
from .store import SQLiteGoalStore
from .stzb_daily import (
    DailyChecklistItem,
    DailyChecklistSnapshot,
    SQLiteDailyChecklistStore,
    StzbDailyProgressController,
    STZB_DAILY_GOAL_FAMILY,
    normalize_goal_family,
)
from .stzb_daily_benchmark import DailyFixtureReport, run_stzb_daily_fixture_benchmark

__all__ = [
    "GoalPreflight",
    "GoalSpecificationDraft",
    "GoalCompletionAssessment",
    "CriterionAssessment",
    "SuccessCriterion",
    "StructuredGoalModel",
    "GoalRepairManager",
    "RepairApplyError",
    "GoalService",
    "PreflightResult",
    "ProductionGoalRepairs",
    "SQLiteGoalStore",
    "DailyChecklistItem",
    "DailyChecklistSnapshot",
    "SQLiteDailyChecklistStore",
    "StzbDailyProgressController",
    "STZB_DAILY_GOAL_FAMILY",
    "normalize_goal_family",
    "DailyFixtureReport",
    "run_stzb_daily_fixture_benchmark",
    "create_goal_router",
    "goal_error_handler",
]
