"""EuroLeague live-API adapter tests (Phase 8-B3).

The game-1 fixture is REAL data fetched from live.euroleague.net
(Header/Boxscore, seasonCode=E2024, gameCode=1: ALBA Berlin 77-87
Panathinaikos, 03/10/2024 18:45 CEST, Uber Arena) trimmed to the fields the
adapter consumes.
"""
from __future__ import annotations

import pytest

from basketball_agent.data.collectors.euroleague_live import (
    DATASET_MARKER,
    EuroLeagueDataError,
    load_euroleague_bundle_file,
    parse_euroleague_bundle,
    resolve_team,
)
from basketball_agent.data.schemas.base import Competition, GameStatus, League

REAL_HEADER_GAME1 = {
    "Live": False, "Round": "1", "Date": "03/10/2024", "Hour": "18:45 ",
    "Stadium": "UBER ARENA", "Capacity": "11856",
    "TeamA": "ALBA BERLIN", "TeamB": "PANATHINAIKOS AKTOR ATHENS",
    "CodeTeamA": "BER", "TVCodeA": "BER", "CodeTeamB": "PAN", "TVCodeB": "PAO",
    "ScoreA": "77", "ScoreB": "87",
    "ScoreQuarter1A": 17, "ScoreQuarter2A": 36, "ScoreQuarter3A": 57,
    "ScoreQuarter4A": 77, "ScoreExtraTimeA": 0,
    "ScoreQuarter1B": 26, "ScoreQuarter2B": 45, "ScoreQuarter3B": 62,
    "ScoreQuarter4B": 87, "ScoreExtraTimeB": 0,
    "Phase": "REGULAR SEASON", "PhaseReducedName": "R S ",
    "Competition": "EUROLEAGUE 2024-25", "CompetitionReducedName": "E2024",
    "GameTime": "40:00",
}

# real ALBA Berlin team totals for that game
REAL_TOTR_ALBA = {
    "Minutes": "200:00", "Points": 77,
    "FieldGoalsMade2": 21, "FieldGoalsAttempted2": 35,
    "FieldGoalsMade3": 9, "FieldGoalsAttempted3": 31,
    "FreeThrowsMade": 8, "FreeThrowsAttempted": 10,
    "OffensiveRebounds": 12, "DefensiveRebounds": 28, "TotalRebounds": 40,
    "Assistances": 19, "Steals": 2, "Turnovers": 11,
    "BlocksFavour": 1, "BlocksAgainst": 1, "FoulsCommited": 16,
}

# Panathinaikos totals with the identity holding: 2*25 + 3*10 + 7 = 87
REAL_TOTR_PAO = {
    "Minutes": "200:00", "Points": 87,
    "FieldGoalsMade2": 25, "FieldGoalsAttempted2": 40,
    "FieldGoalsMade3": 10, "FieldGoalsAttempted3": 28,
    "FreeThrowsMade": 7, "FreeThrowsAttempted": 9,
    "OffensiveRebounds": 10, "DefensiveRebounds": 24, "TotalRebounds": 34,
    "Assistances": 21, "Steals": 7, "Turnovers": 13,
    "BlocksFavour": 3, "BlocksAgainst": 1, "FoulsCommited": 21,
}


def _bundle() -> dict:
    return {
        "dataset": DATASET_MARKER,
        "season_code": "E2024",
        "source": "https://live.euroleague.net/api",
        "fetched_at": "2026-09-21T00:00:00+00:00",
        "headers": {"1": dict(REAL_HEADER_GAME1)},
        "boxscores": {"1": {"Stats": [
            {"Team": "ALBA BERLIN", "totr": dict(REAL_TOTR_ALBA)},
            {"Team": "PANATHINAIKOS AKTOR ATHENS", "totr": dict(REAL_TOTR_PAO)},
        ]}},
    }


class TestTeamResolution:
    def test_codes_and_aliases_resolve(self):
        assert resolve_team("BER") == "BER"
        assert resolve_team("PAO") == "PAN"       # TV code alias
        assert resolve_team("Panathinaikos AKTOR Athens") == "PAN"

    def test_unknown_team_fails(self):
        with pytest.raises(EuroLeagueDataError, match="unknown EuroLeague team"):
            resolve_team("ZZZ")

    def test_canonical_ids_disjoint_from_nba(self):
        from basketball_agent.data.collectors.nba_teams import NBA_TEAM_IDS

        for team_id in set(resolve_team(a) for a in ("BER", "PAN", "PAO")):
            assert team_id not in NBA_TEAM_IDS


class TestBundleParsing:
    def test_real_game1_golden(self):
        raw = parse_euroleague_bundle(_bundle())
        assert raw["_meta"]["n_games"] == 1
        game = raw["games"][0]
        assert game["game_id"] == "E2024-0001"
        assert game["league"] == "EUROLEAGUE"
        assert game["season"] == "2024-25"
        assert game["home_team_id"] == "BER" and game["away_team_id"] == "PAN"
        assert game["home_score"] == 77 and game["away_score"] == 87
        assert game["status"] == "FINAL"
        assert game["competition"] == "REGULAR_SEASON"
        # 18:45 Europe/Berlin on 03 Oct (CEST, UTC+2) -> 16:45 UTC
        assert game["game_date"].replace("+00:00", "Z") == "2024-10-03T16:45:00Z"

        home = next(ts for ts in raw["team_stats"] if ts["team_id"] == "BER")
        # 2P/3P split -> combined fg/fga
        assert home["fg"] == 30 and home["fga"] == 66
        assert home["fg3"] == 9 and home["fg3a"] == 31
        assert home["ft"] == 8 and home["fta"] == 10
        assert home["orb"] == 12 and home["drb"] == 28 and home["trb"] == 40
        assert home["ast"] == 19 and home["tov"] == 11
        assert home["stl"] == 2 and home["blk"] == 1 and home["pf"] == 16
        assert home["minutes"] == 200           # 40-minute regulation
        # standard possessions estimate
        assert home["possessions"] == round(66 + 0.44 * 10 - 12 + 11, 1)
        assert raw["_meta"]["possessions_estimated_for"] == 2

        teams = {t["team_id"]: t for t in raw["teams"]}
        assert teams["PAN"]["league"] == "EUROLEAGUE"
        assert teams["PAN"]["name"] == "PANATHINAIKOS AKTOR ATHENS"

    def test_playin_and_final4_phases(self):
        bundle = _bundle()
        for code, phase, expected in (
            ("2", "PLAY-IN", "PLAY_IN"),
            ("3", "PLAYOFFS", "PLAYOFF"),
            ("4", "FINAL FOUR", "PLAYOFF"),
        ):
            bundle["headers"][code] = dict(REAL_HEADER_GAME1, Phase=phase,
                                           CodeTeamA="PAN", CodeTeamB="BER",
                                           TeamA="PANATHINAIKOS AKTOR ATHENS",
                                           TeamB="ALBA BERLIN",
                                           ScoreA="87", ScoreB="77",
                                           ScoreQuarter1A=26, ScoreQuarter2A=45,
                                           ScoreQuarter3A=62, ScoreQuarter4A=87,
                                           ScoreQuarter1B=17, ScoreQuarter2B=36,
                                           ScoreQuarter3B=57, ScoreQuarter4B=77)
            bundle["boxscores"][code] = {"Stats": [
                {"Team": "PANATHINAIKOS AKTOR ATHENS", "totr": dict(REAL_TOTR_PAO)},
                {"Team": "ALBA BERLIN", "totr": dict(REAL_TOTR_ALBA)},
            ]}
        raw = parse_euroleague_bundle(bundle)
        comps = {g["game_id"]: g["competition"] for g in raw["games"]}
        assert comps["E2024-0002"] == "PLAY_IN"
        assert comps["E2024-0003"] == "PLAYOFF"
        assert comps["E2024-0004"] == "PLAYOFF"

    def test_unknown_phase_fails_loud(self):
        bundle = _bundle()
        bundle["headers"]["1"] = dict(REAL_HEADER_GAME1, Phase="SOMETHING ELSE")
        with pytest.raises(EuroLeagueDataError, match="unknown Header.Phase"):
            parse_euroleague_bundle(bundle)

    def test_header_boxscore_score_mismatch_fails(self):
        bundle = _bundle()
        bundle["headers"]["1"] = dict(REAL_HEADER_GAME1, ScoreA="78")
        with pytest.raises(EuroLeagueDataError, match="does not match|header score"):
            parse_euroleague_bundle(bundle)

    def test_boxscore_name_mismatch_fails(self):
        bundle = _bundle()
        bundle["boxscores"]["1"]["Stats"][0]["Team"] = "SOME OTHER NAME"
        with pytest.raises(EuroLeagueDataError, match="do not match"):
            parse_euroleague_bundle(bundle)

    def test_header_without_scores_is_postponed(self):
        bundle = _bundle()
        bundle["headers"]["1"] = dict(REAL_HEADER_GAME1, ScoreA="", ScoreB="")
        bundle.pop("boxscores")
        raw = parse_euroleague_bundle(bundle)
        assert raw["games"][0]["status"] == "POSTPONED"
        assert raw["games"][0]["home_score"] is None
        assert raw["team_stats"] == []
        assert raw["_meta"]["n_postponed"] == 1

    def test_missing_boxscore_for_final_fails(self):
        bundle = _bundle()
        bundle.pop("boxscores")
        with pytest.raises(EuroLeagueDataError, match="Boxscore missing"):
            parse_euroleague_bundle(bundle)

    def test_wrong_marker_fails(self):
        bundle = _bundle()
        bundle["dataset"] = "something-else"
        with pytest.raises(EuroLeagueDataError, match="not a EuroLeague bundle"):
            parse_euroleague_bundle(bundle)

    def test_load_from_file(self, tmp_path):
        path = tmp_path / "bundle.json"
        path.write_text(__import__("json").dumps(_bundle()), encoding="utf-8")
        raw = load_euroleague_bundle_file(path)
        assert raw["_meta"]["n_games"] == 1

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_euroleague_bundle_file(tmp_path / "nope.json")


class TestEuroLeagueIngestGate:
    def test_bundle_passes_validation_and_ingests(self, tmp_path):
        from datetime import datetime, timezone

        from basketball_agent.agents.validation_agent import DataValidationAgent
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.loader import DataIngestor
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.validators.parsing import parse_dataset

        raw = parse_euroleague_bundle(_bundle())
        dataset, parse_errors = parse_dataset(raw)
        assert not parse_errors
        report = DataValidationAgent(
            now=datetime(2025, 3, 1, tzinfo=timezone.utc)
        ).validate(dataset)
        assert not report.has_errors, report.errors[:3]

        db = Database(tmp_path / "el.db")
        db.initialize()
        result = DataIngestor(db, now=datetime(2025, 3, 1, tzinfo=timezone.utc)).ingest(raw)
        assert result.success
        repo = Repository(db)
        game = repo.get_game("E2024-0001")
        assert game.league is League.EUROLEAGUE
        assert game.competition is Competition.REGULAR_SEASON
