"""Unit tests for the DataValidationAgent end-to-end behavior."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from basketball_agent.agents.validation_agent import DataValidationAgent

pytestmark = pytest.mark.unit

EXPECTED_CHECKS = {
    "completeness.duplicate_games",
    "completeness.game_dates",
    "completeness.team_stat_coverage",
    "consistency.team_stats",
    "consistency.team_stat_bounds",
    "consistency.player_stats",
    "consistency.game_vs_team_stats",
    "consistency.availability",
    "temporal.availability_source_time",
    "referential.integrity",
}


class TestValidationAgent:
    def test_clean_dataset_passes(self, sample_dataset, validation_agent):
        report = validation_agent.validate(sample_dataset)
        assert not report.has_errors
        assert report.issues == []
        assert report.checks_run == EXPECTED_CHECKS

    def test_all_checks_ran(self, sample_dataset, validation_agent):
        report = validation_agent.validate(sample_dataset)
        assert report.checks_run == EXPECTED_CHECKS

    def test_duplicate_game_fails(self, sample_dataset, validation_agent):
        ds = replace(sample_dataset, games=sample_dataset.games + (sample_dataset.games[0],))
        report = validation_agent.validate(ds)
        assert report.has_errors
        assert any(i.check == "completeness.duplicate_games" for i in report.errors)

    def test_broken_box_score_fails(self, sample_dataset, validation_agent):
        broken = sample_dataset.team_stats[0].model_copy(update={"points": 999})
        ds = replace(sample_dataset, team_stats=(broken,) + sample_dataset.team_stats[1:])
        report = validation_agent.validate(ds)
        assert report.has_errors
        assert any(i.check == "consistency.team_stats" for i in report.errors)

    def test_future_availability_fails(self, sample_dataset, validation_agent):
        game = sample_dataset.games[0]
        bad = sample_dataset.availability[0].model_copy(
            update={"source_time": game.game_date + timedelta(hours=2)}
        )
        ds = replace(sample_dataset, availability=sample_dataset.availability + (bad,))
        report = validation_agent.validate(ds)
        assert report.has_errors
        assert any(i.check == "temporal.availability_source_time" for i in report.errors)

    def test_summary_counts(self, sample_dataset, validation_agent):
        # one ERROR (duplicate game) + one WARNING (player minutes > 48)
        games = sample_dataset.games + (sample_dataset.games[0],)
        overtime_line = sample_dataset.player_stats[0].model_copy(update={"minutes": 50.0})
        ds = replace(sample_dataset, games=games, player_stats=(overtime_line,) + sample_dataset.player_stats[1:])
        report = validation_agent.validate(ds)
        summary = report.summary()
        assert summary["errors"] >= 1
        assert summary["warnings"] >= 1
        assert summary["total_issues"] == summary["errors"] + summary["warnings"]

    def test_to_text_lists_issues(self, sample_dataset, validation_agent):
        ds = replace(sample_dataset, games=sample_dataset.games + (sample_dataset.games[0],))
        report = validation_agent.validate(ds)
        text = report.to_text()
        assert "[ERROR]" in text
        assert "completeness.duplicate_games" in text

    def test_to_text_empty(self, sample_dataset, validation_agent):
        report = validation_agent.validate(sample_dataset)
        assert report.to_text() == "No issues found."

    def test_default_clock_is_utc_aware(self):
        agent = DataValidationAgent()
        assert agent.now.tzinfo is not None

    def test_injectable_clock_is_used(self, sample_dataset, fixed_now):
        # A FINAL game dated after fixed_now must fail when the clock says so.
        game = sample_dataset.games[0].model_copy(
            update={"game_date": fixed_now + timedelta(days=30)}
        )
        ds = replace(sample_dataset, games=(game,))
        report = DataValidationAgent(now=fixed_now).validate(ds)
        assert any(i.check == "completeness.game_dates" for i in report.errors)
