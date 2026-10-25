"""Phase 12-B: Repository.update_game_result + NBA fixtures pass."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.collection.production_loop import ProductionLoop
from basketball_agent.agents.decision_log import DecisionLog
from basketball_agent.collection.scheduler import CollectionScheduler
from basketball_agent.collection.state import CollectorState
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _seed_nba_teams(db, monkeypatch):
    """Fresh DB needs the two NBA teams the ESPN rows reference (FK);
    the loop's injury step is stubbed so unit tests never touch the net."""
    from basketball_agent.data.schemas.base import League
    from basketball_agent.data.schemas.team import Team

    repo = Repository(db)
    for tid, name in (("MIL", "Milwaukee Bucks"),
                      ("CHI", "Chicago Bulls")):
        repo.upsert_team(Team(team_id=tid, league=League.NBA,
                              name=name, abbreviation=tid))

    from basketball_agent.data.collectors import espn_injuries

    class _Stub:
        availability_written = 0
        records = []
        skipped_unresolved_player = 0
        errors = []

    monkeypatch.setattr(espn_injuries, "refresh_injuries",
                        lambda db, **kw: _Stub())
    yield


def _row(source_id, *, status="0", home="MIL", away="CHI", season="2026-27",
         date="2026-10-23", time="20:00", hs="", as_="",
         competition="REGULAR_SEASON"):
    return {"source_id": source_id, "date": date, "match_time": time,
            "home_id": home, "away_id": away, "status_raw": status,
            "home_score": hs, "away_score": as_, "season": season,
            "competition": competition, "espn_state": "pre"}


def _loop(db, tmp_path, rows) -> ProductionLoop:
    from basketball_agent.agents.prediction_agent import PredictionAgent

    core = PredictionAgent(Repository(db), n_simulations=20, seed=7)
    sched = CollectionScheduler.__new__(CollectionScheduler)
    sched.state = CollectorState(tmp_path / "collector")
    sched._season_overrides = {}

    def _discover(league):
        if league == "NBA":
            return {"source": "mock-espn", "rows": rows, "season": None,
                    "extra": {}}
        return {"source": "mock", "rows": [], "season": None, "extra": {}}

    sched._discover = _discover
    return ProductionLoop(db, sched, core, DecisionLog(tmp_path / "agent_log"),
                          CollectorState(tmp_path / "collector"),
                          state_dir=tmp_path / "collector")


def _sched_game(db, game_id, *, kickoff=None, home="MIL", away="CHI",
                status=GameStatus.SCHEDULED, hs=None, as_=None):
    from basketball_agent.data.schemas.base import Competition, League

    Repository(db).insert_game(type("G", (), {
        "game_id": game_id, "league": League.NBA, "season": "2026-27",
        "game_date": kickoff or datetime(2026, 10, 23, 20, 0, tzinfo=UTC),
        "home_team_id": home, "away_team_id": away,
        "home_score": hs, "away_score": as_, "status": status,
        "competition": Competition.REGULAR_SEASON,
    }))


class TestUpdateGameResult:
    def test_scheduled_to_final(self, db):
        _sched_game(db, "NBA26-0001")
        game = Repository(db).update_game_result(
            "NBA26-0001", home_score=110, away_score=105)
        assert game.status is GameStatus.FINAL
        assert (game.home_score, game.away_score) == (110, 105)
        # read-back through the cache
        again = Repository(db).get_game("NBA26-0001")
        assert again.status is GameStatus.FINAL

    def test_already_final_refuses_overwrite(self, db):
        _sched_game(db, "NBA26-0001", status=GameStatus.FINAL, hs=1, as_=0)
        with pytest.raises(ValueError, match="already FINAL"):
            Repository(db).update_game_result(
                "NBA26-0001", home_score=110, away_score=105)

    def test_missing_game_fails(self, db):
        with pytest.raises(LookupError):
            Repository(db).update_game_result(
                "NOPE", home_score=1, away_score=0)

    def test_negative_score_fails(self, db):
        _sched_game(db, "NBA26-0001")
        with pytest.raises(ValueError, match=">= 0"):
            Repository(db).update_game_result(
                "NBA26-0001", home_score=-1, away_score=0)


class TestNbaFixturesPass:
    def test_upcoming_row_becomes_scheduled_game(self, db, tmp_path):
        loop = _loop(db, tmp_path, [_row("401")])
        from basketball_agent.collection.production_loop import LoopReport
        rep = LoopReport()
        loop._nba_fixtures_pass(rep, now=NOW)
        assert rep.nba_upserted == 1
        game = Repository(db).get_game("NBA26-0001")
        assert game is not None
        assert game.status is GameStatus.SCHEDULED
        assert (game.home_team_id, game.away_team_id) == ("MIL", "CHI")
        assert game.game_date == datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
        assert game.season == "2026-27"

    def test_rerun_never_duplicates(self, db, tmp_path):
        loop = _loop(db, tmp_path, [_row("401")])
        from basketball_agent.collection.production_loop import LoopReport
        for _ in range(2):
            rep = LoopReport()
            loop._nba_fixtures_pass(rep, now=NOW)
        assert rep.nba_upserted == 0
        games = [g for g in Repository(db).list_games()
                 if g.game_id.startswith("NBA26-")]
        assert len(games) == 1

    def test_known_final_row_flips_to_final(self, db, tmp_path):
        loop = _loop(db, tmp_path, [_row("401")])
        from basketball_agent.collection.production_loop import LoopReport
        rep = LoopReport()
        loop._nba_fixtures_pass(rep, now=NOW)  # creates NBA26-0001
        loop2 = _loop(db, tmp_path, [
            _row("401", status="1", hs="112", as_="108", time="")])
        # ledger is directory-backed: same tmp_path -> same ledger
        rep2 = LoopReport()
        loop2._nba_fixtures_pass(rep2, now=NOW)
        assert rep2.nba_finalized == 1
        assert rep2.nba_upserted == 0
        game = Repository(db).get_game("NBA26-0001")
        assert game.status is GameStatus.FINAL
        assert (game.home_score, game.away_score) == (112, 108)

    def test_unknown_final_row_skipped(self, db, tmp_path):
        loop = _loop(db, tmp_path, [
            _row("999", status="1", hs="100", as_="99")])
        from basketball_agent.collection.production_loop import LoopReport
        rep = LoopReport()
        loop._nba_fixtures_pass(rep, now=NOW)
        assert rep.nba_finalized == 0
        assert rep.nba_upserted == 0

    def test_row_without_season_counted_as_error(self, db, tmp_path):
        loop = _loop(db, tmp_path, [_row("401", season="")])
        from basketball_agent.collection.production_loop import LoopReport
        rep = LoopReport()
        loop._nba_fixtures_pass(rep, now=NOW)
        assert rep.errors and rep.errors[0]["stage"] == "nba_fixtures"
        assert rep.nba_upserted == 0

    def test_fetch_failure_isolated_to_report(self, db, tmp_path):
        loop = _loop(db, tmp_path, [])
        loop._scheduler._discover = lambda league: (
            _raise() if league == "NBA"
            else {"source": "mock", "rows": [], "season": None, "extra": {}})
        from basketball_agent.collection.production_loop import LoopReport
        rep = LoopReport()
        loop._nba_fixtures_pass(rep, now=NOW)  # must not raise
        assert rep.errors and "nba_fixtures" in rep.errors[0]["stage"]

    def test_playoff_competition_respected(self, db, tmp_path):
        loop = _loop(db, tmp_path, [_row("401", competition="PLAYOFF")])
        from basketball_agent.collection.production_loop import LoopReport
        rep = LoopReport()
        loop._nba_fixtures_pass(rep, now=NOW)
        game = Repository(db).get_game("NBA26-0001")
        assert game.competition.value == "PLAYOFF"


def _raise():
    raise RuntimeError("network down")
