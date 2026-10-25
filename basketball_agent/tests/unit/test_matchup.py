"""Phase 12-C: matchup resolution (api/matchup)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.api.matchup import (
    TeamResolutionError,
    find_scheduled,
    resolve_team,
)
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.team import Team

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def seeded_db(db):
    repo = Repository(db)
    for tid, name in (
        ("MIL", "Milwaukee Bucks"), ("CHI", "Chicago Bulls"),
        ("LAL", "Los Angeles Lakers"), ("LAC", "Los Angeles Clippers"),
        ("NBL-SEM", "SE Melbourne Phoenix"),
    ):
        league = League.NBL if tid.startswith("NBL-") else League.NBA
        repo.upsert_team(Team(team_id=tid, league=league,
                              name=name, abbreviation=tid))
    yield


def _sched(db, game_id, home, away, *, kickoff, league=League.NBA):
    from basketball_agent.data.schemas.base import Competition

    Repository(db).insert_game(type("G", (), {
        "game_id": game_id, "league": league, "season": "2026-27",
        "game_date": kickoff, "home_team_id": home, "away_team_id": away,
        "home_score": None, "away_score": None,
        "status": GameStatus.SCHEDULED,
        "competition": Competition.REGULAR_SEASON,
    }))


class TestResolveTeam:
    @pytest.mark.parametrize("text,expected", [
        ("MIL", "MIL"), ("mil", "MIL"),                 # exact id
        ("湖人", "LAL"), ("雄鹿", "MIL"),
        ("Lakers", "LAL"), ("bucks", "MIL"),            # name substring
        ("Milwaukee", "MIL"),
        ("gsw", "GSW"), ("GS", "GSW"), ("utah", "UTA"),  # alias tables
    ])
    def test_resolutions(self, db, text, expected):
        assert resolve_team(Repository(db), "NBA", text) == expected

    def test_chinese_alias_resolves_without_db_row(self, db):
        # DAL has no seeded team row; the NBA franchise map is authoritative
        assert resolve_team(Repository(db), "NBA", "独行侠") == "DAL"

    def test_ambiguous_substring_fails_with_candidates(self, db):
        with pytest.raises(TeamResolutionError, match="LAC"):
            resolve_team(Repository(db), "NBA", "Los Angeles")

    def test_unknown_fails_with_known_list(self, db):
        with pytest.raises(TeamResolutionError, match="MIL"):
            resolve_team(Repository(db), "NBA", "Real Madrid")

    def test_league_isolation(self, db):
        assert resolve_team(Repository(db), "NBL", "NBL-SEM") == "NBL-SEM"
        with pytest.raises(TeamResolutionError):
            resolve_team(Repository(db), "NBL", "MIL")

    def test_empty_league_fails(self, db):
        with pytest.raises(TeamResolutionError):
            resolve_team(Repository(db), "WNBA", "anything")

    def test_empty_input_fails(self, db):
        with pytest.raises(TeamResolutionError):
            resolve_team(Repository(db), "NBA", "   ")


class TestFindScheduled:
    def test_finds_nearest_upcoming(self, db):
        _sched(db, "G1", "MIL", "CHI",
               kickoff=NOW + timedelta(days=3))
        _sched(db, "G2", "MIL", "CHI",
               kickoff=NOW + timedelta(days=1))
        game = find_scheduled(Repository(db), "NBA", "MIL", "CHI", now=NOW)
        assert game is not None and game.game_id == "G2"

    def test_swapped_home_away_not_matched(self, db):
        _sched(db, "G1", "MIL", "CHI",
               kickoff=NOW + timedelta(days=1))
        assert find_scheduled(
            Repository(db), "NBA", "CHI", "MIL", now=NOW) is None

    def test_past_kickoff_excluded(self, db):
        _sched(db, "G1", "MIL", "CHI",
               kickoff=NOW - timedelta(hours=2))
        assert find_scheduled(
            Repository(db), "NBA", "MIL", "CHI", now=NOW) is None

    def test_beyond_horizon_excluded(self, db):
        _sched(db, "G1", "MIL", "CHI",
               kickoff=NOW + timedelta(days=30))
        assert find_scheduled(
            Repository(db), "NBA", "MIL", "CHI", now=NOW,
            horizon_days=14) is None
