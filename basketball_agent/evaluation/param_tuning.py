"""Parameter estimation on the TRAINING window only (spec §10, §14, §15).

Every fitted parameter (HCA, logistic scale, shrinkage k, decay lambda) is
estimated exclusively from FINAL games strictly before ``cutoff``; the test
window is never touched here.  Results are frozen into :class:`FittedParams`
and persisted with their training window, selection method and validation
score so the audit chain stays complete.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    StrengthPredictor,
    sweep_decay_lambda,
)
from basketball_agent.evaluation.metrics import log_loss
from basketball_agent.models.win_probability import WinProbabilityConfig

UTC = timezone.utc

LAMBDA_CANDIDATES = (0.0, 0.02, 0.05)
SHRINKAGE_CANDIDATES = (0, 2, 5, 10, 20)
LOGISTIC_CANDIDATES = tuple(x / 2.0 for x in range(16, 37))  # 8.0 .. 18.0


@dataclass(frozen=True)
class FittedParams:
    home_court_advantage: float
    margin_logistic_scale: float
    shrinkage_k: float
    decay_lambda: float
    training_window: tuple[str, str]  # (start, cutoff) ISO
    selection_method: str
    validation_logloss: float
    fitted_at: str

    def to_win_config(self) -> WinProbabilityConfig:
        return WinProbabilityConfig(
            shrinkage_k=self.shrinkage_k,
            home_court_advantage=self.home_court_advantage,
            margin_logistic_scale=self.margin_logistic_scale,
        )


def _train_games(repo: Repository, cutoff: datetime):
    return sorted(
        (g for g in repo.list_games(status=GameStatus.FINAL) if g.game_date < cutoff),
        key=lambda g: (g.game_date, g.game_id),
    )


def estimate_home_court_advantage(repo: Repository, cutoff: datetime) -> float:
    """HCA = mean home margin over the training window (balanced schedules
    make this an unbiased estimate)."""
    games = _train_games(repo, cutoff)
    if not games:
        raise ValueError("training window is empty")
    return sum(g.home_score - g.away_score for g in games) / len(games)


def estimate_logistic_scale(repo: Repository, cutoff: datetime, hca: float) -> float:
    """Grid-search the logistic scale on the training window via walk-forward
    strength-model margins (no test data involved)."""
    games = _train_games(repo, cutoff)
    if len(games) < 10:
        raise ValueError("training window too small for scale estimation")
    best_scale, best_loss = LOGISTIC_CANDIDATES[0], None
    for scale in LOGISTIC_CANDIDATES:
        cfg = WinProbabilityConfig(
            shrinkage_k=5.0, home_court_advantage=hca, margin_logistic_scale=scale
        )
        recs = BacktestEngine(
            repo, StrengthPredictor(config=cfg), BacktestConfig(warmup_games=5)
        ).run(game_ids=[g.game_id for g in games]).records
        pairs = [
            (r.prediction.prob_home, r.actual_home_win)
            for r in recs if r.prediction is not None and r.actual_home_win is not None
        ]
        if len(pairs) < 5:
            continue
        loss = log_loss([y for _, y in pairs], [p for p, _ in pairs])
        if best_loss is None or loss < best_loss - 1e-12:
            best_loss, best_scale = loss, scale
    return best_scale


def estimate_shrinkage_k(repo: Repository, cutoff: datetime, hca: float, scale: float) -> float:
    """Grid-search shrinkage k on the training window (walk-forward)."""
    games = _train_games(repo, cutoff)
    if len(games) < 10:
        raise ValueError("training window too small for shrinkage estimation")
    best_k, best_loss = float(SHRINKAGE_CANDIDATES[0]), None
    for k in SHRINKAGE_CANDIDATES:
        cfg = WinProbabilityConfig(
            shrinkage_k=float(k), home_court_advantage=hca, margin_logistic_scale=scale
        )
        recs = BacktestEngine(
            repo, StrengthPredictor(config=cfg), BacktestConfig(warmup_games=5)
        ).run(game_ids=[g.game_id for g in games]).records
        pairs = [
            (r.prediction.prob_home, r.actual_home_win)
            for r in recs if r.prediction is not None and r.actual_home_win is not None
        ]
        if len(pairs) < 5:
            continue
        loss = log_loss([y for _, y in pairs], [p for p, _ in pairs])
        if best_loss is None or loss < best_loss - 1e-12:
            best_loss, best_k = loss, float(k)
    return best_k


def fit_v1_params(
    repo: Repository,
    cutoff: datetime,
    *,
    lambda_candidates: tuple[float, ...] = LAMBDA_CANDIDATES,
    n_simulations: int = 100,
    seed: int = 7,
    warmup_games: int = 5,
) -> FittedParams:
    """Full train-only parameter fit (HCA -> scale -> k -> decay lambda).

    Every step is evaluated by walk-forward backtest restricted to the
    training window.  The returned validation log loss is that of the final
    frozen configuration on the training window.
    """
    games = _train_games(repo, cutoff)
    if len(games) < 10:
        raise ValueError("training window too small to fit parameters")

    hca = estimate_home_court_advantage(repo, cutoff)
    scale = estimate_logistic_scale(repo, cutoff, hca)
    k = estimate_shrinkage_k(repo, cutoff, hca, scale)

    # decay lambda: sweep on the training window only
    sweep = sweep_decay_lambda(
        repo, list(lambda_candidates),
        n_simulations=n_simulations, warmup_games=warmup_games, seed=seed,
    )
    best_lambda, best_sweep = min(
        sweep.items(), key=lambda kv: kv[1]["log_loss"] or float("inf")
    )

    window_start = games[0].game_date
    fitted_at = datetime.now(timezone.utc).isoformat()
    return FittedParams(
        home_court_advantage=hca,
        margin_logistic_scale=scale,
        shrinkage_k=k,
        decay_lambda=best_lambda,
        training_window=(window_start.isoformat(), cutoff.isoformat()),
        selection_method=(
            "HCA: mean home margin; logistic scale: grid 8..18 step .5; "
            "shrinkage k: grid {0,2,5,10,20}; decay lambda: sweep "
            f"{sorted(lambda_candidates)} — all by train-window walk-forward"
        ),
        validation_logloss=float(best_sweep["log_loss"]),
        fitted_at=fitted_at,
    )


def save_fitted_params(params: FittedParams, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(params), indent=2), encoding="utf-8")


def load_fitted_params(path: Path | str) -> FittedParams:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["training_window"] = tuple(data["training_window"])
    return FittedParams(**data)


def format_param_report(params: FittedParams) -> str:
    lines = [
        "FROZEN PARAMETER REPORT",
        "=======================",
        f"parameter             value",
        f"home_court_advantage  {params.home_court_advantage:.2f}",
        f"margin_logistic_scale {params.margin_logistic_scale:.2f}",
        f"shrinkage_k           {params.shrinkage_k:.1f}",
        f"decay_lambda          {params.decay_lambda:.3f}",
        f"training window       {params.training_window[0][:10]} .. "
        f"{params.training_window[1][:10]}",
        f"selection method      {params.selection_method}",
        f"validation logloss    {params.validation_logloss:.4f}",
    ]
    return "\n".join(lines)
