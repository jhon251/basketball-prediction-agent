"""Unit tests for the stats.nba.com team-gamelog adapter (llimllib layout)."""
from __future__ import annotations

import pandas as pd
import pytest

from basketball_agent.data.collectors.nba_csv import IngestDataError
from basketball_agent.data.collectors.nba_gamelog import (
    load_nba_gamelog_frame, looks_like_gamelog,
)


def _row(**overrides):
    base = dict(
        season_year="2025-26", game_id="0022500001", game_date="2025-10-21T00:00:00",
        team_abbreviation="LAL", matchup="LAL vs. BOS", pts=112,
        fgm=40, fga=85, fg3m=12, fg3a=30, ftm=20, fta=24,
        oreb=9, dreb=33, reb=42, ast=25, tov=12, stl=8, blk=5, pf=19, poss=101,
    )
    base.update(overrides)
    return base


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestGamelogAdapter:
    def test_looks_like_gamelog(self):
        assert looks_like_gamelog(_frame([_row()]))
        box = pd.DataFrame([{"game_date": "2025-10-21", "home_team": "LAL",
                             "away_team": "BOS", "home_score": 112, "away_score": 105}])
        assert not looks_like_gamelog(box)

    def test_normal_game_pivot(self):
        rows = [
            _row(),  # home row
            _row(team_abbreviation="BOS", matchup="BOS @ LAL", pts=105,
                 fgm=38, fga=88, fg3m=13, fg3a=36, ftm=16, fta=20,
                 oreb=10, dreb=30, reb=40, ast=22, tov=14, stl=7, blk=4, pf=21, poss=102),
        ]
        result = load_nba_gamelog_frame(_frame(rows))
        assert result["_meta"]["n_games"] == 1
        game = result["games"][0]
        assert game["game_id"] == "0022500001"
        assert game["home_team_id"] == "LAL" and game["away_team_id"] == "BOS"
        assert game["home_score"] == 112 and game["away_score"] == 105
        assert game["season"] == "2025-26"
        assert game["status"] == "FINAL"
        home = next(ts for ts in result["team_stats"] if ts["team_id"] == "LAL")
        away = next(ts for ts in result["team_stats"] if ts["team_id"] == "BOS")
        # fgm->fg, ftm->ft, oreb->orb, dreb->drb, reb->trb
        assert home["fg"] == 40 and home["ft"] == 20
        assert home["orb"] == 9 and home["drb"] == 33 and home["trb"] == 42
        assert home["possessions"] == 101.0
        assert away["possessions"] == 102.0

    def test_historical_aliases_resolve(self):
        # NJN/NOH appear in older-season gamelogs
        rows = [
            _row(game_id="0021100001", season_year="2011-12",
                 team_abbreviation="NJN", matchup="NJN vs. BOS", pts=100),
            _row(game_id="0021100001", season_year="2011-12",
                 team_abbreviation="BOS", matchup="BOS @ NJN", pts=98),
        ]
        result = load_nba_gamelog_frame(_frame(rows))
        game = result["games"][0]
        assert game["home_team_id"] == "BKN"  # NJN -> BKN
        assert game["away_team_id"] == "BOS"

    def test_neutral_site_deterministic(self):
        # both rows "@" (neutral site): home = alphabetically second team
        rows = [
            _row(game_id="0022400147", team_abbreviation="MIA", matchup="MIA @ WAS", pts=99),
            _row(game_id="0022400147", team_abbreviation="WAS", matchup="WAS @ MIA", pts=98),
        ]
        result = load_nba_gamelog_frame(_frame(rows))
        game = result["games"][0]
        assert game["home_team_id"] == "WAS" and game["away_team_id"] == "MIA"
        assert result["_meta"]["neutral_site_games"] == 1

    def test_exhibitions_excluded(self):
        rows = [
            _row(),  # regular game stays
            _row(team_abbreviation="BOS", matchup="BOS @ LAL", pts=105,
                 fgm=38, fga=88, fg3m=13, fg3a=36, ftm=16, fta=20,
                 oreb=10, dreb=30, reb=40, ast=22, tov=14, stl=7, blk=4, pf=21, poss=102),
            _row(game_id="0012500055", team_abbreviation="GUA", matchup="GUA @ MIN", pts=74),
            _row(game_id="0012500055", team_abbreviation="MIN", matchup="MIN vs. GUA", pts=100),
            _row(game_id="0032500004", team_abbreviation=None, matchup=None, pts=40),
        ]
        result = load_nba_gamelog_frame(_frame(rows))
        assert result["_meta"]["n_games"] == 1

    def test_unknown_team_fails_loudly(self):
        rows = [
            _row(),
            _row(team_abbreviation="XYZ", matchup="XYZ @ LAL", pts=100),
        ]
        with pytest.raises(IngestDataError, match="unknown team abbreviations"):
            load_nba_gamelog_frame(_frame(rows))

    def test_missing_columns_fails(self):
        with pytest.raises(IngestDataError, match="missing required columns"):
            load_nba_gamelog_frame(_frame([{"season_year": "2025-26"}]))

    def test_single_team_row_fails(self):
        with pytest.raises(IngestDataError, match="expected 2 team rows"):
            load_nba_gamelog_frame(_frame([_row()]))

    def test_dispatch_via_file(self, tmp_path):
        """A gamelog parquet routes through the gamelog adapter automatically."""
        rows = [
            _row(),
            _row(team_abbreviation="BOS", matchup="BOS @ LAL", pts=105,
                 fgm=38, fga=88, fg3m=13, fg3a=36, ftm=16, fta=20,
                 oreb=10, dreb=30, reb=40, ast=22, tov=14, stl=7, blk=4, pf=21, poss=102),
        ]
        path = tmp_path / "gamelog_test.parquet"
        _frame(rows).to_parquet(path, index=False)
        from basketball_agent.data.collectors.nba_parquet import load_nba_games_file

        result = load_nba_games_file(path)
        assert result["_meta"]["n_games"] == 1

    def test_competition_tagged_from_prefix(self):
        rows = [
            _row(),  # 002 -> REGULAR_SEASON
            _row(team_abbreviation="BOS", matchup="BOS @ LAL", pts=105,
                 fgm=38, fga=88, fg3m=13, fg3a=36, ftm=16, fta=20,
                 oreb=10, dreb=30, reb=40, ast=22, tov=14, stl=7, blk=4, pf=21, poss=102),
            # 004 -> PLAYOFF
            _row(game_id="0042500405", season_year="2025-26",
                 game_date="2026-06-13T00:00:00", team_abbreviation="SAS",
                 matchup="SAS vs. NYK", pts=90),
            _row(game_id="0042500405", season_year="2025-26",
                 game_date="2026-06-13T00:00:00", team_abbreviation="NYK",
                 matchup="NYK @ SAS", pts=94),
        ]
        result = load_nba_gamelog_frame(_frame(rows))
        comps = {g["game_id"]: g["competition"] for g in result["games"]}
        assert comps["0022500001"] == "REGULAR_SEASON"
        assert comps["0042500405"] == "PLAYOFF"

    def test_merge_preserves_neutral_count(self):
        from basketball_agent.data.collectors.nba_parquet import merge_raw_datasets

        rows = [
            _row(team_abbreviation="MIA", matchup="MIA @ WAS", pts=99,
                 game_id="0022400147"),
            _row(team_abbreviation="WAS", matchup="WAS @ MIA", pts=98,
                 game_id="0022400147"),
        ]
        raw = load_nba_gamelog_frame(_frame(rows))
        assert raw["_meta"]["neutral_site_games"] == 1
        merged = merge_raw_datasets([raw, raw])
        assert merged["_meta"]["neutral_site_games"] == 2
        assert merged["_meta"]["n_games"] == 2
