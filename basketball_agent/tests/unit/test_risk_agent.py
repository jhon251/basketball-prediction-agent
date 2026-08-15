"""Unit tests for the Risk Agent (flags + confidence; never modifies results)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from basketball_agent.agents.risk_agent import RiskAgent
from basketball_agent.simulation.monte_carlo import MonteCarloRunner, SimulationRun
from basketball_agent.tests.unit.model_helpers import make_features, make_params, make_team

pytestmark = pytest.mark.unit


def assess(home=None, away=None, elo_prob=0.95, **feature_overrides):
    home = home or make_team("H", ortg=120.0, drtg=90.0)
    away = away or make_team("A", ortg=90.0, drtg=120.0)
    features = make_features(home, away, elo_win_prob_home=elo_prob)
    for key, value in feature_overrides.items():
        features = replace(features, **{key: value})
    params = make_params(home=home, away=away)
    mc = MonteCarloRunner(seed=42).run(params, n_simulations=1000)
    return features, params, mc, RiskAgent().assess(features, params, mc)


class TestRiskAgent:
    def test_clean_case_full_confidence(self):
        # near-symmetric moderate matchup: no flag condition is met
        home = make_team("H", ortg=108.0, drtg=102.0, pace=90.0)
        away = make_team("A", ortg=102.0, drtg=108.0, pace=90.0)
        features = make_features(home, away, elo_win_prob_home=0.72)
        params = make_params(home=home, away=away)
        mc = MonteCarloRunner(seed=42).run(params, n_simulations=1000)
        risk = RiskAgent().assess(features, params, mc)
        assert risk.risk_flags == ()
        assert risk.confidence_score == pytest.approx(100.0)

    def test_close_matchup_flagged(self):
        features, params, mc, risk = assess(home=make_team("H"), away=make_team("A"), elo_prob=0.5)
        assert any(f.code == "close_matchup" for f in risk.risk_flags)
        assert risk.confidence_score < 100.0

    def test_model_disagreement_flagged(self):
        # elo says 50%, simulation will be very confident for the strong home
        _, _, _, risk = assess(elo_prob=0.5)
        assert any(f.code == "model_disagreement" for f in risk.risk_flags)

    def test_high_variance_flagged(self):
        home = make_team("H", ortg=120.0, drtg=90.0)
        away = make_team("A", ortg=90.0, drtg=120.0)
        params = make_params(home=home, away=away)
        mc = MonteCarloRunner(seed=42).run(params, n_simulations=1000)
        features = make_features(home, away, elo_win_prob_home=0.98)
        # force a huge spread by hand-crafting a bimodal result set
        spread_runs = tuple(
            SimulationRun(home_score=200 + i % 2 * 100, away_score=0,
                          margin=200 + i % 2 * 100, total=200 + i % 2 * 100, winner="home")
            for i in range(1000)
        )
        fake = replace(mc, runs=spread_runs)
        risk = RiskAgent().assess(features, params, fake)
        assert any(f.code == "high_variance" for f in risk.risk_flags)

    def test_injury_uncertainty_flagged(self):
        _, _, _, risk = assess(home_available_share=0.6)
        flags = [f for f in risk.risk_flags if f.code == "injury_uncertainty"]
        assert flags and flags[0].severity == "HIGH"

    def test_data_uncertainty_flagged(self):
        _, _, _, risk = assess(home=make_team("H", games=4, ortg=120.0, drtg=90.0))
        assert any(f.code == "data_uncertainty" for f in risk.risk_flags)

    def test_fatigue_flagged(self):
        _, _, _, risk = assess(home_back_to_back=True)
        assert any(f.code == "fatigue_uncertainty" for f in risk.risk_flags)

    def test_shooting_dependence_flagged(self):
        _, _, _, risk = assess(home=make_team("H", ortg=120.0, drtg=90.0, p3a_share=0.5))
        assert any(f.code == "extreme_shooting_dependence" for f in risk.risk_flags)

    def test_confidence_bounded(self):
        # stack many penalties: worst case still >= min_confidence
        features, params, mc, risk = assess(
            home=make_team("H", games=2, ortg=90.0, drtg=120.0),
            away=make_team("A", games=2, ortg=120.0, drtg=90.0),
            elo_prob=0.05,
            home_available_share=0.4,
            away_available_share=0.4,
            home_back_to_back=True,
        )
        assert risk.confidence_score >= 10.0
        assert risk.confidence_score <= 100.0

    def test_risk_never_modifies_results(self):
        strong = make_team("H", ortg=120.0, drtg=90.0)
        weak = make_team("A", ortg=90.0, drtg=120.0)
        features, params, mc, risk = assess(home=strong, away=weak, elo_prob=0.5)
        # the risk assessment must be a pure observer
        assert mc.home_win_probability == pytest.approx(
            MonteCarloRunner(seed=42).run(make_params(home=strong, away=weak),
                                          n_simulations=1000).home_win_probability
        )
        assert risk.risk_flags  # flags exist, but results were untouched
