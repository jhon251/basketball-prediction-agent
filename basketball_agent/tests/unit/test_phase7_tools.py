"""Unit tests for error analysis and the Phase 7 final report renderer."""
from __future__ import annotations

import pytest

from basketball_agent.data.collectors.manifest import DataSourceManifest
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    RandomPredictor,
)
from basketball_agent.evaluation.error_analysis import error_analysis
from basketball_agent.evaluation.phase7_report import build_phase7_report
from basketball_agent.evaluation.param_tuning import FittedParams

pytestmark = pytest.mark.unit


class TestErrorAnalysis:
    def test_groups_produced(self, ingested_db):
        result = BacktestEngine(
            __import__("basketball_agent.data.db.repository", fromlist=["Repository"]).Repository(
                ingested_db
            ),
            RandomPredictor(),
            BacktestConfig(warmup_games=0),
        ).run()
        groups = error_analysis(result, __import__(
            "basketball_agent.data.db.repository", fromlist=["Repository"]
        ).Repository(ingested_db))
        assert "home_favorites (p>=0.5)" in groups
        assert "close games (|margin|<=5)" in groups
        assert "season 2024-25" in groups
        for name, metrics in groups.items():
            assert set(metrics) == {"n", "accuracy", "log_loss", "brier"}

    def test_empty_result(self, ingested_db):
        from basketball_agent.data.db.repository import Repository

        repo = Repository(ingested_db)
        result = BacktestEngine(repo, RandomPredictor(), BacktestConfig(warmup_games=99)).run()
        assert error_analysis(result, repo) == {}


class TestPhase7Report:
    def _fixture_report(self) -> str:
        manifest = DataSourceManifest(
            source="nba_games", status="AVAILABLE", file_name="x.csv",
            file_hash="a" * 64, retrieved_at="2025-01-01T00:00:00+00:00",
            source_version="v1",
        )
        coverage = {
            "seasons": ["2024-25"], "games": 100, "teams": 30,
            "date_range": ("2024-10-01", "2025-01-01"),
            "missing_games": "unavailable without a schedule reference",
            "missing_scores": 0, "missing_stats": 0, "duplicate_games": 0,
        }
        params = FittedParams(
            home_court_advantage=2.4, margin_logistic_scale=11.0, shrinkage_k=5.0,
            decay_lambda=0.02, training_window=("2024-10-01T00:00:00+00:00",
                                                "2025-01-01T00:00:00+00:00"),
            selection_method="grid", validation_logloss=0.68,
            fitted_at="2025-01-02T00:00:00+00:00",
        )
        experiments = {
            "EXP-NBA-001": {"description": "d", "test_window": ("a", "b"), "models": {
                "random": {"accuracy": 0.5, "log_loss": 0.693, "brier_score": 0.25, "ece": 0.1},
                "simulation": {"accuracy": 0.55, "log_loss": 0.68, "brier_score": 0.24, "ece": 0.08},
            }},
            "EXP-NBA-002": {"description": "d", "train_cutoff": "a",
                            "chosen_lambda": 0.02, "chosen_validation_logloss": 0.68},
            "EXP-NBA-003": {"description": "d", "weights": {"elo": 0.1, "strength": 0.2, "sim": 0.7},
                            "test_logloss": {"elo": 0.7, "strength": 0.69, "simulation": 0.68,
                                             "ensemble": 0.66}},
            "EXP-NBA-004": {"description": "d", "test_window": ("a", "b"), "models": {
                "random": {"accuracy": 0.5, "log_loss": 0.693, "brier_score": 0.25, "ece": 0.1},
                "simulation": {"accuracy": 0.55, "log_loss": 0.68, "brier_score": 0.24, "ece": 0.08},
            }, "simulation_vs_baselines": {"simulation vs random": "simulation BETTER by 0.013 logloss"}},
        }
        return build_phase7_report(
            source_status={"source": "nba_games (fixture)", "status": "AVAILABLE"},
            manifests=[manifest],
            coverage_report=coverage,
            leakage={"total": 10, "passed": 10, "failed": 0},
            windows={"train": "2024-10..2024-12", "validation": "n/a", "test": "2025-01.."},
            params=params,
            experiments=experiments,
            reliability_table=[],
            score_metrics={"home_mae": 10.0, "away_mae": 11.0, "total_mae": 12.0, "margin_mae": 13.0},
            error_groups={"close games (|margin|<=5)": {"n": 3, "accuracy": 0.5, "log_loss": 0.7, "brier": 0.26}},
            known_limitations=["no real data yet"],
        )

    def test_report_contains_all_required_sections(self):
        report = self._fixture_report()
        for section in (
            "# Phase 7 Final Report",
            "## Data",
            "Source:",
            "Date Range:",
            "## Temporal Integrity",
            "Leakage Tests:",
            "Passed:",
            "Failed:",
            "## Backtest",
            "Train:",
            "Test:",
            "## Results",
            "| Model | Accuracy | LogLoss | Brier | ECE |",
            "| random |",
            "## Score Prediction",
            "Home MAE:",
            "## Calibration",
            "## Experiments",
            "### EXP-NBA-001",
            "### EXP-NBA-002",
            "### EXP-NBA-003",
            "### EXP-NBA-004",
            "## Frozen Parameters",
            "| HCA |",
            "## Error Analysis",
            "## Conclusion",
            "## Known Limitations",
        ):
            assert section in report

    def test_conclusion_answers_key_questions(self):
        report = self._fixture_report()
        assert "1. V1 on real NBA data:" in report
        assert "2. Simulation vs baselines:" in report
        assert "3. Ensemble vs best single model:" in report
        assert "6. Next step:" in report
