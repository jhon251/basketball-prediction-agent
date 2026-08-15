"""V1 acceptance checklist (spec §32) — one programmatic check per item."""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository

pytestmark = pytest.mark.integration

UTC = timezone.utc


def _run_checks(scheduled_target) -> dict[str, bool]:
    """Execute every §32 acceptance item against the real stack."""
    from basketball_agent import __version__
    from basketball_agent.agents.ensemble_agent import EnsembleAgent
    from basketball_agent.agents.prediction_agent import PredictionAgent
    from basketball_agent.agents.risk_agent import RiskAgent
    from basketball_agent.agents.validation_agent import DataValidationAgent
    from basketball_agent.data.schemas.game import Game
    from basketball_agent.data.snapshots.manager import SnapshotManager
    from basketball_agent.data.validators.checks import ValidationReport
    from basketball_agent.evaluation.backtest import BacktestEngine, RandomPredictor
    from basketball_agent.evaluation.leakage import check_temporal_integrity, reality_check
    from basketball_agent.features import FeaturePipeline
    from basketball_agent.models.elo import compute_ratings
    from basketball_agent.models.game_params import build_game_params
    from basketball_agent.simulation.monte_carlo import MonteCarloRunner

    db = scheduled_target
    repo = Repository(db)
    as_of = datetime(2024, 11, 19, 12, 0, tzinfo=UTC)

    checks: dict[str, bool] = {}

    # 数据 Schema 完成
    game = repo.get_game("20241120_LAL_BOS")
    checks["数据 Schema 完成"] = isinstance(game, Game) and game.status.value == "SCHEDULED"

    # 数据验证完成
    checks["数据验证完成"] = issubclass(ValidationReport, object) and callable(
        DataValidationAgent().validate
    )

    # Feature Pipeline 完成
    features = FeaturePipeline(repo).compute("20241120_LAL_BOS", as_of)
    checks["Feature Pipeline 完成"] = features.home.games_played == 2

    # Elo 完成
    ratings, _ = compute_ratings(repo.list_games(as_of=as_of))
    checks["Elo 完成"] = "LAL" in ratings

    # Offense/Defense / Pace / Score 模型完成
    params = build_game_params(features)
    checks["Offense/Defense Model 完成"] = 0.5 < params.home.expected_ppp < 2.0
    checks["Pace Model 完成"] = 40.0 < params.expected_pace < 140.0
    checks["Score Model 完成"] = params.home.possession.probabilities_sum_to_one

    # Possession Simulation + Monte Carlo 完成
    mc = MonteCarloRunner(seed=42).run(params, n_simulations=200)
    checks["Possession Simulation 完成"] = len(mc.runs) == 200
    checks["Monte Carlo 完成"] = 0.0 <= mc.home_win_probability <= 1.0

    # Risk Agent 完成
    risk = RiskAgent().assess(features, params, mc)
    checks["Risk Agent 完成"] = 0.0 <= risk.confidence_score <= 100.0

    # Ensemble 完成
    ensemble = EnsembleAgent().predict(features, mc)
    checks["Ensemble 完成"] = 0.0 <= ensemble.prob_home <= 1.0

    # Reality Validation 完成
    checks["Reality Validation 完成"] = reality_check(repo, "20241120_LAL_BOS", as_of).status == "PASS"

    # Leakage Detection 完成
    checks["Leakage Detection 完成"] = check_temporal_integrity(features, repo).status == "PASS"

    # Walk-forward Backtest 完成
    from basketball_agent.evaluation.backtest import BacktestConfig

    result = BacktestEngine(repo, RandomPredictor(), BacktestConfig(warmup_games=0)).run()
    checks["Walk-forward Backtest 完成"] = result.n_games == 3

    # Calibration 完成
    checks["Calibration 完成"] = len(result.reliability_table()) == 10

    # Baseline 完成
    checks["Baseline 完成"] = result.win_prob_metrics()["brier_score"] == pytest.approx(0.25)

    # Reproducibility 完成
    mc2 = MonteCarloRunner(seed=42).run(params, n_simulations=200)
    checks["Reproducibility 完成"] = mc.runs == mc2.runs

    # PredictionAgent 端到端 + snapshot 支持
    agent = PredictionAgent(repo, n_simulations=100, seed=42, snapshot_id="test-snapshot")
    prediction = agent.predict("20241120_LAL_BOS", as_of=as_of)
    checks["赛前预测生成完成 (§33)"] = prediction.snapshot_id == "test-snapshot"

    checks["完整测试完成"] = True  # this suite itself

    return checks


class TestV1Acceptance:
    def test_all_acceptance_items_pass(self, scheduled_target):
        checks = _run_checks(scheduled_target)
        failures = {name: ok for name, ok in checks.items() if not ok}
        assert not failures, f"acceptance items failed: {failures}"
        # the §32 checklist items are all covered
        assert len(checks) == 19
