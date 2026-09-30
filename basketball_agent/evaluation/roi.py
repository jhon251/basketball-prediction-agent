"""ROI backtest (Phase 11-A, deliverable 4; symmetric sides in 11-A.4).

Consumes settled predictions (with stored probabilities) + market closing
odds (from the odds table) and computes the return on investment for a
flat-stake strategy at various edge thresholds.

An edge exists when the model's probability differs from the de-vigged
market probability by more than the threshold.  Betting is SYMMETRIC
across the two moneyline sides (Phase 11-A.4): after the multiplicative
de-vig, ``fair_home + fair_away = 1`` exactly, so the away edge is the
exact negation of the home edge (``edge_away = -edge_home``):

  edge_home >= +t -> bet HOME  at ``american_to_decimal(price_home)``,
                      wins when ``actual_home_win`` is True;
  edge_home <= -t -> bet AWAY at ``american_to_decimal(price_away)``,
                      wins when ``actual_home_win`` is False.

When a bet wins, the payout is ``decimal - 1`` per unit staked;
otherwise the stake (1 unit) is lost.  The default ``sides=("home",)``
reproduces the original one-sided backtest; pass
``sides=("home", "away")`` for the symmetric strategy.

Anti-leakage: odds are used ONLY as a benchmark — they never enter the
feature pipeline or the prediction core.  This module reads stored
probabilities (from settled predictions) and stored odds; it never
re-runs the prediction core.
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.evaluation.devig import (american_to_decimal, moneyline_pair_to_fair)


@dataclass
class RoiResult:
    threshold: float
    n_bets: int
    n_wins: int
    total_staked: float
    total_return: float
    roi: float                       # total_return / total_staked (can be negative)
    profit: float                    # total_return − total_staked
    skipped_no_pair: int = 0
    side: str = "home"               # which moneyline side this row bets


def flat_stake_roi(
    predictions: list[dict],
    odds_rows: list[dict],
    edge_thresholds: tuple[float, ...] = (0.0, 0.02, 0.05, 0.10),
    sides: tuple[str, ...] = ("home",),
) -> list[RoiResult]:
    """Compute flat-stake ROI for each (edge threshold, side) pair.

    ``predictions``: settled predictions with ``game_id``, ``prob_home``,
    ``actual_home_win`` (bool).
    ``odds_rows``: market rows with ``game_id``, ``price_home``,
    ``price_away`` (both required for de-vig), ``market`` == "MONEYLINE",
    ``kind`` == "CLOSE".
    ``sides``: which sides to bet; rows come back threshold-major, then
    in ``sides`` order.  ``("home",)`` (the default) is the original
    one-sided behaviour; ``("home", "away")`` is the symmetric strategy.

    Edge = model_p − fair_home (de-vigged market probability); the away
    edge is its negation.  Rows without both prices are counted in
    ``skipped_no_pair`` (repeated per threshold/side row).
    """
    if not sides or any(s not in ("home", "away") for s in sides):
        raise ValueError(f"sides must be 'home'/'away', got {sides!r}")

    odds_map: dict[str, tuple[float, float]] = {}
    skipped_no_pair = 0
    for o in odds_rows:
        if o.get("market") != "MONEYLINE" or o.get("kind") != "CLOSE":
            continue
        ph, pa = o.get("price_home"), o.get("price_away")
        if ph is None or pa is None:
            skipped_no_pair += 1
            continue
        odds_map[o["game_id"]] = (float(ph), float(pa))

    results = []
    for threshold in edge_thresholds:
        for side in sides:
            bets = []
            for p in predictions:
                pair = odds_map.get(p["game_id"])
                if pair is None:
                    continue
                am_h, am_a = pair
                fair_h, _, _ = moneyline_pair_to_fair(int(am_h), int(am_a))
                model_p = p["prob_home"]
                edge = model_p - fair_h
                if side == "home":
                    if edge < threshold:
                        continue
                    dec = american_to_decimal(int(am_h))
                    won = p.get("actual_home_win")
                else:  # away: edge_home <= -threshold (edge_away >= threshold)
                    if edge > -threshold:
                        continue
                    dec = american_to_decimal(int(am_a))
                    raw = p.get("actual_home_win")
                    won = None if raw is None else (not raw)
                if won is None:
                    continue
                profit = (dec - 1.0) if won else -1.0
                bets.append(profit)
            n = len(bets)
            total_profit = sum(bets)
            total_staked = float(n)
            total_return = total_staked + total_profit
            roi = total_profit / total_staked if total_staked > 0 else 0.0
            results.append(RoiResult(
                threshold=threshold, n_bets=n,
                n_wins=sum(1 for b in bets if b > 0),
                total_staked=total_staked,
                total_return=total_return,
                roi=round(roi, 6),
                profit=round(total_profit, 4),
                skipped_no_pair=skipped_no_pair,
                side=side,
            ))
    return results
