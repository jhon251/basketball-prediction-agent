"""Game odds schema (Phase 11-A): market lines for one game.

This is a BENCHMARK data domain — odds never enter any model feature or
prediction input.  They exist solely to evaluate how the frozen prediction
core performs against market closing prices.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class OddsMarket(str, Enum):
    MONEYLINE = "MONEYLINE"
    SPREAD = "SPREAD"
    TOTAL = "TOTAL"


class OddsKind(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class GameOdds(BaseModel):
    """One odds quote for one game / bookmaker / market / snapshot.

    ``line`` is the point spread or total line (SPREAD/TOTAL only; None
    for MONEYLINE).  ``price_decimal`` is the European decimal odds
    (> 1.0).  ``captured_at`` is the UTC timestamp of the quote.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    game_id: str
    league: str
    bookmaker: str
    market: OddsMarket
    kind: OddsKind
    line: Optional[float] = None
    price_decimal: float = Field(gt=1.0, le=100.0)
    price_home: Optional[float] = None
    price_away: Optional[float] = None
    captured_at: str                    # ISO-8601 UTC
    source: str = ""

    @property
    def prob_home(self) -> Optional[float]:
        """De-vigged home probability (requires both prices)."""
        return None
