"""Shared fixtures for the V1 test suite.

The sample dataset is small but *internally consistent*: every box score
reconciles (points == 2*(fg-fg3) + 3*fg3 + ft, ORB+DRB==TRB, all ratios valid),
scores match team box scores, and all availability source_times precede the
games they describe.  Clean data must pass every check with zero issues.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.agents.validation_agent import DataValidationAgent
from basketball_agent.data.db.connection import Database
from basketball_agent.data.schemas.base import GameStatus, League, PlayerAvailabilityStatus
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.schemas.game import (
    Game,
    PlayerAvailability,
    PlayerGameStats,
    TeamGameStats,
)
from basketball_agent.data.schemas.player import Player
from basketball_agent.data.schemas.team import Team

UTC = timezone.utc

FIXED_NOW = datetime(2025, 2, 1, 12, 0, tzinfo=UTC)

GAME_IDS = {
    "g1": "20241106_LAL_BOS",
    "g2": "20241110_LAL_GSW",
    "g3": "20241115_GSW_BOS",
}

GAME_DATES = {
    "g1": datetime(2024, 11, 6, 0, 0, tzinfo=UTC),
    "g2": datetime(2024, 11, 10, 0, 0, tzinfo=UTC),
    "g3": datetime(2024, 11, 15, 0, 0, tzinfo=UTC),
}


@pytest.fixture
def fixed_now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def sample_teams() -> list[Team]:
    return [
        Team(team_id="LAL", league=League.NBA, name="Los Angeles Lakers", abbreviation="LAL"),
        Team(team_id="BOS", league=League.NBA, name="Boston Celtics", abbreviation="BOS"),
        Team(team_id="GSW", league=League.NBA, name="Golden State Warriors", abbreviation="GSW"),
    ]


@pytest.fixture
def sample_players() -> list[Player]:
    ids = [("LAL-1", "LAL"), ("LAL-2", "LAL"), ("BOS-1", "BOS"), ("BOS-2", "BOS"),
           ("GSW-1", "GSW"), ("GSW-2", "GSW")]
    return [
        Player(player_id=pid, league=League.NBA, name=f"Player {pid}", current_team_id=tid)
        for pid, tid in ids
    ]


@pytest.fixture
def sample_games() -> list[Game]:
    return [
        Game(game_id=GAME_IDS["g1"], league=League.NBA, season="2024-25",
             game_date=GAME_DATES["g1"], home_team_id="LAL", away_team_id="BOS",
             home_score=112, away_score=105, status=GameStatus.FINAL),
        Game(game_id=GAME_IDS["g2"], league=League.NBA, season="2024-25",
             game_date=GAME_DATES["g2"], home_team_id="LAL", away_team_id="GSW",
             home_score=101, away_score=98, status=GameStatus.FINAL),
        Game(game_id=GAME_IDS["g3"], league=League.NBA, season="2024-25",
             game_date=GAME_DATES["g3"], home_team_id="GSW", away_team_id="BOS",
             home_score=95, away_score=110, status=GameStatus.FINAL),
    ]


def _ts(game_key: str, team_id: str, is_home: bool, **overrides) -> TeamGameStats:
    # (points, fg, fga, fg3, fg3a, ft, fta, orb, drb, trb, ast, tov, stl, blk, pf, minutes, poss)
    base = {
        "g1-LAL": dict(points=112, fg=40, fga=85, fg3=12, fg3a=30, ft=20, fta=24,
                       orb=9, drb=33, trb=42, ast=25, tov=12, stl=8, blk=5, pf=19,
                       minutes=240, possessions=101.0),
        "g1-BOS": dict(points=105, fg=38, fga=88, fg3=13, fg3a=36, ft=16, fta=20,
                       orb=10, drb=30, trb=40, ast=22, tov=14, stl=7, blk=4, pf=21,
                       minutes=240, possessions=102.0),
        "g2-LAL": dict(points=101, fg=36, fga=80, fg3=12, fg3a=32, ft=17, fta=20,
                       orb=8, drb=31, trb=39, ast=24, tov=13, stl=6, blk=4, pf=20,
                       minutes=240, possessions=99.0),
        "g2-GSW": dict(points=98, fg=34, fga=78, fg3=10, fg3a=29, ft=20, fta=24,
                       orb=9, drb=29, trb=38, ast=20, tov=15, stl=5, blk=3, pf=18,
                       minutes=240, possessions=98.0),
        "g3-GSW": dict(points=95, fg=33, fga=80, fg3=8, fg3a=26, ft=21, fta=26,
                       orb=10, drb=28, trb=38, ast=19, tov=16, stl=6, blk=4, pf=20,
                       minutes=240, possessions=97.0),
        "g3-BOS": dict(points=110, fg=41, fga=90, fg3=14, fg3a=35, ft=14, fta=17,
                       orb=11, drb=34, trb=45, ast=26, tov=11, stl=9, blk=6, pf=17,
                       minutes=240, possessions=98.0),
    }
    data = base[f"{game_key}-{team_id}"]
    data.update(overrides)
    return TeamGameStats(game_id=GAME_IDS[game_key], team_id=team_id, is_home=is_home, **data)


@pytest.fixture
def sample_team_stats() -> list[TeamGameStats]:
    return [
        _ts("g1", "LAL", True),
        _ts("g1", "BOS", False),
        _ts("g2", "LAL", True),
        _ts("g2", "GSW", False),
        _ts("g3", "GSW", True),
        _ts("g3", "BOS", False),
    ]


def _ps(game_key: str, player_id: str, team_id: str, **overrides) -> PlayerGameStats:
    base = {
        "g1-LAL-1": dict(minutes=36.0, points=30, fga=20, fg3a=8, fta=6, rebounds=8,
                         assists=10, turnovers=3, steals=2, blocks=1, fouls=2),
        "g1-LAL-2": dict(minutes=30.0, points=24, fga=17, fg3a=6, fta=5, rebounds=7,
                         assists=5, turnovers=2, steals=1, blocks=0, fouls=3),
        "g1-BOS-1": dict(minutes=38.0, points=32, fga=21, fg3a=9, fta=4, rebounds=6,
                         assists=9, turnovers=4, steals=3, blocks=1, fouls=2),
        "g1-BOS-2": dict(minutes=34.0, points=28, fga=19, fg3a=7, fta=6, rebounds=8,
                         assists=7, turnovers=2, steals=1, blocks=2, fouls=4),
        "g2-LAL-1": dict(minutes=35.0, points=27, fga=18, fg3a=7, fta=5, rebounds=9,
                         assists=8, turnovers=3, steals=1, blocks=1, fouls=3),
        "g2-LAL-2": dict(minutes=28.0, points=21, fga=15, fg3a=5, fta=4, rebounds=6,
                         assists=6, turnovers=2, steals=2, blocks=0, fouls=2),
        "g2-GSW-1": dict(minutes=37.0, points=31, fga=22, fg3a=8, fta=6, rebounds=5,
                         assists=8, turnovers=5, steals=2, blocks=1, fouls=3),
        "g2-GSW-2": dict(minutes=26.0, points=18, fga=13, fg3a=4, fta=3, rebounds=4,
                         assists=4, turnovers=3, steals=1, blocks=0, fouls=4),
        "g3-GSW-1": dict(minutes=36.0, points=26, fga=19, fg3a=6, fta=5, rebounds=6,
                         assists=7, turnovers=4, steals=2, blocks=1, fouls=2),
        "g3-GSW-2": dict(minutes=30.0, points=22, fga=16, fg3a=5, fta=4, rebounds=5,
                         assists=5, turnovers=3, steals=1, blocks=0, fouls=3),
        "g3-BOS-1": dict(minutes=39.0, points=35, fga=24, fg3a=10, fta=7, rebounds=7,
                         assists=10, turnovers=2, steals=4, blocks=2, fouls=2),
        "g3-BOS-2": dict(minutes=31.0, points=26, fga=18, fg3a=6, fta=5, rebounds=8,
                         assists=6, turnovers=1, steals=1, blocks=1, fouls=3),
    }
    data = base[f"{game_key}-{player_id}"]
    data.update(overrides)
    return PlayerGameStats(game_id=GAME_IDS[game_key], player_id=player_id, team_id=team_id, **data)


@pytest.fixture
def sample_player_stats() -> list[PlayerGameStats]:
    return [
        _ps("g1", "LAL-1", "LAL"), _ps("g1", "LAL-2", "LAL"),
        _ps("g1", "BOS-1", "BOS"), _ps("g1", "BOS-2", "BOS"),
        _ps("g2", "LAL-1", "LAL"), _ps("g2", "LAL-2", "LAL"),
        _ps("g2", "GSW-1", "GSW"), _ps("g2", "GSW-2", "GSW"),
        _ps("g3", "GSW-1", "GSW"), _ps("g3", "GSW-2", "GSW"),
        _ps("g3", "BOS-1", "BOS"), _ps("g3", "BOS-2", "BOS"),
    ]


@pytest.fixture
def sample_availability() -> list[PlayerAvailability]:
    src = "fixture"
    return [
        PlayerAvailability(player_id="LAL-1", game_id=GAME_IDS["g1"],
                           status=PlayerAvailabilityStatus.AVAILABLE,
                           availability_probability=1.0,
                           source_time=GAME_DATES["g1"] - timedelta(days=1), source=src),
        PlayerAvailability(player_id="LAL-2", game_id=GAME_IDS["g1"],
                           status=PlayerAvailabilityStatus.QUESTIONABLE,
                           availability_probability=0.5,
                           source_time=GAME_DATES["g1"] - timedelta(days=1), source=src),
        PlayerAvailability(player_id="BOS-1", game_id=GAME_IDS["g1"],
                           status=PlayerAvailabilityStatus.OUT,
                           availability_probability=0.0,
                           source_time=GAME_DATES["g1"] - timedelta(days=1), source=src),
        PlayerAvailability(player_id="GSW-1", game_id=GAME_IDS["g2"],
                           status=PlayerAvailabilityStatus.PROBABLE,
                           availability_probability=0.75,
                           source_time=GAME_DATES["g2"] - timedelta(days=1), source=src),
    ]


@pytest.fixture
def sample_dataset(
    sample_teams, sample_players, sample_games, sample_team_stats,
    sample_player_stats, sample_availability,
) -> Dataset:
    return Dataset(
        games=tuple(sample_games),
        team_stats=tuple(sample_team_stats),
        player_stats=tuple(sample_player_stats),
        availability=tuple(sample_availability),
        teams=tuple(sample_teams),
        players=tuple(sample_players),
    )


@pytest.fixture
def validation_agent(fixed_now) -> DataValidationAgent:
    return DataValidationAgent(now=fixed_now)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    return database
