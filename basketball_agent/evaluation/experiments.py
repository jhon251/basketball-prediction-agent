"""Phase 7 experiments EXP-NBA-001..004 (spec §17).

Strict protocol: parameters/weights are only ever fit on the TRAIN window;
the TEST window is used for evaluation only.  Every experiment returns its
metrics and an honest comparison — no preset conclusion that the simulation
or the ensemble must win (spec §18).
"""
from __future__ import annotations

from datetime import datetime

from basketball_agent.agents.ensemble_agent import EnsembleAgent
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    EloPredictor,
    RandomPredictor,
    SimulationPredictor,
    StrengthPredictor,
)
from basketball_agent.evaluation.metrics import log_loss
from basketball_agent.evaluation.param_tuning import FittedParams, fit_v1_params
from basketball_agent.models.pace_model import PaceConfig
from basketball_agent.models.score_model import ScoreModelConfig
from basketball_agent.models.win_probability import WinProbabilityConfig


def _test_game_ids(repo: Repository, test_start: datetime, test_end: datetime | None) -> list[str]:
    games = repo.list_games(status=GameStatus.FINAL)
    return [
        g.game_id for g in games
        if g.game_date >= test_start and (test_end is None or g.game_date < test_end)
    ]


def exp_nba_001_v1_on_real_data(
    repo: Repository,
    test_start: datetime,
    test_end: datetime | None = None,
    *,
    n_simulations: int = 1000,
    seed: int = 7,
    warmup_games: int = 5,
) -> dict:
    """Current V1 (frozen defaults) on the test window — does it have ANY
    predictive value?  Returns per-model win-probability metrics."""
    ids = _test_game_ids(repo, test_start, test_end)
    config = BacktestConfig(n_simulations=n_simulations, seed=seed, warmup_games=warmup_games)
    models = {}
    for name, predictor in (
        ("random", RandomPredictor()),
        ("elo", EloPredictor()),
        ("strength", StrengthPredictor()),
        ("simulation", SimulationPredictor(n_simulations=n_simulations, seed=seed)),
    ):
        result = BacktestEngine(repo, predictor, config).run(game_ids=ids)
        models[name] = result.win_prob_metrics()
    return {
        "experiment": "EXP-NBA-001",
        "description": "Current V1 on real NBA data (frozen defaults)",
        "test_window": (test_start.isoformat(),
                        (test_end.isoformat() if test_end else "end of data")),
        "n_test_games_considered": len(ids),
        "models": models,
    }


def exp_nba_002_decay_lambda_sweep(
    repo: Repository,
    train_cutoff: datetime,
    *,
    candidates: tuple[float, ...] = (0.0, 0.02, 0.05),
    n_simulations: int = 100,
    seed: int = 7,
    warmup_games: int = 5,
) -> dict:
    """Decay-lambda sweep on the TRAIN window only."""
    from basketball_agent.evaluation.backtest import sweep_decay_lambda

    sweep = sweep_decay_lambda(
        repo, list(candidates),
        n_simulations=n_simulations, warmup_games=warmup_games, seed=seed,
    )
    best = min(sweep.items(), key=lambda kv: kv[1]["log_loss"] or float("inf"))
    return {
        "experiment": "EXP-NBA-002",
        "description": "Decay lambda sweep (train window only)",
        "train_cutoff": train_cutoff.isoformat(),
        "sweep": {str(lam): summary for lam, summary in sweep.items()},
        "chosen_lambda": best[0],
        "chosen_validation_logloss": best[1]["log_loss"],
    }


def exp_nba_003_ensemble_vs_individual(
    repo: Repository,
    train_cutoff: datetime,
    test_start: datetime,
    test_end: datetime | None = None,
    *,
    n_simulations: int = 1000,
    fit_n_simulations: int = 150,
    seed: int = 7,
    warmup_games: int = 5,
) -> dict:
    """Fit the ensemble on TRAIN only; compare it against every single
    component on TEST.  Weights never see the test window."""
    agent = EnsembleAgent()
    weights = agent.fit(
        repo, train_cutoff, n_simulations=fit_n_simulations,
        seed=seed, warmup_games=warmup_games,
    )
    ids = _test_game_ids(repo, test_start, test_end)
    config = BacktestConfig(n_simulations=n_simulations, seed=seed, warmup_games=warmup_games)
    losses: dict[str, float | None] = {}
    for name, predictor in (
        ("elo", EloPredictor()),
        ("strength", StrengthPredictor()),
        ("simulation", SimulationPredictor(n_simulations=n_simulations, seed=seed)),
    ):
        result = BacktestEngine(repo, predictor, config).run(game_ids=ids)
        recs = [r for r in result.records if r.prediction is not None and r.actual_home_win is not None]
        losses[name] = log_loss(
            [r.actual_home_win for r in recs], [r.prediction.prob_home for r in recs]
        ) if recs else None

    ensemble_agent = EnsembleAgent(weights, n_simulations=n_simulations, seed=seed)
    ensemble_probs: list[float] = []
    outcomes: list[bool] = []
    engine = BacktestEngine(repo, ensemble_agent, config)  # type: ignore[arg-type]
    for r in engine.run(game_ids=ids).records:
        if r.prediction is not None and r.actual_home_win is not None:
            ensemble_probs.append(r.prediction.prob_home)
            outcomes.append(r.actual_home_win)
    ensemble_loss = log_loss(outcomes, ensemble_probs) if outcomes else None

    return {
        "experiment": "EXP-NBA-003",
        "description": "Ensemble vs individual models (weights fitted on train only)",
        "train_cutoff": train_cutoff.isoformat(),
        "test_window": (test_start.isoformat(),
                        (test_end.isoformat() if test_end else "end of data")),
        "weights": {
            "elo": weights.w_elo, "strength": weights.w_strength, "sim": weights.w_sim,
        },
        "test_logloss": {"elo": losses["elo"], "strength": losses["strength"],
                         "simulation": losses["simulation"], "ensemble": ensemble_loss},
    }


def exp_nba_004_simulation_vs_baselines(
    repo: Repository,
    test_start: datetime,
    test_end: datetime | None = None,
    *,
    fitted: FittedParams | None = None,
    n_simulations: int = 1000,
    seed: int = 7,
    warmup_games: int = 5,
) -> dict:
    """Monte Carlo simulation vs simple baselines on the TEST window."""
    ids = _test_game_ids(repo, test_start, test_end)
    config = BacktestConfig(n_simulations=n_simulations, seed=seed, warmup_games=warmup_games)
    results = {}
    predictors = {
        "random": RandomPredictor(),
        "elo": EloPredictor(),
        "strength": StrengthPredictor(
            config=fitted.to_win_config() if fitted else None
        ),
        "simulation": SimulationPredictor(n_simulations=n_simulations, seed=seed),
    }
    for name, predictor in predictors.items():
        result = BacktestEngine(repo, predictor, config).run(game_ids=ids)
        results[name] = result.win_prob_metrics()
    comparison = _compare(results)
    return {
        "experiment": "EXP-NBA-004",
        "description": "Simulation vs baselines on the test window",
        "test_window": (test_start.isoformat(),
                        (test_end.isoformat() if test_end else "end of data")),
        "models": results,
        "simulation_vs_baselines": comparison,
    }


def _compare(results: dict[str, dict]) -> dict[str, str]:
    sim = results.get("simulation", {}).get("log_loss")
    out: dict[str, str] = {}
    if sim is None:
        return {"note": "no simulation results"}
    for name in ("random", "elo", "strength"):
        other = results.get(name, {}).get("log_loss")
        if other is None:
            out[f"simulation vs {name}"] = "n/a"
            continue
        delta = other - sim
        if delta > 0.01:
            out[f"simulation vs {name}"] = f"simulation BETTER by {delta:.3f} logloss"
        elif delta < -0.01:
            out[f"simulation vs {name}"] = f"simulation WORSE by {-delta:.3f} logloss"
        else:
            out[f"simulation vs {name}"] = "simulation ≈ baseline (within 0.01)"
    return out


def run_phase7_experiments(
    repo: Repository,
    train_cutoff: datetime,
    test_start: datetime,
    test_end: datetime | None = None,
    *,
    n_simulations: int = 500,
    seed: int = 7,
    warmup_games: int = 5,
) -> dict:
    """EXP-NBA-001..004 with strict train/test isolation.

    Returns a dict keyed by experiment id.  Parameters are fitted on the
    train window and FROZEN for all test-window evaluations.
    """
    fitted = fit_v1_params(
        repo, train_cutoff, n_simulations=min(n_simulations, 150), seed=seed,
        warmup_games=warmup_games,
    )
    return {
        "fitted_params": fitted,
        "EXP-NBA-001": exp_nba_001_v1_on_real_data(
            repo, test_start, test_end, n_simulations=n_simulations,
            seed=seed, warmup_games=warmup_games,
        ),
        "EXP-NBA-002": exp_nba_002_decay_lambda_sweep(
            repo, train_cutoff, n_simulations=min(n_simulations, 150),
            seed=seed, warmup_games=warmup_games,
        ),
        "EXP-NBA-003": exp_nba_003_ensemble_vs_individual(
            repo, train_cutoff, test_start, test_end, n_simulations=n_simulations,
            fit_n_simulations=min(n_simulations, 150), seed=seed, warmup_games=warmup_games,
        ),
        "EXP-NBA-004": exp_nba_004_simulation_vs_baselines(
            repo, test_start, test_end, fitted=fitted, n_simulations=n_simulations,
            seed=seed, warmup_games=warmup_games,
        ),
    }
