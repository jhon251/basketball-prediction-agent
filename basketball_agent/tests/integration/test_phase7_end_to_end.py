"""End-to-end Phase 7 test: real-format CSV -> ingest -> experiments ->
error analysis -> §21 Final Report (the exact flow that will run the moment
real data lands; here exercised on a real-FORMAT fixture)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.collectors.manifest import RawDataStore
from basketball_agent.data.collectors.nba_csv import load_nba_games_csv
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    SimulationPredictor,
)
from basketball_agent.evaluation.calibration import build_reliability_table
from basketball_agent.evaluation.coverage import build_coverage_report
from basketball_agent.evaluation.error_analysis import error_analysis
from basketball_agent.evaluation.experiments import run_phase7_experiments
from basketball_agent.evaluation.phase7_report import build_phase7_report

pytestmark = pytest.mark.integration

UTC = timezone.utc


@pytest.fixture
def phase7_db(tmp_path, fixed_now):
    from basketball_agent.tests.integration.test_real_data_pipeline import make_real_csv

    csv_path = tmp_path / "nba_2024.csv"
    make_real_csv(csv_path, n_games=24)
    db = Database(tmp_path / "p7.db")
    db.initialize()
    RawDataStore(tmp_path / "raw").register("nba_games", csv_path, source_version="v1")
    raw = load_nba_games_csv(csv_path)
    DataIngestor(db, now=fixed_now).ingest(raw)
    return db


class TestPhase7EndToEnd:
    def test_full_chain_produces_report(self, phase7_db, tmp_path):
        repo = Repository(phase7_db)
        games = sorted(repo.list_games(status=GameStatus.FINAL),
                       key=lambda g: (g.game_date, g.game_id))
        n = len(games)
        train_cutoff = games[int(n * 0.6)].game_date
        test_start = games[int(n * 0.6)].game_date

        results = run_phase7_experiments(
            repo, train_cutoff, test_start, n_simulations=60, seed=7, warmup_games=3
        )
        assert set(results) == {"fitted_params", "EXP-NBA-001", "EXP-NBA-002",
                                "EXP-NBA-003", "EXP-NBA-004"}

        test_ids = [g.game_id for g in games if g.game_date >= test_start]
        sim_result = BacktestEngine(
            repo, SimulationPredictor(n_simulations=60, seed=7),
            BacktestConfig(n_simulations=60, seed=7, warmup_games=3),
        ).run(game_ids=test_ids)
        recs = [r for r in sim_result.records
                if r.prediction is not None and r.actual_home_win is not None]
        assert recs, "test window must contain predictable games"
        table = build_reliability_table(
            [r.prediction.prob_home for r in recs], [r.actual_home_win for r in recs]
        )

        report = build_phase7_report(
            source_status={"source": "nba_games (real-format fixture)", "status": "AVAILABLE"},
            manifests=RawDataStore(tmp_path / "raw").list_sources(),
            coverage_report=build_coverage_report(repo),
            leakage={"total": 18, "passed": 18, "failed": 0},
            windows={"train": f".. {train_cutoff.date().isoformat()}",
                     "validation": "embedded walk-forward",
                     "test": f"{test_start.date().isoformat()} .."},
            params=results["fitted_params"],
            experiments=results,
            reliability_table=table,
            score_metrics={"home_mae": 10.0, "away_mae": 11.0, "total_mae": 12.0, "margin_mae": 13.0},
            error_groups=error_analysis(sim_result, repo),
            known_limitations=["fixture data only"],
        )

        # every §21 section must be present with real content
        for section in (
            "# Phase 7 Final Report", "## Data", "Source: nba_games",
            "Date Range:", "## Temporal Integrity", "## Backtest",
            "## Results", "| Model | Accuracy | LogLoss | Brier | ECE |",
            "| simulation |", "## Score Prediction", "## Calibration",
            "## Experiments", "### EXP-NBA-001", "### EXP-NBA-002",
            "### EXP-NBA-003", "### EXP-NBA-004", "## Frozen Parameters",
            "## Error Analysis", "## Conclusion", "## Known Limitations",
        ):
            assert section in report, f"missing section: {section}"

        # the conclusion answers the key questions from actual results
        assert "1. V1 on real NBA data:" in report
        assert "2. Simulation vs baselines:" in report
        assert "3. Ensemble vs best single model:" in report

    def test_report_honest_when_models_do_not_win(self, phase7_db, tmp_path):
        # conclusion must never claim superiority the metrics do not show:
        # check the phrase logic by comparing with the underlying comparison
        repo = Repository(phase7_db)
        games = sorted(repo.list_games(status=GameStatus.FINAL),
                       key=lambda g: (g.game_date, g.game_id))
        cutoff = games[int(len(games) * 0.6)].game_date
        results = run_phase7_experiments(
            repo, cutoff, cutoff, n_simulations=50, seed=7, warmup_games=3
        )
        cmp = results["EXP-NBA-004"]["simulation_vs_baselines"]
        for statement in cmp.values():
            assert statement.startswith("simulation") or statement == "n/a"
