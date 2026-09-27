"""CBA (Chinese Basketball Association) official-API adapter (Phase 8-E).

Source: the official data SPA (``https://www.cbaleague.com/data/``) backed by
``https://data-server.cbaleague.com``.  Every JSON response is AES-128/ECB/
PKCS7 encrypted; the key ships in the site's own public front-end bundle.
Phase 8-E source decision: B0's preferred RealGM route is blocked in this
environment (HTTP 403 on direct fetches; the reader service serves only
stale mid-season snapshots; Basketball-Reference fails at connection level;
the SciDB 2022-23 dataset is referee data, not box scores) — the B0 fallback
chain therefore lands on the official site, which publishes complete
schedules and per-game team box scores for every season back to ~2019-20.

Bundle layout (produced by ``tools/fetch_cba_bundle.py``, marker
``cba-api-v1``): ``teams`` (official list), ``months`` (per-day official
game counts — the schedule reference for completeness audits), ``days``
(per-day game rows with home/away ids, scores, ``matchTypeId``,
``status``, ``quarter``/``minutes``), ``team_logs`` (per-team, per-stage
per-game TEAM box scores; stage 1 = regular season, 2 = playoffs).

Interpretation rules (fail loud, never guess):

* team identity = the official numeric club id, stored as ``CBA-<id>``
  (namespace prefix — the EL ``MIL`` collision precedent);
* ``matchTypeId`` maps onto the shared Competition enum: 1 -> REGULAR_SEASON,
  2 -> PLAYOFF, 9 (relegation, historical) -> CUP; 3 (preseason) and
  4 (all-star) are exhibitions and are dropped at the adapter (counted in
  ``_meta``); unknown ids are a hard error;
* ``status`` 4 is FINAL; anything else with a score is a hard error and
  without a score is skipped (not played yet);
* game length comes from the schedule row: ``minutes == regulation_minutes
  + 5 * overtimes`` with ``overtimes = quarter - 4``; the regulation value
  is INFERRED from the bundle (the CBA moved from 4x12 (2880 s) to FIBA
  4x10 (2400 s) in 2025-26 — the field is season-dependent) and must be
  unambiguous, otherwise a hard error; team minutes are
  ``(regulation + 5*OT) * 5`` (5 players on the floor);
* box scores come from the two clubs' match-log rows (joined by
  ``scheduleId``) and MUST reconcile with the schedule scores (two-source
  rule, mirrors EuroLeague Header/Boxscore and NBL calendar/detail);
  the standard points and rebounds identities must hold exactly;
* possessions are the standard estimate and counted in ``_meta``.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.game import Game, TeamGameStats
from basketball_agent.data.schemas.team import Team

DATASET_MARKER = "cba-api-v1"

_BEIJING = timezone(timedelta(hours=8))  # CBA plays entirely in UTC+8

_MATCH_TYPE_TO_COMPETITION: dict[int, Competition] = {
    1: Competition.REGULAR_SEASON,
    2: Competition.PLAYOFF,
    9: Competition.CUP,  # relegation series (historical); non-regular
}
_DROPPED_TYPE_NAMES: dict[int, str] = {3: "preseason", 4: "all-star"}


class CBADataError(RuntimeError):
    """Raised when the bundle cannot be mapped into validated records."""


def resolve_team(team_id: Any) -> str:
    """Official numeric club id -> stored id (``CBA-`` prefixed)."""
    text = str(team_id or "").strip()
    if not text.isdigit() or not 4 <= len(text) <= 9:
        raise CBADataError(f"implausible CBA team id: {team_id!r}")
    return f"CBA-{text}"


def _season_label(year: int) -> str:
    return f"{year}-{str((year + 1) % 100).zfill(2)}"


def _competition_for(match_type_id: Any) -> Competition | None:
    """Competition segment for one game; ``None`` = exhibition (drop)."""
    try:
        mtid = int(match_type_id)
    except (TypeError, ValueError):
        raise CBADataError(f"unknown CBA matchTypeId {match_type_id!r}") from None
    if mtid in _DROPPED_TYPE_NAMES:
        return None
    competition = _MATCH_TYPE_TO_COMPETITION.get(mtid)
    if competition is None:
        raise CBADataError(
            f"unknown CBA matchTypeId {match_type_id!r} (extend "
            "cba_api._MATCH_TYPE_TO_COMPETITION from a real payload; never guess)"
        )
    return competition


def _status_for(row: dict) -> GameStatus | None:
    status = row.get("status")
    if status == 4:
        return GameStatus.FINAL
    return None  # not played / unknown state: caller decides


def _regulation_minutes(rows: list[dict]) -> int:
    """Infer the season's regulation length from quarter==4 rows.

    The CBA moved from 4x12 to FIBA 4x10 in 2025-26, so the bundle must
    tell us; a mixed/ambiguous schedule is a hard error.
    """
    base = Counter(
        int(r["minutes"]) for r in rows
        if r.get("status") == 4 and r.get("quarter") == 4
    )
    if not base:
        raise CBADataError("no finished quarter==4 games to infer regulation length")
    top, count = base.most_common(1)[0]
    if top <= 0 or top % 5 != 0:
        raise CBADataError(f"implausible regulation minutes {top!r}")
    if len(base) > 1:
        others = {k: v for k, v in base.items() if k != top}
        raise CBADataError(
            f"ambiguous regulation length: modal {top} min but also {others} "
            "(per-season bundles required; extend cba_api deliberately)"
        )
    return top


def _line_from_log(row: dict, points: int, team_minutes: int) -> dict:
    fg = int(row["fieldGoals"])
    fga = int(row["fieldGoalsAttempted"])
    fg3 = int(row["threePointGoals"])
    fg3a = int(row["threePointAttempted"])
    ft = int(row["freeThrows"])
    fta = int(row["freeThrowsAttempted"])
    orb = int(row["reboundsOffensive"])
    drb = int(row["reboundsDefensive"])
    trb_source = int(row["rebounds"])
    line = {
        "minutes": team_minutes,
        "points": points,
        "fg": fg, "fga": fga, "fg3": fg3, "fg3a": fg3a, "ft": ft, "fta": fta,
        "orb": orb, "drb": drb, "trb": trb_source,
        "ast": int(row["assists"]),
        "tov": int(row["turnovers"]),
        "stl": int(row["steals"]),
        "blk": int(row["blocked"]),
        "pf": int(row["personalFouls"]),
    }
    # source identity checks (fail loud — the log row is the official record)
    if points != 2 * (fg - fg3) + 3 * fg3 + ft:
        raise CBADataError(
            f"points identity broken: {points} != 2*{fg - fg3}+3*{fg3}+{ft}"
        )
    if trb_source != orb + drb:
        raise CBADataError(
            f"rebounds identity broken: {trb_source} != {orb}+{drb}"
        )
    if fga < fg3a or fg < fg3 or fta < ft or fga < fg:
        raise CBADataError("attempted/made consistency broken (fga<fg3a etc.)")
    line["possessions"] = round(fga + 0.44 * fta - orb + line["tov"], 1)
    return line


def parse_cba_bundle(bundle: dict) -> dict:
    if bundle.get("dataset") != DATASET_MARKER:
        raise CBADataError(
            f"not a CBA bundle (expected dataset={DATASET_MARKER!r})"
        )
    year = int(bundle["season"])
    season = _season_label(year)

    teams_meta: dict[int, str] = {}
    for t in bundle.get("teams") or []:
        teams_meta[int(t["value"])] = str(t["key"]).strip()
    if not teams_meta:
        raise CBADataError("bundle contains no team list")

    log_rows: dict[int, dict[int, dict]] = {}  # scheduleId -> teamId -> row
    for group in bundle.get("team_logs") or []:
        team_id = int(group["teamId"])
        for row in group.get("rows") or []:
            sid = row.get("scheduleId")
            existing = log_rows.setdefault(int(sid), {})
            if team_id in existing:
                raise CBADataError(
                    f"schedule {sid}: duplicate log row for team {team_id}"
                )
            existing[team_id] = row

    day_rows: list[tuple[str, dict]] = []
    for day in sorted(bundle.get("days") or {}):
        for row in bundle["days"][day]:
            day_rows.append((day, row))
    day_rows.sort(key=lambda dr: (str(dr[1].get("scheduleTime")), int(dr[1]["scheduleId"])))

    regulation = _regulation_minutes([r for _, r in day_rows])

    games: list[Game] = []
    team_stats: list[TeamGameStats] = []
    teams: dict[str, Team] = {}
    dropped = Counter()
    skipped_no_score = 0
    estimated = 0
    ot_games = 0

    for day, row in day_rows:
        try:
            competition = _competition_for(row.get("matchTypeId"))
        except CBADataError as exc:
            raise CBADataError(
                f"game {row.get('scheduleId')} ({day}): {exc}"
            ) from exc
        if competition is None:
            dropped[_DROPPED_TYPE_NAMES[int(row["matchTypeId"])]] += 1
            continue

        status = _status_for(row)
        if status is None:
            if row.get("homeTeamScore") is not None or row.get("visitingTeamScore") is not None:
                raise CBADataError(
                    f"game {row.get('scheduleId')}: unrecognised status "
                    f"{row.get('status')!r} with scores "
                    "(extend cba_api._status_for; never guess)"
                )
            skipped_no_score += 1
            continue

        schedule_id = int(row["scheduleId"])
        home_t = int(row["homeTeamId"])
        away_t = int(row["visitingTeamId"])
        for t in (home_t, away_t):
            if t not in teams_meta:
                raise CBADataError(
                    f"game {schedule_id}: team {t} missing from the official "
                    "team list (refetch the bundle)"
                )
        home_id, away_id = resolve_team(home_t), resolve_team(away_t)

        quarter = row.get("quarter")
        minutes = row.get("minutes")
        if not isinstance(quarter, int) or not isinstance(minutes, int):
            raise CBADataError(f"game {schedule_id}: missing quarter/minutes")
        if quarter < 4:
            raise CBADataError(f"game {schedule_id}: impossible quarter {quarter}")
        overtimes = quarter - 4
        expected_game_minutes = regulation + 5 * overtimes
        if minutes != expected_game_minutes:
            raise CBADataError(
                f"game {schedule_id}: {minutes} game minutes for quarter={quarter} "
                f"(expected {expected_game_minutes} = {regulation} + 5*OT)"
            )
        team_minutes = expected_game_minutes * 5

        pair = log_rows.get(schedule_id, {})
        home_row = pair.get(home_t)
        away_row = pair.get(away_t)
        if home_row is None or away_row is None:
            missing = [t for t in (home_t, away_t) if t not in pair]
            raise CBADataError(
                f"game {schedule_id}: no official match-log row for team(s) "
                f"{missing} (refetch the bundle)"
            )

        home_score = int(row["homeTeamScore"])
        away_score = int(row["visitingTeamScore"])
        # two-source rule: schedule scores MUST reconcile with both clubs'
        # independent match-log rows
        for club_row, own, opp, label in (
            (home_row, home_score, away_score, "home"),
            (away_row, away_score, home_score, "away"),
        ):
            if int(club_row["points"]) != own or int(club_row["acrossPoints"]) != opp:
                raise CBADataError(
                    f"game {schedule_id}: schedule score {home_score}-{away_score} "
                    f"!= {label} match-log row "
                    f"{club_row['points']}-{club_row['acrossPoints']}"
                )
            if int(club_row["teamId"]) != (home_t if label == "home" else away_t):
                raise CBADataError(f"game {schedule_id}: {label} log row team mismatch")

        game_date = datetime.strptime(
            str(row["scheduleTime"]), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=_BEIJING)

        games.append(Game(
            game_id=f"CBA{year % 100:02d}-{len(games) + 1:04d}",
            league=League.CBA, season=season, game_date=game_date,
            home_team_id=home_id, away_team_id=away_id,
            home_score=home_score, away_score=away_score,
            status=GameStatus.FINAL, competition=competition,
        ))
        if overtimes:
            ot_games += 1
        for team_id, club_row, is_home in ((home_id, home_row, True),
                                           (away_id, away_row, False)):
            line = _line_from_log(club_row, int(club_row["points"]), team_minutes)
            team_stats.append(TeamGameStats(
                game_id=games[-1].game_id, team_id=team_id,
                is_home=is_home, **line,
            ))
            estimated += 1

    for team_id, name in teams_meta.items():
        stored = resolve_team(team_id)
        teams[stored] = Team(
            team_id=stored, league=League.CBA, name=name, abbreviation=stored,
        )

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
            "n_dropped_preseason": dropped["preseason"],
            "n_dropped_allstar": dropped["all-star"],
            "n_cup": sum(1 for g in games if g.competition is Competition.CUP),
            "n_skipped_no_score": skipped_no_score,
            "n_overtime_games": ot_games,
            "regulation_minutes": regulation,
            "possessions_estimated_for": estimated,
            "season": season,
            "source": bundle.get("source"),
        },
    }


def load_cba_bundle_file(path: Path | str) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CBA bundle not found: {path}")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CBADataError(f"bundle is not valid JSON: {exc}") from exc
    return parse_cba_bundle(bundle)
