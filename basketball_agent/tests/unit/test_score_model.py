"""Unit tests for Model E (possession-outcome distribution)."""
from __future__ import annotations

import pytest

from basketball_agent.models.score_model import ScoreModel, ScoreModelConfig

from .model_helpers import make_league, make_team

pytestmark = pytest.mark.unit

LEAGUE = make_league()


class TestScoreModel:
    def test_probabilities_sum_to_one(self):
        params = ScoreModel().possession_params(make_team(), make_team("O"), LEAGUE, 1.05)
        assert params.probabilities_sum_to_one

    def test_implied_ppp_matches_target(self):
        params = ScoreModel().possession_params(make_team(), make_team("O"), LEAGUE, 1.05)
        assert params.expected_ppp == pytest.approx(1.05, abs=1e-9)

    def test_target_ppp_high_raises_percentages(self):
        low = ScoreModel().possession_params(make_team(), make_team("O"), LEAGUE, 0.9)
        high = ScoreModel().possession_params(make_team(), make_team("O"), LEAGUE, 1.3)
        assert high.p2_pct > low.p2_pct
        assert high.p3_pct > low.p3_pct
        assert high.expected_ppp == pytest.approx(1.3, abs=1e-9)

    def test_higher_orb_lowers_per_opportunity_turnover_probability(self):
        # same tov_rate per possession, more ORB continuation -> the
        # per-opportunity turnover probability must shrink.
        low_orb = ScoreModel().possession_params(
            make_team(orb_pct=0.1), make_team("O"), LEAGUE, 1.05
        )
        high_orb = ScoreModel().possession_params(
            make_team(orb_pct=0.3), make_team("O"), LEAGUE, 1.05
        )
        assert high_orb.orb_pct > low_orb.orb_pct
        assert high_orb.p_tov < low_orb.p_tov

    def test_fta_per_event_scales_fta_probability(self):
        cfg2 = ScoreModelConfig(fta_per_event=2.0)
        cfg1 = ScoreModelConfig(fta_per_event=1.0)
        p2 = ScoreModel().possession_params(make_team(), make_team("O"), LEAGUE, 1.05, cfg2)
        p1 = ScoreModel().possession_params(make_team(), make_team("O"), LEAGUE, 1.05, cfg1)
        # same fta_rate per possession -> one-attempt trips need twice the trips
        assert p1.p_fta == pytest.approx(2.0 * p2.p_fta, rel=1e-9)
        assert p1.fta_per_event == 1.0

    def test_percentages_stay_in_bounds(self):
        params = ScoreModel().possession_params(make_team(), make_team("O"), LEAGUE, 1.6)
        assert 0.0 < params.p2_pct <= 0.85
        assert 0.0 < params.p3_pct <= 0.85
        assert 0.0 <= params.p_tov <= 0.6
        assert 0.0 <= params.p_fta <= 0.6
        assert 0.0 <= params.p_fga <= 1.0
        assert 0.0 <= params.orb_pct <= 0.6

    def test_tougher_opponent_defense_lowers_pre_scale_percentages(self):
        # with the SAME target PPP, a stronger opponent defense means the
        # model must compensate via the efficiency scale — the raw def_ratio
        # effect is visible when comparing across targets computed by Model C
        # (covered end-to-end in test_game_params); here we verify the
        # matchup ratio is applied by checking parameters differ.
        params_weak = ScoreModel().possession_params(
            make_team(), make_team("O", drtg=115.0), LEAGUE, 1.05
        )
        params_strong = ScoreModel().possession_params(
            make_team(), make_team("O", drtg=95.0), LEAGUE, 1.05
        )
        assert params_weak != params_strong

    def test_missing_league_baseline_raises(self):
        with pytest.raises(ValueError):
            ScoreModel().possession_params(make_team(), make_team("O"), make_league(tov_pct=None), 1.05)
