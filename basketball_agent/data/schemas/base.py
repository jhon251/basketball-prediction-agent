"""Shared enumerations used across all schemas."""
from __future__ import annotations

from enum import Enum


class League(str, Enum):
    NBA = "NBA"
    G_LEAGUE = "G_LEAGUE"
    CBA = "CBA"
    EUROLEAGUE = "EUROLEAGUE"
    ACB = "ACB"          # Liga Endesa (Spain) — Phase 8-B6
    OTHER = "OTHER"


class Competition(str, Enum):
    """Within-league competition segment (Phase 8-B2).

    NBA mapping from the stats.nba.com game_id prefix: 002 -> REGULAR_SEASON,
    004 -> PLAYOFF, 005 -> PLAY_IN, 006 -> CUP (NBA Cup final), everything
    else -> OTHER.  Default prediction/training history uses REGULAR_SEASON
    only; other segments must be selected explicitly.
    """

    REGULAR_SEASON = "REGULAR_SEASON"
    PLAY_IN = "PLAY_IN"
    PLAYOFF = "PLAYOFF"
    CUP = "CUP"
    OTHER = "OTHER"


# Competition segments excluded from default prediction/training history —
# every segment NOT listed here (plus untagged legacy rows) stays eligible.
NON_REGULAR_COMPETITIONS: frozenset[Competition] = frozenset(
    {Competition.PLAY_IN, Competition.PLAYOFF, Competition.CUP}
)


class GameStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    LIVE = "LIVE"
    FINAL = "FINAL"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"


class PlayerAvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    PROBABLE = "PROBABLE"
    QUESTIONABLE = "QUESTIONABLE"
    DOUBTFUL = "DOUBTFUL"
    OUT = "OUT"
    SUSPENDED = "SUSPENDED"
