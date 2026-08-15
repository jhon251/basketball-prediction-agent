"""Calibration: reliability diagram data + expected calibration error (§25).

The reliability table answers "when the model said 50-60%, how often did the
home team actually win?" — the system must report probability *credibility*,
not just accuracy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ReliabilityBin:
    bin_low: float
    bin_high: float
    count: int
    mean_predicted: float | None
    actual_rate: float | None


def build_reliability_table(
    probs: Sequence[float], outcomes: Sequence[bool], n_bins: int = 10
) -> list[ReliabilityBin]:
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        idx = [
            j for j, p in enumerate(probs)
            if lo <= p < hi or (hi == 1.0 and p == hi)
        ]
        count = len(idx)
        bins.append(ReliabilityBin(
            bin_low=lo,
            bin_high=hi,
            count=count,
            mean_predicted=(sum(probs[j] for j in idx) / count) if count else None,
            actual_rate=(sum(1 for j in idx if outcomes[j]) / count) if count else None,
        ))
    return bins


def expected_calibration_error(table: Sequence[ReliabilityBin]) -> float:
    """ECE = sum over bins of (|bin|/n) * |actual_rate - mean_predicted|."""
    total = sum(b.count for b in table)
    if total == 0:
        return 0.0
    error = 0.0
    for b in table:
        if b.count and b.actual_rate is not None and b.mean_predicted is not None:
            error += (b.count / total) * abs(b.actual_rate - b.mean_predicted)
    return error
