"""Phase 8-B2 tests: competition schema/migration + windowed training."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from basketball_agent.data.db.connection import Database, _GAME_ID_PREFIX_COMPETITION
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import Competition, GameStatus
from basketball_agent.features import FeaturePipeline
from basketball_agent.evaluation.experiments import resolve_train_start

UTC = timezone.utc


class TestCompetitionMigration:
    def test_new_db_has_competition_column(self, tmp_path):
        db = Database(tmp_path / "new.db")
        db.initialize()
        with db.session() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
        assert "competition" in cols

    def test_migration_backfills_from_prefix(self, tmp_path):
        """An old (v2-style) games table is migrated in place and NBA game-id
        prefixes are backfilled; non-NBA-prefix rows stay NULL (untagged)."""
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        # minimal v2-style schema (no competition column)
        conn.executescript(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
            "CREATE TABLE teams (team_id TEXT PRIMARY KEY, league TEXT NOT NULL,"
            " name TEXT NOT NULL, abbreviation TEXT NOT NULL);"
            "CREATE TABLE games (game_id TEXT PRIMARY KEY, league TEXT NOT NULL,"
            " season TEXT NOT NULL, game_date TEXT NOT NULL, home_team_id TEXT NOT NULL"
            " REFERENCES teams(team_id), away_team_id TEXT NOT NULL REFERENCES teams(team_id),"
            " home_score INTEGER, away_score INTEGER, status TEXT NOT NULL,"
            " inserted_at TEXT NOT NULL);"
            "INSERT INTO teams VALUES ('LAL','NBA','Los Angeles Lakers','LAL'),"
            " ('BOS','NBA','Boston Celtics','BOS');"
            "INSERT INTO games VALUES ('0022500001','NBA','2025-26','2025-10-21T00:00:00+00:00',"
            " 'LAL','BOS',112,105,'FINAL','x');"
            "INSERT INTO games VALUES ('0042500405','NBA','2025-26','2026-06-13T00:00:00+00:00',"
            " 'LAL','BOS',90,94,'FINAL','x');"
            "INSERT INTO games VALUES ('20241106_LAL_BOS','NBA','2024-25','2024-11-06T00:00:00+00:00',"
            " 'LAL','BOS',1,0,'FINAL','x');"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        db.initialize()  # migrates + backfills
        repo = Repository(db)
        assert repo.get_game("0022500001").competition is Competition.REGULAR_SEASON
        assert repo.get_game("0042500405").competition is Competition.PLAYOFF
        # legacy id without a recognizable prefix: kept, untagged, never guessed
        assert repo.get_game("20241106_LAL_BOS").competition is None

        # idempotent: re-initialize does not change anything
        db.initialize()
        assert repo.get_game("0022500001").competition is Competition.REGULAR_SEASON


class TestCompetitionFiltering:
    @pytest.fixture
    def mixed_db(self, scheduled_target):
        """Scheduled-target fixture DB (untagged history) plus tagged playoff history."""
        from basketball_agent.data.schemas.base import League
        from basketball_agent.data.schemas.game import Game

        repo = Repository(scheduled_target)
        repo.insert_game(Game(
            game_id="0042400111", league=League.NBA,
            season="2024-25", game_date=datetime(2024, 11, 10, 2, 0, tzinfo=UTC),
            home_team_id="LAL", away_team_id="BOS", home_score=100, away_score=90,
            status=GameStatus.FINAL, competition=Competition.PLAYOFF,
        ))
        return scheduled_target

    def test_default_history_excludes_non_regular(self, mixed_db):
        repo = Repository(mixed_db)
        features = FeaturePipeline(repo).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        # the tagged PLAYOFF game (dated before as_of) must be excluded by default
        assert "0042400111" not in features.provenance.games_used

    def test_opt_in_includes_non_regular(self, mixed_db):
        repo = Repository(mixed_db)
        features = FeaturePipeline(repo, include_non_regular_history=True).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        assert "0042400111" in features.provenance.games_used


class TestRepositoryCacheEquivalence:
    """The read cache must return exactly what the SQL queries returned."""

    def test_cache_matches_sql(self, ingested_db):
        repo = Repository(ingested_db)
        as_of = datetime(2024, 11, 12, tzinfo=UTC)
        # warm the cache
        cached_games = repo.list_games(as_of=as_of, status=GameStatus.FINAL)
        cached_stats = repo.list_team_stats(as_of=as_of)
        # compare against direct SQL (same statements the old implementation used)
        with ingested_db.session() as conn:
            sql_games = conn.execute(
                "SELECT * FROM games WHERE game_date <= ? AND status = ? "
                "ORDER BY game_date ASC, game_id ASC",
                (as_of.isoformat(), GameStatus.FINAL.value),
            ).fetchall()
            sql_stats = conn.execute(
                "SELECT * FROM team_game_stats WHERE game_id IN "
                "(SELECT game_id FROM games WHERE game_date <= ?) "
                "ORDER BY game_id ASC, team_id ASC",
                (as_of.isoformat(),),
            ).fetchall()
        assert [g.game_id for g in cached_games] == [r["game_id"] for r in sql_games]
        assert [ts.game_id for ts in cached_stats] == [r["game_id"] for r in sql_stats]
        assert [ts.team_id for ts in cached_stats] == [r["team_id"] for r in sql_stats]

    def test_write_invalidates_cache(self, ingested_db):
        from basketball_agent.data.schemas.base import League
        from basketball_agent.data.schemas.game import Game

        repo = Repository(ingested_db)
        n0 = len(repo.list_games())
        repo.insert_game(Game(
            game_id="90000101_LAL_BOS", league=League.NBA, season="2024-25",
            game_date=datetime(2024, 9, 1, tzinfo=UTC), home_team_id="LAL",
            away_team_id="BOS", home_score=1, away_score=0, status=GameStatus.FINAL,
        ))
        assert len(repo.list_games()) == n0 + 1

    def test_resolve_train_start(self):
        cutoff = datetime(2026, 2, 1, tzinfo=UTC)
        assert resolve_train_start(cutoff, None) is None
        assert resolve_train_start(cutoff, 1) == datetime(2025, 10, 1, tzinfo=UTC)
        assert resolve_train_start(cutoff, 3) == datetime(2023, 10, 1, tzinfo=UTC)
        assert resolve_train_start(cutoff, 5) == datetime(2021, 10, 1, tzinfo=UTC)
        # September cutoff belongs to the season starting the previous year
        sep = datetime(2026, 9, 15, tzinfo=UTC)
        assert resolve_train_start(sep, 1) == datetime(2025, 10, 1, tzinfo=UTC)
        with pytest.raises(ValueError):
            resolve_train_start(cutoff, 0)

    def test_windowed_fit_bounds_train_games(self, tmp_path):
        """fit_v1_params with train_start only fits on games in the window."""
        from basketball_agent.data.collectors.synthetic import (
            SyntheticLeagueConfig, generate_league,
        )
        from basketball_agent.data.db.loader import DataIngestor
        from basketball_agent.data.schemas.dataset import Dataset
        from basketball_agent.evaluation.param_tuning import fit_v1_params

        # synthetic season spanning Nov 2024 .. Feb 2025 (games_per_pair=2, 6 teams = 30 games)
        teams, players, games, team_stats, player_stats = generate_league(
            SyntheticLeagueConfig(n_teams=6, games_per_pair=2, seed=2025)
        )
        ds = Dataset(games=tuple(games), team_stats=tuple(team_stats),
                     player_stats=tuple(player_stats), teams=tuple(teams),
                     players=tuple(players))
        raw = {k: [m.model_dump(mode="json") for m in v] for k, v in
               (("teams", ds.teams), ("players", ds.players), ("games", ds.games),
                ("team_stats", ds.team_stats), ("player_stats", ds.player_stats))}
        db = Database(tmp_path / "w.db")
        db.initialize()
        DataIngestor(db, now=datetime(2025, 3, 1, tzinfo=UTC)).ingest(raw)
        repo = Repository(db)

        # synthetic season runs 2024-11-01 .. 2024-11-30, one game per day
        cutoff = datetime(2024, 11, 15, tzinfo=UTC)
        n_before = sum(1 for g in repo.list_games(status=GameStatus.FINAL)
                       if g.game_date < cutoff)
        params = fit_v1_params(repo, cutoff, train_start=datetime(2024, 11, 5, tzinfo=UTC),
                               n_simulations=20, warmup_games=2)
        assert params.training_window[0][:10] == "2024-11-05"
        assert n_before > 0  # there WERE games before the cutoff outside the window
