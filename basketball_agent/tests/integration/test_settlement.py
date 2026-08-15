"""Integration tests for prediction settlement and the ensemble fit."""
from __future__ import annotations

import pytest

from basketball_agent.agents.ensemble_agent import EnsembleAgent
from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.data.collectors.synthetic import SyntheticLeagueConfig, generate_league
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.evaluation.settlement import SettlementStore

pytestmark = pytest.mark.integration


@pytest.fixture
def synthetic_db(tmp_path, fixed_now):
    teams, players, games, team_stats, player_stats = generate_league(
        SyntheticLeagueConfig(n_teams=6, games_per_pair=2, seed=2025)
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
    db = Database(tmp_path / "settle.db")
    db.initialize()
    DataIngestor(db, now=fixed_now).ingest(raw)
    return db


class TestSettlementStore:
    def test_predict_save_settle_loop(self, synthetic_db):
        repo = Repository(synthetic_db)
        games = sorted(repo.list_games(), key=lambda g: (g.game_date, g.game_id))
        target = games[10]  # mid-season game: both teams have history
        agent = PredictionAgent(repo, n_simulations=200, seed=7)
        prediction = agent.predict(target.game_id, as_of=target.game_date)

        store = SettlementStore(synthetic_db)
        prediction_id = store.save(prediction)
        assert prediction_id > 0

        settled = store.settle(target.game_id)
        home_win = target.home_score > target.away_score
        p = prediction.win_probability_home
        assert settled.brier == pytest.approx((p - (1.0 if home_win else 0.0)) ** 2)
        assert settled.correct == ((p >= 0.5) == home_win)
        assert settled.actual_home_score == target.home_score
        assert settled.log_loss > 0.0

    def test_aggregate_metrics_over_settled(self, synthetic_db):
        repo = Repository(synthetic_db)
        games = sorted(repo.list_games(), key=lambda g: (g.game_date, g.game_id))
        agent = PredictionAgent(repo, n_simulations=100, seed=7)
        store = SettlementStore(synthetic_db)
        # late-season games: every team has real history by then
        for target in games[22:28]:
            prediction = agent.predict(target.game_id, as_of=target.game_date)
            store.save(prediction)
            store.settle(target.game_id)
        metrics = store.aggregate_metrics()
        assert metrics["n_settled"] == 6
        assert 0.0 <= metrics["brier_score"] <= 1.0
        assert 0.0 <= metrics["accuracy"] <= 1.0
        assert 0.0 <= metrics["ece"] <= 1.0

    def test_settle_requires_final_game(self, synthetic_db, fixed_now):
        repo = Repository(synthetic_db)
        from basketball_agent.data.schemas.base import GameStatus, League
        from basketball_agent.data.schemas.game import Game
        from datetime import datetime, timezone

        repo.insert_game(Game(
            game_id="FUTURE_GAME", league=League.NBA, season="2024-25",
            game_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            home_team_id="T01", away_team_id="T02",
            home_score=None, away_score=None, status=GameStatus.SCHEDULED,
        ))
        with pytest.raises(ValueError):
            SettlementStore(synthetic_db).settle("FUTURE_GAME")

    def test_settle_unknown_game_raises(self, synthetic_db):
        with pytest.raises(ValueError):
            SettlementStore(synthetic_db).settle("NOPE")


class TestEnsembleFit:
    def test_fit_on_train_window_is_honest_and_reproducible(self, synthetic_db):
        repo = Repository(synthetic_db)
        games = sorted(repo.list_games(), key=lambda g: (g.game_date, g.game_id))
        cutoff = games[15].game_date  # train on the first ~15 games

        agent = EnsembleAgent()
        weights = agent.fit(repo, cutoff, n_simulations=150, seed=7, warmup_games=3)
        assert weights is not None
        # weights are a valid normalized distribution
        assert abs(weights.w_elo + weights.w_strength + weights.w_sim - 1.0) < 1e-9

        agent2 = EnsembleAgent()
        weights2 = agent2.fit(repo, cutoff, n_simulations=150, seed=7, warmup_games=3)
        assert weights == weights2

    def test_fitted_ensemble_never_worse_than_best_single_component(self, synthetic_db):
        from basketball_agent.evaluation.backtest import (
            BacktestConfig, BacktestEngine, EloPredictor, SimulationPredictor, StrengthPredictor,
        )
        from basketball_agent.evaluation.metrics import log_loss

        repo = Repository(synthetic_db)
        games = sorted(repo.list_games(), key=lambda g: (g.game_date, g.game_id))
        cutoff = games[15].game_date
        train_ids = [g.game_id for g in games if g.game_date < cutoff]
        config = BacktestConfig(n_simulations=150, seed=7, warmup_games=3)

        component_probs: dict[str, list[float]] = {}
        outcomes: list[bool] = []
        for name, pred in (
            ("elo", EloPredictor()),
            ("strength", StrengthPredictor()),
            ("sim", SimulationPredictor(n_simulations=150, seed=7)),
        ):
            result = BacktestEngine(repo, pred, config).run(game_ids=train_ids)
            for r in result.records:
                if r.prediction is None or r.actual_home_win is None:
                    continue
                component_probs.setdefault(name, []).append(r.prediction.prob_home)
                if name == "elo":
                    outcomes.append(r.actual_home_win)

        agent = EnsembleAgent()
        weights = agent.fit(repo, cutoff, n_simulations=150, seed=7, warmup_games=3)
        combined = [
            weights.w_elo * component_probs["elo"][i]
            + weights.w_strength * component_probs["strength"][i]
            + weights.w_sim * component_probs["sim"][i]
            for i in range(len(outcomes))
        ]
        combined_loss = log_loss(outcomes, combined)
        best_single = min(
            log_loss(outcomes, component_probs[name]) for name in ("elo", "strength", "sim")
        )
        assert combined_loss <= best_single + 1e-9
