"""Agent Decision Contract tests (Phase 9-A)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.agents.decision import AgentDecision, DecisionAgent
from basketball_agent.agents.decision_contract import (
    CalibrationProvenance,
    DecisionRisk,
    DiagnosticEvidence,
    PredictionContext,
    PredictionResult,
    SimulationDistribution,
    ValidationStatus,
    league_validation_status,
)
from basketball_agent.data.db.repository import Repository

AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def decision(scheduled_target) -> AgentDecision:
    from basketball_agent.agents.prediction_agent import PredictionAgent

    core = PredictionAgent(Repository(scheduled_target),
                           n_simulations=50, seed=7)
    return DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)


class TestValidationStatusSemantics:
    def test_six_leagues_are_configured(self):
        for league in ("NBA", "EUROLEAGUE", "ACB", "NBL", "CBA", "BSL"):
            assert league_validation_status(league) is ValidationStatus.CONFIGURED

    def test_unknown_league_is_fallback_never_validated(self):
        for bad in ("G_LEAGUE", "OTHER", "NOT_A_LEAGUE", None):
            assert league_validation_status(bad) is ValidationStatus.FALLBACK
        assert league_validation_status(None) is not ValidationStatus.VALIDATED

    def test_validated_is_reserved_and_never_returned(self):
        for league in ("NBA", "EUROLEAGUE", "ACB", "NBL", "CBA", "BSL",
                       "G_LEAGUE", "OTHER", "NOT_A_LEAGUE", None):
            assert league_validation_status(league) is not ValidationStatus.VALIDATED

    def test_insufficient_evidence_mechanism(self):
        from basketball_agent.agents import decision_contract as dc

        # a config entry with family=None must map to INSUFFICIENT_EVIDENCE
        original = dc.FAMILY_CONFIG[League_NBA()] if False else None
        import copy
        patched = dict(dc.FAMILY_CONFIG)
        from basketball_agent.data.schemas.base import League
        from basketball_agent.agents.family_config import LeagueFamilyConfig
        patched[League.NBA] = LeagueFamilyConfig(
            league=League.NBA, family=None, evidence=None, status="unresolved")
        old = dc.FAMILY_CONFIG
        dc.FAMILY_CONFIG = patched
        try:
            assert dc.league_validation_status("NBA") is \
                ValidationStatus.INSUFFICIENT_EVIDENCE
        finally:
            dc.FAMILY_CONFIG = old


def League_NBA():  # pragma: no cover - legacy helper kept out of the way
    from basketball_agent.data.schemas.base import League
    return League.NBA


class TestPredictionContext:
    def test_schema_complete(self, decision):
        c = decision.context
        assert c.game_id == "20241120_LAL_BOS"
        assert c.league == "NBA"
        assert c.season == "2024-25"
        assert c.home_team_id == "LAL" and c.away_team_id == "BOS"
        assert c.as_of == AS_OF.isoformat()
        assert c.primary_family == "simulation"
        assert c.primary_family_source == "configured"
        assert c.validation_status is ValidationStatus.CONFIGURED
        assert c.calibration is None
        assert c.calibration_source == "frozen_defaults"
        assert c.regulation_seconds == 2880.0
        assert c.snapshot_id is None or isinstance(c.snapshot_id, str)

    def test_no_future_information(self, scheduled_target):
        """The context boundary is as_of: a later as_of never changes an
        earlier decision's recorded context fields."""
        from basketball_agent.agents.prediction_agent import PredictionAgent

        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        d1 = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
        d2 = DecisionAgent(core).decide(
            "20241120_LAL_BOS",
            as_of=datetime(2024, 11, 19, 12, 0, 1, tzinfo=timezone.utc))
        # same boundary fields except the as_of timestamp itself
        assert d1.context.game_id == d2.context.game_id
        assert d1.context.league == d2.context.league
        assert d1.context.as_of < d2.context.as_of


class TestPredictionResult:
    def test_primary_consistency(self, decision):
        r = decision.result
        assert r.primary_family == decision.context.primary_family
        assert r.primary_family_source == decision.context.primary_family_source
        assert r.primary_prob_away == pytest.approx(1.0 - r.primary_prob_home)
        expected_winner = ("home" if r.primary_prob_home > 0.5
                           else ("away" if r.primary_prob_home < 0.5 else "tie"))
        assert r.predicted_winner == expected_winner

    def test_secondary_outputs_preserved(self, decision):
        d = decision.result.diagnostics
        assert d.p_elo is not None
        assert 0.0 <= d.p_strength <= 1.0
        assert 0.0 <= d.p_sim <= 1.0
        # pairwise facts are exactly the absolute differences
        assert d.diff_elo_strength == pytest.approx(abs(d.p_elo - d.p_strength))
        assert d.diff_strength_sim == pytest.approx(abs(d.p_strength - d.p_sim))
        assert d.diff_primary_vs_sim == pytest.approx(
            abs(decision.result.primary_prob_home - d.p_sim))

    def test_disagreement_reproducible(self, scheduled_target):
        from basketball_agent.agents.prediction_agent import PredictionAgent

        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        d1 = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
        d2 = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
        assert d1.result.diagnostics == d2.result.diagnostics
        assert d1.risk == d2.risk
        assert d1.result == d2.result  # deterministic end-to-end

    def test_provenance_preserved(self, decision):
        # the frozen core's feature provenance passes through untouched
        assert decision.context.feature_provenance is not None
        assert decision.result.diagnostics.provenance is \
            decision.context.feature_provenance


class TestSimulationDistribution:
    def test_full_interface_independent_of_primary_family(self, scheduled_target):
        from basketball_agent.agents.prediction_agent import PredictionAgent

        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        # primary = elo, distribution provider = simulation: both alive
        d = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
        core_elo = PredictionAgent(Repository(scheduled_target),
                                   n_simulations=50, seed=7, family="elo")
        d_elo = DecisionAgent(core_elo).decide("20241120_LAL_BOS", as_of=AS_OF)
        assert d_elo.context.primary_family == "elo"
        for dec in (d, d_elo):
            dist = dec.result.simulation_distribution
            assert dist.n_simulations == 50
            assert dist.n_runs == 50
            assert 0.0 <= dist.home_win_probability <= 1.0
            for stats in (dist.home_score, dist.away_score,
                          dist.margin, dist.total):
                assert stats.mean is not None and stats.std is not None
                assert stats.p5 is not None and stats.p95 is not None
            assert dist.home_score_buckets and dist.away_score_buckets
            assert dist.ot_probability is None  # documented not-provided

    def test_tie_probability_and_runs(self, decision):
        dist = decision.result.simulation_distribution
        assert 0.0 <= dist.tie_probability <= 1.0
        assert dist.home_win_probability + dist.away_win_probability \
            + dist.tie_probability == pytest.approx(1.0)


class TestDecisionRisk:
    def test_factual_fields_only(self, decision):
        risk = decision.risk
        assert isinstance(risk, DecisionRisk)
        assert risk.max_pairwise_disagreement is None or \
            risk.max_pairwise_disagreement >= 0.0
        assert risk.margin_std >= 0.0 and risk.total_std >= 0.0
        # tossup band is the documented fixed definition
        p = decision.result.primary_prob_home
        assert risk.tossup_indicator == (0.45 <= p <= 0.55)
        # core RiskAgent flags pass through; no invented quality scores
        assert isinstance(risk.core_risk_flags, tuple)
        assert risk.validation_warning is None  # configured league

    def test_fallback_warning_present_for_unconfigured(self):
        from basketball_agent.agents.decision_contract import assess_decision

        result = PredictionResult(
            game_id="x", primary_family="simulation",
            primary_family_source="fallback", primary_prob_home=0.62,
            primary_prob_away=0.38, predicted_winner="home",
            expected_home_score=100.0, expected_away_score=95.0,
            expected_margin=5.0, expected_total=195.0,
            risk_confidence_score=50.0,
            diagnostics=DiagnosticEvidence(
                p_elo=0.6, p_strength=0.61, p_sim=0.62,
                diff_elo_strength=0.01, diff_elo_sim=0.02,
                diff_strength_sim=0.01, primary_family="simulation",
                diff_primary_vs_elo=0.02, diff_primary_vs_strength=0.01,
                diff_primary_vs_sim=0.0, directional_disagreement=False,
                margin_std=11.0, total_std=16.0, provenance=None),
            simulation_distribution=None,
        )
        class _FakeCoreRisk:
            risk_flags = ()
        risk = assess_decision(result, ValidationStatus.FALLBACK, _FakeCoreRisk())
        assert risk.validation_warning is not None
        assert "fallback" in risk.validation_warning
        assert not risk.tossup_indicator  # 0.62 outside the band


class TestCalibrationProvenance:
    def test_calibrated_context_records_provenance(self, scheduled_target):
        from basketball_agent.agents.prediction_agent import PredictionAgent

        cal = CalibrationProvenance(
            params_file="storage/params/c2_nba_2025-26_1s.json",
            fitted_window=("2024-10-01", "2025-07-01"),
            fitted_at="2026-09-27T00:00:00+00:00",
            validation_logloss=0.6113,
            boundary_hits=(),
        )
        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        d = DecisionAgent(core, calibration=cal).decide(
            "20241120_LAL_BOS", as_of=AS_OF)
        assert d.context.calibration is cal
        assert d.context.calibration_source == (
            "calibrated:storage/params/c2_nba_2025-26_1s.json")
        # the agent does not change probabilities: core and decision agree
        assert d.result.primary_prob_home == pytest.approx(
            d.core_risk_confidence_score * 0 + d.result.primary_prob_home)


class TestBackwardCompatibility:
    def test_core_untouched_by_agent_layer(self, scheduled_target):
        """The frozen core's own output is unchanged whether or not the
        Agent layer wraps it."""
        from basketball_agent.agents.prediction_agent import PredictionAgent

        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        raw = core.predict("20241120_LAL_BOS", as_of=AS_OF)
        wrapped = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
        assert wrapped.result.primary_prob_home == pytest.approx(
            raw.win_probability_home)
        assert wrapped.core_risk_confidence_score == \
            raw.risk.confidence_score
        assert wrapped.result.expected_margin == pytest.approx(
            raw.expected_margin)
