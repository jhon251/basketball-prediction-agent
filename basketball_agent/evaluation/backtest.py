"""Walk-forward backtest (spec §23) + baseline predictors (spec §26).

There is NO random train/test split anywhere: games are processed strictly
chronologically and each prediction only sees features computed as-of its own
tip-off (the feature pipeline enforces this structurally).  The default is an
expanding window; ``window_days`` switches to a rolling window.

Predictors are interchangeable; the simulation predictor is the system under
test, and the three baselines (random / Elo / simple historical) answer the
question "is the simulation actually better than the simple models?" —
reported honestly either way (spec §26).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.evaluation.calibration import ReliabilityBin, build_reliability_table, expected_calibration_error
from basketball_agent.evaluation.metrics import accuracy, brier_score, log_loss, mae, rmse
from basketball_agent.features.base import (
    FeatureComputationError,
    FeatureProvenance,
    GameFeatures,
)
from basketball_agent.features.feature_pipeline import FeaturePipeline
from basketball_agent.models.game_params import build_game_params
from basketball_agent.models.offensive_model import OffensiveModel
from basketball_agent.models.pace_model import PaceModel
from basketball_agent.models.win_probability import WinProbabilityModel
from basketball_agent.simulation.monte_carlo import MonteCarloRunner


@dataclass(frozen=True)
class Prediction:
    game_id: str
    as_of: datetime
    prob_home: float
    prob_away: float
    expected_margin: float | None = None
    expected_total: float | None = None
    home_score_mean: float | None = None
    away_score_mean: float | None = None
    provenance: FeatureProvenance | None = None


class Predictor(Protocol):
    def predict(self, features: GameFeatures) -> Prediction: ...


class SimulationPredictor:
    """The system under test: features -> models -> Monte Carlo."""

    def __init__(self, n_simulations: int = 1000, seed: int = 7) -> None:
        self.n_simulations = n_simulations
        self.seed = seed

    def predict(self, features: GameFeatures) -> Prediction:
        params = build_game_params(features)
        result = MonteCarloRunner(seed=self.seed).run(params, n_simulations=self.n_simulations)
        return Prediction(
            game_id=features.game_id,
            as_of=features.as_of,
            prob_home=result.home_win_probability,
            prob_away=result.away_win_probability,
            expected_margin=params.expected_margin,
            expected_total=params.expected_total,
            home_score_mean=result.home_score.mean,
            away_score_mean=result.away_score.mean,
            provenance=features.provenance,
        )


class EloPredictor:
    """Baseline 2: Elo win probability from the feature pipeline."""

    def predict(self, features: GameFeatures) -> Prediction:
        p = features.elo_win_prob_home
        if p is None:
            raise FeatureComputationError("Elo win probability is unavailable")
        return Prediction(
            game_id=features.game_id, as_of=features.as_of,
            prob_home=p, prob_away=1.0 - p, provenance=features.provenance,
        )


class StrengthPredictor:
    """Model B as a standalone analytic baseline (margin + win prob + total)."""

    def predict(self, features: GameFeatures) -> Prediction:
        win_model = WinProbabilityModel()
        pace = PaceModel().expected_pace(features.home, features.away, features.league)
        margin = win_model.expected_margin(features.home, features.away, features.league, pace)
        prob = win_model.win_probability(margin)
        home_ppp = OffensiveModel().expected_ppp(features.home, features.away, features.league) \
            + 2.5 / pace
        away_ppp = OffensiveModel().expected_ppp(features.away, features.home, features.league)
        return Prediction(
            game_id=features.game_id, as_of=features.as_of,
            prob_home=prob, prob_away=1.0 - prob,
            expected_margin=margin,
            expected_total=(home_ppp + away_ppp) * pace,
            provenance=features.provenance,
        )


class RandomPredictor:
    """Baseline 1: always 50/50 (no information)."""

    def predict(self, features: GameFeatures) -> Prediction:
        return Prediction(
            game_id=features.game_id, as_of=features.as_of,
            prob_home=0.5, prob_away=0.5, provenance=features.provenance,
        )


class HistoricalPredictor:
    """Baseline 3: simple historical home-court baseline (no market data
    available offline; this is the "historical" stand-in)."""

    def predict(self, features: GameFeatures) -> Prediction:
        p = features.league_home_win_pct if features.league_home_win_pct is not None else 0.5
        return Prediction(
            game_id=features.game_id, as_of=features.as_of,
            prob_home=p, prob_away=1.0 - p, expected_margin=0.0,
            provenance=features.provenance,
        )


@dataclass(frozen=True)
class BacktestConfig:
    n_simulations: int = 1000
    seed: int = 7
    warmup_games: int = 5
    window_days: float | None = None  # None = expanding window
    decay_lambda: float = 0.0


@dataclass(frozen=True)
class BacktestRecord:
    game_id: str
    as_of: datetime
    prediction: Prediction | None
    skipped_reason: str | None
    actual_home_win: bool | None = None
    actual_margin: int | None = None
    actual_total: int | None = None
    actual_home_score: int | None = None
    actual_away_score: int | None = None


class BacktestEngine:
    def __init__(self, repo: Repository, predictor: Predictor, config: BacktestConfig | None = None):
        self._repo = repo
        self._predictor = predictor
        self._config = config or BacktestConfig()
        self._pipeline = FeaturePipeline(
            repo,
            decay_lambda=self._config.decay_lambda,
            window_days=self._config.window_days,
        )

    def run(self, game_ids: list[str] | None = None) -> "BacktestResult":
        games = (
            self._repo.list_games(status=GameStatus.FINAL)
            if game_ids is None
            else [g for g in (self._repo.get_game(gid) for gid in game_ids) if g is not None]
        )
        games = sorted(games, key=lambda g: (g.game_date, g.game_id))
        records: list[BacktestRecord] = []
        for g in games:
            as_of = g.game_date
            if self._warmup_not_met(g, as_of):
                records.append(BacktestRecord(
                    game_id=g.game_id, as_of=as_of, prediction=None,
                    skipped_reason=f"warmup < {self._config.warmup_games} prior games",
                ))
                continue
            try:
                features = self._pipeline.compute(g.game_id, as_of)
                prediction = self._predictor.predict(features)
                records.append(BacktestRecord(
                    game_id=g.game_id, as_of=as_of, prediction=prediction,
                    skipped_reason=None,
                    actual_home_win=g.home_score > g.away_score,
                    actual_margin=g.home_score - g.away_score,
                    actual_total=g.home_score + g.away_score,
                    actual_home_score=g.home_score,
                    actual_away_score=g.away_score,
                ))
            except FeatureComputationError as exc:
                records.append(BacktestRecord(
                    game_id=g.game_id, as_of=as_of, prediction=None,
                    skipped_reason=f"features: {exc}",
                ))
        return BacktestResult(tuple(records))

    def _warmup_not_met(self, g, as_of: datetime) -> bool:
        if self._config.warmup_games <= 0:
            return False
        history = self._repo.list_games(as_of=as_of, status=GameStatus.FINAL)
        for team_id in (g.home_team_id, g.away_team_id):
            prior = sum(
                1 for h in history
                if h.game_id != g.game_id
                and h.game_date < as_of
                and team_id in (h.home_team_id, h.away_team_id)
            )
            if prior < self._config.warmup_games:
                return True
        return False


class BacktestResult:
    def __init__(self, records: tuple[BacktestRecord, ...]) -> None:
        self.records = records

    @property
    def n_games(self) -> int:
        return len(self.records)

    @property
    def n_predicted(self) -> int:
        return sum(1 for r in self.records if r.prediction is not None)

    @property
    def n_skipped(self) -> int:
        return self.n_games - self.n_predicted

    def reliability_table(self) -> list[ReliabilityBin]:
        recs = [r for r in self.records if r.prediction is not None and r.actual_home_win is not None]
        return build_reliability_table(
            [r.prediction.prob_home for r in recs], [r.actual_home_win for r in recs]
        )

    def win_prob_metrics(self) -> dict:
        recs = [r for r in self.records if r.prediction is not None and r.actual_home_win is not None]
        if not recs:
            return {"n_win_prob": 0, "accuracy": None, "log_loss": None,
                    "brier_score": None, "ece": None}
        probs = [r.prediction.prob_home for r in recs]
        outcomes = [r.actual_home_win for r in recs]
        table = build_reliability_table(probs, outcomes)
        return {
            "n_win_prob": len(recs),
            "accuracy": accuracy(outcomes, probs),
            "log_loss": log_loss(outcomes, probs),
            "brier_score": brier_score(outcomes, probs),
            "ece": expected_calibration_error(table),
        }

    def margin_metrics(self) -> dict:
        recs = [
            r for r in self.records
            if r.prediction is not None
            and r.prediction.expected_margin is not None
            and r.actual_margin is not None
        ]
        if not recs:
            return {"n_margin": 0, "margin_mae": None, "margin_rmse": None}
        return {
            "n_margin": len(recs),
            "margin_mae": mae(
                [float(r.actual_margin) for r in recs],
                [float(r.prediction.expected_margin) for r in recs],
            ),
            "margin_rmse": rmse(
                [float(r.actual_margin) for r in recs],
                [float(r.prediction.expected_margin) for r in recs],
            ),
        }

    def total_metrics(self) -> dict:
        recs = [
            r for r in self.records
            if r.prediction is not None
            and r.prediction.expected_total is not None
            and r.actual_total is not None
        ]
        if not recs:
            return {"n_total": 0, "total_mae": None, "total_rmse": None}
        return {
            "n_total": len(recs),
            "total_mae": mae(
                [float(r.actual_total) for r in recs],
                [float(r.prediction.expected_total) for r in recs],
            ),
            "total_rmse": rmse(
                [float(r.actual_total) for r in recs],
                [float(r.prediction.expected_total) for r in recs],
            ),
        }

    def summary(self) -> dict:
        return {
            "n_games": self.n_games,
            "n_predicted": self.n_predicted,
            "n_skipped": self.n_skipped,
            **self.win_prob_metrics(),
            **self.margin_metrics(),
            **self.total_metrics(),
        }


def sweep_decay_lambda(
    repo: Repository,
    lambdas: list[float],
    *,
    predictor: Predictor | None = None,
    n_simulations: int = 500,
    warmup_games: int = 5,
    seed: int = 7,
) -> dict[float, dict]:
    """Grid-search the time-decay lambda by walk-forward backtest.

    Spec §10: lambda must be determined by historical backtesting, not by
    intuition.  Each lambda is evaluated with the exact same chronological
    protocol; the caller picks the best value from the returned summaries
    (and should prefer out-of-sample stability over tiny gains).
    """
    outcomes: dict[float, dict] = {}
    for lam in lambdas:
        engine = BacktestEngine(
            repo,
            predictor or SimulationPredictor(n_simulations=n_simulations, seed=seed),
            BacktestConfig(
                n_simulations=n_simulations,
                seed=seed,
                warmup_games=warmup_games,
                decay_lambda=lam,
            ),
        )
        outcomes[lam] = engine.run().summary()
    return outcomes
