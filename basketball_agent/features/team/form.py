"""Recent-form features computed from chronologically ordered game rows.

Form windows always use the team's *last* N games relative to the as-of
timestamp — never future games, never the game being predicted.
"""
from __future__ import annotations

from basketball_agent.features.team.boxscore import TeamGameDerived


def last_n_stats(
    rows: list[TeamGameDerived], n: int
) -> tuple[float | None, float | None]:
    """Win percentage and average margin over the last ``n`` games.

    ``rows`` must already be sorted oldest -> newest.  Returns
    ``(win_pct, avg_margin)`` or ``(None, None)`` when there are no rows.
    """
    if not rows:
        return None, None
    window = rows[-n:]
    wins = sum(1 for r in window if r.margin > 0)
    avg_margin = sum(r.margin for r in window) / len(window)
    return wins / len(window), avg_margin
