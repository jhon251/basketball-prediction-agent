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

    def test_ingest_validation_failure_blocks(self, tmp_path):
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text(
            "game_date,home_team,away_team,home_score,away_score\n"
            "2024-10-25,LAL,BOS,112,105\n"
            "2024-10-25,LAL,BOS,112,105\n",  # duplicate game_id -> validation FAIL
            encoding="utf-8",
        )
        db_path = tmp_path / "db2" / "basketball.db"
        code, out, err = run_cli(["ingest", str(csv_path)], {"BASKETBALL_DB_PATH": str(db_path)})
        assert code == 1
        assert "FAIL" in err or "ERROR" in err

    def test_coverage_empty_db(self, tmp_path):
        db_path = tmp_path / "db3" / "basketball.db"
        code, out, _ = run_cli(["coverage"], {"BASKETBALL_DB_PATH": str(db_path)})
        assert code == 0
        assert "Games: 0" in out
        assert "Seasons: n/a" in out
