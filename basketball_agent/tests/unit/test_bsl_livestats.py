"""BSL (Tuerkiye) adapter tests (Phase 8-F).

The golden fixture is REAL 2025-26 season data:

* opener: Anadolu Efes 90 - 76 Safiport Erokspor, 2025-09-26, 1. Hafta
  (regulation; the feed's real "199:60" minutes quirk = exactly 200:00);
* overtime: Bursaspor 91 - 87 Galatasaray, 2025-12-06, 10. Hafta (1 OT,
  "224:60" team minutes = 225);
* the season's ONE verified LiveStats source defect (match 303916: feed
  frozen mid-Q4, never finalised upstream) -- excluded and counted;
* the 2026-06-19 Playoff Final: Besiktas 75 - 77 Fenerbahce.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

UTC = timezone.utc

import pytest

from basketball_agent.data.collectors.bsl_livestats import (
    DATASET_MARKER,
    BSLDataError,
    load_bsl_bundle_file,
    parse_bsl_bundle,
    resolve_team,
)
from basketball_agent.data.schemas.base import Competition, GameStatus, League

M_OPENER = {"match": {"matchId": 303912, "matchDate": "2025-09-26T19:00:00", "matchTime": "19:00", "week": "1. Hafta", "matchStatusId": 1, "hasUzatma": False, "genuisId": "2736922", "homeTeam": {"id": 246936, "name": "ANADOLU EFES", "score": "90"}, "awayTeam": {"id": 246964, "name": "SAFİPORT EROKSPOR", "score": "76"}}, "ls": {"inOT": 0, "period": 4, "periodType": "REGULAR", "periodLength": 10, "periodsMax": 4, "clock": "00:00"}, "teams": {"1": {"code": "EFS", "name": "ANADOLU EFES", "shortName": "ANADOLU EFES", "score": 90, "full_score": 90, "tot_sMinutes": "199:60", "tot_sFieldGoalsMade": 32, "tot_sFieldGoalsAttempted": 59, "tot_sTwoPointersMade": 25, "tot_sTwoPointersAttempted": 33, "tot_sThreePointersMade": 7, "tot_sThreePointersAttempted": 26, "tot_sFreeThrowsMade": 19, "tot_sFreeThrowsAttempted": 25, "tot_sReboundsOffensive": 10, "tot_sReboundsDefensive": 32, "tot_sReboundsTotal": 42, "tot_sAssists": 27, "tot_sTurnovers": 15, "tot_sSteals": 3, "tot_sBlocks": 3, "tot_sFoulsPersonal": 20, "p1_score": 24, "p2_score": 24, "p3_score": 18, "p4_score": 24}, "2": {"code": "ERK", "name": "ESENLER EROKSPOR", "shortName": "ESENLER EROKSPOR", "score": 76, "full_score": 76, "tot_sMinutes": "199:60", "tot_sFieldGoalsMade": 26, "tot_sFieldGoalsAttempted": 69, "tot_sTwoPointersMade": 19, "tot_sTwoPointersAttempted": 38, "tot_sThreePointersMade": 7, "tot_sThreePointersAttempted": 31, "tot_sFreeThrowsMade": 17, "tot_sFreeThrowsAttempted": 22, "tot_sReboundsOffensive": 14, "tot_sReboundsDefensive": 20, "tot_sReboundsTotal": 34, "tot_sAssists": 14, "tot_sTurnovers": 11, "tot_sSteals": 11, "tot_sBlocks": 1, "tot_sFoulsPersonal": 20, "p1_score": 15, "p2_score": 13, "p3_score": 21, "p4_score": 27}}}

M_OT = {"match": {"matchId": 303983, "matchDate": "2025-12-06T20:30:00", "matchTime": "20:30", "week": "10. Hafta", "matchStatusId": 1, "hasUzatma": True, "genuisId": "2715767", "homeTeam": {"id": 246962, "name": "BURSASPOR BASKETBOL", "score": "91"}, "awayTeam": {"id": 246966, "name": "GALATASARAY MCT TECHNIC", "score": "87"}}, "ls": {"inOT": 1, "period": 1, "periodType": "OVERTIME", "periodLength": 10, "periodsMax": 4, "clock": "00:00"}, "teams": {"1": {"code": "BUR", "name": "BURSASPOR BASKETBOL", "shortName": "BURSASPOR BASKETBOL", "score": 91, "full_score": 91, "tot_sMinutes": "224:60", "tot_sFieldGoalsMade": 30, "tot_sFieldGoalsAttempted": 61, "tot_sTwoPointersMade": 16, "tot_sTwoPointersAttempted": 28, "tot_sThreePointersMade": 14, "tot_sThreePointersAttempted": 33, "tot_sFreeThrowsMade": 17, "tot_sFreeThrowsAttempted": 20, "tot_sReboundsOffensive": 6, "tot_sReboundsDefensive": 31, "tot_sReboundsTotal": 37, "tot_sAssists": 25, "tot_sTurnovers": 20, "tot_sSteals": 6, "tot_sBlocks": 1, "tot_sFoulsPersonal": 23, "p1_score": 29, "p2_score": 23, "p3_score": 14, "p4_score": 11}, "2": {"code": "GSM", "name": "GALATASARAY MCT TECHNIC", "shortName": "GALATASARAY MCT TECHNIC", "score": 87, "full_score": 87, "tot_sMinutes": "224:60", "tot_sFieldGoalsMade": 28, "tot_sFieldGoalsAttempted": 70, "tot_sTwoPointersMade": 18, "tot_sTwoPointersAttempted": 39, "tot_sThreePointersMade": 10, "tot_sThreePointersAttempted": 31, "tot_sFreeThrowsMade": 21, "tot_sFreeThrowsAttempted": 24, "tot_sReboundsOffensive": 13, "tot_sReboundsDefensive": 26, "tot_sReboundsTotal": 39, "tot_sAssists": 17, "tot_sTurnovers": 13, "tot_sSteals": 9, "tot_sBlocks": 3, "tot_sFoulsPersonal": 23, "p1_score": 23, "p2_score": 26, "p3_score": 13, "p4_score": 15}}}

M_DEFECT = {"match": {"matchId": 303916, "matchDate": "2025-10-04T18:00:00", "matchTime": "18:00", "week": "2. Hafta", "matchStatusId": 1, "hasUzatma": False, "genuisId": "2715211", "homeTeam": {"id": 246974, "name": "TÜRK TELEKOM", "score": "98"}, "awayTeam": {"id": 246960, "name": "BAHÇEŞEHİR KOLEJİ", "score": "102"}}, "ls": {"inOT": 0, "period": 4, "periodType": "REGULAR", "periodLength": 10, "periodsMax": 4, "clock": "09:37"}, "teams": {"1": {"code": "TTK", "name": "TÜRK TELEKOM", "shortName": "TÜRK TELEKOM ", "score": 75, "full_score": 75, "tot_sMinutes": "151:55", "tot_sFieldGoalsMade": 28, "tot_sFieldGoalsAttempted": 49, "tot_sTwoPointersMade": 18, "tot_sTwoPointersAttempted": 29, "tot_sThreePointersMade": 10, "tot_sThreePointersAttempted": 20, "tot_sFreeThrowsMade": 9, "tot_sFreeThrowsAttempted": 13, "tot_sReboundsOffensive": 7, "tot_sReboundsDefensive": 18, "tot_sReboundsTotal": 25, "tot_sAssists": 22, "tot_sTurnovers": 11, "tot_sSteals": 4, "tot_sBlocks": 3, "tot_sFoulsPersonal": 16, "p1_score": 25, "p2_score": 28, "p3_score": 22, "p4_score": 0}, "2": {"code": "BSK", "name": "BAHÇESEHIR KOLEJI", "shortName": "BAHÇESEHIR KOLEJI", "score": 74, "full_score": 74, "tot_sMinutes": "151:55", "tot_sFieldGoalsMade": 25, "tot_sFieldGoalsAttempted": 56, "tot_sTwoPointersMade": 17, "tot_sTwoPointersAttempted": 33, "tot_sThreePointersMade": 8, "tot_sThreePointersAttempted": 23, "tot_sFreeThrowsMade": 16, "tot_sFreeThrowsAttempted": 19, "tot_sReboundsOffensive": 12, "tot_sReboundsDefensive": 17, "tot_sReboundsTotal": 29, "tot_sAssists": 14, "tot_sTurnovers": 7, "tot_sSteals": 8, "tot_sBlocks": 1, "tot_sFoulsPersonal": 12, "p1_score": 18, "p2_score": 31, "p3_score": 22, "p4_score": 3}}}

M_FINAL = {"match": {"matchId": 345256, "matchDate": "2026-06-19T20:00:00", "matchTime": "20:00", "week": "Playoff Final", "matchStatusId": 1, "hasUzatma": False, "genuisId": "2860199", "homeTeam": {"id": 246961, "name": "BEŞİKTAŞ GAİN", "score": "75"}, "awayTeam": {"id": 246965, "name": "FENERBAHÇE BEKO", "score": "77"}}, "ls": {"inOT": 0, "period": 4, "periodType": "REGULAR", "periodLength": 10, "periodsMax": 4, "clock": "00:00"}, "teams": {"1": {"code": "BJK", "name": "BEŞİKTAŞ GAİN", "shortName": "BEŞİKTAŞ GAİN", "score": 75, "full_score": 75, "tot_sMinutes": "199:60", "tot_sFieldGoalsMade": 25, "tot_sFieldGoalsAttempted": 59, "tot_sTwoPointersMade": 12, "tot_sTwoPointersAttempted": 28, "tot_sThreePointersMade": 13, "tot_sThreePointersAttempted": 31, "tot_sFreeThrowsMade": 12, "tot_sFreeThrowsAttempted": 14, "tot_sReboundsOffensive": 6, "tot_sReboundsDefensive": 25, "tot_sReboundsTotal": 31, "tot_sAssists": 15, "tot_sTurnovers": 7, "tot_sSteals": 2, "tot_sBlocks": 3, "tot_sFoulsPersonal": 19, "p1_score": 20, "p2_score": 25, "p3_score": 15, "p4_score": 15}, "2": {"code": "FBB", "name": "FENERBAHÇE BEKO", "shortName": "FENERBAHÇE BEKO", "score": 77, "full_score": 77, "tot_sMinutes": "199:60", "tot_sFieldGoalsMade": 26, "tot_sFieldGoalsAttempted": 61, "tot_sTwoPointersMade": 19, "tot_sTwoPointersAttempted": 36, "tot_sThreePointersMade": 7, "tot_sThreePointersAttempted": 25, "tot_sFreeThrowsMade": 18, "tot_sFreeThrowsAttempted": 26, "tot_sReboundsOffensive": 13, "tot_sReboundsDefensive": 28, "tot_sReboundsTotal": 41, "tot_sAssists": 16, "tot_sTurnovers": 6, "tot_sSteals": 3, "tot_sBlocks": 1, "tot_sFoulsPersonal": 17, "p1_score": 16, "p2_score": 24, "p3_score": 16, "p4_score": 21}}}


def _bundle() -> dict:
    return {
        "dataset": DATASET_MARKER,
        "league": "BSL",
        "season": "2025-2026",
        "activity_id": 20728,
        "season_id": 172,
        "source": "test fixture (real 2025-26 rows)",
        "fetched_at": "2026-09-27T00:00:00+00:00",
        "schedule": [copy.deepcopy(g["match"]) for g in
                     (M_OPENER, M_DEFECT, M_OT, M_FINAL)],
        "livestats": {g["match"]["genuisId"]:
                      copy.deepcopy(dict(g["ls"], teams=copy.deepcopy(g["teams"])))
                      for g in (M_OPENER, M_DEFECT, M_OT, M_FINAL)},
    }


class TestResolveTeam:
    def test_tbf_ids_are_prefixed(self):
        assert resolve_team(246936) == "BSL-246936"
        assert resolve_team("246936") == "BSL-246936"

    def test_implausible_ids_rejected(self):
        for bad in ("", None, "EFES", "12", "1" * 10, 12.5):
            with pytest.raises(BSLDataError):
                resolve_team(bad)


class TestParseRealBundle:
    def test_four_real_games_with_correct_mapping(self):
        out = parse_bsl_bundle(_bundle())
        m = out["_meta"]
        assert m["n_games"] == 3  # the defect match is excluded
        assert m["n_regular"] == 2
        assert m["n_playoff"] == 1
        assert m["n_overtime_games"] == 1
        assert m["n_excluded_source_defects"] == 1
        assert m["excluded_source_defects"][0]["matchId"] == 303916
        games = out["games"]
        assert games[0]["game_id"] == "BSL25-0001"
        assert games[0]["league"] == "BSL"
        assert games[0]["season"] == "2025-26"
        assert games[0]["competition"] == "REGULAR_SEASON"
        assert games[0]["home_team_id"] == "BSL-246936"
        assert games[0]["away_team_id"] == "BSL-246964"
        # Tuerkiye time (UTC+3) preserved
        assert games[0]["game_date"] == "2025-09-26T19:00:00+03:00"

    def test_minutes_quirk_and_ot(self):
        out = parse_bsl_bundle(_bundle())
        by_game = {}
        for ts in out["team_stats"]:
            by_game.setdefault(ts["game_id"], []).append(ts)
        reg = by_game["BSL25-0001"]
        ot = by_game["BSL25-0002"]
        assert all(t["minutes"] == 200 for t in reg)  # 199:60 == 200:00
        assert all(t["minutes"] == 225 for t in ot)   # 224:60 == 225:00
        home = [t for t in ot if t["is_home"]]
        assert home[0]["team_id"] == "BSL-246962"
        assert home[0]["points"] == 91
        assert home[0]["possessions"] == round(61 + 0.44 * 20 - 6 + 20, 1)

    def test_identity_checks_hold_on_real_rows(self):
        out = parse_bsl_bundle(_bundle())
        for ts in out["team_stats"]:
            assert ts["points"] == 2 * (ts["fg"] - ts["fg3"]) + 3 * ts["fg3"] + ts["ft"]
            assert ts["trb"] == ts["orb"] + ts["drb"]

    def test_teams_built_from_tbf_ids(self):
        out = parse_bsl_bundle(_bundle())
        ids = {t["team_id"] for t in out["teams"]}
        assert "BSL-246936" in ids
        names = {t["team_id"]: t["name"] for t in out["teams"]}
        assert names["BSL-246936"] == "ANADOLU EFES"


class TestCompetitionAndStatus:
    def test_playoff_final_maps_to_playoff(self):
        out = parse_bsl_bundle(_bundle())
        finals = [g for g in out["games"] if g["competition"] == "PLAYOFF"]
        assert len(finals) == 1
        assert finals[0]["home_team_id"] == "BSL-246961"
        assert finals[0]["away_score"] == 77

    def test_unknown_week_label_is_a_hard_error(self):
        bundle = _bundle()
        bundle["schedule"][0]["week"] = "Playoff Son 16"
        with pytest.raises(BSLDataError, match="unknown BSL week label"):
            parse_bsl_bundle(bundle)

    def test_unplayed_row_without_scores_is_skipped(self):
        bundle = _bundle()
        row = copy.deepcopy(bundle["schedule"][0])
        row["matchId"] = 3999999
        row["homeTeam"]["score"] = None
        row["awayTeam"]["score"] = None
        row["matchStatusId"] = 2
        bundle["schedule"].append(row)
        out = parse_bsl_bundle(bundle)
        assert out["_meta"]["n_skipped_no_score"] == 1
        assert out["_meta"]["n_games"] == 3

    def test_nonstandard_status_with_scores_is_a_hard_error(self):
        bundle = _bundle()
        bundle["schedule"][0]["matchStatusId"] = 3
        with pytest.raises(BSLDataError, match="unrecognised matchStatusId"):
            parse_bsl_bundle(bundle)

    def test_missing_livestats_payload_is_a_hard_error(self):
        bundle = _bundle()
        del bundle["livestats"]["2736922"]
        with pytest.raises(BSLDataError, match="no fetched"):
            parse_bsl_bundle(bundle)


class TestTwoSourceAndDefects:
    def test_score_mismatch_fails(self):
        bundle = _bundle()
        bundle["schedule"][0]["homeTeam"]["score"] = "91"
        with pytest.raises(BSLDataError, match="schedule score"):
            parse_bsl_bundle(bundle)

    def test_known_defect_is_excluded_and_counted(self):
        out = parse_bsl_bundle(_bundle())
        assert len(out["games"]) == 3  # 4 schedule rows, 1 defect excluded
        assert out["_meta"]["excluded_source_defects"][0]["matchId"] == 303916

    def test_defect_entry_with_complete_data_is_ingested(self):
        # If upstream ever finalises the feed, the game must ingest.
        bundle = _bundle()
        t1 = bundle["livestats"]["2715211"]["teams"]["1"]
        t2 = bundle["livestats"]["2715211"]["teams"]["2"]
        # a plausible completed feed (identity-consistent with the TBF final)
        t1.update({"score": 98, "full_score": 98, "tot_sMinutes": "199:60",
                   "tot_sFieldGoalsMade": 33, "tot_sFieldGoalsAttempted": 62,
                   "tot_sTwoPointersMade": 22, "tot_sTwoPointersAttempted": 36,
                   "tot_sThreePointersMade": 11, "tot_sThreePointersAttempted": 26,
                   "tot_sFreeThrowsMade": 21, "tot_sFreeThrowsAttempted": 27,
                   "tot_sReboundsOffensive": 12, "tot_sReboundsDefensive": 26,
                   "tot_sReboundsTotal": 38})
        t2.update({"score": 102, "full_score": 102, "tot_sMinutes": "199:60",
                   "tot_sFieldGoalsMade": 36, "tot_sFieldGoalsAttempted": 70,
                   "tot_sTwoPointersMade": 24, "tot_sTwoPointersAttempted": 42,
                   "tot_sThreePointersMade": 12, "tot_sThreePointersAttempted": 28,
                   "tot_sFreeThrowsMade": 18, "tot_sFreeThrowsAttempted": 22,
                   "tot_sReboundsOffensive": 15, "tot_sReboundsDefensive": 24,
                   "tot_sReboundsTotal": 39})
        out = parse_bsl_bundle(bundle)
        assert out["_meta"]["n_games"] == 4
        assert out["_meta"]["n_excluded_source_defects"] == 0


class TestGameLengthRules:
    def test_wild_minutes_fail(self):
        bundle = _bundle()
        bundle["livestats"]["2736922"]["teams"]["1"]["tot_sMinutes"] = "240:00"
        with pytest.raises(BSLDataError, match="team minutes"):
            parse_bsl_bundle(bundle)

    def test_uzatma_must_agree_with_minutes(self):
        bundle = _bundle()
        row = next(r for r in bundle["schedule"] if r["matchId"] == 303983)
        row["hasUzatma"] = False  # OT game flagged no-OT
        with pytest.raises(BSLDataError, match="hasUzatma"):
            parse_bsl_bundle(bundle)

    def test_shorter_than_regulation_fails(self):
        bundle = _bundle()
        for g in (bundle["livestats"]["2736922"]["teams"]["1"],
                  bundle["livestats"]["2736922"]["teams"]["2"]):
            g["tot_sMinutes"] = "180:00"
        # the minute drop trips the defect gate only for LISTED matches;
        # this game is not listed -> hard error
        with pytest.raises(BSLDataError):
            parse_bsl_bundle(bundle)


class TestSourceIdentities:
    def test_points_identity_violation_fails(self):
        bundle = _bundle()
        # break the scoring identity while keeping the score cross-check
        t = bundle["livestats"]["2736922"]["teams"]["1"]
        t["tot_sFieldGoalsMade"] = 31  # 2*(31-7)+3*7+19 = 88 != 90
        with pytest.raises(BSLDataError, match="points identity broken"):
            parse_bsl_bundle(bundle)

    def test_rebounds_identity_violation_fails(self):
        bundle = _bundle()
        t = bundle["livestats"]["2736922"]["teams"]["2"]
        t["tot_sReboundsOffensive"] = 15  # 15+20 != 34
        with pytest.raises(BSLDataError, match="rebounds identity broken"):
            parse_bsl_bundle(bundle)

    def test_wrong_marker_fails(self):
        bundle = _bundle()
        bundle["dataset"] = "something-else"
        with pytest.raises(BSLDataError, match="not a BSL bundle"):
            parse_bsl_bundle(bundle)


class TestFileLoading:
    def test_roundtrip_from_file(self, tmp_path):
        path = tmp_path / "bsl.json"
        path.write_text(json.dumps(_bundle(), ensure_ascii=False),
                        encoding="utf-8")
        out = load_bsl_bundle_file(path)
        assert out["_meta"]["n_games"] == 3

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_bsl_bundle_file(tmp_path / "nope.json")


class TestBSLIngestGate:
    def test_bundle_passes_validation_and_ingests(self, tmp_path):
        from basketball_agent.agents.validation_agent import DataValidationAgent
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.loader import DataIngestor
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.validators.parsing import parse_dataset

        raw = parse_bsl_bundle(_bundle())
        dataset, parse_errors = parse_dataset(raw)
        assert not parse_errors
        report = DataValidationAgent(
            now=datetime(2026, 7, 1, tzinfo=UTC)
        ).validate(dataset)
        assert not report.has_errors, report.errors[:3]

        db = Database(tmp_path / "bsl.db")
        db.initialize()
        DataIngestor(db, now=datetime(2026, 7, 1, tzinfo=UTC)).ingest(raw)
        repo = Repository(db)
        game = repo.get_game("BSL25-0002")
        assert game.league is League.BSL
        assert game.competition is Competition.REGULAR_SEASON
        assert game.home_score == 91 and game.away_score == 87


class TestClubIdContinuity:
    """Phase 8-H: TBF ids are per-season; 2024-25 clubs map to the
    canonical 2025-26 ids so cross-season history links."""

    def test_2024_25_ids_are_remapped(self):
        assert resolve_team(164374, "2024-25") == "BSL-246936"  # Anadolu Efes
        assert resolve_team(164392, "2024-25") == "BSL-246974"  # Türk Telekom

    def test_single_season_clubs_keep_their_ids(self):
        assert resolve_team(164379, "2024-25") == "BSL-164379"  # Darüşşafaka (relegated)
        assert resolve_team(164393, "2024-25") == "BSL-164393"  # SEMT77 Yalovaspor (relegated)

    def test_2025_26_and_seasonless_calls_unaffected(self):
        assert resolve_team(246936, "2025-26") == "BSL-246936"
        assert resolve_team(246936) == "BSL-246936"
        assert resolve_team(164374) == "BSL-164374"

    def test_2024_25_bundle_rows_join_across_seasons(self):
        bundle = _bundle()
        row = copy.deepcopy(bundle["schedule"][0])
        row["matchId"] = 235591
        row["season"] = "2024-2025"
        row["genuisId"] = ""
        row["week"] = "5. Hafta"
        row["matchDate"] = "2024-11-10T17:00:00"
        row["homeTeam"] = {"id": 164374, "name": "ANADOLU EFES", "score": "90"}
        row["awayTeam"] = {"id": 164377, "name": "BURSASPOR YÖRSAN", "score": "76"}
        box = {
            "box_source": "tbf-native",
            "teams": {
                "1": {"code": "", "name": "ANADOLU EFES", "score": 90,
                      "tot_sMinutes": "200:00", "tot_sFieldGoalsMade": 32,
                      "tot_sFieldGoalsAttempted": 59, "tot_sThreePointersMade": 7,
                      "tot_sThreePointersAttempted": 26, "tot_sFreeThrowsMade": 19,
                      "tot_sFreeThrowsAttempted": 25, "tot_sReboundsOffensive": 10,
                      "tot_sReboundsDefensive": 32, "tot_sReboundsTotal": 42,
                      "tot_sAssists": 27, "tot_sTurnovers": 15, "tot_sSteals": 3,
                      "tot_sBlocks": 3, "tot_sFoulsPersonal": 20},
                "2": {"code": "", "name": "BURSASPOR", "score": 76,
                      "tot_sMinutes": "200:00", "tot_sFieldGoalsMade": 26,
                      "tot_sFieldGoalsAttempted": 69, "tot_sThreePointersMade": 7,
                      "tot_sThreePointersAttempted": 31, "tot_sFreeThrowsMade": 17,
                      "tot_sFreeThrowsAttempted": 22, "tot_sReboundsOffensive": 14,
                      "tot_sReboundsDefensive": 20, "tot_sReboundsTotal": 34,
                      "tot_sAssists": 14, "tot_sTurnovers": 11, "tot_sSteals": 11,
                      "tot_sBlocks": 1, "tot_sFoulsPersonal": 20},
            },
        }
        bundle["tbf_boxes"] = {"235591": box}
        bundle["schedule"].append(row)
        out = parse_bsl_bundle(bundle)
        game = next(g for g in out["games"]
                    if g["game_date"].startswith("2024-11-10"))
        assert game["away_team_id"] == "BSL-246962"   # joined to 2025-26 Bursaspor
        assert out["_meta"]["n_box_tbf_native"] == 1
