"""Schedule features: rest days and back-to-back flags."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.data.schemas.game import Game


def rest_days(team_id: str, game_date: datetime, history: Iterable[Game]) -> float | None:
    """Days between ``game_date`` and the team's previous FINAL game.

    ``history`` must only contain games known before the prediction moment
    (the pipeline guarantees this).  Returns None when the team has no prior
    game.
    """
    prior = [
        g for g in history
        if g.status is GameStatus.FINAL
        and g.game_date < game_date
        and team_id in (g.home_team_id, g.away_team_id)
    ]
    if not prior:
        return None
    last = max(prior, key=lambda g: g.game_date)
    return (game_date - last.game_date).total_seconds() / 86400.0


def is_back_to_back(rest: float | None) -> bool | None:
    """True when the team played yesterday (rest <= 1 day)."""
    return (rest is not None and rest <= 1.0) if rest is not None else None
