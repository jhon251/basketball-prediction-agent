"""Phase 12-F: confidence calibration table math + API attachment."""
from __future__ import annotations

import json

import pytest

from basketball_agent.evaluation.confidence_calibration import (
    bucket_of,
    bucket_label,
    build_family_table,
    build_payload,
    load_payload,
    lookup,
    save_payload,
)


def _entry(p_home: float, outcome: bool) -> dict:
    return {"game_id": "g", "season": "s", "outcome": outcome,
            "p_random": 0.5, "p_elo": p_home, "p_strength": p_home,
            "p_simulation": p_home, "p_market": None}


class TestBuckets:
    def test_edges(self):
        assert bucket_of(0.5) == (0.5, 0.55)
        assert bucket_of(0.549) == (0.5, 0.55)
        assert bucket_of(0.55) == (0.55, 0.60)
        assert bucket_of(0.999) == (0.95, 1.0)
        assert bucket_of(1.0) == (0.95, 1.0)

    def test_out_of_range(self):
        assert bucket_of(0.49) is None
        assert bucket_of(-0.1) is None
        assert bucket_of(1.01) is None

    def test_label(self):
        assert bucket_label(0.5, 0.55) == "0.50-0.55"


class TestBuildTable:
    def test_picked_side_accounting(self):
        # p_home 0.30 -> picked AWAY with p_picked 0.70, bucket 0.70-0.75;
        # outcome False (home lost) -> picked side won -> hit
        rows = build_family_table(
            [_entry(0.30, False), _entry(0.30, True)], "elo")
        assert len(rows) == 1
        row = rows[0]
        assert row["bucket"] == "0.70-0.75"
        assert row["n"] == 2 and row["hits"] == 1
        assert row["rate"] == 0.5

    def test_home_pick_bucket(self):
        rows = build_family_table([_entry(0.58, True)], "simulation")
        assert rows[0]["bucket"] == "0.55-0.60"
        assert rows[0]["hits"] == 1

    def test_empty_buckets_dropped(self):
        rows = build_family_table([_entry(0.51, True)], "elo")
        assert [r["bucket"] for r in rows] == ["0.50-0.55"]

    def test_missing_family_ignored(self):
        assert build_family_table(
            [{"outcome": True, "p_elo": 0.9}], "strength") == []


def _make_payload() -> dict:
    tables = {"NBA": {"simulation": [
        {"bucket": "0.60-0.65", "lo": 0.60, "hi": 0.65,
         "n": 100, "hits": 63, "rate": 0.63}]}}
    return build_payload(tables, windows={"NBA": ["2022-10-01",
                                                  "latest"]},
                         n_simulations=300, seed=7)


class TestLookupAndPersistence:
    def test_lookup_hit(self):
        cal = lookup(_make_payload(), "NBA", "simulation", 0.62)
        assert cal == {"bucket": "0.60-0.65", "picked_side_is_home": True,
                       "n": 100, "empirical_hit_rate": 0.63}

    def test_lookup_away_pick(self):
        cal = lookup(_make_payload(), "NBA", "simulation", 0.38)
        assert cal is not None
        assert cal["picked_side_is_home"] is False
        assert cal["bucket"] == "0.60-0.65"

    def test_lookup_misses(self):
        p = _make_payload()
        assert lookup(p, "NBL", "simulation", 0.62) is None
        assert lookup(p, "NBA", "elo", 0.62) is None
        assert lookup(None, "NBA", "simulation", 0.62) is None
        assert lookup(p, "NBA", "simulation", 0.52) is None  # bucket absent

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "cal.json"
        save_payload(_make_payload(), path)
        assert load_payload(path) is not None
        assert load_payload(tmp_path / "missing.json") is None
        cal = lookup(load_payload(path), "NBA", "simulation", 0.61)
        assert cal["empirical_hit_rate"] == 0.63


class TestApiAttachment:
    def test_predict_includes_calibration_when_file_present(
            self, ingested_db, tmp_path, _future_target_from_conftest):
        from basketball_agent.api.server import BasketballApiApp, PredictRequest

        path = tmp_path / "cal.json"
        save_payload(_make_payload(), path)
        app = BasketballApiApp(ingested_db, state_dir=tmp_path,
                               n_simulations=40, seed=7,
                               calibration_path=path)
        out = app.predict(PredictRequest(game_id="FUTURE-0001"))
        assert "calibration" in out
        # fixture table has only the 0.60-0.65 bucket — either a hit or a
        # clean miss, never a crash
        assert out["calibration"] is None or \
            "empirical_hit_rate" in out["calibration"]

    def test_predict_calibration_none_without_file(
            self, ingested_db, tmp_path, _future_target_from_conftest):
        from basketball_agent.api.server import BasketballApiApp, PredictRequest

        app = BasketballApiApp(ingested_db, state_dir=tmp_path,
                               n_simulations=40, seed=7,
                               calibration_path=tmp_path / "nope.json")
        out = app.predict(PredictRequest(game_id="FUTURE-0001"))
        assert out["calibration"] is None


@pytest.fixture
def _future_target_from_conftest(ingested_db):
    """Reuse the same future-game fixture shape as test_api_server."""
    from datetime import datetime, timedelta, timezone

    from basketball_agent.data.schemas.base import Competition, GameStatus, League
    from basketball_agent.data.schemas.game import Game
    from basketball_agent.data.db.repository import Repository

    Repository(ingested_db).insert_game(Game(
        game_id="FUTURE-0001", league=League.NBA, season="2099-00",
        game_date=datetime.now(timezone.utc) + timedelta(days=3),
        home_team_id="LAL", away_team_id="BOS",
        home_score=None, away_score=None,
        status=GameStatus.SCHEDULED,
        competition=Competition.REGULAR_SEASON,
    ))
    return ingested_db
