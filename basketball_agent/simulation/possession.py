"""Possession-level event resolution (spec §15, §16).

One *possession* is a sequence of *opportunities*; after a missed field goal
the offense keeps the ball with probability ``orb_pct`` (OFFENSIVE_REBOUND)
and gets another opportunity.  Every outcome probability comes from the
historical-data-derived :class:`PossessionParams` — nothing is guessed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum

from basketball_agent.models.score_model import PossessionParams


class PossessionEvent(str, Enum):
    TWO_PT_MADE = "2PT_MADE"
    TWO_PT_MISS = "2PT_MISS"
    THREE_PT_MADE = "3PT_MADE"
    THREE_PT_MISS = "3PT_MISS"
    FREE_THROW = "FREE_THROW"
    TURNOVER = "TURNOVER"
    OFFENSIVE_REBOUND = "OFFENSIVE_REBOUND"


@dataclass(frozen=True)
class PossessionResult:
    event: PossessionEvent
    points: int = 0
    possession_over: bool = True
    fta: int = 0
    ftm: int = 0


def resolve_opportunity(rng: random.Random, params: PossessionParams) -> PossessionResult:
    """Resolve one scoring opportunity.

    Draws, in order: turnover -> free-throw trip -> field-goal attempt
    (2PT/3PT by ``p3a_share``); a missed FGA either extends the possession
    (OFFENSIVE_REBOUND) or ends it.
    """
    r = rng.random()
    if r < params.p_tov:
        return PossessionResult(PossessionEvent.TURNOVER, 0, True)
    r -= params.p_tov
    if r < params.p_fta:
        fta = int(round(params.fta_per_event))
        ftm = sum(1 for _ in range(fta) if rng.random() < params.ft_pct)
        return PossessionResult(PossessionEvent.FREE_THROW, ftm, True, fta=fta, ftm=ftm)

    if rng.random() < params.p3a_share:
        if rng.random() < params.p3_pct:
            return PossessionResult(PossessionEvent.THREE_PT_MADE, 3, True)
        if rng.random() < params.orb_pct:
            return PossessionResult(PossessionEvent.OFFENSIVE_REBOUND, 0, False)
        return PossessionResult(PossessionEvent.THREE_PT_MISS, 0, True)

    if rng.random() < params.p2_pct:
        return PossessionResult(PossessionEvent.TWO_PT_MADE, 2, True)
    if rng.random() < params.orb_pct:
        return PossessionResult(PossessionEvent.OFFENSIVE_REBOUND, 0, False)
    return PossessionResult(PossessionEvent.TWO_PT_MISS, 0, True)
