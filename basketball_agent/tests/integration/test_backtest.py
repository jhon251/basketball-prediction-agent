"""Integration tests for the walk-forward backtest (spec §23-§26)."""
from __future__ import annotations

import pytest

from basketball_agent.data.collectors.synthetic import SyntheticLeagueConfig, generate_league
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    EloPredictor,
    HistoricalPredictor,
    RandomPredictor,
    SimulationPredictor,
    StrengthPredictor,
)
from basketball_agent.evaluation.leakage import check_temporal_integrity

pytestmark = pytest.mark.integration


class TestFixtureBacktest:
    def test_walk_forward_over_fixture(self, ingested_db, fixed_now):
        engine = BacktestEngine(
            Repository(ingested_db),
            SimulationPredictor(n_simulations=300, seed=7),
            BacktestConfig(warmup_games=0),
        )
        result = engine.run()
        # g1: no history for anyone; g2: GSW has no history; g3: predictable
        assert result.n_games == 3
        assert result.n_predicted == 1
        assert result.n_skipped == 2
        predicted = [r for r in result.records if r.prediction is not None][0]
        assert predicted.game_id == "20241115_GSW_BOS"
        assert 0.0 <= predicted.prediction.prob_home <= 1.0
        # actuals from the fixture: GSW 95 - 110 BOS (home lost)
        assert predicted.actual_home_win is False
        assert predicted.actual_margin == -15
        assert predicted.actual_total == 205
        # metrics exist over the single prediction
        summary = result.summary()
        assert summary["n_predicted"] == 1
        assert summary["accuracy"] in (0.0, 1.0)
        assert summary["log_loss"] is not None
        assert summary["brier_score"] is not None
        assert summary["ece"] is not None
        # reliability table over 1 sample: 10 bins, one occupied
        table = result.reliability_table()
        assert len(table) == 10
        assert sum(b.count for b in table) == 1

    def test_no_leakage_in_backtest_provenance(self, ingested_db):
        engine = BacktestEngine(
            Repository(ingested_db),
            SimulationPredictor(n_simulations=100, seed=7),
            BacktestConfig(warmup_games=0),
        )
        result = engine.run()
        repo = Repository(ingested_db)
        for r in result.records:
            if r.prediction is None or r.prediction.provenance is None:
                continue
            for gid in r.prediction.provenance.games_used:
                assert repo.get_game(gid).game_date < r.as_of


class TestSyntheticBacktest:
    @pytest.fixture
    def synthetic_db(self, tmp_path, fixed_now):
        teams, players, games, team_stats, player_stats = generate_league(
            SyntheticLeagueConfig(n_teams=6, games_per_pair=2, seed=2025)
        )
        dataset = Dataset(
            games=tuple(games), team_stats=tuple(team_stats),
            player_stats=tuple(player_stats), teams=tuple(teams), players=tuple(players),
        )
        raw = {
            "teams": [t.model_dump(mode="json") for t in dataset.teams],
            "players": [p.model_dump(mode="json") for p in dataset.players],
            "games": [g.model_dump(mode="json") for g in dataset.games],
            "team_stats": [ts.model_dump(mode="json") for ts in dataset.team_stats],
            "player_stats": [ps.model_dump(mode="json") for ps in dataset.player_stats],
        }
        db = Database(tmp_path / "synthetic.db")
        db.initialize()
        DataIngestor(db, now=fixed_now).ingest(raw)
        return db

    def test_synthetic_walk_forward_backtest(self, synthetic_db):
        engine = BacktestEngine(
            Repository(synthetic_db),
            SimulationPredictor(n_simulations=300, seed=7),
            BacktestConfig(warmup_games=3, decay_lambda=0.0),
        )
        result = engine.run()
        summary = result.summary()
        assert summary["n_games"] == 30
        assert summary["n_predicted"] > 0
        assert summary["n_skipped"] + summary["n_predicted"] == 30
        # honest metric bounds on synthetic data
        assert 0.0 <= summary["accuracy"] <= 1.0
        assert 0.0 <= summary["log_loss"] <= 2.0
        assert 0.0 <= summary["brier_score"] <= 1.0
        assert 0.0 <= summary["ece"] <= 1.0
        assert summary["margin_mae"] > 0
        assert summary["total_mae"] > 0
        table = result.reliability_table()
        assert len(table) == 10
        assert sum(b.count for b in table) == summary["n_predicted"]

    def test_baselines_all_produce_metrics(self, synthetic_db):
        repo = Repository(synthetic_db)
        config = BacktestConfig(warmup_games=3)
        outcomes = {}
        for name, predictor in (
            ("simulation", SimulationPredictor(n_simulations=300, seed=7)),
            ("elo", EloPredictor()),
            ("random", RandomPredictor()),
            ("historical", HistoricalPredictor()),
            ("strength", StrengthPredictor()),
        ):
            result = BacktestEngine(repo, predictor, config).run()
            outcomes[name] = result.win_prob_metrics()
            assert outcomes[name]["n_win_prob"] > 0
            assert 0.0 <= outcomes[name]["accuracy"] <= 1.0
        # theory: constant-0.5 Brier score is exactly 0.25 for any outcomes
        assert outcomes["random"]["brier_score"] == pytest.approx(0.25)
        # all baselines saw the same number of predictable games
        assert len({outcomes[n]["n_win_prob"] for n in outcomes}) == 1

    def test_every_prediction_temporally_integral(self, synthetic_db):
        engine = BacktestEngine(
            Repository(synthetic_db),
            SimulationPredictor(n_simulations=100, seed=7),
            BacktestConfig(warmup_games=2),
        )
        result = engine.run()
        repo = Repository(synthetic_db)
        for r in result.records:
            if r.prediction is None:
                continue
            # provenance games strictly before as_of
            for gid in r.prediction.provenance.games_used:
                assert repo.get_game(gid).game_date < r.as_of

    def test_warmup_skips_early_games(self, synthetic_db):
        engine = BacktestEngine(
            Repository(synthetic_db),
            RandomPredictor(),
            BacktestConfig(warmup_games=4),
        )
        result = engine.run()
        skipped = [r for r in result.records if r.prediction is None]
        assert skipped, "some early games must be skipped for warmup"
        assert all(r.skipped_reason and "warmup" in r.skipped_reason for r in skipped)

    def test_decay_lambda_sweep_runs_identical_protocol(self, synthetic_db):
        from basketball_agent.evaluation.backtest import sweep_decay_lambda

        outcomes = sweep_decay_lambda(
            Repository(synthetic_db),
            [0.0, 0.02],
            n_simulations=200,
            warmup_games=3,
            seed=7,
        )
        assert set(outcomes) == {0.0, 0.02}
        for summary in outcomes.values():
            assert summary["n_predicted"] > 0
            assert 0.0 <= summary["log_loss"] <= 2.0
        # identical protocol -> identical n_predicted across lambdas
        assert outcomes[0.0]["n_predicted"] == outcomes[0.02]["n_predicted"]
