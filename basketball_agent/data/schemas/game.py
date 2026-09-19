"""Core game-related schemas.

Datetimes must be timezone-aware.  This is a deliberate, schema-level
decision: it is the first line of defence against temporal data leakage.
"""
from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from basketball_agent.data.schemas.base import (
    Competition,
    GameStatus,
    League,
    PlayerAvailabilityStatus,
)


class Game(BaseModel):
    """A single basketball game (scheduled, live or final)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    game_id: str = Field(min_length=1)
    league: League
    season: str = Field(min_length=1)
    game_date: AwareDatetime
    home_team_id: str = Field(min_length=1)
    away_team_id: str = Field(min_length=1)
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    status: GameStatus
    competition: Competition | None = None  # None = untagged (legacy rows)

    @model_validator(mode="after")
    def _check_structural_invariants(self) -> "Game":
        if self.home_team_id == self.away_team_id:
            raise ValueError("home_team_id and away_team_id must differ")
        if self.status is GameStatus.FINAL and (self.home_score is None or self.away_score is None):
            raise ValueError("FINAL games must have both scores")
        if self.status is GameStatus.SCHEDULED and (self.home_score is not None or self.away_score is not None):
            raise ValueError("SCHEDULED games must not have scores")
        return self


class TeamGameStats(BaseModel):
    """Team box score for one game."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    game_id: str
    team_id: str
    is_home: bool
    minutes: int = Field(ge=0)
    points: int = Field(ge=0)
    possessions: float = Field(ge=0)
    fg: int = Field(ge=0)
    fga: int = Field(ge=0)
    fg3: int = Field(ge=0)
    fg3a: int = Field(ge=0)
    ft: int = Field(ge=0)
    fta: int = Field(ge=0)
    orb: int = Field(ge=0)
    drb: int = Field(ge=0)
    trb: int = Field(ge=0)
    ast: int = Field(ge=0)
    tov: int = Field(ge=0)
    stl: int = Field(ge=0)
    blk: int = Field(ge=0)
    pf: int = Field(ge=0)


class PlayerGameStats(BaseModel):
    """Individual player line for one game (simplified V1 granularity)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    game_id: str
    player_id: str
    team_id: str
    minutes: float = Field(ge=0)
    points: int = Field(ge=0)
    fga: int = Field(ge=0)
    fg3a: int = Field(ge=0)
    fta: int = Field(ge=0)
    rebounds: int = Field(ge=0)
    assists: int = Field(ge=0)
    turnovers: int = Field(ge=0)
    steals: int = Field(ge=0)
    blocks: int = Field(ge=0)
    fouls: int = Field(ge=0)


class PlayerAvailability(BaseModel):
    """Availability record for one player and one game.

    ``source_time`` is mandatory: it is the key field used by the
    temporal-integrity / leakage checks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: str
    game_id: str
    status: PlayerAvailabilityStatus
    availability_probability: float = Field(ge=0.0, le=1.0)
    source_time: AwareDatetime
    source: str = "unknown"
