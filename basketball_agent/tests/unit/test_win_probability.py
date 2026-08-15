"""Unit tests for Model B (team strength -> margin -> win probability)."""
from __future__ import annotations

import pytest

from basketball_agent.models.win_probability import WinProbabilityConfig, WinProbabilityModel

from .model_helpers import make_league, make_team

pytestmark = pytest.mark.unit


class TestWinProbabilityModel:
    def test_strength_is_shrunk_net_rating(self):
        league = make_league()
        model = WinProbabilityModel()
        assert model.team_strength(make_team(nrtg=5.0, games=10), league,
                                   WinProbabilityConfig(shrinkage_k=0.0)) == pytest.approx(5.0)
        # (2*10 + 5*0)/7 = 20/7
        assert model.team_strength(make_team(nrtg=10.0, games=2), league,
                                   WinProbabilityConfig(shrinkage_k=5.0)) == pytest.approx(20 / 7)

    def test_expected_margin_formula(self):
        league = make_league()
        model = WinProbabilityModel()
        cfg = WinProbabilityConfig(shrinkage_k=0.0, home_court_advantage=2.5)
        margin = model.expected_margin(
            make_team("H", nrtg=5.0), make_team("A", nrtg=2.0), league, 100.0, cfg
        )
        # each team plays 50 possessions: 3.0 points/100poss * 0.5 + HCA
        assert margin == pytest.approx(1.5 + 2.5)

    def test_margin_scales_with_pace(self):
        league = make_league()
        model = WinProbabilityModel()
        cfg = WinProbabilityConfig(shrinkage_k=0.0, home_court_advantage=2.5)
        slow = model.expected_margin(make_team("H", nrtg=5.0), make_team("A", nrtg=2.0), league, 90.0, cfg)
        fast = model.expected_margin(make_team("H", nrtg=5.0), make_team("A", nrtg=2.0), league, 110.0, cfg)
        assert slow < fast

    def test_logistic_properties(self):
        model = WinProbabilityModel()
        assert model.win_probability(0.0) == pytest.approx(0.5)
        assert model.win_probability(10.0) > 0.7
        assert model.win_probability(-10.0) < 0.3
        m = 4.3127106
        assert model.win_probability(m) == pytest.approx(0.5926703, abs=1e-4)
        assert model.win_probability(m) + model.win_probability(-m) == pytest.approx(1.0)
