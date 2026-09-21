"""Phase 7 experiments EXP-NBA-001..004 (spec §17).

Strict protocol: parameters/weights are only ever fit on the TRAIN window;
the TEST window is used for evaluation only.  Every experiment returns its
metrics and an honest comparison — no preset conclusion that the simulation
or the ensemble must win (spec §18).

Phase 8-B2 additions: the training window can be bounded (``train_start`` /
``train_seasons``) so multi-season fitting stays feasible, and
:func:`run_window_backtest` evaluates the frozen-default predictors over an
explicit multi-season window.  Chronological order and leakage rules are
untouched; no model formula or default parameter changes here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from basketball_agent.agents.ensemble_agent import EnsembleAgent
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import (
    NON_REGULAR_COMPETITIONS, GameStatus, League,
)
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestRecord,
    BacktestResult,
    EloPredictor,
    RandomPredictor,
    SimulationPredictor,
    StrengthPredictor,
    warmup_not_met,
)
from basketball_agent.evaluation.calibration import build_reliability_table
from basketball_agent.evaluation.metrics import log_loss, mae
from basketball_agent.evaluation.param_tuning import FittedParams, fit_v1_params
from basketball_agent.features.base import FeatureComputationError
from basketball_agent.features.feature_pipeline import FeaturePipeline
from basketball_agent.models.pace_model import PaceConfig
from basketball_agent.models.score_model import ScoreModelConfig
from basketball_agent.models.win_probability import WinProbabilityConfig


def nba_season_start_year(moment: datetime) -> int:
    """Season-start year under the NBA calendar (seasons begin in October)."""
    return moment.year if moment.month >= 10 else moment.year - 1


def resolve_train_start(cutoff: datetime, train_seasons: int | None) -> datetime | None:
    """Bounded training window start for ``train_seasons`` NBA seasons.

    ``None`` (all available history) returns ``None``.  Season boundaries are
    October 1 of the NBA season-start year — a pure windowing convention, no
    game data is involved.
    """
    if train_seasons is None:
        return None
    if train_seasons < 1:
        raise ValueError("train_seasons must be >= 1")
    start_year = nba_season_start_year(cutoff) - (train_seasons - 1)
    return datetime(start_year, 10, 1, tzinfo=timezone.utc)


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
    train_start: datetime | None = None,
) -> dict:
    """Decay-lambda sweep on the TRAIN window only."""
    from basketball_agent.evaluation.backtest import sweep_decay_lambda

    train_games = sorted(
        (g for g in repo.list_games(status=GameStatus.FINAL)
         if (train_start is None or g.game_date >= train_start) and g.game_date < train_cutoff),
        key=lambda g: (g.game_date, g.game_id),
    )
    sweep = sweep_decay_lambda(
        repo, list(candidates),
        n_simulations=n_simulations, warmup_games=warmup_games, seed=seed,
        game_ids=[g.game_id for g in train_games],
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
    train_start: datetime | None = None,
) -> dict:
    """Fit the ensemble on TRAIN only; compare it against every single
    component on TEST.  Weights never see the test window."""
    agent = EnsembleAgent()
    weights = agent.fit(
        repo, train_cutoff, n_simulations=fit_n_simulations,
        seed=seed, warmup_games=warmup_games,
        train_start=train_start,
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
    train_start: datetime | None = None,
) -> dict:
    """EXP-NBA-001..004 with strict train/test isolation.

    Returns a dict keyed by experiment id.  Parameters are fitted on the
    train window ``[train_start, train_cutoff)`` and FROZEN for all
    test-window evaluations.
    """
    fitted = fit_v1_params(
        repo, train_cutoff, train_start=train_start,
        n_simulations=min(n_simulations, 150), seed=seed,
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
            seed=seed, warmup_games=warmup_games, train_start=train_start,
        ),
        "EXP-NBA-003": exp_nba_003_ensemble_vs_individual(
            repo, train_cutoff, test_start, test_end, n_simulations=n_simulations,
            fit_n_simulations=min(n_simulations, 150), seed=seed, warmup_games=warmup_games,
            train_start=train_start,
        ),
        "EXP-NBA-004": exp_nba_004_simulation_vs_baselines(
            repo, test_start, test_end, fitted=fitted, n_simulations=n_simulations,
            seed=seed, warmup_games=warmup_games,
        ),
    }


def run_window_backtest(
    repo: Repository,
    window_start: datetime,
    window_end: datetime | None,
    *,
    train_seasons: int | None = 1,
    n_simulations: int = 100,
    seed: int = 7,
    warmup_games: int = 5,
    league: League | None = None,
) -> dict:
    """Multi-season walk-forward backtest over one evaluation window.

    Phase 8-B2: evaluates the frozen-default predictors (random / elo /
    strength / simulation) on every REGULAR_SEASON FINAL game in
    ``[window_start, window_end)`` (explicitly non-regular segments are
    excluded; untagged legacy rows stay eligible); features are computed
    as-of each game's own tip-off over its full prior history (regular
    season only by default — the BacktestConfig handles that).  ``league``
    restricts the window to one league's games (Phase 8-B3; feature history
    is always league-scoped anyway).

    The four predictors share ONE feature computation per game: a single
    chronological pass feeds all predictors, producing records identical to
    running four independent BacktestEngines (each predictor is a pure
    function of the same features; the simulation reseeds per game).  This
    is a performance-only form — at league history depth ~19k games it
    avoids three redundant feature recomputations per game.

    Fitting, when requested via ``train_seasons``, happens only on the
    window immediately BEFORE ``window_start`` and is reported separately —
    never used to change model defaults.
    """
    if league is not None and not isinstance(league, League):
        # Phase 8-B5: a bare string (e.g. "EUROLEAGUE") used to match no rows
        # and silently report an empty window; convert explicitly so a typo
        # fails loudly instead of looking like "no games to evaluate".
        league = League(league)

    games = [
        g for g in repo.list_games(
            status=GameStatus.FINAL,
            league=league,
            exclude_competitions=tuple(NON_REGULAR_COMPETITIONS),
        )
        if g.game_date >= window_start and (window_end is None or g.game_date < window_end)
    ]
    games.sort(key=lambda g: (g.game_date, g.game_id))

    config = BacktestConfig(
        n_simulations=n_simulations, seed=seed, warmup_games=warmup_games,
    )
    pipeline = FeaturePipeline(repo)
    predictors: dict[str, object] = {
        "random": RandomPredictor(),
        "elo": EloPredictor(),
        "strength": StrengthPredictor(),
        "simulation": SimulationPredictor(n_simulations=n_simulations, seed=seed),
    }
    records_by: dict[str, list[BacktestRecord]] = {name: [] for name in predictors}

    for g in games:
        as_of = g.game_date
        actual = dict(
            actual_home_win=g.home_score > g.away_score,
            actual_margin=g.home_score - g.away_score,
            actual_total=g.home_score + g.away_score,
            actual_home_score=g.home_score,
            actual_away_score=g.away_score,
        )
        if warmup_not_met(repo, g, as_of, warmup_games):
            for records in records_by.values():
                records.append(BacktestRecord(
                    game_id=g.game_id, as_of=as_of, prediction=None,
                    skipped_reason=f"warmup < {warmup_games} prior games",
                ))
            continue
        try:
            features = pipeline.compute(g.game_id, as_of)
        except FeatureComputationError as exc:
            for records in records_by.values():
                records.append(BacktestRecord(
                    game_id=g.game_id, as_of=as_of, prediction=None,
                    skipped_reason=f"features: {exc}",
                ))
            continue
        for name, predictor in predictors.items():
            records_by[name].append(BacktestRecord(
                game_id=g.game_id, as_of=as_of, prediction=predictor.predict(features),
                skipped_reason=None, **actual,
            ))

    results: dict[str, dict] = {}
    for name, records in records_by.items():
        result = BacktestResult(tuple(records))
        metrics = result.summary()
        sim_recs = [r for r in result.records
                    if r.prediction is not None and r.actual_home_win is not None]
        metrics["reliability"] = [
            {"bin_low": b.bin_low, "bin_high": b.bin_high, "count": b.count,
             "mean_predicted": b.mean_predicted, "actual_rate": b.actual_rate}
            for b in build_reliability_table(
                [r.prediction.prob_home for r in sim_recs],
                [r.actual_home_win for r in sim_recs])
        ]
        results[name] = metrics

    fitted: FittedParams | None = None
    if train_seasons is not None:
        # anchor the fit window to the season BEFORE the evaluation window:
        # window_start sits on a season boundary, so resolving against it
        # directly would produce an empty [start, cutoff) interval
        anchor = window_start - timedelta(days=1)
        train_start = resolve_train_start(anchor, train_seasons)
        fitted = fit_v1_params(
            repo, window_start, train_start=train_start,
            n_simulations=min(n_simulations, 150), seed=seed, warmup_games=warmup_games,
            league=league,
        )

    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat() if window_end else "end of data",
        "n_window_games": len(games),
        "train_seasons": train_seasons,
        "fitted_params": fitted,
        "models": results,
    }
