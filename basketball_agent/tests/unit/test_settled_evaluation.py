"""Read-only settled evaluation & error analysis tests (Phase 10-A)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.db.connection import Database
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game
from basketball_agent.evaluation.settled_evaluation import (
    MIN_SLICE_SAMPLES,
    EvaluationFilter,
    SettlementEvaluator,
    per_model_evaluation,
)

UTC = timezone.utc


def _db_with_settled(tmp_path) -> Database:
    """Two leagues/seasons of settled predictions with known outcomes."""
    db = Database(tmp_path / "eval.db")
    db.initialize()
    from basketball_agent.data.db.loader import DataIngestor
    from basketball_agent.data.db.repository import Repository
    from basketball_agent.agents.prediction_agent import PredictionAgent
    from basketball_agent.agents.risk_agent import RiskAssessment, RiskFlag
    from basketball_agent.agents.ensemble_agent import EnsemblePrediction, EnsembleWeights
    from basketball_agent.features.base import FeatureProvenance
    from basketball_agent.models.game_params import GamePredictionParams
    from basketball_agent.simulation.monte_carlo import (
        DistributionStats, MonteCarloResult,
    )

    games = []
    # CBA 2024-25 (48-min era) and 2025-26 (40-min era) + NBA games
    specs = []  # (game_id, league, season, home, away, hs, as_)
    for i in range(40):  # 40 CBA 2024-25 games
        specs.append((f"CBA24-{i:04d}", "CBA", "2024-25", "CBA-A", "CBA-B",
                      100 + i, 90 + (i % 20)))
    for i in range(40):  # 40 CBA 2025-26 games
        specs.append((f"CBA25-{i:04d}", "CBA", "2025-26", "CBA-A", "CBA-B",
                      95 + (i % 15), 88 + i % 10))
    for i in range(10):  # 10 NBA games
        specs.append((f"202411{i:02d}_LAL_BOS", "NBA", "2024-25",
                      "NBA-A", "NBA-B", 110, 100 + i % 5))
    with db.transaction() as conn:
        for team_id, league in (("CBA-A", "CBA"), ("CBA-B", "CBA"),
                                ("NBA-A", "NBA"), ("NBA-B", "NBA")):
            conn.execute(
                "INSERT INTO teams (team_id, league, name, abbreviation) "
                "VALUES (?, ?, ?, ?)",
                (team_id, league, team_id, team_id))
        for gid, league, season, home, away, hs, as_ in specs:
            conn.execute(
                "INSERT INTO games (game_id, league, season, game_date, "
                "home_team_id, away_team_id, home_score, away_score, status, "
                "competition, inserted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, league, season, f"2025-0{1 + len(games) % 9}-01T12:00:00+00:00",
                 home, away, hs, as_, "FINAL", "REGULAR_SEASON",
                 "2025-01-01T00:00:00+00:00"))
            games.append(gid)

    store_stub = type("S", (), {})()
    # insert predictions directly with known probabilities (home wins in all)
    with db.transaction() as conn:
        for i, gid in enumerate(games):
            p_home = 0.48 if i % 4 == 0 else (0.55 + (i % 30) / 100.0)
            conn.execute(
                "INSERT INTO predictions (game_id, as_of, prob_home, prob_away, "
                "expected_margin, expected_total, n_simulations, seed, confidence, "
                "risk_flags, model_version, snapshot_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, "2025-01-01T00:00:00+00:00", p_home, 1 - p_home,
                 5.0 + i % 7, 190.0 + i % 11, 50, 7, 80.0, "[]", "0.1.0",
                 None, "2025-01-01T00:00:00+00:00"))

    # settle every prediction (records become settled with actual scores)
    from basketball_agent.evaluation.settlement import SettlementStore
    store = SettlementStore(db)
    for gid in games:
        store.settle(gid)
    return db


@pytest.fixture()
def evaluator(tmp_path) -> SettlementEvaluator:
    return SettlementEvaluator(_db_with_settled(tmp_path))


class TestMetricCorrectness:
    def test_overall_metrics_match_hand_computation(self, evaluator):
        m = evaluator.evaluate(EvaluationFilter())
        # all 90 settled records; hand-check via raw recompute
        records = evaluator._scoped(EvaluationFilter())
        assert m.n == 90
        import math
        ll = -sum(
            math.log(p) for p, r in zip((r["settled"].prob_home for r in records),
                                        records) if r["settled"].actual_home_score >
            r["settled"].actual_away_score)
        # home wins in every fixture -> logloss = -mean(log p)
        assert m.log_loss == pytest.approx(round(ll / len(records), 4))
        assert m.accuracy == pytest.approx(round(
            sum(1 for r in records if r["settled"].prob_home >= 0.5) / 90, 4))
        assert m.sufficient is True

    def test_margin_and_total_mae(self, evaluator):
        m = evaluator.evaluate(EvaluationFilter(league="CBA"))
        records = evaluator._scoped(EvaluationFilter(league="CBA"))
        expected = sum(abs(r["settled"].expected_margin -
                           (r["settled"].actual_home_score
                            - r["settled"].actual_away_score))
                       for r in records) / len(records)
        assert m.margin_mae == pytest.approx(round(expected, 4))
        assert m.total_mae is not None


class TestFiltersAndIsolation:
    def test_league_filter_isolates(self, evaluator):
        nba = evaluator.evaluate(EvaluationFilter(league="NBA"))
        cba = evaluator.evaluate(EvaluationFilter(league="CBA"))
        assert nba.n == 10 and cba.n == 80
        assert nba.n + cba.n == evaluator.evaluate(EvaluationFilter()).n

    def test_cba_dual_season_slices_separate(self, evaluator):
        by_season = evaluator.evaluate_by_season(
            EvaluationFilter(league="CBA"))
        assert set(by_season) == {"2024-25", "2025-26"}
        assert by_season["2024-25"].n == 40
        assert by_season["2025-26"].n == 40

    def test_season_filter(self, evaluator):
        m = evaluator.evaluate(EvaluationFilter(season="2025-26"))
        assert m.n == 40

    def test_empty_slice_reported_not_fabricated(self, evaluator):
        m = evaluator.evaluate(EvaluationFilter(league="BSL"))
        assert m.n == 0 and m.accuracy is None


class TestSmallSampleGuard:
    def test_min_slice_samples_constant(self):
        assert MIN_SLICE_SAMPLES == 30

    def test_small_slices_flagged(self, evaluator):
        slices = evaluator.error_slices(EvaluationFilter(league="NBA"))
        for name, m in slices.items():
            if m.n < MIN_SLICE_SAMPLES:
                assert m.sufficient is False
                if m.n > 0:
                    assert "descriptive only" in m.note


class TestErrorSlices:
    def test_bands_partition_all_records(self, evaluator):
        slices = evaluator.error_slices(EvaluationFilter())
        band_names = ("p_lt_0.40", "p_0.40_to_0.60", "p_0.60_to_0.80",
                      "p_ge_0.80")
        assert sum(slices[b].n for b in band_names) == 90
        assert slices["tossup_0.45_to_0.55"].n >= 0

    def test_calibration_drift_buckets(self, evaluator):
        drift = evaluator.calibration_drift(EvaluationFilter())
        # all records settled/as-of 2025-01-01 -> one H1 bucket
        assert set(drift) == {"2025-H1"}
        assert sum(m.n for m in drift.values()) == 90


class TestUnavailableNotFabricated:
    def test_model_disagreement_reported_unavailable(self, evaluator):
        disc = evaluator.model_disagreement_slices(EvaluationFilter())
        assert disc["available"] is False
        assert "window-backtest" in disc["reason"]
        assert disc["n_records"] == 90


class TestHistoricalCutoff:
    def test_window_end_excludes_later_records(self, evaluator):
        before = evaluator.evaluate(EvaluationFilter())
        cutoff = evaluator.evaluate(EvaluationFilter(
            window_end=datetime(2025, 1, 1, tzinfo=UTC)))
        # every record is as-of exactly 2025-01-01 -> excluded by exclusive end
        assert cutoff.n == 0
        assert before.n == 90
        start = evaluator.evaluate(EvaluationFilter(
            window_start=datetime(2025, 1, 1, tzinfo=UTC)))
        assert start.n == 90


class TestReadOnlyAndDeterminism:
    def test_evaluation_does_not_mutate_db(self, tmp_path, evaluator):
        db_path = tmp_path / "eval.db"
        import hashlib
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        evaluator.evaluate(EvaluationFilter())
        evaluator.error_slices(EvaluationFilter())
        evaluator.calibration_drift(EvaluationFilter())
        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before == after

    def test_deterministic(self, evaluator):
        a = evaluator.evaluate(EvaluationFilter(league="CBA"))
        b = evaluator.evaluate(EvaluationFilter(league="CBA"))
        assert a == b
        s1 = evaluator.error_slices(EvaluationFilter())
        s2 = evaluator.error_slices(EvaluationFilter())
        assert s1 == s2


class TestPerModelEvaluation:
    def test_uses_frozen_engine(self, tmp_path):
        """Per-model evaluation delegates to the frozen window-backtest
        engine and returns its per-model metrics (small synthetic league)."""
        from basketball_agent.data.collectors.synthetic import (
            SyntheticLeagueConfig, generate_league,
        )
        from basketball_agent.data.db.loader import DataIngestor
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.schemas.dataset import Dataset

        teams, players, games, team_stats, player_stats = generate_league(
            SyntheticLeagueConfig(n_teams=6, games_per_pair=3, seed=2025))
        ds = Dataset(games=tuple(games), team_stats=tuple(team_stats),
                     player_stats=tuple(player_stats), teams=tuple(teams),
                     players=tuple(players))
        raw = {k: [m.model_dump(mode="json") for m in v] for k, v in
               (("teams", ds.teams), ("players", ds.players), ("games", ds.games),
                ("team_stats", ds.team_stats), ("player_stats", ds.player_stats))}
        db = Database(tmp_path / "pm.db")
        db.initialize()
        DataIngestor(db, now=datetime(2025, 3, 1, tzinfo=UTC)).ingest(raw)
        repo = Repository(db)
        result = per_model_evaluation(
            repo, datetime(2024, 10, 1, tzinfo=UTC),
            datetime(2025, 7, 1, tzinfo=UTC), n_simulations=20)
        assert set(result) == {"random", "elo", "strength", "simulation"}
        for model, m in result.items():
            assert "accuracy" in m and "log_loss" in m and "brier_score" in m
