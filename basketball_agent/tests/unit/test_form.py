"""Unit tests for recent-form features."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.features.team.boxscore import TeamGameDerived
from basketball_agent.features.team.form import last_n_stats

pytestmark = pytest.mark.unit

UTC = timezone.utc
BASE = datetime(2024, 11, 1, tzinfo=UTC)

MARGINS = [5.0, -2.0, 10.0, -4.0, 8.0, -6.0]


def make_rows(margins):
    rows = []
    for i, m in enumerate(margins):
        rows.append(TeamGameDerived(
            game_id=f"g{i}", game_date=BASE + timedelta(days=i), team_id="T",
            opponent_id="O", points=100, points_allowed=100 - m, margin=m,
            possessions=100.0, opp_possessions=100.0, pace=100.0,
            ortg=None, drtg=None, nrtg=None, efg_pct=None, ts_pct=None,
            fg3_pct=None, ft_pct=None, orb_pct=None, drb_pct=None, trb_pct=None,
            tov_pct=None, opp_tov_pct=None,
        ))
    return rows


class TestLastNStats:
    def test_last5(self):
        win_pct, avg_margin = last_n_stats(make_rows(MARGINS), 5)
        assert win_pct == pytest.approx(2 / 5)
        assert avg_margin == pytest.approx(6 / 5)

    def test_last10_covers_all(self):
        win_pct, avg_margin = last_n_stats(make_rows(MARGINS), 10)
        assert win_pct == pytest.approx(3 / 6)
        assert avg_margin == pytest.approx(11 / 6)

    def test_empty_rows(self):
        assert last_n_stats([], 5) == (None, None)

    def test_window_larger_than_history_uses_all(self):
        win_pct, _ = last_n_stats(make_rows(MARGINS), 100)
        assert win_pct == pytest.approx(3 / 6)
