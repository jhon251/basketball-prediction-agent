"""Unit tests for leakage detection and reality validation (FAIL semantics)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.data.db.repository import Repository
from basketball_agent.evaluation.leakage import check_temporal_integrity, reality_check
from basketball_agent.features import FeaturePipeline
from basketball_agent.tests.unit.model_helpers import make_features

pytestmark = pytest.mark.unit

UTC = timezone.utc
AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=UTC)


class TestTemporalIntegrity:
    def test_clean_pipeline_features_pass(self, scheduled_target):
        features = FeaturePipeline(Repository(scheduled_target)).compute("20241120_LAL_BOS", AS_OF)
        report = check_temporal_integrity(features, Repository(scheduled_target))
        assert report.status == "PASS"
        assert report.violations == ()
        assert "leakage.games_before_as_of" in report.checks_performed

    def test_future_game_in_provenance_fails(self, scheduled_target, fixed_now):
        features = FeaturePipeline(Repository(scheduled_target)).compute("20241120_LAL_BOS", AS_OF)
        bad = replace(features, provenance=replace(
            features.provenance,
            games_used=features.provenance.games_used + ("20241120_LAL_BOS",),
        ))
        report = check_temporal_integrity(bad, Repository(scheduled_target))
        assert report.status == "FAIL"
        assert any("20241120_LAL_BOS" in v for v in report.violations)

    def test_target_in_provenance_fails(self, scheduled_target):
        features = FeaturePipeline(Repository(scheduled_target)).compute("20241120_LAL_BOS", AS_OF)
        bad = replace(features, provenance=replace(
            features.provenance, games_used=("20241106_LAL_BOS",) + (features.game_id,)
        ))
        report = check_temporal_integrity(bad, Repository(scheduled_target))
        assert report.status == "FAIL"

    def test_future_availability_in_provenance_fails(self, scheduled_target):
        features = FeaturePipeline(Repository(scheduled_target)).compute("20241120_LAL_BOS", AS_OF)
        future = (AS_OF + timedelta(hours=1)).isoformat()
        bad = replace(features, provenance=replace(
            features.provenance, availability_used=(("LAL-1", future),)
        ))
        report = check_temporal_integrity(bad, Repository(scheduled_target))
        assert report.status == "FAIL"
        assert any("after as_of" in v for v in report.violations)

    def test_unknown_game_in_provenance_fails(self, scheduled_target):
        features = FeaturePipeline(Repository(scheduled_target)).compute("20241120_LAL_BOS", AS_OF)
        bad = replace(features, provenance=replace(
            features.provenance, games_used=("NOPE",)
        ))
        report = check_temporal_integrity(bad, Repository(scheduled_target))
        assert report.status == "FAIL"

    def test_synthetic_features_without_repo_games(self, tmp_path):
        # provenance referencing games the repo does not know -> FAIL
        features = make_features()
        features = replace(features, provenance=replace(
            features.provenance, games_used=("ghost",)
        ))
        from basketball_agent.data.db.connection import Database

        db = Database(tmp_path / "leak.db")
        db.initialize()
        report = check_temporal_integrity(features, Repository(db))
        assert report.status == "FAIL"


class TestRealityCheck:
    def test_valid_target_passes(self, scheduled_target):
        report = reality_check(Repository(scheduled_target), "20241120_LAL_BOS", AS_OF)
        assert report.status == "PASS"

    def test_missing_game_fails(self, scheduled_target):
        report = reality_check(Repository(scheduled_target), "NOPE", AS_OF)
        assert report.status == "FAIL"
        assert any("does not exist" in v for v in report.violations)

    def test_prediction_after_game_fails(self, scheduled_target):
        late = datetime(2024, 11, 21, 0, 0, tzinfo=UTC)
        report = reality_check(Repository(scheduled_target), "20241120_LAL_BOS", late)
        assert report.status == "FAIL"
        assert any("after game start" in v for v in report.violations)

    def test_unknown_team_fails(self, tmp_path):
        # A game referencing an unregistered team cannot exist under FK
        # enforcement; construct it with FKs off to test the defense-in-depth
        # reality check.
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.schemas.base import GameStatus, League

        db = Database(tmp_path / "reality.db")
        db.initialize()
        conn = db.connect()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO games (game_id, league, season, game_date, home_team_id, "
            "away_team_id, home_score, away_score, status, inserted_at) "
            "VALUES ('g', 'NBA', '2024-25', '2024-11-06T00:00:00+00:00', 'LAL', 'GHOST', "
            "NULL, NULL, 'SCHEDULED', '2024-11-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()
        report = reality_check(Repository(db), "g", datetime(2024, 11, 5, tzinfo=UTC))
        assert report.status == "FAIL"
        assert any("not registered" in v for v in report.violations)
