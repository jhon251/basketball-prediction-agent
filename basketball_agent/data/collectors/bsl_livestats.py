"""BSL (Turkish Basketbol Süper Ligi) adapter (Phase 8-F).

Sources (both verified against live responses):

* TBF official API (``www.tbf.org.tr/api/...``) — season enumeration and
  the authoritative schedule: ``Match/get-all-matches-for-filter`` returns
  every match with home/away ids, scores, week label and ``genuisId`` (the
  FIBA LiveStats game id).  This is the schedule reference.
* FIBA LiveStats via Genius Sports
  (``fibalivestats.dcd.shared.geniussports.com/data/{id}/data.json``) —
  official team-level box scores.  B0's primary path; per-game data works
  but LiveStats has no schedule->id mapping (NBL 8-D precedent), so TBF
  supplies the enumeration.

Interpretation rules (fail loud, never guess):

* team identity = the TBF numeric team id, stored as ``BSL-<id>``
  (namespace prefix — the EL ``MIL`` collision precedent);
* week label maps onto the shared Competition enum: ``N. Hafta`` ->
  REGULAR_SEASON; ``Playoff Çeyrek Final`` / ``Playoff Yarı Final`` /
  ``Playoff Final`` -> PLAYOFF; anything else is a hard error;
* TBF ``matchStatusId`` 1 (played) with scores on both sides is FINAL;
  missing scores are skipped and counted; any other status WITH scores is
  a hard error (extend deliberately from a real payload);
* the LiveStats team objects carry no home/away marker: they are mapped to
  home/away by score (a basketball score never ties) and MUST reconcile
  with the TBF schedule scores (two-source rule, mirrors EuroLeague
  Header/Boxscore and NBL calendar/detail);
* LiveStats ``sFieldGoals*`` are the 2P+3P combined figures (identity
  checked against the 2P/3P splits on every row); the standard points and
  rebounds identities must hold exactly;
* BSL plays FIBA rules: 4 x 10 min regulation = 200 team-minutes, 5-minute
  OTs add 25 team-minutes each.  LiveStats ``tot_sMinutes`` is ``MM:SS``
  with scorekeeper quirks (``199:60`` = exactly 200:00); team minutes are
  validated against ``200 + 25 * OT`` with a bounded +/- 2-minute
  tolerance (drift counted in ``_meta``, never repaired) and the OT count
  derived from minutes must agree with the TBF ``hasUzatma`` flag;
* possessions are the standard estimate and counted in ``_meta``;
* per-player rows, play-by-play, shot and lead data exist in LiveStats but
  are dropped at fetch time (Phase 8-F records their presence only).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.game import Game, TeamGameStats
from basketball_agent.data.schemas.team import Team

DATASET_MARKER = "bsl-livestats-v1"

_TR_TIME = timezone(timedelta(hours=3))  # Türkiye is permanent UTC+3

_REGULATION_TEAM_MINUTES = 200.0  # 4 x 10 min x 5 players (FIBA)
_OT_TEAM_MINUTES = 25.0           # 5-minute OT x 5 players
_MINUTES_TOLERANCE = 2.0          # bounded drift; scorekeeper quirks (199:60)

_WEEK_RE = re.compile(r"^(\d+)\.\s*Hafta$")
_PLAYOFF_WEEKS = {
    "Playoff Çeyrek Final",
    "Playoff Yarı Final",
    "Playoff Final",
}
_PLAY_IN_WEEKS = {
    "Play-in",  # the 2024-25 season introduced a play-in round
}

#: Verified LiveStats source defects (ACB precedent: explicit exclusion,
#: never silent repair).  A listed match is EXCLUDED only when its LiveStats
#: payload actually fails the box-score gates; if the feed is ever
#: completed upstream, the game is ingested normally.
KNOWN_SOURCE_DEFECTS: dict[int, str] = {
    303916: (
        "BSL25 week 2 (2025-10-04) Türk Telekom vs Bahçeşehir Koleji: "
        "LiveStats feed frozen mid-Q4 (75-74, clock 09:37, 152 team-minutes) "
        "and never finalised upstream — refetched 2026-09-27, byte-identical; "
        "TBF schedule has the true final 98-102 but no box score, so the "
        "game cannot be ingested without fabricating data. Excluded."
    ),
}


class BSLDataError(RuntimeError):
    """Raised when the bundle cannot be mapped into validated records."""


#: TBF assigns team ids per league-activity (per season) — the same club
#: carries a different id in 2024-25 vs 2025-26 (verified: zero id overlap
#: across the two ingested seasons; TBF's own model is a per-season
#: "team process", see getTeamProcessIdForSeason in their front-end).
#: Cross-season Elo/strength history therefore requires joining clubs
#: explicitly.  This map redirects each 2024-25 club id to the canonical
#: (2025-26) id of the SAME club; pairs were verified against the TBF team
#: names of both seasons (sponsor prefixes change, base clubs match).
#: Clubs present in only one season (Darüşşafaka LASSA and SEMT77
#: Yalovaspor relegated after 2024-25; Safiport Erokspor and Trabzonspor
#: promoted for 2025-26) are intentionally absent — no cross-season link
#: is possible for them.
CLUB_ID_CONTINUITY_2024_25: dict[int, int] = {
    164374: 246936,  # Anadolu Efes
    164375: 246960,  # Bahçeşehir Koleji
    164376: 246961,  # Beşiktaş (Fibabanka -> Gain)
    164377: 246962,  # Bursaspor (Yörsan -> Basketbol)
    164378: 246963,  # Onvo Büyükçekmece Basketbol
    164380: 246965,  # Fenerbahçe Beko
    164381: 246966,  # Galatasaray (-> MCT Technic)
    164382: 246967,  # Karşıyaka
    164383: 246968,  # Manisa Basket (Divissa -> Glint)
    164384: 246969,  # Yukatel Merkezefendi Belediyesi Basket
    164389: 246970,  # Mersin Spor
    164390: 246971,  # Aliağa Petkimspor (-> Petkim Spor)
    164391: 246972,  # Tofaş
    164392: 246974,  # Türk Telekom
}


def resolve_team(team_id: Any, season: str | None = None) -> str:
    """TBF numeric team id -> stored id (``BSL-`` prefixed).

    With a ``season`` given, per-season club ids are mapped to the
    canonical (2025-26) id of the same club (Phase 8-H) so that
    cross-season history links; without a season the raw id is used.
    """
    text = str(team_id or "").strip()
    if not text.isdigit() or not 4 <= len(text) <= 9:
        raise BSLDataError(f"implausible BSL team id: {team_id!r}")
    numeric = int(text)
    if season == "2024-25":
        numeric = CLUB_ID_CONTINUITY_2024_25.get(numeric, numeric)
    return f"BSL-{numeric}"


def _season_label(season: str) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{4})", str(season).strip())
    if not m:
        raise BSLDataError(f"implausible BSL season {season!r}")
    y0, y1 = int(m.group(1)), int(m.group(2))
    if y1 != y0 + 1:
        raise BSLDataError(f"implausible BSL season {season!r}")
    return f"{y0}-{str(y1 % 100).zfill(2)}"


def _competition_for(week: Any) -> Competition:
    text = str(week or "").strip()
    if _WEEK_RE.fullmatch(text):
        return Competition.REGULAR_SEASON
    if text in _PLAYOFF_WEEKS:
        return Competition.PLAYOFF
    if text in _PLAY_IN_WEEKS:
        return Competition.PLAY_IN
    raise BSLDataError(
        f"unknown BSL week label {week!r} (extend "
        "bsl_livestats._PLAYOFF_WEEKS from a real payload; never guess)"
    )


def _parse_minutes(raw: Any) -> float:
    """LiveStats ``tot_sMinutes`` 'MM:SS' -> team minutes as float.

    ``199:60`` is a real scorekeeper rendering of 200:00; normalise via
    total seconds.
    """
    m = re.fullmatch(r"(\d+):(\d{1,2})", str(raw or "").strip())
    if not m:
        raise BSLDataError(f"implausible tot_sMinutes {raw!r}")
    minutes, seconds = int(m.group(1)), int(m.group(2))
    return (minutes * 60 + seconds) / 60.0


def _status_and_scores(row: dict) -> tuple[GameStatus | None, int | None, int | None]:
    home, away = row.get("homeTeam") or {}, row.get("awayTeam") or {}
    home_score = home.get("score")
    away_score = away.get("score")
    if home_score in (None, "") or away_score in (None, ""):
        if str(row.get("matchStatusId")) == "1":
            raise BSLDataError(
                f"match {row.get('matchId')}: played (status 1) but scores missing"
            )
        return None, None, None
    if str(row.get("matchStatusId")) != "1":
        raise BSLDataError(
            f"match {row.get('matchId')}: unrecognised matchStatusId "
            f"{row.get('matchStatusId')!r} with scores "
            "(extend bsl_livestats; never guess)"
        )
    return GameStatus.FINAL, int(home_score), int(away_score)


def _ls_team_by_score(ls: dict, home_score: int, away_score: int,
                      game_id: str) -> tuple[dict, dict]:
    """LiveStats team rows carry no home/away marker; map by score."""
    pairs = []
    for k in ("1", "2"):
        t = (ls.get("teams") or {}).get(k) or {}
        score = t.get("score")
        if score is None:
            raise BSLDataError(f"game {game_id}: LiveStats row {k} has no score")
        pairs.append((int(score), t))
    by_score = {s: t for s, t in pairs}
    if len(by_score) != 2:
        raise BSLDataError(
            f"game {game_id}: LiveStats scores do not map uniquely "
            f"({home_score}-{away_score})"
        )
    home = by_score.get(home_score)
    away = by_score.get(away_score)
    if home is None or away is None:
        raise BSLDataError(
            f"game {game_id}: schedule score {home_score}-{away_score} != "
            f"LiveStats scores {sorted(by_score)}"
        )
    return home, away


def _int(t: dict, key: str) -> int:
    v = t.get(key)
    if v is None:
        raise BSLDataError(f"missing {key} in LiveStats row")
    return int(v)


def _line_from_ls(t: dict, points: int, minutes: int) -> dict:
    fg = _int(t, "tot_sFieldGoalsMade")
    fga = _int(t, "tot_sFieldGoalsAttempted")
    fg3 = _int(t, "tot_sThreePointersMade")
    fg3a = _int(t, "tot_sThreePointersAttempted")
    ft = _int(t, "tot_sFreeThrowsMade")
    fta = _int(t, "tot_sFreeThrowsAttempted")
    orb = _int(t, "tot_sReboundsOffensive")
    drb = _int(t, "tot_sReboundsDefensive")
    trb_source = _int(t, "tot_sReboundsTotal")
    line = {
        "minutes": minutes,
        "points": points,
        "fg": fg, "fga": fga, "fg3": fg3, "fg3a": fg3a, "ft": ft, "fta": fta,
        "orb": orb, "drb": drb, "trb": trb_source,
        "ast": _int(t, "tot_sAssists"),
        "tov": _int(t, "tot_sTurnovers"),
        "stl": _int(t, "tot_sSteals"),
        "blk": _int(t, "tot_sBlocks"),
        "pf": _int(t, "tot_sFoulsPersonal"),
    }
    # source identity checks (fail loud — LiveStats is the official record)
    if points != 2 * (fg - fg3) + 3 * fg3 + ft:
        raise BSLDataError(
            f"points identity broken: {points} != 2*{fg - fg3}+3*{fg3}+{ft}"
        )
    if trb_source != orb + drb:
        raise BSLDataError(f"rebounds identity broken: {trb_source} != {orb}+{drb}")
    if fga < fg3a or fg < fg3 or fta < ft or fga < fg:
        raise BSLDataError("attempted/made consistency broken (fga<fg3a etc.)")
    line["possessions"] = round(fga + 0.44 * fta - orb + line["tov"], 1)
    return line


def parse_bsl_bundle(bundle: dict) -> dict:
    if bundle.get("dataset") != DATASET_MARKER:
        raise BSLDataError(
            f"not a BSL bundle (expected dataset={DATASET_MARKER!r})"
        )
    season = _season_label(bundle.get("season", ""))
    year = int(season.split("-")[0])
    livestats: dict = bundle.get("livestats") or {}
    tbf_boxes: dict = bundle.get("tbf_boxes") or {}
    if not livestats and not tbf_boxes:
        raise BSLDataError("bundle contains no box-score payloads")
    schedule: list[dict] = bundle.get("schedule") or []
    if not schedule:
        raise BSLDataError("bundle contains no schedule rows")

    schedule = sorted(schedule, key=lambda r: (str(r.get("matchDate")), int(r["matchId"])))

    games: list[Game] = []
    team_stats: list[TeamGameStats] = []
    teams: dict[str, Team] = {}
    team_ls_codes: dict[str, set[str]] = {}
    skipped_no_score = 0
    excluded_defects: list[int] = []
    box_counts = {"livestats": 0, "tbf-native": 0}
    minutes_drifted = 0
    estimated = 0
    ot_games = 0

    for row in schedule:
        game_seq = len(games) + 1
        game_id = f"BSL{year % 100:02d}-{game_seq:04d}"
        competition = _competition_for(row.get("week"))
        status, home_score, away_score = _status_and_scores(row)
        if status is None:
            skipped_no_score += 1
            continue

        genius_id = row.get("genuisId")
        match_id = int(row.get("matchId"))
        ls = livestats.get(str(genius_id)) if genius_id not in (None, "") else None
        box_source = "livestats"
        if ls is None:
            # seasons without LiveStats ids (e.g. 2024-25): TBF-native boxes
            tbf_box = tbf_boxes.get(str(match_id))
            if tbf_box is not None:
                ls = tbf_box
                box_source = "tbf-native"
        if ls is None:
            raise BSLDataError(
                f"game {game_id}: FINAL match {match_id} has no fetched box "
                "payload (refetch with --resume)"
            )
        box_counts[box_source] += 1

        try:
            ls_home, ls_away = _ls_team_by_score(ls, home_score, away_score,
                                                 game_id)
            probe_minutes = _parse_minutes(ls_home.get("tot_sMinutes"))
            if probe_minutes < _REGULATION_TEAM_MINUTES - _MINUTES_TOLERANCE:
                raise BSLDataError(
                    f"game {game_id}: incomplete LiveStats feed "
                    f"({probe_minutes} team minutes < regulation)"
                )
        except BSLDataError:
            if match_id in KNOWN_SOURCE_DEFECTS:
                excluded_defects.append(match_id)
                continue
            raise

        home_t = (row.get("homeTeam") or {}).get("id")
        away_t = (row.get("awayTeam") or {}).get("id")
        # continuity map is keyed by the ROW's own season (a bundle is
        # normally single-season; the row season is authoritative)
        row_season = _season_label(row["season"]) if row.get("season") else season
        home_id, away_id = resolve_team(home_t, row_season), resolve_team(away_t, row_season)

        # two-source rule: TBF schedule vs LiveStats box (independent feeds)
        if int(ls_home.get("score")) != home_score or int(ls_away.get("score")) != away_score:
            raise BSLDataError(
                f"game {game_id}: schedule score {home_score}-{away_score} != "
                f"LiveStats score {ls_home.get('score')}-{ls_away.get('score')}"
            )

        minutes = _parse_minutes(ls_home.get("tot_sMinutes"))
        ot_count = round((minutes - _REGULATION_TEAM_MINUTES) / _OT_TEAM_MINUTES)
        if ot_count < 0:
            raise BSLDataError(
                f"game {game_id}: {minutes} team minutes shorter than regulation"
            )
        expected = _REGULATION_TEAM_MINUTES + _OT_TEAM_MINUTES * ot_count
        if abs(minutes - expected) > _MINUTES_TOLERANCE:
            raise BSLDataError(
                f"game {game_id}: {minutes} team minutes for ~{ot_count} OT "
                f"(expected ~{expected})"
            )
        if float(minutes) != expected:
            minutes_drifted += 1
        has_uzatma = bool(row.get("hasUzatma"))
        if has_uzatma != (ot_count > 0):
            raise BSLDataError(
                f"game {game_id}: hasUzatma={has_uzatma} but minutes imply "
                f"{ot_count} OT (extend rules only from real evidence)"
            )

        date_text = str(row.get("matchDate") or "")[:19]
        game_date = datetime.strptime(date_text, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_TR_TIME)

        games.append(Game(
            game_id=game_id, league=League.BSL, season=season,
            game_date=game_date, home_team_id=home_id, away_team_id=away_id,
            home_score=home_score, away_score=away_score,
            status=GameStatus.FINAL, competition=competition,
        ))
        if ot_count:
            ot_games += 1
        team_minutes = round(minutes)
        for team_id, ls_team, is_home in ((home_id, ls_home, True),
                                          (away_id, ls_away, False)):
            line = _line_from_ls(ls_team, int(ls_team["score"]), team_minutes)
            team_stats.append(TeamGameStats(
                game_id=game_id, team_id=team_id, is_home=is_home, **line,
            ))
            estimated += 1
        for team_id, ls_team in ((home_id, ls_home), (away_id, ls_away)):
            code = str(ls_team.get("code") or "").strip()
            if code:
                team_ls_codes.setdefault(team_id, set()).add(code)

    for row in schedule:
        for side in ("homeTeam", "awayTeam"):
            t = row.get(side) or {}
            if t.get("id") in (None, ""):
                continue
            row_season = _season_label(row["season"]) if row.get("season") else season
            stored = resolve_team(t["id"], row_season)
            if stored not in teams:
                teams[stored] = Team(
                    team_id=stored, league=League.BSL,
                    name=str(t.get("name") or "").strip(),
                    abbreviation=stored,
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
            "n_regular": sum(1 for g in games
                             if g.competition is Competition.REGULAR_SEASON),
            "n_playoff": sum(1 for g in games if g.competition is Competition.PLAYOFF),
            "n_skipped_no_score": skipped_no_score,
            "n_excluded_source_defects": len(excluded_defects),
            "excluded_source_defects": [
                {"matchId": mid, "reason": KNOWN_SOURCE_DEFECTS[mid]}
                for mid in excluded_defects
            ],
            "n_box_livestats": box_counts["livestats"],
            "n_box_tbf_native": box_counts["tbf-native"],
            "n_overtime_games": ot_games,
            "team_minutes_drifted": minutes_drifted,
            "possessions_estimated_for": estimated,
            "ls_codes_per_team": {k: sorted(v) for k, v in team_ls_codes.items()},
            "season": season,
            "source": bundle.get("source"),
        },
    }


def load_bsl_bundle_file(path: Path | str) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"BSL bundle not found: {path}")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BSLDataError(f"bundle is not valid JSON: {exc}") from exc
    return parse_bsl_bundle(bundle)
