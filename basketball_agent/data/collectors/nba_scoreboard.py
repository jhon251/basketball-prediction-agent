"""NBA upcoming-fixtures adapter — ESPN public scoreboard API (Phase 12-B).

Source: ``https://site.api.espn.com/apis/site/v2/sports/basketball/nba/
scoreboard`` (``?dates=YYYYMMDD`` for a specific day).  No auth required;
probed live 2026-10-23 from this machine (12 events).

Purpose: give the production loop / API NBA rows in the SAME row format
the BSL/NBL/CBA upsert path already consumes, so upcoming NBA games can
enter the core as SCHEDULED rows and be predicted before tip-off.  Final
scores from the same source later flip those rows to FINAL (via
``Repository.update_game_result``), which is what prediction settlement
requires — the parquet season-refresh stays the deep-history path.

ESPN abbreviation quirks vs the canonical franchise codes
(``nba_teams.NBA_TEAM_IDS``), verified against the live API for all 30
teams: GS→GSW, NO→NOP, NY→NYK, SA→SAS, UTAH→UTA, WSH→WAS; all others
match verbatim.  Any unknown abbreviation fails loudly
(``UnknownTeamError``) — never guessed (spec: fail, never warn).
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
_TIMEOUT = 30
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "basketball-agent/1.0 (+schedule collection)"
)

# ESPN abbreviation -> canonical NBA franchise code (see module docstring).
ESPN_ABBR_TO_NBA: dict[str, str] = {
    "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "CHA": "CHA", "CHI": "CHI",
    "CLE": "CLE", "DAL": "DAL", "DEN": "DEN", "DET": "DET", "GS": "GSW",
    "HOU": "HOU", "IND": "IND", "LAC": "LAC", "LAL": "LAL", "MEM": "MEM",
    "MIA": "MIA", "MIL": "MIL", "MIN": "MIN", "NO": "NOP", "NY": "NYK",
    "OKC": "OKC", "ORL": "ORL", "PHI": "PHI", "PHX": "PHX", "POR": "POR",
    "SAC": "SAC", "SA": "SAS", "TOR": "TOR", "UTAH": "UTA", "WSH": "WAS",
}


class UnknownTeamError(ValueError):
    """ESPN reported a team abbreviation outside the 30-franchise map."""


class ScoreboardFetchError(RuntimeError):
    """Network / HTTP failure while fetching the ESPN scoreboard."""


def fetch_scoreboard(
    dates: Sequence[str | date_cls] | None = None,
    *,
    timeout: int = _TIMEOUT,
) -> dict[str, Any]:
    """Fetch one or more scoreboard days, merging events by ESPN event id.

    ``dates`` entries are ``YYYYMMDD`` strings or ``date`` objects;
    ``None`` fetches today (ESPN server's notion of "today").
    """
    day_strs = [d.strftime("%Y%m%d") if isinstance(d, date_cls) else str(d)
                for d in (dates or [None])]  # type: ignore[list-item]
    merged: dict[str, Any] = {"leagues": [], "events": []}
    seen: set[str] = set()
    for day in day_strs:
        url = SCOREBOARD_URL + (f"?dates={day}" if day else "")
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
        except Exception as exc:  # noqa: BLE001 — network boundary
            raise ScoreboardFetchError(f"{type(exc).__name__}: {exc}") from exc
        for ev in payload.get("events", []):
            eid = str(ev.get("id"))
            if eid in seen:
                continue
            seen.add(eid)
            merged["events"].append(ev)
    if not merged["leagues"] and payload.get("leagues"):
        merged["leagues"] = payload["leagues"]
    return merged


def load_scoreboard_file(path: Path | str) -> dict[str, Any]:
    """Offline/test path: read a saved scoreboard JSON payload."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _espn_state(event: dict[str, Any]) -> str:
    return str(event.get("status", {}).get("type", {}).get("state", ""))


def _competitors(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for comp in event.get("competitions", []):
        for c in comp.get("competitors", []):
            side = str(c.get("homeAway", "")).lower()
            if side in ("home", "away"):
                out[side] = c
    return out


def _canonical(abbr: str) -> str:
    code = ESPN_ABBR_TO_NBA.get(abbr)
    if code is None:
        raise UnknownTeamError(
            f"ESPN abbreviation {abbr!r} not in the 30-franchise map")
    return code


def parse_scoreboard_events(
    events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """ESPN events -> production-loop row format.

    Row contract (same keys the BSL/NBL/CBA upsert path consumes):
    ``source_id`` (ESPN event id), ``date`` (YYYY-MM-DD, UTC wall),
    ``match_time`` (HH:MM, UTC wall — NBA kickoffs are stored in UTC),
    ``home_id`` / ``away_id`` (canonical franchise codes), ``status_raw``
    ("0" pre / "1" final), ``home_score`` / ``away_score`` ("" until
    final).  In-progress events are dropped entirely (not predictable,
    not settleable from this pass).
    """
    rows: list[dict[str, Any]] = []
    for ev in events:
        state = _espn_state(ev)
        if state == "in":
            continue
        comps = _competitors(ev)
        if "home" not in comps or "away" not in comps:
            continue
        home = comps["home"]
        away = comps["away"]
        raw_dt = str(ev.get("date", ""))
        if not raw_dt:
            continue
        dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        dt_utc = dt.astimezone(timezone.utc)
        home_abbr = str(home.get("team", {}).get("abbreviation", ""))
        away_abbr = str(away.get("team", {}).get("abbreviation", ""))
        season = ev.get("season", {})
        season_year = season.get("year")
        season_type = season.get("type")
        # ESPN's year is the year the season ENDS (2026-27 -> year 2027);
        # preseason (type 1) is dropped — exhibitions never enter history.
        season_label = (
            f"{int(season_year) - 1}-{int(season_year) % 100:02d}"
            if isinstance(season_year, int)
            else ""
        )
        if season_type == 1:
            continue
        competition = {2: "REGULAR_SEASON", 3: "PLAYOFF",
                       4: "PLAY_IN"}.get(season_type, "OTHER")
        row = {
            "source_id": str(ev.get("id")),
            "date": dt_utc.strftime("%Y-%m-%d"),
            "match_time": dt_utc.strftime("%H:%M"),
            "home_id": _canonical(home_abbr),
            "away_id": _canonical(away_abbr),
            "status_raw": "1" if state == "post" else "0",
            "home_score": "",
            "away_score": "",
            "season": season_label,
            "competition": competition,
            "espn_state": state,
        }
        if state == "post":
            row["home_score"] = str(home.get("score", "") or "")
            row["away_score"] = str(away.get("score", "") or "")
            if not row["home_score"] or not row["away_score"]:
                continue  # final without a score is unusable for settlement
        rows.append(row)
    return rows


def upcoming_days(n_days: int = 8, *, today: date_cls | None = None) -> list[date_cls]:
    """The default fetch window: today .. today + n_days - 1."""
    start = today or datetime.now(timezone.utc).date()
    return [start + timedelta(days=i) for i in range(n_days)]


def fetch_upcoming_rows(
    n_days: int = 8, *, today: date_cls | None = None,
) -> list[dict[str, Any]]:
    """Convenience: fetch + parse the next ``n_days`` of NBA fixtures."""
    payload = fetch_scoreboard(upcoming_days(n_days, today=today))
    return parse_scoreboard_events(payload.get("events", []))
