"""ACB fetch tool: pure-function tests (no network)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tools.fetch_acb_bundle import (
    DATASET_MARKER,
    FetchError,
    build_bundle,
    calendar_matches,
    resolve_edition,
    season_code,
    season_label,
)

UTC = timezone.utc
EDITION = {"id": 90, "seasonStartYear": 2025, "seasonEndYear": 2026, "rounds": [], "weeks": []}
ROOT = {
    "availableFilters": {
        "competitions": [{"id": 1, "name": "Liga Endesa", "editions": []}],
        "seasons": [{"id": 89, "seasonStartYear": 2024, "seasonEndYear": 2025},
                    EDITION],
    }
}
CALENDAR = {
    "selectedFilters": {"competition": 1, "season": 90},
    "teams": [{"id": 4384, "clubId": 14, "abbreviatedName": "UNI", "fullName": "Unicaja"}],
    "rounds": [
        {"id": 5884, "roundNumber": 1, "subphaseId": None, "subphaseNumber": None,
         "matches": [{"id": 104459, "homeTeamId": 4384, "awayTeamId": 4389,
                      "homeScore": 86, "awayScore": 68,
                      "startDateTime": "2025-10-04T16:00:00Z",
                      "matchStatus": "FINALIZED"}]},
        {"id": 6013, "roundNumber": 46, "subphaseId": 291, "subphaseNumber": 4,
         "matches": [{"id": 105373, "homeTeamId": 4386, "awayTeamId": 4397,
                      "homeScore": 84, "awayScore": 108,
                      "startDateTime": "2026-06-24T18:00:00Z",
                      "matchStatus": "FINALIZED"}]},
    ],
}


class TestSeasonLabels:
    def test_season_code_and_label(self):
        assert season_code(EDITION) == "ACB2025"
        assert season_label(EDITION) == "2025-26"

    def test_label_uses_the_source_end_year_when_present(self):
        assert season_label({"seasonStartYear": 1999, "seasonEndYear": 2000}) == "1999-00"

    def test_resolve_edition_returns_the_source_record(self):
        assert resolve_edition(ROOT, 1, 90) is EDITION

    def test_unknown_edition_fails(self):
        with pytest.raises(FetchError, match="edition 77 not present"):
            resolve_edition(ROOT, 1, 77)


class TestCalendarFlattening:
    def test_matches_carry_their_round_context(self):
        matches = calendar_matches(CALENDAR)
        assert [m["id"] for m in matches] == [104459, 105373]
        assert matches[0]["roundNumber"] == 1 and matches[0]["subphaseId"] is None
        assert matches[1]["roundNumber"] == 46 and matches[1]["subphaseId"] == 291

    def test_empty_calendar_is_empty(self):
        assert calendar_matches({}) == []


class TestBundleAssembly:
    def test_bundle_marks_the_dataset_and_counts(self):
        bundle = build_bundle(
            calendar_root=ROOT, calendar=CALENDAR, competition_id=1,
            competition_name="Liga Endesa", edition=EDITION,
            headers={"104459": {"matchId": 104459}},
            boxscores={"104459": {"teamBoxscores": []}},
            fetched_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
        )
        assert bundle["dataset"] == DATASET_MARKER
        assert bundle["edition_id"] == 90
        assert bundle["season"] == "2025-26"
        assert bundle["season_code"] == "ACB2025"
        assert bundle["n_calendar_matches"] == 2
        assert bundle["n_headers"] == 1 and bundle["n_boxscores"] == 1
        assert bundle["fetched_at"] == "2026-09-21T12:00:00+00:00"
        assert bundle["calendar_skipped"] == {}
        # keys are stringified and sorted numerically for stable archives
        assert list(bundle["headers"]) == ["104459"]

    def test_skipped_matches_are_recorded_not_hidden(self):
        bundle = build_bundle(
            calendar_root=ROOT, calendar=CALENDAR, competition_id=1,
            competition_name="Liga Endesa", edition=EDITION,
            headers={}, boxscores={}, skipped={"105373": "no payload"},
        )
        assert bundle["calendar_skipped"] == {"105373": "no payload"}
