"""Simplified player layer (spec §17).

V1 does not simulate individual player actions, but the interfaces for the
V2 player-level engine are reserved here: usage, efficiency, minutes share
and on/off.  What V1 *does* use:

* ``team_efficiency_multiplier`` — availability- and efficiency-weighted
  team strength adjustment (core-player efficiency, injuries), applied to
  the team's expected PPP before simulation;
* minutes distribution — possession seconds are allocated to available
  players by minutes share and recorded in the game state.

The multiplier math is explicit and data-driven; an LLM never adjusts it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerProfile:
    """Reserved V2 hook plus the fields V1 uses."""

    player_id: str
    team_id: str
    usage: float = 1.0        # V2: share of team possessions used
    efficiency: float = 1.0   # relative scoring efficiency (V1: used)
    minutes_share: float = 1.0  # relative minutes weight (V1: used)
    on_off: float = 0.0       # V2: per-100-poss on/off impact
    available: bool = True    # availability (injury/rest); V1: used


def team_efficiency_multiplier(profiles: list[PlayerProfile]) -> float:
    """Availability-weighted average efficiency of the roster.

    ``sum(share_i * eff_i over available) / sum(share_i over available)``.
    Unavailable players are excluded from both numerator and denominator.
    Returns 1.0 (no adjustment) when no available player carries minutes.
    """
    numerator = 0.0
    denominator = 0.0
    for p in profiles:
        if not p.available:
            continue
        numerator += p.minutes_share * p.efficiency
        denominator += p.minutes_share
    if denominator <= 0:
        return 1.0
    return numerator / denominator
