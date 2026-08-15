"""Tests for GamePredictionParams assembly (fixture-based, hand-computed)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.db.repository import Repository
from basketball_agent.features import FeaturePipeline
from basketball_agent.models.game_params import build_game_params
from basketball_agent.models.offensive_model import OffenseConfig
from basketball_agent.models.pace_model import PaceConfig
from basketball_agent.models.score_model import ScoreModelConfig
from basketball_agent.models.win_probability import WinProbabilityConfig

pytestmark = pytest.mark.unit

UTC = timezone.utc
AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=UTC)

# Hand-computed expectations below assume NO shrinkage (k=0).
RAW_CONFIGS = dict(
    win_config=WinProbabilityConfig(shrinkage_k=0.0),
    pace_config=PaceConfig(shrinkage_k=0.0),
    score_config=ScoreModelConfig(shrinkage_k=0.0),
    offense_config=OffenseConfig(shrinkage_k=0.0),
)


def make_params(db, raw: bool = False):
    features = FeaturePipeline(Repository(db)).compute("20241120_LAL_BOS", AS_OF)
    configs = RAW_CONFIGS if raw else {}
    return features, build_game_params(features, **configs)


class TestGamePredictionParams:
    def test_expected_pace(self, scheduled_target):
        _, params = make_params(scheduled_target, raw=True)
        assert params.expected_pace == pytest.approx(100.336134, abs=1e-5)

    def test_expected_ppps_with_home_court(self, scheduled_target):
        _, params = make_params(scheduled_target, raw=True)
        assert params.home.expected_ppp == pytest.approx(1.0902416, abs=1e-6)
        assert params.away.expected_ppp == pytest.approx(1.0463492, abs=1e-6)

    def test_expected_margin_and_strength_win_probability(self, scheduled_target):
        _, params = make_params(scheduled_target, raw=True)
        assert params.expected_margin == pytest.approx(4.3127096, abs=1e-5)
        assert params.strength_win_prob_home == pytest.approx(0.5926710, abs=1e-6)

    def test_expected_total(self, scheduled_target):
        _, params = make_params(scheduled_target, raw=True)
        assert params.expected_total == pytest.approx(214.3773, abs=1e-3)

    def test_possession_params_are_self_consistent(self, scheduled_target):
        _, params = make_params(scheduled_target)
        for side in (params.home, params.away):
            assert side.possession.probabilities_sum_to_one
            # implied PPP must match the Model C target (exact linear solve)
            assert side.possession.expected_ppp == pytest.approx(side.expected_ppp, abs=1e-9)
            assert side.expected_possessions == pytest.approx(params.expected_pace)

    def test_home_possession_hand_values(self, scheduled_target):
        _, params = make_params(scheduled_target, raw=True)
        p = params.home.possession
        assert p.p_tov == pytest.approx(0.1138804, abs=1e-5)
        assert p.p_fta == pytest.approx(0.0970385, abs=1e-5)
        assert p.p_fga == pytest.approx(0.7890810, abs=1e-5)
        assert p.p2_pct == pytest.approx(0.4751462, abs=1e-5)
        assert p.p3_pct == pytest.approx(0.3649208, abs=1e-5)

    def test_elo_win_prob_passthrough(self, scheduled_target):
        features, params = make_params(scheduled_target)
        assert params.elo_win_prob_home == pytest.approx(features.elo_win_prob_home)
        assert params.elo_win_prob_home is not None

    def test_provenance_passthrough(self, scheduled_target):
        features, params = make_params(scheduled_target)
        assert params.provenance == features.provenance
        assert params.as_of == AS_OF
        assert params.game_id == "20241120_LAL_BOS"

    def test_deterministic(self, scheduled_target):
        _, p1 = make_params(scheduled_target)
        _, p2 = make_params(scheduled_target)
        assert p1 == p2
