"""Phase 12-G: player log adapter + injury adapter tests (fixtures, no net)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from basketball_agent.data.collectors.espn_injuries import (
    STATUS_MAP,
    UnknownInjuryStatusError,
    apply_injuries,
    parse_injuries,
    load_injuries_file,
)
from basketball_agent.data.collectors.nba_playerlog import (
    PlayerlogError,
    _minutes_to_float,
    parse_playerlog,
    parse_roster,
)

UTC = timezone.utc
INJ_FIXTURE = (Path(__file__).parent.parent / "fixtures"
               / "espn_injuries_20261025.json")


def _log_row(**over) -> dict:
    base = {
        "gameId": 22500088, "teamTricode": "BOS", "personId": 201942,
        "firstName": "DeMar", "familyName": "DeRozan",
        "minutes": "37:25", "points": 30, "fieldGoalsAttempted": 20,
        "threePointersAttempted": 2, "freeThrowsAttempted": 8,
        "reboundsTotal": 5, "assists": 4, "turnovers": 2,
        "steals": 1, "blocks": 0, "foulsPersonal": 1,
    }
    base.update(over)
    return base


class TestMinutesParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("37:25", 37.4), ("0:30", 0.5), ("48:00", 48.0),
        ("30:60", 31.0),  # upstream rounding artifact -> carry
        ("", 0.0), (None, 0.0), ("nan", 0.0),
    ])
    def test_formats(self, raw, expected):
        assert _minutes_to_float(raw) == pytest.approx(expected, abs=0.05)

    def test_garbage_fails_loud(self):
        with pytest.raises(PlayerlogError):
            _minutes_to_float("abc")
        with pytest.raises(PlayerlogError):
            _minutes_to_float("12:99")


class TestParsePlayerlog:
    GAMES = {"0022500088": ("BOS", "LAL")}

    def test_row_contract(self):
        bundle = parse_playerlog([_log_row()], games_by_id=self.GAMES)
        assert len(bundle.stats) == 1
        s = bundle.stats[0]
        assert s["game_id"] == "0022500088"  # leading zeros restored
        assert s["player_id"] == "201942"
        assert s["team_id"] == "BOS"
        assert s["minutes"] == pytest.approx(37.4, abs=0.05)
        assert s["points"] == 30 and s["fga"] == 20
        assert bundle.players[0]["name"] == "DeMar DeRozan"

    def test_unknown_game_skipped_counted(self):
        bundle = parse_playerlog([_log_row(gameId=999)],
                                 games_by_id=self.GAMES)
        assert bundle.stats == []
        assert bundle.skipped_unknown_game == 1

    def test_dnp_row_skipped(self):
        bundle = parse_playerlog([_log_row(minutes="")],
                                 games_by_id=self.GAMES)
        assert bundle.stats == []
        assert bundle.skipped_no_minutes == 1

    def test_historical_alias_team_canonicalised(self):
        bundle = parse_playerlog([_log_row(teamTricode="NJN")],
                                 games_by_id={"0022500088": ("BKN", "LAL")})
        assert bundle.stats[0]["team_id"] == "BKN"

    def test_unknown_team_fails_loud(self):
        with pytest.raises(PlayerlogError, match="teamTricode"):
            parse_playerlog([_log_row(teamTricode="XXX")],
                            games_by_id=self.GAMES)

    def test_missing_person_id_fails(self):
        with pytest.raises(PlayerlogError, match="personId"):
            parse_playerlog([_log_row(personId=None)],
                            games_by_id=self.GAMES)


class TestParseRoster:
    def test_basic(self):
        rows = parse_roster([
            {"player_id": 201942, "player_name": "DeMar DeRozan",
             "team_abbreviation": "BOS"},
            {"player_id": None, "player_name": "ghost"},
        ])
        assert rows[0] == {"player_id": "201942",
                           "name": "DeMar DeRozan", "team_id": "BOS"}
        assert len(rows) == 1


class TestStatusMap:
    def test_out_is_hard_zero(self):
        assert STATUS_MAP["Out"] == ("OUT", 0.0)

    def test_every_status_has_explicit_probability(self):
        for status, (name, p) in STATUS_MAP.items():
            assert 0.0 <= p <= 1.0
            assert name and p is not None


class TestParseInjuries:
    @pytest.fixture(scope="class")
    def records(self):
        return parse_injuries(load_injuries_file(INJ_FIXTURE))

    def test_real_fixture_parsed(self, records):
        assert len(records) >= 50
        out = [r for r in records if r.status == "OUT"]
        assert out and all(r.probability == 0.0 for r in out)
        teams = {r.team_id for r in records}
        assert all(len(t) == 3 for t in teams)
        assert "ATL" in teams  # ESPN id 1 -> ATL via the map

    def test_espn_player_id_parsed_from_link(self, records):
        gueye = next(r for r in records
                     if r.player_name == "Mouhamed Gueye")
        assert gueye.espn_player_id == "4712863"

    def test_unknown_status_fails_loud(self):
        payload = {"injuries": [{"id": "1", "injuries": [
            {"status": "Maybe?", "date": "2026-10-25T00:00Z",
             "athlete": {"displayName": "X Y", "links": []}}]}]}
        with pytest.raises(UnknownInjuryStatusError):
            parse_injuries(payload)


class TestApplyInjuries:
    @pytest.fixture
    def setup(self, db):
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.schemas.base import (
            Competition, GameStatus, League,
        )
        from basketball_agent.data.schemas.game import Game
        from basketball_agent.data.schemas.player import Player
        from basketball_agent.data.schemas.team import Team

        repo = Repository(db)
        for tid, name in (("MIL", "Milwaukee Bucks"),
                          ("CHI", "Chicago Bulls")):
            repo.upsert_team(Team(team_id=tid, league=League.NBA,
                                  name=name, abbreviation=tid))
        repo.upsert_player(Player(player_id="1", league=League.NBA,
                                  name="Giannis Antetokounmpo",
                                  current_team_id="MIL"))
        now = datetime.now(UTC)
        repo.insert_game(Game(
            game_id="G1", league=League.NBA, season="2026-27",
            game_date=now + timedelta(days=1),
            home_team_id="MIL", away_team_id="CHI",
            home_score=None, away_score=None,
            status=GameStatus.SCHEDULED,
            competition=Competition.REGULAR_SEASON,
        ))
        return repo

    def _record(self, name="Giannis Antetokounmpo", team="MIL"):
        from basketball_agent.data.collectors.espn_injuries import (
            InjuryRecord,
        )

        return InjuryRecord(team_id=team, player_name=name,
                            espn_player_id="999", status="OUT",
                            probability=0.0,
                            reported_at=datetime.now(UTC))

    def test_writes_availability_for_upcoming_game(self, setup):
        repo = setup
        res = apply_injuries(repo, [self._record()])
        assert res.availability_written == 1
        rows = repo.list_availability(game_id="G1")
        assert len(rows) == 1 and rows[0].status.value == "OUT"
        assert rows[0].availability_probability == 0.0
        assert rows[0].source == "espn-injuries"

    def test_idempotent_rerun_skips(self, setup):
        repo = setup
        apply_injuries(repo, [self._record()])
        res2 = apply_injuries(repo, [self._record()])
        assert res2.availability_written == 0
        assert res2.skipped_existing == 1

    def test_unresolved_player_counted(self, setup):
        res = apply_injuries(setup, [self._record(name="Nobody Here")])
        assert res.skipped_unresolved_player == 1
        assert res.availability_written == 0

    def test_ambiguous_name_unresolved(self, setup):
        from basketball_agent.data.schemas.base import League
        from basketball_agent.data.schemas.player import Player

        repo = setup
        repo.upsert_player(Player(player_id="2", league=League.NBA,
                                  name="giannis antetokounmpo",
                                  current_team_id="CHI"))
        res = apply_injuries(repo, [self._record()])
        assert res.skipped_unresolved_player == 1

    def test_other_team_games_not_polluted(self, setup):
        repo = setup
        rec = self._record(team="CHI")
        res = apply_injuries(repo, [rec])
        rows = repo.list_availability(game_id="G1")
        assert len(rows) == 1  # CHI plays in G1 (away side)
        assert rows[0].player_id == "1"
