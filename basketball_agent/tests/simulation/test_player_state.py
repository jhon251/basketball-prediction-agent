"""Unit tests for the simplified player layer."""
from __future__ import annotations

import pytest

from basketball_agent.simulation.player_state import PlayerProfile, team_efficiency_multiplier

pytestmark = pytest.mark.simulation


def prof(pid, efficiency=1.0, share=1.0, available=True, team="T"):
    return PlayerProfile(
        player_id=pid, team_id=team, efficiency=efficiency,
        minutes_share=share, available=available,
    )


class TestTeamEfficiencyMultiplier:
    def test_weighted_average(self):
        profiles = [prof("a", 1.0, 1.0), prof("b", 0.5, 1.0)]
        assert team_efficiency_multiplier(profiles) == pytest.approx(0.75)

    def test_out_players_excluded(self):
        profiles = [
            prof("a", 2.0, 1.0, available=False),  # star out
            prof("b", 1.0, 1.0),
        ]
        assert team_efficiency_multiplier(profiles) == pytest.approx(1.0)

    def test_minutes_share_weights(self):
        profiles = [prof("a", 1.0, 3.0), prof("b", 0.5, 1.0)]
        assert team_efficiency_multiplier(profiles) == pytest.approx((3.0 + 0.5) / 4.0)

    def test_no_available_players_is_neutral(self):
        assert team_efficiency_multiplier([prof("a", 2.0, 1.0, available=False)]) == pytest.approx(1.0)

    def test_empty_roster_is_neutral(self):
        assert team_efficiency_multiplier([]) == pytest.approx(1.0)

    def test_all_out_star_removed_from_numerator_and_denominator(self):
        # star out: numerator and denominator both drop by his share
        profiles = [prof("star", 1.5, 4.0, available=False), prof("role", 1.0, 6.0)]
        assert team_efficiency_multiplier(profiles) == pytest.approx(1.0)
