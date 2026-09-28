"""Final Agent Orchestration (Phase 9-E).

Single deterministic, auditable entry point that chains the frozen layers:

    Prediction Core (8-L)
      → AgentDecision        (9-A, agents/decision.py)
      → RiskResult           (9-B, agents/risk_analysis.py)
      → DecisionRecord       (9-C, agents/decision_record.py)
      → ExplanationResult    (9-D, agents/explanation.py)
    = AgentBundle (this module)

``AgentOrchestrator.orchestrate`` ONLY calls and assembles the existing
capabilities — it never recomputes a probability, risk, decision state or
explanation; it never mutates the frozen layers; ``as_of`` MUST be
provided explicitly by the caller (no clock is ever consulted); the same
inputs always yield a fieldwise-equal bundle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from basketball_agent.agents.decision import AgentDecision, DecisionAgent
from basketball_agent.agents.decision_contract import CalibrationProvenance
from basketball_agent.agents.decision_record import DecisionRecord, DecisionRecordAgent
from basketball_agent.agents.explanation import ExplanationAgent, ExplanationResult
from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent, RiskResult


@dataclass(frozen=True)
class AgentBundle:
    """The complete, immutable output of one orchestration run."""

    agent_decision: AgentDecision          # 9-A (context + result + status + core risk)
    risk_result: RiskResult                # 9-B
    decision_record: DecisionRecord        # 9-C (rule trace + evidence references)
    explanation: ExplanationResult         # 9-D

    @property
    def context(self):
        """9-A PredictionContext (league/season/as_of/regulation/provenance)."""
        return self.agent_decision.context

    @property
    def evidence_trace(self) -> tuple:
        """decision → evidence → source references (9-C, by reference)."""
        return self.decision_record.evidence_references

    @property
    def validation_status(self):
        return self.agent_decision.status

    @property
    def decision_state(self):
        return self.decision_record.decision_state

    @property
    def primary_prob_home(self) -> float:
        """The frozen core's primary probability, verbatim."""
        return self.agent_decision.result.primary_prob_home


class AgentOrchestrator:
    """Assembles the frozen agent layers into one bundle."""

    def __init__(
        self,
        prediction_agent: PredictionAgent,
        *,
        calibration: CalibrationProvenance | None = None,
    ) -> None:
        # 9-A consumer wraps the frozen core exactly once
        self._decision_agent = DecisionAgent(prediction_agent,
                                             calibration=calibration)
        self._risk_agent = RiskAnalysisAgent()
        self._record_agent = DecisionRecordAgent()
        self._explanation_agent = ExplanationAgent()

    def orchestrate(self, game_id: str, as_of: datetime) -> AgentBundle:
        """Run the full chain for one game.

        ``as_of`` is REQUIRED (the caller owns the information boundary —
        the orchestrator never consults a clock).
        """
        if as_of is None:
            raise TypeError(
                "as_of must be provided explicitly by the caller; the "
                "orchestrator never uses the current time")
        decision = self._decision_agent.decide(game_id, as_of=as_of)
        risk = self._risk_agent.analyze(decision)
        record = self._record_agent.build(decision, risk)
        explanation = self._explanation_agent.explain(record)
        return AgentBundle(
            agent_decision=decision,
            risk_result=risk,
            decision_record=record,
            explanation=explanation,
        )


def format_agent_bundle(bundle: AgentBundle) -> str:
    """Read-only text rendering of one bundle (CLI adapter helper)."""
    ctx = bundle.context
    r = bundle.agent_decision.result
    lines = [
        "AGENT BUNDLE (read-only orchestration)",
        "=" * 60,
        f"Game: {ctx.game_id}  [{ctx.league} {ctx.season or ''}]",
        f"As-of boundary: {ctx.as_of}",
        f"Teams: {ctx.home_team_id} vs {ctx.away_team_id}",
        f"Regulation: {ctx.regulation_seconds:.0f}s",
        f"Validation status: {bundle.validation_status.value}",
        "",
        "--- prediction (frozen core, verbatim) ---",
        f"Primary family: {r.primary_family} "
        f"(source: {r.primary_family_source})",
        f"Primary probability: {r.primary_prob_home:.4f} home / "
        f"{r.primary_prob_away:.4f} away -> {r.predicted_winner}",
        f"Expected: margin {r.expected_margin:+.1f}, total {r.expected_total:.1f}",
        "",
        "--- decision ---",
        f"State: {bundle.decision_state.value}",
        f"Rule: {bundle.decision_record.rule_trace.rule_id}",
        f"Conditions: {dict(bundle.decision_record.rule_trace.conditions)}",
        "",
        "--- risk facts (9-B) ---",
        f"Flags: {list(bundle.risk_result.flags) or 'none'}",
        f"Tossup band: {bundle.risk_result.probability_facts.tossup_band}",
        f"Model dispersion: "
        f"{bundle.risk_result.model_agreement_facts.dispersion_band}",
        "",
        "--- provenance / evidence trace ---",
        f"Snapshot: {ctx.snapshot_id or 'n/a'}  |  Model: "
        f"{bundle.agent_decision.model_version}",
        f"Calibration: {ctx.calibration_source}",
    ]
    for ref in bundle.evidence_trace:
        lines.append(f"  - {ref.name}: {ref.location} [{ref.source}]")
    lines.append("")
    lines.append("--- limitations ---")
    for item in bundle.explanation.limitations:
        lines.append(f"  - {item}")
    return "\n".join(lines)
