"""ROI backtest tests (Phase 11-A.2): de-vig wiring + edge threshold sweep."""
from __future__ import annotations

import pytest

from basketball_agent.evaluation.roi import flat_stake_roi


def _am(dec):
    if dec >= 2.0:
        return int((dec - 1) * 100), -int(100 * dec / (dec - 1))
    return -int(100 / (dec - 1)), int((dec - 1) * 100)


def _odds_row(game_id, dec):
    h, a = _am(dec)
    return {"game_id": game_id, "price_home": h, "price_away": a,
            "market": "MONEYLINE", "kind": "CLOSE"}


def _pred(gid, prob, won):
    return {"game_id": gid, "prob_home": prob, "actual_home_win": won}


class TestFlatStakeRoi:
    def test_all_correct_positive_roi(self):
        preds = [_pred(f"g{i}", 0.65, True) for i in range(10)]
        odds = [_odds_row(f"g{i}", 2.0) for i in range(10)]
        r = flat_stake_roi(preds, odds, edge_thresholds=(0.0,))[0]
        assert r.n_bets == 10 and r.roi > 0

    def test_all_wrong_negative_roi(self):
        preds = [_pred(f"g{i}", 0.70, False) for i in range(10)]
        odds = [_odds_row(f"g{i}", 1.5) for i in range(10)]
        r = flat_stake_roi(preds, odds, edge_thresholds=(0.0,))[0]
        assert r.n_bets == 10 and r.roi < 0

    def test_edge_threshold_filters_bets(self):
        preds = [_pred(f"g{i}", 0.5 + i * 0.01, i % 2 == 0) for i in range(20)]
        odds = [_odds_row(f"g{i}", 1.85) for i in range(20)]
        results = flat_stake_roi(preds, odds, edge_thresholds=(0.0, 0.05, 0.10))
        assert results[0].n_bets >= results[1].n_bets >= results[2].n_bets

    def test_no_odds_no_bets(self):
        preds = [_pred("g1", 0.65, True)]
        r = flat_stake_roi(preds, [], edge_thresholds=(0.0,))[0]
        assert r.n_bets == 0

    def test_deterministic(self):
        preds = [_pred(f"g{i}", 0.6, i % 3 == 0) for i in range(30)]
        odds = [_odds_row(f"g{i}", 1.9) for i in range(30)]
        assert (flat_stake_roi(preds, odds) ==
                flat_stake_roi(preds, odds))
