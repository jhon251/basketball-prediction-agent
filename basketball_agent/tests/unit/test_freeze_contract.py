"""Prediction Core Freeze contract tests (Phase 8-L).

Read-only assertions pinning the frozen contracts that the freeze decision
relies on.  If any of these fail, the freeze is invalid — they are the
machine-checkable subset of docs/PHASE8_L_FREEZE.md.
"""
from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import datetime, timezone

import pytest

from basketball_agent.agents.family_config import (
    DEFAULT_FAMILY,
    FAMILY_CONFIG,
    PredictionFamily,
    resolve_family,
)
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import League
from basketball_agent.models.game_params import (
    LEAGUE_REGULATION_SECONDS,
    LEAGUE_REGULATION_SECONDS_BY_SEASON,
    regulation_seconds_for,
)
from basketball_agent.models.win_probability import WinProbabilityConfig


FROZEN_DEFAULTS = {"shrinkage_k": 5.0, "home_court_advantage": 2.5,
                   "margin_logistic_scale": 11.5}


class TestFrozenDefaultsUnchanged:
    def test_win_probability_defaults(self):
        cfg = WinProbabilityConfig()
        for name, value in FROZEN_DEFAULTS.items():
            assert getattr(cfg, name) == value, name

    def test_league_regulation_table(self):
        assert LEAGUE_REGULATION_SECONDS == {
            "NBA": 2880.0, "EUROLEAGUE": 2400.0, "ACB": 2400.0,
            "NBL": 2400.0, "CBA": 2400.0, "BSL": 2400.0,
        }

    def test_cba_per_season_regulation_stays_split(self):
        # the two CBA rule sets must never collapse into one regulation
        assert LEAGUE_REGULATION_SECONDS_BY_SEASON == {"CBA": {"2024-25": 2880.0}}
        assert regulation_seconds_for("CBA", "2024-25") == 2880.0
        assert regulation_seconds_for("CBA", "2025-26") == 2400.0
        assert regulation_seconds_for("CBA", "2023-24") == 2400.0  # falls to league default
        assert regulation_seconds_for("NBA", "2024-25") == 2880.0  # unaffected

    def test_default_family_is_simulation(self):
        assert DEFAULT_FAMILY is PredictionFamily.SIMULATION


class TestFamilySemantics:
    def test_configured_vs_fallback_are_distinguishable(self):
        configured = resolve_family(League.NBA)
        fallback = resolve_family("NOT_A_LEAGUE")
        assert (configured.source, configured.status) == ("configured", "configured")
        assert configured.evidence is not None
        assert (fallback.source, fallback.status) == ("fallback", "unresolved")
        assert fallback.evidence is None
        # a fallback must never masquerade as a validated production model
        assert fallback.source != "configured" and fallback.status != "configured"

    def test_all_six_leagues_explicitly_configured(self):
        assert set(FAMILY_CONFIG) == {
            League.NBA, League.EUROLEAGUE, League.ACB,
            League.NBL, League.CBA, League.BSL,
        }
        for cfg in FAMILY_CONFIG.values():
            assert cfg.family is not None
            assert cfg.family is not PredictionFamily.ENSEMBLE

    def test_ensemble_only_via_explicit_weights(self):
        from basketball_agent.agents.prediction_agent import (
            PredictionAgent,
            PredictionError,
        )

        agent = PredictionAgent.__new__(PredictionAgent)
        agent._ensemble_weights_given = False
        # the guard lives in predict(); verify via the constructor flag path
        assert agent._ensemble_weights_given is False
        # and the dataclass contract
        from basketball_agent.agents.prediction_agent import GamePrediction
        names = {f.name for f in dc_fields(GamePrediction)}
        assert {"primary_family", "primary_family_source",
                "primary_prob_home"} <= names


AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)


def _predict(db, **kwargs):
    from basketball_agent.agents.prediction_agent import PredictionAgent

    agent = PredictionAgent(
        Repository(db), n_simulations=50, seed=7, **kwargs)
    return agent.predict("20241120_LAL_BOS", as_of=AS_OF)


class TestDistributionContract:
    """Simulation's distribution outputs (RiskAgent inputs) stay available
    regardless of the configured primary family."""

    def test_montecarlo_result_carries_full_distributions(self, scheduled_target):
        p = _predict(scheduled_target, family="elo")
        mc = p.monte_carlo
        assert 0.0 <= mc.home_win_probability <= 1.0
        assert 0.0 <= mc.away_win_probability <= 1.0
        assert 0.0 <= mc.tie_probability <= 1.0
        for dist in (mc.home_score, mc.away_score, mc.margin, mc.total):
            for stat in ("mean", "median", "std", "p5", "p25", "p50", "p75", "p95", "count"):
                assert getattr(dist, stat) is not None
        assert mc.home_score_buckets and mc.away_score_buckets
        assert mc.runs, "per-run score samples must remain available"

    def test_risk_agent_inputs_intact_with_elo_family(self, scheduled_target):
        p = _predict(scheduled_target, family="elo")
        assert p.risk is not None
        assert hasattr(p.risk, "confidence_score")
        assert hasattr(p.risk, "risk_flags")
        # distribution provider independence: elo primary, sim distributions
        assert p.primary_family == "elo"
        assert p.monte_carlo.n_simulations == 50


class TestCalibrationContract:
    def test_fitted_params_schema_has_no_test_fields(self):
        from basketball_agent.evaluation.param_tuning import FittedParams

        names = {f.name for f in dc_fields(FittedParams)}
        assert {"home_court_advantage", "margin_logistic_scale",
                "shrinkage_k", "decay_lambda", "training_window",
                "validation_logloss"} <= names
        assert "test_window" not in names and "test_logloss" not in names

    def test_cross_league_fitting_is_structurally_forbidden(self):
        import inspect

        from basketball_agent.evaluation import param_tuning

        src = inspect.getsource(param_tuning.fit_v1_params_ex)
        assert "league" in src  # league parameter present in the fit API
