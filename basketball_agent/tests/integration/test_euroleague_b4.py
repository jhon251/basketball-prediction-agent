"""B4 integration: real EuroLeague audit + league-scoped windows, NBA untouched.

These tests need the real snapshot DB (``storage/basketball.db``); they SKIP
cleanly when it is absent so the suite stays green on a clean checkout.

They are read-only: nothing is written back into ``storage/``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from basketball_agent.config.settings import Settings
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import League
from basketball_agent.evaluation.league_audit import FAIL, audit_league

pytestmark = pytest.mark.integration

UTC = timezone.utc
EL_WINDOW_START = datetime(2024, 10, 1, tzinfo=UTC)
EL_WINDOW_END = datetime(2025, 7, 1, tzinfo=UTC)


def _real_repo() -> Repository:
    db_path = Settings.from_env().db_path
    if not Path(db_path).exists():
        pytest.skip(f"real snapshot DB not available at {db_path}")
    return Repository(Database(db_path))


class TestRealEuroLeagueAudit:
    def test_euroleague_audit_passes_with_reference(self):
        audit = audit_league(
            _real_repo(), League.EUROLEAGUE,
            expected_regular_season_games=306, leakage_sample=2,
        )
        assert audit.status != FAIL, [c.detail for c in audit.failed_checks]
        assert audit.n_games >= 306
        assert audit.seasons, "at least one season must be present"
        for season in audit.seasons:
            assert season.regular_season == 306, season
        assert audit.leakage_sampled, "as-of feature computation must have run"
        assert audit.leakage_failures == ()

    def test_euroleague_namespace_and_regulation(self):
        audit = audit_league(_real_repo(), League.EUROLEAGUE, leakage_sample=0)
        by_name = {c.name: c for c in audit.checks}
        assert by_name["team.namespace"].status == "PASS"
        assert by_name["regulation.minutes"].status == "PASS"
        assert by_name["timezone.aware"].status == "PASS"
        assert by_name["game_id.unique"].status == "PASS"

    def test_nba_rows_unaffected(self):
        """EuroLeague ingestion must not have touched the NBA rows."""
        repo = _real_repo()
        nba_games = [g for g in repo.list_games() if g.league is League.NBA]
        assert len(nba_games) >= 20000, "NBA history must still be present in full"
        audit = audit_league(repo, League.NBA, leakage_sample=0)
        assert audit.status != FAIL, [c.detail for c in audit.failed_checks]
        by_name = {c.name: c for c in audit.checks}
        # NBA namespace must carry no EL- ids (collision guard)
        assert by_name["team.namespace"].status == "PASS"


class TestRealWindowBacktests:
    def test_euroleague_window_backtest_runs(self):
        from basketball_agent.evaluation.experiments import run_window_backtest

        result = run_window_backtest(
            _real_repo(), EL_WINDOW_START, EL_WINDOW_END,
            train_seasons=None, n_simulations=20, seed=7, warmup_games=5,
            league=League.EUROLEAGUE,
        )
        assert set(result["models"]) == {"random", "elo", "strength", "simulation"}
        assert result["n_window_games"] >= 300
        for name, metrics in result["models"].items():
            assert metrics["n_win_prob"] > 0, name
            assert 0.0 <= metrics["log_loss"] <= 1.5, name
            assert 0.0 <= metrics["accuracy"] <= 1.0, name
        # EuroLeague margin/total predictions exist for the analytic models
        for name in ("strength", "simulation"):
            assert result["models"][name]["margin_mae"] is not None

    def test_nba_window_backtest_still_runs(self):
        """A short NBA window still works (no regression from B4 changes)."""
        from basketball_agent.evaluation.experiments import run_window_backtest

        result = run_window_backtest(
            _real_repo(),
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 8, tzinfo=UTC),
            train_seasons=None, n_simulations=10, seed=7, warmup_games=5,
            league=League.NBA,
        )
        assert result["n_window_games"] > 0
        assert set(result["models"]) == {"random", "elo", "strength", "simulation"}
