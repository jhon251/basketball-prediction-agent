"""Parse raw records (dicts) into schema-validated models.

Parse errors are collected — never swallowed — so ingestion can decide to
block and report every bad record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.schemas.game import (
    Game,
    PlayerAvailability,
    PlayerGameStats,
    TeamGameStats,
)
from basketball_agent.data.schemas.player import Player
from basketball_agent.data.schemas.team import Team


@dataclass(frozen=True)
class ParseError:
    record_type: str
    index: int
    message: str
    raw_record: dict = field(default_factory=dict)


def _parse(
    record_type: str, model_cls: type[BaseModel], raw_records: list[dict[str, Any]] | None
) -> tuple[list[BaseModel], list[ParseError]]:
    parsed: list[BaseModel] = []
    errors: list[ParseError] = []
    for i, raw in enumerate(raw_records or []):
        try:
            parsed.append(model_cls.model_validate(raw))
        except ValidationError as exc:
            errors.append(ParseError(record_type, i, str(exc), dict(raw)))
    return parsed, errors


def _parse_optional(
    key: str, record_type: str, model_cls: type[BaseModel], raw: dict[str, Any]
) -> tuple[tuple[BaseModel, ...] | None, list[ParseError]]:
    """Parse a registry key; returns ``None`` when the key is absent so that
    "no registry provided" is distinguishable from "registry provided empty"."""
    if key not in raw or raw[key] is None:
        return None, []
    parsed, errors = _parse(record_type, model_cls, raw[key])
    return tuple(parsed), errors


def parse_dataset(raw: dict[str, list[dict[str, Any]]]) -> tuple[Dataset, list[ParseError]]:
    """Parse a raw dataset dict into a validated ``Dataset``.

    Expected keys: ``teams``, ``players``, ``games``, ``team_stats``,
    ``player_stats``, ``availability``.
    """
    errors: list[ParseError] = []
    games, e = _parse("Game", Game, raw.get("games"))
    errors.extend(e)
    team_stats, e = _parse("TeamGameStats", TeamGameStats, raw.get("team_stats"))
    errors.extend(e)
    player_stats, e = _parse("PlayerGameStats", PlayerGameStats, raw.get("player_stats"))
    errors.extend(e)
    availability, e = _parse("PlayerAvailability", PlayerAvailability, raw.get("availability"))
    errors.extend(e)
    teams, e = _parse_optional("teams", "Team", Team, raw)
    errors.extend(e)
    players, e = _parse_optional("players", "Player", Player, raw)
    errors.extend(e)
    dataset = Dataset(
        games=tuple(games),
        team_stats=tuple(team_stats),
        player_stats=tuple(player_stats),
        availability=tuple(availability),
        teams=teams,
        players=players,
    )
    return dataset, errors
