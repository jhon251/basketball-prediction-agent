"""Shared statistical helpers for the model layer."""
from __future__ import annotations


def shrink(value: float | None, mean: float, n_games: int, k: float) -> float:
    """Shrink a per-team estimate toward the league mean.

    ``blended = (n*value + k*mean) / (n+k)`` — the standard James-Stein-style
    regression-to-the-mean for noisy per-team rates.  ``k`` is the strength of
    the prior (number of pseudo-games); with ``k=0`` or ``n<=0`` the raw value
    is returned.  A missing value falls back to the league mean.
    """
    if value is None:
        return mean
    if n_games <= 0 or k <= 0:
        return value
    return (n_games * value + k * mean) / (n_games + k)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
