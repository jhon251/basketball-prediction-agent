"""Unit tests for per-game derived statistics (explicit formulas)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.schemas.game import TeamGameStats
from basketball_agent.features.team.boxscore import derive

pytestmark = pytest.mark.unit

UTC = timezone.utc
GAME_DATE = datetime(2024, 11, 6, tzinfo=UTC)


def make_stats(team_id, **overrides) -> TeamGameStats:
    base = dict(
        game_id="g1", team_id=team_id, is_home=True, minutes=240, points=112,
        possessions=101.0, fg=40, fga=85, fg3=12, fg3a=30, ft=20, fta=24,
        orb=9, drb=33, trb=42, ast=25, tov=12, stl=8, blk=5, pf=19,
    )
    base.update(overrides)
    return TeamGameStats(**base)


LAL = make_stats("LAL")
BOS = make_stats("BOS", is_home=False, points=105, possessions=102.0,
                 fg=38, fga=88, fg3=13, fg3a=36, ft=16, fta=20,
                 orb=10, drb=30, trb=40, tov=14)


class TestDerive:
    def test_lal_derived_values(self):
        d = derive("g1", GAME_DATE, LAL, BOS)
        assert d.team_id == "LAL"
        assert d.opponent_id == "BOS"
        assert d.margin == pytest.approx(7.0)
        assert d.pace == pytest.approx(101.5)
        assert d.ortg == pytest.approx(112 * 100 / 101)
        assert d.drtg == pytest.approx(105 * 100 / 102)
        assert d.nrtg == pytest.approx(112 * 100 / 101 - 105 * 100 / 102)
        assert d.efg_pct == pytest.approx(46 / 85)
        assert d.ts_pct == pytest.approx(112 / (2 * (85 + 0.44 * 24)))
        assert d.fg3_pct == pytest.approx(12 / 30)
        assert d.ft_pct == pytest.approx(20 / 24)
        assert d.orb_pct == pytest.approx(9 / 39)
        assert d.drb_pct == pytest.approx(33 / 43)
        assert d.trb_pct == pytest.approx(42 / 82)
        assert d.tov_pct == pytest.approx(12 / 101)
        assert d.opp_tov_pct == pytest.approx(14 / 101)

    def test_away_side_mirrors_margin(self):
        d_home = derive("g1", GAME_DATE, LAL, BOS)
        d_away = derive("g1", GAME_DATE, BOS, LAL)
        assert d_home.margin == pytest.approx(-d_away.margin)
        assert d_away.nrtg == pytest.approx(-d_home.nrtg)

    def test_zero_attempt_guards(self):
        d = derive("g1", GAME_DATE, make_stats("LAL", fg3a=0), BOS)
        assert d.fg3_pct is None
        d2 = derive("g1", GAME_DATE, make_stats("LAL", fta=0), BOS)
        assert d2.ft_pct is None

    def test_zero_possessions_guards(self):
        d = derive("g1", GAME_DATE, make_stats("LAL", possessions=0.0), BOS)
        assert d.ortg is None
        assert d.nrtg is None
