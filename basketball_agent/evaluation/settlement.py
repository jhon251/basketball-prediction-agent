"""Prediction settlement (spec §33).

Predictions are persisted with their full context and settled against the
final score once the game is FINAL: Prediction -> Game -> Result ->
Evaluation -> Calibration -> Model Improvement.  Aggregate metrics over the
settled set feed the calibration loop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from basketball_agent.data.db.connection import Database
from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.evaluation.calibration import build_reliability_table, expected_calibration_error
from basketball_agent.evaluation.metrics import accuracy, brier_score, log_loss

if TYPE_CHECKING:  # avoids an agents<->evaluation import cycle at runtime
    from basketball_agent.agents.prediction_agent import GamePrediction


@dataclass(frozen=True)
class SettledPrediction:
    prediction_id: int
    game_id: str
    as_of: datetime
    prob_home: float
    prob_away: float
    expected_margin: float | None
    expected_total: float | None
    actual_home_score: int
    actual_away_score: int
    brier: float
    log_loss: float
    correct: bool


class SettlementStore:
    """Persists and settles predictions (one row per prediction)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def save(
        self,
        prediction: "GamePrediction",
        created_at: datetime | None = None,
    ) -> int:
        created_at = created_at or datetime.now(timezone.utc)
        flags = json.dumps(
            [{"code": f.code, "severity": f.severity, "message": f.message}
             for f in prediction.risk.risk_flags]
        )
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO predictions (game_id, as_of, prob_home, prob_away, "
                "expected_margin, expected_total, n_simulations, seed, confidence, "
                "risk_flags, model_version, snapshot_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (prediction.game_id, prediction.as_of.isoformat(),
                 prediction.win_probability_home, prediction.win_probability_away,
                 prediction.expected_margin, prediction.expected_total,
                 prediction.monte_carlo.n_simulations, prediction.monte_carlo.seed,
                 prediction.risk.confidence_score, flags,
                 prediction.model_version, prediction.snapshot_id,
                 created_at.isoformat()),
            )
            prediction_id = cur.lastrowid
        return int(prediction_id)

    def settle(self, game_id: str, settled_at: datetime | None = None) -> SettledPrediction:
        """Settle every unsettled prediction for a FINAL game against its score."""
        settled_at = settled_at or datetime.now(timezone.utc)
        with self._db.connect() as conn:
            game = conn.execute(
                "SELECT home_score, away_score, status FROM games WHERE game_id = ?",
                (game_id,),
            ).fetchone()
            if game is None:
                raise ValueError(f"game {game_id} not found")
            if game["status"] != GameStatus.FINAL.value or game["home_score"] is None:
                raise ValueError(f"game {game_id} is not FINAL with scores — cannot settle")
            rows = conn.execute(
                "SELECT * FROM predictions WHERE game_id = ? AND settled = 0",
                (game_id,),
            ).fetchall()
        if not rows:
            raise ValueError(f"no unsettled predictions for game {game_id}")

        actual_home = int(game["home_score"])
        actual_away = int(game["away_score"])
        home_win = actual_home > actual_away
        settled: list[SettledPrediction] = []
        for row in rows:
            p = float(row["prob_home"])
            brier = (p - (1.0 if home_win else 0.0)) ** 2
            ll = -((1.0 if home_win else 0.0) * _safe_log(p)
                   + (0.0 if home_win else 1.0) * _safe_log(1.0 - p))
            correct = (p >= 0.5) == home_win
            with self._db.transaction() as conn:
                conn.execute(
                    "UPDATE predictions SET settled = 1, settled_at = ?, "
                    "actual_home_score = ?, actual_away_score = ?, brier = ?, "
                    "log_loss = ?, correct = ? WHERE prediction_id = ?",
                    (settled_at.isoformat(), actual_home, actual_away, brier, ll,
                     int(correct), row["prediction_id"]),
                )
            settled.append(SettledPrediction(
                prediction_id=row["prediction_id"], game_id=game_id,
                as_of=datetime.fromisoformat(row["as_of"]),
                prob_home=p, prob_away=float(row["prob_away"]),
                expected_margin=row["expected_margin"],
                expected_total=row["expected_total"],
                actual_home_score=actual_home, actual_away_score=actual_away,
                brier=brier, log_loss=ll, correct=correct,
            ))
        return settled[0] if len(settled) == 1 else settled[-1]

    def list_settled(self) -> list[SettledPrediction]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM predictions WHERE settled = 1 ORDER BY settled_at ASC"
            ).fetchall()
        out = []
        for r in rows:
            out.append(SettledPrediction(
                prediction_id=r["prediction_id"], game_id=r["game_id"],
                as_of=datetime.fromisoformat(r["as_of"]),
                prob_home=float(r["prob_home"]), prob_away=float(r["prob_away"]),
                expected_margin=r["expected_margin"], expected_total=r["expected_total"],
                actual_home_score=r["actual_home_score"],
                actual_away_score=r["actual_away_score"],
                brier=float(r["brier"]), log_loss=float(r["log_loss"]),
                correct=bool(r["correct"]),
            ))
        return out

    def aggregate_metrics(self) -> dict:
        """Calibration-loop metrics over all settled predictions."""
        settled = self.list_settled()
        if not settled:
            return {"n_settled": 0, "accuracy": None, "brier_score": None,
                    "log_loss": None, "ece": None}
        probs = [s.prob_home for s in settled]
        outcomes = [s.actual_home_score > s.actual_away_score for s in settled]
        table = build_reliability_table(probs, outcomes)
        return {
            "n_settled": len(settled),
            "accuracy": accuracy(outcomes, probs),
            "brier_score": brier_score(outcomes, probs),
            "log_loss": log_loss(outcomes, probs),
            "ece": expected_calibration_error(table),
        }


def _safe_log(x: float) -> float:
    import math

    return math.log(min(max(x, 1e-12), 1.0 - 1e-12))
