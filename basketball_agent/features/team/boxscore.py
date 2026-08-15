"""Per-game derived statistics computed from raw, validated box scores.

Every value here comes from an explicit formula (see the ``derive`` function
docstring); nothing is estimated or guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from basketball_agent.data.schemas.game import TeamGameStats


def _ratio(num: float, den: float) -> float | None:
    """Safe ratio; returns None when the denominator is not positive."""
    return (num / den) if den > 0 else None


@dataclass(frozen=True)
class TeamGameDerived:
    """One team's derived stats for one game (opponent-aware)."""

    game_id: str
    game_date: datetime
    team_id: str
    opponent_id: str
    points: int
    points_allowed: int
    margin: float
    possessions: float
    opp_possessions: float
    pace: float
    ortg: float | None
    drtg: float | None
    nrtg: float | None
    efg_pct: float | None
    ts_pct: float | None
    fg3_pct: float | None
    ft_pct: float | None
    orb_pct: float | None
    drb_pct: float | None
    trb_pct: float | None
    tov_pct: float | None
    opp_tov_pct: float | None


def derive(
    game_id: str,
    game_date: datetime,
    own: TeamGameStats,
    opp: TeamGameStats,
) -> TeamGameDerived:
    """Derive opponent-aware statistics for ``own``'s side of one game.

    Formulas:
      pace       = (poss_own + poss_opp) / 2
      ORtg       = points / possessions * 100
      DRtg       = points_allowed / opp_possessions * 100
      NetRtg     = ORtg - DRtg
      eFG%       = (fg + 0.5 * fg3) / fga
      TS%        = points / (2 * (fga + 0.44 * fta))
      ORB%       = orb / (orb + opp_drb)
      DRB%       = drb / (drb + opp_orb)
      TRB%       = trb / (trb + opp_trb)
      TOV%       = tov / possessions
      opp_TOV%   = opp_tov / possessions
    """
    pace = (own.possessions + opp.possessions) / 2.0
    ortg = _ratio(own.points * 100.0, own.possessions)
    drtg = _ratio(opp.points * 100.0, opp.possessions)
    nrtg = (ortg - drtg) if (ortg is not None and drtg is not None) else None
    return TeamGameDerived(
        game_id=game_id,
        game_date=game_date,
        team_id=own.team_id,
        opponent_id=opp.team_id,
        points=own.points,
        points_allowed=opp.points,
        margin=float(own.points - opp.points),
        possessions=own.possessions,
        opp_possessions=opp.possessions,
        pace=pace,
        ortg=ortg,
        drtg=drtg,
        nrtg=nrtg,
        efg_pct=_ratio(own.fg + 0.5 * own.fg3, own.fga),
        ts_pct=_ratio(own.points, 2.0 * (own.fga + 0.44 * own.fta)),
        fg3_pct=_ratio(own.fg3, own.fg3a),
        ft_pct=_ratio(own.ft, own.fta),
        orb_pct=_ratio(own.orb, own.orb + opp.drb),
        drb_pct=_ratio(own.drb, own.drb + opp.orb),
        trb_pct=_ratio(own.trb, own.trb + opp.trb),
        tov_pct=_ratio(own.tov, own.possessions),
        opp_tov_pct=_ratio(opp.tov, own.possessions),
    )
