"""Unit tests for the Elo model (explicit-formula baseline, Model A)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game
from basketball_agent.models.elo import (
    EloConfig,
    compute_ratings,
    expected_score,
    margin_multiplier,
    update_ratings,
)

pytestmark = pytest.mark.unit

UTC = timezone.utc


def make_game(game_id, home, away, home_score, away_score, game_date) -> Game:
    return Game(
        game_id=game_id, league=League.NBA, season="2024-25",
        game_date=game_date, home_team_id=home, away_team_id=away,
        home_score=home_score, away_score=away_score, status=GameStatus.FINAL,
    )


class TestExpectedScore:
    def test_equal_ratings_neutral_court_is_even(self):
        assert expected_score(1500.0, 1500.0, EloConfig(home_advantage=0.0)) == pytest.approx(0.5)

    def test_symmetry_on_neutral_court(self):
        cfg = EloConfig(home_advantage=0.0)
        a = expected_score(1620.0, 1490.0, cfg)
        b = expected_score(1490.0, 1620.0, cfg)
        assert a + b == pytest.approx(1.0)

    def test_home_advantage_raises_expectation(self):
        neutral = expected_score(1500.0, 1500.0, EloConfig(home_advantage=0.0))
        home = expected_score(1500.0, 1500.0, EloConfig(home_advantage=100.0))
        assert neutral == pytest.approx(0.5)
        assert home > neutral
        assert home == pytest.approx(0.640065, abs=1e-4)

    def test_higher_rating_increases_expectation(self):
        cfg = EloConfig(home_advantage=0.0)
        assert expected_score(1600.0, 1500.0, cfg) > 0.5


class TestMarginMultiplier:
    def test_increases_with_margin(self):
        assert margin_multiplier(20.0, 0.0) > margin_multiplier(2.0, 0.0)

    def test_decreases_with_elo_gap(self):
        assert margin_multiplier(10.0, 0.0) > margin_multiplier(10.0, 300.0)

    def test_strictly_positive(self):
        assert margin_multiplier(0.0, 0.0) > 0.0


class TestUpdateRatings:
    def test_zero_sum(self):
        cfg = EloConfig()
        res = update_ratings(1500.0, 1500.0, 110, 100, cfg)
        assert res.home_rating_after + res.away_rating_after == pytest.approx(3000.0)

    def test_winner_gains_loser_loses(self):
        cfg = EloConfig()
        res = update_ratings(1500.0, 1500.0, 105, 100, cfg)
        assert res.home_rating_after > 1500.0
        assert res.away_rating_after < 1500.0
        assert res.home_expected == pytest.approx(expected_score(1500.0, 1500.0, cfg))

    def test_upset_lowers_favorite(self):
        cfg = EloConfig()
        res = update_ratings(1600.0, 1400.0, 95, 110, cfg)
        assert res.home_rating_after < 1600.0
        assert res.away_rating_after > 1400.0


class TestComputeRatings:
    FIXTURE_GAMES = [
        make_game("g1", "LAL", "BOS", 112, 105, datetime(2024, 11, 6, tzinfo=UTC)),
        make_game("g2", "LAL", "GSW", 101, 98, datetime(2024, 11, 10, tzinfo=UTC)),
        make_game("g3", "GSW", "BOS", 95, 110, datetime(2024, 11, 15, tzinfo=UTC)),
    ]

    def test_sorts_chronologically_regardless_of_input_order(self):
        ratings, _ = compute_ratings(list(reversed(self.FIXTURE_GAMES)), EloConfig())
        assert ratings["BOS"] > ratings["LAL"] > ratings["GSW"]

    def test_hand_computed_ratings(self):
        # Values computed independently by hand with the documented formulas.
        ratings, _ = compute_ratings(self.FIXTURE_GAMES, EloConfig())
        assert ratings["LAL"] == pytest.approx(1509.242, abs=0.05)
        assert ratings["BOS"] == pytest.approx(1510.393, abs=0.05)
        assert ratings["GSW"] == pytest.approx(1480.365, abs=0.05)

    def test_deterministic(self):
        r1, res1 = compute_ratings(self.FIXTURE_GAMES, EloConfig())
        r2, res2 = compute_ratings(self.FIXTURE_GAMES, EloConfig())
        assert r1 == r2
        assert res1 == res2

    def test_skips_non_final_and_incomplete_games(self):
        scheduled = Game(
            game_id="gx", league=League.NBA, season="2024-25",
            game_date=datetime(2024, 11, 17, tzinfo=UTC),
            home_team_id="LAL", away_team_id="BOS",
            home_score=None, away_score=None, status=GameStatus.SCHEDULED,
        )
        ratings, results = compute_ratings(self.FIXTURE_GAMES + [scheduled], EloConfig())
        assert len(results) == 3
        assert all(r.game_id != "gx" for r in results)
