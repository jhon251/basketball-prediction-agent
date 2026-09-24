"""Phase 8-C1 tests: per-league calibration closed loop."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.game import Game
from basketball_agent.evaluation.backtest import SimulationPredictor
from basketball_agent.evaluation.league_calibration import (
    calibrate_window, frozen_vs_fitted_table,
)
from basketball_agent.evaluation.param_tuning import (
    _boundary_hits,
    fit_v1_params,
    fit_v1_params_ex,
    load_fitted_params,
    save_fitted_params,
)
from basketball_agent.features import FeaturePipeline
from basketball_agent.models.win_probability import WinProbabilityConfig

UTC = timezone.utc


def _synthetic_nba_db(tmp_path):
    """30-game synthetic NBA season, Nov 2024, one game per day."""
    from basketball_agent.data.collectors.synthetic import (
        SyntheticLeagueConfig, generate_league,
    )
    from basketball_agent.data.db.loader import DataIngestor
    from basketball_agent.data.schemas.dataset import Dataset

    teams, players, games, team_stats, player_stats = generate_league(
        SyntheticLeagueConfig(n_teams=6, games_per_pair=2, seed=2025)
    )
    ds = Dataset(games=tuple(games), team_stats=tuple(team_stats),
                 player_stats=tuple(player_stats), teams=tuple(teams),
                 players=tuple(players))
    raw = {k: [m.model_dump(mode="json") for m in v] for k, v in
           (("teams", ds.teams), ("players", ds.players), ("games", ds.games),
            ("team_stats", ds.team_stats), ("player_stats", ds.player_stats))}
    db = Database(tmp_path / "cal.db")
    db.initialize()
    DataIngestor(db, now=datetime(2025, 3, 1, tzinfo=UTC)).ingest(raw)
    return db



def _without_stamp(params):
    from dataclasses import replace
    return replace(params, fitted_at="")


class TestBoundaryHits:
    def test_edges_are_recorded(self):
        hits = _boundary_hits(4.0, 0.0, 0.05)  # C2 grid: 4.0 is the lower edge
        assert any("LOWER" in h for h in hits)
        assert any("k=0" in h for h in hits)
        assert any("lambda=0.05" in h and "UPPER" in h for h in hits)

    def test_interior_values_have_no_hits(self):
        # 8.0 and 11.5 were the C1 lower edge / frozen default; both are
        # interior points of the C2 grid now
        assert _boundary_hits(8.0, 5.0, 0.02) == ()
        assert _boundary_hits(11.5, 20.0, 0.02) == ()

    def test_fit_records_its_own_hits(self, tmp_path):
        repo = Repository(_synthetic_nba_db(tmp_path))
        fitted = fit_v1_params(
            repo, datetime(2024, 11, 20, tzinfo=UTC),
            n_simulations=20, warmup_games=2,
        )
        assert fitted.boundary_hits == _boundary_hits(
            fitted.margin_logistic_scale, fitted.shrinkage_k, fitted.decay_lambda
        )


class TestNoLeakageInCalibration:
    def test_future_games_cannot_change_fitted_params(self, tmp_path):
        """Fitting is a pure function of the train window: games dated AFTER
        the cutoff must not change a single fitted value."""
        db = _synthetic_nba_db(tmp_path)
        repo = Repository(db)
        cutoff = datetime(2024, 11, 20, tzinfo=UTC)
        before = _without_stamp(fit_v1_params(
            repo, cutoff, n_simulations=20, warmup_games=2))

        # a league-final game in December: after the cutoff, must not matter
        repo.insert_game(Game(
            game_id="90001201_T01_T02", league=League.NBA, season="2024-25",
            game_date=datetime(2024, 12, 1, tzinfo=UTC),
            home_team_id="T01", away_team_id="T02", home_score=100, away_score=90,
            status=GameStatus.FINAL,
        ))
        after = _without_stamp(fit_v1_params(
            repo, cutoff, n_simulations=20, warmup_games=2))
        assert before == after

    def test_ex_returns_sweep_and_same_params(self, tmp_path):
        db = _synthetic_nba_db(tmp_path)
        repo = Repository(db)
        cutoff = datetime(2024, 11, 20, tzinfo=UTC)
        plain = _without_stamp(fit_v1_params(
            repo, cutoff, n_simulations=20, warmup_games=2))
        ex, sweep = fit_v1_params_ex(repo, cutoff, n_simulations=20,
                                     warmup_games=2, include_sweep=True)
        assert _without_stamp(ex) == plain
        assert set(sweep) == {0.0, 0.02, 0.05}
        assert all("log_loss" in s for s in sweep.values())


class TestLeagueScopedCalibration:
    def test_calibration_only_uses_its_own_league(self, tmp_path):
        """With an ACB game present, an NBA calibration must be unchanged."""
        db = _synthetic_nba_db(tmp_path)
        repo = Repository(db)
        cutoff = datetime(2024, 11, 20, tzinfo=UTC)
        before = _without_stamp(fit_v1_params(
            repo, cutoff, league=League.NBA, n_simulations=20, warmup_games=2))

        from basketball_agent.data.schemas.team import Team

        repo.upsert_team(Team(team_id="ACB-XXX", league=League.ACB,
                              name="Test ACB Home", abbreviation="ACB-XXX"))
        repo.upsert_team(Team(team_id="ACB-YYY", league=League.ACB,
                              name="Test ACB Away", abbreviation="ACB-YYY"))
        repo.insert_game(Game(
            game_id="ACB2025-999999", league=League.ACB, season="2025-26",
            game_date=datetime(2024, 11, 10, tzinfo=UTC),
            home_team_id="ACB-XXX", away_team_id="ACB-YYY",
            home_score=80, away_score=70, status=GameStatus.FINAL,
        ))
        after = _without_stamp(fit_v1_params(
            repo, cutoff, league=League.NBA, n_simulations=20, warmup_games=2))
        assert before == after

    def test_calibrate_window_runs_and_reports(self, tmp_path):
        db = _synthetic_nba_db(tmp_path)
        repo = Repository(db)
        result = calibrate_window(
            repo, League.NBA,
            cutoff=datetime(2024, 11, 20, tzinfo=UTC),
            test_start=datetime(2024, 11, 21, tzinfo=UTC),
            test_end=datetime(2024, 12, 1, tzinfo=UTC),
            n_simulations=20, warmup_games=2,
        )
        assert result["league"] == "NBA"
        assert result["n_test_games"] > 0
        for name in ("frozen_elo", "frozen_strength", "frozen_simulation",
                     "fitted_elo", "fitted_strength", "fitted_simulation"):
            assert "log_loss" in result["metrics"][name], name
        # frozen and fitted elo are identical: Elo has no calibrated component
        assert (result["metrics"]["frozen_elo"]["log_loss"]
                == result["metrics"]["fitted_elo"]["log_loss"])
        table = frozen_vs_fitted_table(result)
        assert "frozen_simulation" in table and "fitted_simulation" in table
        assert "boundary hits" in table

    def test_test_window_excludes_non_regular(self, tmp_path):
        db = _synthetic_nba_db(tmp_path)
        repo = Repository(db)
        repo.insert_game(Game(
            game_id="0042400999", league=League.NBA, season="2024-25",
            game_date=datetime(2024, 11, 25, tzinfo=UTC),
            home_team_id="T01", away_team_id="T02", home_score=90, away_score=80,
            status=GameStatus.FINAL, competition=Competition.PLAYOFF,
        ))
        result = calibrate_window(
            repo, League.NBA,
            cutoff=datetime(2024, 11, 20, tzinfo=UTC),
            test_start=datetime(2024, 11, 21, tzinfo=UTC),
            test_end=datetime(2024, 12, 1, tzinfo=UTC),
            n_simulations=20, warmup_games=2,
        )
        assert "0042400999" not in result["metrics"]["frozen_elo"]  # not in metrics
        # stronger check: the n_test_games count must ignore the playoff game
        ids = result["n_test_games"]
        games_in_window = [
            g for g in repo.list_games(status=GameStatus.FINAL, league=League.NBA)
            if datetime(2024, 11, 21, tzinfo=UTC) <= g.game_date
            < datetime(2024, 12, 1, tzinfo=UTC)
        ]
        assert ids == len(games_in_window) - 1  # minus the playoff game


class TestParamsApplication:
    def test_simulation_predictor_honors_win_config(self, tmp_path):
        db = _synthetic_nba_db(tmp_path)
        repo = Repository(db)
        games = sorted(
            (g for g in repo.list_games(status=GameStatus.FINAL)),
            key=lambda g: (g.game_date, g.game_id),
        )
        target = games[-1]
        features = FeaturePipeline(repo).compute(
            target.game_id, target.game_date.replace(hour=0),
        )
        frozen = SimulationPredictor(n_simulations=60, seed=7).predict(features)
        # extreme HCA pushes the home probability up monotonically
        extreme = SimulationPredictor(
            n_simulations=60, seed=7,
            win_config=WinProbabilityConfig(home_court_advantage=100.0),
        ).predict(features)
        assert extreme.prob_home > frozen.prob_home

    def test_params_file_roundtrip_and_backcompat(self, tmp_path):
        db = _synthetic_nba_db(tmp_path)
        repo = Repository(db)
        fitted = fit_v1_params(repo, datetime(2024, 11, 20, tzinfo=UTC),
                               n_simulations=20, warmup_games=2)
        path = tmp_path / "params.json"
        save_fitted_params(fitted, path)
        reloaded = load_fitted_params(path)
        assert reloaded == fitted

        # a params file written BEFORE boundary_hits existed must still load
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("boundary_hits")
        old_path = tmp_path / "legacy.json"
        old_path.write_text(json.dumps(legacy), encoding="utf-8")
        assert load_fitted_params(old_path).boundary_hits == ()

    def test_predict_agent_applies_fitted_params(self, tmp_path):
        from basketball_agent.agents.prediction_agent import PredictionAgent
        from basketball_agent.data.schemas.team import Team

        db = _synthetic_nba_db(tmp_path)
        repo = Repository(db)
        repo.insert_game(Game(
            game_id="90001205_T03_T04", league=League.NBA, season="2024-25",
            game_date=datetime(2024, 12, 5, tzinfo=UTC),
            home_team_id="T03", away_team_id="T04",
            home_score=None, away_score=None, status=GameStatus.SCHEDULED,
        ))
        as_of = datetime(2024, 12, 4, tzinfo=UTC)
        frozen = PredictionAgent(repo, n_simulations=60, seed=42).predict(
            "90001205_T03_T04", as_of=as_of)
        fitted_agent = PredictionAgent(
            repo, n_simulations=60, seed=42,
            win_config=WinProbabilityConfig(home_court_advantage=100.0),
        ).predict("90001205_T03_T04", as_of=as_of)
        assert (fitted_agent.win_probability_home
                > frozen.win_probability_home)


class TestC2Grids:
    """Phase 8-C2: ONLY the scale and shrinkage grids were widened."""

    def test_widened_grids(self):
        from basketball_agent.evaluation.param_tuning import (
            LAMBDA_CANDIDATES, LOGISTIC_CANDIDATES, SHRINKAGE_CANDIDATES,
        )

        assert LOGISTIC_CANDIDATES[0] == 4.0 and LOGISTIC_CANDIDATES[-1] == 18.0
        assert len(LOGISTIC_CANDIDATES) == 29  # 4.0..18.0 step 0.5
        assert SHRINKAGE_CANDIDATES == (0, 2, 5, 10, 20, 30, 40)
        assert LAMBDA_CANDIDATES == (0.0, 0.02, 0.05)  # untouched

    def test_boundary_messages_track_constants(self):
        hits = _boundary_hits(4.0, 40, 0.05)
        assert any("LOWER edge (4)" in h for h in hits)
        assert any("k=40 at grid UPPER edge (40)" in h for h in hits)
        assert any("lambda=0.05" in h and "UPPER edge (0.05)" in h for h in hits)

    def test_frozen_defaults_untouched(self):
        cfg = WinProbabilityConfig()
        assert cfg.home_court_advantage == 2.5
        assert cfg.margin_logistic_scale == 11.5
        assert cfg.shrinkage_k == 5.0
