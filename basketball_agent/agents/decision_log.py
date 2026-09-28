"""Append-only Decision Log (Phase 10-B).

An immutable, tamper-evident, append-only history of AgentBundles
(Phase 9-E) plus a SEPARATE settlement-event log.  Design rules:

* **Append-only**: records are JSON lines appended in order; the store
  exposes no update or delete API.  Tamper-evidence comes from a hash
  chain (each entry carries ``prev_hash`` and ``entry_hash`` over the
  canonical payload + previous hash); ``verify_chain`` re-walks it.
* **Immutability of predictions**: a decision record is a snapshot of
  what the Agent knew at its ``as_of`` boundary.  Settlement results are
  written ONLY to the separate settlements log, linked by ``record_id``
  — they never enter the prediction payload.
* **Version semantics**: the unique identifier of a record is its
  ``record_id`` = sha256 of the canonical (sorted-key) payload — the same
  inputs always produce the same id, so re-appending the identical bundle
  is a deterministic no-op (idempotent duplicate).  The same game_id at a
  different ``as_of`` (or with any different fact) produces a different
  record_id; both records are retained, ordered by the monotone
  ``sequence`` number.  Nothing is ever overwritten.
* **Isolation**: every record carries its league and regulation_seconds
  (CBA 2024-25 = 2880 s vs 2025-26 = 2400 s pass through verbatim);
  queries filter per league — no cross-league state.
* **No fabrication**: absent values (elo probability when unavailable,
  calibration, ``ot_probability``) are stored as ``null`` with their
  contract meaning preserved.

The log lives under ``storage/agent_log/`` (gitignored) and is consumed
read-only by the Phase 10-A evaluation layer.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from basketball_agent.agents.decision import AgentDecision
from basketball_agent.agents.decision_contract import ValidationStatus
from basketball_agent.agents.decision_record import DecisionRecord
from basketball_agent.agents.explanation import ExplanationResult
from basketball_agent.agents.risk_analysis import RiskResult


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _stats_to_dict(stats) -> dict | None:
    if stats is None:
        return None
    return {name: getattr(stats, name) for name in
            ("mean", "median", "std", "p5", "p25", "p50", "p75", "p95", "count")}


def serialize_bundle(decision_agent: AgentDecision, risk: RiskResult,
                     record: DecisionRecord) -> dict:
    """Deterministic json-safe payload of one decision (as-of boundary facts
    only — settlement data can never appear here)."""
    ctx = decision_agent.context
    result = record.prediction_summary
    diag = record.evidence_summary
    return {
        "schema": "decision-log-v1",
        "record_id": None,            # filled after hashing
        "sequence": None,             # filled by the store
        "prev_hash": None,            # filled by the store
        "entry_hash": None,           # filled by the store
        "game_id": record.game_id,
        "league": record.league,
        "season": record.season,
        "home_team_id": record.home_team_id,
        "away_team_id": record.away_team_id,
        "as_of": record.as_of,
        "primary_family": record.primary_family,
        "validation_status": record.validation_status.value,
        "decision_state": record.decision_state.value,
        "rule_trace": {
            "rule_id": record.rule_trace.rule_id,
            "conditions": [[k, v] for k, v in record.rule_trace.conditions],
        },
        "prediction": {
            "primary_prob_home": result["primary_prob_home"],
            "primary_prob_away": result["primary_prob_away"],
            "predicted_winner": result["predicted_winner"],
            "expected_margin": result["expected_margin"],
            "expected_total": result["expected_total"],
        },
        "model_probabilities": {
            "elo": diag["p_elo"],
            "strength": diag["p_strength"],
            "simulation": diag["p_sim"],
        },
        "simulation_distribution": diag["simulation_distribution"],
        "risk": {
            "flags": list(risk.flags),
            "tossup_indicator": risk.probability_facts.tossup_indicator,
            "tossup_band": list(risk.probability_facts.tossup_band),
            "max_pairwise_disagreement": risk.model_agreement_facts.max_pairwise_disagreement,
            "dispersion_band": risk.model_agreement_facts.dispersion_band,
            "margin_std": risk.distribution_facts.margin_std,
            "total_std": risk.distribution_facts.total_std,
            "core_risk_flags": [dict(code=f.code, severity=f.severity,
                                     message=f.message)
                                for f in risk.core_risk.core_risk_flags],
        },
        "calibration": (
            None if ctx.calibration is None else
            {"source": ctx.calibration_source,
             "params_file": ctx.calibration.params_file,
             "fitted_window": list(ctx.calibration.fitted_window),
             "fitted_at": ctx.calibration.fitted_at,
             "validation_logloss": ctx.calibration.validation_logloss}),
        "calibration_source": ctx.calibration_source,
        "snapshot_id": ctx.snapshot_id,
        "model_version": record.model_version,
        "regulation_seconds": ctx.regulation_seconds,
        "known_limitations": list(risk.evidence_facts.known_limitations),
    }


class DecisionLog:
    """Append-only JSONL store for decision records + settlement events."""

    DECISIONS_FILE = "decisions.jsonl"
    SETTLEMENTS_FILE = "settlements.jsonl"

    def __init__(self, log_dir: Path | str) -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def decisions_path(self) -> Path:
        return self._dir / self.DECISIONS_FILE

    @property
    def settlements_path(self) -> Path:
        return self._dir / self.SETTLEMENTS_FILE

    # ------------------------------------------------------------------ read
    def _read_lines(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def records(self) -> list[dict]:
        return self._read_lines(self.decisions_path)

    def settlements(self) -> list[dict]:
        return self._read_lines(self.settlements_path)

    def record_ids(self) -> set[str]:
        return {r["record_id"] for r in self.records()}

    def get_record(self, record_id: str) -> dict | None:
        for r in self.records():
            if r["record_id"] == record_id:
                return r
        return None

    def settlements_for(self, record_id: str) -> list[dict]:
        return [s for s in self.settlements()
                if s["record_id"] == record_id]

    def filter_records(self, *, league: str | None = None,
                       season: str | None = None,
                       window_start: str | None = None,
                       window_end: str | None = None) -> list[dict]:
        """Read-only query API (consumed by the Phase 10-A evaluation
        layer).  ``window_end`` is an exclusive historical cutoff on the
        record's ``as_of``; nothing here consults the current time."""
        out = []
        for r in self.records():
            if league is not None and r["league"] != league:
                continue
            if season is not None and r["season"] != season:
                continue
            if window_start is not None and r["as_of"] < window_start:
                continue
            if window_end is not None and r["as_of"] >= window_end:
                continue
            out.append(r)
        return out

    def verify_chain(self) -> bool:
        """Re-walk the hash chain; False means the file was tampered with."""
        prev = ""
        for r in self.records():
            payload = {k: v for k, v in r.items()
                       if k not in ("prev_hash", "entry_hash")}
            expected = hashlib.sha256(
                (prev + _canonical(payload)).encode("utf-8")).hexdigest()
            if r["prev_hash"] != prev or r["entry_hash"] != expected:
                return False
            prev = r["entry_hash"]
        return True

    # ----------------------------------------------------------------- write
    def append(self, decision: AgentDecision, risk: RiskResult,
               record: DecisionRecord,
               explanation: ExplanationResult | None = None) -> tuple[str, str]:
        """Append one AgentBundle as a decision record.

        Returns ``(record_id, status)`` with status ``"appended"`` or
        ``"duplicate"`` — re-appending the identical bundle is an
        idempotent no-op (same record_id, nothing written).
        """
        payload = serialize_bundle(decision, risk, record)
        existing = self.record_ids()
        record_id = hashlib.sha256(
            _canonical(payload).encode("utf-8")).hexdigest()[:16]
        if record_id in existing:
            return record_id, "duplicate"
        prev_hash = ""
        if self.records():
            prev_hash = self.records()[-1]["entry_hash"]
        payload["record_id"] = record_id
        payload["sequence"] = len(existing) + 1
        payload["prev_hash"] = prev_hash
        payload["entry_hash"] = hashlib.sha256(
            (prev_hash + _canonical({k: v for k, v in payload.items()
                                     if k not in ("prev_hash", "entry_hash")}))
            .encode("utf-8")).hexdigest()
        with self.decisions_path.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(payload) + "\n")
        return record_id, "appended"

    def append_settlement(self, record_id: str, settled) -> tuple[str, str]:
        """Append a SEPARATE settlement event linked by record_id.

        ``settled`` is a frozen ``SettledPrediction``.  The original
        decision record is never touched; settlement facts live only here.
        """
        if record_id not in self.record_ids():
            raise ValueError(f"unknown record_id {record_id!r}")
        payload = {
            "schema": "settlement-log-v1",
            "record_id": record_id,
            "game_id": settled.game_id,
            "settled_at_boundary": "settlement event; not part of the "
                                   "prediction payload",
            "actual_home_score": settled.actual_home_score,
            "actual_away_score": settled.actual_away_score,
            "correct": settled.correct,
            "brier": settled.brier,
            "log_loss": settled.log_loss,
        }
        event_id = hashlib.sha256(
            _canonical(payload).encode("utf-8")).hexdigest()[:16]
        existing = {s.get("event_id") for s in self.settlements()}
        if event_id in existing:
            return event_id, "duplicate"
        payload["event_id"] = event_id
        with self.settlements_path.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(payload) + "\n")
        return event_id, "appended"
