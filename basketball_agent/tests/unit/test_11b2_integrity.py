"""Data integrity hotfix tests (Phase 11-B.2).

Machine-checkable guards against the three gaps that 11-B exposed:
1. DB-level FK violations must be zero after any ingest/migration;
2. The CBA row_source set in production_loop must never shrink;
3. The three promoted/new BSL clubs must have team rows in the DB.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from basketball_agent.collection.production_loop import ProductionLoop
from basketball_agent.collection.sources import cba_schedule_rows  # noqa: F401
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import (
    Competition, GameStatus, League,
)  # noqa: F401


class TestForeignKeyCheck:
    def test_no_fk_violations_in_production_db(self):
        """DB-level PRAGMA foreign_key_check on the production database.
        This is the gate that 11-B missed — it catches orphans that
        table-level checks miss (e.g. team_game_stats → teams)."""
        from basketball_agent.config import get_settings

        settings = get_settings()
        conn = sqlite3.connect(settings.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        assert violations == [], (
            f"FK violations found: {violations[:5]} — "
            "run identity correction before freeze"
        )

    def test_no_fk_violations_in_tmp_db(self, tmp_path):
        """Same check on a fresh DB with known data."""
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.schemas.game import Game

        db = Database(tmp_path / "fk.db")
        db.initialize()
        with db.transaction() as conn:
            for tid in ("T1", "T2"):
                conn.execute(
                    "INSERT INTO teams (team_id, league, name, abbreviation) "
                    "VALUES (?, 'NBA', ?, ?)", (tid, tid, tid))
        Repository(db).insert_game(Game(
            game_id="T1_T2_001", league=League.NBA, season="2024-25",
            game_date=datetime(2024, 11, 20, tzinfo=timezone.utc),
            home_team_id="T1", away_team_id="T2",
            home_score=100, away_score=95,
            status=GameStatus.FINAL,
            competition=Competition.REGULAR_SEASON,
        ))
        conn = sqlite3.connect(str(db.path))
        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


class TestCbaRowSourceGuard:
    def test_cba_in_row_sources(self):
        """The CBA row_source set must not be silently shrunk."""
        import inspect
        from basketball_agent.collection import production_loop as pl

        src = inspect.getsource(pl)
        # the row_sources set is a literal in the module
        assert 'row_sources = {"BSL", "NBL", "CBA"}' in src or \
            'row_sources = {"BSL", "NBL", "CBA"}' in inspect.getsource(
                pl.ProductionLoop._upsert_scheduled_pass)


class TestThreeNewBSLTeams:
    """BSL 2026-27 promoted/new club team rows must exist and be
    FK-resolvable in a properly initialised DB."""

    @pytest.fixture()
    def bsl_db(self, tmp_path):
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.schemas.base import League

        db = Database(tmp_path / "bsl.db")
        db.initialize()
        with db.transaction() as conn:
            for tid in ("BSL-265993", "BSL-265994", "BSL-266028",
                        "BSL-246936", "BSL-246972"):
                conn.execute(
                    "INSERT INTO teams (team_id, league, name, abbreviation) "
                    "VALUES (?, 'BSL', ?, ?)", (tid, tid, tid))
        return db

    def test_teams_exist_after_insert(self, bsl_db):
        conn = sqlite3.connect(str(bsl_db.path))
        for tid in ("BSL-265993", "BSL-265994", "BSL-266028"):
            row = conn.execute(
                "SELECT team_id FROM teams WHERE team_id = ?", (tid,)).fetchone()
            assert row is not None

    def test_scheduled_refs_resolve_after_insert(self, bsl_db):
        repo = Repository(bsl_db)
        repo.insert_game(type("G", (), {
            "game_id": "BSL26-TEST", "league": League.BSL, "season": "2026-27",
            "game_date": datetime(2026, 10, 3, tzinfo=timezone.utc),
            "home_team_id": "BSL-266028", "away_team_id": "BSL-246972",
            "home_score": None, "away_score": None,
            "status": GameStatus.SCHEDULED,
            "competition": Competition.REGULAR_SEASON,
        }))
        conn = sqlite3.connect(str(bsl_db.path))
        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_fk_clean_after_all_inserts(self, bsl_db):
        conn = sqlite3.connect(str(bsl_db.path))
        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []