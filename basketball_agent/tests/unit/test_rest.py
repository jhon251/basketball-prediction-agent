"""Unit tests for schedule (rest) features."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game
from basketball_agent.features.schedule.rest import is_back_to_back, rest_days

pytestmark = pytest.mark.unit

UTC = timezone.utc
NOW = datetime(2024, 11, 20, tzinfo=UTC)


def make_game(game_id, home, away, game_date) -> Game:
    return Game(
        game_id=game_id, league=League.NBA, season="2024-25", game_date=game_date,
        home_team_id=home, away_team_id=away, home_score=100, away_score=90,
        status=GameStatus.FINAL,
    )


class TestRestDays:
    def test_rest_from_last_game(self):
        history = [
            make_game("g1", "LAL", "BOS", NOW - timedelta(days=10)),
            make_game("g2", "LAL", "GSW", NOW - timedelta(days=2)),
        ]
        assert rest_days("LAL", NOW, history) == pytest.approx(2.0)

    def test_ignores_games_after_target_date(self):
        history = [
            make_game("g1", "LAL", "BOS", NOW - timedelta(days=10)),
            make_game("g2", "LAL", "GSW", NOW + timedelta(days=1)),  # future: must be ignored
        ]
        assert rest_days("LAL", NOW, history) == pytest.approx(10.0)

    def test_team_without_history(self):
        assert rest_days("GSW", NOW, [make_game("g1", "LAL", "BOS", NOW - timedelta(days=1))]) is None

    def test_away_games_count(self):
        history = [make_game("g1", "BOS", "LAL", NOW - timedelta(days=3))]
        assert rest_days("LAL", NOW, history) == pytest.approx(3.0)


class TestBackToBack:
    def test_b2b_true(self):
        assert is_back_to_back(0.8) is True
        assert is_back_to_back(1.0) is True

    def test_b2b_false(self):
        assert is_back_to_back(2.0) is False

    def test_unknown_rest(self):
        assert is_back_to_back(None) is None
