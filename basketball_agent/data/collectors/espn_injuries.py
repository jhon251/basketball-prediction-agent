"""ESPN NBA injury-report adapter (Phase 12-G).

Source (no auth, probed live 2026-10-25, 26 records):
``https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries``
→ ``injuries: [{id, displayName (team), injuries: [{status, date,
athlete: {displayName, links[…/id/<espn-id>/…]}}]}]``.

Design notes:
* ESPN reports injuries per TEAM + PLAYER (no game context).  We attach
  each record to every UPCOMING SCHEDULED NBA game of that team inside
  the refresh window, as a ``PlayerAvailability`` row with
  ``source_time`` = fetch time (< kickoff by construction — the leakage
  rule is structurally satisfied, never assumed).
* Hard mapping rules (DATA_REQUIREMENTS): OUT must be 0.0, AVAILABLE
  would be 1.0 (ESPN never reports AVAILABLE here); every other status
  maps explicitly, unknown statuses fail loudly.
* Player identity: ESPN athlete has no id field — the id is parsed from
  the player-card link.  Cross-source join is by exact case-insensitive
  full name against the ``players`` table (personId space); ambiguous or
  unmatched names are skipped and COUNTED, never guessed.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

INJURIES_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
)
_TIMEOUT = 30
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "basketball-agent/1.0 (+injury collection)"
)

# ESPN team id -> ESPN abbreviation (fetched live 2026-10-25 from the
# teams endpoint; abbreviation canonicalisation reuses ESPN_ABBR_TO_NBA).
ESPN_TEAM_ID_TO_ABBR: dict[str, str] = {
    "1": "ATL", "2": "BOS", "17": "BKN", "30": "CHA", "4": "CHI",
    "5": "CLE", "6": "DAL", "7": "DEN", "8": "DET", "9": "GS",
    "10": "HOU", "11": "IND", "12": "LAC", "13": "LAL", "29": "MEM",
    "14": "MIA", "15": "MIL", "16": "MIN", "3": "NO", "18": "NY",
    "25": "OKC", "19": "ORL", "20": "PHI", "21": "PHX", "22": "POR",
    "23": "SAC", "24": "SA", "28": "TOR", "26": "UTAH", "27": "WSH",
}

# ESPN status -> (PlayerAvailabilityStatus, availability_probability).
# OUT/AVAILABLE are pinned by DATA_REQUIREMENTS (0.0 / 1.0); the rest are
# explicit, documented estimates.
STATUS_MAP: dict[str, tuple[str, float]] = {
    "Out": ("OUT", 0.0),
    "Inactive": ("OUT", 0.0),
    "Suspended": ("SUSPENDED", 0.0),
    "Doubtful": ("DOUBTFUL", 0.25),
    "Questionable": ("QUESTIONABLE", 0.5),
    "Day-To-Day": ("PROBABLE", 0.75),
}


class InjuryFetchError(RuntimeError):
    """Network / HTTP failure fetching the ESPN injury report."""


class UnknownInjuryStatusError(ValueError):
    """ESPN reported a status outside STATUS_MAP (never guessed)."""


class UnknownTeamError(ValueError):
    """ESPN team id/abbreviation outside the 30-franchise map."""


@dataclass
class InjuryRecord:
    team_id: str            # canonical NBA franchise code
    player_name: str        # "First Last"
    espn_player_id: str     # parsed from the player-card link ("" if absent)
    status: str             # PlayerAvailabilityStatus value
    probability: float
    reported_at: datetime


@dataclass
class InjuryRefreshResult:
    records: list[InjuryRecord] = field(default_factory=list)
    availability_written: int = 0
    skipped_unresolved_player: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)


def fetch_injuries(*, timeout: int = _TIMEOUT) -> dict[str, Any]:
    req = urllib.request.Request(INJURIES_URL, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:  # noqa: BLE001 — network boundary
        raise InjuryFetchError(f"{type(exc).__name__}: {exc}") from exc


def load_injuries_file(path: Path | str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _canonical_team(team_id: str) -> str:
    from basketball_agent.data.collectors.nba_scoreboard import (
        ESPN_ABBR_TO_NBA,
    )

    abbr = ESPN_TEAM_ID_TO_ABBR.get(str(team_id))
    if abbr is None:
        raise UnknownTeamError(f"ESPN team id {team_id!r} not in the map")
    code = ESPN_ABBR_TO_NBA.get(abbr)
    if code is None:
        raise UnknownTeamError(f"ESPN abbreviation {abbr!r} not canonical")
    return code


def _parse_espn_player_id(athlete: dict[str, Any]) -> str:
    for link in athlete.get("links", []):
        href = str(link.get("href", ""))
        if "/id/" in href:
            tail = href.split("/id/", 1)[1].split("/", 1)[0]
            if tail.isdigit():
                return tail
    return ""


def parse_injuries(payload: dict[str, Any]) -> list[InjuryRecord]:
    records: list[InjuryRecord] = []
    for group in payload.get("injuries", []):
        team_id = _canonical_team(group.get("id"))
        for inj in group.get("injuries", []):
            status_raw = str(inj.get("status") or "")
            mapped = STATUS_MAP.get(status_raw)
            if mapped is None:
                raise UnknownInjuryStatusError(
                    f"ESPN status {status_raw!r} not in STATUS_MAP "
                    f"(team {team_id})")
            athlete = inj.get("athlete") or {}
            name = str(athlete.get("displayName") or "").strip()
            if not name:
                continue  # nothing to join on; counted upstream by caller
            date_raw = str(inj.get("date") or "")
            reported_at = (
                datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                if date_raw else datetime.now(timezone.utc))
            records.append(InjuryRecord(
                team_id=team_id,
                player_name=name,
                espn_player_id=_parse_espn_player_id(athlete),
                status=mapped[0],
                probability=mapped[1],
                reported_at=reported_at,
            ))
    return records


def resolve_player_ids(
    repo, records: list[InjuryRecord],
) -> tuple[list[tuple[InjuryRecord, str]], int]:
    """Exact case-insensitive full-name join against players.

    Returns (resolved pairs, unresolved count).  Ambiguous names are
    unresolved (never guessed)."""
    by_name: dict[str, list[str]] = {}
    for p in repo.list_players():
        if p.league.value == "NBA":
            by_name.setdefault(p.name.strip().casefold(), []).append(
                p.player_id)
    resolved: list[tuple[InjuryRecord, str]] = []
    unresolved = 0
    for r in records:
        hits = by_name.get(r.player_name.strip().casefold(), [])
        if len(hits) == 1:
            resolved.append((r, hits[0]))
        else:
            unresolved += 1
    return resolved, unresolved


def apply_injuries(
    repo,
    records: list[InjuryRecord],
    *,
    now: datetime | None = None,
    window_days: int = 8,
) -> InjuryRefreshResult:
    """Attach injury records to upcoming SCHEDULED NBA games."""
    from basketball_agent.data.db.repository import Repository
    from basketball_agent.data.schemas.base import GameStatus, League
    from basketball_agent.data.schemas.game import PlayerAvailability

    now = now or datetime.now(timezone.utc)
    result = InjuryRefreshResult(records=records)
    resolved, unresolved = resolve_player_ids(repo, records)
    result.skipped_unresolved_player = unresolved
    if not resolved:
        return result

    repo = repo if isinstance(repo, Repository) else Repository(repo)
    end = now + timedelta(days=window_days)
    upcoming = [
        g for g in repo.list_games(status=GameStatus.SCHEDULED,
                                   league=League.NBA)
        if now <= g.game_date <= end
    ]
    by_team: dict[str, list] = {}
    for g in upcoming:
        by_team.setdefault(g.home_team_id, []).append(g)
        by_team.setdefault(g.away_team_id, []).append(g)

    seen: set[tuple[str, str]] = {
        (a.player_id, a.game_id) for a in repo.list_availability()}
    for record, player_id in resolved:
        for g in by_team.get(record.team_id, []):
            key = (player_id, g.game_id)
            if key in seen:
                result.skipped_existing += 1
                continue
            try:
                repo.insert_availability(PlayerAvailability(
                    player_id=player_id,
                    game_id=g.game_id,
                    status=record.status,  # type: ignore[arg-type]
                    availability_probability=record.probability,
                    source_time=now,
                    source="espn-injuries",
                ))
                seen.add(key)
                result.availability_written += 1
            except Exception as exc:  # noqa: BLE001 — per-row isolation
                if len(result.errors) < 10:
                    result.errors.append(
                        f"{g.game_id}/{player_id}: "
                        f"{type(exc).__name__}: {exc}")
    return result


def refresh_injuries(
    db, *, now: datetime | None = None, window_days: int = 8,
) -> InjuryRefreshResult:
    """Fetch ESPN + apply to upcoming games (one live refresh)."""
    from basketball_agent.data.db.repository import Repository

    repo = Repository(db)
    records = parse_injuries(fetch_injuries())
    return apply_injuries(repo, records, now=now, window_days=window_days)
