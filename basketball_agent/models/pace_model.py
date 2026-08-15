"""Model D: expected game pace (possessions).

Multiplicative matchup model: expected possessions = league-average pace
scaled by both teams' (shrunk) paces relative to the league average.
Both teams share the same possessions count in V1 (end-of-quarter effects
are out of scope).
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.features.base import LeagueFeatureSet, TeamFeatureSet
from basketball_agent.models._stats import clamp, shrink


@dataclass(frozen=True)
class PaceConfig:
    shrinkage_k: float = 5.0
    min_pace: float = 40.0
    max_pace: float = 140.0


class PaceModel:
    def expected_pace(
        self,
        home: TeamFeatureSet,
        away: TeamFeatureSet,
        league: LeagueFeatureSet,
        config: PaceConfig | None = None,
    ) -> float:
        cfg = config or PaceConfig()
        if league.pace is None:
            raise ValueError("league pace baseline is missing")
        home_pace = shrink(home.pace, league.pace, home.games_played, cfg.shrinkage_k)
        away_pace = shrink(away.pace, league.pace, away.games_played, cfg.shrinkage_k)
        pace = league.pace * (home_pace / league.pace) * (away_pace / league.pace)
        return clamp(pace, cfg.min_pace, cfg.max_pace)
