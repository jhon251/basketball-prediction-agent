"""Model C (defense side): expected points allowed per possession.

Mirror image of the offensive model: a team's defensive expectation against
an opponent is the league-average PPP scaled by the team's (shrunk)
defensive rating and the opponent's (shrunk) offensive rating.
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.features.base import LeagueFeatureSet, TeamFeatureSet
from basketball_agent.models._stats import clamp, shrink


@dataclass(frozen=True)
class DefenseConfig:
    shrinkage_k: float = 5.0
    min_ppp: float = 0.4
    max_ppp: float = 1.6


class DefensiveModel:
    """Expected points per possession allowed by ``team`` against ``opp``."""

    def expected_ppp_allowed(
        self,
        team: TeamFeatureSet,
        opp: TeamFeatureSet,
        league: LeagueFeatureSet,
        config: DefenseConfig | None = None,
    ) -> float:
        cfg = config or DefenseConfig()
        if league.ortg is None or league.drtg is None:
            raise ValueError("league offensive/defensive rating baseline is missing")
        deff = shrink(team.drtg, league.drtg, team.games_played, cfg.shrinkage_k)
        off = shrink(opp.ortg, league.ortg, opp.games_played, cfg.shrinkage_k)
        ppp = (league.ortg / 100.0) * (deff / league.drtg) * (off / league.ortg)
        return clamp(ppp, cfg.min_ppp, cfg.max_ppp)
