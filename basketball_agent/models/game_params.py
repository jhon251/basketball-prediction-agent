"""Assemble Models A-E into the Phase-4 simulation input contract.

``build_game_params`` turns leakage-safe ``GameFeatures`` into
``GamePredictionParams``: expected PPP per team (Model C), expected pace
(Model D), expected margin / win probability (Model B) and per-team
possession-outcome distributions (Model E).  The Monte Carlo engine consumes
exactly this object — the models stay pure functions of the features, so
they inherit the feature layer's temporal-integrity guarantees.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from basketball_agent.features.base import FeatureProvenance, GameFeatures
from basketball_agent.models.offensive_model import OffenseConfig, OffensiveModel
from basketball_agent.models.pace_model import PaceConfig, PaceModel
from basketball_agent.models.score_model import PossessionParams, ScoreModel, ScoreModelConfig
from basketball_agent.models.win_probability import WinProbabilityConfig, WinProbabilityModel


@dataclass(frozen=True)
class TeamGameParams:
    """Per-team simulation parameters for one upcoming game."""

    team_id: str
    expected_ppp: float
    expected_possessions: float
    possession: PossessionParams


@dataclass(frozen=True)
class GamePredictionParams:
    """Everything the possession-level simulator needs for one game."""

    game_id: str
    as_of: datetime
    home: TeamGameParams
    away: TeamGameParams
    expected_pace: float
    expected_margin: float
    expected_total: float
    elo_win_prob_home: float | None
    strength_win_prob_home: float
    provenance: FeatureProvenance
    # League-specific regulation length (Phase 8-B3).  Default is the NBA's
    # 48 minutes so every pre-existing constructor keeps its behaviour; the
    # 40-minute FIBA leagues carry 2400.0 explicitly.
    regulation_seconds: float = 2880.0


# Regulation length per league (Phase 8-B3).  Unknown/legacy (None) defaults
# to the NBA value; extend this table when a new league is ingested.
LEAGUE_REGULATION_SECONDS: dict[str, float] = {
    "NBA": 2880.0,        # 4 x 12 min
    "EUROLEAGUE": 2400.0, # 4 x 10 min (FIBA)
    "ACB": 2400.0,        # 4 x 10 min (FIBA) — Phase 8-B6
    "NBL": 2400.0,        # 4 x 10 min (FIBA) — Phase 8-D
}


def build_game_params(
    features: GameFeatures,
    *,
    win_config: WinProbabilityConfig | None = None,
    pace_config: PaceConfig | None = None,
    score_config: ScoreModelConfig | None = None,
    offense_config: OffenseConfig | None = None,
) -> GamePredictionParams:
    """Assemble all model outputs from computed features.

    Home-court advantage is applied as total points over the game: the home
    team plays ``pace`` possessions, so the per-possession bonus is
    ``hca / pace``.
    """
    win_config = win_config or WinProbabilityConfig()
    pace_model = PaceModel()
    win_model = WinProbabilityModel()
    offense_model = OffensiveModel()
    score_model = ScoreModel()

    pace = pace_model.expected_pace(features.home, features.away, features.league, pace_config)

    home_ppp = offense_model.expected_ppp(features.home, features.away, features.league, offense_config)
    # Away PPP = away offense vs home defense (the defensive model is the
    # tested mirror image of the same formula).
    away_ppp = offense_model.expected_ppp(features.away, features.home, features.league, offense_config)

    home_ppp = home_ppp + win_config.home_court_advantage / pace

    margin = win_model.expected_margin(features.home, features.away, features.league, pace, win_config)
    win_prob = win_model.win_probability(margin, win_config)

    home_params = TeamGameParams(
        team_id=features.home_team_id,
        expected_ppp=home_ppp,
        expected_possessions=pace,
        possession=score_model.possession_params(
            features.home, features.away, features.league, home_ppp, score_config
        ),
    )
    away_params = TeamGameParams(
        team_id=features.away_team_id,
        expected_ppp=away_ppp,
        expected_possessions=pace,
        possession=score_model.possession_params(
            features.away, features.home, features.league, away_ppp, score_config
        ),
    )

    return GamePredictionParams(
        game_id=features.game_id,
        as_of=features.as_of,
        home=home_params,
        away=away_params,
        expected_pace=pace,
        expected_margin=margin,
        expected_total=(home_ppp + away_ppp) * pace,
        elo_win_prob_home=features.elo_win_prob_home,
        strength_win_prob_home=win_prob,
        provenance=features.provenance,
        regulation_seconds=LEAGUE_REGULATION_SECONDS.get(
            features.game_league or "NBA", 2880.0
        ),
    )
