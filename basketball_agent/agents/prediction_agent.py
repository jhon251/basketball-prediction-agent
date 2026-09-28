"""Prediction Agent: the end-to-end pre-game prediction orchestrator.

Flow: reality check -> leakage-safe features -> statistical models ->
Monte Carlo -> risk assessment -> family-routed primary probability ->
ensemble (diagnostic).  Any reality or temporal-integrity FAIL aborts the
prediction (spec §2.2, §22) — the agent never fabricates probabilities.

Phase 8-K semantics: the PRIMARY win probability comes from the league's
explicitly configured prediction family (``agents.family_config``); the
pre-8-K behavior — primary = ensemble with default weights (0,0,1) — is
bit-identical to the SIMULATION family and remains the default/fallback.
The ensemble stays attached as an experimental/diagnostic layer and is
never a configured default family.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from basketball_agent import __version__
from basketball_agent.agents.ensemble_agent import EnsembleAgent, EnsemblePrediction, EnsembleWeights
from basketball_agent.agents.family_config import (
    PredictionFamily,
    resolve_family,
)
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
    # Phase 8-K: which family provided the PRIMARY probability and its
    # value.  With the inherited default (simulation) ``primary_prob_home``
    # is bit-identical to ``ensemble.prob_home`` as produced before 8-K.
    primary_family: str = PredictionFamily.SIMULATION.value
    primary_family_source: str = "configured"
    primary_prob_home: float | None = None

    @property
    def win_probability_home(self) -> float:
        if self.primary_prob_home is not None:
            return self.primary_prob_home
        return self.ensemble.prob_home

    @property
    def win_probability_away(self) -> float:
        return 1.0 - self.win_probability_home

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
        family: "PredictionFamily | str | None" = None,
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
        # Phase 8-K: explicit family override; None resolves per league
        # through the static family configuration (fallback = simulation).
        if family is not None and not isinstance(family, PredictionFamily):
            family = PredictionFamily(family)
        self._family_override = family
        self._ensemble_weights_given = ensemble_weights is not None
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

        # ---- family routing (Phase 8-K) --------------------------------
        if self._family_override is not None:
            resolution = None
            family = self._family_override
            source = "override"
        else:
            resolution = resolve_family(features.game_league)
            family = resolution.family
            source = resolution.source
        if family is PredictionFamily.ENSEMBLE:
            # ensemble is experimental/diagnostic: explicit weights must be
            # provided by the caller — it is never a default family
            if not self._ensemble_weights_given:
                raise PredictionError(
                    "ensemble family requires explicit EnsembleWeights "
                    "(experimental layer, never a default)"
                )
            primary = ensemble.prob_home
        elif family is PredictionFamily.ELO:
            p = features.elo_win_prob_home
            if p is None:
                raise PredictionError("elo family unavailable: Elo probability missing")
            primary = p
        elif family is PredictionFamily.STRENGTH:
            primary = params.strength_win_prob_home
        else:  # SIMULATION (the inherited production behavior)
            if self._ensemble_weights_given:
                # pre-8-K explicit-weights callers keep their exact behavior
                primary = ensemble.prob_home
            else:
                # bit-identical to the old default path: weights (0,0,1)
                # make ensemble.prob_home == the simulation win rate
                primary = mc.home_win_probability

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
            primary_family=family.value,
            primary_family_source=source,
            primary_prob_home=primary,
        )
