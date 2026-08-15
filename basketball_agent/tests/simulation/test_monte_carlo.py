"""Unit tests for the Monte Carlo runner and distribution statistics."""
from __future__ import annotations

import pytest

from basketball_agent.simulation.monte_carlo import (
    DistributionStats,
    MonteCarloRunner,
    compute_stats,
)

from basketball_agent.tests.unit.model_helpers import make_params

pytestmark = pytest.mark.simulation


class TestComputeStats:
    def test_known_distribution(self):
        values = list(range(1, 101))
        stats = compute_stats(values)
        assert stats.mean == pytest.approx(50.5)
        assert stats.median == pytest.approx(50.5)
        assert stats.p50 == pytest.approx(50.5)
        assert stats.p5 == pytest.approx(5.95)
        assert stats.p95 == pytest.approx(95.05)
        assert stats.p25 == pytest.approx(25.75)
        assert stats.p75 == pytest.approx(75.25)
        assert stats.count == 100
        assert stats.std > 0

    def test_constant_distribution(self):
        stats = compute_stats([7.0] * 10)
        assert stats.std == pytest.approx(0.0)
        assert stats.p5 == stats.p95 == pytest.approx(7.0)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            compute_stats([])


class TestMonteCarloRunner:
    def test_probabilities_sum_to_one(self):
        result = MonteCarloRunner(seed=42).run(make_params(), n_simulations=1000)
        total = result.home_win_probability + result.away_win_probability + result.tie_probability
        assert total == pytest.approx(1.0)
        assert result.n_simulations == 1000
        assert len(result.runs) == 1000

    def test_percentile_ordering(self):
        result = MonteCarloRunner(seed=1).run(make_params(), n_simulations=1000)
        for stats in (result.home_score, result.away_score, result.margin, result.total):
            assert stats.p5 <= stats.p25 <= stats.p50 <= stats.p75 <= stats.p95
            assert stats.count == 1000

    def test_runs_match_winner_probabilities(self):
        result = MonteCarloRunner(seed=2).run(make_params(), n_simulations=1000)
        home_wins = sum(1 for r in result.runs if r.winner == "home")
        assert home_wins / 1000 == pytest.approx(result.home_win_probability)

    def test_score_buckets(self):
        result = MonteCarloRunner(seed=3).run(make_params(), n_simulations=1000)
        assert result.home_score_buckets
        top = result.home_score_buckets[0]
        assert top.count > 0
        assert 0.0 < top.share <= 1.0
        low, high = top.label.split("-")
        assert int(high) - int(low) == 4
        # buckets sorted descending by count
        counts = [b.count for b in result.home_score_buckets]
        assert counts == sorted(counts, reverse=True)

    def test_reproducibility_same_seed(self):
        runner = MonteCarloRunner()
        r1 = runner.run(make_params(), n_simulations=500, seed=123)
        r2 = runner.run(make_params(), n_simulations=500, seed=123)
        assert r1.runs == r2.runs
        assert r1 == r2

    def test_different_seeds_differ(self):
        runner = MonteCarloRunner()
        r1 = runner.run(make_params(), n_simulations=500, seed=1)
        r2 = runner.run(make_params(), n_simulations=500, seed=2)
        assert r1.runs != r2.runs

    def test_default_n_simulations(self):
        from basketball_agent.simulation.monte_carlo import DEFAULT_N_SIMULATIONS

        assert DEFAULT_N_SIMULATIONS == 10000

    def test_invalid_n_rejected(self):
        with pytest.raises(ValueError):
            MonteCarloRunner().run(make_params(), n_simulations=0)

    def test_mean_near_expected_total(self):
        result = MonteCarloRunner(seed=9).run(make_params(), n_simulations=2000)
        assert result.total.mean == pytest.approx(result.total.median, abs=2.0)
