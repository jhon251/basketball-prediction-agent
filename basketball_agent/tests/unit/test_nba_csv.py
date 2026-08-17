"""Unit tests for the real-NBA CSV collector (both layouts, aliases, errors)."""
from __future__ import annotations

import pytest

from basketball_agent.data.collectors.nba_csv import IngestDataError, load_nba_games_csv
from basketball_agent.data.validators.parsing import parse_dataset

pytestmark = pytest.mark.unit


GAME_PER_ROW_CSV = """\
game_date,home_team,away_team,home_score,away_score,home_fg,home_fga,home_fg3,home_fg3a,home_ft,home_fta,home_orb,home_drb,home_trb,home_ast,home_tov,home_stl,home_blk,home_pf,away_fg,away_fga,away_fg3,away_fg3a,away_ft,away_fta,away_orb,away_drb,away_trb,away_ast,away_tov,away_stl,away_blk,away_pf
2024-10-25,LAL,BOS,112,105,40,85,12,30,20,24,9,33,42,25,12,8,5,19,38,88,13,36,16,20,10,30,40,22,14,7,4,21
2024-10-27,GSW,LAL,98,101,34,78,10,29,20,24,9,29,38,20,15,5,3,18,36,80,12,32,17,20,8,31,39,24,13,6,4,20
"""

ROW_PER_TEAM_CSV = """\
date,team,is_home,score,fg,fga,fg3,fg3a,ft,fta,orb,drb,trb,ast,tov,stl,blk,pf
2024-11-01,LAL,1,110,39,84,11,29,21,25,10,32,42,24,13,7,5,18
2024-11-01,BOS,0,103,37,86,14,35,15,19,9,30,39,21,15,6,4,20
2024-11-03,SEA,1,100,35,80,9,27,21,26,11,30,41,20,16,5,3,19
2024-11-03,LAL,0,104,37,82,13,31,17,21,8,32,40,23,12,6,4,17
"""


class TestGamePerRowLayout:
    def test_parse_with_full_stats(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(GAME_PER_ROW_CSV, encoding="utf-8")
        raw = load_nba_games_csv(f)
        dataset, errors = parse_dataset(raw)
        assert not errors
        assert len(dataset.games) == 2
        assert len(dataset.team_stats) == 4
        g = dataset.games[0]
        assert g.home_team_id == "LAL" and g.away_team_id == "BOS"
        assert g.home_score == 112 and g.away_score == 105
        assert g.status.value == "FINAL"
        # stats mapped to the right side
        ts = [t for t in dataset.team_stats if t.game_id == g.game_id and t.team_id == "LAL"][0]
        assert ts.fg == 40 and ts.fga == 85 and ts.points == 112

    def test_historical_alias_unified(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(
            "game_date,home_team,away_team,home_score,away_score\n"
            "2024-11-01,SEA,NJN,100,90\n", encoding="utf-8"
        )
        raw = load_nba_games_csv(f)
        dataset, _ = parse_dataset(raw)
        assert dataset.games[0].home_team_id == "OKC"
        assert dataset.games[0].away_team_id == "BKN"

    def test_possessions_estimated_when_missing(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(GAME_PER_ROW_CSV, encoding="utf-8")
        raw = load_nba_games_csv(f)
        dataset, _ = parse_dataset(raw)
        # possessions absent in this CSV -> estimate fga + 0.44*fta - orb + tov
        ts = dataset.team_stats[0]
        assert ts.possessions == pytest.approx(85 + 0.44 * 24 - 9 + 12)
        assert raw["_meta"]["possessions_estimated_for"] == 4

    def test_unknown_team_reported_loudly(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(
            "game_date,home_team,away_team,home_score,away_score\n"
            "2024-11-01,LAL,XYZ,100,90\n", encoding="utf-8"
        )
        with pytest.raises(IngestDataError) as exc:
            load_nba_games_csv(f)
        assert "XYZ" in str(exc.value)

    def test_missing_required_column(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text("game_date,home_team,away_team\n2024-11-01,LAL,BOS\n", encoding="utf-8")
        with pytest.raises(IngestDataError) as exc:
            load_nba_games_csv(f)
        assert "home_score" in str(exc.value)

    def test_column_alias_normalization(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(
            "DATE,HT,AT,PTS_HOME,PTS_AWAY\n2024/11/01,lal,bos,110,105\n", encoding="utf-8"
        )
        raw = load_nba_games_csv(f)
        dataset, _ = parse_dataset(raw)
        assert dataset.games[0].home_score == 110


class TestRowPerTeamLayout:
    def test_parse_and_pair(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(ROW_PER_TEAM_CSV, encoding="utf-8")
        raw = load_nba_games_csv(f)
        dataset, errors = parse_dataset(raw)
        assert not errors
        assert len(dataset.games) == 2
        g = dataset.games[0]
        assert g.home_team_id == "LAL" and g.away_team_id == "BOS"
        assert g.home_score == 110 and g.away_score == 103
        # SEA maps to OKC in the second game
        assert dataset.games[1].home_team_id == "OKC"

    def test_odd_rows_rejected(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(
            "date,team,is_home,score\n2024-11-01,LAL,1,110\n", encoding="utf-8"
        )
        with pytest.raises(IngestDataError) as exc:
            load_nba_games_csv(f)
        assert "odd number" in str(exc.value)


class TestNbaDataStyleAliases:
    def test_visitor_and_made_shot_naming(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(
            "game_id,game_date,home_team_id,visitor_team_id,home_score,visitor_score,"
            "fgm_home,fga_home,fg3m_home,fg3a_home,ftm_home,fta_home,orb_home,drb_home,trb_home,"
            "fgm_visitor,fga_visitor,fg3m_visitor,fg3a_visitor,ftm_visitor,fta_visitor,"
            "orb_visitor,drb_visitor,trb_visitor\n"
            "g1,2024-10-25,LAL,BOS,112,105,"
            "40,85,12,30,20,24,9,33,42,"
            "38,88,13,36,16,20,10,30,40\n",
            encoding="utf-8",
        )
        raw = load_nba_games_csv(f)
        dataset, errors = parse_dataset(raw)
        assert not errors
        g = dataset.games[0]
        assert g.home_team_id == "LAL" and g.away_team_id == "BOS"
        assert g.home_score == 112 and g.away_score == 105
        lal = [t for t in dataset.team_stats if t.team_id == "LAL"][0]
        bos = [t for t in dataset.team_stats if t.team_id == "BOS"][0]
        assert lal.fg == 40 and lal.fg3 == 12 and lal.ft == 20 and lal.trb == 42
        assert bos.fg == 38 and bos.fg3 == 13 and bos.ft == 16 and bos.trb == 40

    def test_3pm_alias_row_per_team(self, tmp_path):
        f = tmp_path / "games.csv"
        f.write_text(
            "date,team,is_home,score,fgm,fga,3pm,fg3a,ftm,fta,orb,drb,trb,ast,tov,stl,blk,pf\n"
            "2024-11-01,LAL,1,110,39,84,11,29,21,25,10,32,42,24,13,7,5,18\n"
            "2024-11-01,BOS,0,103,37,86,14,35,15,19,9,30,39,21,15,6,4,20\n",
            encoding="utf-8",
        )
        dataset, errors = parse_dataset(load_nba_games_csv(f))
        assert not errors
        lal = [t for t in dataset.team_stats if t.team_id == "LAL"][0]
        assert lal.fg == 39 and lal.fg3 == 11 and lal.ft == 21


class TestCanonicalCsvWriter:
    def test_roundtrip(self, tmp_path):
        from basketball_agent.data.collectors.nba_csv import write_canonical_csv

        src = tmp_path / "in.csv"
        src.write_text(GAME_PER_ROW_CSV, encoding="utf-8")
        raw = load_nba_games_csv(src)
        out = tmp_path / "canonical.csv"
        write_canonical_csv(raw, out)
        roundtrip, errors = parse_dataset(load_nba_games_csv(out))
        original, _ = parse_dataset(raw)
        assert not errors
        assert len(roundtrip.games) == len(original.games)
        assert roundtrip.games == original.games
        assert roundtrip.team_stats == original.team_stats
