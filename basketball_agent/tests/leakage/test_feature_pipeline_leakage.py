"""Leakage tests for the feature pipeline (spec §2.2: future info = FAIL).

The pipeline's guards are structural (as_of filters, target exclusion) and
defense-in-depth (assertions).  These tests verify each guard behaves.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League, PlayerAvailabilityStatus
from basketball_agent.data.schemas.game import Game, PlayerAvailability
from basketball_agent.features import FeatureComputationError, FeaturePipeline

pytestmark = pytest.mark.leakage

UTC = timezone.utc
AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=UTC)


def make_pipeline(db) -> FeaturePipeline:
    return FeaturePipeline(Repository(db))


class TestFeaturePipelineLeakageGuards:
    def test_target_game_excluded_from_own_features(self, ingested_db):
        # as_of exactly at g3's tip-off: g3's own box score and player stats
        # fall inside the inclusive as_of window and must still be excluded.
        feats = make_pipeline(ingested_db).compute(
            "20241115_GSW_BOS", datetime(2024, 11, 15, 0, 0, tzinfo=UTC)
        )
        assert "20241115_GSW_BOS" not in feats.provenance.games_used
        assert feats.provenance.games_used == ("20241106_LAL_BOS", "20241110_LAL_GSW")
        assert feats.provenance.team_stats_used == 4  # 2 games x 2 teams; g3's rows excluded
        assert feats.home.games_played == 1
        assert feats.away.games_played == 1

    def test_availability_published_after_as_of_is_ignored(self, scheduled_target):
        repo = Repository(scheduled_target)
        good = datetime(2024, 11, 18, 12, 0, tzinfo=UTC)
        late = AS_OF + timedelta(hours=6)  # after as_of, still before the game
        repo.insert_availability(PlayerAvailability(
            player_id="LAL-1", game_id="20241120_LAL_BOS",
            status=PlayerAvailabilityStatus.OUT, availability_probability=0.0,
            source_time=good,
        ))
        repo.insert_availability(PlayerAvailability(
            player_id="LAL-2", game_id="20241120_LAL_BOS",
            status=PlayerAvailabilityStatus.AVAILABLE, availability_probability=1.0,
            source_time=good,
        ))
        # Future information: LAL-1 suddenly PROBABLE — must be invisible.
        repo.insert_availability(PlayerAvailability(
            player_id="LAL-1", game_id="20241120_LAL_BOS",
            status=PlayerAvailabilityStatus.PROBABLE, availability_probability=0.9,
            source_time=late,
        ))
        feats = make_pipeline(scheduled_target).compute("20241120_LAL_BOS", AS_OF)
        assert feats.home_available_share == pytest.approx(29 / 64.5)  # unchanged by late record
        assert len(feats.provenance.availability_used) == 2
        used_sources = {t[1] for t in feats.provenance.availability_used}
        assert good.isoformat() in used_sources
        assert late.isoformat() not in used_sources

    def test_game_dated_exactly_at_as_of_is_excluded(self, scheduled_target):
        repo = Repository(scheduled_target)
        edge = Game(
            game_id="EDGE_GAME", league=League.NBA, season="2024-25",
            game_date=AS_OF,  # same instant as the prediction moment
            home_team_id="LAL", away_team_id="GSW",
            home_score=90, away_score=80, status=GameStatus.FINAL,
        )
        repo.insert_game(edge)
        feats = make_pipeline(scheduled_target).compute("20241120_LAL_BOS", AS_OF)
        assert "EDGE_GAME" not in feats.provenance.games_used
        # The repository's inclusive as_of filter sees it, the pipeline's
        # strict filter must not.
        assert any(g.game_id == "EDGE_GAME" for g in repo.list_games(as_of=AS_OF, status=GameStatus.FINAL))

    def test_no_history_game_is_at_or_after_as_of(self, scheduled_target):
        feats = make_pipeline(scheduled_target).compute("20241120_LAL_BOS", AS_OF)
        repo = Repository(scheduled_target)
        for gid in feats.provenance.games_used:
            game = repo.get_game(gid)
            assert game.game_date < AS_OF
            assert game.status is GameStatus.FINAL

    def test_as_of_after_game_start_fails(self, ingested_db):
        with pytest.raises(FeatureComputationError):
            make_pipeline(ingested_db).compute(
                "20241106_LAL_BOS", datetime(2024, 11, 6, 12, 0, tzinfo=UTC)
            )
