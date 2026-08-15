"""Unit tests for one full game simulation."""
from __future__ import annotations

import random

import pytest

from basketball_agent.models.game_params import GamePredictionParams, TeamGameParams
from basketball_agent.models.score_model import PossessionParams
from basketball_agent.simulation.game_simulator import GameSimulator, SimulationConfig
from basketball_agent.simulation.player_state import PlayerProfile

from basketball_agent.tests.unit.model_helpers import make_params

pytestmark = pytest.mark.simulation


def scoreless_params(pace: float = 100.0) -> GamePredictionParams:
    """Params where nobody can ever score (pure turnovers) -> 0-0 ties."""
    possession = PossessionParams(
        p_tov=1.0, p_fta=0.0, p_fga=0.0, p3a_share=0.0,
        p2_pct=0.0, p3_pct=0.0, ft_pct=0.0, fta_per_event=2.0,
        orb_pct=0.0, expected_ppp=0.0,
    )
    team = TeamGameParams("H", expected_ppp=0.0, expected_possessions=pace, possession=possession)
    away = TeamGameParams("A", expected_ppp=0.0, expected_possessions=pace, possession=possession)
    return GamePredictionParams(
        game_id="g", as_of=None, home=team, away=away, expected_pace=pace,
        expected_margin=0.0, expected_total=0.0,
        elo_win_prob_home=None, strength_win_prob_home=0.5,
        provenance=None,
    )


class TestGameSimulator:
    def test_deterministic_given_seed(self):
        params = make_params()
        sim = GameSimulator()
        g1 = sim.simulate(params, random.Random(42))
        g2 = sim.simulate(params, random.Random(42))
        assert g1 == g2

    def test_scores_within_plausible_bounds(self):
        params = make_params()
        sim = GameSimulator()
        for seed in range(20):
            g = sim.simulate(params, random.Random(seed))
            assert 0 <= g.home_score <= 300
            assert 0 <= g.away_score <= 300
            assert g.total == g.home_score + g.away_score
            assert g.margin == g.home_score - g.away_score
            assert g.winner in ("home", "away", "tie")

    def test_possessions_track_expected_pace(self):
        params = make_params()  # league 100, teams 100 -> expected pace 100
        sim = GameSimulator()
        for seed in range(10):
            g = sim.simulate(params, random.Random(seed))
            assert 95 <= g.home_possessions + g.away_possessions <= 105

    def test_four_quarters_recorded(self):
        g = GameSimulator().simulate(make_params(), random.Random(1))
        assert len(g.quarters) == 4
        assert g.quarters[-1] == (g.home_score, g.away_score)
        assert g.overtime_quarters == 0

    def test_tie_goes_to_overtime_up_to_cap(self):
        params = scoreless_params()
        sim = GameSimulator(SimulationConfig(max_overtimes=3))
        g = sim.simulate(params, random.Random(0))
        assert g.home_score == 0 and g.away_score == 0
        assert g.winner == "tie"
        assert g.overtime_quarters == 3
        assert len(g.quarters) == 4 + 3

    def test_overtime_not_played_without_tie(self):
        g = GameSimulator().simulate(make_params(), random.Random(2))
        assert g.overtime_quarters == 0

    def test_player_minutes_distributed_by_share(self):
        params = make_params()
        roster = [
            PlayerProfile("p1", "H", minutes_share=1.0),
            PlayerProfile("p2", "H", minutes_share=3.0),
        ]
        g = GameSimulator().simulate(params, random.Random(5), home_roster=roster)
        m1 = g.player_minutes["p1"]
        m2 = g.player_minutes["p2"]
        assert m2 == pytest.approx(3.0 * m1, rel=1e-9)
        # each team plays ~48/2 regulation minutes spread over its players
        assert m1 + m2 == pytest.approx(24.0, abs=0.5)

    def test_roster_efficiency_adjusts_expected_score(self):
        params = make_params()
        sim = GameSimulator()
        rng_factory = lambda seed: random.Random(seed)
        # identical seeds; only the roster differs
        base = sim.simulate(params, rng_factory(42))
        boosted = sim.simulate(
            params, rng_factory(42),
            home_roster=[PlayerProfile("p1", "H", efficiency=1.2, minutes_share=1.0)],
        )
        assert boosted.home_score > base.home_score
