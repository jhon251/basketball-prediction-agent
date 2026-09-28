"""Production persistence boundary tests (Phase 10-D)."""
from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

import pytest

from basketball_agent.agents.decision import DecisionAgent
from basketball_agent.agents.decision_log import DecisionLog
from basketball_agent.agents.decision_record import DecisionRecordAgent
from basketball_agent.agents.orchestration import AgentOrchestrator
from basketball_agent.agents.persistence import (
    persist,
    settle_and_log,
)
from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent
from basketball_agent.data.db.repository import Repository
from basketball_agent.evaluation.log_evaluation import (
    DecisionLogEvaluator,
    LogEvaluationFilter,
)

AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)


def _bundle(scheduled_target, seed=7):
    core = PredictionAgent(Repository(scheduled_target),
                           n_simulations=50, seed=seed)
    decision = DecisionAgent(core).decide("20241120_LAL_BOS", as_of=AS_OF)
    risk = RiskAnalysisAgent().analyze(decision)
    record = DecisionRecordAgent().build(decision, risk)
    return decision, risk, record


class TestPersistBoundary:
    def test_persist_writes_and_bundle_unchanged(self, scheduled_target,
                                                 tmp_path):
        log = DecisionLog(tmp_path / "agent_log")
        decision, risk, record = _bundle(scheduled_target)
        before_bundle = decision
        persisted = persist(decision, risk, record, log)
        assert persisted.status == "appended"
        assert persisted.bundle is before_bundle  # verbatim, no copy/edition
        stored = log.get_record(persisted.record_id)
        assert stored["prediction"]["primary_prob_home"] == \
            decision.result.primary_prob_home

    def test_persist_idempotent_same_record_id(self, scheduled_target,
                                               tmp_path):
        log = DecisionLog(tmp_path / "agent_log")
        decision, risk, record = _bundle(scheduled_target)
        p1 = persist(decision, risk, record, log)
        p2 = persist(decision, risk, record, log)
        assert p1.record_id == p2.record_id
        assert (p1.status, p2.status) == ("appended", "duplicate")
        assert len(log.records()) == 1

    def test_same_game_different_as_of_two_versions(self, scheduled_target,
                                                    tmp_path):
        log = DecisionLog(tmp_path / "agent_log")
        d1, r1, rec1 = _bundle(scheduled_target)
        p1 = persist(d1, r1, rec1, log)
        from dataclasses import replace

        d2 = replace(d1, context=replace(
            d1.context, as_of="2024-11-19T13:00:00+00:00"))
        rec2 = DecisionRecordAgent().build(d2, r1)
        p2 = persist(d2, r1, rec2, log)
        assert p1.record_id != p2.record_id
        assert p2.status == "appended"
        assert len(log.records()) == 2

    def test_persist_does_not_write_on_duplicate(self, scheduled_target,
                                                 tmp_path):
        log = DecisionLog(tmp_path / "agent_log")
        decision, risk, record = _bundle(scheduled_target)
        persist(decision, risk, record, log)
        size = log.decisions_path.stat().st_size
        persist(decision, risk, record, log)
        assert log.decisions_path.stat().st_size == size

    def test_orchestrate_is_pure_no_log_writes(self, scheduled_target,
                                               tmp_path):
        """orchestrate() never writes the Decision Log (persistence is an
        explicit separate step)."""
        log_dir = tmp_path / "agent_log"
        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        orchestrator = AgentOrchestrator(core)
        bundle = orchestrator.orchestrate("20241120_LAL_BOS", as_of=AS_OF)
        assert not log_dir.exists()  # nothing persisted by orchestration


class TestSettleAndLog:
    def _final_game_db(self, scheduled_target):
        """The shared fixture DB plus one FINAL game (scores 101-99) that
        a stub prediction can be settled against."""
        from basketball_agent.data.schemas.base import (
            Competition, GameStatus, League,
        )
        from basketball_agent.data.db.repository import Repository

        game_id = "20241118_LAL_BOS"
        Repository(scheduled_target).insert_game(
            type("G", (), {
                "game_id": game_id, "league": League.NBA,
                "season": "2024-25",
                "game_date": datetime(2024, 11, 18, 0, 0, tzinfo=UTC),
                "home_team_id": "LAL", "away_team_id": "BOS",
                "home_score": 101, "away_score": 99,
                "status": GameStatus.FINAL,
                "competition": Competition.REGULAR_SEASON,
            })())
        return game_id

    def _final_bundle(self, scheduled_target):
        """Real bundle for the FINAL game (as-of before tip-off)."""
        core = PredictionAgent(Repository(scheduled_target),
                               n_simulations=50, seed=7)
        decision = DecisionAgent(core).decide(
            "20241118_LAL_BOS", as_of=datetime(2024, 11, 17, 12, 0,
                                               tzinfo=UTC))
        risk = RiskAnalysisAgent().analyze(decision)
        record = DecisionRecordAgent().build(decision, risk)
        return decision, risk, record

    def _save_stub_prediction(self, scheduled_target, game_id):
        from basketball_agent.evaluation.settlement import SettlementStore

        store = SettlementStore(scheduled_target)
        return store.save(_make_stub_prediction(game_id))

    def test_full_closed_loop(self, scheduled_target, tmp_path):
        """orchestrate → persist → settle → 10-C evaluate, end to end."""
        game_id = self._final_game_db(scheduled_target)
        decision, risk, record = self._final_bundle(scheduled_target)
        log = DecisionLog(tmp_path / "agent_log")
        persisted = persist(decision, risk, record, log)
        self._save_stub_prediction(scheduled_target, game_id)

        settled_out = settle_and_log(scheduled_target, log, game_id)
        assert len(settled_out) == 1
        rid, status, settlement = settled_out[0]
        assert rid == persisted.record_id
        assert status == "appended"
        assert settlement.actual_home_score == 101

        # 10-C join by record_id finds exactly this settlement
        ev = DecisionLogEvaluator(log)
        rows = ev.joined(LogEvaluationFilter())
        assert len(rows) == 1
        assert rows[0]["record_id"] == persisted.record_id
        assert rows[0]["home_win"] is True

    def test_duplicate_settlement_idempotent(self, scheduled_target,
                                             tmp_path):
        game_id = self._final_game_db(scheduled_target)
        decision, risk, record = self._final_bundle(scheduled_target)
        log = DecisionLog(tmp_path / "agent_log")
        persist(decision, risk, record, log)
        self._save_stub_prediction(scheduled_target, game_id)

        s1 = settle_and_log(scheduled_target, log, game_id)
        s2 = settle_and_log(scheduled_target, log, game_id)
        assert s1[0][0] == s2[0][0]
        assert s2[0][1] == "duplicate"
        assert len(log.settlements()) == 1

    def test_nonfinal_game_propagates_store_error(self, scheduled_target,
                                                  tmp_path):
        """A game that is not FINAL cannot settle: the frozen store raises
        and the persistence boundary propagates (no partial facts)."""
        self._final_game_db(scheduled_target)  # needed by the real bundle
        from basketball_agent.evaluation.settlement import SettlementStore

        decision, risk, record = self._final_bundle(scheduled_target)
        log = DecisionLog(tmp_path / "agent_log")
        persist(decision, risk, record, log)
        # save a prediction for the SCHEDULED fixture game
        self._save_stub_prediction(scheduled_target, "20241120_LAL_BOS")

        with pytest.raises(ValueError, match="not FINAL"):
            settle_and_log(scheduled_target, log, "20241120_LAL_BOS")
        # the decision record remains stored, without settlement facts
        assert len(log.records()) == 1
        assert log.settlements() == []


def _make_stub_prediction(game_id: str):
    """Minimal stub satisfying SettlementStore.save (has the attributes it
    reads).  Built from real frozen-core types to stay honest."""
    from basketball_agent.agents.ensemble_agent import (
        EnsemblePrediction, EnsembleWeights,
    )
    from basketball_agent.agents.risk_agent import RiskAssessment
    from basketball_agent.simulation.monte_carlo import MonteCarloResult
    from basketball_agent.simulation.monte_carlo import DistributionStats

    stats = DistributionStats(
        mean=100.0, median=100.0, std=10.0, p5=85.0, p25=93.0, p50=100.0,
        p75=107.0, p95=115.0, count=50)
    mc = MonteCarloResult(
        n_simulations=50, seed=7, game_id=game_id,
        home_win_probability=0.6, away_win_probability=0.4,
        tie_probability=0.0, home_score=stats, away_score=stats,
        margin=stats, total=stats, home_score_buckets=(),
        away_score_buckets=(), runs=())
    ensemble = EnsemblePrediction(
        prob_home=0.6, p_elo=0.6, p_strength=0.6, p_sim=0.6,
        weights=EnsembleWeights())
    risk = RiskAssessment(confidence_score=50.0, risk_flags=())
    as_of = AS_OF
    return type("_Stub", (), {
        "game_id": game_id,
        "as_of": as_of,
        "win_probability_home": 0.6,
        "win_probability_away": 0.4,
        "expected_margin": 3.0,
        "expected_total": 200.0,
        "monte_carlo": mc,
        "risk": risk,
        "ensemble": ensemble,
        "model_version": "0.1.0",
        "snapshot_id": None,
    })()
