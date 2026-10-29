"""EuroLeague season fixtures bootstrap (Phase 12-J).

The E2026 season has no offline bundle yet and no calendar endpoint, so
upcoming games must be enumerated: ``Header?gameCode=<n>&seasonCode=E<yyyy>``
is walked from code 1 until the schedule runs out (consecutive empty
payloads), which is exactly the tools/fetch_euroleague_bundle.py pattern.
Per game:

* header with ``ScoreA/ScoreB`` (played) -> Boxscore fetched too, the
  pair becomes a one-game bundle and goes through the SAME
  ``parse_euroleague_bundle`` validation as a season refresh (score
  cross-check, timezone conversion, alias resolution) -> FINAL row +
  team stats;
* header without scores (future) -> SCHEDULED row built directly; a
  score-less header whose kickoff passed >6h ago maps to POSTPONED,
  the same semantics the archived-bundle parser applies;
* a cached score-less header whose kickoff passed is REFETCHED (scores
  appear on the live endpoint only after the game).

Game ids follow the deep-refresh convention ``E{yyyy}-{code:04d}`` and
the SourceGameLedger records ``source_id=str(code)`` so re-runs are
idempotent and a later full-season refresh reuses the SAME rows.  Raw
headers/boxscores accumulate into ``storage/raw/euroleague_live/
euroleague_<season>_raw.json`` in the canonical bundle format, so the
normal ``validate -> ingest`` path can always replay the season offline.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from basketball_agent.collection.ledger import SourceGameLedger
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.game import Game, TeamGameStats
from basketball_agent.data.schemas.team import Team

SEASON_API_BASE = "https://live.euroleague.net/api"
DATASET_MARKER = "euroleague-live-v1"
DEFAULT_MAX_CODE = 340
DEFAULT_TRAILING_EMPTY_STOP = 3
DEFAULT_PAUSE = 0.4
#: a score-less header within this window after kickoff is "source lag";
#: beyond it the game is POSTPONED (source had its chance)
_KICKOFF_LAG = timedelta(hours=6)


@dataclass
class EuroLeagueBootstrapDelta:
    headers_reused: int = 0
    headers_fetched: int = 0
    upserted_scheduled: int = 0
    upserted_postponed: int = 0
    inserted_final: int = 0
    finalized: int = 0
    team_stats_inserted: int = 0
    boxscores_fetched: int = 0
    stopped_at_code: int | None = None
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _default_fetch(url: str, *, pause: float) -> dict:
    import time

    from basketball_agent.collection.sources import _get_json

    payload = _get_json(url)
    time.sleep(pause)
    return payload


def _is_header_payload(payload) -> bool:
    return (isinstance(payload, dict)
            and bool(payload.get("CodeTeamA") or payload.get("TeamA")))


def _bundle_paths(out_dir: Path, season_code: str) -> tuple[Path, Path]:
    base = out_dir / f"euroleague_{season_code}_raw.json"
    return base, base.with_name(base.name + ".manifest.json")


def _load_existing(out_dir: Path, season_code: str) -> dict:
    path, _ = _bundle_paths(out_dir, season_code)
    if not path.exists():
        return {"headers": {}, "boxscores": {}, "empty_game_codes": []}
    bundle = json.loads(path.read_text(encoding="utf-8"))
    return {
        "headers": dict(bundle.get("headers") or {}),
        "boxscores": dict(bundle.get("boxscores") or {}),
        "empty_game_codes": list(bundle.get("empty_game_codes") or []),
    }


def _write_bundle(out_dir: Path, season_code: str, headers: dict,
                  boxscores: dict, empty_codes: list[int]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path, manifest_path = _bundle_paths(out_dir, season_code)
    bundle = {
        "dataset": DATASET_MARKER,
        "season_code": season_code,
        "source": SEASON_API_BASE,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "n_headers": len(headers),
        "n_boxscores": len(boxscores),
        "empty_game_codes": sorted(empty_codes),
        "headers": {str(k): v for k, v in sorted(headers.items(),
                                                 key=lambda kv: int(kv[0]))},
        "boxscores": {str(k): v for k, v in sorted(boxscores.items(),
                                                   key=lambda kv: int(kv[0]))},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    manifest = {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": len(headers),
        "fetched_at": bundle["fetched_at"],
        "source": SEASON_API_BASE,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2),
                             encoding="utf-8")
    return path


def _kickoff_utc(season_code: str, code: int, header: dict) -> datetime:
    from basketball_agent.data.collectors.euroleague_live import (
        TEAM_TIMEZONES, _parse_local_datetime, resolve_team,
    )

    home_id = resolve_team(header["CodeTeamA"])
    tz_name = TEAM_TIMEZONES.get(home_id.removeprefix("EL-"))
    if tz_name is None:
        raise ValueError(
            f"game {season_code}-{code:04d}: no timezone for {home_id!r}")
    return _parse_local_datetime(header.get("Date"), header.get("Hour"),
                                 tz_name)


def _cached_header_reusable(header: dict, *, now: datetime) -> bool:
    """Cached headers stay when scored (final) or not yet due (future);
    a stale score-less header is refetched because the live endpoint
    only publishes scores after the game."""
    from basketball_agent.data.collectors.euroleague_live import _score_int

    if (_score_int(header.get("ScoreA"), "ScoreA") is not None
            or _score_int(header.get("ScoreB"), "ScoreB") is not None):
        return True
    try:
        return _kickoff_utc("", 0, header) + _KICKOFF_LAG > now
    except Exception:  # noqa: BLE001 — unparseable cache: refetch
        return False


def _header_to_game(season_code: str, code: int, header: dict,
                    status: GameStatus) -> Game:
    """Header -> Game row (SCHEDULED/POSTPONED), same validation as the
    archived-bundle parser."""
    from basketball_agent.data.collectors.euroleague_live import (
        PHASE_TO_COMPETITION, _season_label, resolve_team,
    )

    game_id = f"{season_code}-{code:04d}"
    phase = str(header.get("Phase", "")).strip().upper()
    competition_raw = PHASE_TO_COMPETITION.get(phase)
    if competition_raw is None:
        raise ValueError(
            f"game {game_id}: unknown Header.Phase {header.get('Phase')!r}")
    home_id = resolve_team(header["CodeTeamA"])
    away_id = resolve_team(header["CodeTeamB"])
    if home_id == away_id:
        raise ValueError(f"game {game_id}: TeamA == TeamB ({home_id})")
    return Game(
        game_id=game_id, league=League.EUROLEAGUE,
        season=_season_label(season_code),
        game_date=_kickoff_utc(season_code, code, header),
        home_team_id=home_id, away_team_id=away_id,
        home_score=None, away_score=None, status=status,
        competition=Competition(competition_raw),
    )


def _apply_played(repo: Repository, ledger: SourceGameLedger,
                  delta: EuroLeagueBootstrapDelta, season_code: str,
                  code: int, header: dict, box: dict) -> None:
    """Played game: one-game bundle through the season-refresh parser."""
    from basketball_agent.data.collectors.euroleague_live import (
        parse_euroleague_bundle,
    )

    key = str(code)
    game_id = f"{season_code}-{code:04d}"
    parsed = parse_euroleague_bundle({
        "dataset": DATASET_MARKER,
        "season_code": season_code,
        "headers": {key: header},
        "boxscores": {key: box},
    })
    game_row = next(iter(parsed["games"]))
    existing = repo.get_game(game_id)
    if existing is not None and existing.status is GameStatus.FINAL:
        return  # settled already — idempotent
    for team in parsed["teams"]:
        repo.upsert_team(Team(**team))
    if existing is None:
        repo.insert_game(Game(**game_row))
        ledger.record("EUROLEAGUE", game_row["season"], key, game_id)
        delta.inserted_final += 1
    else:
        repo.update_game_result(
            game_id, home_score=int(game_row["home_score"]),
            away_score=int(game_row["away_score"]))
        delta.finalized += 1
    for ts in parsed["team_stats"]:
        repo.insert_team_stats(TeamGameStats(**ts))
        delta.team_stats_inserted += 1


def bootstrap_euroleague_season(
    db,
    state_dir,
    season_code: str = "E2026",
    *,
    out_dir: Path | str | None = None,
    max_code: int = DEFAULT_MAX_CODE,
    trailing_empty_stop: int = DEFAULT_TRAILING_EMPTY_STOP,
    pause: float = DEFAULT_PAUSE,
    fetch_fn: Callable[[str], dict] | None = None,
    now: datetime | None = None,
) -> EuroLeagueBootstrapDelta:
    """Enumerate Header codes and upsert the season's games.

    ``fetch_fn(url) -> payload`` is injectable for tests; the live path
    reuses the collector ``_get_json`` retry wrapper with a pause between
    requests.  Re-runs reuse every still-valid cached header and skip
    ledger-known rows, so a nightly call only pays for new codes and
    freshly played games.
    """
    from basketball_agent.data.collectors.euroleague_live import _score_int

    out_dir = Path(out_dir) if out_dir is not None else Path(
        "storage/raw/euroleague_live")
    now = now or datetime.now(timezone.utc)
    fetch = fetch_fn or (lambda url: _default_fetch(url, pause=pause))
    repo = Repository(db) if not isinstance(db, Repository) else db
    ledger = SourceGameLedger(state_dir)
    delta = EuroLeagueBootstrapDelta()

    existing = _load_existing(out_dir, season_code)
    headers: dict[str, dict] = existing["headers"]
    boxscores: dict[str, dict] = existing["boxscores"]
    empty_codes: list[int] = list(existing["empty_game_codes"])

    trailing_empty = 0
    for code in range(1, max_code + 1):
        key = str(code)
        cached = headers.get(key)
        if cached is not None and _cached_header_reusable(cached, now=now):
            delta.headers_reused += 1
            continue
        url = (f"{SEASON_API_BASE}/Header?gameCode={code}"
               f"&seasonCode={season_code}")
        try:
            payload = fetch(url)
        except Exception:  # noqa: BLE001 — enumeration boundary
            empty_codes.append(code)
            trailing_empty += 1
            if trailing_empty >= trailing_empty_stop:
                delta.stopped_at_code = code
                break
            continue
        if not _is_header_payload(payload):
            empty_codes.append(code)
            trailing_empty += 1
            if trailing_empty >= trailing_empty_stop:
                delta.stopped_at_code = code
                break
            continue
        trailing_empty = 0
        headers[key] = payload
        delta.headers_fetched += 1

    # Played games need the Boxscore; fetch only what the headers say is
    # played (ScoreA/ScoreB present) and cache into the raw bundle.
    for key, header in sorted(headers.items(), key=lambda kv: int(kv[0])):
        if boxscores.get(key) is not None:
            continue
        if (_score_int(header.get("ScoreA"), "ScoreA") is None
                and _score_int(header.get("ScoreB"), "ScoreB") is None):
            continue
        code = int(key)
        url = (f"{SEASON_API_BASE}/Boxscore?gameCode={code}"
               f"&seasonCode={season_code}")
        try:
            box = fetch(url)
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            delta.errors.append({"stage": "euroleague_boxscore",
                                 "source_id": key,
                                 "error": f"{type(exc).__name__}: {exc}"})
            continue
        if isinstance(box, dict) and box.get("Stats"):
            boxscores[key] = box
            delta.boxscores_fetched += 1

    for key, header in sorted(headers.items(), key=lambda kv: int(kv[0])):
        code = int(key)
        try:
            box = boxscores.get(key)
            if box is not None:
                _apply_played(repo, ledger, delta, season_code, code,
                              header, box)
                continue
            if ledger.has("EUROLEAGUE", key):
                continue  # known non-played row — idempotent
            try:
                kickoff = _kickoff_utc(season_code, code, header)
            except Exception:
                kickoff = None
            if kickoff is not None and kickoff + _KICKOFF_LAG <= now:
                status, counter = GameStatus.POSTPONED, "upserted_postponed"
            else:
                status, counter = GameStatus.SCHEDULED, "upserted_scheduled"
            game = _header_to_game(season_code, code, header, status)
            repo.insert_game(game)
            ledger.record("EUROLEAGUE", game.season, key, game.game_id)
            setattr(delta, counter, getattr(delta, counter) + 1)
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            delta.errors.append({"stage": "euroleague_fixtures",
                                 "source_id": key,
                                 "error": f"{type(exc).__name__}: {exc}"})

    _write_bundle(out_dir, season_code, headers, boxscores, empty_codes)
    return delta
