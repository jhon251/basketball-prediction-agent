"""Regression tests: hand-computed expected feature values for the fixture.

These numbers were computed independently by hand from the documented
formulas (Elo sequence, pace means, availability share).  They pin the
pipeline's behavior so silent changes are caught.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import PlayerAvailabilityStatus
from basketball_agent.data.schemas.game import PlayerAvailability
from basketball_agent.features import FeaturePipeline

pytestmark = pytest.mark.regression

UTC = timezone.utc
AS_OF = datetime(2024, 11, 19, 12, 0, tzinfo=UTC)


class TestFeatureRegression:
    def test_fixture_pipeline_values(self, scheduled_target):
        repo = Repository(scheduled_target)
        src = datetime(2024, 11, 18, 12, 0, tzinfo=UTC)
        repo.insert_availability(PlayerAvailability(
            player_id="LAL-1", game_id="20241120_LAL_BOS",
            status=PlayerAvailabilityStatus.OUT, availability_probability=0.0,
            source_time=src,
        ))
        repo.insert_availability(PlayerAvailability(
            player_id="LAL-2", game_id="20241120_LAL_BOS",
            status=PlayerAvailabilityStatus.AVAILABLE, availability_probability=1.0,
            source_time=src,
        ))
        feats = FeaturePipeline(repo).compute("20241120_LAL_BOS", AS_OF)

        # Elo after the 3-game fixture sequence (hand-computed)
        assert feats.home.elo == pytest.approx(1509.2422, abs=0.05)
        assert feats.away.elo == pytest.approx(1510.3931, abs=0.05)
        # Net ratings (hand-computed from box scores)
        assert feats.home.nrtg == pytest.approx(4.985058, abs=1e-4)
        assert feats.away.nrtg == pytest.approx(3.178421, abs=1e-4)
        assert feats.nrtg_diff == pytest.approx(1.806637, abs=1e-4)
        # Pace
        assert feats.home.pace == pytest.approx(100.0)
        assert feats.away.pace == pytest.approx(99.5)
        assert feats.expected_pace == pytest.approx(99.75)
        # Elo win probability for home LAL (rating + HCA)
        assert feats.elo_win_prob_home == pytest.approx(0.638532, abs=1e-3)
        # Schedule (target tips off at 02:00 on 11-20)
        assert feats.home_rest_days == pytest.approx(10 + 2 / 24)
        assert feats.away_rest_days == pytest.approx(5 + 2 / 24)
        assert feats.league_home_win_pct == pytest.approx(2 / 3)
        # Availability: LAL-1 OUT (0.0 * 35.5 min), LAL-2 in (1.0 * 29 min)
        assert feats.home_available_share == pytest.approx(29 / 64.5)
        assert feats.away_available_share == pytest.approx(1.0)
        # Provenance shape
        assert len(feats.provenance.games_used) == 3
        assert len(feats.provenance.availability_used) == 2
