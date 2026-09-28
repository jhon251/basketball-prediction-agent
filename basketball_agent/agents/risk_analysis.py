"""Risk Analysis Layer (Phase 9-B): structured factual risk analysis.

``RiskAnalysisAgent`` consumes one Phase 9-A ``AgentDecision`` bundle
(which already wraps the frozen Prediction Core's output) and organizes
the facts that are ALREADY PRESENT in the contract into a stable
``RiskResult``.  It is a PURE CONSUMER:

* it never recomputes a probability, overrides a family, or selects a
  model;
* it never renames or reinterprets frozen semantics — spread is spread
  (not "prediction error"), model disagreement is disagreement (not
  "failure"), a fallback league is not "validated";
* it produces no composite score ("risk = 73") — every number is a raw
  fact with a documented definition, and the flag list is deterministic;
* all state comes from the single game's contract — no league-shared
  state, no cross-league thresholds, no future information.

Determinism: the same ``AgentDecision`` always yields a field-identical
``RiskResult`` (pure function; unit-tested).
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.agents.decision import AgentDecision
from basketball_agent.agents.decision_contract import (
    ValidationStatus,
)

#: fixed tossup band — the SAME definition as Phase 9-A's
#: ``DecisionRisk.tossup_indicator`` (never redefined here)
TOSSUP_BAND = (0.45, 0.55)

#: descriptive dispersion buckets for model-probability dispersion.
#: These are FACT BANDS (documented numeric ranges), not quality grades.
DISPERSION_BANDS = (
    ("directional_disagreement", None),  # special: assigned first, see below
    ("dispersion_le_0.05", 0.05),
    ("dispersion_0.05_to_0.15", 0.15),
    ("dispersion_gt_0.15", None),
)


@dataclass(frozen=True)
class ProbabilityFacts:
    """Category: probability.  Raw facts around the primary probability."""

    primary_family: str
    primary_family_source: str
    primary_prob_home: float
    primary_prob_away: float
    predicted_winner: str
    distance_from_tossup: float          # |p - 0.5| (pure arithmetic fact)
    tossup_indicator: bool               # p inside TOSSUP_BAND (9-A definition)
    tossup_band: tuple[float, float]     # the band itself, for traceability


@dataclass(frozen=True)
class ModelAgreementFacts:
    """Category: model agreement.  Facts from DiagnosticEvidence only."""

    p_elo: float | None
    p_strength: float
    p_sim: float
    diff_elo_strength: float | None
    diff_elo_sim: float | None
    diff_strength_sim: float
    max_pairwise_disagreement: float | None
    directional_disagreement: bool
    dispersion_band: str                 # descriptive numeric bucket (see DISPERSION_BANDS)
    diff_primary_vs_elo: float | None
    diff_primary_vs_strength: float
    diff_primary_vs_sim: float


@dataclass(frozen=True)
class DistributionFacts:
    """Category: distribution.  Simulation spread facts (spread is NOT
    prediction error and NOT a calibrated uncertainty).  Stats fields are
    ``None`` only when the consumed bundle carries no distribution — the
    frozen core always provides them."""

    n_simulations: int
    n_runs: int
    tie_probability: float
    margin_std: float | None
    total_std: float | None
    margin_p5: float | None
    margin_p50: float | None
    margin_p95: float | None
    margin_width: float | None           # p95 - p5 (pure arithmetic fact)
    total_p5: float | None
    total_p50: float | None
    total_p95: float | None
    total_width: float | None
    home_score_std: float | None
    away_score_std: float | None
    ot_probability: float | None         # stays None by contract (not provided by frozen core)


@dataclass(frozen=True)
class EvidenceFacts:
    """Category: evidence sufficiency.  Status-derived facts only."""

    validation_status: ValidationStatus
    calibration_source: str              # "frozen_defaults" or "calibrated:<file>"
    has_calibration: bool
    known_limitations: tuple[str, ...]   # fixed, status-derived factual statements


@dataclass(frozen=True)
class ProvenanceFacts:
    """Category: provenance.  Pass-through of the decision's provenance."""

    league: str
    season: str | None
    as_of: str
    snapshot_id: str | None
    model_version: str
    regulation_seconds: float
    feature_provenance: object           # frozen core FeatureProvenance (reference)


#: fixed, status-derived factual limitation statements (no interpretation
#: beyond what the status itself already means per Phase 8-L/9-A)
_STATUS_LIMITATIONS: dict[ValidationStatus, tuple[str, ...]] = {
    ValidationStatus.CONFIGURED: (
        "league has an explicit family configuration; configured does not "
        "mean universally optimal",
    ),
    ValidationStatus.FALLBACK: (
        "league unconfigured: primary family is the inherited default, "
        "not a validated choice",
    ),
    ValidationStatus.UNRESOLVED: (
        "league family decision not made",
    ),
    ValidationStatus.INSUFFICIENT_EVIDENCE: (
        "available evidence explicitly marked insufficient for a family choice",
    ),
    ValidationStatus.VALIDATED: (),      # reserved status: no automatic facts
}


@dataclass(frozen=True)
class RiskResult:
    """Structured factual risk analysis of ONE game's decision bundle.

    No composite score exists by design (Phase 9-B): every field is a raw
    fact, a fixed-definition indicator, or a pass-through.
    """

    game_id: str
    league: str
    as_of: str
    probability_facts: ProbabilityFacts
    model_agreement_facts: ModelAgreementFacts
    distribution_facts: DistributionFacts
    evidence_facts: EvidenceFacts
    provenance_facts: ProvenanceFacts
    flags: tuple[str, ...]               # deterministic factual flags (see _build_flags)
    core_risk: object                    # frozen DecisionRisk pass-through (semantics unchanged)
    validation_status: ValidationStatus


def _build_flags(prob: ProbabilityFacts, agree: ModelAgreementFacts,
                 evidence: EvidenceFacts,
                 core_flags: tuple) -> tuple[str, ...]:
    """Deterministic factual flags.  Each flag states a FACT; none implies
    a probability of being wrong or a model failure."""
    flags: list[str] = []
    if prob.tossup_indicator:
        flags.append("tossup_band")
    if agree.directional_disagreement:
        flags.append("directional_model_disagreement")
    if evidence.validation_status is ValidationStatus.FALLBACK:
        flags.append("validation_fallback")
    elif evidence.validation_status is ValidationStatus.UNRESOLVED:
        flags.append("validation_unresolved")
    elif evidence.validation_status is ValidationStatus.INSUFFICIENT_EVIDENCE:
        flags.append("validation_insufficient_evidence")
    if not evidence.has_calibration:
        flags.append("calibration_frozen_defaults")
    flags.extend(f"core:{f.code}" for f in core_flags)
    return tuple(flags)


class RiskAnalysisAgent:
    """Pure consumer of one ``AgentDecision``; produces ``RiskResult``."""

    def analyze(self, decision: AgentDecision) -> RiskResult:
        result = decision.result
        diag = result.diagnostics
        dist = result.simulation_distribution
        ctx = decision.context

        prob = ProbabilityFacts(
            primary_family=result.primary_family,
            primary_family_source=result.primary_family_source,
            primary_prob_home=result.primary_prob_home,
            primary_prob_away=result.primary_prob_away,
            predicted_winner=result.predicted_winner,
            distance_from_tossup=abs(result.primary_prob_home - 0.5),
            tossup_indicator=decision.risk.tossup_indicator,
            tossup_band=TOSSUP_BAND,
        )

        if diag.directional_disagreement:
            band = "directional_disagreement"
        elif diag.diff_strength_sim is not None:
            pairs = [d for d in (diag.diff_elo_strength, diag.diff_elo_sim,
                                 diag.diff_strength_sim) if d is not None]
            max_d = max(pairs) if pairs else 0.0
            if max_d <= 0.05:
                band = "dispersion_le_0.05"
            elif max_d <= 0.15:
                band = "dispersion_0.05_to_0.15"
            else:
                band = "dispersion_gt_0.15"
        else:
            band = "dispersion_le_0.05"
        agreement = ModelAgreementFacts(
            p_elo=diag.p_elo,
            p_strength=diag.p_strength,
            p_sim=diag.p_sim,
            diff_elo_strength=diag.diff_elo_strength,
            diff_elo_sim=diag.diff_elo_sim,
            diff_strength_sim=diag.diff_strength_sim,
            max_pairwise_disagreement=decision.risk.max_pairwise_disagreement,
            directional_disagreement=diag.directional_disagreement,
            dispersion_band=band,
            diff_primary_vs_elo=diag.diff_primary_vs_elo,
            diff_primary_vs_strength=diag.diff_primary_vs_strength,
            diff_primary_vs_sim=diag.diff_primary_vs_sim,
        )

        def _stat(stats, name):
            return None if stats is None else getattr(stats, name)

        margin_width = (dist.margin.p95 - dist.margin.p5
                        if dist.margin is not None else None)
        total_width = (dist.total.p95 - dist.total.p5
                       if dist.total is not None else None)
        distribution = DistributionFacts(
            n_simulations=dist.n_simulations,
            n_runs=dist.n_runs,
            tie_probability=dist.tie_probability,
            margin_std=_stat(dist.margin, "std"),
            total_std=_stat(dist.total, "std"),
            margin_p5=_stat(dist.margin, "p5"),
            margin_p50=_stat(dist.margin, "p50"),
            margin_p95=_stat(dist.margin, "p95"),
            margin_width=margin_width,
            total_p5=_stat(dist.total, "p5"),
            total_p50=_stat(dist.total, "p50"),
            total_p95=_stat(dist.total, "p95"),
            total_width=total_width,
            home_score_std=_stat(dist.home_score, "std"),
            away_score_std=_stat(dist.away_score, "std"),
            ot_probability=dist.ot_probability,   # stays None by contract
        )

        evidence = EvidenceFacts(
            validation_status=decision.status,
            calibration_source=ctx.calibration_source,
            has_calibration=ctx.calibration is not None,
            known_limitations=_STATUS_LIMITATIONS[decision.status],
        )

        provenance = ProvenanceFacts(
            league=ctx.league,
            season=ctx.season,
            as_of=ctx.as_of,
            snapshot_id=ctx.snapshot_id,
            model_version=decision.model_version,
            regulation_seconds=ctx.regulation_seconds,
            feature_provenance=ctx.feature_provenance,
        )

        return RiskResult(
            game_id=ctx.game_id,
            league=ctx.league,
            as_of=ctx.as_of,
            probability_facts=prob,
            model_agreement_facts=agreement,
            distribution_facts=distribution,
            evidence_facts=evidence,
            provenance_facts=provenance,
            flags=_build_flags(prob, agreement, evidence,
                               decision.risk.core_risk_flags),
            core_risk=decision.risk,
            validation_status=decision.status,
        )
