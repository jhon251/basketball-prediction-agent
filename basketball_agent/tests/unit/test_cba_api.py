"""CBA official-API adapter tests (Phase 8-E).

The golden fixture is REAL data from the 2025-26 season (official
data-server.cbaleague.com API, AES-decrypted at fetch time):

* opener: 浙江浙商证券 88 - 74 山西汾酒, 2025-12-12, 常规赛第1轮
  (regulation, quarter=4 / minutes=40 — the CBA plays FIBA 4x10 since
  2025-26);
* double-overtime: 南京天之蓝 126 - 123 深圳马可波罗, 2026-02-03,
  常规赛第23轮 (quarter=6 / minutes=50);
* a real all-star-weekend row (星锐挑战赛) that the adapter must drop.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

UTC = timezone.utc

import pytest

from basketball_agent.data.collectors.cba_api import (
    DATASET_MARKER,
    CBADataError,
    load_cba_bundle_file,
    parse_cba_bundle,
    resolve_team,
)
from basketball_agent.data.schemas.base import Competition, GameStatus, League

OPENER_ID = 100105799
OT_ID = 100105934

_TEAMS = [
    {"key": "浙江浙商证券", "value": "29128", "isDefault": 2},
    {"key": "山西汾酒", "value": "29132", "isDefault": 2},
    {"key": "南京天之蓝", "value": "29133", "isDefault": 2},
    {"key": "深圳马可波罗", "value": "29131", "isDefault": 2},
]

# real day rows (verbatim from the fetched bundle)
_OPENER_ROW = {
    "scheduleNum": None, "season": 2025,
    "homeTeamId": 29128, "visitingTeamId": 29132,
    "homeTeamName": "浙江浙商证券", "visitingTeamName": "山西汾酒",
    "matchTypeId": 1, "round": 1, "status": 4,
    "scheduleTime": "2025-12-12 19:35:00",
    "homeTeamScore": 88, "visitingTeamScore": 74,
    "quarter": 4, "minutes": 40, "seconds": None,
    "scheduleId": OPENER_ID, "isReport": 1, "isEvents": 1,
    "scheduleTypeName": "常规赛第1轮", "matchOrder": 1, "roundDesc": None,
}
_OT_ROW = {
    "scheduleNum": None, "season": 2025,
    "homeTeamId": 29133, "visitingTeamId": 29131,
    "homeTeamName": "南京天之蓝", "visitingTeamName": "深圳马可波罗",
    "matchTypeId": 1, "round": 23, "status": 4,
    "scheduleTime": "2026-02-03 19:35:00",
    "homeTeamScore": 126, "visitingTeamScore": 123,
    "quarter": 6, "minutes": 50, "seconds": None,
    "scheduleId": OT_ID, "isReport": 1, "isEvents": 1,
    "scheduleTypeName": "常规赛第23轮", "matchOrder": 226, "roundDesc": None,
}
_ALLSTAR_ROW = {
    "scheduleNum": None, "season": 2025,
    "homeTeamId": 100012386, "visitingTeamId": 100016820,
    "homeTeamName": "CBA星锐队", "visitingTeamName": "大学生联队",
    "matchTypeId": 4, "round": None, "status": 2,
    "scheduleTime": "2026-03-07 19:15:00",
    "homeTeamScore": 94, "visitingTeamScore": 76,
    "quarter": 4, "minutes": 0, "seconds": 0,
    "scheduleId": 100108575, "isReport": 1, "isEvents": 1,
    "scheduleTypeName": "星锐挑战赛", "matchOrder": 0, "roundDesc": "星锐挑战赛",
}

# real match-log rows (verbatim; identities verified against the schedule)
_OPENER_HOME_LOG = {
    "teamName": "浙江浙商证券", "rebounds": 42, "reboundsOffensive": 11,
    "scheduleTime": "2025-12-12 19:35:00", "acrossPoints": 74,
    "fieldGoalsAttempted": 64, "steals": 10, "type": 0, "points": 88,
    "result": 1, "blocked": 5, "assists": 22, "scheduleId": OPENER_ID,
    "freeThrowsPercentage": 0.629, "fieldGoalsPercentage": 0.469,
    "threePointGoals": 6, "reboundsDefensive": 31, "freeThrowsAttempted": 35,
    "fieldGoals": 30, "threePointPercentage": 0.261, "personalFouls": 18,
    "threePointAttempted": 23, "freeThrows": 22, "teamId": 29128,
    "turnovers": 16, "acrossTeamId": 29132, "acrossTeamName": "山西汾酒",
}
_OPENER_AWAY_LOG = {
    "teamName": "山西汾酒", "rebounds": 46, "reboundsOffensive": 15,
    "scheduleTime": "2025-12-12 19:35:00", "acrossPoints": 88,
    "fieldGoalsAttempted": 73, "steals": 12, "type": 1, "points": 74,
    "result": 2, "blocked": 4, "assists": 17, "scheduleId": OPENER_ID,
    "freeThrowsPercentage": 0.5, "fieldGoalsPercentage": 0.411,
    "threePointGoals": 4, "reboundsDefensive": 31, "freeThrowsAttempted": 20,
    "fieldGoals": 30, "threePointPercentage": 0.2, "personalFouls": 28,
    "threePointAttempted": 20, "freeThrows": 10, "teamId": 29132,
    "turnovers": 17, "acrossTeamId": 29128, "acrossTeamName": "浙江浙商证券",
}
_OT_HOME_LOG = {
    "teamName": "南京天之蓝", "rebounds": 58, "reboundsOffensive": 20,
    "scheduleTime": "2026-02-03 19:35:00", "acrossPoints": 123,
    "fieldGoalsAttempted": 85, "steals": 7, "type": 0, "points": 126,
    "result": 1, "blocked": 2, "assists": 19, "scheduleId": OT_ID,
    "freeThrowsPercentage": 0.8, "fieldGoalsPercentage": 0.459,
    "threePointGoals": 16, "reboundsDefensive": 38, "freeThrowsAttempted": 40,
    "fieldGoals": 39, "threePointPercentage": 0.39, "personalFouls": 25,
    "threePointAttempted": 41, "freeThrows": 32, "teamId": 29133,
    "turnovers": 26, "acrossTeamId": 29131, "acrossTeamName": "深圳马可波罗",
}
_OT_AWAY_LOG = {
    "teamName": "深圳马可波罗", "rebounds": 40, "reboundsOffensive": 11,
    "scheduleTime": "2026-02-03 19:35:00", "acrossPoints": 126,
    "fieldGoalsAttempted": 90, "steals": 15, "type": 1, "points": 123,
    "result": 2, "blocked": 4, "assists": 27, "scheduleId": OT_ID,
    "freeThrowsPercentage": 0.593, "fieldGoalsPercentage": 0.511,
    "threePointGoals": 15, "reboundsDefensive": 29, "freeThrowsAttempted": 27,
    "fieldGoals": 46, "threePointPercentage": 0.405, "personalFouls": 26,
    "threePointAttempted": 37, "freeThrows": 16, "teamId": 29131,
    "turnovers": 16, "acrossTeamId": 29133, "acrossTeamName": "南京天之蓝",
}


def _bundle() -> dict:
    return {
        "dataset": DATASET_MARKER,
        "league": "CBA",
        "season": 2025,
        "source": "test fixture (real 2025-26 rows)",
        "fetched_at": "2026-09-27T00:00:00+00:00",
        "teams": copy.deepcopy(_TEAMS),
        "months": [],
        "days": {
            "2025-12-12": [copy.deepcopy(_OPENER_ROW)],
            "2026-02-03": [copy.deepcopy(_OT_ROW)],
        },
        "team_logs": [
            {"teamId": 29128, "teamName": "浙江浙商证券", "stage": 1,
             "total": 1, "rows": [copy.deepcopy(_OPENER_HOME_LOG)]},
            {"teamId": 29132, "teamName": "山西汾酒", "stage": 1,
             "total": 1, "rows": [copy.deepcopy(_OPENER_AWAY_LOG)]},
            {"teamId": 29133, "teamName": "南京天之蓝", "stage": 1,
             "total": 1, "rows": [copy.deepcopy(_OT_HOME_LOG)]},
            {"teamId": 29131, "teamName": "深圳马可波罗", "stage": 1,
             "total": 1, "rows": [copy.deepcopy(_OT_AWAY_LOG)]},
        ],
    }


class TestResolveTeam:
    def test_club_ids_are_prefixed(self):
        assert resolve_team(29115) == "CBA-29115"
        assert resolve_team("29115") == "CBA-29115"
        assert resolve_team(100074683) == "CBA-100074683"

    def test_implausible_ids_rejected(self):
        for bad in ("", None, "ABC", "12", "1" * 10, 12.5):
            with pytest.raises(CBADataError):
                resolve_team(bad)


class TestParseRealBundle:
    def test_two_real_games_with_correct_mapping(self):
        out = parse_cba_bundle(_bundle())
        assert out["_meta"]["n_games"] == 2
        assert out["_meta"]["n_final"] == 2
        assert out["_meta"]["regulation_minutes"] == 40
        assert out["_meta"]["n_overtime_games"] == 1
        assert out["_meta"]["possessions_estimated_for"] == 4
        games = out["games"]
        assert games[0]["game_id"] == "CBA25-0001"
        assert games[0]["league"] == "NBA" or True  # league checked below
        assert games[0]["league"] == "CBA"
        assert games[0]["season"] == "2025-26"
        assert games[0]["competition"] == "REGULAR_SEASON"
        assert games[0]["status"] == "FINAL"
        assert games[0]["home_team_id"] == "CBA-29128"
        assert games[0]["away_team_id"] == "CBA-29132"
        # Beijing time (UTC+8) preserved
        assert games[0]["game_date"] == "2025-12-12T19:35:00+08:00"
        assert games[1]["game_id"] == "CBA25-0002"

    def test_ot_team_minutes_are_five_on_floor(self):
        out = parse_cba_bundle(_bundle())
        by_game = {}
        for ts in out["team_stats"]:
            by_game.setdefault(ts["game_id"], []).append(ts)
        reg = by_game["CBA25-0001"]
        ot = by_game["CBA25-0002"]
        assert all(t["minutes"] == 200 for t in reg)   # 40 min x 5 players
        assert all(t["minutes"] == 250 for t in ot)    # 50 min x 5 players
        home = [t for t in ot if t["is_home"]]
        away = [t for t in ot if not t["is_home"]]
        assert len(home) == 1 and len(away) == 1
        assert home[0]["team_id"] == "CBA-29133"
        assert home[0]["points"] == 126 and away[0]["points"] == 123
        assert home[0]["possessions"] == round(85 + 0.44 * 40 - 20 + 26, 1)

    def test_teams_built_from_official_list(self):
        out = parse_cba_bundle(_bundle())
        assert {t["team_id"] for t in out["teams"]} == {
            "CBA-29128", "CBA-29132", "CBA-29133", "CBA-29131",
        }
        names = {t["team_id"]: t["name"] for t in out["teams"]}
        assert names["CBA-29133"] == "南京天之蓝"


class TestCompetitionMapping:
    def _with_day_row(self, row: dict) -> dict:
        bundle = _bundle()
        bundle["days"]["2026-03-07"] = [copy.deepcopy(row)]
        return bundle

    def test_allstar_row_is_dropped_and_counted(self):
        out = parse_cba_bundle(self._with_day_row(_ALLSTAR_ROW))
        assert out["_meta"]["n_dropped_allstar"] == 1
        assert out["_meta"]["n_games"] == 2

    def test_preseason_row_is_dropped_and_counted(self):
        row = dict(_ALLSTAR_ROW, matchTypeId=3, scheduleId=100108576,
                   homeTeamId=29128, visitingTeamId=29132,
                   homeTeamName="浙江浙商证券", visitingTeamName="山西汾酒",
                   status=4, quarter=4, minutes=40)
        out = parse_cba_bundle(self._with_day_row(row))
        assert out["_meta"]["n_dropped_preseason"] == 1
        assert out["_meta"]["n_games"] == 2

    def test_relegation_maps_to_cup(self):
        bundle = _bundle()
        row = dict(_OPENER_ROW, matchTypeId=9, scheduleId=100108577,
                   scheduleTime="2026-03-20 19:35:00")
        bundle["days"]["2026-03-20"] = [row]
        bundle["team_logs"] += [
            {"teamId": 29128, "teamName": "浙江浙商证券", "stage": 2,
             "total": 1, "rows": [dict(_OPENER_HOME_LOG,
                                       scheduleId=100108577)]},
            {"teamId": 29132, "teamName": "山西汾酒", "stage": 2,
             "total": 1, "rows": [dict(_OPENER_AWAY_LOG,
                                       scheduleId=100108577)]},
        ]
        out = parse_cba_bundle(bundle)
        assert out["_meta"]["n_cup"] == 1
        assert out["games"][-1]["competition"] == "CUP"

    def test_unknown_match_type_is_a_hard_error(self):
        row = dict(_OPENER_ROW, matchTypeId=7)
        bundle = self._with_day_row(row)
        with pytest.raises(CBADataError, match="unknown CBA matchTypeId"):
            parse_cba_bundle(bundle)


class TestStatusAndScores:
    # these tests mutate the OT game so the untouched regulation game keeps
    # anchoring the bundle's regulation-length inference
    def test_nonfinal_with_scores_is_a_hard_error(self):
        row = dict(_OT_ROW, status=2)
        with pytest.raises(CBADataError, match="unrecognised status"):
            parse_cba_bundle(_with_row(_bundle(), row))

    def test_nonfinal_without_scores_is_skipped(self):
        row = dict(_OT_ROW, status=1, homeTeamScore=None,
                   visitingTeamScore=None)
        out = parse_cba_bundle(_with_row(_bundle(), row))
        assert out["_meta"]["n_skipped_no_score"] == 1
        assert out["_meta"]["n_games"] == 1

    def test_schedule_score_must_reconcile_with_log_rows(self):
        row = dict(_OPENER_ROW, homeTeamScore=89)
        bundle = _with_row(_bundle(), row)
        with pytest.raises(CBADataError, match="!= home match-log row"):
            parse_cba_bundle(bundle)

    def test_missing_log_row_is_a_hard_error(self):
        bundle = _bundle()
        bundle["team_logs"] = [g for g in bundle["team_logs"]
                               if g["teamId"] != 29132]
        with pytest.raises(CBADataError, match="no official match-log row"):
            parse_cba_bundle(bundle)

    def test_unknown_club_is_a_hard_error(self):
        row = dict(_OT_ROW, homeTeamId=29199, homeTeamName="不存在队")
        with pytest.raises(CBADataError, match="missing from the official"):
            parse_cba_bundle(_with_row(_bundle(), row))


class TestGameLengthRules:
    def test_minutes_quarter_inconsistency_fails(self):
        row = dict(_OT_ROW, minutes=48)  # 2 OT must be 40 + 2*5
        with pytest.raises(CBADataError, match="game minutes"):
            parse_cba_bundle(_with_row(_bundle(), row))

    def test_overtime_marked_on_regulation_length_fails(self):
        row = dict(_OPENER_ROW, minutes=45)  # quarter=4 must be exactly 40
        with pytest.raises(CBADataError, match="game minutes"):
            parse_cba_bundle(_with_row(_bundle(), row))

    def test_quarter_below_four_fails(self):
        row = dict(_OT_ROW, quarter=3)
        with pytest.raises(CBADataError, match="impossible quarter"):
            parse_cba_bundle(_with_row(_bundle(), row))

    def test_ambiguous_regulation_length_fails(self):
        bundle = _bundle()
        bundle["days"]["2026-02-04"] = [dict(_OPENER_ROW, minutes=48,
                                             scheduleId=100105800)]
        bundle["team_logs"].append(
            {"teamId": 29128, "teamName": "浙江浙商证券", "stage": 2,
             "total": 1, "rows": [dict(_OPENER_HOME_LOG,
                                       scheduleId=100105800)]})
        bundle["team_logs"].append(
            {"teamId": 29132, "teamName": "山西汾酒", "stage": 2,
             "total": 1, "rows": [dict(_OPENER_AWAY_LOG,
                                       scheduleId=100105800)]})
        with pytest.raises(CBADataError, match="ambiguous regulation length"):
            parse_cba_bundle(bundle)


class TestSourceIdentities:
    def test_points_identity_violation_fails(self):
        # keep points (score cross-check passes) but break the scoring
        # identity: 2*(29-6)+3*6+22 = 86 != 88
        bad = dict(_OPENER_HOME_LOG, fieldGoals=29)
        bundle = _bundle()
        bundle["team_logs"][0]["rows"][0] = bad
        with pytest.raises(CBADataError, match="points identity broken"):
            parse_cba_bundle(bundle)

    def test_rebounds_identity_violation_fails(self):
        bad = dict(_OPENER_AWAY_LOG, reboundsOffensive=16)  # 16+31 != 46
        bundle = _bundle()
        bundle["team_logs"][1]["rows"][0] = bad
        with pytest.raises(CBADataError, match="rebounds identity broken"):
            parse_cba_bundle(bundle)

    def test_duplicate_log_row_fails(self):
        bundle = _bundle()
        bundle["team_logs"].append(
            {"teamId": 29128, "teamName": "浙江浙商证券", "stage": 2,
             "total": 1, "rows": [copy.deepcopy(_OPENER_HOME_LOG)]})
        with pytest.raises(CBADataError, match="duplicate log row"):
            parse_cba_bundle(bundle)

    def test_wrong_marker_fails(self):
        bundle = _bundle()
        bundle["dataset"] = "something-else"
        with pytest.raises(CBADataError, match="not a CBA bundle"):
            parse_cba_bundle(bundle)


class TestFileLoading:
    def test_roundtrip_from_file(self, tmp_path):
        path = tmp_path / "cba.json"
        path.write_text(json.dumps(_bundle(), ensure_ascii=False),
                        encoding="utf-8")
        out = load_cba_bundle_file(path)
        assert out["_meta"]["n_games"] == 2

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_cba_bundle_file(tmp_path / "nope.json")


class TestCBAIngestGate:
    def test_bundle_passes_validation_and_ingests(self, tmp_path):
        from basketball_agent.agents.validation_agent import DataValidationAgent
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.loader import DataIngestor
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.validators.parsing import parse_dataset

        raw = parse_cba_bundle(_bundle())
        dataset, parse_errors = parse_dataset(raw)
        assert not parse_errors
        report = DataValidationAgent(
            now=datetime(2026, 7, 1, tzinfo=UTC)
        ).validate(dataset)
        assert not report.has_errors, report.errors[:3]

        db = Database(tmp_path / "cba.db")
        db.initialize()
        DataIngestor(db, now=datetime(2026, 7, 1, tzinfo=UTC)).ingest(raw)
        repo = Repository(db)
        game = repo.get_game("CBA25-0002")
        assert game.league is League.CBA
        assert game.competition is Competition.REGULAR_SEASON
        assert game.home_score == 126 and game.away_score == 123


def _with_row(bundle: dict, row: dict) -> dict:
    """Replace one club's day row (matched by scheduleId) with ``row``."""
    bundle = copy.deepcopy(bundle)
    for day, rows in bundle["days"].items():
        for i, r in enumerate(rows):
            if r.get("scheduleId") == row["scheduleId"]:
                rows[i] = copy.deepcopy(row)
                return bundle
    raise AssertionError(f"scheduleId {row.get('scheduleId')} not in fixture")
