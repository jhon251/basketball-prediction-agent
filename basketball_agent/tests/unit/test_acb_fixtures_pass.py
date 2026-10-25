"""Phase 12-H: ACB fixtures pass (edition-id bridge + source-id scheme)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.collection.production_loop import (
    LoopReport, ProductionLoop,
)
from basketball_agent.agents.decision_log import DecisionLog
from basketball_agent.collection.scheduler import CollectionScheduler
from basketball_agent.collection.state import CollectorState
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

# edition-scoped ids (4475 etc.) bridged to stable clubIds via headers
BRIDGE = {"4475": {"club": 4, "name": "Surne Bilbao", "abbr": "SBB"},
          "4471": {"club": 10, "name": "Kids&Us Manresa", "abbr": "MAN"}}


def _row(sid, *, status="0", home="4475", away="4471", date="2026-10-25",
         time="11:00", hs="", as_=""):
    return {"source_id": sid, "date": date, "match_time": time,
            "home_id": home, "away_id": away, "status_raw": status,
            "home_score": hs, "away_score": as_, "season": "2026-27",
            "round": 6}


@pytest.fixture(autouse=True)
def _seed(monkeypatch):
    from basketball_agent.data.schemas.base import League
    from basketball_agent.data.schemas.team import Team

    def seed(db):
        repo = Repository(db)
        for code, name in (("SBB", "Surne Bilbao Basket"),
                           ("MAN", "Baxi Manresa")):
            repo.upsert_team(Team(team_id=f"ACB-{code}", league=League.ACB,
                                  name=name, abbreviation=code))
        return repo

    from basketball_agent.collection import sources
    monkeypatch.setattr(sources, "acb_edition_team_map",
                        lambda **kw: BRIDGE)
    yield seed


def _loop(db, tmp_path, rows) -> ProductionLoop:
    from basketball_agent.agents.prediction_agent import PredictionAgent

    core = PredictionAgent(Repository(db), n_simulations=20, seed=7)
    sched = CollectionScheduler.__new__(CollectionScheduler)
    sched.state = CollectorState(tmp_path / "collector")
    sched._season_overrides = {}

    def _discover(league):
        if league == "ACB":
            return {"source": "mock-acb", "rows": rows, "season": None,
                    "extra": {}}
        return {"source": "mock", "rows": [], "season": None, "extra": {}}

    sched._discover = _discover
    return ProductionLoop(db, sched, core, DecisionLog(tmp_path / "agent_log"),
                          CollectorState(tmp_path / "collector"),
                          state_dir=tmp_path / "collector")


class TestAcbFixturesPass:
    def test_upcoming_row_uses_source_id_scheme(self, db, tmp_path, _seed):
        _seed(db)
        loop = _loop(db, tmp_path, [_row("105412")])
        rep = LoopReport()
        loop._acb_fixtures_pass(rep, now=NOW)
        assert rep.acb_upserted == 1
        game = Repository(db).get_game("ACB2026-105412")
        assert game is not None
        assert game.status is GameStatus.SCHEDULED
        assert (game.home_team_id, game.away_team_id) == (
            "ACB-SBB", "ACB-MAN")
        assert game.season == "2026-27"

    def test_rerun_idempotent(self, db, tmp_path, _seed):
        _seed(db)
        loop = _loop(db, tmp_path, [_row("105412")])
        for _ in range(2):
            rep = LoopReport()
            loop._acb_fixtures_pass(rep, now=NOW)
        assert rep.acb_upserted == 0

    def test_known_final_row_settles(self, db, tmp_path, _seed):
        _seed(db)
        loop = _loop(db, tmp_path, [_row("105412")])
        rep = LoopReport()
        loop._acb_fixtures_pass(rep, now=NOW)
        loop2 = _loop(db, tmp_path, [
            _row("105412", status="1", hs="88", as_="81")])
        rep2 = LoopReport()
        loop2._acb_fixtures_pass(rep2, now=NOW)
        assert rep2.acb_finalized == 1
        game = Repository(db).get_game("ACB2026-105412")
        assert game.status is GameStatus.FINAL
        assert (game.home_score, game.away_score) == (88, 81)

    def test_unknown_final_row_skipped(self, db, tmp_path, _seed):
        _seed(db)
        loop = _loop(db, tmp_path, [
            _row("999", status="1", hs="80", as_="70")])
        rep = LoopReport()
        loop._acb_fixtures_pass(rep, now=NOW)
        assert rep.acb_finalized == 0 and rep.acb_upserted == 0

    def test_unbridged_edition_id_fails_own_row_only(self, db, tmp_path,
                                                     _seed):
        _seed(db)
        rows = [_row("1", home="9999"), _row("2")]
        loop = _loop(db, tmp_path, rows)
        rep = LoopReport()
        loop._acb_fixtures_pass(rep, now=NOW)
        assert rep.acb_upserted == 1  # the good row still lands
        assert rep.errors and rep.errors[0]["stage"] == "acb_fixtures"

    def test_discovery_failure_isolated(self, db, tmp_path, _seed):
        _seed(db)
        loop = _loop(db, tmp_path, [])
        def _boom(league):
            raise RuntimeError("net down")
        loop._scheduler._discover = _boom
        rep = LoopReport()
        loop._acb_fixtures_pass(rep, now=NOW)  # must not raise
        assert rep.errors and rep.errors[0]["stage"] == "acb_fixtures"
