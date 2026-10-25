"""ACB real-data integration (Phase 8-B6 single season, extended in B7).

These tests need the real snapshot DB (``storage/basketball.db``) with the ACB
seasons ingested; they SKIP cleanly when it is absent so the suite stays green
on a clean checkout.  They are read-only: nothing is written back into
``storage/``.

B7 added five more complete seasons (2020-21 .. 2024-25), so the expectations
below cover the full six-season ACB history.  Per-season regular-season counts
differ (2020-21 had 19 clubs / 38 rounds), which is why the audit is given a
per-season schedule reference.
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
#: season -> (all games, regular-season games, playoff games) from the source calendars
#: 2021-22 is 326 (not 327): ACB2021-102902 is a documented source defect
#: (BUR playTime over-count) excluded without repair -> 305 regular, not 306.
ACB_SEASONS: dict[str, tuple[int, int, int]] = {
    "2020-21": (360, 342, 18),   # 19 clubs, 38 rounds (COVID-era longer season)
    "2021-22": (326, 305, 21),
    "2022-23": (325, 306, 19),
    "2023-24": (326, 306, 20),
    "2024-25": (325, 306, 19),
    "2025-26": (327, 306, 21),
}
ACB_TOTAL_GAMES = sum(v[0] for v in ACB_SEASONS.values())          # 1989
ACB_REGULAR_GAMES = sum(v[1] for v in ACB_SEASONS.values())        # 1871
ACB_PLAYOFF_GAMES = sum(v[2] for v in ACB_SEASONS.values())        # 118
ACB_DISTINCT_CLUBS = 25   # union of the six seasons' club registries
ACB_WINDOW_START = datetime(2025, 10, 1, tzinfo=UTC)
ACB_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)


def _real_repo() -> Repository:
    db_path = Settings.from_env().db_path
    if not Path(db_path).exists():
        pytest.skip(f"real snapshot DB not available at {db_path}")
    return Repository(Database(db_path))


def _acb_games(repo: Repository) -> list:
    # FINAL only: the pinned per-season counts audit INGESTED history;
    # production SCHEDULED rows (Phase 12-H fixtures) are a different
    # lifecycle and must not inflate them.
    games = [g for g in repo.list_games() if g.league is League.ACB
             and g.status is GameStatus.FINAL]
    if not games:
        pytest.skip("ACB data not ingested yet")
    return games


class TestRealACBAudit:
    def test_acb_audit_passes_with_per_season_reference(self):
        repo = _real_repo()
        _acb_games(repo)
        audit = audit_league(
            repo, League.ACB, leakage_sample=2,
            expected_regular_by_season={s: v[1] for s, v in ACB_SEASONS.items()},
        )
        assert audit.status != FAIL, [c.detail for c in audit.failed_checks]
        assert audit.n_games == ACB_TOTAL_GAMES
        by_season = {s.season: s for s in audit.seasons}
        assert set(by_season) == set(ACB_SEASONS)
        for season, (total, regular, playoff) in ACB_SEASONS.items():
            assert by_season[season].games == total, season
            assert by_season[season].regular_season == regular, season
            assert by_season[season].other_competitions == playoff, season
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
        assert by_competition.get("REGULAR_SEASON") == ACB_REGULAR_GAMES
        assert by_competition.get("PLAYOFF") == ACB_PLAYOFF_GAMES
        assert all(g.status is GameStatus.FINAL for g in games)
        minutes = sorted({ts.minutes for ts in repo.list_team_stats()
                          if ts.game_id.startswith("ACB")})
        # 201/202 are the two documented 2020-21 playTime-drift rows
        # (ACB2020-101226/ACB-CAZ, ACB2020-101300/ACB-FUE), retained verbatim
        # and counted in _meta.team_minutes_drifted / the audit WARN
        assert all(m in (200, 201, 202, 225, 250) for m in minutes), minutes

    def test_club_identity_survives_sponsor_renames(self):
        """The same club must keep ONE stored id across renamed seasons.

        Real examples: Baskonia was TDS (2020-21) and is BKN (2025-26);
        Tenerife was LNT and is LLT; Joventut was CJB and is JOV.
        """
        repo = _real_repo()
        games = _acb_games(repo)
        for season in ACB_SEASONS:
            assert any(g.season == season for g in games), season
        for team_id in ("ACB-BKN", "ACB-LLT", "ACB-JOV", "ACB-DGC", "ACB-MAN",
                        "ACB-UCM", "ACB-BUR", "ACB-SBB", "ACB-FUE", "ACB-ILE"):
            seasons = {g.season for g in games
                       if team_id in (g.home_team_id, g.away_team_id)}
            assert len(seasons) >= 2, (team_id, seasons)
        # the historical-only clubs appear only in their own seasons
        for team_id, season in (("ACB-MOV", "2020-21"), ("ACB-GBC", "2020-21"),
                                ("ACB-ZPA", "2023-24"), ("ACB-COR", "2024-25")):
            seasons = {g.season for g in games
                       if team_id in (g.home_team_id, g.away_team_id)}
            assert seasons == {season}, (team_id, seasons)

    def test_distinct_club_count(self):
        repo = _real_repo()
        games = _acb_games(repo)
        clubs = {t for g in games for t in (g.home_team_id, g.away_team_id)}
        assert len(clubs) == ACB_DISTINCT_CLUBS, sorted(clubs)
        per_season: dict[str, set[str]] = {}
        for g in games:
            per_season.setdefault(g.season, set()).update(
                (g.home_team_id, g.away_team_id))
        assert len(per_season["2020-21"]) == 19
        for season in ACB_SEASONS:
            if season != "2020-21":
                assert len(per_season[season]) == 18, season

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
    """Golden assertions against the archived raw bundles (untrimmed)."""

    def _bundle_paths(self) -> list[Path]:
        raw_dir = Path(Settings.from_env().raw_dir) / "acb_api"
        candidates = sorted(
            p for p in raw_dir.glob("acb_competition1_edition*.json")
            if p.is_file() and not p.name.endswith(".manifest.json")
        ) if raw_dir.exists() else []
        if not candidates:
            pytest.skip(f"archived ACB raw bundles not available in {raw_dir}")
        return candidates

    def test_every_archived_bundle_parses_and_matches_its_season(self):
        from basketball_agent.data.collectors.acb_api import load_acb_bundle_file

        parsed_seasons = {}
        for path in self._bundle_paths():
            raw = load_acb_bundle_file(path)
            season = raw["_meta"]["season"]
            parsed_seasons[season] = raw
            total, regular, playoff = ACB_SEASONS[season]
            assert raw["_meta"]["n_games"] == total, season
            assert raw["_meta"]["n_final"] == total, season
            assert all(t["team_id"].startswith("ACB-") for t in raw["teams"])
            by_competition: dict[str, int] = {}
            for game in raw["games"]:
                by_competition[game["competition"]] = \
                    by_competition.get(game["competition"], 0) + 1
            assert by_competition == {"REGULAR_SEASON": regular, "PLAYOFF": playoff}, season
        assert set(parsed_seasons) == set(ACB_SEASONS), sorted(parsed_seasons)

    def test_archived_bundles_score_and_stats_match_the_database(self):
        from basketball_agent.data.collectors.acb_api import load_acb_bundle_file

        repo = _real_repo()
        games = {g.game_id: g for g in _acb_games(repo)}
        stored = {(ts.game_id, ts.team_id): ts for ts in repo.list_team_stats()
                  if ts.game_id.startswith("ACB")}
        compared_games = compared_rows = 0
        for path in self._bundle_paths()[:1]:  # full field-by-field for one season
            raw = load_acb_bundle_file(path)
            for game in raw["games"]:
                have = games[game["game_id"]]
                assert have.home_team_id == game["home_team_id"]
                assert have.away_team_id == game["away_team_id"]
                assert have.home_score == game["home_score"]
                assert have.away_score == game["away_score"]
                assert have.season == game["season"]
                assert have.competition.value == game["competition"]
                compared_games += 1
            for ts in raw["team_stats"]:
                have = stored[(ts["game_id"], ts["team_id"])]
                for field in ("minutes", "points", "possessions", "fg", "fga", "fg3",
                              "fg3a", "ft", "fta", "orb", "drb", "trb", "ast", "tov",
                              "stl", "blk", "pf", "is_home"):
                    assert getattr(have, field) == ts[field], (ts["game_id"], field)
                compared_rows += 1
        assert compared_games > 300 and compared_rows > 600

    def test_every_ingested_game_has_two_team_rows(self):
        repo = _real_repo()
        games = {g.game_id for g in _acb_games(repo)}
        rows: dict[str, int] = {}
        for ts in repo.list_team_stats():
            if ts.game_id in games:
                rows[ts.game_id] = rows.get(ts.game_id, 0) + 1
        assert len(rows) == ACB_TOTAL_GAMES
        assert set(rows.values()) == {2}


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
