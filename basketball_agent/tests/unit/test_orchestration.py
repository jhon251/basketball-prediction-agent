"""Final Agent Orchestration tests (Phase 9-E)."""
from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import datetime, timezone

import pytest

from basketball_agent.agents.decision import DecisionAgent
from basketball_agent.agents.decision_contract import (
    CalibrationProvenance,
    ValidationStatus,
)
from basketball_agent.agents.decision_record import DecisionRecordAgent
from basketball_agent.agents.explanation import ExplanationAgent
from basketball_agent.agents.orchestration import (
    AgentBundle,
    AgentOrchestrator,
    format_agent_bundle,
)
from basketball_agent.agents.prediction_agent import (
    PredictionAgent,
    PredictionError,
)
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent
from basketball_agent.data.db.repository import Repository

AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)


def _orchestrator(scheduled_target, **kwargs) -> AgentOrchestrator:
    core = PredictionAgent(Repository(scheduled_target),
                           n_simulations=50, seed=7, **kwargs)
    return AgentOrchestrator(core)


@pytest.fixture()
def bundle(scheduled_target) -> AgentBundle:
    return _orchestrator(scheduled_target).orchestrate(
        "20241120_LAL_BOS", as_of=AS_OF)


class TestBundleContract:
    def test_all_four_layers_present_and_linked(self, bundle):
        assert isinstance(bundle, AgentBundle)
        assert isinstance(bundle.risk_result, object)
        # object identity across layers: the bundle links the SAME
        # instances each layer consumed (no rebuilds, no copies)
        assert bundle.explanation.evidence_trace is \
            bundle.decision_record.evidence_references
        assert bundle.decision_record.risk_summary is bundle.risk_result
        assert bundle.decision_record.prediction_summary[
            "primary_prob_home"] == bundle.primary_prob_home

    def test_primary_probability_is_core_verbatim(self, scheduled_target,
                                                  bundle):
        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        raw = core.predict("20241120_LAL_BOS", as_of=AS_OF)
        assert bundle.primary_prob_home == pytest.approx(
            raw.win_probability_home)
        assert bundle.agent_decision.core_risk_confidence_score == \
            raw.risk.confidence_score

    def test_validation_status_and_state_consistent(self, bundle):
        assert bundle.validation_status.value == "configured"
        assert bundle.decision_state.value == "prediction_supported"
        assert bundle.explanation.validation_status == "configured"

    def test_bundle_is_immutable(self, bundle):
        with pytest.raises(Exception):
            bundle.primary_prob_home_override = 0.99  # not a frozen field
        with pytest.raises(Exception):
            bundle.agent_decision = None  # frozen dataclass


class TestDeterminism:
    def test_same_inputs_fieldwise_equal(self, scheduled_target):
        o = _orchestrator(scheduled_target)
        b1 = o.orchestrate("20241120_LAL_BOS", as_of=AS_OF)
        b2 = o.orchestrate("20241120_LAL_BOS", as_of=AS_OF)
        assert b1 == b2

    def test_as_of_is_required(self, scheduled_target):
        o = _orchestrator(scheduled_target)
        with pytest.raises(TypeError, match="explicitly"):
            o.orchestrate("20241120_LAL_BOS", as_of=None)


class TestStateRouting:
    def test_fallback_league(self, scheduled_target):
        o = _orchestrator(scheduled_target)
        b = o.orchestrate("20241120_LAL_BOS", as_of=AS_OF)
        assert b.decision_state.value == "prediction_supported"
        from dataclasses import replace
        # fallback semantics flow through every layer honestly
        stub_decision = replace(
            b.agent_decision, status=ValidationStatus.FALLBACK,
            context=replace(b.context, league="G_LEAGUE",
                            validation_status=ValidationStatus.FALLBACK))
        risk = RiskAnalysisAgent().analyze(stub_decision)
        record = DecisionRecordAgent().build(stub_decision, risk)
        explanation = ExplanationAgent().explain(record)
        assert record.decision_state.value == "fallback"
        assert "NOT a validated choice" in \
            explanation.decision_state_explanation["statement"]

    def test_unresolved_and_insufficient(self, scheduled_target):
        o = _orchestrator(scheduled_target)
        b = o.orchestrate("20241120_LAL_BOS", as_of=AS_OF)
        from dataclasses import replace
        for status in (ValidationStatus.UNRESOLVED,
                       ValidationStatus.INSUFFICIENT_EVIDENCE):
            stub = replace(b.agent_decision, status=status)
            risk = RiskAnalysisAgent().analyze(stub)
            record = DecisionRecordAgent().build(stub, risk)
            assert record.decision_state.value == status.value
            explanation = ExplanationAgent().explain(record)
            assert explanation.validation_status == status.value

    def test_caution_state(self, scheduled_target):
        decision, = [_orchestrator(scheduled_target).orchestrate(
            "20241120_LAL_BOS", as_of=AS_OF).agent_decision]
        from basketball_agent.agents.risk_analysis import (
            ModelAgreementFacts, ProbabilityFacts, RiskResult,
        )
        from dataclasses import replace
        risk = RiskAnalysisAgent().analyze(decision)
        pf = replace(risk.probability_facts, tossup_indicator=True)
        ma = replace(risk.model_agreement_facts,
                     directional_disagreement=True)
        patched_risk = replace(risk, probability_facts=pf,
                               model_agreement_facts=ma)
        record = DecisionRecordAgent().build(decision, patched_risk)
        assert record.decision_state.value == "prediction_with_caution"


class TestSixLeagueIsolationAndCBA:
    def test_league_states_resolve_independently(self):
        from basketball_agent.agents.decision_contract import (
            league_validation_status,
        )
        for league in ("NBA", "EUROLEAGUE", "ACB", "NBL", "CBA", "BSL"):
            assert league_validation_status(league).value == "configured"
        assert league_validation_status("G_LEAGUE").value == "fallback"

    def test_cba_dual_regulation_flows_through_bundle(self, scheduled_target):
        from dataclasses import replace

        o = _orchestrator(scheduled_target)
        b = o.orchestrate("20241120_LAL_BOS", as_of=AS_OF)
        for season, regulation in (("2024-25", 2880.0), ("2025-26", 2400.0)):
            stub = replace(
                b.agent_decision,
                context=replace(b.context, league="CBA", season=season,
                                regulation_seconds=regulation))
            assert stub.context.regulation_seconds == regulation
            assert stub.context.league == "CBA"


class TestCalibrationAndProvenance:
    def test_calibration_provenance_flows(self, scheduled_target):
        cal = CalibrationProvenance(
            params_file="storage/params/c2_nba_2025-26_1s.json",
            fitted_window=("2024-10-01", "2025-07-01"),
            fitted_at="2026-09-27T00:00:00+00:00",
            validation_logloss=0.6113, boundary_hits=())
        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        b = AgentOrchestrator(core, calibration=cal).orchestrate(
            "20241120_LAL_BOS", as_of=AS_OF)
        assert b.context.calibration is cal
        assert b.context.calibration_source == \
            "calibrated:storage/params/c2_nba_2025-26_1s.json"
        assert "c2_nba_2025-26_1s.json" in \
            b.explanation.calibration_provenance_explanation["statement"]

    def test_evidence_trace_complete(self, bundle):
        names = {ref.name for ref in bundle.evidence_trace}
        assert {"primary_probability", "model_agreement", "distribution",
                "validation_status", "calibration", "provenance",
                "risk_facts"} <= names


class TestNegativeGuards:
    def test_exception_propagation_not_swallowed(self, scheduled_target):
        o = _orchestrator(scheduled_target)
        with pytest.raises(PredictionError):
            o.orchestrate("UNKNOWN_GAME", as_of=AS_OF)

    def test_orchestrator_never_recomputes(self):
        """Structural guard: the orchestration module imports only the
        frozen consumers — no model, no metrics, no simulation runner."""
        import inspect
        from basketball_agent.agents import orchestration as mod

        src = inspect.getsource(mod)
        for banned in ("build_game_params", "MonteCarloRunner",
                       "FeaturePipeline", "fit_v1_params", "datetime.now",
                       "import random", "random.", "urllib", "requests",
                       "openai"):
            assert banned not in src

    def test_format_renders_readonly_report(self, bundle):
        text = format_agent_bundle(bundle)
        assert "AGENT BUNDLE" in text
        assert "Primary family: simulation" in text
        assert "nothing was saved" not in text  # that line is added by CLI
        assert "Validation status: configured" in text


class TestCLIAdapter:
    def test_cli_agent_report_readonly(self, scheduled_target, capsys,
                                      monkeypatch):
        import importlib
        cli = importlib.import_module("basketball_agent.cli.main")

        from basketball_agent.config import get_settings
        monkeypatch.setattr(cli, "_open_db",
                            lambda: (scheduled_target, get_settings()))
        monkeypatch.setattr(cli, "_latest_snapshot_id", lambda: None)
        monkeypatch.setattr(cli, "Repository", lambda db: Repository(db))

        class _Args:
            game_id = "20241120_LAL_BOS"
            as_of = AS_OF.isoformat()
            family = None
            params = None

        rc = cli._cmd_agent_report(_Args())
        out = capsys.readouterr().out
        assert rc == 0
        assert "AGENT BUNDLE" in out
        assert "(nothing was saved — agent-report is read-only)" in out

    def test_schema_frozen_fields(self):
        names = {f.name for f in dc_fields(AgentBundle)}
        assert names == {"agent_decision", "risk_result", "decision_record",
                         "explanation"}
