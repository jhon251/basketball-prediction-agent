"""Unit tests for the simplified player-availability feature."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.schemas.base import PlayerAvailabilityStatus
from basketball_agent.data.schemas.game import PlayerAvailability
from basketball_agent.features.player.availability import available_minutes_share

pytestmark = pytest.mark.unit

UTC = timezone.utc


def make_record(player_id, status, prob) -> PlayerAvailability:
    return PlayerAvailability(
        player_id=player_id, game_id="g", status=status,
        availability_probability=prob,
        source_time=datetime(2024, 11, 18, tzinfo=UTC),
    )


class TestAvailableMinutesShare:
    def test_weighted_share(self):
        records = [
            make_record("p1", PlayerAvailabilityStatus.AVAILABLE, 1.0),
            make_record("p2", PlayerAvailabilityStatus.QUESTIONABLE, 0.5),
        ]
        minutes = {"p1": 30.0, "p2": 30.0, "p3": 40.0}  # p3 has no record -> available
        assert available_minutes_share(records, minutes) == pytest.approx(0.85)

    def test_out_player_removes_minutes(self):
        records = [make_record("p1", PlayerAvailabilityStatus.OUT, 0.0)]
        minutes = {"p1": 30.0, "p2": 30.0}
        assert available_minutes_share(records, minutes) == pytest.approx(0.5)

    def test_no_records_is_fully_available(self):
        assert available_minutes_share([], {"p1": 30.0}) == pytest.approx(1.0)

    def test_no_weighted_players_is_full(self):
        assert available_minutes_share([make_record("p1", PlayerAvailabilityStatus.OUT, 0.0)], {}) == pytest.approx(1.0)

    def test_zero_minute_players_ignored(self):
        records = [make_record("p1", PlayerAvailabilityStatus.OUT, 0.0)]
        minutes = {"p1": 0.0, "p2": 30.0}
        assert available_minutes_share(records, minutes) == pytest.approx(1.0)

    def test_records_for_unweighted_players_have_no_effect(self):
        records = [make_record("ghost", PlayerAvailabilityStatus.OUT, 0.0)]
        assert available_minutes_share(records, {"p2": 30.0}) == pytest.approx(1.0)
