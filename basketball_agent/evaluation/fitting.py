"""Phase 8-B5: shared train-window fitting datasets.

The train-window grid searches (HCA / logistic scale / shrinkage k / decay
lambda) all run the *same* walk-forward protocol; only the model-layer config
changes.  Features do not depend on (HCA, scale, k) at all, so the old code
re-derived the whole history once per candidate — 21 strength passes for the
scale grid plus 5 more for the shrinkage grid.

This module computes the feature pass ONCE per ``(league, decay_lambda)`` and
evaluates every candidate config against the cached features.  Results are
**bit-identical**: the same games are visited in the same order, the same
``warmup_not_met`` decisions and ``FeaturePipeline`` calls happen, and the very
same predictor math is applied to the very same feature objects, so the
resulting :class:`BacktestResult` metrics equal the per-config
``BacktestEngine`` runs exactly (pinned by a regression test).

League purity is enforced here: a dataset is built from one league only and
raises otherwise — there is no cross-league caching or training.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from basketball_agent.data.db.repository import Repository
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestRecord,
    BacktestResult,
    Predictor,
    warmup_not_met,
)
from basketball_agent.features import FeatureComputationError, FeaturePipeline
from basketball_agent.features.base import GameFeatures


@dataclass(frozen=True)
class SkippedGame:
    game_id: str
    as_of: datetime
    reason: str


@dataclass(frozen=True)
class FeatureRecord:
    game_id: str
    as_of: datetime
    features: GameFeatures
    actual_home_win: bool
    actual_margin: int
    actual_total: int
    actual_home_score: int
    actual_away_score: int


@dataclass(frozen=True)
class FittingDataset:
    """One cached feature pass over a train window (single league, one lambda)."""

    league: str
    decay_lambda: float
    warmup_games: int
    slots: tuple[SkippedGame | FeatureRecord, ...]
    n_feature_computations: int

    @property
    def n_games(self) -> int:
        return len(self.slots)

    @property
    def n_predicted(self) -> int:
        return sum(1 for s in self.slots if isinstance(s, FeatureRecord))

    def to_backtest_result(self, predictor: Predictor) -> BacktestResult:
        """Rebuild the exact BacktestResult the engine would produce.

        The record sequence (including warmup/feature skips) matches
        ``BacktestEngine.run(game_ids)`` one-to-one.
        """
        records: list[BacktestRecord] = []
        for slot in self.slots:
            if isinstance(slot, SkippedGame):
                records.append(BacktestRecord(
                    game_id=slot.game_id, as_of=slot.as_of, prediction=None,
                    skipped_reason=slot.reason,
                ))
                continue
            records.append(BacktestRecord(
                game_id=slot.game_id, as_of=slot.as_of,
                prediction=predictor.predict(slot.features), skipped_reason=None,
                actual_home_win=slot.actual_home_win,
                actual_margin=slot.actual_margin,
                actual_total=slot.actual_total,
                actual_home_score=slot.actual_home_score,
                actual_away_score=slot.actual_away_score,
            ))
        return BacktestResult(tuple(records))

    def metrics(self, predictor: Predictor) -> dict:
        return self.to_backtest_result(predictor).summary()

    def win_prob_logloss(self, predictor: Predictor) -> float | None:
        return self.metrics(predictor)["log_loss"]


def build_fitting_dataset(
    repo: Repository,
    game_ids: list[str],
    *,
    decay_lambda: float = 0.0,
    warmup_games: int = 5,
) -> FittingDataset:
    """Compute (and cache) one walk-forward feature pass over ``game_ids``."""
    games = []
    for game_id in game_ids:
        game = repo.get_game(game_id)
        if game is None:
            raise ValueError(f"unknown game in fitting window: {game_id}")
        games.append(game)

    leagues = {g.league for g in games}
    if len(leagues) != 1:
        raise ValueError(
            "a fitting dataset must contain exactly one league "
            f"(got {sorted(l.value for l in leagues)}) — cross-league fitting is "
            "forbidden; scope the fit with league=League.NBA / League.EUROLEAGUE"
        )
    league = next(iter(leagues))

    games.sort(key=lambda g: (g.game_date, g.game_id))
    pipeline = FeaturePipeline(repo, decay_lambda=decay_lambda)
    slots: list[SkippedGame | FeatureRecord] = []
    computations = 0

    for g in games:
        as_of = g.game_date
        if warmup_not_met(repo, g, as_of, warmup_games):
            slots.append(SkippedGame(
                game_id=g.game_id, as_of=as_of,
                reason=f"warmup < {warmup_games} prior games",
            ))
            continue
        try:
            features = pipeline.compute(g.game_id, as_of)
        except FeatureComputationError as exc:
            slots.append(SkippedGame(
                game_id=g.game_id, as_of=as_of, reason=f"features: {exc}",
            ))
            continue
        computations += 1
        slots.append(FeatureRecord(
            game_id=g.game_id, as_of=as_of, features=features,
            actual_home_win=g.home_score > g.away_score,
            actual_margin=g.home_score - g.away_score,
            actual_total=g.home_score + g.away_score,
            actual_home_score=g.home_score,
            actual_away_score=g.away_score,
        ))

    return FittingDataset(
        league=league.value,
        decay_lambda=decay_lambda,
        warmup_games=warmup_games,
        slots=tuple(slots),
        n_feature_computations=computations,
    )


def engine_equivalent_config(
    n_simulations: int = 500, seed: int = 7, warmup_games: int = 5, decay_lambda: float = 0.0
) -> BacktestConfig:
    """The BacktestConfig the engine would use for the same protocol."""
    return BacktestConfig(
        n_simulations=n_simulations, seed=seed,
        warmup_games=warmup_games, decay_lambda=decay_lambda,
    )


def engine_equivalent_engine(
    repo: Repository, predictor: Predictor, dataset: FittingDataset,
    *, n_simulations: int = 500, seed: int = 7,
) -> BacktestEngine:
    """An engine configured exactly like the cached dataset's protocol.

    Used by tests to prove the cached path and the engine path agree.
    """
    return BacktestEngine(
        repo, predictor,
        engine_equivalent_config(
            n_simulations=n_simulations, seed=seed,
            warmup_games=dataset.warmup_games, decay_lambda=dataset.decay_lambda,
        ),
    )
