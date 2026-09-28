"""Production collection layer tests (Phase 10-E).

All discovery is mocked — no network in tests, no real data mutation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.collection.scheduler import (
    CollectionScheduler,
    refresh_season,
)
from basketball_agent.collection.state import CollectorState
from basketball_agent.data.db.repository import Repository


def _rows(n=2, prefix="m", status="1"):
    return [{"source_id": f"{prefix}{i}", "date": "2026-09-2" + str(i),
             "status_raw": status, "home": "A", "away": "B",
             "home_score": 90 + i, "away_score": 85 + i}
            for i in range(n)]


def _scheduler(monkeypatch, tmp_path, discover: dict):
    sched = CollectionScheduler.__new__(CollectionScheduler)
    sched.state = CollectorState(tmp_path / "collector")
    sched._bundle_dir = tmp_path / "bundles"
    sched._season_overrides = {}

    def _discover(league):
        item = discover[league]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sched, "_discover", _discover)
    return sched


class TestCollectorState:

    def test_heartbeat_and_isolation(self, tmp_path):
        state = CollectorState(tmp_path / "collector")
        state.update("BSL", status="failed", source="tbf", errors=["x"])
        state.update("CBA", status="success", source="cba", new_games=3)
        bsl = state.load("BSL")
        cba = state.load("CBA")
        assert bsl["last_status"] == "failed"
        assert cba["last_status"] == "success" and cba["last_new_games"] == 3
        assert bsl["heartbeat"] and cba["heartbeat"]

    def test_success_does_not_regress(self, tmp_path):
        state = CollectorState(tmp_path / "collector")
        state.update("BSL", status="success", source="tbf")
        first = state.load("BSL")["last_success_at"]
        state.update("BSL", status="failed", source="tbf", errors=["e"])
        assert state.load("BSL")["last_success_at"] == first
        assert state.load("BSL")["last_error"] == "e"

    def test_seen_games_merge_with_isolation(self, tmp_path):
        state = CollectorState(tmp_path / "collector")
        state.update_seen_games("BSL", {"m1": "aa"})
        state.update_seen_games("BSL", {"m2": "bb"})
        state.update_seen_games("CBA", {"c1": "dd"})
        assert state.seen_games("BSL") == {"m1": "aa", "m2": "bb"}
        assert state.seen_games("CBA") == {"c1": "dd"}

    def test_run_history_capped(self, tmp_path):
        from basketball_agent.collection.state import RUN_HISTORY_LIMIT

        state = CollectorState(tmp_path / "collector")
        for _ in range(RUN_HISTORY_LIMIT + 5):
            state.update("BSL", status="success", source="x")
        assert len(state.load("BSL")["runs"]) == RUN_HISTORY_LIMIT

    def test_season_ingested_stamp(self, tmp_path):
        state = CollectorState(tmp_path / "collector")
        state.mark_season_ingested("BSL", "2026-27", "snap123")
        stamped = state.load("BSL")["seasons_ingested"][0]
        assert stamped["season"] == "2026-27"
        assert stamped["snapshot_id"] == "snap123"


class TestSchedulerIdempotency:
    def test_second_run_no_change(self, monkeypatch, tmp_path):
        sched = _scheduler(monkeypatch, tmp_path,
                           {"BSL": {"source": "tbf", "rows": _rows(),
                                    "season": "2026-2027"}})
        r1 = sched.run(leagues=["BSL"])[0]
        assert r1.new_games == 2
        r2 = sched.run(leagues=["BSL"])[0]
        assert r2.status == "no_change"
        assert r2.new_games == 0 and r2.updated_games == 0

    def test_updates_detected(self, monkeypatch, tmp_path):
        rows = _rows()
        _scheduler(monkeypatch, tmp_path,
                   {"BSL": {"source": "tbf", "rows": rows,
                            "season": "2026-2027"}}).run(leagues=["BSL"])
        rows[0]["home_score"] = 99  # score correction on a seen game
        r2 = _scheduler(monkeypatch, tmp_path,
                        {"BSL": {"source": "tbf", "rows": rows,
                                 "season": "2026-2027"}}).run(leagues=["BSL"])[0]
        assert r2.updated_games == 1 and r2.new_games == 0

    def test_scheduled_to_final_transition(self, monkeypatch, tmp_path):
        rows = _rows(n=1, status="2")  # scheduled/live
        _scheduler(monkeypatch, tmp_path,
                   {"BSL": {"source": "tbf", "rows": rows,
                            "season": "2026-2027"}}).run(leagues=["BSL"])
        rows[0]["status_raw"] = "1"
        rows[0]["home_score"] = 90
        rows[0]["away_score"] = 85
        r2 = _scheduler(monkeypatch, tmp_path,
                        {"BSL": {"source": "tbf", "rows": rows,
                                 "season": "2026-2027"}}).run(leagues=["BSL"])[0]
        assert r2.updated_games == 1  # Scheduled -> Final fingerprint change


class TestPartialFailure:
    def test_one_league_failure_isolated(self, monkeypatch, tmp_path):
        results = _scheduler(monkeypatch, tmp_path, {
            "BSL": RuntimeError("network down"),
            "CBA": {"source": "cba", "rows": [], "season": "2026-27"},
            "NBL": {"source": "nbl", "rows": _rows(n=3),
                    "season": "2026-27"},
        }).run(leagues=["BSL", "CBA", "NBL"])
        by = {r.league: r for r in results}
        assert by["BSL"].status == "failed" and by["BSL"].errors
        assert by["CBA"].status == "partial"
        assert by["CBA"].validation and "zero games" in by["CBA"].validation
        assert by["NBL"].status == "success" and by["NBL"].new_games == 3
        state = CollectorState(tmp_path / "collector")
        assert state.load("BSL")["last_status"] == "failed"
        assert state.load("NBL")["last_status"] == "success"

    def test_failure_recorded_with_error_and_no_success(self, monkeypatch,
                                                        tmp_path):
        _scheduler(monkeypatch, tmp_path, {
            "BSL": RuntimeError("TLS drop"),
        }).run(leagues=["BSL"])
        s = CollectorState(tmp_path / "collector").load("BSL")
        assert s["last_status"] == "failed"
        assert "TLS drop" in s["last_errors"][0]
        assert "last_success_at" not in s  # never succeeded


class TestValidationAnomaly:
    def test_zero_rows_marked_anomaly(self, monkeypatch, tmp_path):
        r = _scheduler(monkeypatch, tmp_path, {
            "CBA": {"source": "cba", "rows": [], "season": "2026-27"},
        }).run(leagues=["CBA"])[0]
        assert r.validation and "zero games" in r.validation
        assert r.status == "partial"

    def test_manual_refresh_collectors_skip_zero_row_anomaly(
            self, monkeypatch, tmp_path):
        r = _scheduler(monkeypatch, tmp_path, {
            "NBA": {"source": "parquet", "rows": [], "manual_refresh": True},
        }).run(leagues=["NBA"])[0]
        assert r.validation is None  # file-refresh source: zero rows is normal
        assert r.status == "no_change"


class TestSeasonRefreshGate:
    def test_ingested_season_refused(self, tmp_path):
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.schemas.base import (
            Competition, GameStatus, League,
        )

        db = Database(tmp_path / "g.db")
        db.initialize()
        repo = Repository(db)
        with db.transaction() as conn:
            for tid in ("BSL-A", "BSL-B"):
                conn.execute(
                    "INSERT INTO teams (team_id, league, name, abbreviation) "
                    "VALUES (?, 'BSL', ?, ?)", (tid, tid, tid))
        repo.insert_game(type("G", (), {
            "game_id": "BSL26-0001", "league": League.BSL,
            "season": "2026-27",
            "game_date": datetime(2026, 9, 25, tzinfo=timezone.utc),
            "home_team_id": "BSL-A", "away_team_id": "BSL-B",
            "home_score": 90, "away_score": 89,
            "status": GameStatus.FINAL,
            "competition": Competition.REGULAR_SEASON,
        })())
        state = CollectorState(tmp_path / "collector")
        state.update("BSL", status="success", source="tbf",
                     season="2026-2027")
        result = refresh_season(repo, tmp_path / "collector", db, "BSL")
        # the TBF label "2026-2027" is normalised to the core's "2026-27"
        assert result.status == "already_ingested"
        assert "already present" in result.errors[0]

    def test_unknown_season_fails_clearly(self, tmp_path):
        from basketball_agent.data.db.connection import Database

        db = Database(tmp_path / "g2.db")
        db.initialize()
        result = refresh_season(Repository(db), tmp_path / "collector", db,
                                "BSL")
        assert result.status == "failed"
        assert "no season known" in result.errors[0]

    def test_unsupported_league(self, tmp_path):
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.repository import Repository

        db = Database(tmp_path / "g3.db")
        db.initialize()
        state = CollectorState(tmp_path / "collector")
        state.update("NBA", status="success", source="parquet",
                     season="2025-26")
        result = refresh_season(Repository(db), tmp_path / "collector", db,
                                "NBA")
        assert result.status == "unsupported"
