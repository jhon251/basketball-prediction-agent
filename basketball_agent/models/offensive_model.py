"""Model C (offense side): expected points per possession.

Explicit multiplicative matchup model: a team's PPP against an opponent is
the league-average PPP scaled by the team's (shrunk) offensive rating and the
opponent's (shrunk) defensive rating, both relative to league averages.
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.features.base import LeagueFeatureSet, TeamFeatureSet
from basketball_agent.models._stats import clamp, shrink


@dataclass(frozen=True)
class OffenseConfig:
    shrinkage_k: float = 5.0
    min_ppp: float = 0.4
    max_ppp: float = 1.6


class OffensiveModel:
    """Expected points per possession for ``team`` against ``opp``."""

    def expected_ppp(
        self,
        team: TeamFeatureSet,
        opp: TeamFeatureSet,
        league: LeagueFeatureSet,
        config: OffenseConfig | None = None,
    ) -> float:
        cfg = config or OffenseConfig()
        if league.ortg is None or league.drtg is None:
            raise ValueError("league offensive/defensive rating baseline is missing")
        off = shrink(team.ortg, league.ortg, team.games_played, cfg.shrinkage_k)
        deff = shrink(opp.drtg, league.drtg, opp.games_played, cfg.shrinkage_k)
        ppp = (league.ortg / 100.0) * (off / league.ortg) * (deff / league.drtg)
        return clamp(ppp, cfg.min_ppp, cfg.max_ppp)
