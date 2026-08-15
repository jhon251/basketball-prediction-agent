"""Direct unit tests for every individual validation check function."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.data.schemas.base import GameStatus, League, PlayerAvailabilityStatus
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.schemas.game import Game, PlayerAvailability, PlayerGameStats, TeamGameStats
from basketball_agent.data.validators import checks
from basketball_agent.data.validators.checks import Severity, ValidationReport

pytestmark = pytest.mark.unit

UTC = timezone.utc
NOW = datetime(2025, 2, 1, 12, 0, tzinfo=UTC)


def make_game(game_id="g1", **overrides) -> Game:
    base = dict(
        game_id=game_id, league=League.NBA, season="2024-25",
        game_date=datetime(2024, 11, 6, tzinfo=UTC),
        home_team_id="LAL", away_team_id="BOS",
        home_score=112, away_score=105, status=GameStatus.FINAL,
    )
    base.update(overrides)
    return Game(**base)


def make_ts(team_id="LAL", is_home=True, **overrides) -> TeamGameStats:
    base = dict(
        game_id="g1", team_id=team_id, is_home=is_home, minutes=240, points=112,
        possessions=101.0, fg=40, fga=85, fg3=12, fg3a=30, ft=20, fta=24,
        orb=9, drb=33, trb=42, ast=25, tov=12, stl=8, blk=5, pf=19,
    )
    base.update(overrides)
    return TeamGameStats(**base)


def make_ps(**overrides) -> PlayerGameStats:
    base = dict(
        game_id="g1", player_id="LAL-1", team_id="LAL", minutes=36.0, points=30,
        fga=20, fg3a=8, fta=6, rebounds=8, assists=10,
        turnovers=3, steals=2, blocks=1, fouls=2,
    )
    base.update(overrides)
    return PlayerGameStats(**base)


def make_av(**overrides) -> PlayerAvailability:
    base = dict(
        player_id="LAL-1", game_id="g1", status=PlayerAvailabilityStatus.QUESTIONABLE,
        availability_probability=0.5, source_time=datetime(2024, 11, 5, tzinfo=UTC),
    )
    base.update(overrides)
    return PlayerAvailability(**base)


class TestCompletenessChecks:
    def test_duplicate_game_flags_error(self):
        report = ValidationReport()
        checks.check_duplicate_games(report, [make_game(), make_game()])
        assert report.has_errors
        assert any(i.check == "completeness.duplicate_games" for i in report.errors)

    def test_unique_games_pass(self):
        report = ValidationReport()
        checks.check_duplicate_games(report, [make_game("g1"), make_game("g2")])
        assert not report.has_errors

    def test_final_game_in_future_flags_error(self):
        report = ValidationReport()
        future = make_game(game_date=NOW + timedelta(days=1))
        checks.check_game_dates(report, [future], now=NOW)
        assert report.has_errors

    def test_scheduled_game_in_future_passes(self):
        report = ValidationReport()
        future = make_game(game_date=NOW + timedelta(days=1), status=GameStatus.SCHEDULED,
                           home_score=None, away_score=None)
        checks.check_game_dates(report, [future], now=NOW)
        assert not report.has_errors

    def test_ancient_date_flags_error(self):
        report = ValidationReport()
        checks.check_game_dates(report, [make_game(game_date=datetime(1899, 1, 1, tzinfo=UTC))], now=NOW)
        assert report.has_errors

    def test_missing_box_score_for_final_game(self):
        report = ValidationReport()
        checks.check_team_stat_coverage(report, [make_game()], [make_ts()])
        assert any("missing away team box score" in i.message for i in report.errors)

    def test_box_score_for_team_not_in_game(self):
        report = ValidationReport()
        checks.check_team_stat_coverage(report, [make_game()], [make_ts(), make_ts("XXX", False)])
        assert report.has_errors


class TestTeamStatConsistencyChecks:
    def test_clean_stats_pass(self):
        report = ValidationReport()
        checks.check_team_stat_consistency(report, [make_ts()])
        assert not report.has_errors
        assert "consistency.team_stats" in report.checks_run

    def test_points_identity_mismatch(self):
        report = ValidationReport()
        checks.check_team_stat_consistency(report, [make_ts(points=113)])
        assert any("points (113)" in i.message for i in report.errors)

    @pytest.mark.parametrize("overrides", [
        dict(fg=86),      # FG > FGA
        dict(fg3=31),     # FG3 > FG3A
        dict(fg=11),      # FG3 > FG (fg3=12)
        dict(fg3a=86),    # FG3A > FGA
        dict(ft=25),      # FT > FTA
        dict(trb=41),     # ORB + DRB != TRB
        dict(points=300), # points exceed theoretical maximum
    ])
    def test_impossible_stat_combinations(self, overrides):
        report = ValidationReport()
        checks.check_team_stat_consistency(report, [make_ts(**overrides)])
        assert report.has_errors

    def test_minutes_out_of_range(self):
        report = ValidationReport()
        checks.check_team_stat_bounds(report, [make_ts(minutes=500)])
        assert report.has_errors

    def test_possessions_out_of_range(self):
        report = ValidationReport()
        checks.check_team_stat_bounds(report, [make_ts(possessions=500.0)])
        assert report.has_errors

    def test_bounds_clean_pass(self):
        report = ValidationReport()
        checks.check_team_stat_bounds(report, [make_ts()])
        assert not report.has_errors


class TestPlayerStatChecks:
    def test_minutes_over_60_is_error(self):
        report = ValidationReport()
        checks.check_player_stat_bounds(report, [make_ps(minutes=61.0)])
        assert report.has_errors

    def test_minutes_over_48_is_warning(self):
        report = ValidationReport()
        checks.check_player_stat_bounds(report, [make_ps(minutes=50.0)])
        assert not report.has_errors
        assert report.warnings
        assert report.warnings[0].severity is Severity.WARNING

    def test_points_exceed_theoretical_max(self):
        report = ValidationReport()
        checks.check_player_stat_bounds(report, [make_ps(points=100, fga=10, fta=0)])
        assert report.has_errors

    def test_clean_line_passes(self):
        report = ValidationReport()
        checks.check_player_stat_bounds(report, [make_ps()])
        assert not report.has_errors
        assert not report.warnings


class TestGameVsTeamStatsChecks:
    def test_score_mismatch(self):
        report = ValidationReport()
        checks.check_game_score_vs_team_stats(report, [make_game()], [make_ts(points=115)])
        assert report.has_errors

    def test_is_home_flag_contradiction(self):
        report = ValidationReport()
        checks.check_game_score_vs_team_stats(report, [make_game()], [make_ts("BOS", is_home=True)])
        assert any("is_home flag" in i.message for i in report.errors)

    def test_clean_pass(self):
        report = ValidationReport()
        checks.check_game_score_vs_team_stats(
            report, [make_game()], [make_ts(), make_ts("BOS", is_home=False, points=105)])
        assert not report.has_errors


class TestAvailabilityChecks:
    def test_out_with_positive_probability_warns(self):
        report = ValidationReport()
        checks.check_availability_consistency(
            report, [make_av(status=PlayerAvailabilityStatus.OUT, availability_probability=0.3)])
        assert report.warnings and not report.has_errors

    def test_available_below_one_warns(self):
        report = ValidationReport()
        checks.check_availability_consistency(
            report, [make_av(status=PlayerAvailabilityStatus.AVAILABLE, availability_probability=0.8)])
        assert report.warnings and not report.has_errors

    def test_clean_pass(self):
        report = ValidationReport()
        checks.check_availability_consistency(report, [make_av()])
        assert not report.has_errors and not report.warnings


class TestTemporalChecks:
    def test_source_time_after_game_fails(self):
        report = ValidationReport()
        game = make_game()
        a = make_av(source_time=game.game_date + timedelta(hours=1))
        checks.check_availability_temporal(report, [a], {game.game_id: game})
        assert report.has_errors
        assert any(i.check == "temporal.availability_source_time" for i in report.errors)

    def test_source_time_before_game_passes(self):
        report = ValidationReport()
        game = make_game()
        a = make_av(source_time=game.game_date - timedelta(days=1))
        checks.check_availability_temporal(report, [a], {game.game_id: game})
        assert not report.has_errors


class TestReferentialChecks:
    def _ds(self, **overrides) -> Dataset:
        base = dict(
            games=(make_game(),),
            team_stats=(make_ts(),),
            teams=(),
            players=(),
            availability=(),
            player_stats=(),
        )
        base.update(overrides)
        return Dataset(**base)

    def test_unknown_team_in_game(self):
        from basketball_agent.data.schemas.team import Team

        report = ValidationReport()
        checks.check_referential_integrity(
            report, self._ds(teams=(Team(team_id="LAL", league=League.NBA, name="Lakers", abbreviation="LAL"),)))
        assert report.has_errors

    def test_unknown_player_in_stats(self):
        report = ValidationReport()
        checks.check_referential_integrity(report, self._ds(player_stats=(make_ps(),)))
        assert report.has_errors  # player registry provided but empty -> unknown player

    def test_team_stat_references_unknown_game(self):
        report = ValidationReport()
        checks.check_referential_integrity(report, self._ds(games=(), team_stats=(make_ts(),)))
        assert any("references unknown game" in i.message for i in report.errors)

    def test_player_stat_team_not_in_game(self):
        report = ValidationReport()
        checks.check_referential_integrity(report, self._ds(player_stats=(make_ps(team_id="GSW"),)))
        assert any("did not play in game" in i.message for i in report.errors)

    def test_availability_references_unknown_game(self):
        report = ValidationReport()
        checks.check_referential_integrity(report, self._ds(availability=(make_av(game_id="g999"),)))
        assert any("references unknown game" in i.message for i in report.errors)

    def test_full_registry_clean_pass(self):
        from basketball_agent.data.schemas.player import Player
        from basketball_agent.data.schemas.team import Team

        report = ValidationReport()
        ds = self._ds(
            teams=(Team(team_id="LAL", league=League.NBA, name="Lakers", abbreviation="LAL"),
                   Team(team_id="BOS", league=League.NBA, name="Celtics", abbreviation="BOS")),
            players=(Player(player_id="LAL-1", league=League.NBA, name="P1", current_team_id="LAL"),),
            team_stats=(make_ts(), make_ts("BOS", is_home=False, points=105)),
            player_stats=(make_ps(),),
            availability=(make_av(),),
        )
        checks.check_referential_integrity(report, ds)
        assert not report.has_errors
