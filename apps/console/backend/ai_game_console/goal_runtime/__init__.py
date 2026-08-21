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
    "create_goal_router",
    "goal_error_handler",
]
