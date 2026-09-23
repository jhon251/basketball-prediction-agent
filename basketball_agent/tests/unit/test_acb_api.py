"""ACB official-API adapter tests (Phase 8-B6).

The bundle is built to be **internally consistent with the real 2025-26
payload** for match 104459 (Unicaja 86-68 Surne Bilbao, 2025-10-04T16:00:00Z):
the team totals, the quarter scores and the player/team row split are the
published ones, while the player rows themselves are a compact synthetic split
of those same totals (the real payload has 12 players per side and lives
untrimmed in ``storage/raw/acb_api`` — the real-data golden assertions run in
``tests/integration/test_acb_b6.py``).
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from basketball_agent.data.collectors.acb_api import (
    CLUB_ID_TO_TEAM,
    DATASET_MARKER,
    TEAM_TIMEZONES,
    ACBDataError,
    parse_acb_bundle,
    resolve_team,
)

# real published team totals for ACB 2025-26 match 104459 (period 0)
REAL_TOTAL_UNI = {
    "points": 86, "freeThrowsMade": 15, "freeThrowsAttempted": 22,
    "twoPointersMade": 25, "twoPointersAttempted": 43,
    "threePointersMade": 7, "threePointersAttempted": 18,
    "assists": 26, "offRebounds": 11, "defRebounds": 26, "totalRebounds": 37,
    "steals": 7, "turnovers": 17, "blocks": 0, "receivedBlocks": 3, "dunks": 4,
    "personalFouls": 25, "foulsDrawn": 22, "plusMinus": 90, "rating": 97,
}
#: the non-player (team) row of the same game
REAL_TEAM_ROW_UNI = {
    "points": 0, "freeThrowsMade": 0, "freeThrowsAttempted": 0,
    "twoPointersMade": 0, "twoPointersAttempted": 0, "threePointersMade": 0,
    "threePointersAttempted": 0, "assists": 0, "offRebounds": 1, "defRebounds": 1,
    "totalRebounds": 2, "steals": 0, "turnovers": 1, "blocks": 0,
    "receivedBlocks": 0, "dunks": 0, "personalFouls": 0, "foulsDrawn": 0,
    "plusMinus": 0, "rating": 1,
}
REAL_TOTAL_SBB = {
    "points": 68, "freeThrowsMade": 18, "freeThrowsAttempted": 28,
    "twoPointersMade": 16, "twoPointersAttempted": 33,
    "threePointersMade": 6, "threePointersAttempted": 23,
    "assists": 13, "offRebounds": 8, "defRebounds": 22, "totalRebounds": 30,
    "steals": 9, "turnovers": 15, "blocks": 3, "receivedBlocks": 0, "dunks": 4,
    "personalFouls": 22, "foulsDrawn": 25, "plusMinus": -90, "rating": 63,
}
REAL_TEAM_ROW_SBB = {
    "points": 0, "freeThrowsMade": 0, "freeThrowsAttempted": 0,
    "twoPointersMade": 0, "twoPointersAttempted": 0, "threePointersMade": 0,
    "threePointersAttempted": 0, "assists": 0, "offRebounds": 0, "defRebounds": 2,
    "totalRebounds": 2, "steals": 1, "turnovers": 0, "blocks": 0,
    "receivedBlocks": 0, "dunks": 0, "personalFouls": 0, "foulsDrawn": 0,
    "plusMinus": 0, "rating": 1,
}

PLAYER_FIELDS = (
    "points", "freeThrowsMade", "freeThrowsAttempted", "twoPointersMade",
    "twoPointersAttempted", "threePointersMade", "threePointersAttempted",
    "assists", "offRebounds", "defRebounds", "totalRebounds", "steals",
    "turnovers", "blocks", "personalFouls",
)


def _players(total: dict, team_row: dict, n_players: int, playtime: str) -> list[dict]:
    """Split (total - team) across ``n_players`` rows; the first takes the rest."""
    remainder = {f: int(total[f]) - int(team_row[f]) for f in PLAYER_FIELDS}
    rows = []
    for index in range(n_players):
        stats = {f: (remainder[f] if index == 0 else 0) for f in PLAYER_FIELDS}
        rows.append({
            "player": {"id": 900000 + index, "nickname": f"Test Player {index + 1}"},
            "onCourt": False, "playTime": playtime, "isStarted": index < 5,
            **stats, "receivedBlocks": 0, "dunks": 0, "foulsDrawn": 0,
            "plusMinus": 0, "rating": 0,
        })
    return rows


def _team_entry(team_id: int, club_id: int, code: str, name: str,
                total: dict, team_row: dict, periods: int,
                n_players: int = 10, playtime: str = "20:00",
                trailing_empty_ot: bool = False) -> dict:
    stats_by_periods = [{
        "quarter": 0,
        "stats": {"players": _players(total, team_row, n_players, playtime),
                  "team": dict(team_row), "total": dict(total)},
    }]
    # real per-period rows carry nonzero totals and on-court seconds for this
    # team (a team CAN score 0 in a quarter but its players still clock time);
    # the all-zero shape is reserved for the trailing empty OT slot artifact
    for quarter in range(1, periods + 1):
        q_total = {**{f: 0 for f in PLAYER_FIELDS}, "points": 2}
        q_team = {f: 0 for f in PLAYER_FIELDS}
        stats_by_periods.append({"quarter": quarter, "stats": {
            "players": _players(q_total, q_team, n_players, playtime),
            "team": q_team, "total": q_total}})
    if trailing_empty_ot:
        stats_by_periods.append({"quarter": periods + 1, "stats": {
            "players": [], "team": {}, "total": {}}})
    return {
        "team": {"id": team_id, "clubId": club_id, "abbreviatedName": code,
                 "fullName": name},
        "headCoach": "Test Coach", "assistantCoaches": [],
        "statsByPeriods": stats_by_periods,
    }


def _bundle(*, periods: int = 4, playtime: str = "20:00",
            home_score: int = 86, away_score: int = 68,
            quarter_scores: list[dict] | None = None,
            status: str = "FINALIZED",
            subphase_id: int | None = None,
            trailing_empty_ot: bool = False) -> dict:
    quarter_scores = quarter_scores or [
        {"quarter": 1, "homeScore": 15, "awayScore": 18},
        {"quarter": 2, "homeScore": 31, "awayScore": 16},
        {"quarter": 3, "homeScore": 20, "awayScore": 23},
        {"quarter": 4, "homeScore": 20, "awayScore": 11},
    ]
    match = {
        "id": 104459, "homeTeamId": 4384, "awayTeamId": 4389,
        "homeScore": home_score, "awayScore": away_score,
        "startDateTime": "2025-10-04T16:00:00Z", "matchStatus": status,
        "isOnHalfTime": False, "currentQuarter": periods,
        "roundId": 5884, "roundNumber": 1,
        "subphaseId": subphase_id, "subphaseNumber": None,
    }
    return {
        "dataset": DATASET_MARKER,
        "competition_id": 1, "competition_name": "Liga Endesa",
        "edition_id": 90, "season": "2025-26", "season_code": "ACB2025",
        "source": "https://api2.acb.com",
        "fetched_at": "2026-09-21T00:00:00+00:00",
        "n_calendar_matches": 1, "n_headers": 1, "n_boxscores": 1,
        "calendar_skipped": {},
        "calendar_root": {"availableFilters": {"competitions": [], "seasons": []}},
        "calendar": {
            "selectedFilters": {"competition": 1, "season": 90},
            "teams": [
                {"id": 4384, "clubId": 14, "abbreviatedName": "UNI", "fullName": "Unicaja"},
                {"id": 4389, "clubId": 4, "abbreviatedName": "SBB",
                 "fullName": "Surne Bilbao"},
            ],
            "matches": [],
            "rounds": [{"id": 5884, "roundNumber": 1, "subphaseId": subphase_id,
                        "subphaseNumber": None, "matches": [match]}],
        },
        "headers": {"104459": {
            "matchId": 104459, "competitionId": 1, "start": "2025-10-04T16:00:00Z",
            "matchStatus": "FINALIZED", "isOnHalfTime": False, "timeLeft": "00:00",
            "currentQuarter": periods,
            "currentHomeScore": home_score, "currentAwayScore": away_score,
            "teams": {"home": {"id": 4384, "clubId": 14, "abbreviatedName": "UNI"},
                      "away": {"id": 4389, "clubId": 4, "abbreviatedName": "SBB"}},
            "quarterScores": quarter_scores,
            "broadcastChannels": [], "availableContent": {"boxscore": True},
            "highLightsVideoURL": None,
        }},
        "boxscores": {"104459": {
            "matchFinished": True,
            "teamBoxscores": [
                _team_entry(4384, 14, "UNI", "Unicaja", REAL_TOTAL_UNI,
                            REAL_TEAM_ROW_UNI, periods, playtime=playtime,
                            trailing_empty_ot=trailing_empty_ot),
                _team_entry(4389, 4, "SBB", "Surne Bilbao", REAL_TOTAL_SBB,
                            REAL_TEAM_ROW_SBB, periods, playtime=playtime,
                            trailing_empty_ot=trailing_empty_ot),
            ],
            "arena": "Martin Carpena", "attendance": 10123, "referees": [],
        }},
    }


class TestTeamResolution:
    def test_known_clubs_resolve_with_namespace(self):
        assert resolve_team(14) == "ACB-UNI"
        assert resolve_team("4") == "ACB-SBB"     # string ids come from JSON
        assert all(len(code) == 3 and code.isupper()
                   for code in CLUB_ID_TO_TEAM.values())

    def test_unknown_club_fails_loudly(self):
        with pytest.raises(ACBDataError, match="unknown ACB clubId"):
            resolve_team(999999)

    def test_acb_ids_cannot_collide_with_nba_or_euroleague(self):
        from basketball_agent.data.collectors.euroleague_live import (
            resolve_team as resolve_el,
        )
        from basketball_agent.data.collectors.nba_teams import NBA_TEAM_IDS

        for club_id in CLUB_ID_TO_TEAM:
            stored = resolve_team(club_id)
            assert stored not in NBA_TEAM_IDS
            assert stored.startswith("ACB-") and not stored.startswith("EL-")
        assert resolve_el("MAD") == "EL-MAD" != resolve_team(9)   # Real Madrid

    def test_two_canary_clubs_are_atlantic(self):
        assert TEAM_TIMEZONES["DGC"] == "Atlantic/Canary"
        assert TEAM_TIMEZONES["LLT"] == "Atlantic/Canary"
        assert TEAM_TIMEZONES["BAR"] == "Europe/Madrid"

    def test_relegation_promotion_clubs_resolve(self):
        """Clubs that only appear in some seasons (Phase 8-B7 evidence)."""
        assert resolve_team(6) == "ACB-MOV"      # Movistar Estudiantes
        assert resolve_team(7) == "ACB-GBC"      # Acunsa GBC
        assert resolve_team(15) == "ACB-BET"     # Real Betis Baloncesto
        assert resolve_team(17) == "ACB-FUE"     # Fuenlabrada
        assert resolve_team(57) == "ACB-MOB"     # Monbus Obradoiro
        assert resolve_team(624) == "ACB-ZPA"    # Zunder Palencia
        assert resolve_team(657) == "ACB-COR"    # Leyma Coruña


class TestSponsorCodeAliases:
    """ACB clubs rename with their sponsor; one club keeps ONE identity."""

    @pytest.mark.parametrize("club_id,code,canonical", [
        (3, "KBA", "BKN"), (3, "TDS", "BKN"),     # Kirolbet / TD Systems Baskonia
        (5, "HGC", "DGC"), (5, "GCA", "DGC"),     # Herbalife / Gran Canaria
        (8, "CJB", "JOV"),                        # Club Joventut Badalona
        (10, "BAX", "MAN"),                       # BAXI Manresa
        (12, "UCB", "UCM"),                       # UCAM Murcia CB
        (17, "MKF", "FUE"), (17, "URF", "FUE"),   # Montakit / Urbas Fuenlabrada
        (28, "IBT", "LLT"), (28, "LNT", "LLT"),   # Iberostar / Lenovo Tenerife
        (549, "SPB", "BUR"),                      # San Pablo Burgos
        (658, "HIO", "ILE"),                      # Hiopos Lleida
    ])
    def test_recorded_alias_parses_to_the_canonical_team(self, club_id, code, canonical):
        bundle = _bundle()
        bundle["calendar"]["teams"][0].update(
            {"clubId": club_id, "abbreviatedName": code, "fullName": "Alias Club"})
        bundle["boxscores"]["104459"]["teamBoxscores"][0]["team"].update(
            {"clubId": club_id, "abbreviatedName": code})
        raw = parse_acb_bundle(bundle)
        assert raw["games"][0]["home_team_id"] == f"ACB-{canonical}"
        assert raw["_meta"]["team_code_aliases_used"] == {code: canonical}

    def test_bilbao_alias_on_the_away_side(self):
        bundle = _bundle()
        bundle["calendar"]["teams"][1].update(
            {"clubId": 4, "abbreviatedName": "RBB", "fullName": "RETAbet Bilbao"})
        bundle["boxscores"]["104459"]["teamBoxscores"][1]["team"].update(
            {"clubId": 4, "abbreviatedName": "RBB"})
        raw = parse_acb_bundle(bundle)
        assert raw["games"][0]["away_team_id"] == "ACB-SBB"
        assert raw["_meta"]["team_code_aliases_used"] == {"RBB": "SBB"}

    def test_alias_of_another_club_is_rejected(self):
        """A recorded alias must belong to THAT club id (no fuzzy matching)."""
        from basketball_agent.data.collectors.acb_api import TEAM_CODE_ALIASES

        bundle = _bundle()
        assert "KBA" in TEAM_CODE_ALIASES[3]       # KBA belongs to clubId 3 …
        bundle["calendar"]["teams"][0]["abbreviatedName"] = "KBA"  # … not to 14
        with pytest.raises(ACBDataError, match="now abbreviates as"):
            parse_acb_bundle(bundle)

    def test_canonical_code_reports_no_alias_use(self):
        raw = parse_acb_bundle(_bundle())
        assert raw["_meta"]["team_code_aliases_used"] == {}


class TestGoldenParse:
    def test_real_match_104459(self):
        raw = parse_acb_bundle(_bundle())
        assert raw["_meta"]["n_games"] == 1
        assert raw["_meta"]["n_final"] == 1
        game = raw["games"][0]
        assert game["game_id"] == "ACB2025-104459"
        assert game["league"] == "ACB"
        assert game["season"] == "2025-26"
        assert (game["home_team_id"], game["away_team_id"]) == ("ACB-UNI", "ACB-SBB")
        assert (game["home_score"], game["away_score"]) == (86, 68)
        assert game["status"] == "FINAL"
        assert game["competition"] == "REGULAR_SEASON"
        # source publishes UTC; stored verbatim
        assert game["game_date"].replace("+00:00", "Z") == "2025-10-04T16:00:00Z"

    def test_team_stats_come_from_the_published_totals(self):
        raw = parse_acb_bundle(_bundle())
        stats = {ts["team_id"]: ts for ts in raw["team_stats"]}
        assert set(stats) == {"ACB-UNI", "ACB-SBB"}
        uni = stats["ACB-UNI"]
        assert stats["ACB-SBB"]["is_home"] is False and uni["is_home"] is True
        assert uni["minutes"] == 200                      # 4 x 10 min
        assert uni["points"] == 86
        assert uni["fg"] == 25 + 7 and uni["fga"] == 43 + 18
        assert uni["fg3"] == 7 and uni["fg3a"] == 18
        assert (uni["ft"], uni["fta"]) == (15, 22)
        assert (uni["orb"], uni["drb"], uni["trb"]) == (11, 26, 37)
        assert uni["ast"] == 26 and uni["tov"] == 17 and uni["stl"] == 7
        assert uni["pf"] == 25
        # possessions are not published -> the standard estimate, recorded in meta
        assert uni["possessions"] == round(uni["fga"] + 0.44 * uni["fta"] - uni["orb"] + uni["tov"], 1)
        assert raw["_meta"]["possessions_estimated_for"] == 2

    def test_teams_registry_is_namespaced(self):
        raw = parse_acb_bundle(_bundle())
        ids = {t["team_id"] for t in raw["teams"]}
        assert ids == {"ACB-UNI", "ACB-SBB"}
        assert all(t["league"] == "ACB" for t in raw["teams"])

    def test_playoff_subphase_maps_to_playoff(self):
        raw = parse_acb_bundle(_bundle(subphase_id=293))
        assert raw["games"][0]["competition"] == "PLAYOFF"

    def test_unknown_subphase_fails(self):
        with pytest.raises(ACBDataError, match="unknown playoff subphaseId"):
            parse_acb_bundle(_bundle(subphase_id=777))


class TestOvertime:
    def test_overtime_minutes_and_periods(self):
        # a 5-period (OT) game: team minutes are 225, quarter scores still sum
        # to the real 86-68 final score
        bundle = _bundle(
            periods=5, playtime="22:30",
            quarter_scores=[
                {"quarter": 1, "homeScore": 20, "awayScore": 22},
                {"quarter": 2, "homeScore": 25, "awayScore": 21},
                {"quarter": 3, "homeScore": 18, "awayScore": 19},
                {"quarter": 4, "homeScore": 11, "awayScore": 4},
                {"quarter": 5, "homeScore": 12, "awayScore": 2},
            ],
        )
        raw = parse_acb_bundle(bundle)
        assert raw["_meta"]["n_overtime"] == 1
        assert all(ts["minutes"] == 225 for ts in raw["team_stats"])

    def test_wrong_overtime_minutes_fail(self):
        bundle = _bundle(periods=5, playtime="20:00",  # 200 min in a 5-period game
                         quarter_scores=[
                             {"quarter": 1, "homeScore": 15, "awayScore": 18},
                             {"quarter": 2, "homeScore": 31, "awayScore": 16},
                             {"quarter": 3, "homeScore": 20, "awayScore": 23},
                             {"quarter": 4, "homeScore": 20, "awayScore": 11},
                             {"quarter": 5, "homeScore": 0, "awayScore": 0},
                         ])
        with pytest.raises(ACBDataError, match="team playtime"):
            parse_acb_bundle(bundle)

    def test_old_season_playtime_drift_is_accepted_and_counted(self):
        """2020-21 real-data find: team playtime sums drift off the
        period-derived expectation (up to ~2min observed).  Bounded drift
        passes and is counted in _meta.team_minutes_drifted; the stored
        minutes carry the nearest whole minute."""
        # 10 players x 20:11 = 201:50 (12110s) vs 12000s expected -> +110s
        raw = parse_acb_bundle(_bundle(playtime="20:11"))
        assert raw["_meta"]["team_minutes_drifted"] == 2
        assert all(ts["minutes"] == 202 for ts in raw["team_stats"])

    def test_trailing_empty_ot_slot_is_ignored(self):
        """Edition-87 real find: a 1-OT game ships an extra all-zero quarter
        row (0 points, 00:00 playTime).  The adapter must derive 5 played
        periods, not 6."""
        bundle = _bundle(
            periods=5, playtime="22:30", trailing_empty_ot=True,
            quarter_scores=[
                {"quarter": 1, "homeScore": 20, "awayScore": 22},
                {"quarter": 2, "homeScore": 25, "awayScore": 21},
                {"quarter": 3, "homeScore": 18, "awayScore": 19},
                {"quarter": 4, "homeScore": 11, "awayScore": 4},
                {"quarter": 5, "homeScore": 12, "awayScore": 2},
            ],
        )
        raw = parse_acb_bundle(bundle)
        assert raw["_meta"]["n_overtime"] == 1
        assert all(ts["minutes"] == 225 for ts in raw["team_stats"])

    def test_wild_playtime_drift_still_fails(self):
        # 200 min in a 5-period game: +15min off the 225min expectation
        bundle = _bundle(periods=5, playtime="20:00",
                         quarter_scores=[
                             {"quarter": 1, "homeScore": 15, "awayScore": 18},
                             {"quarter": 2, "homeScore": 31, "awayScore": 16},
                             {"quarter": 3, "homeScore": 20, "awayScore": 23},
                             {"quarter": 4, "homeScore": 20, "awayScore": 11},
                             {"quarter": 5, "homeScore": 0, "awayScore": 0},
                         ])
        with pytest.raises(ACBDataError, match="team playtime"):
            parse_acb_bundle(bundle)


class TestFailLoud:
    def test_wrong_dataset_marker(self):
        bundle = _bundle()
        bundle["dataset"] = "something-else"
        with pytest.raises(ACBDataError, match="not an ACB bundle"):
            parse_acb_bundle(bundle)

    def test_missing_boxscore_is_an_error(self):
        bundle = _bundle()
        bundle["boxscores"] = {}
        with pytest.raises(ACBDataError, match="boxscore missing"):
            parse_acb_bundle(bundle)

    def test_skipped_finalised_match_is_an_error(self):
        bundle = _bundle()
        bundle["calendar_skipped"] = {"104459": "boxscore endpoint returned no payload"}
        with pytest.raises(ACBDataError, match="without a fetched"):
            parse_acb_bundle(bundle)

    def test_home_away_disagreement_is_an_error(self):
        bundle = _bundle()
        bundle["headers"]["104459"]["teams"]["home"]["id"] = 4389
        bundle["headers"]["104459"]["teams"]["away"]["id"] = 4384
        with pytest.raises(ACBDataError, match="calendar home/away"):
            parse_acb_bundle(bundle)

    def test_unknown_club_in_calendar_is_an_error(self):
        bundle = _bundle()
        bundle["calendar"]["teams"][0]["clubId"] = 123456
        with pytest.raises(ACBDataError, match="unknown ACB clubId"):
            parse_acb_bundle(bundle)

    def test_club_abbreviation_drift_is_an_error(self):
        """A clubId whose official code changed must be re-evidenced by hand."""
        bundle = _bundle()
        bundle["calendar"]["teams"][0]["abbreviatedName"] = "XYZ"
        with pytest.raises(ACBDataError, match="now abbreviates as"):
            parse_acb_bundle(bundle)

    def test_score_mismatch_between_endpoints_is_an_error(self):
        bundle = _bundle()
        bundle["headers"]["104459"]["currentAwayScore"] = 70
        with pytest.raises(ACBDataError, match="calendar score"):
            parse_acb_bundle(bundle)

    def test_boxscore_totals_must_match_the_score(self):
        bundle = _bundle()
        bundle["boxscores"]["104459"]["teamBoxscores"][0]["statsByPeriods"][0][
            "stats"]["total"]["points"] = 84
        with pytest.raises(ACBDataError, match="players"):
            parse_acb_bundle(bundle)

    def test_scoring_identity_is_enforced(self):
        bundle = _bundle()
        # break 2PM/3PM/FT vs points while keeping every other identity intact
        total = bundle["boxscores"]["104459"]["teamBoxscores"][0]["statsByPeriods"][0]["stats"]["total"]
        total["twoPointersMade"] = 24
        with pytest.raises(ACBDataError, match="players|points !="):
            parse_acb_bundle(bundle)

    def test_naive_or_local_timestamp_is_rejected(self):
        bundle = _bundle()
        bundle["calendar"]["rounds"][0]["matches"][0]["startDateTime"] = "2025-10-04T18:00:00"
        with pytest.raises(ACBDataError, match="not published as UTC"):
            parse_acb_bundle(bundle)

    def test_unknown_status_is_an_error(self):
        bundle = _bundle(status="WEIRD_STATUS")
        with pytest.raises(ACBDataError, match="unknown matchStatus"):
            parse_acb_bundle(bundle)

    def test_quarter_scores_must_sum_to_the_score(self):
        bundle = _bundle()
        bundle["headers"]["104459"]["quarterScores"][3]["homeScore"] = 21
        with pytest.raises(ACBDataError, match="quarter scores"):
            parse_acb_bundle(bundle)


class TestNonFinalStatuses:
    """The 2025-26 season is 327/327 FINALIZED, so these branches are exercised
    with synthetic payloads (documented as such)."""

    @pytest.mark.parametrize("raw_status,expected", [
        ("POSTPONED", "POSTPONED"),
        ("SUSPENDED", "POSTPONED"),
        ("CANCELED", "CANCELLED"),
        ("CANCELLED", "CANCELLED"),
        ("NOT_STARTED", "SCHEDULED"),
    ])
    def test_status_mapping_without_scores(self, raw_status, expected):
        bundle = deepcopy(_bundle(status=raw_status))
        match = bundle["calendar"]["rounds"][0]["matches"][0]
        match["homeScore"] = None
        match["awayScore"] = None
        bundle["headers"] = {}
        bundle["boxscores"] = {}
        bundle["n_headers"] = bundle["n_boxscores"] = 0
        raw = parse_acb_bundle(bundle)
        game = raw["games"][0]
        assert game["status"] == expected
        assert game["home_score"] is None and game["away_score"] is None
        assert raw["team_stats"] == []
        if expected == "POSTPONED":
            assert raw["_meta"]["n_postponed"] == 1
        if expected == "CANCELLED":
            assert raw["_meta"]["n_cancelled"] == 1

    def test_finalised_without_scores_is_an_error(self):
        bundle = _bundle()
        match = bundle["calendar"]["rounds"][0]["matches"][0]
        match["homeScore"] = match["awayScore"] = None
        with pytest.raises(ACBDataError, match="FINALIZED without scores"):
            parse_acb_bundle(bundle)
