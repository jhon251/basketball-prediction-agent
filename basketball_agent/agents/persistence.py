"""Production persistence boundary (Phase 10-D).

Bridges the Phase 9-E orchestration (PURE — no I/O) and the Phase 10-B
append-only Decision Log:

* ``orchestrate(game_id, as_of)`` stays pure: logging is an EXPLICIT
  subsequent step;
* ``persist`` stores one AgentBundle exactly as produced — it never
  changes the bundle contents (the returned ``record_id`` is the
  content-hash id from the 10-B store; append is idempotent, so the same
  bundle yields the same id and a ``duplicate`` status without writing);
* settlements accumulate ONLY through ``log_settlement`` in the separate
  settlement log, linked by record_id; the prediction payload is never
  touched;
* the same game at different as_of boundaries produces different
  record_ids — both versions coexist;
* test/backtest runs simply never call ``persist``: nothing is written
  unless this boundary is invoked explicitly.

Also provides ``settle_and_log``: runs the existing SettlementStore
settlement for a FINAL game (unchanged behavior) and appends one
settlement event per stored decision record of that game.
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.agents.decision import AgentDecision
from basketball_agent.agents.decision_log import DecisionLog
from basketball_agent.agents.decision_record import DecisionRecord
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent, RiskResult


@dataclass(frozen=True)
class PersistResult:
    """Explicit result of one persistence attempt."""

    record_id: str
    status: str                 # "appended" | "duplicate"
    bundle: AgentDecision       # verbatim — logging must not change it
    risk: RiskResult


def persist(bundle_decision: AgentDecision, risk: RiskResult,
            record: DecisionRecord, log: DecisionLog) -> PersistResult:
    """Store one decision (bundle + risk + record) in the Decision Log.

    Pure passthrough of the bundle: the record_id is derived from the
    content, and the append is idempotent (10-B semantics).
    """
    record_id, status = log.append(bundle_decision, risk, record)
    return PersistResult(record_id=record_id, status=status,
                         bundle=bundle_decision, risk=risk)


def settle_and_log(db, log: DecisionLog, game_id: str,
                   settled_at=None) -> list[tuple[str, str, object]]:
    """Settle a FINAL game through the EXISTING SettlementStore (unchanged
    predict→save→settle chain) and append one settlement event per stored
    decision record of that game.

    Returns ``(record_id, status, settlement)`` tuples in record order.
    Handles: settlement not yet arrived (game not FINAL → the underlying
    store raises, propagated), duplicate events (idempotent), and records
    with no stored decision (no events appended).
    """
    from basketball_agent.evaluation.settlement import SettlementStore

    store = SettlementStore(db)
    game_records = [r for r in log.records() if r["game_id"] == game_id]
    try:
        settled = store.settle(game_id, settled_at=settled_at)
        if not isinstance(settled, list):
            settled = [settled]
    except ValueError as exc:
        # re-settlement of an already-settled game: the store raises
        # "no unsettled predictions" — every record of this game that
        # already has a settlement event is returned as "duplicate";
        # records WITHOUT one mean an inconsistent state and still raise.
        already = [(r["record_id"],)
                   for r in game_records
                   if log.settlements_for(r["record_id"])]
        if len(already) == len(game_records) and game_records:
            return [(rid, "duplicate", ev)
                    for (rid,), ev in
                    zip(already, (log.settlements_for(r) for (r,) in already))]
        raise
    if not isinstance(settled, list):
        settled = [settled]
    out: list[tuple[str, str, object]] = []
    for record in game_records:
        rid = record["record_id"]
        event_id, status = log.append_settlement(rid, settled[-1])
        out.append((rid, status, settled[-1]))
    return out
