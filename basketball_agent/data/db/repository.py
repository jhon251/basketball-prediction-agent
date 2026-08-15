"""Repository layer — the only code path that touches SQL.

Design rules:

* every read used for modeling must go through an ``as_of`` timestamp filter
  (structural leakage guard);
* writes accept schema-validated models only.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from basketball_agent.data.db.connection import Database
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.schemas.game import (
    Game,
    PlayerAvailability,
    PlayerGameStats,
    TeamGameStats,
)
from basketball_agent.data.schemas.player import Player
from basketball_agent.data.schemas.team import Team


def _parse_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _game_from_row(row: sqlite3.Row) -> Game:
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

    # ------------------------------------------------------------------ writes

    def upsert_team(self, team: Team) -> None:
        with self._db.transaction() as conn:
            self._upsert_team(conn, team)

    def upsert_player(self, player: Player) -> None:
        with self._db.transaction() as conn:
            self._upsert_player(conn, player)

    def insert_game(self, game: Game, inserted_at: datetime | None = None) -> None:
        inserted_at = inserted_at or datetime.now(timezone.utc)
        with self._db.transaction() as conn:
            self._insert_game(conn, game, inserted_at)

    def insert_team_stats(self, ts: TeamGameStats) -> None:
        with self._db.transaction() as conn:
            self._insert_team_stats(conn, ts)

    def insert_player_stats(self, ps: PlayerGameStats) -> None:
        with self._db.transaction() as conn:
            self._insert_player_stats(conn, ps)

    def insert_availability(self, a: PlayerAvailability) -> None:
        with self._db.transaction() as conn:
            self._insert_availability(conn, a)

    def load_dataset(self, dataset: Dataset, inserted_at: datetime | None = None) -> int:
        """Write a whole validated dataset in ONE transaction.

        Returns the number of rows written.  Any failure rolls back every row.
        """
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
            "home_score, away_score, status, inserted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (game.game_id, game.league.value, game.season, game.game_date.isoformat(),
             game.home_team_id, game.away_team_id, game.home_score, game.away_score,
             game.status.value, inserted_at.isoformat()),
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

    def get_player(self, player_id: str) -> Player | None:
        with self._db.session() as conn:
            row = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
        return _player_from_row(row) if row else None

    def get_game(self, game_id: str) -> Game | None:
        with self._db.session() as conn:
            row = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
        return _game_from_row(row) if row else None

    def list_games(
        self,
        *,
        as_of: datetime | None = None,
        league: League | None = None,
        status: GameStatus | None = None,
    ) -> list[Game]:
        """List games; ``as_of`` excludes any game dated after the timestamp."""
        sql = "SELECT * FROM games WHERE 1=1"
        params: list = []
        if as_of is not None:
            sql += " AND game_date <= ?"
            params.append(as_of.isoformat())
        if league is not None:
            sql += " AND league = ?"
            params.append(league.value)
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        sql += " ORDER BY game_date ASC, game_id ASC"
        with self._db.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_game_from_row(r) for r in rows]

    def list_team_stats(
        self,
        *,
        game_id: str | None = None,
        team_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[TeamGameStats]:
        sql = "SELECT * FROM team_game_stats WHERE 1=1"
        params: list = []
        if game_id is not None:
            sql += " AND game_id = ?"
            params.append(game_id)
        if team_id is not None:
            sql += " AND team_id = ?"
            params.append(team_id)
        if as_of is not None:
            sql += " AND game_id IN (SELECT game_id FROM games WHERE game_date <= ?)"
            params.append(as_of.isoformat())
        sql += " ORDER BY game_id ASC, team_id ASC"
        with self._db.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_team_stats_from_row(r) for r in rows]

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
