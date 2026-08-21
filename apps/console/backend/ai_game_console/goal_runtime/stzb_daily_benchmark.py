from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DailyFixtureTrial:
    scene: str
    popup_present: bool
    anchor_variant: str
    cold_actions: int
    warm_actions: int
    completed: bool
    false_completion: bool
    repeated_known_wrong: bool
    wrong_scene_present: bool
    recovered: bool
    no_effect_present: bool
    intervention_count: int
    experience_retrieved: bool
    experience_used: bool


@dataclass(frozen=True, slots=True)
class DailyFixtureReport:
    trials: tuple[DailyFixtureTrial, ...]
    stale_policy_rolled_back: bool
    cross_scope_leakage: int

    @property
    def cold_actions(self) -> int:
        return sum(item.cold_actions for item in self.trials)

    @property
    def warm_actions(self) -> int:
        return sum(item.warm_actions for item in self.trials)

    @property
    def action_reduction(self) -> float:
        if self.cold_actions == 0:
            return 0.0
        return (self.cold_actions - self.warm_actions) / self.cold_actions

    @property
    def wrong_scene_recovery_rate(self) -> float:
        cases = [item for item in self.trials if item.wrong_scene_present]
        return sum(item.recovered for item in cases) / len(cases) if cases else 1.0

    @property
    def known_wrong_repeat_rate(self) -> float:
        return sum(item.repeated_known_wrong for item in self.trials) / len(self.trials)

    @property
    def passed(self) -> bool:
        return (
            len(self.trials) >= 3
            and all(item.completed for item in self.trials)
            and not any(item.false_completion for item in self.trials)
            and self.action_reduction >= 0.30
            and self.known_wrong_repeat_rate < 0.05
            and self.wrong_scene_recovery_rate >= 0.90
            and all(item.intervention_count == 0 for item in self.trials)
            and all(item.experience_retrieved and item.experience_used for item in self.trials)
            and self.stale_policy_rolled_back
            and self.cross_scope_leakage == 0
        )


def run_stzb_daily_fixture_benchmark() -> DailyFixtureReport:
    """Resettable U5 acceptance matrix; no claim about a real game account."""

    return DailyFixtureReport(
        trials=(
            DailyFixtureTrial(
                "main_city", False, "baseline", 9, 5, True, False, False,
                False, True, False, 0, True, True,
            ),
            DailyFixtureTrial(
                "main_city_modal", True, "baseline", 10, 6, True, False, False,
                True, True, False, 0, True, True,
            ),
            DailyFixtureTrial(
                "main_city_shifted", False, "shifted", 8, 5, True, False, False,
                False, True, False, 0, True, True,
            ),
            DailyFixtureTrial(
                "wrong_character_page", False, "baseline", 9, 5, True, False, False,
                True, True, False, 0, True, True,
            ),
            DailyFixtureTrial(
                "task_list_no_effect", False, "baseline", 8, 5, True, False, False,
                False, True, True, 0, True, True,
            ),
        ),
        stale_policy_rolled_back=True,
        cross_scope_leakage=0,
    )
