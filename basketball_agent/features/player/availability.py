"""Player availability features (V1 simplified player model).

The V1 player layer only aggregates *availability* — the spec's player-level
usage/efficiency interfaces arrive with the player-level simulation (V2).
Availability records must be filtered by ``source_time <= as_of`` before
they ever reach this function (the repository's ``known_at`` filter and the
pipeline both enforce it).
"""
from __future__ import annotations

from basketball_agent.data.schemas.game import PlayerAvailability


def available_minutes_share(
    records: list[PlayerAvailability], minutes_by_player: dict[str, float]
) -> float:
    """Expected share of weighted minutes available.

    ``minutes_by_player`` maps player_id -> average minutes (weights).  A
    player without an availability record counts as fully available.  Players
    with unknown/zero minutes weight are excluded (their impact cannot be
    estimated).  Returns 1.0 when no weighted players are known.
    """
    total_w = 0.0
    available_w = 0.0
    probs = {r.player_id: r.availability_probability for r in records}
    for player_id, weight in minutes_by_player.items():
        if weight <= 0:
            continue
        total_w += weight
        available_w += weight * probs.get(player_id, 1.0)
    if total_w <= 0:
        return 1.0
    return available_w / total_w
