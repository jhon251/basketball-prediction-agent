"""Phase 8-I analysis: cross-era stability + model independence (READ-ONLY).

Mirrors ``run_window_backtest``'s single-pass loop exactly (same predictors,
same frozen defaults, same as-of feature discipline) but keeps every
per-game record so we can segment by era and quantify how much independent
information each predictor carries.  No parameters are fitted, no defaults
are touched, nothing is written to the package or storage.

Outputs one JSON per league under ``.tmp/8i_<league>.json`` with:
* per-season metrics for random/elo/strength/simulation
  (acc/logloss/brier/ECE + margin/total MAE where defined);
* pairwise Pearson correlations of home-win probabilities;
* three-model direction agreement rate and disagreement rate;
* per-model logloss/accuracy on the disagreement subset;
* pairwise error overlap (joint-wrong share of each model's errors);
* an equal-weight ensemble (mean of the three probabilities — no fitting,
  therefore no leakage) vs the best individual model.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basketball_agent.config import get_settings
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import (
    Competition,
    GameStatus,
    League,
    NON_REGULAR_COMPETITIONS,
)
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    EloPredictor,
    RandomPredictor,
    SimulationPredictor,
    StrengthPredictor,
    warmup_not_met,
)
from basketball_agent.evaluation.experiments import run_window_backtest  # noqa: F401 (parity reference)
from basketball_agent.features.feature_pipeline import (
    FeatureComputationError,
    FeaturePipeline,
)
from basketball_agent.models.win_probability import WinProbabilityConfig
from basketball_agent.evaluation.metrics import accuracy, brier_score, log_loss


def _ece(probs: list[float], outs: list[bool], bins: int = 10) -> float:
    if not probs:
        return 0.0
    total = len(probs)
    e = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        bucket = [(p, o) for p, o in zip(probs, outs) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if not bucket:
            continue
        conf = sum(p for p, _ in bucket) / len(bucket)
        rate = sum(o for _, o in bucket) / len(bucket)
        e += len(bucket) / total * abs(conf - rate)
    return e


def _metrics(probs: list[float], outs: list[bool],
             margins: list[tuple[float, float]] | None) -> dict:
    n = len(outs)
    if n == 0:
        return {"n": 0}
    out: dict = {
        "n": n,
        "acc": round(accuracy(outs, probs), 4),
        "logloss": round(log_loss(outs, probs), 4),
        "brier": round(brier_score(outs, probs), 4),
        "ece": round(_ece(probs, outs), 4),
    }
    if margins is not None:
        pm = [m for m, _ in margins if m is not None]
        am = [a for _, a in margins if a is not None]
        pt = [t for t, _ in margins if t is not None] if False else None
        if pm:
            out["margin_mae"] = round(sum(abs(p - a) for p, a in zip(pm, am)) / len(pm), 2)
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (vx * vy) if vx and vy else float("nan")


def _ll(probs: list[float], outs: list[bool]) -> float:
    eps = 1e-12
    return -sum(
        (o * math.log(max(p, eps)) + (1 - o) * math.log(max(1 - p, eps)))
        for p, o in zip(probs, outs)) / len(outs)


def _acc(probs: list[float], outs: list[bool]) -> float:
    return sum((p >= 0.5) == o for p, o in zip(probs, outs)) / len(outs)


def analyze(records: list[dict]) -> dict:
    models = ("elo", "strength", "simulation")
    # ---------- per-season era metrics
    seasons: dict[str, list[dict]] = {}
    for r in records:
        seasons.setdefault(r["season"], []).append(r)
    era_table = {}
    for season in sorted(seasons):
        rows = seasons[season]
        outs = [r["home_win"] for r in rows]
        margins = [(r["margin"], r["total"]) for r in rows]
        entry: dict = {"random": None}
        entry["elo"] = _metrics([r["p_elo"] for r in rows], outs, None)
        entry["strength"] = _metrics([r["p_strength"] for r in rows], outs, None)
        entry["simulation"] = _metrics([r["p_simulation"] for r in rows], outs, None)
        # margin/total MAE from strength & sim expected values
        for m in ("strength", "simulation"):
            mm = [(r[f"margin_{m}"], r["margin"]) for r in rows
                  if r[f"margin_{m}"] is not None]
            tt = [(r[f"total_{m}"], r["total"]) for r in rows
                  if r[f"total_{m}"] is not None]
            if mm:
                entry[m]["margin_mae"] = round(
                    sum(abs(p - a) for p, a in mm) / len(mm), 2)
            if tt:
                entry[m]["total_mae"] = round(
                    sum(abs(p - a) for p, a in tt) / len(tt), 2)
        # random (0.5) reference
        entry["random"] = {
            "n": len(rows),
            "acc": round(sum(o is False for o in outs) / len(outs), 4),
            "logloss": 0.6931, "brier": 0.25,
        }
        era_table[season] = entry

    # ---------- independence on the full window
    outs = [r["home_win"] for r in records]
    P = {m: [r[f"p_{m}"] for r in records] for m in models}
    cor = {f"{a}_{b}": round(_pearson(P[a], P[b]), 4)
           for a, b in (("elo", "strength"), ("elo", "simulation"),
                        ("strength", "simulation"))}

    agree = sum(
        (P["elo"][i] >= 0.5) == (P["strength"][i] >= 0.5) == (P["simulation"][i] >= 0.5)
        for i in range(len(outs)))
    direction_agreement = round(agree / len(outs), 4)
    dis_idx = [i for i in range(len(outs))
               if not ((P["elo"][i] >= 0.5) == (P["strength"][i] >= 0.5)
                       and (P["elo"][i] >= 0.5) == (P["simulation"][i] >= 0.5))]
    disagreement = {
        "n": len(dis_idx),
        "rate": round(len(dis_idx) / len(outs), 4) if outs else None,
        "subset": {
            m: {"logloss": round(_ll([P[m][i] for i in dis_idx],
                                     [outs[i] for i in dis_idx]), 4),
                "acc": round(_acc([P[m][i] for i in dis_idx],
                                  [outs[i] for i in dis_idx]), 4)}
            for m in models} if len(dis_idx) >= 20 else "insufficient (<20)",
    }

    wrong = {m: {i for i in range(len(outs)) if (P[m][i] >= 0.5) != outs[i]}
             for m in models}
    overlap = {}
    for a, b in (("elo", "strength"), ("elo", "simulation"),
                 ("strength", "simulation")):
        inter = len(wrong[a] & wrong[b])
        overlap[f"{a}_{b}"] = {
            "joint_wrong": inter,
            f"share_of_{a}_errors": round(inter / len(wrong[a]), 4) if wrong[a] else None,
            f"share_of_{b}_errors": round(inter / len(wrong[b]), 4) if wrong[b] else None,
            "jaccard": round(inter / len(wrong[a] | wrong[b]), 4)
            if (wrong[a] | wrong[b]) else None,
        }
    all3_wrong = len(wrong["elo"] & wrong["strength"] & wrong["simulation"])
    any_unique = {
        m: len(wrong[m] - set().union(*(wrong[o] for o in models if o != m)))
        for m in models}

    # ---------- equal-weight ensemble (no fitting -> no leakage)
    ens = [round((P["elo"][i] + P["strength"][i] + P["simulation"][i]) / 3, 6)
           for i in range(len(outs))]
    best_single = min(models, key=lambda m: _ll(P[m], outs))
    ensemble = {
        "definition": "equal-weight mean of elo/strength/simulation probs",
        "logloss": round(_ll(ens, outs), 4),
        "acc": round(_acc(ens, outs), 4),
        "best_single_model": best_single,
        "best_single_logloss": round(_ll(P[best_single], outs), 4),
        "delta_vs_best_single": round(
            _ll(P[best_single], outs) - _ll(ens, outs), 4),
    }

    return {
        "n_games": len(records),
        "eras": era_table,
        "prob_correlation_pearson": cor,
        "direction_agreement_rate": direction_agreement,
        "disagreement": disagreement,
        "error_overlap": overlap,
        "all_three_wrong": all3_wrong,
        "unique_errors": any_unique,
        "ensemble_equal_weight": ensemble,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--window-start", required=True)
    ap.add_argument("--window-end", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-simulations", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--warmup", type=int, default=5)
    args = ap.parse_args()

    settings = get_settings()
    db = Database(settings.db_path)
    db.initialize()
    repo = Repository(db)
    league = League(args.league)
    window_start = datetime.fromisoformat(args.window_start).replace(tzinfo=timezone.utc)
    window_end = (datetime.fromisoformat(args.window_end).replace(tzinfo=timezone.utc)
                  if args.window_end else None)

    games = [
        g for g in repo.list_games(
            status=GameStatus.FINAL, league=league,
            exclude_competitions=tuple(NON_REGULAR_COMPETITIONS))
        if g.game_date >= window_start and (window_end is None or g.game_date < window_end)
    ]
    games.sort(key=lambda g: (g.game_date, g.game_id))
    print(f"{league.value}: {len(games)} regular FINAL games in window")

    config = BacktestConfig(
        n_simulations=args.n_simulations, seed=args.seed,
        warmup_games=args.warmup)
    pipeline = FeaturePipeline(repo)
    predictors = {
        "random": RandomPredictor(),
        "elo": EloPredictor(),
        "strength": StrengthPredictor(),
        "simulation": SimulationPredictor(
            n_simulations=args.n_simulations, seed=args.seed),
    }

    records: list[dict] = []
    for i, g in enumerate(games, 1):
        as_of = g.game_date
        if warmup_not_met(repo, g, as_of, args.warmup):
            continue
        try:
            features = pipeline.compute(g.game_id, as_of)
        except FeatureComputationError:
            continue
        row = {
            "game_id": g.game_id,
            "date": as_of.date().isoformat(),
            "season": g.season,
            "home_win": g.home_score > g.away_score,
            "margin": float(g.home_score - g.away_score),
            "total": float(g.home_score + g.away_score),
        }
        for name, predictor in predictors.items():
            if name == "random":
                continue
            p = predictor.predict(features)
            row[f"p_{name}"] = round(p.prob_home, 6)
            row[f"margin_{name}"] = p.expected_margin
            row[f"total_{name}"] = p.expected_total
        records.append(row)
        if i % 500 == 0:
            print(f"  {i}/{len(games)}")

    out = {"league": league.value, "window_start": args.window_start,
           "window_end": args.window_end or "end of data",
           "analysis": analyze(records), "records": records}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    a = out["analysis"]
    print("eras:", {k: v["n_games"] if "n_games" in v else v["elo"]["n"]
                    for k, v in a["eras"].items()})
    print("corr:", a["prob_correlation_pearson"])
    print("direction_agreement:", a["direction_agreement_rate"],
          "| disagreement n:", a["disagreement"]["n"])
    print("ensemble:", a["ensemble_equal_weight"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
