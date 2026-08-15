"""Monte Carlo runner and result statistics (spec §18, §19).

Every run records home_score / away_score / margin / total / winner; the
result exposes win probabilities, full per-stat distribution summaries
(mean / median / std / P5 / P25 / P50 / P75 / P95) and most-likely score
buckets.  Reproducibility: the same ``seed`` always yields identical runs
(deterministic mode); ``seed=None`` uses system entropy (production
stochastic mode).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence

from basketball_agent.models.game_params import GamePredictionParams
from basketball_agent.simulation.game_simulator import GameSimulator, SimulatedGame
from basketball_agent.simulation.player_state import PlayerProfile

DEFAULT_N_SIMULATIONS = 10000
SCORE_BUCKET_WIDTH = 5


@dataclass(frozen=True)
class SimulationRun:
    home_score: int
    away_score: int
    margin: int
    total: int
    winner: str  # "home" | "away" | "tie"


@dataclass(frozen=True)
class DistributionStats:
    """Summary statistics of one simulated distribution (spec §19)."""

    mean: float
    median: float
    std: float
    p5: float
    p25: float
    p50: float
    p75: float
    p95: float
    count: int


@dataclass(frozen=True)
class ScoreBucket:
    label: str  # e.g. "110-114"
    count: int
    share: float


@dataclass(frozen=True)
class MonteCarloResult:
    n_simulations: int
    seed: int | None
    game_id: str
    home_win_probability: float
    away_win_probability: float
    tie_probability: float
    home_score: DistributionStats
    away_score: DistributionStats
    margin: DistributionStats
    total: DistributionStats
    home_score_buckets: tuple[ScoreBucket, ...]
    away_score_buckets: tuple[ScoreBucket, ...]
    runs: tuple[SimulationRun, ...]


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile over a sorted sequence (0 <= q <= 1)."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("cannot compute percentiles of an empty distribution")
    pos = q * (n - 1)
    low = int(pos)
    high = min(low + 1, n - 1)
    frac = pos - low
    return sorted_values[low] * (1.0 - frac) + sorted_values[high] * frac


def compute_stats(values: Sequence[float]) -> DistributionStats:
    if len(values) == 0:
        raise ValueError("cannot compute stats of an empty distribution")
    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n
    variance = sum((v - mean) ** 2 for v in ordered) / n  # population std
    return DistributionStats(
        mean=mean,
        median=_percentile(ordered, 0.5),
        std=math.sqrt(variance),
        p5=_percentile(ordered, 0.05),
        p25=_percentile(ordered, 0.25),
        p50=_percentile(ordered, 0.5),
        p75=_percentile(ordered, 0.75),
        p95=_percentile(ordered, 0.95),
        count=n,
    )


def _buckets(values: Sequence[int], top: int = 5) -> tuple[ScoreBucket, ...]:
    counts: dict[str, int] = {}
    for v in values:
        low = (v // SCORE_BUCKET_WIDTH) * SCORE_BUCKET_WIDTH
        label = f"{low}-{low + SCORE_BUCKET_WIDTH - 1}"
        counts[label] = counts.get(label, 0) + 1
    total = len(values)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(
        ScoreBucket(label=label, count=count, share=count / total)
        for label, count in ranked[:top]
    )


class MonteCarloRunner:
    """Runs N possession-level simulations and aggregates the distributions."""

    def __init__(self, simulator: GameSimulator | None = None, seed: int | None = None) -> None:
        self._simulator = simulator or GameSimulator()
        self._seed = seed

    def run(
        self,
        params: GamePredictionParams,
        n_simulations: int | None = None,
        seed: int | None = None,
        home_roster: list[PlayerProfile] | None = None,
        away_roster: list[PlayerProfile] | None = None,
    ) -> MonteCarloResult:
        n = n_simulations if n_simulations is not None else DEFAULT_N_SIMULATIONS
        if n < 1:
            raise ValueError("n_simulations must be >= 1")
        effective_seed = self._seed if seed is None else seed
        rng = random.Random(effective_seed)
        games: list[SimulatedGame] = [
            self._simulator.simulate(params, rng, home_roster, away_roster) for _ in range(n)
        ]
        return MonteCarloResult(
            n_simulations=n,
            seed=effective_seed,
            game_id=params.game_id,
            home_win_probability=sum(1 for g in games if g.winner == "home") / n,
            away_win_probability=sum(1 for g in games if g.winner == "away") / n,
            tie_probability=sum(1 for g in games if g.winner == "tie") / n,
            home_score=compute_stats([g.home_score for g in games]),
            away_score=compute_stats([g.away_score for g in games]),
            margin=compute_stats([float(g.margin) for g in games]),
            total=compute_stats([float(g.total) for g in games]),
            home_score_buckets=_buckets([g.home_score for g in games]),
            away_score_buckets=_buckets([g.away_score for g in games]),
            runs=tuple(
                SimulationRun(
                    home_score=g.home_score,
                    away_score=g.away_score,
                    margin=g.margin,
                    total=g.total,
                    winner=g.winner,
                )
                for g in games
            ),
        )
