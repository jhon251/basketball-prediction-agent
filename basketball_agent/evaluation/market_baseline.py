"""Market baseline backtest (Phase 11-A.2, hardened in 11-A.3).

Runs the frozen walk-forward loop (identical to run_window_backtest) and
joins the per-game model probabilities with stored closing odds to
produce market-baseline metrics alongside the frozen model metrics.

Sample discipline (Phase 11-A.3):

* ALL rows (model and market) are computed on the SAME subset of games —
  those that have both a model probability and a closing-odds pair.
  This is the ``subset=with_odds`` set and is the ONLY comparison that
  is fair across models and market.
* A ``subset=all`` reference row is also emitted for each model (using
  the full walk-forward window, not just the odds intersection) so the
  reader can see how much the odds coverage restriction shifts each
  model's metrics.  ``subset=all`` rows are REFERENCE ONLY — they must
  not be compared against ``subset=with_odds`` rows.

Evidence kind: ``backtest_with_market_baseline``.  No model is re-run;
no parameter is fitted; no odds value enters any feature.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import (
    NON_REGULAR_COMPETITIONS,
    GameStatus,
    League,
)
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    EloPredictor,
    RandomPredictor,
    SimulationPredictor,
    StrengthPredictor,
    warmup_not_met,
)
from basketball_agent.features.feature_pipeline import (
    FeatureComputationError,
    FeaturePipeline,
)
from basketball_agent.models.game_params import build_game_params


def _ll(probs: list[float], outs: list[bool]) -> float | None:
    if not outs:
        return None
    return round(-sum(
        (1.0 if o else 0.0) * math.log(max(p, 1e-12))
        + (0.0 if o else 1.0) * math.log(max(1.0 - p, 1e-12))
        for p, o in zip(probs, outs)) / len(outs), 4)


def _acc(probs: list[float], outs: list[bool]) -> float | None:
    if not outs:
        return None
    return round(sum((p >= 0.5) == o for p, o in zip(probs, outs))
                 / len(outs), 4)


def _brier(probs: list[float], outs: list[bool]) -> float | None:
    if not outs:
        return None
    return round(sum((p - (1.0 if o else 0.0)) ** 2
                     for p, o in zip(probs, outs)) / len(outs), 4)


# ---------------------------------------------------------------------------
# odds loading


class OddsDataUnavailable(RuntimeError):
    """Raised when the odds table is missing or cannot be queried."""


def load_odds_map(db) -> dict[str, tuple[int, int]]:
    """Load CLOSE MONEYLINE odds: game_id -> (am_home, am_away).

    Raises ``OddsDataUnavailable`` if the table is missing or unreadable
    (fail-loud, never silently return an empty map).
    """
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT game_id, price_home, price_away FROM game_odds "
                "WHERE market = 'MONEYLINE' AND kind = 'CLOSE' "
                "AND price_home IS NOT NULL AND price_away IS NOT NULL"
            ).fetchall()
        return {r["game_id"]: (int(r["price_home"]), int(r["price_away"]))
                for r in rows}
    except Exception as exc:
        if "no such table" in str(exc).lower():
            raise OddsDataUnavailable(
                "odds table does not exist — run odds ingest first") from exc
        raise


# ---------------------------------------------------------------------------
# market baseline backtest


@dataclass(frozen=True)
class BaselineRow:
    """One model or market metric row on a specific subset."""

    model: str
    subset: str                     # "with_odds" or "all"
    n_games: int
    n_with_odds: int
    log_loss: float | None
    brier: float | None
    accuracy: float | None


def market_baseline_backtest(
    db,
    window_start: datetime,
    window_end: datetime | None,
    *,
    league: League | None = None,
    n_simulations: int = 100,
    seed: int = 7,
    warmup_games: int = 5,
) -> list[BaselineRow]:
    """Frozen walk-forward + closing-odds market baseline.

    Returns ``BaselineRow`` objects on TWO subsets:
      subset=with_odds  — the fair comparison set (games with odds)
      subset=all        — the full walk-forward window (reference only)
    """
    from basketball_agent.evaluation.devig import moneyline_pair_to_fair
    from basketball_agent.evaluation.settlement import SettlementStore

    store = SettlementStore(db)
    odds_map = load_odds_map(db)

    config = BacktestConfig(
        n_simulations=n_simulations, seed=seed, warmup_games=warmup_games)
    from basketball_agent.data.db.repository import Repository

    repo = Repository(db)
    pipeline = FeaturePipeline(repo)
    predictors = {
        "random": RandomPredictor(),
        "elo": EloPredictor(),
        "strength": StrengthPredictor(),
        "simulation": SimulationPredictor(
            n_simulations=n_simulations, seed=seed),
    }

    games = [
        g for g in Repository(db).list_games(
            status=GameStatus.FINAL, league=league,
            exclude_competitions=tuple(NON_REGULAR_COMPETITIONS))
        if g.game_date >= window_start
        and (window_end is None or g.game_date < window_end)
    ]
    games.sort(key=lambda g: (g.game_date, g.game_id))

    # collect per-game entries
    entries: list[dict] = []
    for g in games:
        as_of = g.game_date
        if warmup_not_met(repo, g, as_of, warmup_games):
            continue
        try:
            features = pipeline.compute(g.game_id, as_of)
        except FeatureComputationError:
            continue
        outcome = g.home_score > g.away_score
        entry = {
            "game_id": g.game_id, "season": g.season,
            "outcome": outcome,
            "p_random": 0.5,
        }
        for model in ("elo", "strength", "simulation"):
            pred = predictors[model].predict(features)
            entry[f"p_{model}"] = pred.prob_home
        # market fair probability from stored odds
        pair = odds_map.get(g.game_id)
        if pair is not None:
            fh, _, _ = moneyline_pair_to_fair(int(pair[0]), int(pair[1]))
            entry["p_market"] = fh
        entries.append(entry)

    if not entries:
        return []

    models = ("random", "elo", "strength", "simulation", "market")
    rows: list[BaselineRow] = []

    # subset=with_odds: only games that have a market probability
    with_odds = [e for e in entries if "p_market" in e]
    # subset=all: the full walk-forward window
    for subset_name, subset in (("with_odds", with_odds), ("all", entries)):
        for model in models:
            probs: list[float] = []
            outs: list[bool] = []
            for e in subset:
                p = e.get(f"p_{model}")
                if p is not None:
                    probs.append(p)
                    outs.append(e["outcome"])
            n_odds = sum(1 for e in subset if "p_market" in e)
            rows.append(BaselineRow(
                model=model, subset=subset_name, n_games=len(outs),
                n_with_odds=n_odds,
                log_loss=_ll(probs, outs), brier=_brier(probs, outs),
                accuracy=_acc(probs, outs)))

    return rows


def db_games(db, league=None):
    """Yield FINAL REGULAR_SEASON games (same filters as run_window_backtest)."""
    return Repository(db).list_games(
        status=GameStatus.FINAL, league=league,
        exclude_competitions=tuple(NON_REGULAR_COMPETITIONS))
