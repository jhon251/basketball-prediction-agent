"""Per-league calibration: frozen defaults vs fitted params (Phase 8-C1).

One league at a time — the fit is league-scoped (Phase 8-B5), the evaluation
datasets are league-pure (Phase 8-B5 ``FittingDataset``), and nothing here
touches the frozen defaults.  Protocol, strictly chronological:

1. TRAIN  — ``fit_v1_params_ex`` fits HCA / logistic scale / shrinkage k /
   decay lambda by walk-forward over ``[train_start, cutoff)`` for ONE league;
   grid edges hit are recorded (never auto-widened).
2. TEST   — the frozen predictor set and the fitted predictor set are
   evaluated on the SAME held-out window ``[test_start, test_end)`` from one
   cached feature pass per decay value (bit-identical to BacktestEngine runs
   per the B5 invariant).  The test window is never seen by the fit.
3. LOOP   — the fitted parameters go to a params file the user applies via
   ``basketball predict --params <file>``; nothing is auto-deployed.

λ evidence: the sweep table (walk-forward logloss per lambda candidate over
the TRAIN window) is reported so "does λ>0 help?" is answered by data, and
if it does not, the frozen defaults stay (requirement 7).
"""
from __future__ import annotations

from datetime import datetime

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import (
    NON_REGULAR_COMPETITIONS,
    GameStatus,
    League,
)
from basketball_agent.evaluation.backtest import (
    EloPredictor,
    SimulationPredictor,
    StrengthPredictor,
)
from basketball_agent.evaluation.fitting import (
    FittingDataset,
    build_fitting_dataset,
)
from basketball_agent.evaluation.param_tuning import (
    FittedParams,
    fit_v1_params_ex,
)
from basketball_agent.models.win_probability import WinProbabilityConfig


def _test_window_ids(
    repo: Repository, league: League, test_start: datetime, test_end: datetime | None
) -> list[str]:
    """REGULAR_SEASON FINAL games of ONE league in the held-out window
    (explicitly non-regular segments excluded; untagged legacy rows stay
    eligible — same semantics as ``run_window_backtest``)."""
    games = [
        g for g in repo.list_games(
            status=GameStatus.FINAL,
            league=league,
            exclude_competitions=tuple(NON_REGULAR_COMPETITIONS),
        )
        if g.game_date >= test_start and (test_end is None or g.game_date < test_end)
    ]
    games.sort(key=lambda g: (g.game_date, g.game_id))
    return [g.game_id for g in games]


def calibrate_window(
    repo: Repository,
    league: League,
    *,
    cutoff: datetime,
    test_start: datetime,
    test_end: datetime | None = None,
    train_start: datetime | None = None,
    lambda_candidates: tuple[float, ...] = (0.0, 0.02, 0.05),
    n_simulations: int = 100,
    seed: int = 7,
    warmup_games: int = 5,
    include_lambda_sweep: bool = True,
) -> dict:
    """Fit on the train window, compare frozen vs fitted on the test window.

    Returns a dict with the fitted parameters (incl. boundary hits), the
    train-window lambda sweep table (when ``include_lambda_sweep``), and the
    frozen/fitted test metrics for elo / strength / simulation.  Elo has no
    calibrated component (lambda does not enter Elo ratings) and is included
    as an untouched reference.
    """
    fitted, sweep = fit_v1_params_ex(
        repo, cutoff,
        train_start=train_start,
        lambda_candidates=lambda_candidates,
        n_simulations=min(n_simulations, 150),
        seed=seed, warmup_games=warmup_games,
        league=league, include_sweep=True,
    )

    ids = _test_window_ids(repo, league, test_start, test_end)
    frozen_ds: FittingDataset = build_fitting_dataset(
        repo, ids, decay_lambda=0.0, warmup_games=warmup_games
    )
    fitted_ds: FittingDataset | None = (
        None if fitted.decay_lambda == 0.0
        else build_fitting_dataset(
            repo, ids, decay_lambda=fitted.decay_lambda, warmup_games=warmup_games
        )
    )
    win_config: WinProbabilityConfig = fitted.to_win_config()

    variants: dict[str, tuple[object, FittingDataset]] = {
        "frozen_elo": (EloPredictor(), frozen_ds),
        "frozen_strength": (StrengthPredictor(), frozen_ds),
        "frozen_simulation": (SimulationPredictor(n_simulations=n_simulations, seed=seed),
                              frozen_ds),
        "fitted_elo": (EloPredictor(), frozen_ds),  # Elo: no fitted component
        "fitted_strength": (StrengthPredictor(config=win_config),
                            fitted_ds or frozen_ds),
        "fitted_simulation": (SimulationPredictor(n_simulations=n_simulations, seed=seed,
                                                  win_config=win_config),
                              fitted_ds or frozen_ds),
    }
    metrics = {
        name: dataset.metrics(predictor)
        for name, (predictor, dataset) in variants.items()
    }

    return {
        "league": league.value,
        "train_window": fitted.training_window,
        "validation_logloss": fitted.validation_logloss,
        "fitted": fitted,
        "lambda_sweep": sweep if include_lambda_sweep else None,
        "boundary_hits": fitted.boundary_hits,
        "test_window": (test_start.isoformat(),
                        test_end.isoformat() if test_end else "end of data"),
        "n_test_games": len(ids),
        "metrics": metrics,
    }


def frozen_vs_fitted_table(result: dict) -> str:
    """Human-readable frozen-vs-fitted comparison for one window."""
    metrics = result["metrics"]
    lines = [
        f"CALIBRATION {result['league']}  "
        f"test {result['test_window'][0][:10]} .. {str(result['test_window'][1])[:10]}  "
        f"n={result['n_test_games']}",
        f"train window {result['train_window'][0][:10]} .. "
        f"{result['train_window'][1][:10]}  val_logloss={result['validation_logloss']:.4f}",
        f"fitted: HCA={result['fitted'].home_court_advantage:.2f} "
        f"scale={result['fitted'].margin_logistic_scale:.2f} "
        f"k={result['fitted'].shrinkage_k:.1f} "
        f"lambda={result['fitted'].decay_lambda:.3f}",
    ]
    if result["boundary_hits"]:
        lines.append(f"boundary hits: {'; '.join(result['boundary_hits'])}")
    else:
        lines.append("boundary hits: none")
    header = (f"{'variant':<18}{'n':>6}{'acc':>8}{'logloss':>9}{'brier':>8}"
              f"{'ece':>8}{'margin_mae':>12}{'total_mae':>11}")
    lines.append(header)
    for name in ("frozen_elo", "frozen_strength", "frozen_simulation",
                 "fitted_elo", "fitted_strength", "fitted_simulation"):
        m = metrics[name]
        fmt = lambda v: f"{v:.4f}" if v is not None else "n/a"
        lines.append(f"{name:<18}{m['n_win_prob']:>6}{fmt(m['accuracy']):>8}"
                     f"{fmt(m['log_loss']):>9}{fmt(m['brier_score']):>8}"
                     f"{fmt(m['ece']):>8}{fmt(m['margin_mae']):>12}"
                     f"{fmt(m['total_mae']):>11}")
    if result.get("lambda_sweep"):
        lines.append("train-window lambda sweep (logloss):")
        for lam in sorted(result["lambda_sweep"]):
            lines.append(f"  lambda={lam:.3f}  logloss="
                         f"{result['lambda_sweep'][lam]['log_loss']:.4f}")
    return "\n".join(lines)
