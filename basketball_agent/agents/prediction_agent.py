"""Prediction Agent: the end-to-end pre-game prediction orchestrator.

Flow: reality check -> leakage-safe features -> statistical models ->
Monte Carlo -> risk assessment -> ensemble probability.  Any reality or
temporal-integrity FAIL aborts the prediction (spec §2.2, §22) — the agent
never fabricates probabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from basketball_agent import __version__
from basketball_agent.agents.ensemble_agent import EnsembleAgent, EnsemblePrediction, EnsembleWeights
from basketball_agent.agents.risk_agent import RiskAgent, RiskAssessment
from basketball_agent.data.db.repository import Repository
from basketball_agent.evaluation.leakage import check_temporal_integrity, reality_check
from basketball_agent.features.base import GameFeatures
from basketball_agent.features.feature_pipeline import FeaturePipeline
from basketball_agent.models.game_params import GamePredictionParams, build_game_params
from basketball_agent.models.win_probability import WinProbabilityConfig
from basketball_agent.simulation.monte_carlo import MonteCarloResult, MonteCarloRunner


class PredictionError(RuntimeError):
    """Raised when a prediction cannot be produced (reality/leakage/data)."""


@dataclass(frozen=True)
class GamePrediction:
    game_id: str
    as_of: datetime
    features: GameFeatures
    params: GamePredictionParams
    monte_carlo: MonteCarloResult
    risk: RiskAssessment
    ensemble: EnsemblePrediction
    model_version: str
    snapshot_id: str | None

    @property
    def win_probability_home(self) -> float:
        return self.ensemble.prob_home

    @property
    def win_probability_away(self) -> float:
        return 1.0 - self.ensemble.prob_home

    @property
    def expected_home_score(self) -> float:
        return self.monte_carlo.home_score.mean

    @property
    def expected_away_score(self) -> float:
        return self.monte_carlo.away_score.mean

    @property
    def expected_margin(self) -> float:
        return self.params.expected_margin

    @property
    def expected_total(self) -> float:
        return self.params.expected_total


class PredictionAgent:
    def __init__(
        self,
        repo: Repository,
        *,
        n_simulations: int = 10000,
        seed: int | None = 42,
        decay_lambda: float = 0.0,
        ensemble_weights: EnsembleWeights | None = None,
        snapshot_id: str | None = None,
        model_version: str = __version__,
        include_non_regular_history: bool = False,
        win_config: "WinProbabilityConfig | None" = None,
    ) -> None:
        self._repo = repo
        self._n_simulations = n_simulations
        self._seed = seed
        self._decay_lambda = decay_lambda
        self._ensemble = EnsembleAgent(
            ensemble_weights, n_simulations=n_simulations, seed=seed if seed is not None else 7
        )
        self._risk = RiskAgent()
        self._snapshot_id = snapshot_id
        self._model_version = model_version
        # Phase 8-C1: optional CALIBRATED strength parameters (explicit
        # --params path); None keeps the frozen defaults untouched.
        self._win_config = win_config
        self._pipeline = FeaturePipeline(
            repo,
            decay_lambda=decay_lambda,
            include_non_regular_history=include_non_regular_history,
        )

    def predict(self, game_id: str, as_of: datetime | None = None) -> GamePrediction:
        as_of = as_of or datetime.now(timezone.utc)

        reality = reality_check(self._repo, game_id, as_of)
        if reality.status == "FAIL":
            raise PredictionError("reality validation FAILED: " + "; ".join(reality.violations))

        features = self._pipeline.compute(game_id, as_of)

        leakage = check_temporal_integrity(features, self._repo)
        if leakage.status == "FAIL":
            raise PredictionError("leakage detection FAILED: " + "; ".join(leakage.violations))

        params = build_game_params(features, win_config=self._win_config)
        mc = MonteCarloRunner(seed=self._seed).run(params, n_simulations=self._n_simulations)
        risk = self._risk.assess(features, params, mc)
        ensemble = self._ensemble.predict(features, mc)
        return GamePrediction(
            game_id=game_id,
            as_of=as_of,
            features=features,
            params=params,
            monte_carlo=mc,
            risk=risk,
            ensemble=ensemble,
            model_version=self._model_version,
            snapshot_id=self._snapshot_id,
        )
