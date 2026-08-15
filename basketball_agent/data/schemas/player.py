"""Player registry schema."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from basketball_agent.data.schemas.base import League


class Player(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: str
    league: League
    name: str
    current_team_id: str | None = None
