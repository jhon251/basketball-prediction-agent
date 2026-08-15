"""Unit tests for Model C (offense/defense expected PPP)."""
from __future__ import annotations

import pytest

from basketball_agent.models.defensive_model import DefenseConfig, DefensiveModel
from basketball_agent.models.offensive_model import OffenseConfig, OffensiveModel

from .model_helpers import make_league, make_team

pytestmark = pytest.mark.unit


class TestOffensiveModel:
    def test_neutral_matchup_equals_league_ppp(self):
        league = make_league()
        ppp = OffensiveModel().expected_ppp(make_team(), make_team("O"), league)
        assert ppp == pytest.approx(1.05)

    def test_stronger_offense_raises_ppp(self):
        league = make_league()
        base = OffensiveModel().expected_ppp(make_team(), make_team("O"), league)
        strong = OffensiveModel().expected_ppp(make_team(ortg=115.0), make_team("O"), league)
        assert strong > base

    def test_worse_opponent_defense_raises_ppp(self):
        league = make_league()
        base = OffensiveModel().expected_ppp(make_team(), make_team("O"), league)
        weak = OffensiveModel().expected_ppp(make_team(), make_team("O", drtg=115.0), league)
        assert weak > base

    def test_hand_computed_value(self):
        league = make_league()
        team = make_team(ortg=110.0)
        opp = make_team("O", drtg=102.0)
        ppp = OffensiveModel().expected_ppp(team, opp, league, OffenseConfig(shrinkage_k=0.0))
        assert ppp == pytest.approx(1.05 * (110 / 105) * (102 / 105))

    def test_shrinkage_pulls_toward_mean(self):
        league = make_league()
        team = make_team(ortg=115.0, games=2)
        opp = make_team("O", games=20)
        raw = OffensiveModel().expected_ppp(team, opp, league, OffenseConfig(shrinkage_k=0.0))
        shrunk = OffensiveModel().expected_ppp(team, opp, league, OffenseConfig(shrinkage_k=5.0))
        # (2*115 + 5*105)/7 = 107.857 < 115
        assert shrunk < raw
        assert shrunk == pytest.approx(1.05 * (107.857142 / 105) * (105 / 105), rel=1e-6)

    def test_missing_league_baseline_raises(self):
        with pytest.raises(ValueError):
            OffensiveModel().expected_ppp(make_team(), make_team("O"), make_league(ortg=None))

    def test_clamping(self):
        league = make_league()
        ppp = OffensiveModel().expected_ppp(
            make_team(ortg=400.0), make_team("O"), league, OffenseConfig(shrinkage_k=0.0)
        )
        assert ppp == pytest.approx(1.6)


class TestDefensiveModel:
    def test_mirror_of_offensive_model(self):
        league = make_league()
        home = make_team("H", ortg=112.0, drtg=103.0)
        away = make_team("A", ortg=101.0, drtg=108.0)
        off_view = OffensiveModel().expected_ppp(away, home, league, OffenseConfig(shrinkage_k=0.0))
        def_view = DefensiveModel().expected_ppp_allowed(home, away, league, DefenseConfig(shrinkage_k=0.0))
        assert def_view == pytest.approx(off_view)

    def test_stronger_defense_lowers_points_allowed(self):
        league = make_league()
        base = DefensiveModel().expected_ppp_allowed(make_team(), make_team("O"), league)
        strong = DefensiveModel().expected_ppp_allowed(make_team(drtg=95.0), make_team("O"), league)
        assert strong < base
