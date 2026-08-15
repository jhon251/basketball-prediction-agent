"""Integration tests for the basketball CLI (in-process; no subprocess)."""
from __future__ import annotations

import contextlib
import io

import pytest

from basketball_agent.cli.main import format_report, main
from basketball_agent.data.db.repository import Repository

pytestmark = pytest.mark.integration


def run_cli(argv, db_path):
    import os

    with _env("BASKETBALL_DB_PATH", db_path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


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


class TestCliPredict:
    def test_predict_scheduled_game(self, scheduled_target):
        from basketball_agent.config.settings import get_settings

        get_settings.cache_clear()
        code, out, err = run_cli(
            ["predict", "20241120_LAL_BOS", "--as-of", "2024-11-19T12:00:00+00:00",
             "--n-simulations", "200", "--seed", "42"],
            str(scheduled_target.path),
        )
        assert code == 0, err
        for required in (
            "Basketball Simulation & Prediction Agent V1",
            "Game: Los Angeles Lakers vs Boston Celtics",
            "Win Probability:",
            "HOME:",
            "AWAY:",
            "Expected Score:",
            "Expected Margin:",
            "Expected Total:",
            "Simulation: 200 games",
            "HOME wins:",
            "AWAY wins:",
            "Score Distribution:",
            "Margin Distribution:",
            "Total Distribution:",
            "Confidence:",
            "Risk Flags:",
            "Model Version:",
            "Data Snapshot:",
        ):
            assert required in out

    def test_predict_deterministic_with_seed(self, scheduled_target):
        from basketball_agent.config.settings import get_settings

        get_settings.cache_clear()
        argv = ["predict", "20241120_LAL_BOS", "--as-of", "2024-11-19T12:00:00+00:00",
                "--n-simulations", "100", "--seed", "7"]
        code1, out1, _ = run_cli(argv, str(scheduled_target.path))
        get_settings.cache_clear()
        code2, out2, _ = run_cli(argv, str(scheduled_target.path))
        assert code1 == code2 == 0
        assert out1 == out2

    def test_predict_unknown_game_fails(self, scheduled_target):
        from basketball_agent.config.settings import get_settings

        get_settings.cache_clear()
        code, _, err = run_cli(
            ["predict", "NOPE", "--n-simulations", "50"], str(scheduled_target.path)
        )
        assert code == 1
        assert "FAIL" in err

    def test_version_command(self, tmp_path):
        from basketball_agent import __version__

        code, out, _ = run_cli(["version"], str(tmp_path / "unused.db"))
        assert code == 0
        assert __version__ in out

    def test_format_report_contents(self, scheduled_target):
        from datetime import datetime, timezone

        from basketball_agent.agents.prediction_agent import PredictionAgent

        repo = Repository(scheduled_target)
        prediction = PredictionAgent(repo, n_simulations=100, seed=7).predict(
            "20241120_LAL_BOS", as_of=datetime(2024, 11, 19, 12, 0, tzinfo=timezone.utc)
        )
        report = format_report(prediction, {"LAL": "Lakers", "BOS": "Celtics"})
        assert "Lakers vs Celtics" in report
        assert "Confidence:" in report
