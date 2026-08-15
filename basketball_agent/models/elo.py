"""Elo rating model (Model A).

Explicit, auditable math — no learned weights, no black boxes.  All
parameters are centralized in :class:`EloConfig`; the feature pipeline
consumes the resulting ratings as team-strength features.

Formulation (FiveThirtyEight-style):

* expected home score share: ``1 / (1 + 10^(-(elo_home + HCA - elo_away) / 400))``
* margin-of-victory multiplier: ``(|margin| + 3)^0.8 / (7.5 + 0.006 * |elo_diff|)``
* ``elo_diff`` includes the home-court advantage.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.data.schemas.game import Game


@dataclass(frozen=True)
class EloConfig:
    """Elo parameters. Defaults follow common NBA conventions."""

    k: float = 20.0
    home_advantage: float = 100.0
    initial_rating: float = 1500.0
    scale: float = 400.0


@dataclass(frozen=True)
class EloResult:
    """Bookkeeping for one rated game (used for diagnostics/audits)."""

    game_id: str
    home_team_id: str
    away_team_id: str
    home_rating_before: float
    away_rating_before: float
    home_expected: float
    home_win: bool
    home_rating_after: float
    away_rating_after: float


def expected_score(home_rating: float, away_rating: float, config: EloConfig) -> float:
    """Probability that the home team wins, per the Elo formula."""
    diff = home_rating + config.home_advantage - away_rating
    return 1.0 / (1.0 + 10.0 ** (-diff / config.scale))


def margin_multiplier(margin: float, elo_diff: float) -> float:
    """Multiplier on K as a function of margin of victory and Elo gap."""
    return (abs(margin) + 3.0) ** 0.8 / (7.5 + 0.006 * abs(elo_diff))


def update_ratings(
    home_before: float,
    away_before: float,
    home_score: int,
    away_score: int,
    config: EloConfig,
    game_id: str = "",
    home_team_id: str = "",
    away_team_id: str = "",
) -> EloResult:
    """Update the two teams' ratings after one game (zero-sum)."""
    margin = home_score - away_score
    home_win = margin > 0
    elo_diff = home_before + config.home_advantage - away_before
    expected = expected_score(home_before, away_before, config)
    k_eff = config.k * margin_multiplier(margin, elo_diff)
    home_actual = 1.0 if home_win else 0.0
    home_after = home_before + k_eff * (home_actual - expected)
    away_after = away_before + k_eff * ((1.0 - home_actual) - (1.0 - expected))
    return EloResult(
        game_id=game_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        home_rating_before=home_before,
        away_rating_before=away_before,
        home_expected=expected,
        home_win=home_win,
        home_rating_after=home_after,
        away_rating_after=away_after,
    )


def compute_ratings(
    games: Iterable[Game], config: EloConfig | None = None
) -> tuple[dict[str, float], list[EloResult]]:
    """Compute Elo ratings by processing games in chronological order.

    Only FINAL games with both scores are rated; anything else is skipped
    (defensive).  Teams enter with ``config.initial_rating`` on first
    appearance.
    """
    config = config or EloConfig()
    ordered = sorted(
        (g for g in games
         if g.status is GameStatus.FINAL and g.home_score is not None and g.away_score is not None),
        key=lambda g: (g.game_date, g.game_id),
    )
    ratings: dict[str, float] = {}
    results: list[EloResult] = []
    for g in ordered:
        ratings.setdefault(g.home_team_id, config.initial_rating)
        ratings.setdefault(g.away_team_id, config.initial_rating)
        res = update_ratings(
            ratings[g.home_team_id],
            ratings[g.away_team_id],
            g.home_score,
            g.away_score,
            config,
            game_id=g.game_id,
            home_team_id=g.home_team_id,
            away_team_id=g.away_team_id,
        )
        ratings[g.home_team_id] = res.home_rating_after
        ratings[g.away_team_id] = res.away_rating_after
        results.append(res)
    return ratings, results
