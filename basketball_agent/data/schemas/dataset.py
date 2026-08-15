"""Container for one validated dataset (all records of one data snapshot)."""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.data.schemas.game import (
    Game,
    PlayerAvailability,
    PlayerGameStats,
    TeamGameStats,
)
from basketball_agent.data.schemas.player import Player
from basketball_agent.data.schemas.team import Team


@dataclass(frozen=True)
class Dataset:
    """Immutable, hashable collection of schema-validated records.

    ``teams`` / ``players`` are the *registries*: ``None`` means "no registry
    was provided" (existence checks skipped), an empty tuple means "registry
    provided but empty" (every reference to a team/player is then an error).
    """

    games: tuple[Game, ...] = ()
    team_stats: tuple[TeamGameStats, ...] = ()
    player_stats: tuple[PlayerGameStats, ...] = ()
    availability: tuple[PlayerAvailability, ...] = ()
    teams: tuple[Team, ...] | None = None
    players: tuple[Player, ...] | None = None
