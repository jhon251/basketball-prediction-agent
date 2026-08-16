"""Unit tests for the NBA team identity map (stable franchise IDs)."""
from __future__ import annotations

import pytest

from basketball_agent.data.collectors.nba_teams import canonical_team_id

pytestmark = pytest.mark.unit


class TestCanonicalTeamId:
    @pytest.mark.parametrize("raw,expected", [
        ("LAL", "LAL"),
        ("lal", "LAL"),
        ("Los Angeles Lakers", "LAL"),
        ("SEA", "OKC"),   # SuperSonics -> Thunder
        ("NJN", "BKN"),   # New Jersey -> Brooklyn
        ("BRK", "BKN"),
        ("NOH", "NOP"),   # Hornets -> Pelicans
        ("VAN", "MEM"),   # Vancouver -> Memphis
        ("CHH", "CHA"),   # Charlotte history
        ("GSW", "GSW"),
        ("PHO", "PHX"),   # historical abbreviation
        ("WSH", "WAS"),
    ])
    def test_known_mappings(self, raw, expected):
        assert canonical_team_id(raw) == expected

    def test_unknown_team_returns_none(self):
        assert canonical_team_id("XYZ") is None

    def test_whitespace_and_case_insensitive(self):
        assert canonical_team_id("  bos ") == "BOS"
