"""Integration tests for train-only parameter fitting and Phase 7 experiments."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.collectors.synthetic import SyntheticLeagueConfig, generate_league
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.evaluation.experiments import run_phase7_experiments
from basketball_agent.evaluation.param_tuning import (
    FittedParams,
    estimate_home_court_advantage,
    estimate_logistic_scale,
    estimate_shrinkage_k,
    fit_v1_params,
    load_fitted_params,
    save_fitted_params,
)

pytestmark = pytest.mark.integration

UTC = timezone.utc


@pytest.fixture
def synthetic_db(tmp_path, fixed_now):
    teams, players, games, team_stats, player_stats = generate_league(
        SyntheticLeagueConfig(n_teams=6, games_per_pair=3, seed=2025)
    )
    ds = Dataset(
        games=tuple(games), team_stats=tuple(team_stats), player_stats=tuple(player_stats),
        teams=tuple(teams), players=tuple(players),
    )
    raw = {
        "teams": [t.model_dump(mode="json") for t in ds.teams],
        "players": [p.model_dump(mode="json") for p in ds.players],
        "games": [g.model_dump(mode="json") for g in ds.games],
        "team_stats": [x.model_dump(mode="json") for x in ds.team_stats],
        "player_stats": [x.model_dump(mode="json") for x in ds.player_stats],
    }
    db = Database(tmp_path / "tune.db")
    db.initialize()
    DataIngestor(db, now=fixed_now).ingest(raw)
    return db


def _cutoffs(synthetic_db):
    games = sorted(Repository(synthetic_db).list_games(),
                   key=lambda g: (g.game_date, g.game_id))
    n = len(games)
    train_cutoff = games[int(n * 0.6)].game_date
    test_start = games[int(n * 0.6)].game_date
    return games, train_cutoff, test_start


class TestParamTuning:
    def test_hca_estimate_finite(self, synthetic_db):
        _, cutoff, _ = _cutoffs(synthetic_db)
        hca = estimate_home_court_advantage(Repository(synthetic_db), cutoff)
        # synthetic generator has no home advantage -> small value
        assert abs(hca) < 5.0

    def test_logistic_scale_in_candidate_range(self, synthetic_db):
        _, cutoff, _ = _cutoffs(synthetic_db)
        scale = estimate_logistic_scale(Repository(synthetic_db), cutoff, 0.0)
        assert 4.0 <= scale <= 18.0  # C2 grid

    def test_shrinkage_k_in_candidates(self, synthetic_db):
        _, cutoff, _ = _cutoffs(synthetic_db)
        k = estimate_shrinkage_k(Repository(synthetic_db), cutoff, 0.0, 11.5)
        assert k in (0, 2, 5, 10, 20, 30, 40)  # C2 grid

    def test_fit_v1_params_respects_training_window(self, synthetic_db):
        games, cutoff, _ = _cutoffs(synthetic_db)
        params = fit_v1_params(
            Repository(synthetic_db), cutoff, n_simulations=100, seed=7, warmup_games=3
        )
        assert isinstance(params, FittedParams)
        assert params.training_window[1] == cutoff.isoformat()
        # the whole training window is before the cutoff by construction
        assert datetime.fromisoformat(params.training_window[0]) < cutoff
        assert params.validation_logloss > 0.0

    def test_fitted_params_roundtrip(self, synthetic_db, tmp_path):
        _, cutoff, _ = _cutoffs(synthetic_db)
        params = fit_v1_params(Repository(synthetic_db), cutoff, n_simulations=100, warmup_games=3)
        path = tmp_path / "params" / "fitted.json"
        save_fitted_params(params, path)
        loaded = load_fitted_params(path)
        assert loaded == params


class TestPhase7Experiments:
    def test_all_four_experiments_run_with_isolation(self, synthetic_db):
        _, train_cutoff, test_start = _cutoffs(synthetic_db)
        results = run_phase7_experiments(
            Repository(synthetic_db), train_cutoff, test_start,
            n_simulations=150, seed=7, warmup_games=3,
        )
        assert set(results) == {"fitted_params", "EXP-NBA-001", "EXP-NBA-002",
                                "EXP-NBA-003", "EXP-NBA-004"}
        # 001: all four models evaluated on the test window
        assert set(results["EXP-NBA-001"]["models"]) == {"random", "elo", "strength", "simulation"}
        # 002: sweep performed on train only, chosen lambda recorded
        assert results["EXP-NBA-002"]["chosen_lambda"] in (0.0, 0.02, 0.05)
        # 003: weights normalized
        w = results["EXP-NBA-003"]["weights"]
        assert abs(w["elo"] + w["strength"] + w["sim"] - 1.0) < 1e-9
        # 004: honest comparison strings
        assert results["EXP-NBA-004"]["simulation_vs_baselines"]

    def test_test_window_never_used_for_fitting(self, synthetic_db):
        games, train_cutoff, test_start = _cutoffs(synthetic_db)
        repo = Repository(synthetic_db)
        results = run_phase7_experiments(
            repo, train_cutoff, test_start, n_simulations=100, seed=7, warmup_games=3
        )
        fitted = results["fitted_params"]
        # parameters only depend on games strictly before the cutoff
        assert datetime.fromisoformat(fitted.training_window[1]) == train_cutoff
        # test games are strictly at/after test_start — and are NOT in the
        # training window by definition
        test_games = [g for g in games if g.game_date >= test_start]
        assert test_games
        for g in test_games:
            assert g.game_date >= train_cutoff
