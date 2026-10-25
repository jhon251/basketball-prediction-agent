"""Repository layer — the only code path that touches SQL.

Design rules:

* every read used for modeling must go through an ``as_of`` timestamp filter
  (structural leakage guard);
* writes accept schema-validated models only.

Reads are served from a per-``Database`` immutable-data cache (Phase 8-B2):
games and team-stat rows never change after ingestion, so they are loaded
once per Database instance and every ``as_of`` read is a slice/filter over
the cached, already-sorted lists.  This is a pure performance change — the
returned objects, values and ordering are identical to the SQL queries.
Any write through the same Database invalidates the cache.
"""
from __future__ import annotations

import bisect
import sqlite3
from datetime import datetime, timezone

from basketball_agent.data.db.connection import Database
from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.schemas.game import (
    Game,
    PlayerAvailability,
    PlayerGameStats,
    TeamGameStats,
)
from basketball_agent.data.schemas.player import Player
from basketball_agent.data.schemas.team import Team


class _ReadOnlyCache:
    """Immutable-row cache shared by all Repository instances of one Database."""

    def __init__(self) -> None:
        self.games: list[Game] | None = None            # sorted (game_date, game_id)
        self.games_by_id: dict[str, Game] | None = None
        self.team_stats: list[TeamGameStats] | None = None  # sorted (game_id, team_id)
        self.stats_dates: list[datetime] | None = None      # parallel to team_stats

    def invalidate(self) -> None:
        self.games = None
        self.games_by_id = None
        self.team_stats = None
        self.stats_dates = None


def _parse_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _game_from_row(row: sqlite3.Row) -> Game:
    keys = row.keys()
    return Game(
        game_id=row["game_id"],
        league=League(row["league"]),
        season=row["season"],
        game_date=_parse_dt(row["game_date"]),
        home_team_id=row["home_team_id"],
        away_team_id=row["away_team_id"],
        home_score=row["home_score"],
        away_score=row["away_score"],
        status=GameStatus(row["status"]),
        competition=(Competition(row["competition"])
                     if "competition" in keys and row["competition"] else None),
    )


def _team_from_row(row: sqlite3.Row) -> Team:
    return Team(
        team_id=row["team_id"],
        league=League(row["league"]),
        name=row["name"],
        abbreviation=row["abbreviation"],
    )


def _player_from_row(row: sqlite3.Row) -> Player:
    return Player(
        player_id=row["player_id"],
        league=League(row["league"]),
        name=row["name"],
        current_team_id=row["current_team_id"],
    )


def _team_stats_from_row(row: sqlite3.Row) -> TeamGameStats:
    return TeamGameStats(
        game_id=row["game_id"],
        team_id=row["team_id"],
        is_home=bool(row["is_home"]),
        minutes=row["minutes"],
        points=row["points"],
        possessions=row["possessions"],
        fg=row["fg"], fga=row["fga"], fg3=row["fg3"], fg3a=row["fg3a"],
        ft=row["ft"], fta=row["fta"],
        orb=row["orb"], drb=row["drb"], trb=row["trb"],
        ast=row["ast"], tov=row["tov"], stl=row["stl"], blk=row["blk"], pf=row["pf"],
    )


def _player_stats_from_row(row: sqlite3.Row) -> PlayerGameStats:
    return PlayerGameStats(
        game_id=row["game_id"],
        player_id=row["player_id"],
        team_id=row["team_id"],
        minutes=row["minutes"],
        points=row["points"],
        fga=row["fga"], fg3a=row["fg3a"], fta=row["fta"],
        rebounds=row["rebounds"], assists=row["assists"],
        turnovers=row["turnovers"], steals=row["steals"],
        blocks=row["blocks"], fouls=row["fouls"],
    )


def _availability_from_row(row: sqlite3.Row) -> PlayerAvailability:
    from basketball_agent.data.schemas.base import PlayerAvailabilityStatus

    return PlayerAvailability(
        player_id=row["player_id"],
        game_id=row["game_id"],
        status=PlayerAvailabilityStatus(row["status"]),
        availability_probability=row["availability_probability"],
        source_time=_parse_dt(row["source_time"]),
        source=row["source"],
    )


class Repository:
    """Typed read/write access to the V1 database."""

    def __init__(self, db: Database) -> None:
        self._db = db
        if not hasattr(db, "_read_cache"):
            db._read_cache = _ReadOnlyCache()
        self._cache: _ReadOnlyCache = db._read_cache

    def _invalidate(self) -> None:
        self._cache.invalidate()

    def _cached_games(self) -> list[Game]:
        if self._cache.games is None:
            with self._db.session() as conn:
                rows = conn.execute(
                    "SELECT * FROM games ORDER BY game_date ASC, game_id ASC"
                ).fetchall()
            self._cache.games = [_game_from_row(r) for r in rows]
            self._cache.games_by_id = {g.game_id: g for g in self._cache.games}
        return self._cache.games

    def _cached_team_stats(self) -> list[TeamGameStats]:
        if self._cache.team_stats is None:
            with self._db.session() as conn:
                rows = conn.execute(
                    "SELECT ts.* FROM team_game_stats ts "
                    "ORDER BY ts.game_id ASC, ts.team_id ASC"
                ).fetchall()
            self._cache.team_stats = [_team_stats_from_row(r) for r in rows]
            dates = {
                g.game_id: g.game_date for g in self._cached_games()
            }
            self._cache.stats_dates = [dates.get(ts.game_id) for ts in self._cache.team_stats]
        return self._cache.team_stats

    # ------------------------------------------------------------------ writes

    def upsert_team(self, team: Team) -> None:
        self._invalidate()
        with self._db.transaction() as conn:
            self._upsert_team(conn, team)

    def upsert_player(self, player: Player) -> None:
        self._invalidate()
        with self._db.transaction() as conn:
            self._upsert_player(conn, player)

    def insert_game(self, game: Game, inserted_at: datetime | None = None) -> None:
        ts = inserted_at or datetime.now(timezone.utc)
        with self._db.transaction() as conn:
            self._insert_game(conn, game, ts)
        self._invalidate()

    def update_game_result(
        self, game_id: str, *, home_score: int, away_score: int,
        status: GameStatus = GameStatus.FINAL,
    ) -> Game:
        """Attach a final score to an existing non-final game row.

        Phase 12-B settlement path for ESPN-sourced NBA scheduled games:
        the scoreboard reports final scores before the parquet season
        refresh lands.  Fail-loud guards: the row must exist, must not
        already be FINAL (a conflicting second result is a data problem,
        never silently overwritten), and scores must be non-negative.
        """
        if home_score < 0 or away_score < 0:
            raise ValueError(
                f"scores must be >= 0, got {home_score}-{away_score}")
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
            if row is None:
                raise LookupError(f"game {game_id!r} not found")
            current = _game_from_row(row)
            if current.status is GameStatus.FINAL:
                raise ValueError(
                    f"game {game_id!r} is already FINAL "
                    f"({current.home_score}-{current.away_score}); "
                    "refusing to overwrite with "
                    f"{home_score}-{away_score}")
            conn.execute(
                "UPDATE games SET home_score = ?, away_score = ?, "
                "status = ? WHERE game_id = ?",
                (home_score, away_score, status.value, game_id))
        self._invalidate()
        updated = self.get_game(game_id)
        assert updated is not None
        return updated
        self._invalidate()
        inserted_at = inserted_at or datetime.now(timezone.utc)
        with self._db.transaction() as conn:
            self._insert_game(conn, game, inserted_at)

    def insert_team_stats(self, ts: TeamGameStats) -> None:
        self._invalidate()
        with self._db.transaction() as conn:
            self._insert_team_stats(conn, ts)

    def insert_player_stats(self, ps: PlayerGameStats) -> None:
        self._invalidate()
        with self._db.transaction() as conn:
            self._insert_player_stats(conn, ps)

    def insert_availability(self, a: PlayerAvailability) -> None:
        self._invalidate()
        with self._db.transaction() as conn:
            self._insert_availability(conn, a)

    def load_dataset(self, dataset: Dataset, inserted_at: datetime | None = None) -> int:
        """Write a whole validated dataset in ONE transaction.

        Returns the number of rows written.  Any failure rolls back every row.
        """
        self._invalidate()
        inserted_at = inserted_at or datetime.now(timezone.utc)
        count = 0
        with self._db.transaction() as conn:
            for team in dataset.teams or ():
                self._upsert_team(conn, team)
                count += 1
            for player in dataset.players or ():
                self._upsert_player(conn, player)
                count += 1
            for game in dataset.games:
                self._insert_game(conn, game, inserted_at)
                count += 1
            for ts in dataset.team_stats:
                self._insert_team_stats(conn, ts)
                count += 1
            for ps in dataset.player_stats:
                self._insert_player_stats(conn, ps)
                count += 1
            for a in dataset.availability:
                self._insert_availability(conn, a)
                count += 1
        return count

    def _upsert_team(self, conn: sqlite3.Connection, team: Team) -> None:
        conn.execute(
            "INSERT INTO teams (team_id, league, name, abbreviation) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(team_id) DO UPDATE SET league=excluded.league, "
            "name=excluded.name, abbreviation=excluded.abbreviation",
            (team.team_id, team.league.value, team.name, team.abbreviation),
        )

    def _upsert_player(self, conn: sqlite3.Connection, player: Player) -> None:
        conn.execute(
            "INSERT INTO players (player_id, league, name, current_team_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(player_id) DO UPDATE SET league=excluded.league, "
            "name=excluded.name, current_team_id=excluded.current_team_id",
            (player.player_id, player.league.value, player.name, player.current_team_id),
        )

    def _insert_game(self, conn: sqlite3.Connection, game: Game, inserted_at: datetime) -> None:
        conn.execute(
            "INSERT INTO games (game_id, league, season, game_date, home_team_id, away_team_id, "
            "home_score, away_score, status, competition, inserted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (game.game_id, game.league.value, game.season, game.game_date.isoformat(),
             game.home_team_id, game.away_team_id, game.home_score, game.away_score,
             game.status.value,
             game.competition.value if game.competition else None,
             inserted_at.isoformat()),
        )

    def _insert_team_stats(self, conn: sqlite3.Connection, ts: TeamGameStats) -> None:
        conn.execute(
            "INSERT INTO team_game_stats (game_id, team_id, is_home, minutes, points, possessions, "
            "fg, fga, fg3, fg3a, ft, fta, orb, drb, trb, ast, tov, stl, blk, pf) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts.game_id, ts.team_id, int(ts.is_home), ts.minutes, ts.points, ts.possessions,
             ts.fg, ts.fga, ts.fg3, ts.fg3a, ts.ft, ts.fta,
             ts.orb, ts.drb, ts.trb, ts.ast, ts.tov, ts.stl, ts.blk, ts.pf),
        )

    def _insert_player_stats(self, conn: sqlite3.Connection, ps: PlayerGameStats) -> None:
        conn.execute(
            "INSERT INTO player_game_stats (game_id, player_id, team_id, minutes, points, "
            "fga, fg3a, fta, rebounds, assists, turnovers, steals, blocks, fouls) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ps.game_id, ps.player_id, ps.team_id, ps.minutes, ps.points,
             ps.fga, ps.fg3a, ps.fta, ps.rebounds, ps.assists,
             ps.turnovers, ps.steals, ps.blocks, ps.fouls),
        )

    def _insert_availability(self, conn: sqlite3.Connection, a: PlayerAvailability) -> None:
        conn.execute(
            "INSERT INTO player_availability (player_id, game_id, status, availability_probability, "
            "source, source_time) VALUES (?, ?, ?, ?, ?, ?)",
            (a.player_id, a.game_id, a.status.value, a.availability_probability,
             a.source, a.source_time.isoformat()),
        )

    # ------------------------------------------------------------------- reads

    def get_team(self, team_id: str) -> Team | None:
        with self._db.session() as conn:
            row = conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
        return _team_from_row(row) if row else None

    def list_teams(self) -> list[Team]:
        """All teams, ordered by team_id (Phase 12-C API layer)."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM teams ORDER BY team_id ASC").fetchall()
        return [_team_from_row(r) for r in rows]

    def get_player(self, player_id: str) -> Player | None:
        with self._db.session() as conn:
            row = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
        return _player_from_row(row) if row else None

    def list_players(self) -> list[Player]:
        """All players, ordered by player_id (Phase 12-G ingest)."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM players ORDER BY player_id ASC").fetchall()
        return [_player_from_row(r) for r in rows]

    def get_game(self, game_id: str) -> Game | None:
        self._cached_games()
        return self._cache.games_by_id.get(game_id)

    def list_games(
        self,
        *,
        as_of: datetime | None = None,
        league: League | None = None,
        status: GameStatus | None = None,
        competition: Competition | None = None,
        exclude_competitions: tuple[Competition, ...] | None = None,
    ) -> list[Game]:
        """List games; ``as_of`` excludes any game dated after the timestamp.

        ``competition`` / ``exclude_competitions`` filter the within-league
        segment; untagged legacy rows (competition IS NULL) are only returned
        when ``competition`` is not used as a positive filter.
        """
        excluded = {c.value for c in exclude_competitions} if exclude_competitions else None
        out = []
        for g in self._cached_games():
            if as_of is not None and g.game_date > as_of:
                continue
            if league is not None and g.league is not league:
                continue
            if status is not None and g.status is not status:
                continue
            if competition is not None and (
                g.competition is None or g.competition is not competition
            ):
                continue
            if excluded is not None and g.competition is not None \
                    and g.competition.value in excluded:
                continue
            out.append(g)
        return out

    def list_team_stats(
        self,
        *,
        game_id: str | None = None,
        team_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[TeamGameStats]:
        out = []
        for i, ts in enumerate(self._cached_team_stats()):
            if game_id is not None and ts.game_id != game_id:
                continue
            if team_id is not None and ts.team_id != team_id:
                continue
            if as_of is not None:
                d = self._cache.stats_dates[i]
                if d is None or d > as_of:
                    continue
            out.append(ts)
        return out

    def list_player_stats(
        self,
        *,
        game_id: str | None = None,
        player_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[PlayerGameStats]:
        sql = "SELECT * FROM player_game_stats WHERE 1=1"
        params: list = []
        if game_id is not None:
            sql += " AND game_id = ?"
            params.append(game_id)
        if player_id is not None:
            sql += " AND player_id = ?"
            params.append(player_id)
        if as_of is not None:
            sql += " AND game_id IN (SELECT game_id FROM games WHERE game_date <= ?)"
            params.append(as_of.isoformat())
        sql += " ORDER BY game_id ASC, player_id ASC"
        with self._db.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_player_stats_from_row(r) for r in rows]

    def list_availability(
        self,
        *,
        game_id: str | None = None,
        player_id: str | None = None,
        known_at: datetime | None = None,
    ) -> list[PlayerAvailability]:
        """List availability records; ``known_at`` excludes information
        published after that moment (temporal integrity guard)."""
        sql = "SELECT * FROM player_availability WHERE 1=1"
        params: list = []
        if game_id is not None:
            sql += " AND game_id = ?"
            params.append(game_id)
        if player_id is not None:
            sql += " AND player_id = ?"
            params.append(player_id)
        if known_at is not None:
            sql += " AND source_time <= ?"
            params.append(known_at.isoformat())
        sql += " ORDER BY game_id ASC, player_id ASC, source_time ASC"
        with self._db.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_availability_from_row(r) for r in rows]

    def game_count(self) -> int:
        with self._db.session() as conn:
            return conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]

    def min_max_game_date(self) -> tuple[datetime | None, datetime | None]:
        with self._db.session() as conn:
            row = conn.execute("SELECT MIN(game_date), MAX(game_date) FROM games").fetchone()
        return _parse_dt(row[0]), _parse_dt(row[1])
