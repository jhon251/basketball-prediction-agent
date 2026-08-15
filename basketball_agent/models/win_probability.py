"""Model B: team strength -> expected margin -> win probability.

Team strength is the (shrunk) net rating.  Expected margin is the strength
difference scaled by expected pace plus a home-court advantage, and the win
probability is the logistic transform of that margin.

All constants are explicit parameters flagged for backtest calibration
(Phase 5); nothing here is learned from a black box or guessed by an LLM.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from basketball_agent.features.base import LeagueFeatureSet, TeamFeatureSet
from basketball_agent.models._stats import shrink


@dataclass(frozen=True)
class WinProbabilityConfig:
    shrinkage_k: float = 5.0
    home_court_advantage: float = 2.5   # points; calibrate in backtest
    margin_logistic_scale: float = 11.5  # points per logit; calibrate in backtest


class WinProbabilityModel:
    def team_strength(
        self,
        team: TeamFeatureSet,
        league: LeagueFeatureSet,
        config: WinProbabilityConfig | None = None,
    ) -> float:
        """Team strength score = shrunk net rating (points per 100 poss)."""
        cfg = config or WinProbabilityConfig()
        return shrink(team.nrtg, league.nrtg or 0.0, team.games_played, cfg.shrinkage_k)

    def expected_margin(
        self,
        home: TeamFeatureSet,
        away: TeamFeatureSet,
        league: LeagueFeatureSet,
        expected_pace: float,
        config: WinProbabilityConfig | None = None,
    ) -> float:
        """Expected margin from the home team's perspective (positive = home).

        Net rating is points per 100 *team* possessions; each team plays
        ``expected_pace / 2`` possessions, hence the ``/ 200`` scaling.
        """
        cfg = config or WinProbabilityConfig()
        strength_home = self.team_strength(home, league, cfg)
        strength_away = self.team_strength(away, league, cfg)
        return (strength_home - strength_away) * expected_pace / 200.0 + cfg.home_court_advantage

    def win_probability(self, margin: float, config: WinProbabilityConfig | None = None) -> float:
        """Logistic win probability for the home team from an expected margin."""
        cfg = config or WinProbabilityConfig()
        return 1.0 / (1.0 + math.exp(-margin / cfg.margin_logistic_scale))
