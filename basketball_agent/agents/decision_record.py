"""DecisionRecord layer (Phase 9-C): the auditable decision protocol.

``DecisionRecordAgent`` consumes one Phase 9-A ``AgentDecision`` plus its
Phase 9-B ``RiskResult`` and produces an explicit, deterministic,
auditable ``DecisionRecord``.  It is a PURE CONSUMER on top of frozen
layers:

* prediction probabilities are passed through verbatim from the 9-A
  ``PredictionResult`` (never recomputed);
* the primary family and calibration are never changed;
* the 9-B ``RiskResult`` is embedded by reference (never redefined);
* the decision state comes from a SMALL set of explicit boolean rules
  over facts that already exist — no weights, no composite scores, no
  LLM, no betting semantics, no future information.

Naming note: Phase 9-A already has a ``DecisionAgent`` (the frozen-core
wrapper producing ``AgentDecision``); this phase's agent is therefore
``DecisionRecordAgent`` — it is the decision layer proper, one step
further down the pipeline:

Prediction Core → AgentDecision (9-A) → RiskResult (9-B) → DecisionRecord (9-C).

Decision states are "the state of the evidence for THIS decision" — they
are NOT model accuracy, win rate, error probability, profit expectation,
or a model-quality score.  No state named "validated" exists: fallback
leagues are recorded as FALLBACK and nothing in this layer can upgrade
them (the DecisionState enum has no such member, by construction).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from basketball_agent.agents.decision import AgentDecision
from basketball_agent.agents.decision_contract import ValidationStatus
from basketball_agent.agents.risk_analysis import RiskResult


class DecisionState(str, Enum):
    """Explicit, auditable decision states (Phase 9-C requirement 2.5).

    Mapping is one-directional and deterministic; there is deliberately NO
    "validated" state — a fallback league can never be re-labelled here.
    """

    PREDICTION_SUPPORTED = "prediction_supported"
    PREDICTION_WITH_CAUTION = "prediction_with_caution"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNRESOLVED = "unresolved"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class DecisionRuleTrace:
    """Which rule fired and which facts it evaluated (audit trail)."""

    rule_id: str
    conditions: tuple[tuple[str, object], ...]   # (fact_name, value) evaluated
    state: DecisionState


@dataclass(frozen=True)
class EvidenceReference:
    """decision → evidence → source traceability entry."""

    name: str
    location: str      # where in the contract the fact lives
    source: str        # which frozen layer produced it


@dataclass(frozen=True)
class DecisionRecord:
    """The auditable record of one decision.  All probabilities and family
    fields are pass-throughs of the 9-A contract; ``risk_summary`` IS the
    9-B RiskResult (embedded by reference, never redefined)."""

    game_id: str
    league: str
    season: str | None
    home_team_id: str
    away_team_id: str
    as_of: str
    primary_family: str
    validation_status: ValidationStatus

    prediction_summary: object         # frozen-core facts (see _prediction_summary)
    evidence_summary: object           # model/distribution/status facts
    risk_summary: RiskResult           # the 9-B object, by reference
    decision_state: DecisionState
    rule_trace: DecisionRuleTrace
    evidence_references: tuple[EvidenceReference, ...]
    model_version: str
    snapshot_id: str | None


def _prediction_summary(decision: AgentDecision) -> dict:
    r = decision.result
    return {
        "primary_prob_home": r.primary_prob_home,
        "primary_prob_away": r.primary_prob_away,
        "predicted_winner": r.predicted_winner,
        "expected_margin": r.expected_margin,
        "expected_total": r.expected_total,
        "primary_family": r.primary_family,
    }


def _evidence_summary(decision: AgentDecision, risk: RiskResult) -> dict:
    diag = decision.result.diagnostics
    dist = decision.result.simulation_distribution
    ctx = decision.context
    return {
        "p_elo": diag.p_elo,
        "p_strength": diag.p_strength,
        "p_sim": diag.p_sim,
        "pairwise_disagreement": {
            "elo_strength": diag.diff_elo_strength,
            "elo_sim": diag.diff_elo_sim,
            "strength_sim": diag.diff_strength_sim,
        },
        "directional_disagreement": diag.directional_disagreement,
        "simulation_distribution": {
            "n_runs": dist.n_runs,
            "tie_probability": dist.tie_probability,
            "margin_std": None if dist.margin is None else dist.margin.std,
            "total_std": None if dist.total is None else dist.total.std,
            "margin_p5_p50_p95": (None if dist.margin is None else
                                  (dist.margin.p5, dist.margin.p50, dist.margin.p95)),
            "total_p5_p50_p95": (None if dist.total is None else
                                 (dist.total.p5, dist.total.p50, dist.total.p95)),
            "ot_probability": dist.ot_probability,   # None by contract
        },
        "validation_status": decision.status.value,
        "calibration_source": ctx.calibration_source,
        "calibration": ctx.calibration,
        "known_limitations": risk.evidence_facts.known_limitations,
    }


def _evidence_references() -> tuple[EvidenceReference, ...]:
    """The fixed decision → evidence → source trace for every record."""
    return (
        EvidenceReference("primary_probability",
                          "PredictionResult.primary_prob_home/away",
                          "Phase 9-A contract (frozen Prediction Core output)"),
        EvidenceReference("model_agreement",
                          "DiagnosticEvidence (p_elo/p_strength/p_sim, pairwise diffs)",
                          "Phase 9-A contract (frozen core model probabilities)"),
        EvidenceReference("distribution",
                          "SimulationDistribution (score/margin/total stats, buckets)",
                          "frozen core MonteCarloResult via Phase 9-A"),
        EvidenceReference("validation_status",
                          "ValidationStatus / family configuration",
                          "Phase 8-K per-league family config (Phase 8-L frozen)"),
        EvidenceReference("calibration",
                          "PredictionContext.calibration / calibration_source",
                          "Phase 8-C1/8-H fitted params (explicit opt-in) or frozen defaults"),
        EvidenceReference("provenance",
                          "PredictionContext.feature_provenance / snapshot_id",
                          "frozen Prediction Core as-of provenance"),
        EvidenceReference("risk_facts",
                          "RiskResult (probability/agreement/distribution/evidence/provenance facts, flags)",
                          "Phase 9-B RiskAnalysisAgent"),
    )


def decide_state(decision: AgentDecision, risk: RiskResult) -> DecisionRuleTrace:
    """The explicit first-version decision rules.

    Order matters and is deterministic; every condition reads an existing
    fact.  No thresholds beyond the pre-existing 9-A/9-B definitions are
    introduced in this version.
    """
    status = decision.status
    if status is ValidationStatus.FALLBACK:
        return DecisionRuleTrace(
            rule_id="validation_status_fallback",
            conditions=(("validation_status", status.value),),
            state=DecisionState.FALLBACK)
    if status is ValidationStatus.UNRESOLVED:
        return DecisionRuleTrace(
            rule_id="validation_status_unresolved",
            conditions=(("validation_status", status.value),),
            state=DecisionState.UNRESOLVED)
    if status is ValidationStatus.INSUFFICIENT_EVIDENCE:
        return DecisionRuleTrace(
            rule_id="validation_status_insufficient_evidence",
            conditions=(("validation_status", status.value),),
            state=DecisionState.INSUFFICIENT_EVIDENCE)
    # CONFIGURED: caution only on the conjunction of two pre-existing facts
    tossup = risk.probability_facts.tossup_indicator
    directional = risk.model_agreement_facts.directional_disagreement
    conditions = (
        ("tossup_indicator", tossup),
        ("directional_disagreement", directional),
    )
    if tossup and directional:
        return DecisionRuleTrace(
            rule_id="tossup_plus_directional_disagreement",
            conditions=conditions,
            state=DecisionState.PREDICTION_WITH_CAUTION)
    return DecisionRuleTrace(
        rule_id="configured_default_supported",
        conditions=conditions,
        state=DecisionState.PREDICTION_SUPPORTED)


class DecisionRecordAgent:
    """Builds the auditable ``DecisionRecord`` from frozen-layer outputs."""

    def build(self, decision: AgentDecision, risk: RiskResult) -> DecisionRecord:
        ctx = decision.context
        trace = decide_state(decision, risk)
        return DecisionRecord(
            game_id=ctx.game_id,
            league=ctx.league,
            season=ctx.season,
            home_team_id=ctx.home_team_id,
            away_team_id=ctx.away_team_id,
            as_of=ctx.as_of,
            primary_family=ctx.primary_family,
            validation_status=decision.status,
            prediction_summary=_prediction_summary(decision),
            evidence_summary=_evidence_summary(decision, risk),
            risk_summary=risk,
            decision_state=trace.state,
            rule_trace=trace,
            evidence_references=_evidence_references(),
            model_version=decision.model_version,
            snapshot_id=decision.snapshot_id,
        )
