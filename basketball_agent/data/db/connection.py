"""Thin SQLite connection manager (stdlib only).

SQLite is the V1 storage engine: zero-config, file-based (a snapshot is just
a file) and fully testable.  The repository layer isolates SQL so a server
database can replace it later without touching the models.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_SQL_PATH = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION = 3

# NBA game_id prefix -> competition (stats.nba.com convention, Phase 8-B2).
_GAME_ID_PREFIX_COMPETITION = {
    "002": "REGULAR_SEASON",
    "004": "PLAYOFF",
    "005": "PLAY_IN",
    "006": "CUP",
}


class Database:
    """Connection manager for one SQLite database file."""

    def __init__(self, path: Path | str) -> None:
        raw = str(path)
        self._in_memory = raw == ":memory:"
        self.path = None if self._in_memory else Path(raw)
        self.schema_sql_path = SCHEMA_SQL_PATH

    def connect(self) -> sqlite3.Connection:
        target = ":memory:" if self._in_memory else str(self.path)
        if not self._in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        """Create the schema (idempotent), migrate older files, stamp version."""
        ddl = self.schema_sql_path.read_text(encoding="utf-8")
        conn = self.connect()
        try:
            conn.executescript(ddl)
            self._migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Idempotent in-place migrations.  Never deletes rows: legacy games
        without a recognizable prefix keep competition NULL (untagged)."""
        columns = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
        if "competition" not in columns:
            conn.execute("ALTER TABLE games ADD COLUMN competition TEXT")
        # backfill untagged NBA games from the game_id prefix; unknown prefixes
        # stay NULL — no silent guessing
        for prefix, competition in _GAME_ID_PREFIX_COMPETITION.items():
            conn.execute(
                "UPDATE games SET competition = ? WHERE competition IS NULL "
                "AND league = 'NBA' AND substr(game_id, 1, 3) = ?",
                (competition, prefix),
            )

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """Read/write session: commits on success, rolls back on error, always closes."""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit name for a write transaction (same semantics as session)."""
        with self.session() as conn:
            yield conn
