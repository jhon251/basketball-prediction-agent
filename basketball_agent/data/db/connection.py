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
SCHEMA_VERSION = 1


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
        """Create the schema (idempotent) and stamp the schema version."""
        ddl = self.schema_sql_path.read_text(encoding="utf-8")
        conn = self.connect()
        try:
            conn.executescript(ddl)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

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
