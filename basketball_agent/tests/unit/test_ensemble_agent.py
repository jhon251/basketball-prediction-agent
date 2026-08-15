"""Unit tests for the Ensemble Agent (trainable, not a simple average)."""
from __future__ import annotations

import pytest

from basketball_agent.agents.ensemble_agent import EnsembleAgent, EnsembleWeights
from basketball_agent.simulation.monte_carlo import MonteCarloRunner
from basketball_agent.tests.unit.model_helpers import make_features, make_params, make_team

pytestmark = pytest.mark.unit


class TestEnsembleWeights:
    def test_valid_weights(self):
        assert EnsembleWeights(0.2, 0.3, 0.5).w_sim == 0.5

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            EnsembleWeights(-0.1, 0.6, 0.5)

    def test_non_normalized_rejected(self):
        with pytest.raises(ValueError):
            EnsembleWeights(0.4, 0.4, 0.4)


class TestEnsembleAgent:
    def test_combine_is_weighted_average(self):
        agent = EnsembleAgent()
        combined = agent.combine_probs(0.5, 0.6, 0.8, EnsembleWeights(0.2, 0.3, 0.5))
        assert combined == pytest.approx(0.2 * 0.5 + 0.3 * 0.6 + 0.5 * 0.8)

    def test_default_weights_simulation_only(self):
        agent = EnsembleAgent()
        features = make_features(make_team("H"), make_team("A"), elo_win_prob_home=0.4)
        params = make_params(home=make_team("H"), away=make_team("A"))
        mc = MonteCarloRunner(seed=7).run(params, n_simulations=300)
        result = agent.predict(features, mc)
        assert result.prob_home == pytest.approx(result.p_sim)
        assert result.p_elo == pytest.approx(0.4)

    def test_probability_bounds(self):
        agent = EnsembleAgent(EnsembleWeights(1 / 3, 1 / 3, 1 / 3))
        features = make_features(make_team("H", ortg=120.0, drtg=90.0),
                                 make_team("A", ortg=90.0, drtg=120.0), elo_win_prob_home=0.9)
        params = make_params(home=make_team("H", ortg=120.0, drtg=90.0),
                             away=make_team("A", ortg=90.0, drtg=120.0))
        mc = MonteCarloRunner(seed=7).run(params, n_simulations=300)
        result = agent.predict(features, mc)
        assert 0.0 <= result.prob_home <= 1.0

    def test_deterministic(self):
        agent = EnsembleAgent(EnsembleWeights(0.3, 0.3, 0.4))
        f1 = make_features(make_team("H"), make_team("A"))
        f2 = make_features(make_team("H"), make_team("A"))
        assert agent.predict(f1).prob_home == pytest.approx(agent.predict(f2).prob_home)
