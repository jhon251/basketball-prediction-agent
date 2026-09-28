"""DecisionAgent: the Agent-layer consumer of the frozen Prediction Core.

Phase 9-A.  This agent does NOT predict anything itself: it calls the
frozen ``PredictionAgent`` exactly once, converts the result into the
Agent Decision Contract (``decision_contract``), and adds a factual risk
view.  It never overrides the primary family, never recomputes a
probability, and never interprets diagnostics beyond the definitions
documented on each contract field.
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.agents.decision_contract import (
    CalibrationProvenance,
    DecisionRisk,
    DiagnosticEvidence,
    PredictionContext,
    PredictionResult,
    SimulationDistribution,
    ValidationStatus,
    assess_decision,
    league_validation_status,
)
from basketball_agent.agents.prediction_agent import GamePrediction, PredictionAgent


@dataclass(frozen=True)
class AgentDecision:
    """One complete, traceable decision input bundle."""

    context: PredictionContext
    result: PredictionResult
    status: ValidationStatus
    risk: DecisionRisk
    core_risk_confidence_score: float      # frozen RiskAgent pass-through
    model_version: str
    snapshot_id: str | None


class DecisionAgent:
    """Wraps one ``PredictionAgent`` (frozen core) and exposes the contract."""

    def __init__(
        self,
        prediction_agent: PredictionAgent,
        *,
        calibration: CalibrationProvenance | None = None,
    ) -> None:
        self._prediction_agent = prediction_agent
        self._calibration = calibration

    def decide(self, game_id: str, as_of=None) -> AgentDecision:
        prediction: GamePrediction = self._prediction_agent.predict(game_id, as_of=as_of)
        return self.from_prediction(prediction)

    def from_prediction(self, prediction: GamePrediction) -> AgentDecision:
        league = prediction.features.game_league
        status = league_validation_status(league)
        result = PredictionResult.from_prediction(prediction)
        context = PredictionContext(
            game_id=prediction.game_id,
            league=league,
            season=prediction.features.game_season,
            home_team_id=prediction.features.home_team_id,
            away_team_id=prediction.features.away_team_id,
            as_of=prediction.as_of.isoformat(),
            primary_family=prediction.primary_family,
            primary_family_source=prediction.primary_family_source,
            validation_status=status,
            calibration=self._calibration,
            calibration_source=(self._calibration.source_label
                                if self._calibration is not None
                                else "frozen_defaults"),
            snapshot_id=prediction.snapshot_id,
            regulation_seconds=prediction.params.regulation_seconds,
            feature_provenance=prediction.features.provenance,
        )
        risk = assess_decision(result, status, prediction.risk)
        return AgentDecision(
            context=context,
            result=result,
            status=status,
            risk=risk,
            core_risk_confidence_score=prediction.risk.confidence_score,
            model_version=prediction.model_version,
            snapshot_id=prediction.snapshot_id,
        )
