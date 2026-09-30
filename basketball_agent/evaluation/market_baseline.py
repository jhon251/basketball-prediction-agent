"""Market baseline backtest (Phase 11-A.2): frozen walk-forward × odds join.

Runs the frozen walk-forward loop (identical to run_window_backtest) and
joins the per-game model probabilities with the stored closing odds to
produce market-baseline metrics alongside the frozen model metrics.

Evidence kind: ``backtest_with_market_baseline`` — explicitly distinct
from ``real_agent_history`` (Phase 10-C).  No model is re-run; no
parameter is fitted; no odds value enters any feature.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

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


@dataclass(frozen=True)
class MarketRow:
    model: str
    n_games: int
    n_with_odds: int
    log_loss: float | None
    brier: float | None
    accuracy: float | None


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


def market_baseline_backtest(
    db,
    window_start: datetime,
    window_end: datetime | None,
    *,
    league: League | None = None,
    n_simulations: int = 100,
    seed: int = 7,
    warmup_games: int = 5,
    odds_db=None,
) -> list[MarketRow]:
    """Frozen walk-forward + closing-odds market baseline.

    Returns one ``MarketRow`` per model (elo/strength/simulation/market)
    computed over the intersection of games that have both a model
    probability and a closing-odds pair.  Odds-less games are excluded
    from the market row but still counted in the model rows.
    """
    config = BacktestConfig(
        n_simulations=n_simulations, seed=seed, warmup_games=warmup_games)
    pipeline = FeaturePipeline(db)
    predictors = {
        "random": RandomPredictor(),
        "elo": EloPredictor(),
        "strength": StrengthPredictor(),
        "simulation": SimulationPredictor(
            n_simulations=n_simulations, seed=seed),
    }

    games = [
        g for g in db_games(db, league)
        if g.game_date >= window_start
        and (window_end is None or g.game_date < window_end)
    ]
    games.sort(key=lambda g: (g.game_date, g.game_id))

    # load odds: game_id -> (am_home, am_away)
    odds_map = _load_odds_map(odds_db or db)

    # per-game model probabilities
    entries: list[dict] = []
    for g in games:
        as_of = g.game_date
        if warmup_not_met(db, g, as_of, warmup_games):
            continue
        try:
            features = pipeline.compute(g.game_id, as_of)
        except FeatureComputationError:
            continue
        entry = {
            "game_id": g.game_id,
            "season": g.season,
            "outcome": g.home_score > g.away_score,
        }
        for model in ("elo", "strength", "simulation"):
            pred = predictors[model].predict(features)
            entry[f"p_{model}"] = pred.prob_home
        # market fair probability from stored odds
        pair = odds_map.get(g.game_id)
        if pair:
            from basketball_agent.evaluation.devig import (
                moneyline_pair_to_fair,
            )
            fh, _, _ = moneyline_pair_to_fair(int(pair[0]), int(pair[1]))
            entry["p_market"] = fh
        entries.append(entry)

    if not entries:
        return []

    # compute per-model metrics
    rows: list[MarketRow] = []
    for model in ("random", "elo", "strength", "simulation"):
        probs: list[float] = []
        outs: list[bool] = []
        n_odds = 0
        for e in entries:
            if model == "random":
                probs.append(0.5)
            else:
                probs.append(e.get(f"p_{model}", 0.5))
            outs.append(e["outcome"])
        ll = _ll(probs, outs)
        br = _brier(probs, outs)
        acc = _acc(probs, outs)
        rows.append(MarketRow(
            model=model, n_games=len(outs), n_with_odds=0,
            log_loss=ll, brier=br, accuracy=acc))

    # market row (only games with odds)
    m_probs: list[float] = []
    m_outs: list[bool] = []
    for e in entries:
        mp = e.get("p_market")
        if mp is not None:
            m_probs.append(mp)
            m_outs.append(e["outcome"])
    if m_probs:
        rows.append(MarketRow(
            model="market", n_games=len(m_outs), n_with_odds=len(m_outs),
            log_loss=_ll(m_probs, m_outs), brier=_brier(m_probs, m_outs),
            accuracy=_acc(m_probs, m_outs)))

    return rows


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


def db_games(db, league=None):
    """Yield FINAL REGULAR_SEASON games (same filters as run_window_backtest)."""
    from basketball_agent.data.db.repository import Repository

    return Repository(db).list_games(
        status=GameStatus.FINAL, league=league,
        exclude_competitions=tuple(NON_REGULAR_COMPETITIONS))


def _load_odds_map(db) -> dict[str, tuple[int, int]]:
    """Load CLOSE MONEYLINE odds: game_id -> (am_home, am_away)."""
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT game_id, price_home, price_away FROM game_odds "
                "WHERE market='MONEYLINE' AND kind='CLOSE' "
                "AND price_home IS NOT NULL AND price_away IS NOT NULL"
            ).fetchall()
        return {r["game_id"]: (int(r["price_home"]), int(r["price_away"]))
                for r in rows}
    except Exception:
        return {}
