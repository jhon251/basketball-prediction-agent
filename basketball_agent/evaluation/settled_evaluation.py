"""Read-only historical evaluation & error analysis (Phase 10-A).

Consumes the existing ``predict → save → settle → aggregate`` results
(``SettlementStore``) plus the frozen walk-forward engine for per-model
evaluation.  This layer is a PURE CONSUMER:

* it never writes to the database or mutates any frozen configuration;
* every slice reports its sample count; slices below ``MIN_SLICE_SAMPLES``
  are flagged ``sufficient=False`` and must not be interpreted as stable
  patterns (guard tested);
* per-model (Elo/Strength/Simulation) evaluation reuses the frozen
  ``window-backtest`` engine — this layer adds slicing, not models;
* model-disagreement facts are NOT stored per settled prediction — the
  analyzer reports them as unavailable rather than fabricating them;
* all metrics come from the frozen core's own functions
  (``evaluation.metrics`` / ``calibration``);
* evaluation windows are explicit inputs; nothing reads "now".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from basketball_agent.data.db.connection import Database
from basketball_agent.evaluation.calibration import (
    build_reliability_table,
    expected_calibration_error,
)
from basketball_agent.evaluation.metrics import accuracy, brier_score, log_loss
from basketball_agent.evaluation.settlement import SettlementStore

#: slices with fewer settled samples are descriptive-only (Phase 10-A rule)
MIN_SLICE_SAMPLES = 30

#: fixed probability confidence bands (documented definitions, reused by
#: the tossup indicator's (0.45, 0.55) neighbourhood)
CONFIDENCE_BANDS = (
    ("p_lt_0.40", 0.0, 0.40),
    ("p_0.40_to_0.60", 0.40, 0.60),
    ("p_0.60_to_0.80", 0.60, 0.80),
    ("p_ge_0.80", 0.80, 1.01),
)
TOSSUP_BAND = (0.45, 0.55)  # same fixed definition as 9-A/9-B


@dataclass(frozen=True)
class EvaluationFilter:
    """Explicit, data-driven evaluation scope.  ``window_end`` is a
    historical cutoff — records settled/as-of after it are excluded."""

    league: str | None = None
    season: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None    # historical cutoff (exclusive)


@dataclass(frozen=True)
class SliceMetrics:
    """Metrics for one slice.  ``sufficient=False`` slices are
    descriptive-only (small-sample guard)."""

    n: int
    accuracy: float | None = None
    log_loss: float | None = None
    brier: float | None = None
    ece: float | None = None
    margin_mae: float | None = None
    total_mae: float | None = None
    sufficient: bool = False
    note: str | None = None


EMPTY_SLICE = SliceMetrics(n=0, note="no settled records in this slice")


def _metrics(probabilities: list[float], outcomes: list[bool],
             margins: list[tuple[float | None, float]],
             totals: list[tuple[float | None, float]]) -> SliceMetrics:
    n = len(outcomes)
    if n == 0:
        return EMPTY_SLICE
    sufficient = n >= MIN_SLICE_SAMPLES
    note = None if sufficient else (
        f"insufficient sample (n={n} < {MIN_SLICE_SAMPLES}): descriptive "
        "only — do not interpret as a stable pattern")
    margin_pairs = [(p, a) for p, a in margins if p is not None]
    total_pairs = [(p, a) for p, a in totals if p is not None]
    ece = expected_calibration_error(
        build_reliability_table(probabilities, outcomes))
    return SliceMetrics(
        n=n,
        accuracy=round(accuracy(outcomes, probabilities), 4),
        log_loss=round(log_loss(outcomes, probabilities), 4),
        brier=round(brier_score(outcomes, probabilities), 4),
        ece=round(ece, 4),
        margin_mae=(round(sum(abs(p - a) for p, a in margin_pairs)
                          / len(margin_pairs), 4) if margin_pairs else None),
        total_mae=(round(sum(abs(p - a) for p, a in total_pairs)
                         / len(total_pairs), 4) if total_pairs else None),
        sufficient=sufficient,
        note=note,
    )


class SettlementEvaluator:
    """Read-only evaluation over the settled-prediction store."""

    def __init__(self, db: Database) -> None:
        self._store = SettlementStore(db)

    def _scoped(self, flt: EvaluationFilter) -> list[dict]:
        """Settled records joined with their game's league/season (read-only)."""
        records = []
        for s in self._store.list_settled():
            meta = self._game_meta(s.game_id)
            if flt.league is not None and meta["league"] != flt.league:
                continue
            if flt.season is not None and meta["season"] != flt.season:
                continue
            if flt.window_start is not None and s.as_of < flt.window_start:
                continue
            if flt.window_end is not None and s.as_of >= flt.window_end:
                continue
            records.append({**meta, "settled": s})
        return records

    def _game_meta(self, game_id: str) -> dict:
        with self._store._db.connect() as conn:
            row = conn.execute(
                "SELECT league, season FROM games WHERE game_id = ?",
                (game_id,)).fetchone()
        if row is None:
            return {"league": None, "season": None}
        return {"league": row["league"], "season": row["season"]}

    @staticmethod
    def _facts(records: list[dict]):
        probabilities = [r["settled"].prob_home for r in records]
        outcomes = [r["settled"].actual_home_score >
                    r["settled"].actual_away_score for r in records]
        margins = [(r["settled"].expected_margin,
                    float(r["settled"].actual_home_score
                          - r["settled"].actual_away_score))
                   for r in records]
        totals = [(r["settled"].expected_total,
                   float(r["settled"].actual_home_score
                         + r["settled"].actual_away_score))
                  for r in records]
        return probabilities, outcomes, margins, totals

    def evaluate(self, flt: EvaluationFilter | None = None) -> SliceMetrics:
        """Overall metrics for the filtered scope."""
        records = self._scoped(flt or EvaluationFilter())
        p, o, m, t = self._facts(records)
        return _metrics(p, o, m, t)

    def evaluate_by_league(self, flt: EvaluationFilter | None = None) -> dict[str, SliceMetrics]:
        records = self._scoped(flt or EvaluationFilter())
        out: dict[str, SliceMetrics] = {}
        by_league: dict[str, list[dict]] = {}
        for r in records:
            by_league.setdefault(r["league"] or "unknown", []).append(r)
        for league in sorted(by_league):
            p, o, m, t = self._facts(by_league[league])
            out[league] = _metrics(p, o, m, t)
        return out

    def evaluate_by_season(self, flt: EvaluationFilter | None = None) -> dict[str, SliceMetrics]:
        records = self._scoped(flt or EvaluationFilter())
        out: dict[str, SliceMetrics] = {}
        by_season: dict[str, list[dict]] = {}
        for r in records:
            by_season.setdefault(r["season"] or "unknown", []).append(r)
        for season in sorted(by_season):
            p, o, m, t = self._facts(by_season[season])
            out[season] = _metrics(p, o, m, t)
        return out

    # ---------------------------------------------------------------- slices
    def error_slices(self, flt: EvaluationFilter | None = None) -> dict[str, SliceMetrics]:
        """Fixed, documented error-analysis slices (tossup + confidence
        bands).  Every slice carries its sample count and the small-sample
        guard."""
        records = self._scoped(flt or EvaluationFilter())
        out: dict[str, SliceMetrics] = {}
        buckets: dict[str, list[dict]] = {name: [] for name, _, _ in CONFIDENCE_BANDS}
        for r in records:
            p = r["settled"].prob_home
            lo, hi = TOSSUP_BAND
            in_tossup = lo <= p <= hi
            if in_tossup:
                buckets.setdefault("tossup_0.45_to_0.55", []).append(r)
            for name, blo, bhi in CONFIDENCE_BANDS:
                if blo <= p < bhi:
                    buckets[name].append(r)
        for name in sorted(buckets):
            p, o, m, t = self._facts(buckets[name])
            out[name] = _metrics(p, o, m, t)
        return out

    def calibration_drift(self, flt: EvaluationFilter | None = None) -> dict[str, SliceMetrics]:
        """Reliability per settlement-time half-year bucket (fixed, explicit
        windows only — drift facts, not a recalibration)."""
        records = self._scoped(flt or EvaluationFilter())
        buckets: dict[str, list[dict]] = {}
        for r in records:
            key = f"{r['settled'].as_of.year}-H{1 if r['settled'].as_of.month <= 6 else 2}"
            buckets.setdefault(key, []).append(r)
        out: dict[str, SliceMetrics] = {}
        for key in sorted(buckets):
            p, o, m, t = self._facts(buckets[key])
            out[key] = _metrics(p, o, m, t)
        return out

    def model_disagreement_slices(self, flt: EvaluationFilter | None = None) -> dict:
        """Model-disagreement analysis is NOT available over settled
        predictions (the store holds only the primary probability).  This
        layer reports that fact instead of fabricating slices."""
        n = len(self._scoped(flt or EvaluationFilter()))
        return {
            "available": False,
            "reason": ("settled predictions store only the primary "
                       "probability; per-model probabilities live in the "
                       "frozen walk-forward engine — use window-backtest "
                       "for per-model and disagreement analysis"),
            "n_records": n,
        }


def per_model_evaluation(repo, window_start: datetime, window_end: datetime,
                         league=None, n_simulations: int = 100,
                         seed: int = 7) -> dict:
    """Per-model (random/elo/strength/simulation) evaluation for one
    explicit window — a thin read-only wrapper around the FROZEN
    ``run_window_backtest`` engine (train_seasons=None: frozen defaults,
    no fitting).  Metrics come straight from the engine."""
    from basketball_agent.evaluation.experiments import run_window_backtest

    results = run_window_backtest(
        repo, window_start, window_end,
        train_seasons=None, n_simulations=n_simulations, seed=seed,
        league=league)
    return {name: {k: metrics[k] for k in
                   ("n_win_prob", "accuracy", "log_loss", "brier_score",
                    "ece", "margin_mae", "total_mae") if k in metrics}
            for name, metrics in results["models"].items()}
