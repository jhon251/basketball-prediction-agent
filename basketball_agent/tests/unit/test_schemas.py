"""Schema-level validation tests: structural invariants live in pydantic.

Cross-field numeric anomalies are *recorded* by the validation checks (not
rejected here) so the Validation Agent can report them instead of losing them.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from basketball_agent.data.schemas.base import GameStatus, League, PlayerAvailabilityStatus
from basketball_agent.data.schemas.game import Game, PlayerAvailability, PlayerGameStats, TeamGameStats

pytestmark = pytest.mark.unit

UTC = timezone.utc


def make_game(**overrides) -> Game:
    base = dict(
        game_id="g1", league="NBA", season="2024-25",
        game_date=datetime(2024, 11, 6, tzinfo=UTC),
        home_team_id="LAL", away_team_id="BOS",
        home_score=110, away_score=100, status="FINAL",
    )
    base.update(overrides)
    return Game(**base)


def make_team_stats(**overrides) -> TeamGameStats:
    base = dict(
        game_id="g1", team_id="LAL", is_home=True, minutes=240, points=112,
        possessions=101.0, fg=40, fga=85, fg3=12, fg3a=30, ft=20, fta=24,
        orb=9, drb=33, trb=42, ast=25, tov=12, stl=8, blk=5, pf=19,
    )
    base.update(overrides)
    return TeamGameStats(**base)


def make_player_stats(**overrides) -> PlayerGameStats:
    base = dict(
        game_id="g1", player_id="LAL-1", team_id="LAL", minutes=36.0, points=30,
        fga=20, fg3a=8, fta=6, rebounds=8, assists=10,
        turnovers=3, steals=2, blocks=1, fouls=2,
    )
    base.update(overrides)
    return PlayerGameStats(**base)


def make_availability(**overrides) -> PlayerAvailability:
    base = dict(
        player_id="LAL-1", game_id="g1", status="QUESTIONABLE",
        availability_probability=0.5,
        source_time=datetime(2024, 11, 5, tzinfo=UTC),
    )
    base.update(overrides)
    return PlayerAvailability(**base)


class TestGameSchema:
    def test_valid_final_game(self):
        game = make_game()
        assert game.status is GameStatus.FINAL
        assert game.home_score == 110

    def test_final_game_requires_scores(self):
        with pytest.raises(ValidationError):
            make_game(home_score=None)

    def test_scheduled_game_forbids_scores(self):
        with pytest.raises(ValidationError):
            make_game(status="SCHEDULED")

    def test_scheduled_game_without_scores_ok(self):
        game = make_game(status="SCHEDULED", home_score=None, away_score=None)
        assert game.status is GameStatus.SCHEDULED

    def test_self_play_rejected(self):
        with pytest.raises(ValidationError):
            make_game(away_team_id="LAL")

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValidationError):
            make_game(game_date=datetime(2024, 11, 6))

    def test_negative_score_rejected(self):
        with pytest.raises(ValidationError):
            make_game(home_score=-1)

    def test_unknown_status_rejected(self):
        with pytest.raises(ValidationError):
            make_game(status="NOT_A_STATUS")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            make_game(vegas_line=-4.5)

    def test_league_coerced_to_enum(self):
        assert make_game().league is League.NBA


class TestTeamGameStatsSchema:
    def test_valid(self):
        ts = make_team_stats()
        assert ts.fg == 40

    @pytest.mark.parametrize("field", ["minutes", "points", "fga", "trb", "ast", "pf"])
    def test_negative_values_rejected(self, field):
        with pytest.raises(ValidationError):
            make_team_stats(**{field: -1})

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            make_team_stats(plus_minus=9)


class TestPlayerGameStatsSchema:
    @pytest.mark.parametrize("field", ["minutes", "points", "fga", "rebounds", "fouls"])
    def test_negative_values_rejected(self, field):
        with pytest.raises(ValidationError):
            make_player_stats(**{field: -1})

    def test_valid(self):
        assert make_player_stats().assists == 10


class TestPlayerAvailabilitySchema:
    def test_valid(self):
        a = make_availability()
        assert a.status is PlayerAvailabilityStatus.QUESTIONABLE

    @pytest.mark.parametrize("prob", [-0.1, 1.1])
    def test_probability_bounds(self, prob):
        with pytest.raises(ValidationError):
            make_availability(availability_probability=prob)

    def test_source_time_required(self):
        with pytest.raises(ValidationError):
            make_availability(source_time=None)

    def test_naive_source_time_rejected(self):
        with pytest.raises(ValidationError):
            make_availability(source_time=datetime(2024, 11, 5))

    def test_unknown_status_rejected(self):
        with pytest.raises(ValidationError):
            make_availability(status="DAY_TO_DAY")
