"""Odds de-vig & market probability (Phase 11-A, deliverable 3).

Converts American odds to decimal, removes the bookmaker margin
(vig/juice) via the multiplicative method, and produces implied
probabilities.  All functions are pure; no I/O.

Multiplicative de-vig is the standard first-order approximation: raw
implied probabilities sum to 1 + margin; we normalise by dividing by the
sum.  This is conservative — it slightly overrates the favourite relative
to more sophisticated methods (Shin, power, additive).  For a baseline
comparison this bias is acceptable and documented.

American odds conventions:
  positive (+150): decimal = 1 + am/100  (underdog)
  negative (−150): decimal = 1 + 100/|am| (favourite)
"""
from __future__ import annotations

import math


def american_to_decimal(am: int | float) -> float:
    """American moneyline odds -> decimal odds."""
    am = float(am)
    if am > 0:
        return round(1.0 + am / 100.0, 6)
    if am < 0:
        return round(1.0 + 100.0 / abs(am), 6)
    raise ValueError(f"American odds cannot be zero: {am}")


def decimal_to_implied(dec: float) -> float:
    """Decimal odds -> raw implied probability (no de-vig)."""
    if dec <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {dec}")
    return round(1.0 / dec, 8)


def multiplicative_devig(
    prob_a: float, prob_b: float
) -> tuple[float, float, float]:
    """Remove bookmaker margin from a two-way market.

    Returns ``(fair_a, fair_b, margin)`` where margin = prob_a + prob_b − 1.
    The fair probabilities sum to 1.0 exactly.

    This is the standard multiplicative (a.k.a. normalisation) method.
    Limitation: it slightly overrates the favourite because it reduces
    every outcome's implied probability proportionally, while real margin
    distribution is typically asymmetric (more on the favourite).
    """
    if prob_a <= 0 or prob_b <= 0:
        raise ValueError(f"implied probabilities must be positive: {prob_a}, {prob_b}")
    total = prob_a + prob_b
    margin = total - 1.0
    return round(prob_a / total, 8), round(prob_b / total, 8), round(margin, 8)


def moneyline_pair_to_fair(
    am_home: int | float, am_away: int | float
) -> tuple[float, float, float]:
    """Convenience: a pair of American moneyline odds -> de-vigged fair
    home/away probabilities + margin.

    Returns ``(fair_home, fair_away, margin)``.
    """
    dec_h = american_to_decimal(am_home)
    dec_a = american_to_decimal(am_away)
    imp_h = decimal_to_implied(dec_h)
    imp_a = decimal_to_implied(dec_a)
    return multiplicative_devig(imp_h, imp_a)


def spread_cover_probability(margin_pts: float, std: float = 12.0) -> float:
    """Normal approximation of cover probability given a margin and std.

    This is the simplest model and is ONLY used for the ROI backtest to
    convert a model margin prediction into a cover probability for a given
    spread line.  It is NOT used for win probability (the frozen core
    handles that)."""
    if std <= 0:
        raise ValueError(f"std must be positive, got {std}")
    return 0.5 * (1.0 + math.erf(margin_pts / (std * math.sqrt(2.0))))
