"""Decision Log evaluation (Phase 10-C): auditable evaluation over the
REAL Agent history recorded in the Phase 10-B append-only logs.

This is a PURE CONSUMER of the Decision Log + Settlement Log:

* prediction facts (probabilities, family, decision state, …) come from
  ``decisions.jsonl``; settlement facts (actual scores) come from
  ``settlements.jsonl``; the two are joined ONLY at evaluation time by
  ``record_id`` — the raw prediction payload is never modified;
* per-model evaluation uses the probabilities ALREADY STORED in the log
  (elo / strength / simulation / primary) — no model is re-run;
* slicing reuses the facts already in the log (decision_state,
  directional disagreement, dispersion band, tossup band);
* explicit historical cutoffs only; nothing reads the current time;
* every slice reports ``n`` and the ``MIN_SLICE_SAMPLES = 30`` guard —
  slices below the threshold are descriptive-only and must not be read
  as stable patterns.

Two evidence kinds exist in this system and are NEVER merged into one
statistic (Phase 10-C requirement 10):

* **backtest evidence** — the frozen walk-forward engine
  (``window-backtest`` / Phase 10-A ``per_model_evaluation``);
* **real Agent historical evidence** — this module, over the append-only
  Decision Log.
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.agents.decision_log import DecisionLog
from basketball_agent.evaluation.settled_evaluation import (
    MIN_SLICE_SAMPLES,
    SliceMetrics,
)

_MODELS = ("elo", "strength", "simulation")


@dataclass(frozen=True)
class LogEvaluationFilter:
    """Explicit scope.  ``window_end`` is an exclusive historical cutoff
    on the record's ``as_of``."""

    league: str | None = None
    season: str | None = None
    family: str | None = None            # primary family filter
    window_start: str | None = None
    window_end: str | None = None        # exclusive cutoff


def _metrics(n: int, probabilities: list[float], outcomes: list[bool],
             margins: list[tuple[float | None, float]],
             totals: list[tuple[float | None, float]]) -> SliceMetrics:
    if n == 0:
        return SliceMetrics(n=0, note="no records in this slice")
    from basketball_agent.evaluation.calibration import (
        build_reliability_table, expected_calibration_error,
    )
    from basketball_agent.evaluation.metrics import accuracy, brier_score, log_loss

    sufficient = n >= MIN_SLICE_SAMPLES
    margin_pairs = [(p, a) for p, a in margins if p is not None]
    total_pairs = [(p, a) for p, a in totals if p is not None]
    return SliceMetrics(
        n=n,
        accuracy=round(accuracy(outcomes, probabilities), 4),
        log_loss=round(log_loss(outcomes, probabilities), 4),
        brier=round(brier_score(outcomes, probabilities), 4),
        ece=round(expected_calibration_error(
            build_reliability_table(probabilities, outcomes)), 4),
        margin_mae=(round(sum(abs(p - a) for p, a in margin_pairs)
                          / len(margin_pairs), 4) if margin_pairs else None),
        total_mae=(round(sum(abs(p - a) for p, a in total_pairs)
                         / len(total_pairs), 4) if total_pairs else None),
        sufficient=sufficient,
        note=None if sufficient else (
            f"insufficient sample (n={n} < {MIN_SLICE_SAMPLES}): descriptive "
            "only — do not interpret as a stable pattern"),
    )


class DecisionLogEvaluator:
    """Read-only evaluation over the append-only Decision Log."""

    def __init__(self, log: DecisionLog) -> None:
        self._log = log

    # ------------------------------------------------------------- join
    def joined(self, flt: LogEvaluationFilter | None = None) -> list[dict]:
        """Join decision records with settlement events at EVALUATION time.

        Records without a settlement event (or with malformed settlement
        data) are skipped and counted in ``skipped`` — the prediction
        payload is never modified.
        """
        flt = flt or LogEvaluationFilter()
        settlements: dict[str, dict] = {}
        for s in self._log.settlements():
            rid = s.get("record_id")
            if rid is not None and rid not in settlements:
                settlements[rid] = s   # first event per record wins

        joined: list[dict] = []
        skipped_no_settlement = 0
        skipped_malformed = 0
        for r in self._log.records():
            if flt.league is not None and r["league"] != flt.league:
                continue
            if flt.season is not None and r["season"] != flt.season:
                continue
            if flt.family is not None and r["primary_family"] != flt.family:
                continue
            if (flt.window_start is not None
                    and r["as_of"] < flt.window_start):
                continue
            if flt.window_end is not None and r["as_of"] >= flt.window_end:
                continue
            settlement = settlements.get(r["record_id"])
            if settlement is None:
                skipped_no_settlement += 1
                continue
            try:
                home = int(settlement["actual_home_score"])
                away = int(settlement["actual_away_score"])
                p_home = float(r["prediction"]["primary_prob_home"])
            except (KeyError, TypeError, ValueError):
                skipped_malformed += 1
                continue
            joined.append({
                "record_id": r["record_id"],
                "game_id": r["game_id"],
                "league": r["league"],
                "season": r["season"],
                "as_of": r["as_of"],
                "primary_family": r["primary_family"],
                "decision_state": r["decision_state"],
                "validation_status": r["validation_status"],
                "dispersion_band": r["risk"]["dispersion_band"],
                "tossup": r["risk"]["tossup_indicator"],
                "directional_disagreement": _directional(r),
                "calibration_source": r["calibration_source"],
                "regulation_seconds": r["regulation_seconds"],
                "probs": {
                    "primary": p_home,
                    "elo": _prob(r, "elo"),
                    "strength": _prob(r, "strength"),
                    "simulation": _prob(r, "simulation"),
                },
                "home_win": home > away,
                "actual_margin": float(home - away),
                "actual_total": float(home + away),
                "expected_margin": r["prediction"]["expected_margin"],
                "expected_total": r["prediction"]["expected_total"],
            })
        self.last_join_stats = {
            "joined": len(joined),
            "skipped_no_settlement": skipped_no_settlement,
            "skipped_malformed": skipped_malformed,
        }
        return joined

    # ---------------------------------------------------------- metrics
    def evaluate(self, flt: LogEvaluationFilter | None = None,
                 by_model: bool = False) -> dict:
        """Real Agent historical metrics for the filtered scope; with
        ``by_model=True`` also per-model metrics (elo/strength/simulation/
        primary) computed from the stored probabilities."""
        rows = self.joined(flt)
        out: dict = {
            "evidence_kind": "real_agent_history",
            "primary": _slice(rows),
        }
        if by_model:
            models: dict[str, list[dict]] = {"primary": rows}
            for m in _MODELS:
                models[m] = [r for r in rows if r["probs"][m] is not None]
            out["by_model"] = {
                name: _slice(rows) for name, rows in models.items()}
        out["slices"] = self.slices(rows)
        out["join_stats"] = dict(self.last_join_stats)
        return out

    # ----------------------------------------------------------- slices
    def slices(self, rows: list[dict] | None = None,
               flt: LogEvaluationFilter | None = None) -> dict[str, SliceMetrics]:
        rows = rows if rows is not None else self.joined(flt)
        out: dict[str, SliceMetrics] = {}
        out.update(_by(rows, lambda r: f"league={r['league']}"))
        out.update(_by(rows, lambda r: f"decision_state={r['decision_state']}"))
        out.update(_by(rows, lambda r: f"validation_status={r['validation_status']}"))
        out.update(_by(rows, lambda r: f"dispersion_band={r['dispersion_band']}"))
        out.update(_by(rows, lambda r:
                       f"tossup={'yes' if r['tossup'] else 'no'}"))
        out.update(_by(rows, lambda r:
                       f"directional_disagreement="
                       f"{'yes' if r['directional_disagreement'] else 'no'}"))
        out.update(_by(rows, lambda r: f"calibration={r['calibration_source']}"))
        return out


def _prob(record: dict, model: str) -> float | None:
    return record.get("model_probabilities", {}).get(model)


def _directional(record: dict) -> bool:
    probs = [p for p in (_prob(record, "elo"), _prob(record, "strength"),
                         _prob(record, "simulation")) if p is not None]
    return any((a >= 0.5) != (b >= 0.5)
               for i, a in enumerate(probs) for b in probs[i + 1:])


def _by(rows: list[dict], key_fn) -> dict[str, SliceMetrics]:
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(key_fn(r), []).append(r)
    out: dict[str, SliceMetrics] = {}
    for name in sorted(buckets):
        rows_b = buckets[name]
        probabilities = [r["probs"]["primary"] for r in rows_b]
        outcomes = [r["home_win"] for r in rows_b]
        margins = [(r["expected_margin"], r["actual_margin"])
                   for r in rows_b]
        totals = [(r["expected_total"], r["actual_total"]) for r in rows_b]
        out[name] = _metrics(len(rows_b), probabilities, outcomes,
                             margins, totals)
    return out


def _slice(rows: list[dict]) -> SliceMetrics:
    probabilities = [r["probs"]["primary"] for r in rows]
    outcomes = [r["home_win"] for r in rows]
    margins = [(r["expected_margin"], r["actual_margin"]) for r in rows]
    totals = [(r["expected_total"], r["actual_total"]) for r in rows]
    return _metrics(len(rows), probabilities, outcomes, margins, totals)
