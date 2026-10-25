"""Phase 12-D: zero-dependency HTTP JSON API + static front-end server.

Wraps the frozen prediction chain (``PredictionAgent`` → decision/risk/
record/explanation agents) plus the Phase 12 spread layer behind four
JSON routes and one static page:

* ``GET  /api/health``           — liveness + DB counts
* ``GET  /api/teams``            — teams grouped by league (NBA 中文标注)
* ``GET  /api/schedule``         — upcoming SCHEDULED games (?league=&days=
                                   &refresh_nba=1 triggers one ESPN pass)
* ``POST /api/predict``          — the front-end's single question:
                                   who wins / does the home side cover,
                                   with what confidence

The predict request either names a ``game_id`` directly or a
``league + home + away`` matchup (resolved via ``api/matchup``; for NBA
the ESPN refresh can be enabled per request with ``refresh_nba=true``).
``home_line`` is the spread on the HOME side's score: 主队让5.5 ⇒ -5.5.
Every probability still comes from data/formulas/Monte Carlo (spec §2);
this layer only orchestrates and serialises.
"""
from __future__ import annotations

import json
from dataclasses import is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, model_validator

from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent
from basketball_agent.agents.spread_probability import (
    cover_from_elo_normal,
    cover_from_samples,
    fit_margin_sigma,
)
from basketball_agent.api.matchup import (
    CHINESE_ALIASES_BY_LEAGUE,
    TeamResolutionError,
    find_scheduled,
    resolve_team,
)
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus

# Which spread provider is PRIMARY per league — decided by measurement
# (docs/PHASE12_A_SPREAD_API.md §12-H): walk-forward line-probe logloss
# per league.  elo_normal wins clearly in NBA (0.4446 vs 0.4692),
# EuroLeague (0.4269 vs 0.4385), ACB (0.3577 vs 0.3779), CBA (0.3150 vs
# 0.3331) and BSL (0.3824 vs 0.3941); NBL is a statistical tie where
# simulation_samples measured marginally better (0.4972 vs 0.5025 on 163
# games) and keeps the simulation default.
SPREAD_SOURCE_BY_LEAGUE: dict[str, str] = {
    "NBA": "elo_normal",
    "EUROLEAGUE": "elo_normal",
    "ACB": "elo_normal",
    "CBA": "elo_normal",
    "BSL": "elo_normal",
}
_DEFAULT_SPREAD_SOURCE = "simulation_samples"


class ApiError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    game_id: str | None = None
    league: str | None = None
    home: str | None = None
    away: str | None = None
    home_line: float | None = None
    as_of: str | None = None  # ISO-8601; default now UTC
    n_simulations: int | None = None
    family: str | None = None  # elo | strength | simulation
    refresh_nba: bool = False
    spread_source: str | None = None  # override the per-league default

    @model_validator(mode="after")
    def _need_game_or_matchup(self) -> "PredictRequest":
        if self.game_id:
            return self
        missing = [f for f, v in (("league", self.league),
                                  ("home", self.home),
                                  ("away", self.away)) if not v]
        if missing:
            raise ValueError(
                "predict needs game_id, or league+home+away "
                f"(missing: {', '.join(missing)})")
        return self


def _json_default(o: Any):
    if isinstance(o, Enum):
        return o.value
    if is_dataclass(o):
        return {f: getattr(o, f) for f in o.__dataclass_fields__}
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"unserialisable {type(o)!r}")


_CN_BY_TEAM: dict[str, str] = {}
for _table in CHINESE_ALIASES_BY_LEAGUE.values():
    for _cn, _tid in _table.items():
        _CN_BY_TEAM.setdefault(_tid, _cn)


class BasketballApiApp:
    """Route handlers; transport-agnostic so tests can call them directly."""

    def __init__(self, db, *, state_dir: Path | str,
                 n_simulations: int = 5000, seed: int = 42,
                 calibration_path: Path | str | None = None) -> None:
        self._db = db
        self._repo = Repository(db)
        self._state_dir = Path(state_dir)
        self._n_simulations = n_simulations
        self._seed = seed
        self._sigma_cache: dict[tuple[str, str], float] = {}
        # Phase 12-F: empirical hit-rate table (confidence-calibrate CLI);
        # absent file -> calibration stays None in responses (opt-in).
        self._calibration_path = (
            Path(calibration_path) if calibration_path is not None
            else self._state_dir.parent / "calibration"
            / "confidence_calibration.json")
        self._calibration_payload: dict | None = None
        self._calibration_mtime: float | None = None

    # ------------------------------------------------------------ helpers
    def _team_label(self, team_id: str) -> dict[str, str]:
        team = self._repo.get_team(team_id)
        name = team.name if team else team_id
        return {"team_id": team_id, "name": name,
                "name_cn": _CN_BY_TEAM.get(team_id, "")}

    def _latest_snapshot_id(self) -> str | None:
        try:
            from basketball_agent.data.snapshots import SnapshotManager  # type: ignore
        except Exception:  # pragma: no cover — optional dependency path
            return None
        return None  # snapshots are optional for the API surface

    def _confidence_calibration(self) -> dict | None:
        """Loaded lazily, refreshed when the file is regenerated."""
        from basketball_agent.evaluation.confidence_calibration import (
            load_payload,
        )

        try:
            mtime = self._calibration_path.stat().st_mtime
        except OSError:
            self._calibration_payload = None
            return None
        if (self._calibration_payload is None
                or mtime != self._calibration_mtime):
            self._calibration_payload = load_payload(self._calibration_path)
            self._calibration_mtime = mtime
        return self._calibration_payload

    def _refresh_nba_injuries_quietly(self) -> dict | None:
        """Best-effort ESPN injury refresh; never blocks the response."""
        try:
            from basketball_agent.data.collectors.espn_injuries import (
                refresh_injuries,
            )
            result = refresh_injuries(self._db)
            if result.availability_written:
                self._repo = Repository(self._db)  # cache invalidated anyway
            return {"written": result.availability_written,
                    "records": len(result.records),
                    "unresolved": result.skipped_unresolved_player}
        except Exception:  # noqa: BLE001 — optional enrichment
            return None

    def _margin_sigma(self, league: str, as_of: datetime) -> float:
        key = (league, as_of.date().isoformat())
        if key in self._sigma_cache:
            return self._sigma_cache[key]
        finals = [
            g for g in self._repo.list_games(
                as_of=as_of, status=GameStatus.FINAL)
            if g.league.value == league
            and g.home_score is not None and g.away_score is not None
        ]
        sigma = fit_margin_sigma(
            [g.home_score - g.away_score for g in finals])
        self._sigma_cache[key] = sigma
        return sigma

    # ------------------------------------------------------------- routes
    def health(self) -> dict:
        games = self._repo.list_games()
        by_status: dict[str, int] = {}
        by_league: dict[str, int] = {}
        for g in games:
            by_status[g.status.value] = by_status.get(g.status.value, 0) + 1
            by_league[g.league.value] = by_league.get(g.league.value, 0) + 1
        return {"status": "ok", "now": datetime.now(timezone.utc).isoformat(),
                "games_total": len(games), "games_by_status": by_status,
                "games_by_league": by_league}

    def teams(self) -> dict:
        grouped: dict[str, list[dict]] = {}
        for t in self._repo.list_teams():
            grouped.setdefault(t.league.value, []).append({
                "team_id": t.team_id, "name": t.name,
                "abbreviation": t.abbreviation,
                "name_cn": _CN_BY_TEAM.get(t.team_id, ""),
            })
        for v in grouped.values():
            v.sort(key=lambda x: x["name"])
        return {"leagues": grouped}

    def schedule(self, league: str | None = None, *, days: int = 7,
                 refresh_nba: bool = False) -> dict:
        if refresh_nba and (league is None or league.upper() == "NBA"):
            from basketball_agent.collection.nba_fixtures import (
                refresh_nba_upcoming,
            )
            delta = refresh_nba_upcoming(self._db, self._state_dir)
            self._repo = Repository(self._db)  # row cache invalidated
            injuries = self._refresh_nba_injuries_quietly()
            refresh = {"upserted": delta.upserted,
                       "finalized": delta.finalized,
                       "errors": delta.errors[:5],
                       "injuries": injuries}
        else:
            refresh = None
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)
        out = []
        for g in self._repo.list_games(status=GameStatus.SCHEDULED):
            if league and g.league.value != league.upper():
                continue
            if not (now <= g.game_date <= end):
                continue
            out.append({
                "game_id": g.game_id, "league": g.league.value,
                "season": g.season, "kickoff": g.game_date.isoformat(),
                "competition": g.competition.value if g.competition else None,
                "home": self._team_label(g.home_team_id),
                "away": self._team_label(g.away_team_id),
            })
        out.sort(key=lambda x: x["kickoff"])
        return {"window_days": days, "refresh": refresh, "games": out}

    def predict(self, req: PredictRequest) -> dict:
        as_of = (datetime.fromisoformat(req.as_of).astimezone(timezone.utc)
                 if req.as_of else datetime.now(timezone.utc))
        game_id = req.game_id
        if not game_id:
            league = (req.league or "").upper()
            home_id = resolve_team(self._repo, league, req.home or "")
            away_id = resolve_team(self._repo, league, req.away or "")
            game = find_scheduled(self._repo, league, home_id, away_id,
                                  now=as_of)
            if game is None and league == "NBA":
                if not req.refresh_nba:
                    raise ApiError(
                        f"no upcoming {home_id} vs {away_id} game found "
                        "within 14 days; retry with refresh_nba=true or "
                        "call /api/schedule?refresh_nba=1", status=404)
                from basketball_agent.collection.nba_fixtures import (
                    refresh_nba_upcoming,
                )
                delta = refresh_nba_upcoming(self._db, self._state_dir)
                self._repo = Repository(self._db)
                if delta.errors:
                    raise ApiError(
                        f"NBA refresh had errors: {delta.errors[:3]}",
                        status=502)
                self._refresh_nba_injuries_quietly()
                game = find_scheduled(self._repo, league, home_id, away_id,
                                      now=as_of)
            if game is None:
                raise ApiError(
                    f"no upcoming {league} game {away_id} @ {home_id} "
                    "within 14 days", status=404)
            game_id = game.game_id
        game = self._repo.get_game(game_id)
        if game is None:
            raise ApiError(f"unknown game_id {game_id!r}", status=404)

        agent = PredictionAgent(
            self._repo,
            n_simulations=req.n_simulations or self._n_simulations,
            seed=self._seed, family=req.family)
        prediction = agent.predict(game_id, as_of=as_of)

        # decision chain (risk + explanation), same as the production loop
        from basketball_agent.agents.decision import DecisionAgent
        from basketball_agent.agents.decision_record import (
            DecisionRecordAgent,
        )
        from basketball_agent.agents.explanation import ExplanationAgent

        decision = DecisionAgent(agent).from_prediction(prediction)
        risk = RiskAnalysisAgent().analyze(decision)
        record = DecisionRecordAgent().build(decision, risk)
        explanation = ExplanationAgent().explain(record)

        league = game.league.value
        # shared inputs for the spread + margin-range layers
        sigma = self._margin_sigma(league, as_of)
        margins = [run.margin for run in prediction.monte_carlo.runs]
        p_elo = prediction.features.elo_win_prob_home
        default_source = SPREAD_SOURCE_BY_LEAGUE.get(
            league, _DEFAULT_SPREAD_SOURCE)

        # Phase 12-I: 分差区间 buckets (主胜/客胜 1-5…20+), both providers
        from basketball_agent.agents.spread_probability import (
            elo_expected_margin, margin_ranges_from_normal,
            margin_ranges_from_samples,
        )

        sim_ranges = (margin_ranges_from_samples(margins)
                      if margins else None)
        elo_ranges = (margin_ranges_from_normal(
            elo_expected_margin(p_elo, sigma), sigma)
            if p_elo is not None else None)
        base_ranges = sim_ranges or elo_ranges or []
        margin_ranges = {
            "default_source": (default_source if elo_ranges
                               else "simulation_samples"),
            "sigma": sigma,
            "buckets": [
                {"code": b["code"], "side": b["side"], "range": b["range"],
                 "simulation_samples": (s["probability"] if sim_ranges
                                        else None),
                 "elo_normal": (e["probability"] if elo_ranges else None)}
                for b, s, e in zip(
                    base_ranges, sim_ranges or base_ranges,
                    elo_ranges or base_ranges)
            ],
        }

        spread = None
        if req.home_line is not None:
            line = float(req.home_line)
            source = (req.spread_source or default_source)
            if source not in ("elo_normal", "simulation_samples"):
                raise ApiError(
                    f"spread_source must be elo_normal|simulation_samples, "
                    f"got {source!r}")
            sim_cover = (cover_from_samples(margins, line)
                         if margins else None)
            elo_cover = cover_from_elo_normal(p_elo, sigma, line)
            spread = {
                "home_line": line,
                "default_source": source,
                "sigma": sigma,
                "simulation_samples": (
                    {"home_cover": sim_cover.home_cover,
                     "push": sim_cover.push,
                     "away_cover": sim_cover.away_cover}
                    if sim_cover else None),
                "elo_normal": {
                    "home_cover": elo_cover.home_cover,
                    "push": elo_cover.push,
                    "away_cover": elo_cover.away_cover,
                },
            }

        # Phase 12-F: empirical hit rate for this probability bucket, when
        # the confidence-calibrate table exists (opt-in; None otherwise).
        from basketball_agent.evaluation.confidence_calibration import lookup

        calibration = lookup(
            self._confidence_calibration(), league,
            prediction.primary_family, prediction.win_probability_home)

        margin_dist = prediction.monte_carlo.margin
        return {
            "game": {
                "game_id": game.game_id, "league": league,
                "season": game.season,
                "kickoff": game.game_date.isoformat(),
                "competition": (game.competition.value
                                if game.competition else None),
                "home": self._team_label(game.home_team_id),
                "away": self._team_label(game.away_team_id),
            },
            "as_of": as_of.isoformat(),
            "model_version": prediction.model_version,
            "snapshot_id": prediction.snapshot_id,
            "win_probability": {
                "home": prediction.win_probability_home,
                "away": prediction.win_probability_away,
                "primary_family": prediction.primary_family,
            },
            "families": {
                "elo": prediction.features.elo_win_prob_home,
                "strength": prediction.params.strength_win_prob_home,
                "simulation": prediction.monte_carlo.home_win_probability,
            },
            "expected": {
                "home_score": prediction.expected_home_score,
                "away_score": prediction.expected_away_score,
                "margin": prediction.expected_margin,
                "total": prediction.expected_total,
            },
            "margin_distribution": {
                "mean": margin_dist.mean, "std": margin_dist.std,
                "p5": margin_dist.p5, "p50": margin_dist.p50,
                "p95": margin_dist.p95,
            },
            "spread": spread,
            "margin_ranges": margin_ranges,
            "risk": {
                "confidence": prediction.risk.confidence_score,
                "flags": [{"code": f.code, "severity": f.severity.value
                           if hasattr(f.severity, "value") else str(f.severity),
                           "message": f.message}
                          for f in prediction.risk.risk_flags],
            },
            "availability": {
                "home_share": prediction.features.home_available_share,
                "away_share": prediction.features.away_available_share,
                "records": [
                    {"player": (self._repo.get_player(a.player_id).name
                                if self._repo.get_player(a.player_id)
                                else a.player_id),
                     "status": a.status.value,
                     "probability": a.availability_probability,
                     "source": a.source}
                    for a in self._repo.list_availability(
                        game_id=game_id, known_at=as_of)
                ],
            },
            "calibration": calibration,
            "explanation": {
                "prediction": explanation.prediction_explanation,
                "risk": explanation.risk_explanation,
                "limitations": list(explanation.limitations),
            },
            "n_simulations": prediction.monte_carlo.n_simulations,
        }


_STATIC_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}


def make_handler(app: BasketballApiApp) -> type[BaseHTTPRequestHandler]:
    static_root = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        server_version = "BasketballAgent/1.0"

        def log_message(self, fmt, *args):  # quiet default access log
            pass

        # ---------------------------------------------------------- output
        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, default=_json_default,
                              ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, path: str) -> None:
            clean = Path(path).name or "index.html"  # no traversal
            if path in ("/", ""):
                clean = "index.html"
            full = (static_root / clean).resolve()
            if not str(full).startswith(str(static_root.resolve())):
                self._send_json({"error": "forbidden"}, status=403)
                return
            if not full.is_file():
                self._send_json({"error": "not found"}, status=404)
                return
            body = full.read_bytes()
            ctype = _STATIC_TYPES.get(full.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ----------------------------------------------------------- routes
        def do_GET(self):  # noqa: N802 — http.server API
            try:
                url = urlparse(self.path)
                if url.path in ("/", "/index.html"):
                    self._send_static("index.html")
                    return
                if url.path == "/api/health":
                    self._send_json(app.health())
                    return
                if url.path == "/api/teams":
                    self._send_json(app.teams())
                    return
                if url.path == "/api/schedule":
                    qs = parse_qs(url.query)
                    league = qs.get("league", [None])[0]
                    days = int(qs.get("days", ["7"])[0])
                    refresh = qs.get("refresh_nba", ["0"])[0] in ("1", "true")
                    self._send_json(
                        app.schedule(league, days=days,
                                     refresh_nba=refresh))
                    return
                self._send_json({"error": f"no route {url.path}"}, status=404)
            except ApiError as exc:
                self._send_json({"error": str(exc)}, status=exc.status)
            except Exception as exc:  # noqa: BLE001 — HTTP boundary
                self._send_json({"error": f"{type(exc).__name__}: {exc}"},
                                status=500)

        def do_POST(self):  # noqa: N802 — http.server API
            try:
                if urlparse(self.path).path != "/api/predict":
                    self._send_json({"error": "no such POST route"},
                                    status=404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ApiError(
                        f"invalid JSON body (must be UTF-8): {exc}") from exc
                try:
                    req = PredictRequest(**payload)
                except Exception as exc:  # noqa: BLE001 — pydantic boundary
                    raise ApiError(f"invalid request: {exc}") from exc
                self._send_json(app.predict(req))
            except ApiError as exc:
                self._send_json({"error": str(exc)}, status=exc.status)
            except Exception as exc:  # noqa: BLE001 — HTTP boundary
                self._send_json({"error": f"{type(exc).__name__}: {exc}"},
                                status=500)

    return Handler


def serve(host: str, port: int, db, *, state_dir: Path | str,
          n_simulations: int = 5000, seed: int = 42) -> None:
    app = BasketballApiApp(db, state_dir=state_dir,
                           n_simulations=n_simulations, seed=seed)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Basketball Agent API serving on http://{host}:{port} "
          f"(static UI at /, JSON at /api/*)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
