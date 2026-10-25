"""Spread-probability calibration experiment (Phase 12-E).

Question: which provider should back the API's 让分 probabilities —
the simulation family's margin samples, or the Elo win probability
mapped through a fitted normal (``agents/spread_probability``)?

Method (honest-evaluation rules, spec §24):
* strict walk-forward — every game is predicted strictly as-of the day
  before its kickoff, on the frozen default parameters;
* no bookmaker lines exist in the data (all odds sources auth-blocked),
  so each game is probed on a grid of lines centred on the model's OWN
  Elo-implied expected margin — this measures whether each provider's
  margin DISTRIBUTION SHAPE is calibrated (centre accuracy is already
  covered by the win-probability logloss in MULTI_SEASON_RESULTS);
* sigma for the Elo provider is fitted once on games strictly before the
  evaluation window (walk-forward safe);
* report log loss / Brier for both providers plus a constant-p baseline.

This module is an experiment driver — it never touches frozen models.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.spread_probability import (
    cover_from_elo_normal,
    cover_from_samples,
    elo_expected_margin,
    fit_margin_sigma,
)

LINE_OFFSETS: tuple[float, ...] = (-10.0, -6.0, -3.0, -1.0, 0.0,
                                   1.0, 3.0, 6.0, 10.0)
_EPS = 1e-6


@dataclass
class CalibrationCounts:
    n_predictions: int = 0
    n_home_covers: int = 0
    logloss: dict[str, float] | None = None

    def finish(self) -> dict:
        assert self.logloss is not None
        n = max(self.n_predictions, 1)
        out = {"n": self.n_predictions,
               "home_cover_rate": self.n_home_covers / n}
        for k, v in self.logloss.items():
            out[k] = v / n
        return out


def _brier(p: float, y: int) -> float:
    return (p - y) ** 2


def evaluate_spread_calibration(
    repo,
    *,
    league: str = "NBA",
    season: str = "2024-25",
    n_simulations: int = 200,
    seed: int = 7,
    line_offsets: tuple[float, ...] = LINE_OFFSETS,
    season_start: str = "2024-10-01",
    sigma_history_end: str = "2024-06-30",
    progress_every: int = 200,
) -> dict:
    """Run the experiment on the real DB; returns the metrics dict."""
    from basketball_agent.data.schemas.base import (
        Competition, GameStatus, League,
    )

    league_enum = League(league)
    cutoff = _dt(sigma_history_end + "T00:00:00+00:00")
    hist_margins = [
        g.home_score - g.away_score
        for g in repo.list_games(as_of=cutoff, status=GameStatus.FINAL,
                                 competition=Competition.REGULAR_SEASON)
        if g.league is league_enum
        and g.home_score is not None and g.away_score is not None
    ]
    sigma = fit_margin_sigma(hist_margins)

    window_start = _dt(season_start + "T00:00:00+00:00")
    window_end = window_start + timedelta(days=300)
    games = [
        g for g in repo.list_games(status=GameStatus.FINAL,
                                   league=league_enum,
                                   competition=Competition.REGULAR_SEASON)
        if window_start <= g.game_date <= window_end
        and g.home_score is not None and g.away_score is not None
    ]
    games.sort(key=lambda g: g.game_date)

    agent = PredictionAgent(repo, n_simulations=n_simulations, seed=seed)
    ll = {"simulation_samples": 0.0, "elo_normal": 0.0, "constant_p": 0.0}
    br = {"simulation_samples": 0.0, "elo_normal": 0.0, "constant_p": 0.0}
    n_pred = 0
    n_home = 0
    skipped = 0
    for i, g in enumerate(games):
        as_of = g.game_date - timedelta(days=1)
        try:
            pred = agent.predict(g.game_id, as_of=as_of)
        except Exception:  # noqa: BLE001 — insufficient history etc.
            skipped += 1
            continue
        p_elo = pred.features.elo_win_prob_home
        margins = [r.margin for r in pred.monte_carlo.runs]
        if not margins or not (0.0 < p_elo < 1.0):
            skipped += 1
            continue
        mu = elo_expected_margin(p_elo, sigma)
        actual = g.home_score - g.away_score
        for off in line_offsets:
            line = round((mu + off) * 2) / 2  # nearest half-point line
            if line == 0.0:
                continue
            y = 1 if actual + line > 0 else 0
            push = (actual + line == 0)
            if push:
                continue  # integer-line pushes are excluded (no stake)
            cs = cover_from_samples(margins, line)
            ce = cover_from_elo_normal(p_elo, sigma, line)
            p_base = 0.5
            ll["simulation_samples"] -= math.log(
                max(cs.home_cover if y else cs.away_cover, _EPS))
            ll["elo_normal"] -= math.log(
                max(ce.home_cover if y else ce.away_cover, _EPS))
            ll["constant_p"] -= math.log(max(p_base, _EPS))
            br["simulation_samples"] += _brier(cs.home_cover, y)
            br["elo_normal"] += _brier(ce.home_cover, y)
            br["constant_p"] += _brier(p_base, y)
            n_pred += 1
            n_home += y
        if progress_every and (i + 1) % progress_every == 0:
            print(f"  ... {i + 1}/{len(games)} games, {n_pred} line-probes")

    n = max(n_pred, 1)
    return {
        "league": league, "season": season,
        "n_games": len(games), "n_evaluated_games": len(games) - skipped,
        "skipped": skipped, "n_line_probes": n_pred,
        "sigma": sigma, "sigma_history_n": len(hist_margins),
        "home_cover_rate": n_home / n,
        "logloss": {k: v / n for k, v in ll.items()},
        "brier": {k: v / n for k, v in br.items()},
    }


def _dt(iso: str):
    from datetime import datetime

    return datetime.fromisoformat(iso)
