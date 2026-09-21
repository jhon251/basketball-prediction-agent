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
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.evaluation.backtest import (
    StrengthPredictor,
    sweep_decay_lambda,
)
from basketball_agent.evaluation.fitting import (
    FittingDataset,
    build_fitting_dataset,
)
from basketball_agent.models.win_probability import WinProbabilityConfig

UTC = timezone.utc

LAMBDA_CANDIDATES = (0.0, 0.02, 0.05)
SHRINKAGE_CANDIDATES = (0, 2, 5, 10, 20)
LOGISTIC_CANDIDATES = tuple(x / 2.0 for x in range(16, 37))  # 8.0 .. 18.0

#: The strength-grid walk-forward pass has always used a fixed 5-game warmup
#: (independent of ``fit_v1_params(warmup_games=...)``, which only ever fed
#: the lambda sweep).  Phase 8-B5 preserves that exactly so fitted parameters
#: stay bit-identical.
_STRENGTH_GRID_WARMUP = 5


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


def _train_games(
    repo: Repository, cutoff: datetime, train_start: datetime | None = None,
    league: League | None = None,
):
    """FINAL games in the TRAINING window: [train_start, cutoff).

    ``train_start`` bounds the fitting window (Phase 8-B2); ``None`` keeps
    the historical all-available behaviour.  ``league`` (Phase 8-B5) keeps a
    fit inside one competition's games: NBA and EuroLeague share one database
    but never share a training set or a cache.  Chronological order is kept
    and no future information can enter: everything is strictly before cutoff.
    """
    games = sorted(
        (g for g in repo.list_games(status=GameStatus.FINAL, league=league)
         if g.game_date < cutoff),
        key=lambda g: (g.game_date, g.game_id),
    )
    if train_start is not None:
        games = [g for g in games if g.game_date >= train_start]
    return games


def estimate_home_court_advantage(
    repo: Repository, cutoff: datetime, train_start: datetime | None = None,
    *, league: League | None = None,
) -> float:
    """HCA = mean home margin over the training window (balanced schedules
    make this an unbiased estimate)."""
    games = _train_games(repo, cutoff, train_start, league)
    if not games:
        raise ValueError("training window is empty")
    return sum(g.home_score - g.away_score for g in games) / len(games)


def estimate_logistic_scale(
    repo: Repository, cutoff: datetime, hca: float,
    train_start: datetime | None = None,
    *,
    dataset: "FittingDataset | None" = None,
    league: League | None = None,
) -> float:
    """Grid-search the logistic scale on the training window via walk-forward
    strength-model margins (no test data involved).

    Phase 8-B5: the walk-forward feature pass is computed ONCE and every
    candidate scale is evaluated on the cached features.  Features do not
    depend on the logistic scale, so the result is bit-identical to the old
    per-candidate engine runs (pinned by a regression test).
    """
    games = _train_games(repo, cutoff, train_start, league)
    if len(games) < 10:
        raise ValueError("training window too small for scale estimation")
    if dataset is None:
        dataset = build_fitting_dataset(
            repo, [g.game_id for g in games],
            decay_lambda=0.0, warmup_games=_STRENGTH_GRID_WARMUP,
        )
    if league is not None and dataset.league != league.value:
        raise ValueError(
            f"scale grid got a {dataset.league} dataset for a "
            f"{league.value} training window — cross-league fitting is forbidden"
        )
    best_scale, best_loss = LOGISTIC_CANDIDATES[0], None
    for scale in LOGISTIC_CANDIDATES:
        cfg = WinProbabilityConfig(
            shrinkage_k=5.0, home_court_advantage=hca, margin_logistic_scale=scale
        )
        if dataset.n_predicted < 5:  # legacy guard: too few usable games
            continue
        loss = dataset.metrics(StrengthPredictor(config=cfg))["log_loss"]
        if best_loss is None or loss < best_loss - 1e-12:
            best_loss, best_scale = loss, scale
    return best_scale


def estimate_shrinkage_k(
    repo: Repository, cutoff: datetime, hca: float, scale: float,
    train_start: datetime | None = None,
    *,
    dataset: "FittingDataset | None" = None,
    league: League | None = None,
) -> float:
    """Grid-search shrinkage k on the training window (walk-forward).

    Phase 8-B5: shares the same cached feature pass as the scale grid.
    """
    games = _train_games(repo, cutoff, train_start, league)
    if len(games) < 10:
        raise ValueError("training window too small for shrinkage estimation")
    if dataset is None:
        dataset = build_fitting_dataset(
            repo, [g.game_id for g in games],
            decay_lambda=0.0, warmup_games=_STRENGTH_GRID_WARMUP,
        )
    if league is not None and dataset.league != league.value:
        raise ValueError(
            f"shrinkage grid got a {dataset.league} dataset for a "
            f"{league.value} training window — cross-league fitting is forbidden"
        )
    best_k, best_loss = float(SHRINKAGE_CANDIDATES[0]), None
    for k in SHRINKAGE_CANDIDATES:
        cfg = WinProbabilityConfig(
            shrinkage_k=float(k), home_court_advantage=hca, margin_logistic_scale=scale
        )
        if dataset.n_predicted < 5:  # legacy guard: too few usable games
            continue
        loss = dataset.metrics(StrengthPredictor(config=cfg))["log_loss"]
        if best_loss is None or loss < best_loss - 1e-12:
            best_loss, best_k = loss, float(k)
    return best_k


def fit_v1_params(
    repo: Repository,
    cutoff: datetime,
    *,
    train_start: datetime | None = None,
    lambda_candidates: tuple[float, ...] = LAMBDA_CANDIDATES,
    n_simulations: int = 100,
    seed: int = 7,
    warmup_games: int = 5,
    league: League | None = None,
) -> FittedParams:
    """Full train-only parameter fit (HCA -> scale -> k -> decay lambda).

    Every step is evaluated by walk-forward backtest restricted to the
    training window ``[train_start, cutoff)`` (``train_start=None`` keeps the
    all-available behaviour).  The returned validation log loss is that of
    the final frozen configuration on the training window.

    ``league`` (Phase 8-B5) restricts the fit to one competition's games, so
    NBA and EuroLeague never train or cache together even though they share
    one database.  ``None`` keeps the legacy "whatever is in the window"
    behaviour, which now fails loudly if that window mixes leagues.

    Phase 8-B5: the walk-forward history is computed once per needed
    ``decay_lambda`` instead of once per grid candidate, which is the whole
    cost of the fit.  Every candidate still sees exactly the same features
    and the same game order, so the fitted values are unchanged.
    """
    games = _train_games(repo, cutoff, train_start, league)
    if len(games) < 10:
        raise ValueError("training window too small to fit parameters")

    game_ids = [g.game_id for g in games]
    hca = estimate_home_court_advantage(repo, cutoff, train_start, league=league)

    # Phase 8-B5: ONE walk-forward feature pass feeds the whole 26-candidate
    # strength grid (21 scales + 5 shrinkage values); the same pass is reused
    # for the lambda=0 leg of the sweep when the warmups coincide.
    grid_dataset = build_fitting_dataset(
        repo, game_ids, decay_lambda=0.0, warmup_games=_STRENGTH_GRID_WARMUP
    )
    scale = estimate_logistic_scale(
        repo, cutoff, hca, train_start, dataset=grid_dataset, league=league
    )
    k = estimate_shrinkage_k(
        repo, cutoff, hca, scale, train_start, dataset=grid_dataset, league=league
    )

    # decay lambda: sweep on the training window only
    datasets: dict[float, FittingDataset] = {}
    for lam in lambda_candidates:
        if lam == 0.0:
            if warmup_games == _STRENGTH_GRID_WARMUP:
                datasets[0.0] = grid_dataset
            else:  # legacy: the sweep honoured its own warmup value
                datasets[0.0] = build_fitting_dataset(
                    repo, game_ids, decay_lambda=0.0, warmup_games=warmup_games
                )
    sweep = sweep_decay_lambda(
        repo, list(lambda_candidates),
        n_simulations=n_simulations, warmup_games=warmup_games, seed=seed,
        game_ids=game_ids, datasets=datasets,
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
