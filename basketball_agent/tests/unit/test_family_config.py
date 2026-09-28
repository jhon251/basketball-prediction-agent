"""Per-league prediction-family configuration tests (Phase 8-K)."""
from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import datetime, timezone

import pytest

from basketball_agent.agents.family_config import (
    DEFAULT_FAMILY,
    FAMILY_CONFIG,
    FamilyEvidence,
    PredictionFamily,
    LeagueFamilyConfig,
    resolve_family,
)
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import League


class TestResolveFamily:
    def test_six_ingested_leagues_are_configured_simulation(self):
        for league in (League.NBA, League.EUROLEAGUE, League.ACB,
                       League.NBL, League.CBA, League.BSL):
            r = resolve_family(league)
            assert r.family is PredictionFamily.SIMULATION
            assert r.source == "configured"
            assert r.status == "configured"
            assert r.evidence is not None

    def test_unknown_league_falls_back_safely(self):
        for bad in (League.G_LEAGUE, League.OTHER, "UNKNOWN_X", None):
            r = resolve_family(bad)
            assert r.family is DEFAULT_FAMILY is PredictionFamily.SIMULATION
            assert r.source == "fallback"
            assert r.status == "unresolved"
            assert r.evidence is None

    def test_string_and_enum_inputs_agree(self):
        assert resolve_family("NBA").family == resolve_family(League.NBA).family
        assert resolve_family("cba").source == "fallback"  # case-sensitive, safe


class TestEvidenceAndNoFutureSelection:
    def test_evidence_schema_has_no_test_window_field(self):
        """Structure enforces the no-future-test-selection rule: a test
        window simply has nowhere to be recorded."""
        names = {f.name for f in dc_fields(FamilyEvidence)}
        assert "test_window" not in names
        assert "test_result" not in names
        assert {"rationale", "train_window", "validation_window",
                "references", "decided_in"} <= names

    def test_every_configured_entry_has_complete_provenance(self):
        for league, cfg in FAMILY_CONFIG.items():
            assert cfg.evidence is not None, league
            assert cfg.evidence.rationale.strip()
            assert cfg.evidence.train_window.strip()
            assert cfg.evidence.validation_window.strip()
            assert cfg.evidence.references
            assert any("docs/" in r for r in cfg.evidence.references)
            assert "8-K" in cfg.evidence.decided_in

    def test_resolution_is_static_and_pure(self):
        """resolve_family consults no runtime data: repeated calls and
        mutated league state cannot change the outcome."""
        r1 = resolve_family(League.NBA)
        r2 = resolve_family(League.NBA)
        assert r1 == r2

    def test_ensemble_is_never_a_configured_family(self):
        for cfg in FAMILY_CONFIG.values():
            assert cfg.family is not PredictionFamily.ENSEMBLE


class TestLeagueIsolation:
    def test_config_entries_are_independent(self):
        cfg_nba = FAMILY_CONFIG[League.NBA]
        cfg_el = FAMILY_CONFIG[League.EUROLEAGUE]
        assert cfg_nba is not cfg_el
        assert cfg_nba.evidence is not cfg_el.evidence
        # mutating one league's mapping slot cannot affect another
        patched = dict(FAMILY_CONFIG)
        patched[League.NBA] = LeagueFamilyConfig(
            league=League.NBA, family=PredictionFamily.ELO,
            evidence=cfg_nba.evidence)
        assert patched[League.EUROLEAGUE].family is PredictionFamily.SIMULATION
        assert FAMILY_CONFIG[League.NBA].family is PredictionFamily.SIMULATION

    def test_per_league_params_are_not_shared(self):
        """No cross-league inheritance: each entry carries its own evidence
        object referencing that league's own archived artifacts."""
        refs = {lg: tuple(cfg.evidence.references)
                for lg, cfg in FAMILY_CONFIG.items()}
        assert len(set(refs.values())) == len(refs)


class TestFamilyRouting:
    AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)

    def _predict(self, scheduled_target, **kwargs):
        from basketball_agent.agents.prediction_agent import PredictionAgent

        agent = PredictionAgent(
            Repository(scheduled_target), n_simulations=50, seed=7, **kwargs)
        return agent.predict("20241120_LAL_BOS", as_of=self.AS_OF)

    def test_routing_changes_primary_prob(self, scheduled_target):
        elo = self._predict(scheduled_target, family="elo")
        strength = self._predict(scheduled_target, family="strength")
        sim = self._predict(scheduled_target, family="simulation")
        assert elo.win_probability_home == pytest.approx(
            elo.features.elo_win_prob_home)
        assert strength.win_probability_home == pytest.approx(
            strength.params.strength_win_prob_home)
        assert sim.win_probability_home == pytest.approx(
            sim.monte_carlo.home_win_probability)
        # the three families genuinely differ on this fixture
        assert len({round(elo.win_probability_home, 6),
                    round(strength.win_probability_home, 6),
                    round(sim.win_probability_home, 6)}) == 3

    def test_primary_metadata_recorded(self, scheduled_target):
        for fam in ("elo", "strength", "simulation"):
            p = self._predict(scheduled_target, family=fam)
            assert p.primary_family == fam
            assert p.primary_family_source == "override"
        # ensemble stays attached as the diagnostic layer in every case
        p = self._predict(scheduled_target, family="elo")
        assert p.ensemble.prob_home == pytest.approx(
            p.monte_carlo.home_win_probability)

    def test_backward_compat_default_is_simulation(self, scheduled_target):
        """Pre-8-K behavior: default primary == ensemble.prob_home ==
        simulation win rate (weights 0,0,1) — bit-identical path."""
        d = self._predict(scheduled_target)
        assert d.primary_family == "simulation"
        assert d.primary_family_source == "configured"
        assert d.win_probability_home == pytest.approx(
            d.ensemble.prob_home)
        assert d.win_probability_home == pytest.approx(
            d.monte_carlo.home_win_probability)

    def test_explicit_weights_keep_pre_8k_path(self, scheduled_target):
        """A caller passing ensemble_weights (pre-8-K API) gets the exact
        old behavior: primary = weighted ensemble, family=simulation."""
        from basketball_agent.agents.ensemble_agent import EnsembleWeights

        w = EnsembleWeights(w_elo=0.2, w_strength=0.3, w_sim=0.5)
        p = self._predict(scheduled_target, ensemble_weights=w)
        assert p.primary_family == "simulation"
        assert p.win_probability_home == pytest.approx(p.ensemble.prob_home)
        expected = (w.w_elo * p.ensemble.p_elo
                    + w.w_strength * p.ensemble.p_strength
                    + w.w_sim * p.ensemble.p_sim)
        assert p.win_probability_home == pytest.approx(expected)

    def test_ensemble_family_requires_explicit_weights(self, scheduled_target):
        from basketball_agent.agents.prediction_agent import (
            PredictionAgent,
            PredictionError,
        )

        agent = PredictionAgent(
            Repository(scheduled_target), n_simulations=50, seed=7,
            family="ensemble")
        with pytest.raises(PredictionError, match="explicit EnsembleWeights"):
            agent.predict("20241120_LAL_BOS", as_of=self.AS_OF)
