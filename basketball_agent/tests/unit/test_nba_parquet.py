"""Unit tests for the local Parquet -> Phase 7 ingest adapter.

The DataFrame normalization (the risky mapping logic) is fully tested here
with plain pandas; the file-reading path needs duckdb/pyarrow and is skipped
when they are unavailable (it runs under an env that has them, e.g. the uv
env with nbadb).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

pandas = pytest.importorskip("pandas")

from basketball_agent.agents.validation_agent import DataValidationAgent
from basketball_agent.data.collectors.nba_csv import IngestDataError
from basketball_agent.data.collectors.nba_parquet import (
    normalize_dataframe,
    read_parquet_frame,
)
from basketball_agent.data.validators.parsing import parse_dataset

pytestmark = pytest.mark.unit

UTC = timezone.utc
NOW = datetime(2025, 2, 1, 12, 0, tzinfo=UTC)


def llimllib_frame() -> "pandas.DataFrame":
    """llimllib/nba_data style game-per-row frame (home/visitor side suffixes,
    made-shot names fgm/fg3m/ftm)."""
    return pandas.DataFrame([
        {
            "game_id": "g1", "game_date": pandas.Timestamp("2024-10-25"),
            "home_team_id": "LAL", "visitor_team_id": "BOS",
            "fgm_home": 40, "fga_home": 85, "fg3m_home": 12, "fg3a_home": 30,
            "ftm_home": 20, "fta_home": 24, "orb_home": 9, "drb_home": 33,
            "trb_home": 42, "ast_home": 25, "tov_home": 12, "stl_home": 8,
            "blk_home": 5, "pf_home": 19,
            "fgm_visitor": 38, "fga_visitor": 88, "fg3m_visitor": 13, "fg3a_visitor": 36,
            "ftm_visitor": 16, "fta_visitor": 20, "orb_visitor": 10, "drb_visitor": 30,
            "trb_visitor": 40, "ast_visitor": 22, "tov_visitor": 14, "stl_visitor": 7,
            "blk_visitor": 4, "pf_visitor": 21,
            # scores via visitor naming
            "home_score": 112, "visitor_score": 105,
        },
        {
            "game_id": "g2", "game_date": pandas.Timestamp("2024-10-27"),
            "home_team_id": "GSW", "visitor_team_id": "LAL",
            "fgm_home": 34, "fga_home": 78, "fg3m_home": 10, "fg3a_home": 29,
            "ftm_home": 20, "fta_home": 24, "orb_home": 9, "drb_home": 29,
            "trb_home": 38, "ast_home": 20, "tov_home": 15, "stl_home": 5,
            "blk_home": 3, "pf_home": 18,
            "fgm_visitor": 36, "fga_visitor": 80, "fg3m_visitor": 12, "fg3a_visitor": 32,
            "ftm_visitor": 17, "fta_visitor": 20, "orb_visitor": 8, "drb_visitor": 31,
            "trb_visitor": 39, "ast_visitor": 24, "tov_visitor": 13, "stl_visitor": 6,
            "blk_visitor": 4, "pf_visitor": 20,
            "home_score": 98, "visitor_score": 101,
        },
    ])


class TestParquetDataframeNormalization:
    def test_llimllib_style_frame_normalizes(self):
        raw = normalize_dataframe(llimllib_frame())
        dataset, errors = parse_dataset(raw)
        assert not errors
        assert len(dataset.games) == 2
        g = dataset.games[0]
        assert g.home_team_id == "LAL" and g.away_team_id == "BOS"
        assert g.home_score == 112 and g.away_score == 105
        ts = [t for t in dataset.team_stats if t.game_id == "g1" and t.team_id == "LAL"][0]
        assert ts.fg == 40 and ts.fg3 == 12 and ts.ft == 20
        assert ts.trb == 42
        report = DataValidationAgent(now=NOW).validate(dataset)
        assert not report.has_errors, report.to_text()

    def test_missing_possessions_estimated(self):
        raw = normalize_dataframe(llimllib_frame())
        # no possessions column -> standard estimate for every team row
        assert raw["_meta"]["possessions_estimated_for"] == 4
        ts = parse_dataset(raw)[0].team_stats[0]
        assert ts.possessions == pytest.approx(85 + 0.44 * 24 - 9 + 12)

    def test_nan_stats_treated_as_missing(self):
        df = llimllib_frame().iloc[[0]].copy()
        df["ast_home"] = float("nan")
        df["tov_home"] = None
        raw = normalize_dataframe(df)
        dataset, _ = parse_dataset(raw)
        ts = [t for t in dataset.team_stats if t.team_id == "LAL"][0]
        assert ts.ast == 0 and ts.tov == 0  # missing -> neutral defaults, gate still applies

    def test_unknown_team_in_frame_fails_loudly(self):
        df = llimllib_frame().iloc[[0]].copy()
        df["home_team_id"] = "XYZ"
        with pytest.raises(IngestDataError) as exc:
            normalize_dataframe(df)
        assert "XYZ" in str(exc.value)

    def test_row_per_team_frame(self):
        df = pandas.DataFrame([
            {"game_date": pandas.Timestamp("2024-11-01"), "team": "LAL", "is_home": True,
             "score": 110, "fgm": 39, "fga": 84, "fg3m": 11, "fg3a": 29,
             "ftm": 21, "fta": 25, "orb": 10, "drb": 32, "trb": 42,
             "ast": 24, "tov": 13, "stl": 7, "blk": 5, "pf": 18},
            {"game_date": pandas.Timestamp("2024-11-01"), "team": "BOS", "is_home": False,
             "score": 103, "fgm": 37, "fga": 86, "fg3m": 14, "fg3a": 35,
             "ftm": 15, "fta": 19, "orb": 9, "drb": 30, "trb": 39,
             "ast": 21, "tov": 15, "stl": 6, "blk": 4, "pf": 20},
        ])
        raw = normalize_dataframe(df)
        dataset, errors = parse_dataset(raw)
        assert not errors
        assert len(dataset.games) == 1
        assert dataset.games[0].home_score == 110
        report = DataValidationAgent(now=NOW).validate(dataset)
        assert not report.has_errors, report.to_text()


class TestParquetReadPath:
    def test_missing_libs_raise_instructive_error(self, tmp_path):
        target = tmp_path / "x.parquet"
        target.write_bytes(b"PAR1")
        try:
            import duckdb  # noqa: F401

            pytest.skip("duckdb available in this environment")
        except ImportError:
            pass
        try:
            import pyarrow  # noqa: F401

            pytest.skip("pyarrow available in this environment")
        except ImportError:
            pass
        with pytest.raises(IngestDataError) as exc:
            read_parquet_frame(target)
        assert "duckdb" in str(exc.value) and "convert" in str(exc.value)

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            from basketball_agent.data.collectors.nba_parquet import load_nba_games_parquet

            load_nba_games_parquet(tmp_path / "nope.parquet")
