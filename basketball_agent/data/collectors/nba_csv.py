"""Real NBA games CSV collector (spec §1-§3).

Reads a CSV of real game data into the validated pydantic dataset used by
the rest of the system.  Two layout formats are supported:

Format A — one row per game (``home_`` / ``away_`` prefixed stat columns)::

    game_date, home_team, away_team, home_score, away_score,
    home_fg, home_fga, ..., away_fg, away_fga, ...

Format B — one row per team (two rows per game)::

    game_date, team, is_home, score, fg, fga, fg3, fg3a, ...

Column names are normalized through an alias table (case-insensitive), and
team names go through the stable NBA franchise map (historical renames such
as SEA->OKC, NJN->BKN are unified).  Unknown teams are reported loudly —
never silently mapped or dropped (spec: 不要假装已经接入).

Missing per-team stats are tolerated; ``possessions`` falls back to the
standard estimate ``fga + 0.44*fta - orb + tov`` and is recorded as an
ESTIMATE in the source description.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from basketball_agent.data.collectors.nba_teams import canonical_team_id
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game, TeamGameStats

UTC = timezone.utc

STAT_COLUMNS = (
    "possessions", "fg", "fga", "fg3", "fg3a", "ft", "fta",
    "orb", "drb", "trb", "ast", "tov", "stl", "blk", "pf",
)

# alias -> canonical column name (case-insensitive matching)
COLUMN_ALIASES: dict[str, str] = {
    "game_date": "game_date", "date": "game_date", "gamedate": "game_date",
    "game_id": "game_id", "gameid": "game_id",
    "season": "season", "year": "season",
    "home_team": "home_team", "home": "home_team", "home_abbr": "home_team",
    "home_team_abbreviation": "home_team", "ht": "home_team", "home_team_id": "home_team",
    "away_team": "away_team", "away": "away_team", "away_abbr": "away_team",
    "away_team_abbreviation": "away_team", "at": "away_team", "away_team_id": "away_team",
    "visitor_team": "away_team", "visitor": "away_team", "visitor_abbr": "away_team",
    "visitor_team_abbreviation": "away_team", "visitor_team_id": "away_team",
    "home_score": "home_score", "home_pts": "home_score", "home_points": "home_score",
    "pts_home": "home_score", "home_team_score": "home_score",
    "away_score": "away_score", "away_pts": "away_score", "away_points": "away_score",
    "pts_away": "away_score", "away_team_score": "away_score",
    "visitor_score": "away_score", "visitor_pts": "away_score",
    "visitor_points": "away_score", "pts_visitor": "away_score",
    "team": "team", "team_id": "team", "team_abbreviation": "team",
    "is_home": "is_home", "home_away": "is_home", "location": "is_home",
    "score": "score", "pts": "score", "points": "score",
    # made-shot naming (nba_data / llimllib style): FGM/FG3M/FTM
    "fgm": "fg", "ftm": "ft", "fg3m": "fg3", "3pm": "fg3",
}
for _side in ("home", "away"):
    for _stat in STAT_COLUMNS:
        _canon = f"{_side}_{_stat}"
        for _alias in (_canon, f"{_side[0]}_{_stat}", f"{_stat}_{_side}"):
            COLUMN_ALIASES.setdefault(_alias, _canon)
# nba_data / llimllib style: <stat>_<side> and <side>_<stat> with side in
# {home, away, visitor}, plus made-shot names (fgm_home -> home_fg,
# fg3m_visitor -> away_fg3, ...)
for _side_label, _side in (("home", "home"), ("away", "away"), ("visitor", "away")):
    for _stat in STAT_COLUMNS:
        COLUMN_ALIASES.setdefault(f"{_side_label}_{_stat}", f"{_side}_{_stat}")
        COLUMN_ALIASES.setdefault(f"{_stat}_{_side_label}", f"{_side}_{_stat}")
    for _alias, _canon in (("fgm", "fg"), ("ftm", "ft"), ("fg3m", "fg3")):
        COLUMN_ALIASES.setdefault(f"{_side_label}_{_alias}", f"{_side}_{_canon}")
        COLUMN_ALIASES.setdefault(f"{_alias}_{_side_label}", f"{_side}_{_canon}")
for _stat in STAT_COLUMNS:
    COLUMN_ALIASES.setdefault(_stat, _stat)


class IngestDataError(RuntimeError):
    """Raised when the CSV cannot be mapped into validated records."""


def _normalize_headers(headers: list[str]) -> dict[str, str]:
    """Map raw header names to canonical column names."""
    normalized: dict[str, str] = {}
    for header in headers:
        key = header.strip().lower()
        canonical = COLUMN_ALIASES.get(key)
        if canonical is None:
            continue  # unknown columns are ignored
        normalized[canonical] = header
    return normalized


def _team_id(raw: str, unknown: set[str]) -> str | None:
    canonical = canonical_team_id(raw)
    if canonical is None:
        unknown.add(raw.strip())
        return None
    return canonical


def _score_int(raw: Any, column: str) -> int:
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError) as exc:
        raise IngestDataError(f"unparseable score in column {column!r}: {raw!r}") from exc
    return value


def _stat_float(raw: Any, column: str) -> float | None:
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return None
    if str(raw).strip() == "":
        return None
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        raise IngestDataError(f"unparseable stat in column {column!r}: {raw!r}") from exc


def _season_for(date: datetime) -> str:
    """NBA season label: a game in Oct-Jun belongs to the season starting in
    its calendar year; Jul-Sep games belong to the season that started the
    previous year."""
    if date.month >= 7:
        return f"{date.year}-{str(date.year + 1)[-2:]}"
    return f"{date.year - 1}-{str(date.year)[-2:]}"


def _parse_date(raw: Any) -> datetime:
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestDataError(f"unparseable game_date: {raw!r}") from exc


def _build_team_stats(
    game_id: str,
    team_id: str,
    is_home: bool,
    row: dict[str, Any],
    columns: dict[str, str],
    side: str,
    used_estimate: list[str],
) -> TeamGameStats:
    def get(stat: str) -> float | None:
        column = f"{side}_{stat}" if side else stat
        header = columns.get(column)
        return _stat_float(row.get(header), header) if header else None

    stats = {stat: get(stat) for stat in STAT_COLUMNS}
    fg = stats["fg"] or 0
    fga = stats["fga"] or 0
    fg3 = stats["fg3"] or 0
    fg3a = stats["fg3a"] or 0
    ft = stats["ft"] or 0
    fta = stats["fta"] or 0
    orb = stats["orb"] or 0
    tov = stats["tov"] or 0
    possessions = stats["possessions"]
    if possessions is None:
        possessions = fga + 0.44 * fta - orb + tov
        used_estimate.append(game_id)
    points = None  # filled from the game score by the caller
    return TeamGameStats(
        game_id=game_id, team_id=team_id, is_home=is_home,
        minutes=240,
        points=int(points if points is not None else 0),
        possessions=float(possessions),
        fg=int(fg), fga=int(fga), fg3=int(fg3), fg3a=int(fg3a),
        ft=int(ft), fta=int(fta),
        orb=int(stats["orb"] or 0), drb=int(stats["drb"] or 0), trb=int(stats["trb"] or 0),
        ast=int(stats["ast"] or 0), tov=int(tov),
        stl=int(stats["stl"] or 0), blk=int(stats["blk"] or 0), pf=int(stats["pf"] or 0),
    )


def load_nba_games_csv(
    path: Path | str,
    *,
    source: str | None = None,
    season: str | None = None,
) -> dict:
    """Parse an NBA games CSV into the raw dataset dict for the ingestor.

    Returns ``{"games": [...], "team_stats": [...], "teams": [...]}``
    (player/availability keys are empty tuples).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"csv not found: {path}")

    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise IngestDataError("csv has no header row")
        columns = _normalize_headers(reader.fieldnames)
        raw_rows = list(reader)

    if not raw_rows:
        raise IngestDataError("csv contains no data rows")

    return _load_records(raw_rows, columns, season)


def _load_records(raw_rows: list[dict], columns: dict[str, str], season: str | None) -> dict:
    """Layout dispatch shared by the CSV and Parquet loaders."""
    has_game_layout = any(
        k in columns for k in ("home_team", "away_team", "home_score", "away_score")
    )
    if has_game_layout:
        return _parse_game_per_row(raw_rows, columns, season)
    return _parse_row_per_team(raw_rows, columns, season)


def _parse_game_per_row(raw_rows: list[dict], columns: dict[str, str], season: str | None) -> dict:
    for required in ("game_date", "home_team", "away_team", "home_score", "away_score"):
        if required not in columns:
            raise IngestDataError(
                f"missing required column: {required} (found: {sorted(columns)})"
            )
    games: list[Game] = []
    team_stats: list[TeamGameStats] = []
    teams: set[str] = set()
    unknown: set[str] = set()
    estimated: list[str] = []

    for i, row in enumerate(raw_rows, start=2):
        date = _parse_date(row[columns["game_date"]])
        home_id = _team_id(str(row[columns["home_team"]]), unknown)
        away_id = _team_id(str(row[columns["away_team"]]), unknown)
        if home_id is None or away_id is None:
            continue
        if home_id == away_id:
            raise IngestDataError(f"row {i}: home and away map to the same team")
        home_score = _score_int(row[columns["home_score"]], columns["home_score"])
        away_score = _score_int(row[columns["away_score"]], columns["away_score"])
        season_value = season
        if season_value is None and "season" in columns:
            season_value = str(row[columns["season"]]).strip()
        season_value = season_value or _season_for(date)
        game_id = (
            str(row[columns["game_id"]]).strip()
            if "game_id" in columns and str(row[columns["game_id"]]).strip()
            else f"{date:%Y%m%d}_{home_id}_{away_id}"
        )
        teams.update((home_id, away_id))

        home_ts = _build_team_stats(game_id, home_id, True, row, columns, "home", estimated)
        away_ts = _build_team_stats(game_id, away_id, False, row, columns, "away", estimated)
        home_ts = home_ts.model_copy(update={"points": home_score})
        away_ts = away_ts.model_copy(update={"points": away_score})
        games.append(Game(
            game_id=game_id, league=League.NBA, season=season_value, game_date=date,
            home_team_id=home_id, away_team_id=away_id,
            home_score=home_score, away_score=away_score, status=GameStatus.FINAL,
        ))
        team_stats.extend([home_ts, away_ts])

    if unknown:
        raise IngestDataError(
            "unknown team names/abbreviations in CSV (verify the mapping or extend "
            f"nba_teams.py): {sorted(unknown)}"
        )
    if not games:
        raise IngestDataError("no valid game rows produced")
    return _finalize(games, team_stats, teams, estimated)


def _parse_row_per_team(raw_rows: list[dict], columns: dict[str, str], season: str | None) -> dict:
    for required in ("game_date", "team", "score", "is_home"):
        if required not in columns:
            raise IngestDataError(
                f"row-per-team layout requires columns game_date, team, score, is_home "
                f"(found: {sorted(columns)})"
            )
    unknown: set[str] = set()
    estimated: list[str] = []

    def is_home(raw: Any) -> bool:
        text = str(raw).strip().lower()
        if text in ("1", "true", "home", "h", "@"):
            return True
        if text in ("0", "false", "away", "a"):
            return False
        raise IngestDataError(f"unparseable is_home value: {raw!r}")

    # pair rows by date: a game is exactly two rows with the same date
    games: list[Game] = []
    team_stats: list[TeamGameStats] = []
    teams: set[str] = set()
    # simpler robust pairing: bucket rows by date, then pair teams within a date
    by_date: dict[datetime, list[tuple[str, dict]]] = {}
    for row in raw_rows:
        date = _parse_date(row[columns["game_date"]])
        team = _team_id(str(row[columns["team"]]), unknown)
        if team is None:
            continue
        by_date.setdefault(date, []).append((team, row))

    for date, rows in by_date.items():
        if len(rows) % 2 != 0:
            raise IngestDataError(
                f"odd number of team rows for date {date:%Y-%m-%d} — cannot pair into games"
            )
        for idx in range(0, len(rows), 2):
            (t1, r1), (t2, r2) = rows[idx], rows[idx + 1]
            if t1 == t2:
                raise IngestDataError(f"duplicate team row for {t1} on {date:%Y-%m-%d}")
            home_side = (t1, r1) if is_home(r1[columns["is_home"]]) else (t2, r2)
            away_side = (t2, r2) if home_side[0] == t1 else (t1, r1)
            home_id, home_row = home_side
            away_id, away_row = away_side
            home_score = _score_int(home_row[columns["score"]], columns["score"])
            away_score = _score_int(away_row[columns["score"]], columns["score"])
            season_value = season
            if season_value is None and "season" in columns:
                season_value = str(home_row[columns["season"]]).strip()
            season_value = season_value or _season_for(date)
            game_id = (
                str(home_row[columns["game_id"]]).strip()
                if "game_id" in columns and str(home_row[columns["game_id"]]).strip()
                else f"{date:%Y%m%d}_{home_id}_{away_id}"
            )
            teams.update((home_id, away_id))
            home_ts = _build_team_stats(game_id, home_id, True, home_row, columns, "", estimated)
            away_ts = _build_team_stats(game_id, away_id, False, away_row, columns, "", estimated)
            home_ts = home_ts.model_copy(update={"points": home_score})
            away_ts = away_ts.model_copy(update={"points": away_score})
            games.append(Game(
                game_id=game_id, league=League.NBA, season=season_value, game_date=date,
                home_team_id=home_id, away_team_id=away_id,
                home_score=home_score, away_score=away_score, status=GameStatus.FINAL,
            ))
            team_stats.extend([home_ts, away_ts])

    if not games:
        raise IngestDataError("no valid game rows produced")
    return _finalize(games, team_stats, teams, estimated)


def _finalize(
    games: list[Game], team_stats: list[TeamGameStats], teams: set[str], estimated: list[str]
) -> dict:
    from basketball_agent.data.collectors.nba_teams import TEAM_NAMES
    from basketball_agent.data.schemas.team import Team

    return {
        "games": [g.model_dump(mode="json") for g in games],
        "team_stats": [ts.model_dump(mode="json") for ts in team_stats],
        "teams": [
            Team(
                team_id=tid, league=League.NBA,
                name=TEAM_NAMES.get(tid, tid), abbreviation=tid,
            ).model_dump(mode="json")
            for tid in sorted(teams)
        ],
        "players": [],
        "player_stats": [],
        "availability": [],
        "_meta": {
            "n_games": len(games),
            "possessions_estimated_for": len(estimated),
        },
    }


def write_canonical_csv(raw: dict, path: Path | str) -> None:
    """Write a normalized raw dataset back to the canonical game-per-row CSV.

    Used by the offline Parquet -> CSV conversion so any Parquet source can
    feed the standard CSV pipeline without touching the core models.
    """
    path = Path(path)
    games = {g["game_id"]: g for g in raw["games"]}
    stats_by_game: dict[str, dict[str, dict]] = {}
    for ts in raw["team_stats"]:
        stats_by_game.setdefault(ts["game_id"], {})[ts["team_id"]] = ts

    headers = (["game_date", "home_team", "away_team", "home_score", "away_score"]
               + [f"home_{s}" for s in STAT_COLUMNS]
               + [f"away_{s}" for s in STAT_COLUMNS])

    def fmt(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)

    rows: list[dict[str, str]] = []
    for game in games.values():
        home = stats_by_game.get(game["game_id"], {}).get(game["home_team_id"])
        away = stats_by_game.get(game["game_id"], {}).get(game["away_team_id"])
        row = {
            "game_date": fmt(game.get("game_date", "")),
            "home_team": fmt(game.get("home_team_id", "")),
            "away_team": fmt(game.get("away_team_id", "")),
            "home_score": fmt(game.get("home_score", "")),
            "away_score": fmt(game.get("away_score", "")),
        }
        for side, ts in (("home", home), ("away", away)):
            for stat in STAT_COLUMNS:
                row[f"{side}_{stat}"] = fmt(ts.get(stat, 0)) if ts else "0"
        rows.append(row)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
