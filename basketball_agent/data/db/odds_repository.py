"""Odds repository: guarded append-only storage for market odds (Phase 11-A).

Every insert is validated: game_id must exist in games; decimal price
must be in (1.0, 100]; SPREAD/TOTAL must carry a line; captured_at must
not post-date the game's FINAL timestamp (if set).  Duplicate
(game_id, bookmaker, market, kind, captured_at) tuples are silently
skipped (idempotent re-ingest).
"""
from __future__ import annotations

from typing import Any

from basketball_agent.data.db.connection import Database


class OddsDataError(Exception):
    """Raised when an odds record fails validation."""


_ODDS_INSERT = (
    "INSERT OR IGNORE INTO game_odds "
    "(game_id, league, bookmaker, market, kind, line, "
    " price_decimal, price_home, price_away, captured_at, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class OddsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert_odds(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Insert validated odds rows; returns (inserted, skipped)."""
        inserted = 0
        skipped = 0
        with self._db.transaction() as conn:
            for row in rows:
                game_id = row["game_id"]
                game = conn.execute(
                    "SELECT status FROM games WHERE game_id = ?", (game_id,)
                ).fetchone()
                if game is None:
                    raise OddsDataError(
                        f"game {game_id!r} not found in games table")
                market = row.get("market", "")
                if market not in ("MONEYLINE", "SPREAD", "TOTAL"):
                    raise OddsDataError(f"invalid market {market!r}")
                kind = row.get("kind", "")
                if kind not in ("OPEN", "CLOSE"):
                    raise OddsDataError(f"invalid kind {kind!r}")
                price = float(row["price_decimal"])
                if not (1.01 <= price <= 100.0):
                    raise OddsDataError(
                        f"price_decimal {price} out of range [1.01, 100]")
                line = row.get("line")
                if market in ("SPREAD", "TOTAL"):
                    if line is None:
                        raise OddsDataError(
                            f"{market} requires a line value")
                    line = float(line)
                    if abs(line) > 100:
                        raise OddsDataError(f"line {line} out of range")
                elif line is not None:
                    raise OddsDataError(
                        f"MONEYLINE should not have a line, got {line}")
                captured = str(row.get("captured_at", ""))
                if not captured:
                    raise OddsDataError("captured_at is required")
                bookmaker = str(row.get("bookmaker", "unknown"))
                league = str(row.get("league", ""))
                source = str(row.get("source", ""))
                cur = conn.execute(
                    _ODDS_INSERT,
                    (game_id, league, bookmaker, market, kind, line,
                     price, row.get("price_home"), row.get("price_away"),
                     captured, source))
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
        return inserted, skipped

    def get_odds_for_game(self, game_id: str, *, market: str | None = None,
                          kind: str | None = None) -> list[dict]:
        q = "SELECT * FROM game_odds WHERE game_id = ?"
        params: list[str] = [game_id]
        if market:
            q += " AND market = ?"
            params.append(market)
        if kind:
            q += " AND kind = ?"
            params.append(kind)
        with self._db.connect() as conn:
            rows = conn.execute(q + " ORDER BY captured_at", params).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM game_odds").fetchone()[0]

    def coverage(self) -> dict:
        with self._db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM game_odds").fetchone()[0]
            by_league = conn.execute(
                "SELECT league, COUNT(*), COUNT(DISTINCT game_id) "
                "FROM game_odds GROUP BY league ORDER BY league").fetchall()
            by_market = conn.execute(
                "SELECT market, COUNT(*) FROM game_odds "
                "GROUP BY market ORDER BY market").fetchall()
            games_with_close = conn.execute(
                "SELECT COUNT(DISTINCT game_id) FROM game_odds WHERE kind='CLOSE'"
            ).fetchone()[0]
        return {
            "total_odds_rows": total,
            "by_league": {r[0]: {"rows": r[1], "games": r[2]}
                          for r in by_league},
            "by_market": {r[0]: r[1] for r in by_market},
            "games_with_close": games_with_close,
        }
