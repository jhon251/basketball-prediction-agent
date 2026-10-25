"""Spread (让分) cover probability layer (Phase 12-A).

Conventions
-----------
* ``home_line`` is the point spread applied to the HOME team's score:
  the home side covers iff ``home_score + home_line > away_score``,
  i.e. ``margin + home_line > 0`` where ``margin = home_score - away_score``.
  A negative home line means the home team is giving points
  ("主队让 5.5" ⇒ ``home_line = -5.5``).
* Basketball scoring margins are integers, so a push
  (``margin + home_line == 0``) is possible only for integer lines.
  Lines must be multiples of 0.5 — anything else fails loudly.

Two independent providers, deliberately both kept:

* ``cover_from_samples`` — counts the frozen Monte Carlo margin samples
  (``MonteCarloResult.runs[*].margin``).  Uses the simulation family.
* ``cover_from_elo_normal`` — Elo win probability mapped to an expected
  margin via the inverse probit ``mu = sigma * Phi^-1(p)`` and scored
  against the line with a continuity-corrected integer-margin normal.
  Uses the family the multi-season backtests found most accurate
  (Elo, ``docs/MULTI_SEASON_RESULTS.md``).

This module never touches frozen models: it only consumes already
produced outputs (margin samples, win probability, historical sigma).
Which provider a caller should default to is decided by measurement —
see ``evaluation/spread_calibration.py`` and ``docs/PHASE12_A_SPREAD_API.md``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist, stdev
from typing import Sequence

_DEFAULT_NBA_LIKE_SIGMA = 11.5  # frozen fallback: NBA margin σ ≈ 11–12
_P_CLAMP = 1e-4

_NORMAL = NormalDist()


class SpreadLineError(ValueError):
    """Invalid point-spread line (not a multiple of 0.5 / out of range)."""


@dataclass(frozen=True)
class CoverProbabilities:
    home_cover: float
    push: float
    away_cover: float
    home_line: float
    method: str  # "simulation_samples" | "elo_normal"

    def __post_init__(self) -> None:
        total = self.home_cover + self.push + self.away_cover
        if not math.isclose(total, 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                f"cover probabilities must sum to 1, got {total}")
        for name in ("home_cover", "push", "away_cover"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{name} out of [0,1]: {v}")


def validate_home_line(home_line: float) -> float:
    """Fail loudly on non-half-point or absurd lines."""
    x = float(home_line)
    if not math.isfinite(x):
        raise SpreadLineError("home_line must be finite")
    if abs(x) > 60.0:
        raise SpreadLineError(f"home_line {x} outside plausible range ±60")
    if not abs(x * 2 - round(x * 2)) < 1e-9:
        raise SpreadLineError(
            f"home_line {x} is not a multiple of 0.5 (betting lines only)")
    return round(x * 2) / 2


def cover_from_samples(
    margins: Sequence[int], home_line: float,
) -> CoverProbabilities:
    """Empirical cover probabilities from integer margin samples."""
    line = validate_home_line(home_line)
    if len(margins) == 0:
        raise ValueError("margins must not be empty")
    home = push = away = 0
    for m in margins:
        mm = m + line
        if mm > 1e-9:
            home += 1
        elif mm < -1e-9:
            away += 1
        else:
            push += 1
    n = len(margins)
    return CoverProbabilities(
        home_cover=home / n, push=push / n, away_cover=away / n,
        home_line=line, method="simulation_samples",
    )


def cover_from_normal(
    mean: float, sigma: float, home_line: float,
) -> CoverProbabilities:
    """Continuity-corrected integer-margin normal cover probabilities.

    ``P(margin >= j) = 1 - Phi((j - 0.5 - mu) / sigma)`` for integer
    margins modelled as discretised normal(mu, sigma).
    """
    line = validate_home_line(home_line)
    s = float(sigma)
    if not math.isfinite(s) or s <= 0.0:
        raise ValueError(f"sigma must be positive and finite, got {sigma}")
    k = -line  # home covers iff margin > k
    if abs(k - round(k)) < 1e-9:
        # integer line: j = k + 1 covers; push mass at k
        j = round(k) + 1
        push = (
            _NORMAL.cdf((k + 0.5 - mean) / s)
            - _NORMAL.cdf((k - 0.5 - mean) / s)
        )
    else:
        # half-point line: j = k + 0.5 covers; no push
        j = k + 0.5
        push = 0.0
    home = 1.0 - _NORMAL.cdf((j - 0.5 - mean) / s)
    home = min(max(home, 0.0), 1.0 - push)
    return CoverProbabilities(
        home_cover=home, push=push, away_cover=1.0 - home - push,
        home_line=line, method="elo_normal",
    )


def elo_expected_margin(p_home: float, sigma: float) -> float:
    """Inverse probit mapping consistent with P(margin>0)=Phi(mu/sigma).

    Clamps pathological 0/1 probabilities instead of exploding.
    """
    p = min(max(float(p_home), _P_CLAMP), 1.0 - _P_CLAMP)
    return float(sigma) * _NORMAL.inv_cdf(p)


def cover_from_elo_normal(
    p_home: float, sigma: float, home_line: float,
) -> CoverProbabilities:
    """Elo win probability → margin → cover (see module docstring)."""
    if not 0.0 <= float(p_home) <= 1.0:
        raise ValueError(f"p_home out of [0,1]: {p_home}")
    mu = elo_expected_margin(p_home, sigma)
    return cover_from_normal(mu, sigma, home_line)


def fit_margin_sigma(
    margins: Sequence[float], *,
    min_samples: int = 200,
    default: float = _DEFAULT_NBA_LIKE_SIGMA,
) -> float:
    """As-of-safe sigma from historical integer margins.

    Falls back to the frozen default when the history is too short or
    degenerate — callers must pass ONLY margins from games strictly
    before the prediction moment (leakage rule §2).
    """
    if len(margins) < min_samples:
        return default
    s = stdev(margins)
    if not math.isfinite(s) or s < 1.0:
        return default
    return s
