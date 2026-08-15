"""Helpers for building synthetic feature objects in model unit tests."""
from __future__ import annotations

from datetime import datetime, timezone

from basketball_agent.features.base import LeagueFeatureSet, TeamFeatureSet

UTC = timezone.utc


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
