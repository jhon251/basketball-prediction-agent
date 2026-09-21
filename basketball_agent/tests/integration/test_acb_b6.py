"""B6 integration: real ACB (Liga Endesa) data, audit, windows and isolation.

These tests need the real snapshot DB (``storage/basketball.db``) with the ACB
season ingested; they SKIP cleanly when it is absent so the suite stays green
on a clean checkout.  They are read-only: nothing is written back into
``storage/``.

The real-data golden assertions parse the archived raw bundle when it is
present, so the adapter is checked against the *untrimmed* source payload as
well as the compact unit fixture.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from basketball_agent.config.settings import Settings
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.evaluation.league_audit import FAIL, audit_league

pytestmark = pytest.mark.integration

UTC = timezone.utc
ACB_SEASON = "2025-26"
ACB_WINDOW_START = datetime(2025, 10, 1, tzinfo=UTC)
ACB_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)
REGULAR_SEASON_GAMES = 306      # 18 teams x 34 rounds / 2
PLAYOFF_GAMES = 21              # QF 11 + SF 6 + Final 4 (real 2025-26 structure)
TOTAL_GAMES = REGULAR_SEASON_GAMES + PLAYOFF_GAMES


def _real_repo() -> Repository:
    db_path = Settings.from_env().db_path
    if not Path(db_path).exists():
        pytest.skip(f"real snapshot DB not available at {db_path}")
    return Repository(Database(db_path))


def _acb_games(repo: Repository) -> list:
    games = [g for g in repo.list_games() if g.league is League.ACB]
    if not games:
        pytest.skip("ACB data not ingested yet")
    return games


class TestRealACBAudit:
    def test_acb_audit_passes_with_schedule_reference(self):
        repo = _real_repo()
        _acb_games(repo)
        audit = audit_league(
            repo, League.ACB, leakage_sample=2,
            expected_regular_season_games=REGULAR_SEASON_GAMES,
        )
        assert audit.status != FAIL, [c.detail for c in audit.failed_checks]
        assert audit.n_games == TOTAL_GAMES
        by_season = {s.season: s for s in audit.seasons}
        assert ACB_SEASON in by_season
        season = by_season[ACB_SEASON]
        assert season.regular_season == REGULAR_SEASON_GAMES
        assert season.other_competitions == PLAYOFF_GAMES
        assert season.teams == 18
        assert audit.leakage_sampled, "as-of feature computation must have run"
        assert audit.leakage_failures == ()

    def test_acb_namespace_ids_timezone_and_regulation(self):
        audit = audit_league(_real_repo(), League.ACB, leakage_sample=0)
        by_name = {c.name: c for c in audit.checks}
        assert by_name["game_id.format"].status == "PASS"
        assert by_name["game_id.unique"].status == "PASS"
        assert by_name["team.namespace"].status == "PASS"
        assert by_name["team.registered"].status == "PASS"
        assert by_name["timezone.aware"].status == "PASS"
        assert by_name["final.consistency"].status == "PASS"
        assert by_name["competition.tags"].status == "PASS"
        # 40-minute regulation: every team row is 200 min (or 200 + 25k with OT)
        check = by_name["regulation.minutes"]
        assert check.status in ("PASS", "WARN"), check.detail
        assert "expected 200" in check.detail

    def test_acb_competition_and_ot_split(self):
        repo = _real_repo()
        games = _acb_games(repo)
        by_competition: dict[str, int] = {}
        for game in games:
            key = game.competition.value if game.competition else "UNTAGGED"
            by_competition[key] = by_competition.get(key, 0) + 1
        assert by_competition.get("REGULAR_SEASON") == REGULAR_SEASON_GAMES
        assert by_competition.get("PLAYOFF") == PLAYOFF_GAMES
        assert all(g.status is GameStatus.FINAL for g in games)
        minutes = sorted({ts.minutes for ts in repo.list_team_stats()
                          if ts.game_id.startswith("ACB")})
        assert all(m >= 200 and (m - 200) % 25 == 0 for m in minutes), minutes

    def test_other_leagues_untouched(self):
        """ACB ingestion must not have touched the NBA or EuroLeague rows."""
        repo = _real_repo()
        nba = [g for g in repo.list_games() if g.league is League.NBA]
        el = [g for g in repo.list_games() if g.league is League.EUROLEAGUE]
        assert len(nba) >= 20000
        assert len(el) >= 1800
        for league in (League.NBA, League.EUROLEAGUE):
            audit = audit_league(repo, league, leakage_sample=0)
            assert audit.status != FAIL, (league, [c.detail for c in audit.failed_checks])


class TestRealACBBundle:
    """Golden assertions against the archived raw bundle (untrimmed)."""

    def _bundle_path(self) -> Path:
        raw_dir = Path(Settings.from_env().raw_dir) / "acb_api"
        candidates = sorted(raw_dir.glob("*.json")) if raw_dir.exists() else []
        if not candidates:
            pytest.skip(f"archived ACB raw bundle not available in {raw_dir}")
        return candidates[0]

    def test_archived_bundle_parses_to_the_ingested_season(self):
        from basketball_agent.data.collectors.acb_api import load_acb_bundle_file

        raw = load_acb_bundle_file(self._bundle_path())
        assert raw["_meta"]["n_games"] == TOTAL_GAMES
        assert raw["_meta"]["n_final"] == TOTAL_GAMES
        assert raw["_meta"]["edition_id"] == 90
        assert raw["_meta"]["season"] == ACB_SEASON
        assert len(raw["teams"]) == 18
        assert all(t["team_id"].startswith("ACB-") for t in raw["teams"])

    def test_archived_bundle_scores_match_the_database(self):
        from basketball_agent.data.collectors.acb_api import load_acb_bundle_file

        repo = _real_repo()
        games = {g.game_id: g for g in _acb_games(repo)}
        raw = load_acb_bundle_file(self._bundle_path())
        for game in raw["games"]:
            stored = games[game["game_id"]]
            assert stored.home_team_id == game["home_team_id"]
            assert stored.away_team_id == game["away_team_id"]
            assert stored.home_score == game["home_score"]
            assert stored.away_score == game["away_score"]
            assert stored.season == game["season"]
            assert stored.competition.value == game["competition"]

    def test_archived_bundle_team_stats_match_the_database(self):
        from basketball_agent.data.collectors.acb_api import load_acb_bundle_file

        repo = _real_repo()
        stored = {(ts.game_id, ts.team_id): ts for ts in repo.list_team_stats()
                  if ts.game_id.startswith("ACB")}
        raw = load_acb_bundle_file(self._bundle_path())
        assert len(raw["team_stats"]) == 2 * TOTAL_GAMES
        for ts in raw["team_stats"]:
            have = stored[(ts["game_id"], ts["team_id"])]
            for field in ("minutes", "points", "possessions", "fg", "fga", "fg3",
                          "fg3a", "ft", "fta", "orb", "drb", "trb", "ast", "tov",
                          "stl", "blk", "pf", "is_home"):
                assert getattr(have, field) == ts[field], (ts["game_id"], field)


class TestRealACBWindows:
    def test_acb_window_backtest_runs(self):
        from basketball_agent.evaluation.experiments import run_window_backtest

        repo = _real_repo()
        _acb_games(repo)  # skips when the ACB season is not ingested
        result = run_window_backtest(
            repo, ACB_WINDOW_START, ACB_WINDOW_END,
            train_seasons=None, n_simulations=20, seed=7, warmup_games=5,
            league=League.ACB,
        )
        assert set(result["models"]) == {"random", "elo", "strength", "simulation"}
        assert result["n_window_games"] >= 300
        for name, metrics in result["models"].items():
            assert metrics["n_win_prob"] > 0, name
            assert 0.0 <= metrics["log_loss"] <= 1.5, name
        for name in ("strength", "simulation"):
            assert result["models"][name]["margin_mae"] is not None

    def test_acb_features_stay_inside_the_league(self):
        from basketball_agent.features import FeaturePipeline

        repo = _real_repo()
        games = sorted(_acb_games(repo), key=lambda g: (g.game_date, g.game_id))
        pipeline = FeaturePipeline(repo)
        features = pipeline.compute(games[-1].game_id, as_of=games[-1].game_date)
        used = features.provenance.games_used
        assert used, "the latest ACB game must have prior ACB history"
        for game_id in used:
            other = repo.get_game(game_id)
            assert other is not None and other.league is League.ACB

    def test_nba_and_euroleague_windows_still_run(self):
        """No regression: the other two leagues still evaluate normally."""
        from basketball_agent.evaluation.experiments import run_window_backtest

        repo = _real_repo()
        result = run_window_backtest(
            repo, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 8, tzinfo=UTC),
            train_seasons=None, n_simulations=10, seed=7, warmup_games=5,
            league=League.NBA,
        )
        assert result["n_window_games"] > 0
        el_games = [g for g in repo.list_games() if g.league is League.EUROLEAGUE]
        assert len(el_games) >= 1800
