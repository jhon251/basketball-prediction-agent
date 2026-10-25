"""NBA player game-log adapter — llimllib playerlog_<season>.parquet
(Phase 12-G: light up the injury/availability feature with real minute
weights).

Purpose: populate ``players`` + ``player_game_stats`` so the frozen
feature pipeline's ``_player_minutes`` (avg minutes, last 5 games) and
``available_minutes_share`` work on real data.  Without player rows the
share is structurally 1.0 and the ``injury_uncertainty`` risk flag can
never fire (docs/PHASE12_A_SPREAD_API.md §12-F 盲区).

Source layout (verified 2026-10-25):
* ``playerlog_<end-year>.parquet`` — one row per player per game:
  ``gameId`` (raw NBA game id, matches the games table 1:1),
  ``personId``, ``firstName``/``familyName``, ``teamTricode`` (30
  franchise codes + historical aliases), ``minutes`` "MM:SS", and box
  totals (fieldGoalsAttempted, threePointersAttempted, freeThrowsAttempted,
  reboundsTotal, assists, turnovers, steals, blocks, foulsPersonal, points).
* ``players_<end-year>.parquet`` — roster: ``player_id`` (same personId
  space), ``player_name``.

Rules:
* rows whose ``gameId`` is not a FINAL game in the DB are skipped and
  counted (never guessed in — FK is the truth);
* players come from the roster first, then any personId appearing in the
  logs that the roster lacks (name = First Last);
* zero/NaN-minute rows (DNP) are skipped and counted — they carry no
  weight information;
* everything fails loudly (one bad row aborts the batch parse; per-row
  insert isolation is the caller's choice).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from basketball_agent.data.collectors.nba_teams import canonical_team_id


class PlayerlogError(ValueError):
    """Malformed player-log row or file."""


@dataclass
class PlayerlogBundle:
    players: list[dict] = field(default_factory=list)   # {player_id, name, team_id}
    stats: list[dict] = field(default_factory=list)     # PlayerGameStats-shaped
    skipped_unknown_game: int = 0
    skipped_no_minutes: int = 0


def _minutes_to_float(raw: object) -> float:
    """'37:25' -> 37 + 25/60 (rounded to 1 decimal, spec granularity)."""
    s = str(raw or "").strip()
    if not s or s.lower() == "nan":
        return 0.0
    parts = s.split(":")
    try:
        if len(parts) == 2:
            m, sec = int(parts[0]), int(parts[1])
        elif len(parts) == 1:
            m, sec = float(parts[0]), 0
        else:
            raise PlayerlogError(f"unparseable minutes {raw!r}")
    except ValueError as exc:
        raise PlayerlogError(f"unparseable minutes {raw!r}") from exc
    if m < 0 or not 0 <= sec <= 60:
        raise PlayerlogError(f"minutes out of range {raw!r}")
    if sec == 60:  # upstream rounding artifact: "30:60" == 31:00
        m, sec = m + 1, 0
    return round(m + sec / 60.0, 1)


def _to_int(raw: object, *, field_name: str) -> int:
    import math

    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError) as exc:
        raise PlayerlogError(
            f"{field_name}={raw!r} is not numeric") from exc


def parse_playerlog(rows: list[dict], *, games_by_id: dict[str, tuple[str, str]]) -> PlayerlogBundle:
    """Normalized rows -> players + PlayerGameStats-shaped dicts.

    ``games_by_id``: game_id -> (home_team_id, away_team_id) for FINAL
    NBA games (drives ``team_id`` canonicalisation and ``is_home``).
    """
    bundle = PlayerlogBundle()
    seen_players: dict[str, dict] = {}
    for r in rows:
        # parquet stores NBA game ids as int -> leading zeros lost; the
        # games table keeps the canonical 10-char "0022500088" form
        game_id = (str(int(r["gameId"])).zfill(10)
                   if r.get("gameId") is not None else "")
        if game_id not in games_by_id:
            bundle.skipped_unknown_game += 1
            continue
        tricode = str(r.get("teamTricode") or "").strip()
        team_id = canonical_team_id(tricode) if tricode else None
        if team_id is None:
            raise PlayerlogError(
                f"game {game_id}: unknown teamTricode {tricode!r}")
        minutes = _minutes_to_float(r.get("minutes"))
        if minutes <= 0:
            bundle.skipped_no_minutes += 1
            continue
        person = str(int(r["personId"])) if r.get("personId") is not None else ""
        if not person:
            raise PlayerlogError(f"game {game_id}: row without personId")
        first = str(r.get("firstName") or "").strip()
        last = str(r.get("familyName") or "").strip()
        name = f"{first} {last}".strip() or f"player-{person}"
        if person not in seen_players:
            seen_players[person] = {
                "player_id": person, "name": name, "team_id": team_id}
        bundle.stats.append({
            "game_id": game_id,
            "player_id": person,
            "team_id": team_id,
            "minutes": minutes,
            "points": _to_int(r.get("points"), field_name="points"),
            "fga": _to_int(r.get("fieldGoalsAttempted"), field_name="fga"),
            "fg3a": _to_int(r.get("threePointersAttempted"), field_name="fg3a"),
            "fta": _to_int(r.get("freeThrowsAttempted"), field_name="fta"),
            "rebounds": _to_int(r.get("reboundsTotal"), field_name="rebounds"),
            "assists": _to_int(r.get("assists"), field_name="assists"),
            "turnovers": _to_int(r.get("turnovers"), field_name="turnovers"),
            "steals": _to_int(r.get("steals"), field_name="steals"),
            "blocks": _to_int(r.get("blocks"), field_name="blocks"),
            "fouls": _to_int(r.get("foulsPersonal"), field_name="fouls"),
        })
    bundle.players = list(seen_players.values())
    return bundle


def parse_roster(rows: list[dict]) -> list[dict]:
    """players_<year>.parquet rows -> player dicts (roster priority)."""
    out = []
    for r in rows:
        pid = r.get("player_id")
        if pid is None or pid == "":
            continue
        name = str(r.get("player_name") or "").strip()
        team = ""
        abbr = str(r.get("team_abbreviation") or "").strip().upper()
        if abbr:
            try:
                team = canonical_team_id(abbr)
            except Exception:  # noqa: BLE001 — roster rows may lag moves
                team = ""
        out.append({"player_id": str(int(pid)) if isinstance(pid, float)
                    else str(pid),
                    "name": name or None, "team_id": team or None})
    return out


def load_parquet_rows(path: Path | str) -> list[dict]:
    import pandas as pd

    df = pd.read_parquet(path)
    return df.to_dict("records")
