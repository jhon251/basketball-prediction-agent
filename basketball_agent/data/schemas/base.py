"""Shared enumerations used across all schemas."""
from __future__ import annotations

from enum import Enum


class League(str, Enum):
    NBA = "NBA"
    G_LEAGUE = "G_LEAGUE"
    CBA = "CBA"
    EUROLEAGUE = "EUROLEAGUE"
    OTHER = "OTHER"


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
