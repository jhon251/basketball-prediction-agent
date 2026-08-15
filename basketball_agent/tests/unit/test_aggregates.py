"""Unit tests for time-decay aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.features.team.aggregates import (
    aggregate_team,
    decay_weights,
    weighted_mean,
)
from basketball_agent.features.team.boxscore import TeamGameDerived

pytestmark = pytest.mark.unit

UTC = timezone.utc
AS_OF = datetime(2025, 2, 1, 12, 0, tzinfo=UTC)


def make_row(game_date, **fields) -> TeamGameDerived:
    base = dict(
        game_id="g", game_date=game_date, team_id="T", opponent_id="O",
        points=100, points_allowed=95, margin=5.0,
        possessions=100.0, opp_possessions=100.0, pace=100.0,
        ortg=None, drtg=None, nrtg=None, efg_pct=None, ts_pct=None,
        fg3_pct=None, ft_pct=None, orb_pct=None, drb_pct=None, trb_pct=None,
        tov_pct=None, opp_tov_pct=None,
    )
    base.update(fields)
    return TeamGameDerived(**base)


class TestDecayWeights:
    def test_zero_lambda_is_uniform(self):
        dates = [AS_OF - timedelta(days=10), AS_OF - timedelta(days=1)]
        assert decay_weights(dates, AS_OF, 0.0) == [1.0, 1.0]

    def test_recent_games_weight_more(self):
        dates = [AS_OF - timedelta(days=10), AS_OF - timedelta(days=1)]
        w = decay_weights(dates, AS_OF, 0.1)
        assert w[1] > w[0]
        assert w[0] == pytest.approx(pow(2.718281828459045, -1.0), rel=1e-3)
        assert w[1] == pytest.approx(pow(2.718281828459045, -0.1), rel=1e-3)

    def test_negative_lambda_rejected(self):
        with pytest.raises(ValueError):
            decay_weights([AS_OF], AS_OF, -0.5)

    def test_clamps_future_dates_defensively(self):
        assert decay_weights([AS_OF + timedelta(days=1)], AS_OF, 0.5) == [1.0]


class TestWeightedMean:
    def test_basic(self):
        assert weighted_mean([10.0, 20.0], [1.0, 3.0]) == pytest.approx(17.5)

    def test_ignores_none(self):
        assert weighted_mean([10.0, None, 20.0], [1.0, 1.0, 1.0]) == pytest.approx(15.0)

    def test_all_none_is_none(self):
        assert weighted_mean([None, None], [1.0, 1.0]) is None

    def test_empty_is_none(self):
        assert weighted_mean([], []) is None


class TestAggregateTeam:
    def test_uniform_aggregation(self):
        rows = [
            make_row(AS_OF - timedelta(days=10), ortg=100.0, pace=100.0),
            make_row(AS_OF - timedelta(days=1), ortg=120.0, pace=104.0),
        ]
        agg = aggregate_team(rows, AS_OF, 0.0)
        assert agg["games_played"] == 2
        assert agg["ortg"] == pytest.approx(110.0)
        assert agg["pace"] == pytest.approx(102.0)
        assert agg["last_game_date"] == AS_OF - timedelta(days=1)

    def test_decay_prefers_recent(self):
        old = make_row(AS_OF - timedelta(days=30), ortg=90.0)
        new = make_row(AS_OF - timedelta(days=1), ortg=110.0)
        uniform = aggregate_team([old, new], AS_OF, 0.0)["ortg"]
        decayed = aggregate_team([old, new], AS_OF, 0.5)["ortg"]
        assert uniform == pytest.approx(100.0)
        assert decayed > uniform  # recent (110) dominates

    def test_missing_field_is_none(self):
        agg = aggregate_team([make_row(AS_OF - timedelta(days=1))], AS_OF, 0.0)
        assert agg["nrtg"] is None
