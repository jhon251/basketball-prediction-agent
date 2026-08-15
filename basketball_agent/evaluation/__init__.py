"""Evaluation: walk-forward backtest, metrics, calibration, leakage detection."""

from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestRecord,
    BacktestResult,
    EloPredictor,
    HistoricalPredictor,
    Prediction,
    Predictor,
    RandomPredictor,
    SimulationPredictor,
    StrengthPredictor,
    sweep_decay_lambda,
)
from basketball_agent.evaluation.calibration import (
    ReliabilityBin,
    build_reliability_table,
    expected_calibration_error,
)
from basketball_agent.evaluation.leakage import (
    LeakageReport,
    check_temporal_integrity,
    reality_check,
)
from basketball_agent.evaluation.metrics import accuracy, brier_score, log_loss, mae, rmse

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestRecord",
    "BacktestResult",
    "EloPredictor",
    "HistoricalPredictor",
    "LeakageReport",
    "Prediction",
    "Predictor",
    "RandomPredictor",
    "ReliabilityBin",
    "SimulationPredictor",
    "StrengthPredictor",
    "accuracy",
    "brier_score",
    "build_reliability_table",
    "check_temporal_integrity",
    "expected_calibration_error",
    "log_loss",
    "mae",
    "reality_check",
    "rmse",
    "sweep_decay_lambda",
]
