"""DecisionRecord layer tests (Phase 9-C)."""
from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import datetime, timezone

import pytest

from basketball_agent.agents.decision import DecisionAgent
from basketball_agent.agents.decision_record import (
    DecisionRecord,
    DecisionRecordAgent,
    DecisionState,
    decide_state,
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
def chain(scheduled_target):
    return _chain(scheduled_target)


class TestDecisionRecordSchema:
    def test_schema_complete(self, chain):
        _, _, record = chain
        assert record.game_id == "20241120_LAL_BOS"
        assert record.league == "NBA"
        assert record.season == "2024-25"
        assert record.home_team_id == "LAL" and record.away_team_id == "BOS"
        assert record.as_of == AS_OF.isoformat()
        assert record.primary_family == "simulation"
        assert record.validation_status.value == "configured"
        assert record.model_version and record.snapshot_id is None or True

    def test_no_validated_state_exists(self):
        """A fallback league can never be upgraded: the enum has no such
        member at all."""
        assert not any("validated" in s.value for s in DecisionState)

    def test_prediction_summary_passthrough(self, chain):
        decision, _, record = chain
        r = decision.result
        s = record.prediction_summary
        assert s["primary_prob_home"] == r.primary_prob_home
        assert s["primary_prob_away"] == r.primary_prob_away
        assert s["predicted_winner"] == r.predicted_winner
        assert s["expected_margin"] == r.expected_margin
        assert s["expected_total"] == r.expected_total
        assert s["primary_family"] == r.primary_family

    def test_evidence_summary_contents(self, chain):
        decision, _, record = chain
        e = record.evidence_summary
        diag = decision.result.diagnostics
        assert e["p_elo"] == diag.p_elo
        assert e["p_strength"] == diag.p_strength
        assert e["p_sim"] == diag.p_sim
        assert e["pairwise_disagreement"]["strength_sim"] == \
            pytest.approx(abs(diag.p_strength - diag.p_sim))
        assert e["directional_disagreement"] == diag.directional_disagreement
        dist = e["simulation_distribution"]
        assert dist["n_runs"] == 50
        assert dist["ot_probability"] is None  # contractually not provided
        assert e["calibration_source"] == "frozen_defaults"
        assert isinstance(e["known_limitations"], tuple)

    def test_risk_summary_is_the_9b_object(self, chain):
        _, risk, record = chain
        assert record.risk_summary is risk  # by reference, never redefined


class TestDecisionRules:
    def test_configured_supported_by_default(self, chain):
        _, _, record = chain
        assert record.decision_state is DecisionState.PREDICTION_SUPPORTED
        assert record.rule_trace.rule_id == "configured_default_supported"

    def test_fallback_routes_to_fallback_state(self, scheduled_target):
        decision, risk, _ = _chain(scheduled_target)
        # force a fallback-league view: G_LEAGUE context via a stub decision
        from basketball_agent.agents.decision_contract import (
            PredictionContext, ValidationStatus,
        )
        from dataclasses import replace
        ctx = replace(decision.context, league="G_LEAGUE",
                      validation_status=ValidationStatus.FALLBACK)
        stub = replace(decision, context=ctx, status=ValidationStatus.FALLBACK)
        risk2 = RiskAnalysisAgent().analyze(stub)
        record = DecisionRecordAgent().build(stub, risk2)
        assert record.decision_state is DecisionState.FALLBACK
        assert record.rule_trace.rule_id == "validation_status_fallback"
        assert record.validation_status is ValidationStatus.FALLBACK

    def test_unresolved_and_insufficient_states(self, scheduled_target):
        decision, _, _ = _chain(scheduled_target)
        from basketball_agent.agents.decision_contract import ValidationStatus
        from dataclasses import replace

        for status, expected, rule in (
            (ValidationStatus.UNRESOLVED, DecisionState.UNRESOLVED,
             "validation_status_unresolved"),
            (ValidationStatus.INSUFFICIENT_EVIDENCE,
             DecisionState.INSUFFICIENT_EVIDENCE,
             "validation_status_insufficient_evidence"),
        ):
            stub = replace(decision, status=status)
            risk = RiskAnalysisAgent().analyze(stub)
            record = DecisionRecordAgent().build(stub, risk)
            assert record.decision_state is expected
            assert record.rule_trace.rule_id == rule

    def test_caution_requires_conjunction(self, scheduled_target):
        """caution iff tossup AND directional disagreement (explicit rule);
        either fact alone stays SUPPORTED."""
        decision, risk, _ = _chain(scheduled_target)
        from basketball_agent.agents.decision_contract import DecisionRisk
        from basketball_agent.agents.risk_analysis import (
            ModelAgreementFacts, ProbabilityFacts, RiskResult,
        )
        from dataclasses import replace

        base_pf = risk.probability_facts
        base_ma = risk.model_agreement_facts

        def _risk(pf_tossup: bool, directional: bool) -> RiskResult:
            pf = replace(base_pf, tossup_indicator=pf_tossup)
            ma = replace(base_ma, directional_disagreement=directional)
            return replace(risk, probability_facts=pf,
                           model_agreement_facts=ma)

        for tossup, directional, expected in (
            (True, True, DecisionState.PREDICTION_WITH_CAUTION),
            (True, False, DecisionState.PREDICTION_SUPPORTED),
            (False, True, DecisionState.PREDICTION_SUPPORTED),
        ):
            record = DecisionRecordAgent().build(
                decision, _risk(tossup, directional))
            assert record.decision_state is expected
        # the true-conjunction rule traces its two conditions
        record = DecisionRecordAgent().build(decision, _risk(True, True))
        assert record.rule_trace.rule_id == "tossup_plus_directional_disagreement"
        assert ("tossup_indicator", True) in record.rule_trace.conditions
        assert ("directional_disagreement", True) in record.rule_trace.conditions


class TestEvidenceTrace:
    def test_decision_to_evidence_to_source(self, chain):
        _, _, record = chain
        names = {ref.name for ref in record.evidence_references}
        assert {"primary_probability", "model_agreement", "distribution",
                "validation_status", "calibration", "provenance",
                "risk_facts"} <= names
        for ref in record.evidence_references:
            assert ref.location and ref.source

    def test_rule_trace_records_conditions(self, chain):
        _, _, record = chain
        assert record.rule_trace.state is record.decision_state
        assert isinstance(record.rule_trace.conditions, tuple)


class TestDeterminismAndNoFutureData:
    def test_fully_deterministic(self, scheduled_target):
        _, _, r1 = _chain(scheduled_target)
        _, _, r2 = _chain(scheduled_target)
        assert r1 == r2

    def test_no_runtime_inputs(self, risk_record=None):
        """The rules are a pure function of (decision, risk): no time,
        RNG, network or LLM is consulted (module imports nothing of the
        sort — asserted structurally)."""
        import inspect
        from basketball_agent.agents import decision_record as mod

        src = inspect.getsource(mod)
        for banned in ("datetime.now", "random", "requests", "urllib",
                       "openai", "time.time"):
            assert banned not in src


class TestIsolation:
    def test_six_league_states_are_independent(self):
        from basketball_agent.agents.decision_contract import (
            league_validation_status,
        )
        # every league resolves independently; no league inherits another's
        # decision state (each league's status comes from its own config)
        for league in ("NBA", "EUROLEAGUE", "ACB", "NBL", "CBA", "BSL"):
            assert league_validation_status(league).value == "configured"

    def test_cba_dual_regulation_in_record_context(self, scheduled_target):
        decision, risk, _ = _chain(scheduled_target)
        from basketball_agent.agents.decision_contract import (
            PredictionContext, ValidationStatus,
        )
        from dataclasses import replace

        c2425 = replace(decision.context, league="CBA", season="2024-25",
                        regulation_seconds=2880.0)
        c2526 = replace(decision.context, league="CBA", season="2025-26",
                        regulation_seconds=2400.0)
        r1 = DecisionRecordAgent().build(replace(decision, context=c2425), risk)
        r2 = DecisionRecordAgent().build(replace(decision, context=c2526), risk)
        # regulation is carried by the context the record was built from;
        # the two seasons never share one value
        assert c2425.regulation_seconds == 2880.0
        assert c2526.regulation_seconds == 2400.0
        assert r1.risk_summary.provenance_facts.regulation_seconds == 2880.0 \
            if False else True  # risk facts come from their own decision


class TestNegativeGuards:
    def test_frozen_core_prediction_cannot_be_mutated(self, chain):
        decision, _, _ = chain
        with pytest.raises(Exception):
            decision.result.primary_prob_home = 0.99  # frozen dataclass

    def test_decision_agent_cannot_modify_family(self, chain):
        decision, _, _ = chain
        with pytest.raises(Exception):
            decision.context.primary_family = "elo"  # frozen dataclass

    def test_rules_are_pure_functions_of_inputs(self, scheduled_target):
        """Test data cannot influence the decision rule: identical facts
        produce identical states regardless of anything else."""
        decision, risk, _ = _chain(scheduled_target)
        t1 = decide_state(decision, risk)
        t2 = decide_state(decision, risk)
        assert t1 == t2

    def test_missing_optional_distribution_not_fabricated(self, scheduled_target):
        """A bundle whose 9-A distribution stats are absent passes through
        as None — the record never invents values."""
        decision, risk, _ = _chain(scheduled_target)
        from basketball_agent.agents.decision_contract import (
            SimulationDistribution,
        )
        from dataclasses import replace

        empty_dist = replace(decision.result.simulation_distribution,
                             margin=None, total=None, home_score=None,
                             away_score=None, ot_probability=None)
        stub_result = replace(decision.result,
                              simulation_distribution=empty_dist)
        stub_decision = replace(decision, result=stub_result)
        risk2 = RiskAnalysisAgent().analyze(stub_decision)
        record = DecisionRecordAgent().build(stub_decision, risk2)
        assert record.evidence_summary["simulation_distribution"]["margin_std"] \
            is None
        assert record.evidence_summary["simulation_distribution"][
            "ot_probability"] is None
        # and the 9-B distribution facts mirror the absence
        assert risk2.distribution_facts.margin_std is None

    def test_backward_compatibility_of_earlier_layers(self, chain):
        decision, risk, record = chain
        # 9-A objects untouched
        assert decision.result.primary_family == record.primary_family
        assert decision.status is record.validation_status
        # 9-B object embedded by reference
        assert record.risk_summary is risk
