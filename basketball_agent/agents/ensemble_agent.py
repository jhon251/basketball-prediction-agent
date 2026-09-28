"""Ensemble Agent (spec §21) — EXPERIMENTAL / DIAGNOSTIC layer (Phase 8-K).

Combines Elo / team strength / simulation probabilities with weights that
are TRAINED on historical data (walk-forward backtest, grid search
minimizing log loss on the training window) — never a hand-picked average,
and never an LLM judgment.  The fit is intentionally transparent and
deterministic; if the simulation adds nothing on the training window, the
weights will honestly reflect that.

Phase 8-K positioning (evidence: PHASE8_J_ARCHITECTURE_AUDIT.md): this
layer is EXPERIMENTAL/DIAGNOSTIC, not a production family.  The default
weights ``(0, 0, 1)`` mean the primary probability IS the simulation win
rate — they are NOT a three-model blend, and the ``ensemble`` name must
not be read as one.  Production primary probabilities are routed by the
explicit per-league family configuration (``agents.family_config``);
reaching this layer's combination in production requires passing explicit
``EnsembleWeights`` (opt-in).  No weight refitting is planned without a
new pre-registered protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    EloPredictor,
    SimulationPredictor,
    StrengthPredictor,
)
from basketball_agent.evaluation.metrics import log_loss
from basketball_agent.features.base import GameFeatures
from basketball_agent.models.game_params import build_game_params
from basketball_agent.models.pace_model import PaceModel
from basketball_agent.models.win_probability import WinProbabilityModel
from basketball_agent.simulation.monte_carlo import MonteCarloResult


@dataclass(frozen=True)
class EnsembleWeights:
    """Weights for Elo / strength / simulation (must be >= 0 and sum to 1)."""

    w_elo: float = 0.0
    w_strength: float = 0.0
    w_sim: float = 1.0

    def __post_init__(self) -> None:
        if min(self.w_elo, self.w_strength, self.w_sim) < 0.0:
            raise ValueError("ensemble weights must be non-negative")
        if abs(self.w_elo + self.w_strength + self.w_sim - 1.0) > 1e-9:
            raise ValueError("ensemble weights must sum to 1")


@dataclass(frozen=True)
class EnsemblePrediction:
    prob_home: float
    p_elo: float
    p_strength: float
    p_sim: float
    weights: EnsembleWeights


class EnsembleAgent:
    def __init__(
        self,
        weights: EnsembleWeights | None = None,
        *,
        n_simulations: int = 1000,
        seed: int = 7,
    ) -> None:
        self.weights = weights or EnsembleWeights()  # default: simulation only
        self.n_simulations = n_simulations
        self.seed = seed

    def combine_probs(
        self, p_elo: float, p_strength: float, p_sim: float,
        weights: EnsembleWeights | None = None,
    ) -> float:
        w = weights or self.weights
        return w.w_elo * p_elo + w.w_strength * p_strength + w.w_sim * p_sim

    def predict(self, features: GameFeatures, mc_result: MonteCarloResult | None = None) -> EnsemblePrediction:
        p_elo = features.elo_win_prob_home if features.elo_win_prob_home is not None else 0.5
        params = build_game_params(features)
        p_strength = params.strength_win_prob_home
        if mc_result is not None:
            p_sim = mc_result.home_win_probability
        else:
            p_sim = SimulationPredictor(
                n_simulations=self.n_simulations, seed=self.seed
            ).predict(features).prob_home
        combined = self.combine_probs(p_elo, p_strength, p_sim)
        return EnsemblePrediction(
            prob_home=combined, p_elo=p_elo, p_strength=p_strength, p_sim=p_sim,
            weights=self.weights,
        )

    def fit(
        self,
        repo: Repository,
        cutoff: datetime,
        *,
        n_simulations: int = 300,
        seed: int = 7,
        warmup_games: int = 5,
        grid_step: float = 0.1,
        train_start: datetime | None = None,
    ) -> EnsembleWeights:
        """Train the weights on FINAL games in ``[train_start, cutoff)``.

        Each component's per-game probabilities come from its own
        walk-forward backtest over the training window (no look-ahead), and
        the weight grid is scored with log loss over games where all three
        components exist.
        """
        train_ids = [
            g.game_id for g in repo.list_games(status=GameStatus.FINAL)
            if g.game_date < cutoff
            and (train_start is None or g.game_date >= train_start)
        ]
        config = BacktestConfig(n_simulations=n_simulations, seed=seed, warmup_games=warmup_games)
        probs: dict[str, dict[str, float]] = {}
        outcomes: dict[str, bool] = {}
        for name, predictor in (
            ("elo", EloPredictor()),
            ("strength", StrengthPredictor()),
            ("sim", SimulationPredictor(n_simulations=n_simulations, seed=seed)),
        ):
            result = BacktestEngine(repo, predictor, config).run(game_ids=train_ids)
            for r in result.records:
                if r.prediction is None or r.actual_home_win is None:
                    continue
                probs.setdefault(r.game_id, {})[name] = r.prediction.prob_home
                outcomes[r.game_id] = r.actual_home_win

        eligible = [gid for gid, ps in probs.items() if set(ps) == {"elo", "strength", "sim"}]
        best_weights: EnsembleWeights | None = None
        best_loss: float | None = None
        n = round(1.0 / grid_step)
        for i in range(n + 1):
            for j in range(n + 1 - i):
                k = n - i - j
                w = EnsembleWeights(i / n, j / n, k / n)
                combined = [
                    self.combine_probs(probs[gid]["elo"], probs[gid]["strength"],
                                       probs[gid]["sim"], w)
                    for gid in eligible
                ]
                loss = log_loss([outcomes[gid] for gid in eligible], combined) if eligible else 0.0
                if best_loss is None or loss < best_loss - 1e-12:
                    best_loss, best_weights = loss, w
        self.weights = best_weights or EnsembleWeights()
        return self.weights
