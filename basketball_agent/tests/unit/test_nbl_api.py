"""NBL official-API adapter tests (Phase 8-D).

The golden fixture is REAL data: NBL26 (2025-26) Championship Final,
Sydney Kings 113 - 101 Adelaide 36ers, 2026-04-05 Qudos Bank Arena
(1 OT; official period-0 totals rows from schedule.nbl.com.au).
"""
from __future__ import annotations

import json
from datetime import timezone

UTC = timezone.utc

import pytest

from basketball_agent.data.collectors.nbl_api import (
    DATASET_MARKER,
    NBLDataError,
    load_nbl_bundle_file,
    parse_nbl_bundle,
    resolve_team,
)
from basketball_agent.data.schemas.base import Competition, GameStatus, League

FIN_ID = "24d0eebf-20c2-11f1-91dc-039d1426885f"


def _cal_entry(**overrides):
    entry = {
        "id": FIN_ID,
        "starts_at_ms": 1775363400000,  # 2026-04-05T04:30:00Z
        "round_label": "CHAMPIONSHIP",
        "match_title": "Sydney Kings v Adelaide 36ers men 05-04-2026",
        "match_slug": "sydney-kings-v-adelaide-36ers-men-05-04-2026",
        "venue_name": "Qudos Bank Arena",
        "home": {"id": "h1", "name": "Sydney Kings", "team_code": "SYD"},
        "away": {"id": "a1", "name": "Adelaide 36ers", "team_code": "ADL"},
        "home_score": 113, "away_score": 101,
        "status_raw": "CONFIRMED", "phase": "complete",
        "season_type": "regular",
    }
    entry.update(overrides)
    return entry


def _totals_row(points, minutes, fgm, fga, tpm, tpa, ftm, fta, orb, drb,
                ast, tov, stl, blk, fouls, period="0", ptype="regular"):
    return {"period": period, "period_type": ptype, "minutes": minutes,
            "points": points, "field_goals_made": fgm,
            "field_goals_attempted": fga, "three_points_made": tpm,
            "three_points_attempted": tpa, "free_throws_made": ftm,
            "free_throws_attempted": fta, "offensive_rebounds": orb,
            "defensive_rebounds": drb, "assists": ast, "turnovers": tov,
            "steals": stl, "blocks": blk, "fouls": fouls}


# real totals rows from the 2026 NBL final
_HOME_TOTALS = _totals_row(113, 225, 41, 82, 13, 35, 18, 26, 17, 31, 25, 9, 8, 3, 24)
_AWAY_TOTALS = _totals_row(101, 225, 32, 80, 12, 42, 25, 30, 19, 30, 24, 13, 8, 1, 24)
_HOME_OT = _totals_row(18, 25, 7, 10, 1, 4, 3, 3, 2, 2, 3, 1, 5, 1, 3,
                       period="11", ptype="overtime")
_AWAY_OT = _totals_row(6, 25, 2, 7, 0, 4, 2, 3, 3, 1, 2, 6, 1, 0, 3,
                       period="11", ptype="overtime")


def _bundle(**cal_overrides):
    return {
        "dataset": DATASET_MARKER,
        "league": "NBL",
        "year": 2025,
        "source": "https://schedule.nbl.com.au/api/calendar",
        "fetched_at": "2026-09-27T00:00:00+00:00",
        "n_calendar_matches": 1,
        "n_complete": 1,
        "calendar": {"matches": [_cal_entry(**cal_overrides)]},
        "matches": {FIN_ID: {
            "id": FIN_ID, "match_status": "complete",
            "start_time_datetime": "2026-04-05T04:30:00",
            "home_score": 113, "away_score": 101,
            "round": "CHAMPIONSHIP", "season_type": "regular",
            "home_team": {"team_code": "SYD", "name": "Sydney Kings"},
            "away_team": {"team_code": "ADL", "name": "Adelaide 36ers"},
            # deep copy: tests mutate these rows and must not poison the
            # module-level golden data
            "team_match_statistics": json.loads(json.dumps([
                _HOME_TOTALS, _AWAY_TOTALS, _HOME_OT, _AWAY_OT,
            ])),
        }},
    }


class TestTeamResolution:
    def test_codes_namespace(self):
        assert resolve_team("SYD") == "NBL-SYD"
        assert resolve_team("adl") == "NBL-ADL"

    def test_implausible_code_fails(self):
        with pytest.raises(NBLDataError, match="implausible NBL team_code"):
            resolve_team("TOOLONGCODE")


class TestBundleParsing:
    def test_real_final_golden(self):
        raw = parse_nbl_bundle(_bundle())
        assert raw["_meta"]["n_games"] == 1
        game = raw["games"][0]
        assert game["game_id"] == "NBL25-0001"
        assert game["league"] == "NBL"
        assert game["season"] == "2025-26"
        assert game["home_team_id"] == "NBL-SYD"
        assert game["away_team_id"] == "NBL-ADL"
        assert game["home_score"] == 113 and game["away_score"] == 101
        assert game["status"] == "FINAL"
        assert game["competition"] == "PLAYOFF"  # CHAMPIONSHIP
        # 2026-04-05T04:30:00Z
        assert game["game_date"].replace("+00:00", "Z") == "2026-04-05T04:30:00Z"

        home = next(ts for ts in raw["team_stats"] if ts["team_id"] == "NBL-SYD")
        # 2P/3P split combined
        assert home["fg"] == 41 and home["fga"] == 82
        assert home["fg3"] == 13 and home["fg3a"] == 35
        assert home["ft"] == 18 and home["fta"] == 26
        assert home["orb"] == 17 and home["drb"] == 31 and home["trb"] == 48
        assert home["minutes"] == 225  # 200 + 1 OT x 25
        assert home["possessions"] == round(82 + 0.44 * 26 - 17 + 9, 1)
        assert raw["_meta"]["possessions_estimated_for"] == 2
        teams = {t["team_id"]: t for t in raw["teams"]}
        assert teams["NBL-SYD"]["league"] == "NBL"

    def test_regular_round_maps_to_regular_season(self):
        bundle = _bundle(**{"round_label": "14"})
        raw = parse_nbl_bundle(bundle)
        assert raw["games"][0]["competition"] == "REGULAR_SEASON"

    def test_exhibitions_dropped_and_counted(self):
        bundle = _bundle()
        bundle["calendar"]["matches"].append(
            _cal_entry(id="ex1", round_label="PRESEASON", season_type="preseason",
                       home_score=None, away_score=None, phase="complete"))
        bundle["calendar"]["matches"].append(
            _cal_entry(id="ex2", round_label="BLITZ", season_type="preseason",
                       home_score=None, away_score=None, phase="complete"))
        bundle["calendar"]["matches"].append(
            _cal_entry(id="ex3", round_label="NBAxNBL", season_type="regular",
                       home_score=None, away_score=None, phase="complete"))
        raw = parse_nbl_bundle(bundle)
        assert raw["_meta"]["n_exhibitions_dropped"] == 3
        assert raw["_meta"]["n_games"] == 1

    def test_unknown_round_label_fails(self):
        bundle = _bundle(round_label="SOMETHING NEW")
        with pytest.raises(NBLDataError, match="unknown NBL round_label"):
            parse_nbl_bundle(bundle)

    def test_postponed_mapping(self):
        bundle = _bundle(status_raw="POSTPONED", phase="postponed",
                         home_score=None, away_score=None)
        bundle["matches"] = {}
        raw = parse_nbl_bundle(bundle)
        assert raw["games"][0]["status"] == "POSTPONED"
        assert raw["_meta"]["n_postponed"] == 1

    def test_future_matches_skipped(self):
        bundle = _bundle(status_raw="SCHEDULED", phase="upcoming",
                         home_score=None, away_score=None)
        bundle["matches"] = {}
        raw = parse_nbl_bundle(bundle)
        assert raw["_meta"]["n_games"] == 0

    def test_totals_score_mismatch_fails(self):
        bundle = _bundle(home_score=999)
        with pytest.raises(NBLDataError, match="calendar score .* != detail score"):
            parse_nbl_bundle(bundle)

    def test_wrong_minutes_fail(self):
        bundle = _bundle()
        bundle["matches"][FIN_ID]["team_match_statistics"][0]["minutes"] = 240
        with pytest.raises(NBLDataError, match="team minutes"):
            parse_nbl_bundle(bundle)

    def test_identity_violation_fails(self):
        bundle = _bundle()
        bundle["matches"][FIN_ID]["team_match_statistics"][1]["free_throws_made"] = 30
        with pytest.raises(NBLDataError, match="points identity"):
            parse_nbl_bundle(bundle)

    def test_wrong_marker_fails(self):
        bundle = _bundle()
        bundle["dataset"] = "something-else"
        with pytest.raises(NBLDataError, match="not an NBL bundle"):
            parse_nbl_bundle(bundle)

    def test_missing_detail_for_final_fails(self):
        bundle = _bundle()
        bundle["matches"] = {}
        with pytest.raises(NBLDataError, match="no fetched detail"):
            parse_nbl_bundle(bundle)

    def test_load_from_file(self, tmp_path):
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(_bundle()), encoding="utf-8")
        raw = load_nbl_bundle_file(path)
        assert raw["_meta"]["n_games"] == 1

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_nbl_bundle_file(tmp_path / "nope.json")


class TestNBLIngestGate:
    def test_bundle_passes_validation_and_ingests(self, tmp_path):
        from datetime import datetime

        from basketball_agent.agents.validation_agent import DataValidationAgent
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.loader import DataIngestor
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.validators.parsing import parse_dataset

        raw = parse_nbl_bundle(_bundle())
        dataset, parse_errors = parse_dataset(raw)
        assert not parse_errors
        report = DataValidationAgent(
            now=datetime(2026, 5, 1, tzinfo=UTC)
        ).validate(dataset)
        assert not report.has_errors, report.errors[:3]

        db = Database(tmp_path / "nbl.db")
        db.initialize()
        DataIngestor(db, now=datetime(2026, 5, 1, tzinfo=UTC)).ingest(raw)
        repo = Repository(db)
        game = repo.get_game("NBL25-0001")
        assert game.league is League.NBL
        assert game.competition is Competition.PLAYOFF


    def test_regulation_minutes_drift_is_bounded(self):
        """Real find (NBL25-0026/NBL-SEM): +2 min team minutes drift, same
        class as the ACB playTime drift.  Bounded drift passes and is
        counted; the stored minutes stay verbatim."""
        bundle = _bundle()
        for row in bundle["matches"][FIN_ID]["team_match_statistics"]:
            if row["period"] == "0" and row["points"] == 101:
                row["minutes"] = 226  # +1 min off the 225 expectation
        raw = parse_nbl_bundle(bundle)
        assert raw["_meta"]["team_minutes_drifted"] == 1
        away = next(ts for ts in raw["team_stats"] if ts["team_id"] == "NBL-ADL")
        assert away["minutes"] == 226  # retained verbatim, not repaired

    def test_wild_minutes_drift_still_fails(self):
        bundle = _bundle()
        for row in bundle["matches"][FIN_ID]["team_match_statistics"]:
            if row["period"] == "0" and row["points"] == 101:
                row["minutes"] = 245  # +20 min off the 225 expectation
        with pytest.raises(NBLDataError, match="team minutes"):
            parse_nbl_bundle(bundle)
