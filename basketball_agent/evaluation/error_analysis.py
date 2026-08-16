"""Error analysis (spec §20): where does the model fail?

Groups test-window predictions by scenario and reports per-group accuracy /
log loss / Brier, so the next step is evidence-driven instead of another
agent added on faith.
"""
from __future__ import annotations

from basketball_agent.data.db.repository import Repository
from basketball_agent.evaluation.backtest import BacktestResult
from basketball_agent.evaluation.metrics import accuracy, brier_score, log_loss


def error_analysis(result: BacktestResult, repo: Repository) -> dict[str, dict]:
    rows = [
        r for r in result.records
        if r.prediction is not None and r.actual_home_win is not None
        and r.actual_margin is not None and r.actual_total is not None
    ]
    if not rows:
        return {}

    total_median = sorted(r.actual_total for r in rows)[len(rows) // 2]

    def group(name: str, predicate) -> dict:
        subset = [r for r in rows if predicate(r)]
        if not subset:
            return {"n": 0, "accuracy": None, "log_loss": None, "brier": None}
        probs = [r.prediction.prob_home for r in subset]
        outcomes = [r.actual_home_win for r in subset]
        return {
            "n": len(subset),
            "accuracy": accuracy(outcomes, probs),
            "log_loss": log_loss(outcomes, probs),
            "brier": brier_score(outcomes, probs),
        }

    game_dates = {g.game_id: g for g in repo.list_games()}
    seasons = sorted({game_dates[r.game_id].season for r in rows if r.game_id in game_dates})

    groups: dict[str, dict] = {
        "home_favorites (p>=0.5)": group("hf", lambda r: r.prediction.prob_home >= 0.5),
        "away_favorites (p<0.5)": group("af", lambda r: r.prediction.prob_home < 0.5),
        "confident (p>=0.65 or p<=0.35)": group(
            "conf", lambda r: r.prediction.prob_home >= 0.65 or r.prediction.prob_home <= 0.35
        ),
        "tossups (0.45<p<0.55)": group(
            "toss", lambda r: 0.45 < r.prediction.prob_home < 0.55
        ),
        "close games (|margin|<=5)": group("close", lambda r: abs(r.actual_margin) <= 5),
        "blowouts (|margin|>=15)": group("blow", lambda r: abs(r.actual_margin) >= 15),
        "high total (>=median)": group("hitot", lambda r: r.actual_total >= total_median),
        "low total (<median)": group("lotot", lambda r: r.actual_total < total_median),
        "home teams": group("home", lambda r: True),  # home team's perspective
    }
    # home/away split uses the model's view of the home side; away is its complement
    groups["away teams"] = {
        "n": groups["home teams"]["n"],
        "accuracy": 1.0 - groups["home teams"]["accuracy"]
        if groups["home teams"]["accuracy"] is not None else None,
        "log_loss": groups["home teams"]["log_loss"],
        "brier": groups["home teams"]["brier"],
    }
    for season in seasons:
        groups[f"season {season}"] = group(
            f"s{season}", lambda r, s=season: game_dates.get(r.game_id).season == s
        )
    return groups
