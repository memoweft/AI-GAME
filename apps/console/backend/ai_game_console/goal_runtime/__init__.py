"""Universal GoalRun compatibility facade."""

from .api import create_goal_router, goal_error_handler
from .domain import (
    CapabilityBindingPlan,
    CriterionAssessment,
    GoalCompletionAssessment,
    GoalSpecificationDraft,
    GoalNotification,
    SuccessCriterion,
)
from .preflight import GoalPreflight, PreflightResult
from .production_repair import ProductionGoalRepairs
from .qwen import StructuredGoalModel
from .repair import GoalRepairManager, RepairApplyError
from .routing import RouteDecision, decide_route
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
    "CapabilityBindingPlan",
    "GoalSpecificationDraft",
    "GoalNotification",
    "GoalCompletionAssessment",
    "CriterionAssessment",
    "SuccessCriterion",
    "StructuredGoalModel",
    "GoalRepairManager",
    "RepairApplyError",
    "RouteDecision",
    "decide_route",
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
