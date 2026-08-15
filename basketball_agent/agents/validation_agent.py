"""Data Validation Agent.

Runs the full battery of data-quality checks over a parsed dataset.  It only
inspects and reports — it never modifies data and never produces predictions.
"""
from __future__ import annotations

from datetime import datetime, timezone

from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.validators.checks import ValidationReport


class DataValidationAgent:
    """Deterministic wrapper around the check battery.

    The clock is injectable so tests (and backtests) can validate a dataset
    "as of" an arbitrary moment instead of wall-clock now.
    """

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now

    @property
    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def validate(self, dataset: Dataset) -> ValidationReport:
        from basketball_agent.data.validators import checks  # local import avoids circularity

        report = ValidationReport()
        checks.check_duplicate_games(report, dataset.games)
        checks.check_game_dates(report, dataset.games, now=self.now)
        checks.check_team_stat_coverage(report, dataset.games, dataset.team_stats)
        checks.check_team_stat_consistency(report, dataset.team_stats)
        checks.check_team_stat_bounds(report, dataset.team_stats)
        checks.check_player_stat_bounds(report, dataset.player_stats)
        checks.check_game_score_vs_team_stats(report, dataset.games, dataset.team_stats)
        checks.check_availability_consistency(report, dataset.availability)
        checks.check_availability_temporal(
            report, dataset.availability, {g.game_id: g for g in dataset.games}
        )
        checks.check_referential_integrity(report, dataset)
        return report
