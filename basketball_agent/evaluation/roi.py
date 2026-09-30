"""ROI backtest (Phase 11-A, deliverable 4): flat-stake market vs model.

Consumes settled predictions (with stored probabilities) + market closing
odds (from the odds table) and computes the return on investment for a
flat-stake strategy at various edge thresholds.

An edge exists when the model's probability exceeds the de-vigged market
probability by more than the threshold.  When an edge exists and the
prediction is correct, the payout is ``price_decimal − 1`` per unit staked;
otherwise the stake (1 unit) is lost.

Anti-leakage: odds are used ONLY as a benchmark — they never enter the
feature pipeline or the prediction core.  This module reads stored
probabilities (from settled predictions) and stored odds; it never
re-runs the prediction core.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from basketball_agent.evaluation.devig import american_to_decimal
from basketball_agent.evaluation.settlement import SettlementStore


@dataclass
class RoiResult:
    threshold: float
    n_bets: int
    n_wins: int
    total_staked: float
    total_return: float
    roi: float                       # total_return / total_staked (can be negative)
    profit: float                    # total_return − total_staked


def flat_stake_roi(
    predictions: list[dict],
    odds_rows: list[dict],
    edge_thresholds: tuple[float, ...] = (0.0, 0.02, 0.05, 0.10),
) -> list[RoiResult]:
    """Compute flat-stake ROI for each edge threshold.

    ``predictions``: settled predictions with ``game_id``, ``prob_home``,
    ``actual_home_win`` (bool).
    ``odds_rows``: market rows with ``game_id``, ``price_decimal``
    (decimal odds for the bet side), ``market`` == "MONEYLINE".

    Only MONEYLINE is evaluated (SPREAD/TOTAL are unavailable if the data
    source doesn't provide spread/total odds).
    """
    # index odds by game_id (CLOSE MONEYLINE only)
    odds_map: dict[str, float] = {}
    for o in odds_rows:
        if o.get("market") == "MONEYLINE" and o.get("kind") == "CLOSE":
            odds_map[o["game_id"]] = float(o["price_decimal"])

    results = []
    for threshold in edge_thresholds:
        bets = []
        for p in predictions:
            gid = p["game_id"]
            if gid not in odds_map:
                continue
            model_p = p["prob_home"]
            dec_odds = odds_map[gid]
            market_p = 1.0 / dec_odds
            if model_p - market_p >= threshold:
                won = p.get("actual_home_win")
                if won is None:
                    continue
                profit = (dec_odds - 1.0) if won else -1.0
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
        ))
    return results
