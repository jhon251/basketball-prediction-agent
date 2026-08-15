"""Time-decayed aggregation of per-game derived statistics.

Spec §10: older games must not carry the same weight as recent ones.
``weight = exp(-lambda * days_since_game)``; ``lambda = 0`` means uniform
(no decay) — the default until the backtest phase determines lambda from
data.  The decay is applied to *aggregation only*; per-game values are
never modified.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

from basketball_agent.features.team.boxscore import TeamGameDerived

AGGREGATED_FIELDS = (
    "ortg", "drtg", "nrtg", "pace",
    "efg_pct", "ts_pct", "fg3_pct", "ft_pct",
    "orb_pct", "drb_pct", "trb_pct",
    "tov_pct", "opp_tov_pct",
    "fga_rate", "fta_rate", "p3a_share", "p2_pct",
)


def decay_weights(game_dates: list[datetime], as_of: datetime, decay_lambda: float) -> list[float]:
    """Compute ``exp(-lambda * days_since)`` for each game date.

    ``decay_lambda`` must be >= 0.  Dates at/after ``as_of`` are clamped to
    weight 1.0 here, but the feature pipeline never passes them (strict
    ``game_date < as_of`` filter) — the clamp is defensive only.
    """
    if decay_lambda < 0:
        raise ValueError("decay_lambda must be >= 0")
    if decay_lambda == 0:
        return [1.0] * len(game_dates)
    return [
        math.exp(-decay_lambda * max((as_of - d).total_seconds(), 0.0) / 86400.0)
        for d in game_dates
    ]


def weighted_mean(values: Iterable[float | None], weights: Iterable[float]) -> float | None:
    """Weighted mean ignoring None values; None when nothing is computable."""
    pairs = [(v, w) for v, w in zip(values, weights) if v is not None and w > 0]
    if not pairs:
        return None
    return sum(v * w for v, w in pairs) / sum(w for _, w in pairs)


def aggregate_team(
    rows: list[TeamGameDerived], as_of: datetime, decay_lambda: float
) -> dict:
    """Aggregate a team's per-game derived stats with time decay.

    Returns a dict with ``games_played``, ``last_game_date`` and one
    decay-weighted mean per field in ``AGGREGATED_FIELDS``.
    """
    ordered = sorted(rows, key=lambda r: r.game_date)
    weights = decay_weights([r.game_date for r in ordered], as_of, decay_lambda)
    out: dict = {
        "games_played": len(ordered),
        "last_game_date": ordered[-1].game_date if ordered else None,
    }
    for field in AGGREGATED_FIELDS:
        out[field] = weighted_mean([getattr(r, field) for r in ordered], weights)
    return out
