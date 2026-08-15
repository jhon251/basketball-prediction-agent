"""Helpers for building synthetic feature objects in model unit tests."""
from __future__ import annotations

from datetime import datetime, timezone

from basketball_agent.features.base import (
    FeatureProvenance,
    GameFeatures,
    LeagueFeatureSet,
    TeamFeatureSet,
)
from basketball_agent.models.game_params import GamePredictionParams, build_game_params
from basketball_agent.models.offensive_model import OffenseConfig
from basketball_agent.models.pace_model import PaceConfig
from basketball_agent.models.score_model import ScoreModelConfig
from basketball_agent.models.win_probability import WinProbabilityConfig

UTC = timezone.utc
AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=UTC)


def make_team(team_id: str = "T", games: int = 10, **overrides) -> TeamFeatureSet:
    base = dict(
        team_id=team_id,
        games_played=games,
        last_game_date=datetime(2024, 11, 1, tzinfo=UTC),
        elo=1500.0,
        ortg=105.0,
        drtg=105.0,
        nrtg=0.0,
        pace=100.0,
        efg_pct=0.5,
        ts_pct=0.55,
        fg3_pct=0.35,
        ft_pct=0.75,
        orb_pct=0.25,
        drb_pct=0.75,
        trb_pct=0.5,
        tov_pct=0.13,
        opp_tov_pct=0.13,
        fga_rate=0.85,
        fta_rate=0.20,
        p3a_share=0.40,
        p2_pct=0.50,
        form_last5_win_pct=0.5,
        form_last5_avg_margin=0.0,
        form_last10_win_pct=0.5,
        form_last10_avg_margin=0.0,
        season_win_pct=0.5,
        season_avg_margin=0.0,
    )
    base.update(overrides)
    # keep the net rating consistent with overridden ratings unless the
    # caller explicitly set it (strength models read nrtg)
    if "nrtg" not in overrides and ("ortg" in overrides or "drtg" in overrides):
        base["nrtg"] = base["ortg"] - base["drtg"]
    return TeamFeatureSet(**base)


def make_league(**overrides) -> LeagueFeatureSet:
    base = dict(
        ortg=105.0,
        drtg=105.0,
        nrtg=0.0,
        pace=100.0,
        efg_pct=0.5,
        ts_pct=0.55,
        fg3_pct=0.35,
        ft_pct=0.75,
        orb_pct=0.25,
        drb_pct=0.75,
        trb_pct=0.5,
        tov_pct=0.13,
        opp_tov_pct=0.13,
        fga_rate=0.85,
        fta_rate=0.20,
        p3a_share=0.40,
        p2_pct=0.50,
        n_team_games=100,
    )
    base.update(overrides)
    return LeagueFeatureSet(**base)


def make_features(
    home: TeamFeatureSet | None = None,
    away: TeamFeatureSet | None = None,
    league: LeagueFeatureSet | None = None,
    elo_win_prob_home: float = 0.5,
) -> GameFeatures:
    home = home or make_team("H")
    away = away or make_team("A")
    league = league or make_league()
    return GameFeatures(
        game_id="g",
        as_of=AS_OF,
        home_team_id=home.team_id,
        away_team_id=away.team_id,
        home=home,
        away=away,
        league=league,
        home_pace=home.pace,
        away_pace=away.pace,
        expected_pace=(home.pace + away.pace) / 2 if home.pace and away.pace else None,
        elo_win_prob_home=elo_win_prob_home,
        nrtg_diff=None,
        home_rest_days=None,
        away_rest_days=None,
        home_back_to_back=None,
        away_back_to_back=None,
        league_home_win_pct=0.5,
        home_available_share=1.0,
        away_available_share=1.0,
        provenance=FeatureProvenance(
            games_used=(),
            team_stats_used=0,
            availability_used=(),
            decay_lambda=0.0,
            elo_k=20.0,
            elo_home_advantage=100.0,
            elo_initial=1500.0,
        ),
    )


def make_params(
    home: TeamFeatureSet | None = None,
    away: TeamFeatureSet | None = None,
    league: LeagueFeatureSet | None = None,
    hca: float = 0.0,
) -> GamePredictionParams:
    """Build simulation params from synthetic team features (no shrinkage)."""
    return build_game_params(
        make_features(home, away, league),
        win_config=WinProbabilityConfig(shrinkage_k=0.0, home_court_advantage=hca),
        pace_config=PaceConfig(shrinkage_k=0.0),
        score_config=ScoreModelConfig(shrinkage_k=0.0),
        offense_config=OffenseConfig(shrinkage_k=0.0),
    )
