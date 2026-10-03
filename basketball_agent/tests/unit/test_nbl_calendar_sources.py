"""NBL calendar discovery source tests (Phase 11-A.4 hotfix).

The 2026-27 NBL calendar API dropped the legacy ``start_time_datetime``
text field (every row's kickoff now lives only in epoch-millis
``starts_at_ms``) — the old ``[:10]`` slice produced empty dates and the
production loop exploded on 145 rows with ``time data ' 00:00'``.  These
tests pin the fixed row contract: UTC kickoff from ``starts_at_ms``
(legacy field kept as fallback), ``NBL-{team_code}`` team refs (the id
shape the teams table and the season-refresh fetcher share), and the
counted-not-guessed empty-date case.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.collection import sources

UTC = timezone.utc

_KICKOFF_MS = 1775363400000          # 2026-04-05T04:30:00Z


def _cal_row(**overrides) -> dict:
    row = {
        "id": "m1",
        "starts_at_ms": _KICKOFF_MS,
        "phase": "upcoming",
        "home": {"id": "h-uuid", "name": "Sydney Kings", "team_code": "SYD"},
        "away": {"id": "a-uuid", "name": "Adelaide 36ers", "team_code": "ADL"},
        "home_score": None, "away_score": None,
    }
    row.update(overrides)
    return row


def _fetch(monkeypatch, matches: list[dict]) -> list[dict]:
    monkeypatch.setattr(sources, "_get_json",
                        lambda url: {"matches": matches})
    return sources.nbl_matches(2026)


class TestNblKickoffUtc:
    def test_epoch_millis_to_utc_date_and_time(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row()])
        assert rows[0]["date"] == "2026-04-05"
        assert rows[0]["match_time"] == "04:30"
        # contract: UTC, matches _league_tz("NBL") in the production loop
        assert (datetime.strptime("2026-04-05 04:30", "%Y-%m-%d %H:%M")
                .replace(tzinfo=UTC)
                == datetime.fromtimestamp(_KICKOFF_MS / 1000, tz=UTC))

    def test_seconds_shaped_epoch_normalised(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row(starts_at_ms=1775363400)])
        assert rows[0]["date"] == "2026-04-05"
        assert rows[0]["match_time"] == "04:30"

    def test_legacy_text_field_fallback(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row(
            starts_at_ms=None, start_time_datetime="2025-12-05 09:30:00")])
        assert rows[0]["date"] == "2025-12-05"
        assert rows[0]["match_time"] == "09:30"

    def test_missing_kickoff_yields_empty_not_garbage(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row(starts_at_ms=None)])
        assert rows[0]["date"] == ""
        assert rows[0]["match_time"] == ""


class TestNblTeamRefs:
    def test_team_code_maps_to_canonical_id(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row()])
        assert rows[0]["home_id"] == "NBL-SYD"
        assert rows[0]["away_id"] == "NBL-ADL"

    def test_implausible_code_falls_back_to_raw_id(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row(
            home={"id": "h-uuid", "name": "X", "team_code": "NOT_VALID!"})])
        assert rows[0]["home_id"] == "h-uuid"

    def test_missing_team_object_falls_back_to_raw_id(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row(away=None)])
        assert rows[0]["away_id"] == "None"


class TestNblRowContract:
    def test_status_and_scores_pass_through(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row(
            phase="complete", home_score=113, away_score=101)])
        assert rows[0]["status_raw"] == "complete"
        assert rows[0]["home_score"] == 113
        assert rows[0]["away_score"] == 101

    def test_source_id_and_names(self, monkeypatch):
        rows = _fetch(monkeypatch, [_cal_row()])
        assert rows[0]["source_id"] == "m1"
        assert rows[0]["home"] == "Sydney Kings"
        assert rows[0]["away"] == "Adelaide 36ers"

    def test_real_2026_calendar_shape_end_to_end(self, monkeypatch):
        """The exact shape the live API returned on 2026-10-03: no
        start_time_datetime key at all, starts_at_ms everywhere."""
        kickoff = datetime(2026, 10, 4, 4, 0, tzinfo=UTC)
        live_row = {
            "id": "36fdee6b-58ad-11f1-ac5d-19f7d3a187d4",
            "starts_at_ms": int(kickoff.timestamp() * 1000),
            "phase": "upcoming",
            "home": {"id": "3bdb8ad6-7cf0-464d-b62c-eaca9145d062",
                     "name": "South East Melbourne Phoenix",
                     "team_code": "SEM"},
            "away": {"id": "5e3f...", "name": "Illawarra Hawks",
                     "team_code": "ILL"},
            "home_score": None, "away_score": None,
        }
        rows = _fetch(monkeypatch, [live_row])
        assert rows[0]["date"] == "2026-10-04"
        assert rows[0]["match_time"] == "04:00"
        assert rows[0]["home_id"] == "NBL-SEM"
        assert rows[0]["away_id"] == "NBL-ILL"
