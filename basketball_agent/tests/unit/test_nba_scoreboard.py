"""Phase 12-B: ESPN NBA scoreboard adapter (data/collectors/nba_scoreboard).

Parsing tests run against a saved REAL payload (2026-10-23, 12 events,
fetched live from site.api.espn.com) — no network in tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from basketball_agent.data.collectors.nba_scoreboard import (
    UnknownTeamError,
    load_scoreboard_file,
    parse_scoreboard_events,
    upcoming_days,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "nba_scoreboard_20261023.json"


def _event(**overrides) -> dict:
    base = {
        "id": "401909848",
        "date": "2026-10-23T20:00Z",
        "name": "Chicago Bulls at Milwaukee Bucks",
        "season": {"year": 2027, "type": 2, "slug": "regular-season"},
        "status": {"type": {"state": "pre"}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "score": "0",
             "team": {"abbreviation": "MIL"}},
            {"homeAway": "away", "score": "0",
             "team": {"abbreviation": "CHI"}},
        ]}],
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="module")
def real_rows() -> list[dict]:
    payload = load_scoreboard_file(FIXTURE)
    return parse_scoreboard_events(payload["events"])


class TestRealFixture:
    def test_twelve_events_parsed(self, real_rows):
        assert len(real_rows) == 12

    def test_row_contract(self, real_rows):
        r = next(x for x in real_rows
                 if x["home_id"] == "MIL" and x["away_id"] == "CHI")
        assert r["source_id"] == "401909848"
        assert r["date"] == "2026-10-23"
        assert r["match_time"] == "20:00"  # stored in UTC
        assert r["status_raw"] == "0"
        assert r["home_score"] == "" and r["away_score"] == ""
        # ESPN year 2027 + type 2 -> label 2026-27 (end-year convention)
        assert r["season"] == "2026-27"
        assert r["competition"] == "REGULAR_SEASON"

    def test_all_rows_have_canonical_ids(self, real_rows):
        for r in real_rows:
            for tid in (r["home_id"], r["away_id"]):
                assert tid.isupper() and len(tid) == 3

    def test_no_duplicate_source_ids(self, real_rows):
        ids = [r["source_id"] for r in real_rows]
        assert len(ids) == len(set(ids))


class TestMappings:
    @pytest.mark.parametrize("espn,canonical", [
        ("GS", "GSW"), ("NO", "NOP"), ("NY", "NYK"),
        ("SA", "SAS"), ("UTAH", "UTA"), ("WSH", "WAS"),
        ("BOS", "BOS"),
    ])
    def test_abbreviation_remap(self, espn, canonical):
        ev = _event(competitions=[{"competitors": [
            {"homeAway": "home", "score": "0",
             "team": {"abbreviation": espn}},
            {"homeAway": "away", "score": "0",
             "team": {"abbreviation": "BOS"}},
        ]}])
        rows = parse_scoreboard_events([ev])
        assert rows[0]["home_id"] == canonical

    def test_unknown_abbr_fails_loud(self):
        ev = _event(competitions=[{"competitors": [
            {"homeAway": "home", "score": "0",
             "team": {"abbreviation": "XXX"}},
            {"homeAway": "away", "score": "0",
             "team": {"abbreviation": "BOS"}},
        ]}])
        with pytest.raises(UnknownTeamError):
            parse_scoreboard_events([ev])


class TestLifecycleRules:
    def test_in_progress_dropped(self):
        ev = _event(status={"type": {"state": "in"}})
        assert parse_scoreboard_events([ev]) == []

    def test_preseason_dropped(self):
        ev = _event(season={"year": 2027, "type": 1, "slug": "preseason"})
        assert parse_scoreboard_events([ev]) == []

    def test_playoff_tagged_not_regular(self):
        ev = _event(season={"year": 2027, "type": 3, "slug": "postseason"})
        rows = parse_scoreboard_events([ev])
        assert rows[0]["competition"] == "PLAYOFF"

    def test_final_carries_scores(self):
        ev = _event(
            status={"type": {"state": "post"}},
            competitions=[{"competitors": [
                {"homeAway": "home", "score": "112",
                 "team": {"abbreviation": "MIL"}},
                {"homeAway": "away", "score": "108",
                 "team": {"abbreviation": "CHI"}},
            ]}])
        rows = parse_scoreboard_events([ev])
        assert rows[0]["status_raw"] == "1"
        assert rows[0]["home_score"] == "112"
        assert rows[0]["away_score"] == "108"

    def test_final_without_score_dropped(self):
        ev = _event(
            status={"type": {"state": "post"}},
            competitions=[{"competitors": [
                {"homeAway": "home", "score": "",
                 "team": {"abbreviation": "MIL"}},
                {"homeAway": "away", "score": "108",
                 "team": {"abbreviation": "CHI"}},
            ]}])
        assert parse_scoreboard_events([ev]) == []

    def test_missing_date_dropped(self):
        ev = _event(date="")
        assert parse_scoreboard_events([ev]) == []


class TestUpcomingDays:
    def test_window_is_inclusive_range(self):
        from datetime import date
        days = upcoming_days(3, today=date(2026, 10, 23))
        assert [d.isoformat() for d in days] == [
            "2026-10-23", "2026-10-24", "2026-10-25"]
