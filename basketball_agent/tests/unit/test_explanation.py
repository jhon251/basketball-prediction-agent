"""Explanation Layer tests (Phase 9-D)."""
from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import datetime, timezone

import pytest

from basketball_agent.agents.decision import DecisionAgent
from basketball_agent.agents.decision_record import (
    DecisionRecordAgent,
    DecisionState,
)
from basketball_agent.agents.explanation import (
    ExplanationAgent,
    ExplanationResult,
)
from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent
from basketball_agent.data.db.repository import Repository

AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)


def _chain(scheduled_target, **agent_kwargs):
    core = PredictionAgent(Repository(scheduled_target),
                           n_simulations=50, seed=7, **agent_kwargs)
    decision = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
    risk = RiskAnalysisAgent().analyze(decision)
    record = DecisionRecordAgent().build(decision, risk)
    return decision, risk, record


@pytest.fixture()
def explanation(scheduled_target) -> ExplanationResult:
    _, _, record = _chain(scheduled_target)
    return ExplanationAgent().explain(record)


class TestSchema:
    def test_required_sections(self, explanation):
        for name in ("decision_state_explanation", "prediction_explanation",
                     "evidence_explanation", "risk_explanation",
                     "calibration_provenance_explanation", "limitations",
                     "evidence_trace"):
            assert getattr(explanation, name) is not None

    def test_no_composite_score_fields(self):
        forbidden = {"score", "quality", "confidence", "uncertainty",
                     "error_probability", "win_probability_estimate"}
        names = {f.name for f in dc_fields(ExplanationResult)}
        assert not (names & forbidden)


class TestVerbatimReferences:
    def test_prediction_values_quoted_verbatim(self, scheduled_target,
                                                explanation):
        _, _, record = _chain(scheduled_target)
        px = explanation.prediction_explanation
        assert px["primary_prob_home"] == \
            record.prediction_summary["primary_prob_home"]
        assert px["expected_margin"] == \
            record.prediction_summary["expected_margin"]
        # the statement embeds the same value (4 decimals of the same float)
        assert f"{record.prediction_summary['primary_prob_home']:.4f}" \
            in px["statement"]

    def test_model_probabilities_quoted(self, scheduled_target, explanation):
        _, _, record = _chain(scheduled_target)
        ex = explanation.evidence_explanation
        assert ex["p_elo"] == record.evidence_summary["p_elo"]
        assert ex["pairwise_disagreement"] == \
            record.evidence_summary["pairwise_disagreement"]
        assert ex["simulation_distribution"] == \
            record.evidence_summary["simulation_distribution"]

    def test_risk_facts_quoted(self, scheduled_target, explanation):
        _, risk, _ = _chain(scheduled_target)
        rx = explanation.risk_explanation
        assert rx["tossup_indicator"] == \
            risk.probability_facts.tossup_indicator
        assert rx["tossup_band"] == risk.probability_facts.tossup_band
        assert rx["margin_std"] == risk.distribution_facts.margin_std
        assert rx["flags"] == risk.flags


class TestStateExplanations:
    def test_rule_trace_explains_the_state(self, scheduled_target,
                                            explanation):
        _, _, record = _chain(scheduled_target)
        dx = explanation.decision_state_explanation
        assert dx["state"] == record.decision_state.value
        assert dx["rule_id"] == record.rule_trace.rule_id
        assert dx["conditions"] == dict(record.rule_trace.conditions)
        assert record.rule_trace.rule_id in dx["statement"]

    def test_supported_state_honest(self, explanation):
        assert explanation.decision_state_explanation["state"] == \
            "prediction_supported"

    def test_fallback_not_upgraded(self, scheduled_target):
        decision, _, _ = _chain(scheduled_target)
        from basketball_agent.agents.decision_contract import ValidationStatus
        from dataclasses import replace

        stub = replace(decision, status=ValidationStatus.FALLBACK,
                       context=replace(decision.context, league="G_LEAGUE",
                                       validation_status=ValidationStatus.FALLBACK))
        risk = RiskAnalysisAgent().analyze(stub)
        record = DecisionRecordAgent().build(stub, risk)
        x = ExplanationAgent().explain(record)
        assert x.decision_state_explanation["state"] == "fallback"
        assert "NOT a validated choice" in \
            x.decision_state_explanation["statement"]
        assert any("not a validated choice" in s for s in x.limitations)
        # the validation status is reported verbatim, never upgraded
        assert x.validation_status == "fallback"

    def test_unresolved_and_insufficient_honest(self, scheduled_target):
        decision, _, _ = _chain(scheduled_target)
        from basketball_agent.agents.decision_contract import ValidationStatus
        from dataclasses import replace

        for status, expected_text in (
            (ValidationStatus.UNRESOLVED, "no family decision exists"),
            (ValidationStatus.INSUFFICIENT_EVIDENCE,
             "insufficient_evidence"),
        ):
            stub = replace(decision, status=status)
            risk = RiskAnalysisAgent().analyze(stub)
            record = DecisionRecordAgent().build(stub, risk)
            x = ExplanationAgent().explain(record)
            assert x.decision_state_explanation["state"] == status.value
            assert expected_text in x.decision_state_explanation["statement"]

    def test_caution_states_both_facts(self, scheduled_target):
        decision, risk, _ = _chain(scheduled_target)
        from basketball_agent.agents.risk_analysis import (
            ModelAgreementFacts, ProbabilityFacts, RiskResult,
        )
        from dataclasses import replace

        pf = replace(risk.probability_facts, tossup_indicator=True)
        ma = replace(risk.model_agreement_facts,
                     directional_disagreement=True)
        caution_risk = replace(risk, probability_facts=pf,
                               model_agreement_facts=ma)
        record = DecisionRecordAgent().build(decision, caution_risk)
        x = ExplanationAgent().explain(record)
        assert x.decision_state_explanation["state"] == \
            "prediction_with_caution"
        assert "tossup_indicator=True" in \
            x.decision_state_explanation["statement"]
        assert "directional_disagreement=True" in \
            x.decision_state_explanation["statement"]


class TestContractPassthrough:
    def test_ot_probability_none_preserved(self, explanation):
        dist = explanation.evidence_explanation["simulation_distribution"]
        assert dist["ot_probability"] is None
        assert "not provided by the frozen core" in \
            explanation.evidence_explanation["statement"]
        assert any("not estimated anywhere" in s for s in explanation.limitations)

    def test_cba_dual_regulation(self, scheduled_target):
        decision, risk, _ = _chain(scheduled_target)
        from basketball_agent.agents.decision_contract import (
            PredictionContext, ValidationStatus,
        )
        from dataclasses import replace

        for season, regulation in (("2024-25", 2880.0), ("2025-26", 2400.0)):
            ctx = replace(decision.context, league="CBA", season=season,
                          regulation_seconds=regulation)
            stub = replace(decision, context=ctx)
            risk2 = RiskAnalysisAgent().analyze(stub)
            record = DecisionRecordAgent().build(stub, risk2)
            x = ExplanationAgent().explain(record)
            # provenance fact quoted verbatim; seasons never share a value
            assert record.risk_summary.provenance_facts.regulation_seconds \
                == regulation
            assert x.game_id == record.game_id

    def test_evidence_trace_unchanged(self, scheduled_target):
        decision, risk, record = _chain(scheduled_target)
        x = ExplanationAgent().explain(record)
        assert x.evidence_trace is record.evidence_references
        assert len(x.evidence_trace) == 7


class TestDeterminismAndPurity:
    def test_same_record_identical_explanation(self, scheduled_target):
        _, _, record = _chain(scheduled_target)
        x1 = ExplanationAgent().explain(record)
        x2 = ExplanationAgent().explain(record)
        assert x1 == x2

    def test_no_runtime_inputs(self):
        import inspect
        from basketball_agent.agents import explanation as mod

        src = inspect.getsource(mod)
        for banned in ("datetime.now", "import random", "random.",
                       "requests", "urllib", "openai", "time.time",
                       "http"):
            assert banned not in src

    def test_immutable_output(self, explanation):
        with pytest.raises(Exception):
            explanation.decision_state_explanation = {}  # frozen dataclass


class TestLimitations:
    def test_limitations_include_contract_and_ethics_items(self, explanation):
        joined = " ".join(explanation.limitations)
        assert "ot_probability" in joined
        assert "no betting, ROI or stake guidance" in joined

    def test_calibration_limitation_when_calibrated(self, scheduled_target):
        decision, risk, _ = _chain(scheduled_target)
        from basketball_agent.agents.decision_contract import (
            CalibrationProvenance, PredictionContext,
        )
        from dataclasses import replace

        cal = CalibrationProvenance(
            params_file="storage/params/c2_nba_2025-26_1s.json",
            fitted_window=("2024-10-01", "2025-07-01"),
            fitted_at="2026-09-27T00:00:00+00:00",
            validation_logloss=0.6113, boundary_hits=())
        ctx = replace(decision.context, calibration=cal,
                      calibration_source=cal.source_label)
        stub = replace(decision, context=ctx)
        risk2 = RiskAnalysisAgent().analyze(stub)
        record = DecisionRecordAgent().build(stub, risk2)
        x = ExplanationAgent().explain(record)
        assert "c2_nba_2025-26_1s.json" in \
            x.calibration_provenance_explanation["statement"]
        assert any("not validated for other periods" in s
                   for s in x.limitations)

    def test_frozen_defaults_statement(self, explanation):
        assert "frozen defaults" in \
            explanation.calibration_provenance_explanation["statement"]
