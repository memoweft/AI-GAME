from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrialMetrics:
    completed: bool
    actions: int
    wrong_scenes: int
    no_progress: int
    recoveries: int
    interventions: int = 0


@dataclass(frozen=True, slots=True)
class ColdWarmResult:
    cold: TrialMetrics
    warm: TrialMetrics
    action_reduction: float
    success_rate_change_points: float
    attributable_retrievals: int
    attributable_uses: int

    @property
    def meets_initial_threshold(self) -> bool:
        no_completion_regression = (not self.cold.completed) or self.warm.completed
        improvement = self.action_reduction >= 0.30 or self.success_rate_change_points >= 20
        return (
            no_completion_regression
            and improvement
            and self.warm.interventions == 0
            and self.attributable_retrievals > 0
            and self.attributable_uses > 0
        )


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteResult:
    scenarios: tuple[ColdWarmResult, ...]
    false_completions: int
    promoted_with_complete_provenance: int
    promoted_total: int
    repeated_known_wrong_actions: int
    known_wrong_opportunities: int
    recovered_known_wrong_scenes: int
    known_wrong_scenes: int

    @property
    def action_reduction(self) -> float:
        cold = sum(item.cold.actions for item in self.scenarios)
        warm = sum(item.warm.actions for item in self.scenarios)
        return 0.0 if cold == 0 else (cold - warm) / cold

    @property
    def meets_u4_thresholds(self) -> bool:
        wrong_repeat_rate = (
            0.0 if self.known_wrong_opportunities == 0
            else self.repeated_known_wrong_actions / self.known_wrong_opportunities
        )
        recovery_rate = (
            1.0 if self.known_wrong_scenes == 0
            else self.recovered_known_wrong_scenes / self.known_wrong_scenes
        )
        provenance_rate = (
            1.0 if self.promoted_total == 0
            else self.promoted_with_complete_provenance / self.promoted_total
        )
        return (
            len(self.scenarios) >= 3
            and all(item.warm.completed for item in self.scenarios)
            and self.false_completions == 0
            and provenance_rate == 1.0
            and wrong_repeat_rate < 0.05
            and recovery_rate >= 0.90
            and self.action_reduction >= 0.30
            and all(item.warm.interventions == 0 for item in self.scenarios)
            and sum(item.attributable_uses for item in self.scenarios) > 0
        )


def compare_trials(
    cold: TrialMetrics, warm: TrialMetrics, *,
    attributable_retrievals: int, attributable_uses: int
) -> ColdWarmResult:
    reduction = 0.0 if cold.actions == 0 else (cold.actions - warm.actions) / cold.actions
    success_change = (int(warm.completed) - int(cold.completed)) * 100.0
    return ColdWarmResult(
        cold, warm, reduction, success_change,
        attributable_retrievals, attributable_uses,
    )
