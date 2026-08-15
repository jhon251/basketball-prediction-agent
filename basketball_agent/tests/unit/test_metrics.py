"""Unit tests for evaluation metrics (hand-computed values)."""
from __future__ import annotations

import math

import pytest

from basketball_agent.evaluation.metrics import accuracy, brier_score, log_loss, mae, rmse

pytestmark = pytest.mark.unit


class TestWinProbMetrics:
    def test_accuracy(self):
        assert accuracy([True, False], [0.6, 0.4]) == pytest.approx(1.0)
        assert accuracy([True, False], [0.4, 0.6]) == pytest.approx(0.0)
        # 0.5 threshold: third prediction 0.5 counts as a "home win" call
        assert accuracy([True, False, True], [0.6, 0.4, 0.5]) == pytest.approx(1.0)

    def test_brier_score_hand_value(self):
        assert brier_score([True, False], [0.7, 0.2]) == pytest.approx(
            (0.3 ** 2 + 0.2 ** 2) / 2
        )

    def test_log_loss_hand_value(self):
        expected = (-math.log(0.7) + -math.log(0.8)) / 2
        assert log_loss([True, False], [0.7, 0.2]) == pytest.approx(expected)

    def test_log_loss_perfect_prediction(self):
        assert log_loss([True, False], [1.0 - 1e-9, 1e-9]) < 1e-6

    def test_brier_of_constant_half_is_quarter(self):
        # Brier score of always-0.5 is exactly 0.25 for ANY outcome sequence
        assert brier_score([True, False, True, False], [0.5] * 4) == pytest.approx(0.25)

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            accuracy([], [])
        with pytest.raises(ValueError):
            brier_score([], [])
        with pytest.raises(ValueError):
            log_loss([], [])


class TestRegressionMetrics:
    def test_mae(self):
        assert mae([2.0, 4.0], [1.0, 3.0]) == pytest.approx(1.0)

    def test_rmse(self):
        assert rmse([2.0, 4.0], [1.0, 3.0]) == pytest.approx(1.0)
        assert rmse([0.0, 4.0], [0.0, 0.0]) == pytest.approx(math.sqrt(8.0))

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            mae([], [])
        with pytest.raises(ValueError):
            rmse([], [])
