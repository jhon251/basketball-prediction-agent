"""Unit tests for calibration (reliability table + ECE, hand values)."""
from __future__ import annotations

import pytest

from basketball_agent.evaluation.calibration import (
    build_reliability_table,
    expected_calibration_error,
)

pytestmark = pytest.mark.unit


class TestReliabilityTable:
    def test_bin_assignment(self):
        probs = [0.55, 0.65, 0.75, 0.85, 0.95]
        outcomes = [True, False, True, True, True]
        table = build_reliability_table(probs, outcomes)
        assert len(table) == 10
        assert table[5].count == 1 and table[5].mean_predicted == pytest.approx(0.55)
        assert table[5].actual_rate == pytest.approx(1.0)
        assert table[9].count == 1 and table[9].mean_predicted == pytest.approx(0.95)

    def test_prob_exactly_one_in_last_bin(self):
        table = build_reliability_table([1.0], [True])
        assert table[9].count == 1
        assert sum(b.count for b in table) == 1

    def test_hand_computed_ece(self):
        probs = [0.55, 0.65, 0.75, 0.85, 0.95]
        outcomes = [True, False, True, True, True]
        table = build_reliability_table(probs, outcomes)
        expected = (0.45 + 0.65 + 0.25 + 0.15 + 0.05) / 5
        assert expected_calibration_error(table) == pytest.approx(expected)

    def test_perfectly_calibrated_ece_zero(self):
        probs = [0.5, 0.5]
        outcomes = [True, False]
        table = build_reliability_table(probs, outcomes)
        assert expected_calibration_error(table) == pytest.approx(0.0)

    def test_empty_table_ece_zero(self):
        assert expected_calibration_error(build_reliability_table([], [])) == pytest.approx(0.0)

    def test_invalid_bins_rejected(self):
        with pytest.raises(ValueError):
            build_reliability_table([0.5], [True], n_bins=0)
