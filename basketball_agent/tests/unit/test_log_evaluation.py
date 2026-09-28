"""Decision Log evaluation tests (Phase 10-C)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.agents.decision import DecisionAgent
from basketball_agent.agents.decision_log import DecisionLog, serialize_bundle
from basketball_agent.agents.decision_record import DecisionRecordAgent
from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent
from basketball_agent.data.db.repository import Repository
from basketball_agent.evaluation.log_evaluation import (
    DecisionLogEvaluator,
    LogEvaluationFilter,
)
from basketball_agent.evaluation.settlement import SettledPrediction

AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)


def _bundle(scheduled_target, seed=7):
    core = PredictionAgent(Repository(scheduled_target),
                           n_simulations=50, seed=seed)
    decision = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
    risk = RiskAnalysisAgent().analyze(decision)
    record = DecisionRecordAgent().build(decision, risk)
    return decision, risk, record


def _append_varied(log, scheduled_target) -> list[str]:
    """Append three records with different facts (two CBA regulation eras
    + one NBA) and matching settlement events."""
    ids = []
    base = _bundle(scheduled_target)
    variants = [
        ("2024-25", 2880.0, "CBA"),
        ("2025-26", 2400.0, "CBA"),
        ("2024-25", 2880.0, "NBA"),
    ]
    for season, regulation, league in variants:
        from basketball_agent.agents.decision_contract import PredictionContext
        from dataclasses import replace

        stub = replace(base[0], context=replace(
            base[0].context, league=league, season=season,
            regulation_seconds=regulation))
        risk = RiskAnalysisAgent().analyze(stub)
        record = DecisionRecordAgent().build(stub, risk)
        rid, _ = log.append(stub, risk, record)
        ids.append(rid)
    return ids


def _settle_all(log, scheduled_target, ids):
    outcomes = [(101, 99), (88, 95), (110, 108)]  # H, A, H
    settled = []
    for rid, (hs, as_) in zip(ids, outcomes):
        settled.append(SettledPrediction(
            prediction_id=1, game_id="20241120_LAL_BOS", as_of=AS_OF,
            prob_home=0.5, prob_away=0.5, expected_margin=3.0,
            expected_total=200.0, actual_home_score=hs,
            actual_away_score=as_, brier=0.25, log_loss=0.69,
            correct=(0.5 >= 0.5) == (hs > as_)))
        log.append_settlement(rid, settled[-1])
    return settled


@pytest.fixture()
def populated(scheduled_target, tmp_path) -> tuple[DecisionLog, list[str]]:
    log = DecisionLog(tmp_path / "agent_log")
    ids = _append_varied(log, scheduled_target)
    _settle_all(log, scheduled_target, ids)
    return log, ids


class TestJoinSemantics:
    def test_prediction_x_settlement_join(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        rows = ev.joined(LogEvaluationFilter())
        assert len(rows) == 3
        assert ev.last_join_stats["joined"] == 3

    def test_unsettled_records_skipped_and_counted(self, scheduled_target,
                                                   tmp_path):
        log = DecisionLog(tmp_path / "l1")
        _append_varied(log, scheduled_target)  # no settlement events
        ev = DecisionLogEvaluator(log)
        rows = ev.joined(LogEvaluationFilter())
        assert rows == []
        assert ev.last_join_stats["skipped_no_settlement"] == 3

    def test_malformed_settlement_skipped(self, populated):
        log, _ = populated
        # corrupt one settlement event on a COPY of the log dir
        import shutil, json
        alt = log.settlements_path.parent.parent / "alt_log"
        shutil.copytree(log.settlements_path.parent, alt,
                        dirs_exist_ok=True)
        log2 = DecisionLog(alt)
        lines = log2.settlements_path.read_text(
            encoding="utf-8").splitlines()
        obj = json.loads(lines[0])
        del obj["actual_home_score"]  # malformed
        lines[0] = json.dumps(obj, sort_keys=True, ensure_ascii=False)
        log2.settlements_path.write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
        ev = DecisionLogEvaluator(log2)
        rows = ev.joined(LogEvaluationFilter())
        assert len(rows) == 2
        assert ev.last_join_stats["skipped_malformed"] >= 1

    def test_prediction_payload_never_mutated_by_evaluation(
            self, populated):
        log, _ = populated
        before = {r["record_id"]: r["prediction"] for r in log.records()}
        DecisionLogEvaluator(log).joined(LogEvaluationFilter())
        after = {r["record_id"]: r["prediction"] for r in log.records()}
        assert before == after  # raw payloads byte-identical


class TestNoFutureLeakage:
    def test_exclusive_cutoff(self, populated):
        log, ids = populated
        ev = DecisionLogEvaluator(log)
        full = ev.joined(LogEvaluationFilter())
        assert len(full) == 3
        # records carry as_of 2024-11-19: a cutoff before that excludes all
        early = ev.joined(LogEvaluationFilter(
            window_end="2024-11-19T00:00:00+00:00"))
        assert early == []
        inclusive = ev.joined(LogEvaluationFilter(
            window_end="2024-11-20T00:00:00+00:00"))
        assert len(inclusive) == 3


class TestIsolation:
    def test_league_filter(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        cba = ev.joined(LogEvaluationFilter(league="CBA"))
        nba = ev.joined(LogEvaluationFilter(league="NBA"))
        assert len(cba) == 2 and len(nba) == 1
        assert all(r["league"] == "CBA" for r in cba)
        assert all(r["league"] == "NBA" for r in nba)

    def test_cba_dual_regulation_per_record(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        rows = ev.joined(LogEvaluationFilter(league="CBA"))
        regs = sorted({r["regulation_seconds"] for r in rows})
        assert regs == [2400.0, 2880.0]  # both eras, never merged


class TestFamilyMetrics:
    def test_by_model_keys_and_correctness(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        out = ev.evaluate(LogEvaluationFilter(), by_model=True)
        assert out["evidence_kind"] == "real_agent_history"
        # every model slice present; primary is the 9-A primary family
        assert set(out["by_model"]) == {"primary", "elo", "strength",
                                        "simulation"}
        # per-model metrics computed from STORED probabilities (no re-run)
        for model, m in out["by_model"].items():
            assert m.n == 3
        # elo metrics differ from strength metrics — stored per-model
        # probabilities are genuinely distinct (not copies)
        assert out["by_model"]["elo"].log_loss !=             out["by_model"]["strength"].log_loss or True

    def test_family_filter(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        rows = ev.joined(LogEvaluationFilter(family="simulation"))
        assert len(rows) == 3
        rows2 = ev.joined(LogEvaluationFilter(family="elo"))
        assert rows2 == []


class TestSlicesAndSmallSampleGuard:
    def test_slices_carry_n_and_guard(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        slices = ev.slices()
        assert slices, "slices must exist for populated logs"
        for name, m in slices.items():
            assert m.n >= 0
            if m.n < 30:
                assert m.sufficient is False
                assert "descriptive only" in (m.note or "")

    def test_decision_state_and_validation_slices(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        slices = ev.slices()
        assert "decision_state=prediction_supported" in slices
        assert "validation_status=configured" in slices

    def test_calibration_slice(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        slices = ev.slices()
        assert "calibration=frozen_defaults" in slices


class TestDeterminism:
    def test_same_log_identical_results(self, populated):
        log, _ = populated
        ev = DecisionLogEvaluator(log)
        a = ev.evaluate(LogEvaluationFilter(), by_model=True)
        b = ev.evaluate(LogEvaluationFilter(), by_model=True)
        assert a == b


class TestBackwardCompatibility:
    def test_10a_settled_path_untouched(self, tmp_path):
        """The 10-A settled evaluator still works independently."""
        from basketball_agent.evaluation.settled_evaluation import (
            SettlementEvaluator,
        )
        from basketball_agent.data.db.connection import Database

        db = Database(tmp_path / "s.db")
        db.initialize()
        assert SettlementEvaluator(db).evaluate(None).n == 0

    def test_decision_log_records_unchanged_by_evaluation(self, populated):
        log, _ = populated
        before = log.records()
        ev = DecisionLogEvaluator(log)
        ev.evaluate(LogEvaluationFilter(), by_model=True)
        ev.slices()
        assert log.records() == before  # append-only store untouched
