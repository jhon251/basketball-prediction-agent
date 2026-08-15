"""Unit tests for the SQLite database and repository layer."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.data.db.connection import SCHEMA_VERSION, Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game
from basketball_agent.data.schemas.player import Player
from basketball_agent.data.schemas.team import Team

pytestmark = pytest.mark.unit

UTC = timezone.utc

EXPECTED_TABLES = {
    "schema_version", "teams", "players", "games",
    "team_game_stats", "player_game_stats", "player_availability",
}


def make_game(game_id="g1", game_date=None, status=GameStatus.FINAL,
              home_score=110, away_score=100) -> Game:
    return Game(
        game_id=game_id, league=League.NBA, season="2024-25",
        game_date=game_date or datetime(2024, 11, 6, tzinfo=UTC),
        home_team_id="LAL", away_team_id="BOS",
        home_score=home_score, away_score=away_score, status=status,
    )


@pytest.fixture
def repo(db) -> Repository:
    return Repository(db)


class TestDatabaseSetup:
    def test_initialize_creates_expected_tables(self, db):
        with db.session() as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert EXPECTED_TABLES <= names

    def test_schema_version_stamped(self, db):
        with db.session() as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION

    def test_initialize_is_idempotent(self, db):
        db.initialize()
        with db.session() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert rows == 1


class TestRepositoryWrites:
    def test_team_roundtrip(self, repo):
        team = Team(team_id="LAL", league=League.NBA, name="Lakers", abbreviation="LAL")
        repo.upsert_team(team)
        assert repo.get_team("LAL") == team

    def test_player_roundtrip(self, repo):
        repo.upsert_team(Team(team_id="LAL", league=League.NBA, name="Lakers", abbreviation="LAL"))
        player = Player(player_id="LAL-1", league=League.NBA, name="P1", current_team_id="LAL")
        repo.upsert_player(player)
        assert repo.get_player("LAL-1") == player

    def test_player_with_unknown_team_raises(self, repo):
        with pytest.raises(sqlite3.IntegrityError):
            repo.upsert_player(Player(player_id="LAL-1", league=League.NBA,
                                      name="P1", current_team_id="NOPE"))

    def test_game_roundtrip(self, repo):
        for tid in ("LAL", "BOS"):
            repo.upsert_team(Team(team_id=tid, league=League.NBA, name=tid, abbreviation=tid))
        game = make_game()
        repo.insert_game(game)
        loaded = repo.get_game("g1")
        assert loaded == game
        assert loaded.game_date.tzinfo is not None

    def test_duplicate_game_insert_raises(self, repo):
        for tid in ("LAL", "BOS"):
            repo.upsert_team(Team(team_id=tid, league=League.NBA, name=tid, abbreviation=tid))
        repo.insert_game(make_game())
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_game(make_game())

    def test_foreign_key_violation_raises(self, repo):
        # teams table is empty -> home_team_id reference fails
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_game(make_game())

    def test_upsert_updates_existing_team(self, repo):
        repo.upsert_team(Team(team_id="LAL", league=League.NBA, name="Old", abbreviation="OLD"))
        repo.upsert_team(Team(team_id="LAL", league=League.NBA, name="Lakers", abbreviation="LAL"))
        assert repo.get_team("LAL").name == "Lakers"


class TestRepositoryReads:
    @pytest.fixture
    def populated(self, repo, sample_teams, sample_games, sample_team_stats):
        for t in sample_teams:
            repo.upsert_team(t)
        for g in sample_games:
            repo.insert_game(g)
        for ts in sample_team_stats:
            repo.insert_team_stats(ts)
        return repo

    def test_list_games_as_of_filters_future(self, populated):
        cutoff = datetime(2024, 11, 10, tzinfo=UTC)  # g2 boundary (inclusive)
        games = populated.list_games(as_of=cutoff)
        assert {g.game_id for g in games} == {"20241106_LAL_BOS", "20241110_LAL_GSW"}

    def test_list_games_as_of_none_returns_all(self, populated):
        assert len(populated.list_games()) == 3

    def test_list_games_status_filter(self, populated):
        final = populated.list_games(status=GameStatus.FINAL)
        assert len(final) == 3

    def test_list_team_stats_as_of(self, populated):
        cutoff = datetime(2024, 11, 10, tzinfo=UTC)
        rows = populated.list_team_stats(as_of=cutoff)
        assert {r.game_id for r in rows} == {"20241106_LAL_BOS", "20241110_LAL_GSW"}
        assert len(rows) == 4

    def test_min_max_game_date(self, populated):
        mn, mx = populated.min_max_game_date()
        assert mn == datetime(2024, 11, 6, tzinfo=UTC)
        assert mx == datetime(2024, 11, 15, tzinfo=UTC)


class TestRepositoryTransactions:
    def test_load_dataset_is_atomic(self, repo, sample_dataset):
        broken = replace_games(sample_dataset, sample_dataset.games + (sample_dataset.games[0],))
        with pytest.raises(sqlite3.IntegrityError):
            repo.load_dataset(broken)
        # The whole transaction must have rolled back: even teams are gone.
        assert repo.get_team("LAL") is None
        assert repo.game_count() == 0

    def test_load_dataset_succeeds(self, repo, sample_dataset):
        count = repo.load_dataset(sample_dataset)
        expected = (len(sample_dataset.teams) + len(sample_dataset.players)
                    + len(sample_dataset.games) + len(sample_dataset.team_stats)
                    + len(sample_dataset.player_stats) + len(sample_dataset.availability))
        assert count == expected
        assert repo.game_count() == 3


def replace_games(ds, games):
    from dataclasses import replace
    return replace(ds, games=games)
