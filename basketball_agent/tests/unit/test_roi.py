"""ROI backtest tests (Phase 11-A, deliverable 4)."""
from __future__ import annotations

import pytest

from basketball_agent.evaluation.roi import flat_stake_roi


def _pred(game_id, prob, won):
    return {"game_id": game_id, "prob_home": prob, "actual_home_win": won}


def _odds(game_id, decimal, market="MONEYLINE", kind="CLOSE"):
    return {"game_id": game_id, "price_decimal": decimal,
            "market": market, "kind": kind}


class TestFlatStakeRoi:
    def test_all_correct_positive_roi(self):
        preds = [_pred(f"g{i}", 0.65, True) for i in range(10)]
        odds = [_odds(f"g{i}", 2.0) for i in range(10)]
        results = flat_stake_roi(preds, odds, edge_thresholds=(0.0,))
        assert len(results) == 1
        r = results[0]
        assert r.n_bets == 10
        assert r.n_wins == 10
        assert r.roi > 0  # 10 wins at 2.0 odds: profit = 10*(2.0-1) = 10

    def test_all_wrong_negative_roi(self):
        preds = [_pred(f"g{i}", 0.70, False) for i in range(10)]
        odds = [_odds(f"g{i}", 1.5) for i in range(10)]
        results = flat_stake_roi(preds, odds, edge_thresholds=(0.0,))
        r = results[0]
        assert r.n_bets == 10 and r.roi < 0

    def test_edge_threshold_filters_bets(self):
        """Higher threshold = fewer bets (only clear edges pass)."""
        preds = [_pred(f"g{i}", 0.5 + i * 0.01, i % 2 == 0)
                 for i in range(20)]
        odds = [_odds(f"g{i}", 1.85) for i in range(20)]
        results = flat_stake_roi(preds, odds,
                                 edge_thresholds=(0.0, 0.05, 0.10))
        assert results[0].n_bets >= results[1].n_bets >= results[2].n_bets

    def test_no_odds_no_bets(self):
        preds = [_pred("g1", 0.65, True)]
        results = flat_stake_roi(preds, [], edge_thresholds=(0.0,))
        assert results[0].n_bets == 0

    def test_deterministic(self):
        preds = [_pred(f"g{i}", 0.6, i % 3 == 0) for i in range(30)]
        odds = [_odds(f"g{i}", 1.9) for i in range(30)]
        r1 = flat_stake_roi(preds, odds)
        r2 = flat_stake_roi(preds, odds)
        assert r1 == r2
