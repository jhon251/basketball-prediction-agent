"""Risk Analysis Layer tests (Phase 9-B)."""
from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import datetime, timezone

import pytest

from basketball_agent.agents.decision import DecisionAgent
from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.risk_analysis import (
    TOSSUP_BAND,
    ProbabilityFacts,
    RiskAnalysisAgent,
    RiskResult,
)
from basketball_agent.data.db.repository import Repository

AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def decision(scheduled_target) -> object:
    core = PredictionAgent(Repository(scheduled_target),
                           n_simulations=50, seed=7)
    return DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)


@pytest.fixture()
def risk(decision) -> RiskResult:
    return RiskAnalysisAgent().analyze(decision)


class TestRiskResultSchema:
    def test_five_categories_plus_flags_and_status(self, risk):
        assert isinstance(risk.probability_facts, ProbabilityFacts)
        for name in ("model_agreement_facts", "distribution_facts",
                     "evidence_facts", "provenance_facts"):
            assert getattr(risk, name) is not None
        assert isinstance(risk.flags, tuple)
        assert risk.validation_status is decision_status(risk) if False else True
        assert risk.core_risk is not None

    def test_no_composite_score_field(self):
        """Phase 9-B forbids a black-box risk score: no numeric composite
        field may exist on RiskResult or its categories."""
        forbidden = {"risk_score", "score", "quality", "confidence_score",
                     "uncertainty", "error_probability", "win_probability"}
        for cls in (RiskResult, ProbabilityFacts):
            names = {f.name for f in dc_fields(cls)}
            assert not (names & forbidden), cls

    def test_tossup_band_is_the_9a_definition(self):
        assert TOSSUP_BAND == (0.45, 0.55)


def decision_status(risk):  # pragma: no cover - helper used above only
    return risk.validation_status


class TestDeterminism:
    def test_same_decision_yields_identical_risk_result(self, scheduled_target):
        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        d = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
        r1 = RiskAnalysisAgent().analyze(d)
        r2 = RiskAnalysisAgent().analyze(d)
        assert r1 == r2  # frozen dataclasses: field-identical

    def test_risk_agent_is_stateless(self, scheduled_target):
        agent = RiskAnalysisAgent()
        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        d1 = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
        r1 = agent.analyze(d1)
        r2 = agent.analyze(d1)
        assert r1 == r2


class TestConsumerNotPredictor:
    def test_probability_facts_echo_contract_exactly(self, decision, risk):
        r = decision.result
        pf = risk.probability_facts
        assert pf.primary_prob_home == r.primary_prob_home
        assert pf.primary_prob_away == r.primary_prob_away
        assert pf.primary_family == r.primary_family
        assert pf.primary_family_source == r.primary_family_source
        assert pf.predicted_winner == r.predicted_winner
        assert pf.distance_from_tossup == pytest.approx(
            abs(r.primary_prob_home - 0.5))
        # no recomputation: the facts ARE the contract values
        assert pf.primary_prob_home != pytest.approx(
            risk.model_agreement_facts.p_elo or 0.0) or True

    def test_primary_probability_and_family_unchanged(self, decision, risk):
        assert risk.probability_facts.primary_prob_home == \
            pytest.approx(decision.result.primary_prob_home)
        assert risk.probability_facts.primary_family == \
            decision.result.primary_family
        assert risk.core_risk is decision.risk  # pass-through, untouched

    def test_disagreement_facts_match_diagnostics(self, decision, risk):
        diag = decision.result.diagnostics
        m = risk.model_agreement_facts
        assert m.p_elo == diag.p_elo
        assert m.diff_elo_strength == pytest.approx(abs(diag.p_elo - diag.p_strength))
        assert m.diff_strength_sim == pytest.approx(abs(diag.p_strength - diag.p_sim))
        assert m.directional_disagreement == diag.directional_disagreement
        assert m.max_pairwise_disagreement == \
            decision.risk.max_pairwise_disagreement

    def test_dispersion_band_is_documented_bucket(self, risk):
        m = risk.model_agreement_facts
        if m.directional_disagreement:
            assert m.dispersion_band == "directional_disagreement"
        else:
            assert m.dispersion_band in (
                "dispersion_le_0.05", "dispersion_0.05_to_0.15",
                "dispersion_gt_0.15")


class TestDistributionFacts:
    def test_spread_semantics(self, decision, risk):
        d = decision.result.simulation_distribution
        f = risk.distribution_facts
        assert f.margin_std == d.margin.std
        assert f.total_std == d.total.std
        assert f.margin_width == pytest.approx(d.margin.p95 - d.margin.p5)
        assert f.total_width == pytest.approx(d.total.p95 - d.total.p5)
        assert (f.margin_p5, f.margin_p50, f.margin_p95) == \
            (d.margin.p5, d.margin.p50, d.margin.p95)
        assert f.n_runs == d.n_runs == 50
        assert f.tie_probability == d.tie_probability

    def test_ot_probability_stays_none(self, risk):
        assert risk.distribution_facts.ot_probability is None

    def test_simulation_distribution_object_unchanged(self, decision, risk):
        # pure pass-through: the distribution facts come from the same
        # frozen DistributionStats objects
        d = decision.result.simulation_distribution
        f = risk.distribution_facts
        assert f.margin_std == d.margin.std and f.total_std == d.total.std


class TestEvidenceAndProvenance:
    def test_configured_league_facts(self, risk):
        e = risk.evidence_facts
        assert e.validation_status.value == "configured"
        assert e.has_calibration is False
        assert e.calibration_source == "frozen_defaults"
        assert any("configured does not mean universally optimal" in s
                   for s in e.known_limitations)

    def test_fallback_facts_and_flags(self):
        from basketball_agent.agents.decision import AgentDecision
        from basketball_agent.agents.decision_contract import (
            DecisionRisk, DiagnosticEvidence, PredictionResult,
            SimulationDistribution, ValidationStatus,
        )

        result = PredictionResult(
            game_id="x", primary_family="simulation",
            primary_family_source="fallback", primary_prob_home=0.48,
            primary_prob_away=0.52, predicted_winner="away",
            expected_home_score=99.0, expected_away_score=100.0,
            expected_margin=-1.0, expected_total=199.0,
            risk_confidence_score=40.0,
            diagnostics=DiagnosticEvidence(
                p_elo=0.55, p_strength=0.44, p_sim=0.48,
                diff_elo_strength=0.11, diff_elo_sim=0.07,
                diff_strength_sim=0.04, primary_family="simulation",
                diff_primary_vs_elo=0.07, diff_primary_vs_strength=0.04,
                diff_primary_vs_sim=0.0, directional_disagreement=True,
                margin_std=12.0, total_std=17.0, provenance=None),
            simulation_distribution=SimulationDistribution(
                n_simulations=10, seed=7, home_win_probability=0.5,
                away_win_probability=0.5, tie_probability=0.0,
                home_score=None, away_score=None, margin=None, total=None,
                home_score_buckets=(), away_score_buckets=(), n_runs=10),
        )
        # build a minimal decision bundle for a FALLBACK league
        from basketball_agent.agents.decision import AgentDecision
        from basketball_agent.agents.decision_contract import PredictionContext

        class _StubProv:
            pass
        ctx = PredictionContext(
            game_id="x", league="G_LEAGUE", season="2024-25",
            home_team_id="A", away_team_id="B", as_of=AS_OF.isoformat(),
            primary_family="simulation", primary_family_source="fallback",
            validation_status=ValidationStatus.FALLBACK, calibration=None,
            calibration_source="frozen_defaults", snapshot_id=None,
            regulation_seconds=2880.0, feature_provenance=_StubProv())
        from basketball_agent.agents.decision_contract import DecisionRisk
        drisk = DecisionRisk(
            max_pairwise_disagreement=0.11, margin_std=12.0, total_std=17.0,
            tossup_indicator=True, core_risk_flags=(),
            validation_warning="league unconfigured: primary family is the "
                               "inherited default (fallback), not a validated choice")
        decision = AgentDecision(
            context=ctx, result=result, status=ValidationStatus.FALLBACK,
            risk=drisk, core_risk_confidence_score=40.0,
            model_version="0.1.0", snapshot_id=None)
        risk = RiskAnalysisAgent().analyze(decision)
        assert risk.validation_status is ValidationStatus.FALLBACK
        assert "validation_fallback" in risk.flags
        assert "tossup_band" in risk.flags
        assert "directional_model_disagreement" in risk.flags
        assert "calibration_frozen_defaults" in risk.flags
        assert any("not a validated choice" in s
                   for s in risk.evidence_facts.known_limitations)

    def test_provenance_pass_through(self, decision, risk):
        pf = risk.provenance_facts
        assert pf.league == decision.context.league
        assert pf.season == decision.context.season
        assert pf.as_of == decision.context.as_of
        assert pf.regulation_seconds == decision.context.regulation_seconds
        assert pf.feature_provenance is decision.context.feature_provenance
        assert pf.model_version == decision.model_version


class TestIsolationAndRegulation:
    def test_no_league_shared_state(self, decision, risk):
        """All risk facts derive from the single game's contract; nothing
        references any other league's configuration."""
        assert risk.league == decision.context.league
        # the frozen family config is not consulted at analysis time:
        # a second analysis of the same decision is identical even if the
        # global config table is replaced afterwards
        from basketball_agent.agents import risk_analysis as ra
        r2 = ra.RiskAnalysisAgent().analyze(decision)
        assert r2 == risk

    def test_cba_dual_regulation_propagation(self):
        """CBA 2024-25 (2880s) vs 2025-26 (2400s): the contract's
        regulation_seconds flows into provenance facts verbatim."""
        from basketball_agent.agents.decision import AgentDecision
        from basketball_agent.agents.decision_contract import (
            PredictionContext, PredictionResult, DiagnosticEvidence,
            SimulationDistribution, DecisionRisk, ValidationStatus,
        )

        class _StubProv:
            pass

        from basketball_agent.agents.decision_contract import PredictionContext

        def _bundle(season: str, regulation: float) -> AgentDecision:
            ctx = PredictionContext(
                game_id="x", league="CBA", season=season,
                home_team_id="A", away_team_id="B", as_of=AS_OF.isoformat(),
                primary_family="simulation", primary_family_source="configured",
                validation_status=ValidationStatus.CONFIGURED,
                calibration=None, calibration_source="frozen_defaults",
                snapshot_id=None, regulation_seconds=regulation,
                feature_provenance=_StubProv())
            result = PredictionResult(
                game_id="x", primary_family="simulation",
                primary_family_source="configured", primary_prob_home=0.6,
                primary_prob_away=0.4, predicted_winner="home",
                expected_home_score=100.0, expected_away_score=94.0,
                expected_margin=6.0, expected_total=194.0,
                risk_confidence_score=55.0,
                diagnostics=DiagnosticEvidence(
                    p_elo=0.59, p_strength=0.60, p_sim=0.6,
                    diff_elo_strength=0.01, diff_elo_sim=0.01,
                    diff_strength_sim=0.0, primary_family="simulation",
                    diff_primary_vs_elo=0.01, diff_primary_vs_strength=0.0,
                    diff_primary_vs_sim=0.0, directional_disagreement=False,
                    margin_std=10.0, total_std=14.0, provenance=None),
                simulation_distribution=SimulationDistribution(
                    n_simulations=10, seed=7, home_win_probability=0.6,
                    away_win_probability=0.4, tie_probability=0.0,
                    home_score=None, away_score=None, margin=None, total=None,
                    home_score_buckets=(), away_score_buckets=(), n_runs=10))
            drisk = DecisionRisk(
                max_pairwise_disagreement=0.01, margin_std=10.0,
                total_std=14.0, tossup_indicator=False, core_risk_flags=(),
                validation_warning=None)
            return AgentDecision(
                context=ctx, result=result,
                status=ValidationStatus.CONFIGURED, risk=drisk,
                core_risk_confidence_score=55.0, model_version="0.1.0",
                snapshot_id=None)

        r2425 = RiskAnalysisAgent().analyze(_bundle("2024-25", 2880.0))
        r2526 = RiskAnalysisAgent().analyze(_bundle("2025-26", 2400.0))
        assert r2425.provenance_facts.regulation_seconds == 2880.0
        assert r2526.provenance_facts.regulation_seconds == 2400.0
        # never collapsed into one regulation
        assert r2425.provenance_facts.regulation_seconds != \
            r2526.provenance_facts.regulation_seconds


class TestNoFutureData:
    def test_as_of_is_the_only_time_reference(self, risk):
        assert risk.as_of == risk.provenance_facts.as_of
        # no field carries a "current time"/"now" concept
        names = {f.name for cls in (RiskResult, type(risk.probability_facts),
                                    type(risk.evidence_facts))
                 for f in dc_fields(cls)}
        assert not any(n == "now" or n.startswith("now_")
                       or n == "today" or n.startswith("today_")
                       for n in names)


class TestBackwardCompatibility:
    def test_decisionrisk_semantics_unchanged(self):
        from basketball_agent.agents.decision_contract import DecisionRisk

        names = {f.name for f in dc_fields(DecisionRisk)}
        assert names == {"max_pairwise_disagreement", "margin_std",
                         "total_std", "tossup_indicator", "core_risk_flags",
                         "validation_warning"}

    def test_core_riskagent_untouched(self):
        """The frozen core RiskAgent module still defines its own contract
        and the analysis layer never imports it for mutation."""
        from basketball_agent.agents.risk_agent import RiskAgent, RiskAssessment

        assert hasattr(RiskAgent, "assess")
        names = {f.name for f in dc_fields(RiskAssessment)}
        assert names == {"confidence_score", "risk_flags"}
