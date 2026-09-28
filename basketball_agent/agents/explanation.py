"""Explanation Layer (Phase 9-D): read-only structured explanations.

``ExplanationAgent`` consumes one Phase 9-C ``DecisionRecord`` and produces
an ``ExplanationResult`` — a structured, deterministic explanation of WHY
the decision looks the way it does.  It is a PURE CONSUMER one step below
the frozen layers:

* every value quoted in an explanation is the verbatim value already
  stored on the record / its RiskResult (no recomputation of
  probabilities, risks, thresholds or model outputs);
* no composite score, quality grade, confidence number, win-rate
  threshold or hidden rule is introduced; statements are deterministic
  f-string renderings of existing facts and fixed definitions;
* no LLM, network, randomness or current time — the same record always
  yields a field-identical ExplanationResult;
* fallback / unresolved / insufficient_evidence leagues are explained
  honestly and are never upgraded to "validated" or "supported";
* contract-absent values (e.g. ``ot_probability = None``) are explained
  as not-provided, never invented.

Chain: Prediction Core → AgentDecision (9-A) → RiskResult (9-B) →
DecisionRecord (9-C) → ExplanationResult (9-D, this layer).
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.agents.decision_record import DecisionRecord, DecisionState
from basketball_agent.agents.decision_contract import ValidationStatus

#: fixed definitions reused verbatim from earlier frozen phases
TOSSUP_BAND = (0.45, 0.55)

_STATE_BASE_STATEMENTS: dict[DecisionState, str] = {
    DecisionState.PREDICTION_SUPPORTED:
        "configured league without a caution trigger: the primary "
        "probability is supported by the frozen prediction core",
    DecisionState.PREDICTION_WITH_CAUTION:
        "configured league with the caution conjunction met: the primary "
        "probability is a statistical tossup AND the model probabilities "
        "disagree directionally (facts, not an error estimate)",
    DecisionState.INSUFFICIENT_EVIDENCE:
        "the league's family configuration is explicitly marked "
        "insufficient_evidence: no family choice is supported by evidence",
    DecisionState.UNRESOLVED:
        "no family decision exists for this league",
    DecisionState.FALLBACK:
        "the league is unconfigured: the primary family is the inherited "
        "default (fallback), NOT a validated choice",
}


@dataclass(frozen=True)
class ExplanationResult:
    """Structured explanation of one DecisionRecord (all values quoted
    verbatim from the record and its RiskResult)."""

    game_id: str
    league: str
    season: str | None
    as_of: str
    decision_state_explanation: dict
    prediction_explanation: dict
    evidence_explanation: dict
    risk_explanation: dict
    calibration_provenance_explanation: dict
    limitations: tuple[str, ...]
    evidence_trace: tuple            # the record's EvidenceReference tuple, by reference
    validation_status: str


def _state_explanation(record: DecisionRecord) -> dict:
    trace = record.rule_trace
    state = record.decision_state
    conditions_text = ", ".join(f"{name}={value}" for name, value in trace.conditions)
    return {
        "state": state.value,
        "rule_id": trace.rule_id,
        "conditions": dict(trace.conditions),
        "statement": (
            f"Decision state '{state.value}' was assigned by rule "
            f"'{trace.rule_id}' with evaluated conditions: {conditions_text}. "
            + _STATE_BASE_STATEMENTS[state]
        ),
    }


def _prediction_explanation(record: DecisionRecord) -> dict:
    s = record.prediction_summary
    return {
        "primary_family": s["primary_family"],
        "primary_prob_home": s["primary_prob_home"],
        "primary_prob_away": s["primary_prob_away"],
        "predicted_winner": s["predicted_winner"],
        "expected_margin": s["expected_margin"],
        "expected_total": s["expected_total"],
        "statement": (
            f"The primary probability ({s['primary_prob_home']:.4f} home / "
            f"{s['primary_prob_away']:.4f} away) was produced by the frozen "
            f"prediction core (family '{s['primary_family']}') and is passed "
            "through verbatim; this layer recomputes nothing."
        ),
    }


def _evidence_explanation(record: DecisionRecord) -> dict:
    e = record.evidence_summary
    pairwise = e["pairwise_disagreement"]
    pair_text = ", ".join(f"{k}={v:.4f}" if v is not None else f"{k}=n/a"
                          for k, v in pairwise.items())
    dist = e["simulation_distribution"]
    ot = dist["ot_probability"]
    return {
        "p_elo": e["p_elo"],
        "p_strength": e["p_strength"],
        "p_sim": e["p_sim"],
        "pairwise_disagreement": dict(pairwise),
        "directional_disagreement": e["directional_disagreement"],
        "simulation_distribution": dict(dist),
        "statement": (
            f"Model probabilities: elo={e['p_elo']}, "
            f"strength={e['p_strength']}, sim={e['p_sim']}; pairwise "
            f"differences ({pair_text}) are facts from the frozen models, "
            "not an error estimate. The simulation distribution is quoted "
            "as-is from the frozen MonteCarloResult"
            + (f"; ot_probability is {ot} (not provided by the frozen core, "
               "therefore not invented here)." if ot is None else ".")
        ),
    }


def _risk_explanation(record: DecisionRecord) -> dict:
    risk = record.risk_summary
    pf = risk.probability_facts
    ma = risk.model_agreement_facts
    df = risk.distribution_facts
    return {
        "flags": risk.flags,
        "tossup_band": pf.tossup_band,
        "tossup_indicator": pf.tossup_indicator,
        "max_pairwise_disagreement": ma.max_pairwise_disagreement,
        "dispersion_band": ma.dispersion_band,
        "margin_std": df.margin_std,
        "total_std": df.total_std,
        "ot_probability": df.ot_probability,
        "statement": (
            f"Risk facts: tossup_indicator={pf.tossup_indicator} "
            f"(fixed band {pf.tossup_band[0]}-{pf.tossup_band[1]}), "
            f"dispersion_band='{ma.dispersion_band}', margin_std="
            f"{df.margin_std}, total_std={df.total_std}. Spread and "
            "disagreement are descriptive facts; they are not prediction "
            "errors, calibrated uncertainties, or a failure signal."
        ),
    }


def _calibration_explanation(record: DecisionRecord) -> dict:
    e = record.evidence_summary
    calibration = e["calibration"]
    source = e["calibration_source"]
    if calibration is None:
        statement = (
            "No calibration was applied: the frozen defaults "
            "(HCA 2.5 / scale 11.5 / k 5) produced this prediction."
        )
    else:
        statement = (
            f"Calibrated parameters were applied via explicit opt-in from "
            f"'{calibration.params_file}' (fitted window "
            f"{calibration.fitted_window[0]} .. {calibration.fitted_window[1]}, "
            f"validation logloss {calibration.validation_logloss})."
        )
    return {
        "calibration_source": source,
        "calibration": calibration,
        "statement": statement,
    }


def _limitations(record: DecisionRecord) -> tuple[str, ...]:
    limitations = list(record.risk_summary.evidence_facts.known_limitations)
    limitations.append(
        "ot_probability is not provided by the frozen core and is not "
        "estimated anywhere in this layer")
    limitations.append(
        "this explanation contains no betting, ROI or stake guidance by "
        "design")
    if record.risk_summary.evidence_facts.has_calibration:
        limitations.append(
            "fitted parameters apply to their own league and training "
            "window only; they are not validated for other periods")
    return tuple(limitations)


class ExplanationAgent:
    """Pure consumer of one DecisionRecord; produces ExplanationResult."""

    def explain(self, record: DecisionRecord) -> ExplanationResult:
        return ExplanationResult(
            game_id=record.game_id,
            league=record.league,
            season=record.season,
            as_of=record.as_of,
            decision_state_explanation=_state_explanation(record),
            prediction_explanation=_prediction_explanation(record),
            evidence_explanation=_evidence_explanation(record),
            risk_explanation=_risk_explanation(record),
            calibration_provenance_explanation=_calibration_explanation(record),
            limitations=_limitations(record),
            evidence_trace=record.evidence_references,
            validation_status=record.validation_status.value,
        )
