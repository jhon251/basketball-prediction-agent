"""ROI backtest tests (Phase 11-A.2): de-vig wiring + edge threshold sweep.

Phase 11-A.4 extends the suite with the symmetric-side strategy:
|edge| >= t games split into home bets (edge >= t, settled at
decimal(price_home)) and away bets (edge <= -t, settled at
decimal(price_away)).
"""
from __future__ import annotations

import pytest

from basketball_agent.evaluation.devig import (
    american_to_decimal,
    moneyline_pair_to_fair,
)
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


class TestSymmetricSides:
    def _pair(self):
        return (-110, 110)  # classic symmetric-vig market

    def test_symmetric_partition_by_abs_edge(self):
        """|edge| >= t games == home bets + away bets (disjoint, exhaustive)."""
        am_h, am_a = self._pair()
        fair_h, _, _ = moneyline_pair_to_fair(am_h, am_a)
        odds = [{"game_id": f"g{i}", "price_home": am_h, "price_away": am_a,
                 "market": "MONEYLINE", "kind": "CLOSE"} for i in range(20)]
        preds = [_pred(f"g{i}", 0.30 + i * 0.025, i % 3 == 0)
                 for i in range(20)]
        results = flat_stake_roi(preds, odds, edge_thresholds=(0.0, 0.05),
                                 sides=("home", "away"))
        by = {(r.threshold, r.side): r for r in results}
        for t in (0.0, 0.05):
            home, away = by[(t, "home")], by[(t, "away")]
            expected = sum(1 for p in preds
                           if abs(p["prob_home"] - fair_h) >= t)
            assert home.n_bets + away.n_bets == expected
            # each game bets at most one side: the id sets are disjoint by
            # construction (edge >= t and edge <= -t cannot both hold for t>0;
            # at t==0 edge==0 goes home), so the partition is exact
            assert home.n_bets + away.n_bets <= len(preds)

    def test_away_side_settled_at_away_decimal(self):
        """Away bets pay decimal(price_away), winning when the home team
        does NOT win."""
        odds = [{"game_id": "g1", "price_home": -200, "price_away": 150,
                 "market": "MONEYLINE", "kind": "CLOSE"}]
        dec_away = american_to_decimal(150)          # 2.5
        assert dec_away == 2.5
        # p_home=0.2 vs fair_home=0.625 -> edge=-0.425 -> away bet
        won_away = flat_stake_roi(
            [_pred("g1", 0.2, False)], odds, edge_thresholds=(0.05,),
            sides=("away",))[0]
        assert won_away.n_bets == 1 and won_away.n_wins == 1
        assert won_away.profit == pytest.approx(dec_away - 1.0)
        assert won_away.roi == pytest.approx(dec_away - 1.0)
        lost_away = flat_stake_roi(
            [_pred("g1", 0.2, True)], odds, edge_thresholds=(0.05,),
            sides=("away",))[0]
        assert lost_away.n_bets == 1 and lost_away.n_wins == 0
        assert lost_away.profit == pytest.approx(-1.0)

    def test_home_side_settled_at_home_decimal(self):
        """Symmetric counterpart: home bets pay decimal(price_home)."""
        odds = [{"game_id": "g1", "price_home": 150, "price_away": -200,
                 "market": "MONEYLINE", "kind": "CLOSE"}]
        # fair_home: dec 2.5 -> 0.4; dec 1.5 -> 0.6667; fair = 0.4/1.0667
        won_home = flat_stake_roi(
            [_pred("g1", 0.8, True)], odds, edge_thresholds=(0.05,),
            sides=("home",))[0]
        assert won_home.n_bets == 1 and won_home.n_wins == 1
        assert won_home.profit == pytest.approx(1.5)

    def test_default_sides_preserves_one_sided_behaviour(self):
        """Default sides=('home',) keeps the original one-sided output
        shape (one row per threshold)."""
        odds = [{"game_id": "g1", "price_home": -200, "price_away": 150,
                 "market": "MONEYLINE", "kind": "CLOSE"}]
        results = flat_stake_roi([_pred("g1", 0.2, False)], odds,
                                 edge_thresholds=(0.05,))
        assert len(results) == 1
        assert results[0].side == "home"
        assert results[0].n_bets == 0  # home edge is negative: no home bet
        both = flat_stake_roi([_pred("g1", 0.2, False)], odds,
                              edge_thresholds=(0.05,),
                              sides=("home", "away"))
        assert [r.side for r in both] == ["home", "away"]
        assert both[1].n_bets == 1    # the away side takes the bet

    def test_invalid_side_rejected(self):
        preds = [_pred("g1", 0.6, True)]
        odds = [_odds_row("g1", 2.0)]
        with pytest.raises(ValueError, match="sides"):
            flat_stake_roi(preds, odds, sides=("middle",))
