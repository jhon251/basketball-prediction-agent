"""Statistical models: Elo, offense/defense, pace, win probability, score distribution.

Every model is an explicit formula over leakage-safe features; the assembled
``GamePredictionParams`` is the input contract for the Phase-4 simulator.
"""

from basketball_agent.models.defensive_model import DefenseConfig, DefensiveModel
from basketball_agent.models.elo import (
    EloConfig,
    EloResult,
    compute_ratings,
    expected_score,
    margin_multiplier,
    update_ratings,
)
from basketball_agent.models.game_params import (
    GamePredictionParams,
    TeamGameParams,
    build_game_params,
)
from basketball_agent.models.offensive_model import OffenseConfig, OffensiveModel
from basketball_agent.models.pace_model import PaceConfig, PaceModel
from basketball_agent.models.score_model import (
    PossessionParams,
    ScoreModel,
    ScoreModelConfig,
)
from basketball_agent.models.win_probability import WinProbabilityConfig, WinProbabilityModel

__all__ = [
    "DefenseConfig",
    "DefensiveModel",
    "EloConfig",
    "EloResult",
    "GamePredictionParams",
    "OffenseConfig",
    "OffensiveModel",
    "PaceConfig",
    "PaceModel",
    "PossessionParams",
    "ScoreModel",
    "ScoreModelConfig",
    "TeamGameParams",
    "WinProbabilityConfig",
    "WinProbabilityModel",
    "build_game_params",
    "compute_ratings",
    "expected_score",
    "margin_multiplier",
    "update_ratings",
]
