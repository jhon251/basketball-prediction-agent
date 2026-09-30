"""De-vig and market probability tests (Phase 11-A, deliverable 3)."""
from __future__ import annotations

import pytest

from basketball_agent.evaluation.devig import (
    american_to_decimal,
    decimal_to_implied,
    moneyline_pair_to_fair,
    multiplicative_devig,
    spread_cover_probability,
)


class TestAmericanToDecimal:
    def test_positive_underdog(self):
        assert american_to_decimal(150) == pytest.approx(2.5)
        assert american_to_decimal(200) == pytest.approx(3.0)
        assert american_to_decimal(100) == pytest.approx(2.0)

    def test_negative_favourite(self):
        assert american_to_decimal(-150) == pytest.approx(
            1 + 100 / 150, rel=1e-4)
        assert american_to_decimal(-200) == pytest.approx(1.5)
        assert american_to_decimal(-110) == pytest.approx(
            1 + 100 / 110, rel=1e-4)

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="cannot be zero"):
            american_to_decimal(0)


class TestDecimalToImplied:
    def test_basic(self):
        assert decimal_to_implied(2.0) == pytest.approx(0.5)
        assert decimal_to_implied(1.5) == pytest.approx(
            2 / 3, rel=1e-4)
        assert decimal_to_implied(4.0) == pytest.approx(0.25)

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="> 1.0"):
            decimal_to_implied(1.0)


class TestMultiplicativeDevig:
    def test_symmetric_market(self):
        """-110/-110: implied = 0.47619 each, total = 0.95238, margin = 0.04762."""
        fair_h, fair_a, margin = multiplicative_devig(
            1 / 1.909, 1 / 1.909)
        assert fair_h + fair_a == pytest.approx(1.0)
        assert margin > 0  # bookmaker has an edge

    def test_asymmetric_market(self):
        """Typical NBA: home -200 / away +170."""
        dec_h = 1 + 100 / 200  # 1.5
        dec_a = 1 + 170 / 100  # 2.7
        imp_h = 1 / 1.5  # 0.6667
        imp_a = 1 / 2.7  # 0.37037
        fair_h, fair_a, margin = multiplicative_devig(imp_h, imp_a)
        assert fair_h + fair_a == pytest.approx(1.0)
        assert fair_h > fair_a  # favourite
        assert 0 < margin < 0.15  # reasonable vig

    def test_result_sums_to_one(self):
        for h, a in ((2.0, 2.0), (1.5, 2.7), (3.0, 1.4)):
            fh, fa, _ = multiplicative_devig(1 / h, 1 / a)
            assert fh + fa == pytest.approx(1.0, abs=1e-6)


class TestMoneylinePair:
    def test_known_values(self):
        """-110 / -110 is the classic vig market."""
        fh, fa, margin = moneyline_pair_to_fair(-110, -110)
        assert fh == pytest.approx(fa)  # symmetric
        assert fh + fa == pytest.approx(1.0)
        assert 0.02 < margin < 0.06  # typical NBA vig ~4.5%

    def test_uneven_market(self):
        fh, fa, margin = moneyline_pair_to_fair(-200, 170)
        assert fh > fa  # favourite
        assert fh + fa == pytest.approx(1.0, abs=1e-6)


class TestSpreadCoverProbability:
    def test_zero_margin_is_coin_flip(self):
        assert spread_cover_probability(0.0, 12.0) == pytest.approx(0.5)

    def test_positive_margin_gt_half(self):
        assert spread_cover_probability(5.0, 12.0) > 0.5

    def test_negative_margin_lt_half(self):
        assert spread_cover_probability(-5.0, 12.0) < 0.5
