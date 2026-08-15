"""Spec §28: Simulation Sanity Tests (1-7)."""
from __future__ import annotations

import pytest

from basketball_agent.simulation.monte_carlo import MonteCarloRunner
from basketball_agent.simulation.player_state import PlayerProfile

from basketball_agent.tests.unit.model_helpers import make_league, make_params, make_team

pytestmark = pytest.mark.simulation


class TestSanityOneMirrorMatchup:
    """§28.1: identical teams -> ~50% / 50%."""

    def test_mirror_matchup_is_even(self):
        params = make_params(home=make_team("H"), away=make_team("A"), hca=0.0)
        result = MonteCarloRunner(seed=42).run(params, n_simulations=10000)
        assert result.home_win_probability == pytest.approx(0.5, abs=0.02)
        assert result.away_win_probability == pytest.approx(0.5, abs=0.02)


class TestSanityTwoDominantTeam:
    """§28.2: A >>> B -> A wins almost always."""

    def test_dominant_home_team_wins_almost_always(self):
        strong = make_team("H", ortg=120.0, drtg=90.0)
        weak = make_team("A", ortg=90.0, drtg=120.0)
        params = make_params(home=strong, away=weak, hca=0.0)
        result = MonteCarloRunner(seed=7).run(params, n_simulations=2000)
        assert result.home_win_probability > 0.9
        assert result.home_score.mean > result.away_score.mean + 20


class TestSanityThreePace:
    """§28.3: raising pace raises the total."""

    def test_higher_pace_raises_total(self):
        league = make_league(pace=100.0)
        slow_home = make_team("H", pace=90.0)
        slow_away = make_team("A", pace=90.0)
        fast_home = make_team("H", pace=110.0)
        fast_away = make_team("A", pace=110.0)
        slow = MonteCarloRunner(seed=5).run(
            make_params(home=slow_home, away=slow_away, league=league), n_simulations=2000
        )
        fast = MonteCarloRunner(seed=5).run(
            make_params(home=fast_home, away=fast_away, league=league), n_simulations=2000
        )
        assert fast.total.mean > slow.total.mean + 10
        assert fast.expected_pace if hasattr(fast, "expected_pace") else True


class TestSanityFourOffenseEfficiency:
    """§28.4: lower offensive efficiency -> fewer points."""

    def test_lower_offense_scores_less(self):
        league = make_league()
        opponent = make_team("A")
        good = make_team("H", ortg=120.0)
        bad = make_team("H", ortg=90.0)
        strong = MonteCarloRunner(seed=11).run(
            make_params(home=good, away=opponent, league=league), n_simulations=2000
        )
        weak = MonteCarloRunner(seed=11).run(
            make_params(home=bad, away=opponent, league=league), n_simulations=2000
        )
        assert strong.home_score.mean > weak.home_score.mean + 10


class TestSanityFiveCorePlayerEfficiency:
    """§28.5: raising a core player's efficiency raises the team's score."""

    def test_core_player_efficiency_raises_score(self):
        params = make_params(home=make_team("H"), away=make_team("A"), hca=0.0)
        base_roster = [PlayerProfile("star", "H", efficiency=1.0, minutes_share=4.0)]
        boosted_roster = [PlayerProfile("star", "H", efficiency=1.5, minutes_share=4.0)]
        runner = MonteCarloRunner(seed=13)
        base = runner.run(params, n_simulations=2000, home_roster=base_roster)
        boosted = runner.run(params, n_simulations=2000, home_roster=boosted_roster)
        assert boosted.home_score.mean > base.home_score.mean + 2


class TestSanitySixInjury:
    """§28.6: a star injury moves team strength in the correct direction."""

    def test_star_out_lowers_team_score(self):
        params = make_params(home=make_team("H"), away=make_team("A"), hca=0.0)
        healthy = [PlayerProfile("star", "H", efficiency=1.5, minutes_share=4.0)]
        injured = [PlayerProfile("star", "H", efficiency=1.5, minutes_share=4.0, available=False)]
        runner = MonteCarloRunner(seed=17)
        with_star = runner.run(params, n_simulations=2000, home_roster=healthy)
        without_star = runner.run(params, n_simulations=2000, home_roster=injured)
        assert with_star.home_score.mean > without_star.home_score.mean
        assert with_star.home_win_probability >= without_star.home_win_probability


class TestSanitySevenReproducibility:
    """§28.7: same seed -> identical results."""

    def test_same_seed_identical_results(self):
        params = make_params()
        r1 = MonteCarloRunner().run(params, n_simulations=1000, seed=2024)
        r2 = MonteCarloRunner().run(params, n_simulations=1000, seed=2024)
        assert r1 == r2

    def test_different_seed_changes_results(self):
        params = make_params()
        r1 = MonteCarloRunner().run(params, n_simulations=1000, seed=1)
        r2 = MonteCarloRunner().run(params, n_simulations=1000, seed=2)
        assert r1.runs != r2.runs
