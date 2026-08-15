"""Team registry schema."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from basketball_agent.data.schemas.base import League


class Team(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str
    league: League
    name: str
    abbreviation: str
