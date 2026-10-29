"""Phase 12-D: API layer tests (app handlers + one HTTP round-trip).

Uses the shared fixture DB (conftest sample dataset) — no network; the
NBA ESPN refresh path is NOT exercised here (covered by the adapter and
fixtures-pass unit tests).
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.api.server import (
    ApiError,
    BasketballApiApp,
    PredictRequest,
    make_handler,
)
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.team import Team

UTC = timezone.utc


@pytest.fixture
def app(ingested_db, tmp_path):
    from basketball_agent.config.settings import get_settings  # noqa: F401

    return BasketballApiApp(ingested_db, state_dir=tmp_path / "collector",
                            n_simulations=60, seed=7)


@pytest.fixture(autouse=True)
def _future_target(ingested_db):
    """One SCHEDULED game a few days out (find_scheduled uses real now,
    so the conftest 2024-dated targets would be missed)."""
    from basketball_agent.data.schemas.base import Competition
    from basketball_agent.data.schemas.game import Game

    repo = Repository(ingested_db)
    kickoff = datetime.now(UTC) + timedelta(days=3)
    repo.insert_game(Game(
        game_id="FUTURE-0001", league=League.NBA, season="2099-00",
        game_date=kickoff,
        home_team_id="LAL", away_team_id="BOS",
        home_score=None, away_score=None,
        status=GameStatus.SCHEDULED,
        competition=Competition.REGULAR_SEASON,
    ))
    return Repository(ingested_db).get_game("FUTURE-0001")


class TestHealthTeamsSchedule:
    def test_health_counts(self, app):
        out = app.health()
        assert out["status"] == "ok"
        assert out["games_total"] > 0
        assert "SCHEDULED" in out["games_by_status"]

    def test_teams_grouped_with_chinese(self, app):
        out = app.teams()
        assert "NBA" in out["leagues"]
        lal = [t for t in out["leagues"]["NBA"] if t["team_id"] == "LAL"]
        assert lal and lal[0]["name_cn"] == "湖人"

    def test_schedule_lists_future_games(self, app):
        out = app.schedule(days=30)
        ids = [g["game_id"] for g in out["games"]]
        assert "FUTURE-0001" in ids


class TestPredict:
    def test_predict_by_game_id_full_payload(self, app, _future_target):
        out = app.predict(PredictRequest(
            game_id=_future_target.game_id, home_line=-3.5))
        assert out["game"]["game_id"] == _future_target.game_id
        assert 0.0 <= out["win_probability"]["home"] <= 1.0
        assert set(out["families"]) == {"elo", "strength", "simulation"}
        assert out["expected"]["total"] > 0
        assert out["spread"]["home_line"] == -3.5
        for src in ("simulation_samples", "elo_normal"):
            block = out["spread"][src]
            if block is None:
                continue
            total = block["home_cover"] + block["push"] + block["away_cover"]
            assert abs(total - 1.0) < 1e-6
        assert 0 <= out["risk"]["confidence"] <= 100
        assert isinstance(out["explanation"]["limitations"], list)

    def test_predict_by_matchup(self, app, _future_target):
        out = app.predict(PredictRequest(
            league=_future_target.league.value,
            home=_future_target.home_team_id,
            away=_future_target.away_team_id))
        assert out["game"]["game_id"] == _future_target.game_id

    def test_unknown_game_404(self, app):
        with pytest.raises(ApiError) as ei:
            app.predict(PredictRequest(game_id="NOPE-1"))
        assert ei.value.status == 404

    def test_matchup_not_found_404(self, app, _future_target):
        # GSW-BOS has no scheduled game (the fixture schedules LAL-BOS)
        with pytest.raises(ApiError) as ei:
            app.predict(PredictRequest(league="NBA", home="GSW",
                                       away="BOS"))
        assert ei.value.status == 404

    def test_request_validation_requires_matchup_fields(self):
        with pytest.raises(Exception):
            PredictRequest(league="NBA", home="LAL")

    def test_bad_spread_source_rejected(self, app, _future_target):
        with pytest.raises(ApiError):
            app.predict(PredictRequest(game_id=_future_target.game_id,
                                       home_line=-3.5,
                                       spread_source="coin_flip"))

    def test_quarter_line_rejected(self, app, _future_target):
        with pytest.raises(Exception):
            app.predict(PredictRequest(game_id=_future_target.game_id,
                                       home_line=-3.7))


class TestHttpRoundTrip:
    def test_endpoints_over_http(self, app):
        from http.server import ThreadingHTTPServer

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://127.0.0.1:{port}"

            def get(path):
                with urllib.request.urlopen(base + path, timeout=10) as r:
                    return json.load(r)

            health = get("/api/health")
            assert health["status"] == "ok"
            teams = get("/api/teams")
            assert "NBA" in teams["leagues"]
            index = urllib.request.urlopen(base + "/", timeout=10).read()
            assert "篮球赛前预测" in index.decode("utf-8")

            req = urllib.request.Request(
                base + "/api/predict",
                data=json.dumps({"game_id": "zzz"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                raised = False
            except urllib.error.HTTPError as exc:
                raised = exc.code == 404
            assert raised, "unknown game_id must 404"
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestMarginRanges:
    def test_block_shape_and_sums(self, app, _future_target):
        out = app.predict(PredictRequest(game_id=_future_target.game_id))
        block = out["margin_ranges"]
        assert len(block["buckets"]) == 13
        assert [b["code"] for b in block["buckets"]][0] == "away_26_plus"
        assert [b["code"] for b in block["buckets"]][-1] == "home_26_plus"
        tie = next(b for b in block["buckets"] if b["code"] == "tie")
        assert tie["side"] == "tie"
        for src in ("simulation_samples", "elo_normal"):
            probs = [b[src] for b in block["buckets"] if b[src] is not None]
            assert probs, f"{src} missing entirely"
            assert abs(sum(probs) - 1.0) < 1e-6
        assert block["default_source"] in ("elo_normal",
                                           "simulation_samples")

    def test_home_win_consistent_with_primary(self, app, _future_target):
        # sum of home buckets ≈ primary home win probability (samples)
        out = app.predict(PredictRequest(game_id=_future_target.game_id))
        block = out["margin_ranges"]
        home_p = sum(b["simulation_samples"] for b in block["buckets"]
                     if b["side"] == "home" and b["simulation_samples"])
        sim_p = out["families"]["simulation"]
        assert home_p == pytest.approx(sim_p, abs=1e-9)


class TestTotals:
    """Phase 12-K: 大小球 over/under block."""

    def test_block_shape_and_sums(self, app, _future_target):
        out = app.predict(PredictRequest(
            game_id=_future_target.game_id, total_line=190.5))
        block = out["totals"]
        assert block["total_line"] == 190.5
        assert block["default_source"] == "simulation_samples"
        assert block["sigma"] > 0
        for src in ("simulation_samples", "normal"):
            probs = block[src]
            assert probs is not None
            assert abs(sum(probs[k] for k in ("over", "push", "under"))
                       - 1.0) < 1e-9
        # half-point line vs integer simulated totals: no push anywhere
        assert block["simulation_samples"]["push"] == 0.0
        assert block["normal"]["push"] == 0.0

    def test_integer_line_push_mass(self, app, _future_target):
        out = app.predict(PredictRequest(
            game_id=_future_target.game_id, total_line=190))
        block = out["totals"]
        assert block["normal"]["push"] > 0.0
        assert abs(sum(block["normal"][k]
                       for k in ("over", "push", "under")) - 1.0) < 1e-9

    def test_absent_without_line(self, app, _future_target):
        out = app.predict(PredictRequest(game_id=_future_target.game_id))
        assert out["totals"] is None

    def test_quarter_line_rejected(self, app, _future_target):
        with pytest.raises(Exception):
            app.predict(PredictRequest(
                game_id=_future_target.game_id, total_line=190.3))

    def test_line_errors_map_to_400(self, app, _future_target):
        # client-input line errors are ApiError(400) at the app layer —
        # the HTTP handler turns ApiError into the status code, so these
        # can never surface as 500s
        with pytest.raises(ApiError) as ei:
            app.predict(PredictRequest(
                game_id=_future_target.game_id, total_line=190.3))
        assert ei.value.status == 400
        with pytest.raises(ApiError) as ei2:
            app.predict(PredictRequest(
                game_id=_future_target.game_id, home_line=-3.7))
        assert ei2.value.status == 400
