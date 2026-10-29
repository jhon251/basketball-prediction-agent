"""Phase 12-J: EuroLeague season fixtures bootstrap (Header enumeration)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from basketball_agent.collection.euroleague_fixtures import (
    bootstrap_euroleague_season,
)
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

HOME_NAME = "CRVENA ZVEZDA MERIDIANBET BELGRADE"
AWAY_NAME = "ZALGIRIS KAUNAS"


def _totr(points: int) -> dict:
    return {"Minutes": "200:00", "Points": points,
            "FieldGoalsMade2": 20, "FieldGoalsAttempted2": 40,
            "FieldGoalsMade3": 8, "FieldGoalsAttempted3": 20,
            "FreeThrowsMade": 15, "FreeThrowsAttempted": 18,
            "OffensiveRebounds": 8, "DefensiveRebounds": 26,
            "TotalRebounds": 34, "Assistances": 18, "Turnovers": 12,
            "Steals": 6, "BlocksFavour": 3, "FoulsCommited": 19}


def _box(home_pts: int, away_pts: int) -> dict:
    return {"Stats": [
        {"Team": HOME_NAME, "totr": _totr(home_pts)},
        {"Team": AWAY_NAME, "totr": _totr(away_pts)},
    ]}


def _header(*, date="22/10/2026", hour="20:00", score_a=None, score_b=None,
            phase="REGULAR SEASON", round_="7"):
    return {"CodeTeamA": "RED", "TeamA": HOME_NAME,
            "CodeTeamB": "ZAL", "TeamB": AWAY_NAME,
            "Date": date, "Hour": hour, "Phase": phase, "Round": round_,
            "ScoreA": score_a, "ScoreB": score_b}


def _world(*, game2: dict | None = None, headers: dict | None = None):
    """fetch_fn over gameCodes 1..: 1 played, 2 future (or overridden),
    everything else empty."""
    headers = headers or {
        "1": _header(score_a="95", score_b="88"),
        "2": game2 if game2 is not None else _header(date="28/10/2026"),
    }
    boxes = {"1": _box(95, 88)}

    def fetch(url: str) -> dict:
        kind = "Header" if "Header?" in url else "Boxscore"
        code = str(int(url.split("gameCode=")[1].split("&")[0]))
        if kind == "Header":
            if code in headers:
                return headers[code]
            raise RuntimeError("beyond schedule")
        if code in boxes:
            return boxes[code]
        return {"Stats": []}

    calls: list[str] = []

    def counted(url: str) -> dict:
        calls.append(url)
        return fetch(url)

    return counted, calls


@pytest.fixture(autouse=True)
def _seed_teams(db):
    from basketball_agent.data.schemas.base import League
    from basketball_agent.data.schemas.team import Team

    repo = Repository(db)
    for code, name in (("RED", HOME_NAME), ("ZAL", AWAY_NAME)):
        repo.upsert_team(Team(team_id=f"EL-{code}",
                              league=League.EUROLEAGUE, name=name,
                              abbreviation=code))


def _bootstrap(db, tmp_path, fetch_fn, *, season="E2026", **kw):
    return bootstrap_euroleague_season(
        db, tmp_path / "collector", season,
        out_dir=tmp_path / "raw", fetch_fn=fetch_fn,
        now=kw.pop("now", NOW), **kw)


class TestBootstrap:
    def test_played_and_future_games(self, db, tmp_path):
        fetch, _ = _world()
        delta = _bootstrap(db, tmp_path, fetch)
        repo = Repository(db)
        final = repo.get_game("E2026-0001")
        assert final is not None and final.status is GameStatus.FINAL
        assert (final.home_score, final.away_score) == (95, 88)
        assert final.game_date.tzinfo is not None  # venue tz -> UTC
        upcoming = repo.get_game("E2026-0002")
        assert upcoming is not None
        assert upcoming.status is GameStatus.SCHEDULED
        assert delta.inserted_final == 1 and delta.upserted_scheduled == 1
        assert delta.team_stats_inserted == 2
        assert delta.stopped_at_code == 5  # codes 3,4,5 empty -> stop
        assert delta.ok

    def test_rerun_reuses_cache_and_is_idempotent(self, db, tmp_path):
        fetch, _ = _world()
        _bootstrap(db, tmp_path, fetch)
        fetch2, _ = _world()
        delta2 = _bootstrap(db, tmp_path, fetch2)
        repo = Repository(db)
        assert delta2.headers_reused == 2 and delta2.headers_fetched == 0
        assert delta2.inserted_final == 0 and delta2.upserted_scheduled == 0
        assert delta2.team_stats_inserted == 0
        assert repo.get_game("E2026-0001").home_score == 95

    def test_stale_cached_header_is_refetched(self, db, tmp_path):
        # a cached score-less header whose kickoff passed must be
        # refetched: the live endpoint publishes scores only after the
        # game, so trusting the cache would freeze the row SCHEDULED
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        (raw_dir / "euroleague_E2026_raw.json").write_text(json.dumps({
            "dataset": "euroleague-live-v1", "season_code": "E2026",
            "headers": {"1": _header(score_a=None, score_b=None)},
            "boxscores": {}, "empty_game_codes": []}), encoding="utf-8")
        fetch, calls = _world()  # scored header + boxscore
        _bootstrap(db, tmp_path, fetch)
        game = Repository(db).get_game("E2026-0001")
        assert game is not None and game.status is GameStatus.FINAL
        assert (game.home_score, game.away_score) == (95, 88)
        assert any("Header?gameCode=1&" in url for url in calls)

    def test_past_scoreless_header_is_postponed(self, db, tmp_path):
        fetch, _ = _world(headers={
            "1": _header(date="20/10/2026", score_a=None, score_b=None),
            "2": _header(date="28/10/2026"),
        })
        # code 1 cached-and-scoreless in the FRESH run is fetched, then
        # re-fetched (stale rule); the response stays score-less
        delta = _bootstrap(db, tmp_path, fetch)
        game = Repository(db).get_game("E2026-0001")
        assert game is not None and game.status is GameStatus.POSTPONED
        assert delta.upserted_postponed == 1

    def test_unknown_phase_fails_own_row_only(self, db, tmp_path):
        fetch, _ = _world(headers={
            "1": _header(score_a="95", score_b="88"),
            "2": _header(date="28/10/2026", phase="MID-SEASON CUP"),
        })
        delta = _bootstrap(db, tmp_path, fetch)
        assert delta.ok is False
        assert delta.errors[0]["source_id"] == "2"
        assert Repository(db).get_game("E2026-0001") is not None

    def test_raw_bundle_written_and_replayable(self, db, tmp_path):
        fetch, _ = _world()
        _bootstrap(db, tmp_path, fetch)
        bundle = json.loads(
            (tmp_path / "raw" / "euroleague_E2026_raw.json").read_text(
                encoding="utf-8"))
        assert bundle["dataset"] == "euroleague-live-v1"
        assert bundle["n_headers"] == 2 and bundle["n_boxscores"] == 1
        assert (tmp_path / "raw" / "euroleague_E2026_raw.json.manifest.json"
                ).exists()

    def test_existing_scheduled_row_is_finalized(self, db, tmp_path):
        # a schedule row landed before kickoff, then the game was played:
        # the later bootstrap refetches the stale header and flips the
        # row to FINAL with scores
        fetch, _ = _world(headers={
            "1": _header(date="25/10/2026"),  # first seen as future
            "2": _header(date="28/10/2026"),
        })
        _bootstrap(db, tmp_path, fetch)
        assert Repository(db).get_game("E2026-0001").status == (
            GameStatus.SCHEDULED)

        later = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        fetch2, _ = _world()  # now with scores + boxscore
        delta2 = _bootstrap(db, tmp_path, fetch2, now=later)
        game = Repository(db).get_game("E2026-0001")
        assert game.status is GameStatus.FINAL
        assert (game.home_score, game.away_score) == (95, 88)
        assert delta2.finalized == 1
