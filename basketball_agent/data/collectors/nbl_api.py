"""NBL official-API adapter (Phase 8-D).

Source (verified against real responses):

* ``https://schedule.nbl.com.au/api/calendar/schedule?league=nbl&year={Y}``
  — season calendar (authoritative home/away, scores, ``team_code``,
  ``round_label``, ``season_type``, ``status_raw``/``phase``);
* ``https://schedule.nbl.com.au/api/calendar/match?match={uuid}`` — per-match
  detail; the ``team_match_statistics`` rows with ``period=="0"`` are the
  official game totals and reconcile with the final score.

Phase 8-D source decision: B0 preferred the FIBA LiveStats ``data.json``
route, but LiveStats game ids have no public mapping from the NBL schedule
(the numeric id is only discoverable per-game via search engines), while the
NBL's own API publishes complete official box scores for every game.  The
official API is therefore primary (ACB precedent); the LiveStats route is
recorded as unavailable-for-enumeration.

Interpretation rules (fail loud, never guess):

* team identity = the official ``team_code`` from the calendar, stored as
  ``NBL-<code>`` (namespace prefix — the EL ``MIL`` collision precedent);
* ``season_type``/``round_label`` map onto the shared Competition enum:
  numeric rounds -> REGULAR_SEASON; SEEDING/QUALIFIER/ELIMINATION/
  PLAYOFF*/CHAMPIONSHIP -> PLAYOFF; IGNITE CUP -> CUP; PRESEASON/BLITZ/
  NBAxNBL/PG exhibitions are dropped at the adapter (counted in ``_meta``),
  mirroring the NBA adapter's 001/003 handling; unknown labels are a hard
  error;
* team minutes come from the period-0 totals row and MUST equal
  ``200 + 25 * overtimes`` (NBL publishes exact integer minutes; the
  ACB-style drift tolerance proved unnecessary here — a mismatch is a data
  error);
* 2-point/3-point splits are combined into ``fg``/``fga``; possessions are
  the standard estimate and counted in ``_meta``;
* the two period-0 rows carry no team marker, so they are mapped to
  home/away by points (basketball scores never tie after overtime) and
  MUST reconcile with the calendar's ``home_score``/``away_score``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.game import Game, TeamGameStats
from basketball_agent.data.schemas.team import Team

DATASET_MARKER = "nbl-api-v1"

_ROUND_TO_COMPETITION_RULES: tuple[tuple[str, str], ...] = (
    ("PLAYOFF", "PLAYOFF"), ("CHAMPIONSHIP", "PLAYOFF"),
    ("SEEDING", "PLAYOFF"), ("QUALIFIER", "PLAYOFF"),
    ("ELIMINATION", "PLAYOFF"), ("FINALS", "PLAYOFF"),
    ("IGNITE CUP", "CUP"),
)
_EXHIBITION_LABELS = ("PRESEASON", "BLITZ", "NBAXNBL", "PG")


def _competition_for(round_label: str, season_type: str) -> Competition | None:
    """Competition segment for one match; ``None`` = exhibition (drop)."""
    label = str(round_label or "").strip().upper()
    if season_type == "preseason":
        return None
    for needle, competition in _ROUND_TO_COMPETITION_RULES:
        if needle in label:
            return Competition(competition)
    if label.isdigit():
        return Competition.REGULAR_SEASON
    for exhibition in _EXHIBITION_LABELS:
        if exhibition in label:
            return None
    raise ValueError(
        f"unknown NBL round_label {round_label!r} (extend "
        "nbl_api._ROUND_TO_COMPETITION_RULES from a real payload; never guess)"
    )


class NBLDataError(RuntimeError):
    """Raised when the bundle cannot be mapped into validated records."""


def resolve_team(code: str) -> str:
    """Official ``team_code`` -> stored id (``NBL-`` prefixed)."""
    text = str(code or "").strip().upper()
    if not text or not text.replace("-", "").isalnum() or len(text) > 4:
        raise NBLDataError(f"implausible NBL team_code: {code!r}")
    return f"NBL-{text}"


def _season_label(year: int) -> str:
    return f"{year}-{str((year + 1) % 100).zfill(2)}"


def _status_for(match: dict) -> GameStatus | None:
    status = str(match.get("status_raw") or "").strip().upper()
    phase = str(match.get("phase") or "").strip().lower()
    if status == "CONFIRMED" and phase == "complete":
        return GameStatus.FINAL
    if "POSTPONE" in status:
        return GameStatus.POSTPONED
    if "CANCEL" in status:
        return GameStatus.CANCELLED
    return None  # not played yet / unknown state: caller decides


def _totals_rows(rows: list[dict], game_id: str) -> list[dict]:
    totals = [r for r in rows if str(r.get("period")) == "0"]
    if len(totals) != 2:
        raise NBLDataError(
            f"game {game_id}: expected exactly 2 period-0 totals rows, got {len(totals)}"
        )
    return totals


def _line_from_totals(row: dict, points: int) -> dict:
    fg2 = int(row["field_goals_made"]) - int(row["three_points_made"])
    fga2 = int(row["field_goals_attempted"]) - int(row["three_points_attempted"])
    line = {
        "minutes": int(row["minutes"]),
        "points": points,
        "fg": int(row["field_goals_made"]),
        "fga": int(row["field_goals_attempted"]),
        "fg3": int(row["three_points_made"]),
        "fg3a": int(row["three_points_attempted"]),
        "ft": int(row["free_throws_made"]),
        "fta": int(row["free_throws_attempted"]),
        "orb": int(row["offensive_rebounds"]),
        "drb": int(row["defensive_rebounds"]),
        "trb": int(row["offensive_rebounds"]) + int(row["defensive_rebounds"]),
        "ast": int(row["assists"]),
        "tov": int(row["turnovers"]),
        "stl": int(row["steals"]),
        "blk": int(row["blocks"]),
        "pf": int(row["fouls"]),
    }
    # source identity checks (fail loud — the totals row is the official record)
    if points != 2 * (line["fg"] - line["fg3"]) + 3 * line["fg3"] + line["ft"]:
        raise NBLDataError(
            f"points identity broken: {points} != 2*{line['fg'] - line['fg3']}"
            f"+3*{line['fg3']}+{line['ft']}"
        )
    if line["trb"] != line["orb"] + line["drb"]:
        raise NBLDataError(f"rebounds identity broken: {line['trb']} != "
                           f"{line['orb']}+{line['drb']}")
    line["possessions"] = round(
        line["fga"] + 0.44 * line["fta"] - line["orb"] + line["tov"], 1
    )
    return line


def parse_nbl_bundle(bundle: dict) -> dict:
    if bundle.get("dataset") != DATASET_MARKER:
        raise NBLDataError(
            f"not an NBL bundle (expected dataset={DATASET_MARKER!r})"
        )
    year = int(bundle["year"])
    season = _season_label(year)
    calendar: dict = bundle.get("calendar") or {}
    matches: dict = bundle.get("matches") or {}
    if not calendar.get("matches"):
        raise NBLDataError("bundle contains no calendar matches")

    games: list[Game] = []
    team_stats: list[TeamGameStats] = []
    teams: dict[str, Team] = {}
    estimated = 0
    dropped_exhibitions = 0
    minutes_drifted = 0
    postponed = cancelled = 0

    calendar_matches = sorted(
        calendar["matches"],
        key=lambda m: (m.get("starts_at_ms") or 0, str(m.get("id"))),
    )
    seq = 0
    for cal in calendar_matches:
        round_label = str(cal.get("round_label") or "")
        season_type = str(cal.get("season_type") or "")
        try:
            competition = _competition_for(round_label, season_type)
        except ValueError as exc:
            raise NBLDataError(f"match {cal.get('id')}: {exc}") from exc
        if competition is None:
            dropped_exhibitions += 1
            continue

        mid = str(cal["id"])
        game_id = f"NBL{year % 100:02d}-{seq + 1:04d}"
        seq += 1

        status = _status_for(cal)
        if status is None:
            # not played / unknown state: only valid with no scores
            if cal.get("home_score") is not None or cal.get("away_score") is not None:
                raise NBLDataError(
                    f"match {cal.get('id')}: unrecognised status "
                    f"{cal.get('status_raw')!r}/{cal.get('phase')!r} with scores "
                    "(extend nbl_api._status_for; never guess)"
                )
            continue  # future match: nothing to ingest yet
        home_id = resolve_team((cal.get("home") or {}).get("team_code"))
        away_id = resolve_team((cal.get("away") or {}).get("team_code"))
        home_name = str((cal.get("home") or {}).get("name") or "").strip()
        away_name = str((cal.get("away") or {}).get("name") or "").strip()
        starts_ms = cal.get("starts_at_ms")
        if starts_ms is None:
            raise NBLDataError(f"game {game_id}: missing starts_at_ms")
        game_date = datetime.fromtimestamp(int(starts_ms) / 1000, tz=timezone.utc)

        if status in (GameStatus.POSTPONED, GameStatus.CANCELLED):
            if status is GameStatus.POSTPONED:
                postponed += 1
            else:
                cancelled += 1
            games.append(Game(
                game_id=game_id, league=League.NBL, season=season,
                game_date=game_date, home_team_id=home_id, away_team_id=away_id,
                home_score=None, away_score=None, status=status,
                competition=competition,
            ))
            continue

        detail = matches.get(mid)
        if detail is None:
            raise NBLDataError(
                f"game {game_id}: FINAL calendar match {mid} has no fetched detail "
                "(refetch with --resume)"
            )
        home_score = int(detail.get("home_score") or 0)
        away_score = int(detail.get("away_score") or 0)
        # two-source rule: the calendar and the match detail are separate
        # responses — their scores MUST agree (mirrors the EuroLeague
        # Header/Boxscore cross-check)
        cal_home, cal_away = cal.get("home_score"), cal.get("away_score")
        if cal_home is not None and away_score is not None:
            if int(cal_home) != home_score or int(cal_away) != away_score:
                raise NBLDataError(
                    f"game {game_id}: calendar score {cal_home}-{cal_away} != "
                    f"detail score {home_score}-{away_score}"
                )

        totals = _totals_rows(detail.get("team_match_statistics") or [], game_id)
        # rows carry no team marker: map by points (a basketball score never ties)
        by_points = {int(r.get("points") or 0): r for r in totals}
        if len(by_points) != 2:
            raise NBLDataError(
                f"game {game_id}: totals rows do not map uniquely to the final score "
                f"({home_score}-{away_score})"
            )
        home_row = by_points.get(home_score)
        away_row = by_points.get(away_score)
        if home_row is None or away_row is None:
            raise NBLDataError(
                f"game {game_id}: totals points {sorted(by_points)} != calendar "
                f"score {home_score}-{away_score}"
            )
        home_line = _line_from_totals(home_row, home_score)
        away_line = _line_from_totals(away_row, away_score)

        overtime_periods = {
            str(r.get("period")) for r in detail.get("team_match_statistics") or []
            if str(r.get("period_type")) == "overtime"
        }
        expected_minutes = 200 + 25 * len(overtime_periods)
        for team_id, line in ((home_id, home_line), (away_id, away_line)):
            # the NBL feed shows the same small per-team minutes drift the ACB
            # 2020-21 feed had (real find: +2 min in NBL25-0026/NBL-SEM);
            # bounded tolerance (+/- 5 min), drifted teams counted in
            # _meta.team_minutes_drifted, never silently repaired
            if abs(line["minutes"] - expected_minutes) > 5:
                raise NBLDataError(
                    f"game {game_id}/{team_id}: {line['minutes']} team minutes for a "
                    f"game with {len(overtime_periods)} OT period(s) "
                    f"(expected ~{expected_minutes})"
                )
            if line["minutes"] != expected_minutes:
                minutes_drifted += 1
        estimated += 2

        games.append(Game(
            game_id=game_id, league=League.NBL, season=season,
            game_date=game_date, home_team_id=home_id, away_team_id=away_id,
            home_score=home_score, away_score=away_score,
            status=GameStatus.FINAL, competition=competition,
        ))
        for team_id, line, is_home in ((home_id, home_line, True),
                                       (away_id, away_line, False)):
            team_stats.append(TeamGameStats(
                game_id=game_id, team_id=team_id, is_home=is_home, **line,
            ))
        for team_id, name in ((home_id, home_name), (away_id, away_name)):
            teams.setdefault(team_id, Team(
                team_id=team_id, league=League.NBL, name=name,
                abbreviation=team_id,
            ))

    return {
        "games": [g.model_dump(mode="json") for g in games],
        "team_stats": [ts.model_dump(mode="json") for ts in team_stats],
        "teams": [t.model_dump(mode="json") for t in teams.values()],
        "players": [],
        "player_stats": [],
        "availability": [],
        "_meta": {
            "n_games": len(games),
            "n_final": sum(1 for g in games if g.status is GameStatus.FINAL),
            "n_postponed": postponed,
            "n_cancelled": cancelled,
            "n_exhibitions_dropped": dropped_exhibitions,
            "team_minutes_drifted": minutes_drifted,
            "possessions_estimated_for": estimated,
            "year": year,
            "season": season,
            "source": bundle.get("source"),
        },
    }


def load_nbl_bundle_file(path: Path | str) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"NBL bundle not found: {path}")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NBLDataError(f"bundle is not valid JSON: {exc}") from exc
    return parse_nbl_bundle(bundle)
