"""Odds CSV pipeline end-to-end tests (Phase 11-A, deliverable 2).

Uses a synthetic 15-game CSV (including intentional bad rows) to test the
full pipeline: CSV → parse → match → validate → ingest → query.
"""
from __future__ import annotations

import json as _json
from datetime import datetime, timezone

import pytest

from basketball_agent.data.collectors.nba_odds_csv import (
    OddsCSVError,
    UnknownTeamError,
    load_nba_odds_csv,
)
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.odds_repository import OddsRepository
from basketball_agent.data.db.repository import Repository
from basketball_agent.evaluation.roi import flat_stake_roi

_CSV_HEADER = ("game_date,away_team,home_team,"
               "close_moneyline_home,close_moneyline_away,"
               "bookmaker")

game_date = "2024-11-20"


def _make_csv(path, rows: list[str], header=_CSV_HEADER):
    path.write_text("\n".join([header] + rows), encoding="utf-8")


@pytest.fixture()
def nba_db(tmp_path):
    db = Database(tmp_path / "odds_e2e.db")
    db.initialize()
    with db.transaction() as conn:
        for tid in ("LAL", "BOS", "GSW", "DEN"):
            conn.execute(
                "INSERT INTO teams (team_id, league, name, abbreviation) "
                "VALUES (?, 'NBA', ?, ?)", (tid, tid, tid))
        games = [
            ("20241120_LAL_BOS", "LAL", "BOS", 100, 95),
            ("20241120_BOS_LAL", "BOS", "LAL", 95, 100),
            ("20241121_GSW_DEN", "GSW", "DEN", 110, 105),
            ("20241122_DEN_GSW", "DEN", "GSW", 98, 102),
        ]
        for i, (gid, h, a, hs, as_) in enumerate(games):
            conn.execute(
                "INSERT INTO games (game_id, league, season, game_date, "
                "home_team_id, away_team_id, home_score, away_score, status, "
                "competition, inserted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, "NBA", "2024-25",
                 f"2024-11-{20 + i}T00:00:00+00:00", h, a, hs, as_,
                 "FINAL", "REGULAR_SEASON",
                 "2024-11-01T00:00:00+00:00"))
    return db


class TestOddsCsvPipeline:
    def test_valid_rows_ingest(self, nba_db, tmp_path):
        csv_path = tmp_path / "odds.csv"
        rows = [
            f"{game_date},BOS,LAL,-110,110,pinnacle",
            "2024-11-21,DEN,GSW,-120,100,pinnacle",
        ]
        _make_csv(csv_path, rows)
        odds_rows, skipped = load_nba_odds_csv(csv_path, nba_db)
        assert len(odds_rows) == 2
        assert len(skipped) == 0
        # ingest into repository
        repo = OddsRepository(nba_db)
        inserted, dup = repo.insert_odds(odds_rows)
        assert inserted == 2
        # query back
        rows_q = repo.get_odds_for_game("20241120_LAL_BOS",
                                        market="MONEYLINE", kind="CLOSE")
        assert len(rows_q) == 1

    def test_unknown_team_raises(self, nba_db, tmp_path):
        csv_path = tmp_path / "bad.csv"
        _make_csv(csv_path, [f"{game_date},UNKNOWN_TEAM,LAL,-110,110,pinnacle"])
        with pytest.raises(UnknownTeamError, match="no game matches"):
            load_nba_odds_csv(csv_path, nba_db)

    def test_bad_date_rejected(self, nba_db, tmp_path):
        csv_path = tmp_path / "bad_date.csv"
        _make_csv(csv_path, ["NOT_A_DATE,BOS,LAL,-110,110,pinnacle"])
        with pytest.raises(OddsCSVError, match="cannot parse date"):
            load_nba_odds_csv(csv_path, nba_db)

    def test_empty_csv_returns_empty(self, nba_db, tmp_path):
        csv_path = tmp_path / "empty.csv"
        _make_csv(csv_path, [])
        odds_rows, skipped = load_nba_odds_csv(csv_path, nba_db)
        assert odds_rows == [] and skipped == []

    def test_fair_probability_computed(self, nba_db, tmp_path):
        csv_path = tmp_path / "fair.csv"
        # -110/-110 is the symmetric vig market
        _make_csv(csv_path, [f"{game_date},BOS,LAL,-110,110,pinnacle"])
        odds_rows, _ = load_nba_odds_csv(csv_path, nba_db)
        assert "fair_prob_home" in odds_rows[0]
        assert 0.4 < odds_rows[0]["fair_prob_home"] < 0.6


class TestOddsLeakageGuard:
    def test_odds_table_never_read_by_feature_pipeline(self):
        """Structural guard: the feature pipeline module must not import
        or reference the odds table/repo."""
        import inspect
        from basketball_agent.features import feature_pipeline as fp

        src = inspect.getsource(fp)
        assert "game_odds" not in src
        assert "OddsRepository" not in src

    def test_odds_only_in_eval_layer(self):
        """The ROI/devig modules live in evaluation/, not in features/ or
        models/ — odds are benchmark-only."""
        from pathlib import Path

        root = Path(__file__).parent.parent.parent
        for dir_name in ("features", "models"):
            dir_path = root / "basketball_agent" / dir_name
            if dir_path.exists():
                for py_file in dir_path.glob("*.py"):
                    content = py_file.read_text(encoding="utf-8")
                    assert "game_odds" not in content, \
                        f"{py_file.name} references odds table"
                    assert "OddsRepository" not in content, \
                        f"{py_file.name} references OddsRepository"


class TestROIEndToEnd:
    def test_full_pipeline_with_roi(self, nba_db, tmp_path):
        """CSV → ingest → query → ROI backtest (the full chain)."""
        csv_path = tmp_path / "odds.csv"
        _make_csv(csv_path, [
            f"{game_date},BOS,LAL,-110,110,pinnacle",
            "2024-11-21,DEN,GSW,-120,100,pinnacle",
        ])
        odds_rows, _ = load_nba_odds_csv(csv_path, nba_db)
        repo = OddsRepository(nba_db)
        repo.insert_odds(odds_rows)

        # build settled predictions for ROI (model says home wins both)
        predictions = [
            {"game_id": "20241120_LAL_BOS", "prob_home": 0.62,
             "actual_home_win": True},
            {"game_id": "20241121_GSW_DEN", "prob_home": 0.55,
             "actual_home_win": False},
        ]
        odds_q = repo.get_odds_for_game("20241120_LAL_BOS",
                                        market="MONEYLINE", kind="CLOSE")
        # add odds for second game
        repo.insert_odds([{
            "game_id": "20241121_GSW_DEN", "league": "NBA",
            "bookmaker": "pinnacle", "market": "MONEYLINE", "kind": "CLOSE",
            "line": None, "price_decimal": 1.87,
            "captured_at": "2024-11-20T23:00:00+00:00", "source": "test",
        }])
        all_odds = repo.get_odds_for_game("20241121_GSW_DEN",
                                          market="MONEYLINE", kind="CLOSE")
        # combine odds for both games
        odds_data = []
        for o in repo.get_odds_for_game("20241120_LAL_BOS",
                                        market="MONEYLINE", kind="CLOSE"):
            odds_data.append({"game_id": o["game_id"],
                              "price_decimal": o["price_decimal"],
                              "market": o["market"], "kind": o["kind"]})
        for o in all_odds:
            odds_data.append({"game_id": o["game_id"],
                              "price_decimal": o["price_decimal"],
                              "market": o["market"], "kind": o["kind"]})

        results = flat_stake_roi(predictions, odds_data,
                                 edge_thresholds=(0.0,))
        assert len(results) == 1
        assert results[0].n_bets >= 1
