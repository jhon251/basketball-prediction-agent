"""Append-only Decision Log tests (Phase 10-B)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from basketball_agent.agents.decision import DecisionAgent
from basketball_agent.agents.decision_log import DecisionLog, serialize_bundle
from basketball_agent.agents.decision_record import DecisionRecordAgent
from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent
from basketball_agent.data.db.repository import Repository
from basketball_agent.evaluation.settlement import SettlementStore

AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def log(tmp_path) -> DecisionLog:
    return DecisionLog(tmp_path / "agent_log")


def _bundle(scheduled_target, **agent_kwargs):
    kwargs = {"n_simulations": 50, "seed": 7, **agent_kwargs}
    core = PredictionAgent(Repository(scheduled_target), **kwargs)
    decision = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
    risk = RiskAnalysisAgent().analyze(decision)
    record = DecisionRecordAgent().build(decision, risk)
    return decision, risk, record


@pytest.fixture()
def appended(scheduled_target, log):
    decision, risk, record = _bundle(scheduled_target)
    rid, status = log.append(decision, risk, record)
    return decision, risk, record, rid, status


class TestAppendOnly:
    def test_append_then_read_roundtrip(self, appended, log):
        decision, risk, record, rid, status = appended
        assert status == "appended"
        stored = log.get_record(rid)
        assert stored["game_id"] == "20241120_LAL_BOS"
        assert stored["league"] == "NBA"
        assert stored["prediction"]["primary_prob_home"] == \
            record.prediction_summary["primary_prob_home"]
        assert stored["decision_state"] == \
            record.decision_state.value
        assert stored["rule_trace"]["rule_id"] == record.rule_trace.rule_id

    def test_no_update_or_delete_api(self):
        """The store structurally cannot modify history: no update/delete
        methods exist."""
        for banned in ("update", "delete", "remove", "rewrite", "truncate"):
            assert not hasattr(DecisionLog, banned)

    def test_hash_chain_tamper_evidence(self, scheduled_target, appended,
                                        log):
        decision, risk, record = _bundle(scheduled_target, seed=11)
        log.append(decision, risk, record)
        assert log.verify_chain() is True
        # tamper with one line on disk -> chain verification fails
        lines = log.decisions_path.read_text(encoding="utf-8").splitlines()
        obj = json.loads(lines[0])
        obj["prediction"]["primary_prob_home"] = 0.99  # silent edit attempt
        lines[0] = json.dumps(obj, sort_keys=True, ensure_ascii=False)
        log.decisions_path.write_text("\n".join(lines) + "\n",
                                      encoding="utf-8")
        assert log.verify_chain() is False

    def test_sequence_monotonic(self, scheduled_target, appended, log):
        decision, risk, record = _bundle(scheduled_target, seed=11)
        log.append(decision, risk, record)
        seqs = [r["sequence"] for r in log.records()]
        assert seqs == sorted(seqs) == [1, 2]


class TestDuplicatesAndVersions:
    def test_duplicate_append_is_idempotent(self, scheduled_target, log):
        decision, risk, record = _bundle(scheduled_target)
        rid1, s1 = log.append(decision, risk, record)
        n_lines = len(log.records())
        rid2, s2 = log.append(decision, risk, record)
        assert rid1 == rid2
        assert (s1, s2) == ("appended", "duplicate")
        assert len(log.records()) == n_lines  # nothing written

    def test_same_game_different_as_of_is_new_version(self, scheduled_target,
                                                      log):
        decision, risk, record = _bundle(scheduled_target)
        rid1, _ = log.append(decision, risk, record)
        from dataclasses import replace
        later = replace(decision, context=replace(
            decision.context, as_of="2024-11-19T13:00:00+00:00"))
        record2 = DecisionRecordAgent().build(later, risk)
        rid2, status = log.append(later, risk, record2)
        assert status == "appended" and rid2 != rid1
        assert len(log.records()) == 2  # both retained, ordered by sequence


class TestSettlementSeparation:
    def test_settlement_never_enters_prediction_payload(self, scheduled_target,
                                                        log):
        decision, risk, record = _bundle(scheduled_target)
        rid, _ = log.append(decision, risk, record)
        # settle the game in the DB and log the settlement event
        store = SettlementStore(Repository(scheduled_target)._db) \
            if hasattr(Repository(scheduled_target), "_db") else None
        # independent of the DB path: append a settlement event from a stub
        from basketball_agent.evaluation.settlement import SettledPrediction

        settled = SettledPrediction(
            prediction_id=1, game_id="20241120_LAL_BOS", as_of=AS_OF,
            prob_home=0.5, prob_away=0.5, expected_margin=3.0,
            expected_total=200.0, actual_home_score=101,
            actual_away_score=99, brier=0.25, log_loss=0.69, correct=True)
        eid, status = log.append_settlement(rid, settled)
        assert status == "appended"
        # the prediction record on disk is byte-identical to before
        stored = log.get_record(rid)
        assert "actual_home_score" not in stored
        assert stored["prediction"]["expected_margin"] == \
            record.prediction_summary["expected_margin"]
        # settlement facts live only in the settlements log
        events = log.settlements_for(rid)
        assert len(events) == 1
        assert events[0]["actual_home_score"] == 101

    def test_settlement_duplicate_idempotent(self, scheduled_target, log):
        decision, risk, record = _bundle(scheduled_target)
        rid, _ = log.append(decision, risk, record)
        from basketball_agent.evaluation.settlement import SettledPrediction

        settled = SettledPrediction(
            prediction_id=1, game_id="20241120_LAL_BOS", as_of=AS_OF,
            prob_home=0.5, prob_away=0.5, expected_margin=3.0,
            expected_total=200.0, actual_home_score=101,
            actual_away_score=99, brier=0.25, log_loss=0.69, correct=True)
        e1, s1 = log.append_settlement(rid, settled)
        e2, s2 = log.append_settlement(rid, settled)
        assert e1 == e2 and (s1, s2) == ("appended", "duplicate")
        assert len(log.settlements()) == 1

    def test_settlement_requires_known_record(self, log):
        from basketball_agent.evaluation.settlement import SettledPrediction

        settled = SettledPrediction(
            prediction_id=1, game_id="x", as_of=AS_OF, prob_home=0.5,
            prob_away=0.5, expected_margin=0.0, expected_total=0.0,
            actual_home_score=1, actual_away_score=0, brier=0.25,
            log_loss=0.69, correct=True)
        with pytest.raises(ValueError, match="unknown record_id"):
            log.append_settlement("nonexistent", settled)


class TestIsolationAndCBA:
    def test_league_filter_isolation(self, scheduled_target, log):
        decision, risk, record = _bundle(scheduled_target)
        rid, _ = log.append(decision, risk, record)
        assert len(log.filter_records(league="NBA")) == 1
        assert log.filter_records(league="CBA") == []
        assert len(log.filter_records()) == 1

    def test_cba_dual_regulation_preserved(self, scheduled_target, log):
        decision, risk, record = _bundle(scheduled_target)
        from basketball_agent.agents.decision_contract import (
            PredictionContext,
        )
        from dataclasses import replace

        for season, regulation in (("2024-25", 2880.0), ("2025-26", 2400.0)):
            stub = replace(decision, context=replace(
                decision.context, league="CBA", season=season,
                regulation_seconds=regulation))
            risk2 = RiskAnalysisAgent().analyze(stub)
            record2 = DecisionRecordAgent().build(stub, risk2)
            rid, status = log.append(stub, risk2, record2)
            assert status == "appended"
            stored = log.get_record(rid)
            assert stored["league"] == "CBA"
            assert stored["regulation_seconds"] == regulation
        # both seasons coexist with their own regulation values
        regs = sorted({r["regulation_seconds"]
                       for r in log.filter_records(league="CBA")})
        assert regs == [2400.0, 2880.0]


class TestDeterministicSerialization:
    def test_serialize_deterministic(self, scheduled_target):
        d1, r1, rec1 = _bundle(scheduled_target)
        d2, r2, rec2 = _bundle(scheduled_target)
        p1 = serialize_bundle(d1, r1, rec1)
        p2 = serialize_bundle(d2, r2, rec2)
        assert p1 == p2
        s1 = json.dumps(p1, sort_keys=True, ensure_ascii=False)
        s2 = json.dumps(p2, sort_keys=True, ensure_ascii=False)
        assert s1 == s2

    def test_no_future_result_leakage(self, scheduled_target, log):
        decision, risk, record = _bundle(scheduled_target)
        rid, _ = log.append(decision, risk, record)
        stored = log.get_record(rid)
        # the payload contains only as-of-boundary facts
        for banned in ("actual_home_score", "actual_away_score", "correct",
                       "settled"):
            assert banned not in json.dumps(stored)
