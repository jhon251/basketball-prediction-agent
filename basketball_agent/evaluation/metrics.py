"""Evaluation metrics (spec §24).

Win probability: accuracy, log loss, Brier score, calibration error.
Score / margin / total: MAE, RMSE.
"""
from __future__ import annotations

import math
from typing import Sequence


def accuracy(y_true: Sequence[bool], probs: Sequence[float], threshold: float = 0.5) -> float:
    if not y_true:
        raise ValueError("cannot compute accuracy on empty predictions")
    correct = sum(
        1 for y, p in zip(y_true, probs) if (p >= threshold) == bool(y)
    )
    return correct / len(y_true)


def log_loss(y_true: Sequence[bool], probs: Sequence[float], eps: float = 1e-12) -> float:
    if not y_true:
        raise ValueError("cannot compute log loss on empty predictions")
    total = 0.0
    for y, p in zip(y_true, probs):
        p = min(max(p, eps), 1.0 - eps)
        yi = 1.0 if y else 0.0
        total += -(yi * math.log(p) + (1.0 - yi) * math.log(1.0 - p))
    return total / len(y_true)


def brier_score(y_true: Sequence[bool], probs: Sequence[float]) -> float:
    if not y_true:
        raise ValueError("cannot compute Brier score on empty predictions")
    return sum((p - (1.0 if y else 0.0)) ** 2 for y, p in zip(y_true, probs)) / len(y_true)


def mae(actuals: Sequence[float], predictions: Sequence[float]) -> float:
    if not actuals:
        raise ValueError("cannot compute MAE on empty values")
    return sum(abs(a - p) for a, p in zip(actuals, predictions)) / len(actuals)


def rmse(actuals: Sequence[float], predictions: Sequence[float]) -> float:
    if not actuals:
        raise ValueError("cannot compute RMSE on empty values")
    return math.sqrt(sum((a - p) ** 2 for a, p in zip(actuals, predictions)) / len(actuals))
