"""Integration tests for the Phase 7 CLI commands (ingest / coverage)."""
from __future__ import annotations

import contextlib
import io

import pytest

from basketball_agent.cli.main import main

pytestmark = pytest.mark.integration


class _env:
    def __init__(self, key, value):
        import os

        self._os = os
        self.key = key
        self.value = value
        self.old = None

    def __enter__(self):
        self.old = self._os.environ.get(self.key)
        self._os.environ[self.key] = self.value
        return self

    def __exit__(self, *exc):
        if self.old is None:
            self._os.environ.pop(self.key, None)
        else:
            self._os.environ[self.key] = self.old


def run_cli(argv, env: dict[str, str]):
    import os as _os

    from basketball_agent.config.settings import get_settings

    get_settings.cache_clear()
    out, err = io.StringIO(), io.StringIO()
    managers = [_env(k, v) for k, v in env.items()]
    try:
        for m in managers:
            m.__enter__()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
    finally:
        for m in reversed(managers):
            m.__exit__(None, None, None)
    return code, out.getvalue(), err.getvalue()


CSV = """\
game_date,home_team,away_team,home_score,away_score
2024-10-25,LAL,BOS,112,105
2024-10-27,BOS,LAL,101,98
2024-10-29,LAL,GSW,110,100
2024-10-31,GSW,LAL,95,108
2024-11-02,BOS,GSW,115,99
2024-11-04,GSW,BOS,102,111
2024-11-06,LAL,BOS,120,118
2024-11-08,BOS,LAL,103,106
2024-11-10,LAL,GSW,99,101
2024-11-12,GSW,LAL,104,97
"""


class TestCliIngest:
    def test_ingest_creates_snapshot_and_reports_coverage(self, tmp_path):
        from basketball_agent.tests.integration.test_real_data_pipeline import make_real_csv

        csv_path = tmp_path / "nba.csv"
        make_real_csv(csv_path, n_games=10)
        db_path = tmp_path / "db" / "basketball.db"
        raw_dir = tmp_path / "raw"
        snap_dir = tmp_path / "snapshots"
        env = {
            "BASKETBALL_DB_PATH": str(db_path),
            "BASKETBALL_RAW_DIR": str(raw_dir),
            "BASKETBALL_PROCESSED_DIR": str(tmp_path / "processed"),
            "BASKETBALL_SNAPSHOTS_DIR": str(snap_dir),
        }
        code, out, err = run_cli(
            ["ingest", str(csv_path), "--source", "nba_games"], env
        )
        assert code == 0, err
        assert "Ingested 10 games" in out
        assert "Data Snapshot:" in out and "n/a" not in out
        assert "NBA DATA COVERAGE REPORT" in out
        # raw copy preserved
        assert (raw_dir / "nba_games" / "nba.csv").exists()
        assert (raw_dir / "nba_games" / "nba.csv.manifest.json").exists()
        # processed dataset must land in the redirected dir, never the real storage
        assert list((tmp_path / "processed" / "nba_games").glob("dataset_*.json"))

    def test_ingest_validation_failure_blocks(self, tmp_path):
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text(
            "game_date,home_team,away_team,home_score,away_score\n"
            "2024-10-25,LAL,BOS,112,105\n"
            "2024-10-25,LAL,BOS,112,105\n",  # duplicate game_id -> validation FAIL
            encoding="utf-8",
        )
        db_path = tmp_path / "db2" / "basketball.db"
        code, out, err = run_cli(
            ["ingest", str(csv_path)],
            {"BASKETBALL_DB_PATH": str(db_path),
             "BASKETBALL_RAW_DIR": str(tmp_path / "raw2"),
             "BASKETBALL_PROCESSED_DIR": str(tmp_path / "processed2")},
        )
        assert code == 1
        assert "FAIL" in err or "ERROR" in err
        # blocked ingest must not leave processed artifacts either
        assert not (tmp_path / "processed2").exists()

    def test_coverage_empty_db(self, tmp_path):
        db_path = tmp_path / "db3" / "basketball.db"
        code, out, _ = run_cli(["coverage"], {"BASKETBALL_DB_PATH": str(db_path)})
        assert code == 0
        assert "Games: 0" in out
        assert "Seasons: n/a" in out


class TestCliDataStatus:
    def test_unavailable_when_no_sources(self, tmp_path):
        db_path = tmp_path / "db" / "basketball.db"
        code, out, _ = run_cli(
            ["data-status"],
            {"BASKETBALL_DB_PATH": str(db_path),
             "BASKETBALL_RAW_DIR": str(tmp_path / "raw")},
        )
        assert code == 0
        assert "SOURCE_STATUS:" in out
        assert "UNAVAILABLE" in out
        assert "DATA_REQUIREMENTS.md" in out

    def test_available_after_ingest(self, tmp_path):
        from basketball_agent.tests.integration.test_real_data_pipeline import make_real_csv

        csv_path = tmp_path / "nba.csv"
        make_real_csv(csv_path, n_games=8)
        env = {
            "BASKETBALL_DB_PATH": str(tmp_path / "db" / "basketball.db"),
            "BASKETBALL_RAW_DIR": str(tmp_path / "raw"),
            "BASKETBALL_PROCESSED_DIR": str(tmp_path / "processed"),
            "BASKETBALL_SNAPSHOTS_DIR": str(tmp_path / "snapshots"),
        }
        code, out, err = run_cli(["ingest", str(csv_path)], env)
        assert code == 0, err
        code, out, _ = run_cli(["data-status"], env)
        assert code == 0
        assert "AVAILABLE" in out
        assert "Latest snapshot:" in out
        assert "nba_games" in out


class TestCliMultiFileAndValidate:
    def test_multi_file_ingest_merges(self, tmp_path):
        from basketball_agent.tests.integration.test_real_data_pipeline import make_real_csv

        f1 = tmp_path / "week1.csv"
        f2 = tmp_path / "week2.csv"
        make_real_csv(f1, n_games=6)
        # second file: continue the schedule with distinct dates (offset 30 days)
        import io

        lines = f2.read_text(encoding="utf-8") if f2.exists() else ""
        make_real_csv(f2, n_games=6)
        content = f2.read_text(encoding="utf-8").splitlines()
        # shift dates of file2 forward by 30 days to avoid duplicate game ids
        shifted = []
        for line in content:
            if line.startswith("game_date") or not line.strip():
                shifted.append(line)
                continue
            parts = line.split(",")
            from datetime import datetime as _dt, timedelta as _td

            d = _dt.strptime(parts[0], "%Y-%m-%d") + _td(days=30)
            parts[0] = d.strftime("%Y-%m-%d")
            shifted.append(",".join(parts))
        f2.write_text("\n".join(shifted) + "\n", encoding="utf-8")

        env = {
            "BASKETBALL_DB_PATH": str(tmp_path / "db" / "basketball.db"),
            "BASKETBALL_RAW_DIR": str(tmp_path / "raw"),
            "BASKETBALL_PROCESSED_DIR": str(tmp_path / "processed"),
            "BASKETBALL_SNAPSHOTS_DIR": str(tmp_path / "snapshots"),
        }
        code, out, err = run_cli(["ingest", str(f1), str(f2)], env)
        assert code == 0, err
        assert "Ingested 12 games from 2 file(s)" in out
        # both raw files archived with manifests
        assert (tmp_path / "raw" / "nba_games" / "week1.csv").exists()
        assert (tmp_path / "raw" / "nba_games" / "week2.csv").exists()

    def test_cross_file_duplicate_fails(self, tmp_path):
        from basketball_agent.tests.integration.test_real_data_pipeline import make_real_csv

        f1 = tmp_path / "a.csv"
        f2 = tmp_path / "b.csv"
        make_real_csv(f1, n_games=4)
        make_real_csv(f2, n_games=4)  # same rows -> cross-file duplicate game ids
        env = {"BASKETBALL_DB_PATH": str(tmp_path / "db" / "basketball.db"),
               "BASKETBALL_RAW_DIR": str(tmp_path / "raw"),
               "BASKETBALL_PROCESSED_DIR": str(tmp_path / "processed")}
        code, out, err = run_cli(["ingest", str(f1), str(f2)], env)
        assert code == 1
        assert "FAIL" in err or "ERROR" in err

    def test_validate_pass(self, tmp_path):
        from basketball_agent.tests.integration.test_real_data_pipeline import make_real_csv

        csv_path = tmp_path / "good.csv"
        make_real_csv(csv_path, n_games=6)
        code, out, err = run_cli(["validate", str(csv_path)], {})
        assert code == 0, err
        assert "RESULT: PASS" in out

    def test_validate_fail(self, tmp_path):
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text(
            "game_date,home_team,away_team,home_score,away_score,home_fg,home_fga,"
            "home_fg3,home_fg3a,home_ft,home_fta,home_orb,home_drb,home_trb,"
            "away_fg,away_fga,away_fg3,away_fg3a,away_ft,away_fta,away_orb,away_drb,away_trb\n"
            "2024-10-25,LAL,BOS,999,105,40,85,12,30,20,24,9,33,42,"
            "38,88,13,36,16,20,10,30,40\n",  # points identity broken -> FAIL
            encoding="utf-8",
        )
        code, out, err = run_cli(["validate", str(csv_path)], {})
        assert code == 1
        assert "RESULT: FAIL" in out

    def _parquet_engine_available(self) -> bool:
        try:
            import duckdb  # noqa: F401

            return True
        except ImportError:
            pass
        try:
            import pandas  # noqa: F401
            import pyarrow  # noqa: F401

            return True
        except ImportError:
            return False

    def test_convert_invalid_parquet_fails_cleanly(self, tmp_path):
        """`convert` on a non-Parquet file must exit 1 with a clean CLI
        message — no traceback, no silent success — regardless of whether a
        parquet engine is installed:

        * engine present -> the underlying parse error is reported as FAIL;
        * no engine -> the instructive "requires duckdb / convert offline"
          hint is shown instead.
        """
        target = tmp_path / "x.parquet"
        target.write_bytes(b"PAR1-not-a-real-parquet")
        code, out, err = run_cli(
            ["convert", str(target), "--out", str(tmp_path / "out.csv")], {}
        )
        assert code == 1
        assert err.strip(), "expected a diagnostic message on stderr"
        if self._parquet_engine_available():
            assert err.startswith("FAIL")
            assert "parquet" in err.lower()
        else:
            assert err.startswith("FAIL")
            assert "duckdb" in err
            assert "convert" in err
        assert not (tmp_path / "out.csv").exists()

    def test_settle_no_predictions_fails(self, tmp_path):
        """`settle` on a FINAL game without saved predictions is a clean FAIL."""
        from basketball_agent.tests.integration.test_real_data_pipeline import make_real_csv

        csv_path = tmp_path / "nba.csv"
        make_real_csv(csv_path, n_games=8)
        env = {
            "BASKETBALL_DB_PATH": str(tmp_path / "db" / "basketball.db"),
            "BASKETBALL_RAW_DIR": str(tmp_path / "raw"),
            "BASKETBALL_PROCESSED_DIR": str(tmp_path / "processed"),
            "BASKETBALL_SNAPSHOTS_DIR": str(tmp_path / "snapshots"),
        }
        code, _, err = run_cli(["ingest", str(csv_path)], env)
        assert code == 0, err
        code, out, err = run_cli(["settle", "20241025_LAL_BOS"],
                                 {"BASKETBALL_DB_PATH": str(env["BASKETBALL_DB_PATH"])})
        assert code == 1
        assert "no unsettled predictions" in err

    def test_settle_saved_prediction_roundtrip(self, tmp_path):
        """`settle` completes the CLI chain: save (via API) -> settle -> report."""
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.agents.prediction_agent import PredictionAgent
        from basketball_agent.evaluation.settlement import SettlementStore
        from basketball_agent.tests.integration.test_real_data_pipeline import make_real_csv

        csv_path = tmp_path / "nba.csv"
        make_real_csv(csv_path, n_games=24)
        db_path = tmp_path / "db" / "basketball.db"
        env = {
            "BASKETBALL_DB_PATH": str(db_path),
            "BASKETBALL_RAW_DIR": str(tmp_path / "raw"),
            "BASKETBALL_PROCESSED_DIR": str(tmp_path / "processed"),
            "BASKETBALL_SNAPSHOTS_DIR": str(tmp_path / "snapshots"),
        }
        code, _, err = run_cli(["ingest", str(csv_path)], env)
        assert code == 0, err

        # save a pre-game prediction for a mid-series FINAL game via the API
        repo = Repository(Database(db_path))
        games = sorted(repo.list_games(), key=lambda g: (g.game_date, g.game_id))
        target = games[len(games) // 2]
        as_of = target.game_date.replace(hour=0)  # tip-off minus a day
        prediction = PredictionAgent(repo, n_simulations=60, seed=42).predict(
            target.game_id, as_of=as_of
        )
        SettlementStore(Database(db_path)).save(prediction)

        code, out, err = run_cli(["settle", target.game_id],
                                 {"BASKETBALL_DB_PATH": str(db_path)})
        assert code == 0, err
        assert "Settled" in out
        assert "Aggregate" in out
        assert "'n_settled': 1" in out


class TestCliExperiments:
    def test_experiments_smoke(self, tmp_path):
        from basketball_agent.data.collectors.synthetic import (
            SyntheticLeagueConfig, generate_league,
        )
        from basketball_agent.data.db.connection import Database
        from basketball_agent.data.db.loader import DataIngestor
        from basketball_agent.data.schemas.dataset import Dataset
        from datetime import datetime, timezone

        teams, players, games, team_stats, player_stats = generate_league(
            SyntheticLeagueConfig(n_teams=6, games_per_pair=2, seed=2025)
        )
        ds = Dataset(
            games=tuple(games), team_stats=tuple(team_stats), player_stats=tuple(player_stats),
            teams=tuple(teams), players=tuple(players),
        )
        raw = {
            "teams": [t.model_dump(mode="json") for t in ds.teams],
            "players": [p.model_dump(mode="json") for p in ds.players],
            "games": [g.model_dump(mode="json") for g in ds.games],
            "team_stats": [x.model_dump(mode="json") for x in ds.team_stats],
            "player_stats": [x.model_dump(mode="json") for x in ds.player_stats],
        }
        db_path = tmp_path / "db" / "basketball.db"
        db = Database(db_path)
        db.initialize()
        DataIngestor(db, now=datetime(2025, 2, 1, 12, 0, tzinfo=timezone.utc)).ingest(raw)

        ordered = sorted(ds.games, key=lambda g: (g.game_date, g.game_id))
        train_cutoff = ordered[15].game_date.isoformat()
        test_start = ordered[15].game_date.isoformat()
        code, out, err = run_cli(
            ["experiments", "--train-cutoff", train_cutoff, "--test-start", test_start,
             "--n-simulations", "50", "--seed", "7", "--warmup", "2"],
            {"BASKETBALL_DB_PATH": str(db_path)},
        )
        assert code == 0, err
        assert "FROZEN PARAMETER REPORT" in out
        for exp_id in ("EXP-NBA-001", "EXP-NBA-002", "EXP-NBA-003", "EXP-NBA-004"):
            assert exp_id in out
