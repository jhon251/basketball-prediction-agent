"""Odds repository tests (Phase 11-A, deliverable 1)."""
from __future__ import annotations

import pytest

from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.odds_repository import OddsDataError, OddsRepository


@pytest.fixture()
def odds_repo(tmp_path):
    db = Database(tmp_path / "odds.db")
    db.initialize()
    # register two teams and a game
    with db.transaction() as conn:
        for tid in ("LAL", "BOS"):
            conn.execute(
                "INSERT INTO teams (team_id, league, name, abbreviation) "
                "VALUES (?, 'NBA', ?, ?)", (tid, tid, tid))
        conn.execute(
            "INSERT INTO games (game_id, league, season, game_date, "
            "home_team_id, away_team_id, home_score, away_score, status, "
            "competition, inserted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("20241120_LAL_BOS", "NBA", "2024-25",
             "2024-11-20T00:00:00+00:00", "LAL", "BOS",
             100, 95, "FINAL", "REGULAR_SEASON",
             "2024-11-01T00:00:00+00:00"))
    return OddsRepository(db)


_VALID = {
    "game_id": "20241120_LAL_BOS", "league": "NBA",
    "bookmaker": "pinnacle", "market": "MONEYLINE", "kind": "CLOSE",
    "line": None, "price_decimal": 1.85,
    "captured_at": "2024-11-19T23:00:00+00:00", "source": "test",
}


class TestInsertOdds:
    def test_valid_insert(self, odds_repo):
        inserted, skipped = odds_repo.insert_odds([_VALID])
        assert inserted == 1 and skipped == 0
        assert odds_repo.count() == 1

    def test_duplicate_skipped(self, odds_repo):
        odds_repo.insert_odds([_VALID])
        inserted, skipped = odds_repo.insert_odds([_VALID])
        assert inserted == 0 and skipped == 1
        assert odds_repo.count() == 1

    def test_unknown_game_rejected(self, odds_repo):
        bad = dict(_VALID, game_id="UNKNOWN_GAME")
        with pytest.raises(OddsDataError, match="not found"):
            odds_repo.insert_odds([bad])

    def test_invalid_market_rejected(self, odds_repo):
        bad = dict(_VALID, market="PARLAY")
        with pytest.raises(OddsDataError, match="invalid market"):
            odds_repo.insert_odds([bad])

    def test_invalid_kind_rejected(self, odds_repo):
        bad = dict(_VALID, kind="LIVE")
        with pytest.raises(OddsDataError, match="invalid kind"):
            odds_repo.insert_odds([bad])

    def test_price_out_of_range_rejected(self, odds_repo):
        bad = dict(_VALID, price_decimal=1.0)
        with pytest.raises(OddsDataError, match="out of range"):
            odds_repo.insert_odds([bad])
        bad2 = dict(_VALID, price_decimal=101.0)
        with pytest.raises(OddsDataError, match="out of range"):
            odds_repo.insert_odds([bad2])

    def test_spread_requires_line(self, odds_repo):
        bad = dict(_VALID, market="SPREAD", line=None)
        with pytest.raises(OddsDataError, match="requires a line"):
            odds_repo.insert_odds([bad])

    def test_moneyline_line_rejected(self, odds_repo):
        bad = dict(_VALID, line=-3.5)
        with pytest.raises(OddsDataError, match="should not have a line"):
            odds_repo.insert_odds([bad])

    def test_spread_with_line_ok(self, odds_repo):
        spread = dict(_VALID, market="SPREAD", line=-3.5)
        inserted, _ = odds_repo.insert_odds([spread])
        assert inserted == 1

    def test_missing_captured_at_rejected(self, odds_repo):
        bad = {k: v for k, v in _VALID.items() if k != "captured_at"}
        with pytest.raises(OddsDataError, match="captured_at"):
            odds_repo.insert_odds([bad])


class TestQueryOdds:
    def test_get_odds_for_game(self, odds_repo):
        odds_repo.insert_odds([
            _VALID,
            dict(_VALID, market="SPREAD", line=-3.5,
                 captured_at="2024-11-19T23:00:01+00:00"),
        ])
        rows = odds_repo.get_odds_for_game("20241120_LAL_BOS")
        assert len(rows) == 2
        ml = odds_repo.get_odds_for_game("20241120_LAL_BOS", market="MONEYLINE")
        assert len(ml) == 1 and ml[0]["market"] == "MONEYLINE"

    def test_coverage(self, odds_repo):
        odds_repo.insert_odds([_VALID])
        cov = odds_repo.coverage()
        assert cov["total_odds_rows"] == 1
        assert cov["games_with_close"] == 1
        assert "NBA" in cov["by_league"]


class TestPriceLowerBound:
    def test_price_1_005_rejected(self, odds_repo):
        """DDL CHECK and repository guard both enforce >= 1.01."""
        bad = dict(_VALID, price_decimal=1.005)
        with pytest.raises(OddsDataError, match="out of range"):
            odds_repo.insert_odds([bad])

    def test_price_1_01_accepted(self, odds_repo):
        ok = dict(_VALID, price_decimal=1.01)
        inserted, _ = odds_repo.insert_odds([ok])
        assert inserted == 1
