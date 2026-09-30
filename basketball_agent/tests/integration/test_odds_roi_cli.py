"""Odds ROI CLI end-to-end tests (Phase 11-A.4).

Fixture CSV → odds ingest → odds coverage → odds roi, all in-process
against a throwaway database.  The ROI command is READ-ONLY: the suite
asserts zero side effects on the decision log (decisions.jsonl bytes)
and on the predictions table (row count) across the whole run.
"""
from __future__ import annotations

import contextlib
import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from basketball_agent.cli.main import main
from basketball_agent.data.collectors.synthetic import (
    SyntheticLeagueConfig,
    generate_league,
)
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.dataset import Dataset

pytestmark = pytest.mark.integration

UTC = timezone.utc

ROI_ARGS = ["--window-start", "2024-11-01T00:00:00+00:00",
            "--window-end", "2024-12-01T00:00:00+00:00",
            "--n-simulations", "20", "--warmup", "2"]


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


def run_cli(argv, db_path):
    from basketball_agent.config.settings import get_settings

    with _env("BASKETBALL_DB_PATH", str(db_path)):
        get_settings.cache_clear()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        get_settings.cache_clear()
        return code, out.getvalue(), err.getvalue()


def _write_odds_csv(db_path: Path, csv_path: Path, n_games: int) -> None:
    """Odds CSV (-110/+110 for every game) for the LAST n_games fixtures."""
    games = sorted(
        Repository(Database(db_path)).list_games(
            status=GameStatus.FINAL, league=League.NBA),
        key=lambda g: (g.game_date, g.game_id))[-n_games:]
    lines = ["game_date,away_team,home_team,"
             "close_moneyline_home,close_moneyline_away,bookmaker"]
    for g in games:
        lines.append(f"{g.game_date.date().isoformat()},"
                     f"{g.away_team_id},{g.home_team_id},-110,110,fixture")
    csv_path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture()
def cli_db(tmp_path):
    """Synthetic-league database + a pre-seeded (non-empty) decision log,
    so the zero-side-effect assertion cannot pass vacuously."""
    db_path = tmp_path / "basketball.db"
    db = Database(db_path)
    db.initialize()
    teams, players, games, team_stats, player_stats = generate_league(
        SyntheticLeagueConfig(n_teams=6, games_per_pair=2, seed=2025))
    ds = Dataset(games=tuple(games), team_stats=tuple(team_stats),
                 player_stats=tuple(player_stats), teams=tuple(teams),
                 players=tuple(players))
    raw = {k: [m.model_dump(mode="json") for m in v] for k, v in
           (("teams", ds.teams), ("players", ds.players), ("games", ds.games),
            ("team_stats", ds.team_stats), ("player_stats", ds.player_stats))}
    DataIngestor(db, now=datetime(2025, 3, 1, tzinfo=UTC)).ingest(raw)

    agent_log = tmp_path / "agent_log"
    agent_log.mkdir()
    (agent_log / "decisions.jsonl").write_bytes(b'{"probe": true}\n')
    return db_path


def _side_state(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        n_predictions = conn.execute(
            "SELECT COUNT(*) FROM predictions").fetchone()[0]
    finally:
        conn.close()
    log = db_path.parent / "agent_log" / "decisions.jsonl"
    return n_predictions, log.read_bytes()


def _roi_rows(out: str):
    """Parse threshold/side result rows into (threshold, side, n)."""
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 7 and parts[1] in ("home", "away"):
            rows.append((float(parts[0]), parts[1], int(parts[2])))
    return rows


class TestOddsRoiCli:
    def test_ingest_coverage_roi_end_to_end(self, cli_db, tmp_path):
        csv_path = tmp_path / "odds.csv"
        _write_odds_csv(cli_db, csv_path, n_games=6)

        n_pred_before, log_before = _side_state(cli_db)

        code, out, err = run_cli(
            ["odds", "ingest", "--file", str(csv_path)], cli_db)
        assert code == 0, err
        assert "odds ingest: 6 inserted" in out

        code, out, err = run_cli(["odds", "coverage"], cli_db)
        assert code == 0, err
        assert "ODDS COVERAGE" in out
        assert "games with CLOSE: 6" in out

        code, out, err = run_cli(
            ["odds", "roi", *ROI_ARGS, "--thresholds", "0.05"], cli_db)
        assert code == 0, err
        assert "ROI BACKTEST" in out
        assert "model=simulation" in out
        rows = _roi_rows(out)
        assert (0.05, "home") in [(t, s) for t, s, _ in rows]
        assert (0.05, "away") in [(t, s) for t, s, _ in rows]
        # the deliverable: at threshold 0.05 at least one side has REAL bets
        assert sum(n for t, _, n in rows if t == 0.05) > 0, out

        # zero side effects: predictions row count and decisions.jsonl bytes
        n_pred_after, log_after = _side_state(cli_db)
        assert n_pred_after == n_pred_before
        assert log_after == log_before

    def test_roi_model_switch_changes_edges(self, cli_db, tmp_path):
        """--model selects the probability source: the random model's edge
        is a constant negative offset (fair_home > 0.5 for -110/+110), so
        at t=0 every entry bets AWAY and none bets home."""
        csv_path = tmp_path / "odds.csv"
        _write_odds_csv(cli_db, csv_path, n_games=6)
        code, _, err = run_cli(["odds", "ingest", "--file", str(csv_path)],
                               cli_db)
        assert code == 0, err

        code, out, err = run_cli(
            ["odds", "roi", *ROI_ARGS, "--thresholds", "0.0",
             "--model", "random"], cli_db)
        assert code == 0, err
        assert "model=random" in out
        rows = _roi_rows(out)
        t0 = {side: n for t, side, n in rows if t == 0.0}
        entries = int(out.split("entries=")[1].split()[0])
        with_odds = int(out.split("with_close_odds=")[1].split()[0])
        assert entries > with_odds > 0  # odds cover a strict subset
        # only odds-bearing entries can bet; every one of them has a
        # negative home edge under the random model -> all bets away
        assert t0["away"] == with_odds
        assert t0["home"] == 0
