"""Temporal-integrity tests: future information must FAIL, not warn.

Spec §2.2: if future information enters a feature, the system reports FAIL.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from basketball_agent.data.db.loader import DataIngestor, IngestBlockedError
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game

pytestmark = pytest.mark.leakage


def raw_from_dataset(ds) -> dict:
    return {
        "teams": [t.model_dump(mode="json") for t in ds.teams],
        "players": [p.model_dump(mode="json") for p in ds.players],
        "games": [g.model_dump(mode="json") for g in ds.games],
        "team_stats": [ts.model_dump(mode="json") for ts in ds.team_stats],
        "player_stats": [ps.model_dump(mode="json") for ps in ds.player_stats],
        "availability": [a.model_dump(mode="json") for a in ds.availability],
    }


class TestTemporalLeakageGuards:
    def test_future_availability_source_time_fails_validation(self, sample_dataset, validation_agent):
        game = sample_dataset.games[0]
        bad = sample_dataset.availability[0].model_copy(
            update={"source_time": game.game_date + timedelta(hours=1)}
        )
        ds = replace(sample_dataset, availability=sample_dataset.availability + (bad,))
        report = validation_agent.validate(ds)
        assert report.has_errors
        assert any(i.check == "temporal.availability_source_time" for i in report.errors)

    def test_ingest_gate_blocks_future_information(self, db, sample_dataset, fixed_now):
        raw = raw_from_dataset(sample_dataset)
        game = sample_dataset.games[0]
        bad = sample_dataset.availability[0].model_copy(
            update={"source_time": game.game_date + timedelta(hours=1)}
        )
        raw["availability"].append(bad.model_dump(mode="json"))
        with pytest.raises(IngestBlockedError):
            DataIngestor(db, now=fixed_now).ingest(raw)
        assert Repository(db).game_count() == 0

    def test_as_of_queries_exclude_future_games(self, db, sample_dataset, fixed_now):
        DataIngestor(db, now=fixed_now).ingest(raw_from_dataset(sample_dataset))
        repo = Repository(db)
        future_game = Game(
            game_id="FUTURE_GAME", league=League.NBA, season="2025-26",
            game_date=fixed_now + timedelta(days=1),
            home_team_id="LAL", away_team_id="BOS",
            home_score=None, away_score=None, status=GameStatus.SCHEDULED,
        )
        repo.insert_game(future_game)
        # The structural guard: as_of queries cannot see the future game.
        assert {g.game_id for g in repo.list_games(as_of=fixed_now)} == {
            g.game_id for g in sample_dataset.games
        }
        assert len(repo.list_games()) == len(sample_dataset.games) + 1

    def test_availability_known_at_filters_later_publications(self, db, sample_dataset, fixed_now):
        DataIngestor(db, now=fixed_now).ingest(raw_from_dataset(sample_dataset))
        repo = Repository(db)
        all_records = repo.list_availability(game_id="20241106_LAL_BOS")
        assert len(all_records) == 3
        game_date = sample_dataset.games[0].game_date
        # All sources were published the day before the game: known_at before
        # publication must exclude them, known_at at game time must keep them.
        before_publication = game_date - timedelta(days=2)
        assert repo.list_availability(game_id="20241106_LAL_BOS", known_at=before_publication) == []
        assert len(repo.list_availability(game_id="20241106_LAL_BOS", known_at=game_date)) == 3
