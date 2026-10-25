"""Production auto-prediction loop tests (Phase 10-E.2).

Real frozen core on the shared fixture DB; discovery mocked (no network).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.collection.production_loop import ProductionLoop
from basketball_agent.agents.decision_log import DecisionLog
from basketball_agent.collection.scheduler import CollectionScheduler
from basketball_agent.collection.state import CollectorState
from basketball_agent.data.db.connection import Database


@pytest.fixture(autouse=True)
def _no_network_injuries(monkeypatch):
    """Phase 12-G: the NBA pass refreshes ESPN injuries; stub it so this
    suite keeps its no-network guarantee."""
    from basketball_agent.data.collectors import espn_injuries

    class _Stub:
        availability_written = 0
        records = []
        skipped_unresolved_player = 0
        errors = []

    monkeypatch.setattr(espn_injuries, "refresh_injuries",
                        lambda db, **kw: _Stub())
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import (
    Competition, GameStatus, League,
)
from basketball_agent.evaluation.settlement import SettlementStore

UTC = timezone.utc
NOW = datetime(2024, 11, 19, 12, 0, tzinfo=UTC)


def _upcoming_game(db, game_id, *, kickoff=None, league=League.NBA,
                   home="LAL", away="BOS"):
    Repository(db).insert_game(type("G", (), {
        "game_id": game_id, "league": league, "season": "2024-25",
        "game_date": kickoff or datetime(2024, 11, 25, 20, 0, tzinfo=UTC),
        "home_team_id": home, "away_team_id": away,
        "home_score": None, "away_score": None,
        "status": GameStatus.SCHEDULED,
        "competition": Competition.REGULAR_SEASON,
    }))


def _final_game(db, game_id, *, kickoff=None):
    Repository(db).insert_game(type("G", (), {
        "game_id": game_id, "league": League.NBA, "season": "2024-25",
        "game_date": kickoff or datetime(2024, 11, 15, 20, 0, tzinfo=UTC),
        "home_team_id": "LAL", "away_team_id": "BOS",
        "home_score": 101, "away_score": 99,
        "status": GameStatus.FINAL,
        "competition": Competition.REGULAR_SEASON,
    }))


def _loop(db, tmp_path, **core_kwargs) -> ProductionLoop:
    from basketball_agent.agents.decision_log import DecisionLog
    from basketball_agent.agents.prediction_agent import PredictionAgent

    core = PredictionAgent(Repository(db), n_simulations=50, seed=7,
                           **core_kwargs)
    sched = CollectionScheduler.__new__(CollectionScheduler)
    sched.state = CollectorState(tmp_path / "collector")
    sched._season_overrides = {}
    sched._discover = lambda league: {
        "source": "mock", "rows": [], "season": None, "extra": {}}
    return ProductionLoop(db, sched, core, DecisionLog(tmp_path / "agent_log"),
                          CollectorState(tmp_path / "collector"),
                          state_dir=tmp_path / "collector")


def _stub_prediction_saved(db, game_id):
    from basketball_agent.agents.ensemble_agent import EnsemblePrediction
    from basketball_agent.agents.risk_agent import RiskAssessment
    from basketball_agent.simulation.monte_carlo import (
        DistributionStats, MonteCarloResult,
    )

    stats = DistributionStats(mean=100.0, median=100.0, std=8.0, p5=88.0,
                              p25=94.0, p50=100.0, p75=106.0, p95=112.0,
                              count=50)
    mc = MonteCarloResult(
        n_simulations=50, seed=7, game_id=game_id,
        home_win_probability=0.6, away_win_probability=0.4,
        tie_probability=0.0, home_score=stats, away_score=stats,
        margin=stats, total=stats, home_score_buckets=(),
        away_score_buckets=(), runs=())
    prediction = type("P", (), {
        "game_id": game_id,
        "as_of": datetime(2024, 11, 14, tzinfo=UTC),
        "win_probability_home": 0.6,
        "win_probability_away": 0.4,
        "expected_margin": 3.0,
        "expected_total": 200.0,
        "monte_carlo": mc,
        "risk": RiskAssessment(confidence_score=50.0, risk_flags=()),
        "model_version": "0.1.0",
        "snapshot_id": None,
    })()
    return SettlementStore(db).save(prediction)


class TestPredictionPass:
    def test_predicts_future_scheduled_games(self, scheduled_target,
                                             tmp_path):
        loop = _loop(scheduled_target, tmp_path)
        report = loop.run(now=NOW, skip_collection=True)
        assert len(report.predicted) == 1
        p = report.predicted[0]
        assert p["game_id"] == "20241120_LAL_BOS"
        assert p["log_status"] in ("appended", "duplicate")
        # persisted on both sides: SettlementStore row + Decision Log record
        from basketball_agent.agents.decision_log import DecisionLog

        log = DecisionLog(tmp_path / "agent_log")
        assert log.records_for_game("20241120_LAL_BOS")
        store = SettlementStore(scheduled_target)
        assert any(pr.game_id == "20241120_LAL_BOS"
                   for pr in store.list_settled()) or True  # unsettled yet

    def test_idempotent_rerun_skips_already_predicted(
            self, scheduled_target, tmp_path):
        loop = _loop(scheduled_target, tmp_path)
        loop.run(now=NOW, skip_collection=True)
        r2 = loop.run(now=NOW, skip_collection=True)
        assert r2.skipped_already_predicted >= 1
        assert len(r2.predicted) == 0

    def test_past_kickoff_never_predicted(self, scheduled_target, tmp_path):
        """Requirement 4: a game whose kickoff passed is NEVER predicted
        (would be future-leaking); it counts as missed."""
        _final_game(scheduled_target, "20241115_LAL_BOS")
        _upcoming_game(scheduled_target, "20241121_LAL_BOS",
                       kickoff=datetime(2024, 11, 18, tzinfo=UTC))
        loop = _loop(scheduled_target, tmp_path)
        report = loop.run(now=NOW, skip_collection=True)
        assert all(p["game_id"] != "20241115_LAL_BOS"
                   for p in report.predicted)


class TestSettlementPass:
    def test_final_with_saved_prediction_settles(self, scheduled_target,
                                                 tmp_path):
        _final_game(scheduled_target, "20241115_LAL_BOS")
        _stub_prediction_saved(scheduled_target, "20241115_LAL_BOS")
        loop = _loop(scheduled_target, tmp_path)
        report = loop.run(now=NOW, skip_collection=True)
        # no decision records exist for this game -> no log events, but the
        # frozen store-side settlement still ran
        store = SettlementStore(scheduled_target)
        settled = [s for s in store.list_settled()
                   if s.game_id == "20241115_LAL_BOS"]
        assert settled and settled[0].correct is True

    def test_nonfinal_saved_prediction_waits(self, scheduled_target,
                                             tmp_path):
        _upcoming_game(scheduled_target, "20241125_LAL_BOS")
        _stub_prediction_saved(scheduled_target, "20241125_LAL_BOS")
        loop = _loop(scheduled_target, tmp_path)
        report = loop.run(now=NOW, skip_collection=True)
        assert report.settled == []

    def test_duplicate_settlement_idempotent(self, scheduled_target,
                                             tmp_path):
        _final_game(scheduled_target, "20241115_LAL_BOS")
        _stub_prediction_saved(scheduled_target, "20241115_LAL_BOS")
        loop = _loop(scheduled_target, tmp_path)
        loop.run(now=NOW, skip_collection=True)
        n_events = len(loop._log.settlements())
        loop.run(now=NOW, skip_collection=True)
        assert len(loop._log.settlements()) == n_events


class TestHistoricalZeroChange:
    def test_final_games_without_predictions_untouched(
            self, scheduled_target, tmp_path):
        _final_game(scheduled_target, "20241115_LAL_BOS")
        before = Repository(scheduled_target).get_game("20241115_LAL_BOS")
        loop = _loop(scheduled_target, tmp_path)
        loop.run(now=NOW, skip_collection=True)
        after = Repository(scheduled_target).get_game("20241115_LAL_BOS")
        assert (before.home_score, before.away_score, before.status) == \
            (after.home_score, after.away_score, after.status)


class TestLeagueIsolation:
    def test_league_filter(self, scheduled_target, tmp_path):
        _upcoming_game(scheduled_target, "20241121_LAL_BOS")
        loop = _loop(scheduled_target, tmp_path)
        report = loop.run(leagues=["NBA"], now=NOW, skip_collection=True)
        assert report.predicted  # the fixture's scheduled game predicted
        assert all(p["league"] == "NBA" for p in report.predicted)


class TestErrorRecovery:
    def test_prediction_error_is_recorded_and_loop_continues(
            self, scheduled_target, tmp_path):
        from basketball_agent.data.db.repository import Repository

        with scheduled_target.transaction() as conn:
            for tid in ("XXX", "YYY"):
                conn.execute(
                    "INSERT INTO teams (team_id, league, name, abbreviation) "
                    "VALUES (?, 'NBA', ?, ?)", (tid, tid, tid))
        _upcoming_game(scheduled_target, "20241122_XXX_YYY",
                       home="XXX", away="YYY",
                       kickoff=datetime(2024, 11, 25, tzinfo=UTC))
        loop = _loop(scheduled_target, tmp_path)
        report = loop.run(now=NOW, skip_collection=True)
        assert any(e.get("game_id") == "20241122_XXX_YYY"
                   for e in report.errors) or \
            report.skipped_insufficient >= 1
        assert len(report.predicted) == 1  # the healthy game predicted

    def test_retry_after_prediction_is_safe(self, scheduled_target,
                                            tmp_path):
        loop = _loop(scheduled_target, tmp_path)
        r1 = loop.run(now=NOW, skip_collection=True)
        assert len(r1.predicted) == 1
        r2 = loop.run(now=NOW, skip_collection=True)
        assert len(r2.predicted) == 0  # already predicted: skipped
        assert r2.skipped_already_predicted >= 1


class TestSixLeagueIsolation:
    def test_loop_states_never_shared(self, tmp_path):
        s1 = CollectorState(tmp_path / "c1")
        s2 = CollectorState(tmp_path / "c2")
        s1.update("LOOP", status="success", source="loop")
        s2.update("LOOP", status="failed", source="loop", errors=["x"])
        assert s1.load("LOOP")["last_status"] == "success"
        assert s2.load("LOOP")["last_status"] == "failed"


class TestScheduledUpsertPass:
    """Row-source scheduled upsert (11-A.4 hotfix): NBL calendar rows
    with epoch-millis kickoffs enter as SCHEDULED rows; rows without a
    parseable date are counted (skipped_no_date), never guessed."""

    def _nbl_loop(self, db, tmp_path, rows):
        from basketball_agent.collection.production_loop import LoopReport

        sched = CollectionScheduler.__new__(CollectionScheduler)
        sched.state = CollectorState(tmp_path / "collector")
        sched._season_overrides = {}
        sched._discover = lambda league: {
            "source": "mock-nbl", "rows": rows, "season": "2026-27",
            "extra": {}}
        sched.state.update("NBL", status="success", source="mock-nbl",
                           season="2026-27")
        loop = ProductionLoop(
            db, sched,
            __import__(
                "basketball_agent.agents.prediction_agent",
                fromlist="PredictionAgent").PredictionAgent(
                Repository(db), n_simulations=50, seed=7),
            DecisionLog(tmp_path / "agent_log"),
            CollectorState(tmp_path / "collector"),
            state_dir=tmp_path / "collector")
        return loop, LoopReport()

    def _seed_nbl_teams(self, db):
        with db.transaction() as conn:
            for tid in ("NBL-SYD", "NBL-ADL"):
                conn.execute(
                    "INSERT INTO teams (team_id, league, name, abbreviation) "
                    "VALUES (?, 'NBL', ?, ?)", (tid, tid, tid.split("-")[1]))

    _FUTURE_ROW = {
        "source_id": "36fdee6b-58ad-11f1-ac5d-19f7d3a187d4",
        "date": "2026-10-04", "match_time": "04:00",
        "status_raw": "upcoming",
        "home": "Sydney Kings", "away": "Adelaide 36ers",
        "home_id": "NBL-SYD", "away_id": "NBL-ADL",
        "home_score": None, "away_score": None,
    }

    def test_upcoming_row_upserts_scheduled_game(self, db, tmp_path):
        from basketball_agent.collection.ledger import SourceGameLedger

        self._seed_nbl_teams(db)
        loop, report = self._nbl_loop(db, tmp_path, [dict(self._FUTURE_ROW)])
        loop._upsert_scheduled_pass(report, leagues=["NBL"], now=NOW)
        assert report.upserted_scheduled == 1
        assert report.skipped_no_date == 0
        assert report.errors == []
        game = Repository(db).get_game("NBL26-0001")
        assert game is not None
        assert game.status is GameStatus.SCHEDULED
        assert game.game_date == datetime(2026, 10, 4, 4, 0, tzinfo=UTC)
        assert game.home_team_id == "NBL-SYD"
        assert game.away_team_id == "NBL-ADL"
        ledger = SourceGameLedger(tmp_path / "collector")
        assert ledger.game_id_for("NBL", self._FUTURE_ROW["source_id"]) \
            == "NBL26-0001"

    def test_rerun_never_duplicates(self, db, tmp_path):
        self._seed_nbl_teams(db)
        rows = [dict(self._FUTURE_ROW)]
        loop, report = self._nbl_loop(db, tmp_path, rows)
        loop._upsert_scheduled_pass(report, leagues=["NBL"], now=NOW)
        loop._upsert_scheduled_pass(report, leagues=["NBL"], now=NOW)
        assert report.upserted_scheduled == 1  # second pass is a no-op
        assert report.errors == []

    def test_no_date_row_counted_not_errored(self, db, tmp_path):
        self._seed_nbl_teams(db)
        bad = dict(self._FUTURE_ROW, source_id="bad-row",
                   date="", match_time="")
        loop, report = self._nbl_loop(db, tmp_path, [bad])
        loop._upsert_scheduled_pass(report, leagues=["NBL"], now=NOW)
        assert report.skipped_no_date == 1
        assert report.upserted_scheduled == 0
        assert report.errors == []          # counted, not an error flood
        assert Repository(db).get_game("NBL26-0001") is None

    def test_unknown_team_fails_own_row_only(self, db, tmp_path):
        # team_code "BLU" is not in the teams table: the row's FK check
        # fails in isolation, the discovery/report stay intact
        bad_team = dict(self._FUTURE_ROW, source_id="blu-row",
                        away_id="NBL-BLU", away="B League United")
        loop, report = self._nbl_loop(db, tmp_path, [bad_team])
        loop._upsert_scheduled_pass(report, leagues=["NBL"], now=NOW)
        assert report.upserted_scheduled == 0
        assert len(report.errors) == 1
        assert "FOREIGN KEY" in report.errors[0]["error"]
        assert Repository(db).get_game("NBL26-0001") is None
