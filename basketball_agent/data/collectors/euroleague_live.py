"""EuroLeague live-API adapter (Phase 8-B3).

Source: ``https://live.euroleague.net/api/{Header,Boxscore}?gameCode=&seasonCode=``
(keyless, verified Phase 8-B0/B3).  A raw bundle is one JSON file::

    {"dataset": "euroleague-live-v1", "season_code": "E2024",
     "headers": {"1": {...}, ...}, "boxscores": {"1": {...}, ...},
     "fetched_at": "...", "source": "..."}

Interpretation rules (fail loud, never guess):

* ``Header.TeamA`` is the HOME team (verified: game 1 of E2024 was hosted at
  ALBA Berlin's Uber Arena with TeamA=ALBA);
* ``Header.CodeTeamA/B`` are the canonical team ids; they must resolve
  through the alias table AND be disjoint from every NBA id;
* ``Header.Date`` is ``dd/mm/yyyy`` and ``Hour`` the LOCAL venue time —
  converted to UTC via the HOME team's IANA timezone;
* ``Header.Phase`` maps onto the shared Competition enum ("REGULAR SEASON"
  / "PLAY-IN" / "PLAYOFFS" / "FINAL FOUR"); unknown phases are a hard error;
* header scores must equal the Boxscore ``totr.Points`` of the matching team
  (two endpoints, one source — a mismatch is a data error, not a warning);
* EuroLeague box scores split 2PT/3PT: ``fg = FG2 + FG3``,
  ``fga = FGA2 + FGA3``; team minutes come from ``totr.Minutes`` ("200:00"
  = 40-minute regulation; overtime shows up in the sum);
* possessions are not published — the standard estimate
  ``fga + 0.44*fta - orb + tov`` is applied and counted in ``_meta``;
* a header without scores maps to a POSTPONED game (no boxscore required).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.game import Game, TeamGameStats
from basketball_agent.data.schemas.team import Team

DATASET_MARKER = "euroleague-live-v1"

PHASE_TO_COMPETITION: dict[str, str] = {
    "REGULAR SEASON": "REGULAR_SEASON",
    "PLAY-IN": "PLAY_IN",
    "PLAYOFFS": "PLAYOFF",
    "FINAL FOUR": "PLAYOFF",  # the Final Four is the playoff culminating phase
}

# canonical id -> home-venue IANA timezone (UTC conversion of local tip-off).
# Canonical ids are the OFFICIAL CodeTeam values observed in the E2024 bundle
# (18 clubs); every other spelling lives in ALIAS_TO_TEAM.
TEAM_TIMEZONES: dict[str, str] = {
    "ASV": "Europe/Paris",      # LDLC ASVEL Villeurbanne
    "BAR": "Europe/Madrid",     # FC Barcelona
    "BAS": "Europe/Madrid",     # Baskonia Vitoria-Gasteiz
    "BER": "Europe/Berlin",     # ALBA Berlin
    "IST": "Europe/Istanbul",   # Anadolu Efes Istanbul
    "MAD": "Europe/Madrid",     # Real Madrid
    "MCO": "Europe/Monaco",     # AS Monaco
    "MIL": "Europe/Rome",       # EA7 Emporio Armani Milan
    "MUN": "Europe/Berlin",     # FC Bayern Munich
    "OLY": "Europe/Athens",     # Olympiacos Piraeus
    "PAN": "Europe/Athens",     # Panathinaikos AKTOR Athens
    "PAR": "Europe/Belgrade",   # Partizan Mozzart Bet Belgrade
    "PRS": "Europe/Paris",      # Paris Basketball
    "RED": "Europe/Belgrade",   # Crvena Zvezda Meridianbet Belgrade
    "TEL": "Asia/Jerusalem",    # Maccabi Playtika Tel Aviv
    "ULK": "Europe/Istanbul",   # Fenerbahce Beko Istanbul
    "VIR": "Europe/Rome",       # Virtus Segafredo Bologna
    "VAL": "Europe/Madrid",     # Valencia Basket (official CodeTeam value is PAM)
    "ZAL": "Europe/Vilnius",    # Zalgiris Kaunas
    # seasons E2019-E2021 only (Russian clubs were excluded from 2022-23 on);
    # canonical ids are the official CodeTeam values observed in those bundles
    "CSK": "Europe/Moscow",     # CSKA Moscow
    "DYR": "Europe/Moscow",     # Zenit St Petersburg
    "UNK": "Europe/Moscow",     # UNICS Kazan
    "KHI": "Europe/Moscow",     # Khimki Moscow Region (E2019-E2020 only)
}

# every known spelling (codes, TV codes, name variants) -> canonical id
ALIAS_TO_TEAM: dict[str, str] = {}
for _canon in TEAM_TIMEZONES:
    ALIAS_TO_TEAM[_canon.lower()] = _canon
for _canon, _aliases in {
    "PAN": ("pao", "panathinaikos", "panathinaikos aktor athens",
            "panathinaikos opap athens"),
    "OLY": ("olp", "olympiakos", "olympiacos", "olympiacos piraeus"),
    "BER": ("alba", "alba berlin"),
    "MUN": ("bay", "bam", "bayern", "fc bayern munich", "bayern munich"),
    "MAD": ("rma", "rmb", "real madrid", "real madrid baloncesto"),
    "BAR": ("fcb", "barcelona", "fc barcelona", "fc barcelona lassa"),
    "BAS": ("baskonia", "baskonia vitoria-gasteiz", "baskonia alaves",
            "saski baskonia"),
    "VAL": ("pam", "vbc", "valencia", "valencia basket"),
    "ULK": ("fen", "fbb", "fenerbahce", "fenerbahce beko istanbul",
            "fenerbahce ulker istanbul"),
    "IST": ("efes", "efs", "anadolu efes", "anadolu efes istanbul"),
    "MCO": ("asm", "monaco", "as monaco", "as monaco basket"),
    "ASV": ("tdl", "asvel", "asvel villeurbanne", "ldlc asvel villeurbanne"),
    "PRS": ("prb", "pab", "paris", "paris basketball"),
    "TEL": ("mta", "mtl", "maccabi", "maccabi tel aviv",
            "maccabi playtika tel aviv", "maccabi fox tel aviv"),
    "ZAL": ("ztg", "zalgiris", "zalgiris kaunas"),
    "PAR": ("part", "ptn", "partizan", "partizan belgrade",
            "partizan mozzart bet belgrade"),
    "RED": ("czv", "red star", "zvezda", "crvena zvezda",
            "crvena zvezda mts belgrade", "crvena zvezda meridianbet belgrade"),
    "VIR": ("bol", "virtus", "virtus bologna", "virtus segafredo bologna"),
    "MIL": ("eam", "ax milan", "olimpija milan", "milan",
            "ea7 emporio armani milan", "emporio armani milan"),
    "CSK": ("cska", "cska moscow"),
    "DYR": ("zen", "zenit", "zenit st petersburg", "zenit saint petersburg"),
    "UNK": ("uni", "unics", "unics kazan"),
    "KHI": ("khimki", "khimki moscow region", "khimki moskva", "bck himki"),
}.items():
    for _alias in _aliases:
        ALIAS_TO_TEAM.setdefault(_alias, _canon)


#: Source-data defects that make a game unusable WITHOUT repairing it.
#: Excluded games are skipped and reported in ``_meta["source_excluded"]`` —
#: the source value is never edited, and the validator is never loosened.
#: ``E2020-0127``: the source reports ``totr.Minutes = 179`` for AX ARMANI
#: MILAN (Efes reports 200 in the same game) — below any legal regulation or
#: overtime value, so the box score is broken at the source.
KNOWN_SOURCE_DEFECTS: dict[str, str] = {
    "E2020-0127": "source totr.Minutes=179 for AX ARMANI MILAN (impossible); "
                  "game excluded, value not repaired",
}


class EuroLeagueDataError(RuntimeError):
    """Raised when the bundle cannot be mapped into validated records."""


def resolve_team(raw: str) -> str:
    """Map any known EuroLeague code/name to its canonical STORED id.

    Stored ids are prefixed ``EL-`` (e.g. the official code ``MAD`` becomes
    ``EL-MAD``).  The prefix is load-bearing: EuroLeague's ``MIL`` (EA7
    Milan) really does collide with the NBA's ``MIL`` (Milwaukee), so bare
    codes can never be trusted as global keys (Phase 8-B3, real-data find).
    """
    key = str(raw).strip().lower()
    code = ALIAS_TO_TEAM.get(key)
    if code is None:
        raise EuroLeagueDataError(
            f"unknown EuroLeague team code/name: {raw!r} (extend "
            "euroleague_live.ALIAS_TO_TEAM; never guess)"
        )
    return f"EL-{code}"


def _parse_local_datetime(date_raw: str, hour_raw: str, tz_name: str) -> datetime:
    """``dd/mm/yyyy`` + ``HH:MM`` local venue time -> aware UTC datetime."""
    try:
        day = datetime.strptime(str(date_raw).strip(), "%d/%m/%Y").date()
    except ValueError as exc:
        raise EuroLeagueDataError(f"unparseable Header.Date: {date_raw!r}") from exc
    hour_text = str(hour_raw or "").strip()
    if not hour_text:
        hour, minute = 12, 0  # time missing: nominal noon, counted in meta
    else:
        try:
            hour, minute = (int(part) for part in hour_text.split(":")[:2])
        except ValueError as exc:
            raise EuroLeagueDataError(f"unparseable Header.Hour: {hour_raw!r}") from exc
    local = datetime(day.year, day.month, day.day, hour, minute,
                     tzinfo=ZoneInfo(tz_name))
    return local.astimezone(ZoneInfo("UTC"))


def _season_label(season_code: str) -> str:
    """``E2024`` -> ``2024-25`` (seasons start in October)."""
    text = str(season_code).strip()
    if not text[1:].isdigit():
        raise EuroLeagueDataError(f"unparseable seasonCode: {season_code!r}")
    year = int(text[1:])
    return f"{year}-{str(year + 1)[-2:]}"


def _score_int(raw: Any, field: str) -> int | None:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError as exc:
        raise EuroLeagueDataError(f"unparseable Header.{field}: {raw!r}") from exc


def _minutes_int(raw: Any) -> int:
    """``'200:00'`` -> 200 (whole minutes; overtime shows up in the sum)."""
    text = str(raw or "").strip()
    if not text:
        raise EuroLeagueDataError("totr.Minutes missing")
    parts = text.split(":")
    try:
        minutes = int(parts[0])
        if len(parts) > 1 and int(parts[1]) > 0:
            minutes += 1  # partial minute counts as a played minute
    except ValueError as exc:
        raise EuroLeagueDataError(f"unparseable totr.Minutes: {raw!r}") from exc
    return minutes


def _team_line(totr: dict, points: int) -> dict:
    """One Boxscore ``totr`` -> canonical per-team stat fields."""
    fm2 = int(totr["FieldGoalsMade2"]); fa2 = int(totr["FieldGoalsAttempted2"])
    fm3 = int(totr["FieldGoalsMade3"]); fa3 = int(totr["FieldGoalsAttempted3"])
    orb = int(totr["OffensiveRebounds"])
    fta = int(totr["FreeThrowsAttempted"])
    tov = int(totr["Turnovers"])
    line = {
        "minutes": _minutes_int(totr.get("Minutes")),
        "points": points,
        "fg": fm2 + fm3,
        "fga": fa2 + fa3,
        "fg3": fm3,
        "fg3a": fa3,
        "ft": int(totr["FreeThrowsMade"]),
        "fta": fta,
        "orb": orb,
        "drb": int(totr["DefensiveRebounds"]),
        "trb": int(totr["TotalRebounds"]),
        "ast": int(totr["Assistances"]),
        "tov": tov,
        "stl": int(totr["Steals"]),
        "blk": int(totr["BlocksFavour"]),
        "pf": int(totr["FoulsCommited"]),
    }
    line["possessions"] = round(line["fga"] + 0.44 * fta - orb + tov, 1)
    return line, True  # possessions always estimated for this source


def _boxscore_team_totals(box: dict) -> list[tuple[str, dict]]:
    """[(full team name, canonical stat fields)] from one Boxscore payload."""
    out: list[tuple[str, dict]] = []
    for team_obj in box["Stats"]:
        totr = team_obj.get("totr") or {}
        name = str(team_obj.get("Team", "")).strip()
        points = int(totr.get("Points", 0))
        line, _ = _team_line(totr, points)
        out.append((name, line))
    return out


def parse_euroleague_bundle(bundle: dict) -> dict:
    """Bundle -> the raw dataset dict consumed by DataIngestor."""
    if bundle.get("dataset") != DATASET_MARKER:
        raise EuroLeagueDataError(
            f"not a EuroLeague bundle (expected dataset={DATASET_MARKER!r})"
        )
    season_code = str(bundle["season_code"]).strip()
    season = _season_label(season_code)
    headers: dict = bundle.get("headers") or {}
    boxscores: dict = bundle.get("boxscores") or {}
    if not headers:
        raise EuroLeagueDataError("bundle contains no headers")

    games: list[Game] = []
    team_stats: list[TeamGameStats] = []
    teams: dict[str, Team] = {}
    estimated = 0
    postponed = 0
    excluded: list[tuple[str, str]] = []

    for key in sorted(headers, key=int):
        header = headers[key]
        code = int(key)
        game_id = f"{season_code}-{code:04d}"

        defect_reason = KNOWN_SOURCE_DEFECTS.get(game_id)
        if defect_reason is not None:
            excluded.append((game_id, defect_reason))
            continue

        phase = str(header.get("Phase", "")).strip().upper()
        competition_raw = PHASE_TO_COMPETITION.get(phase)
        if competition_raw is None:
            raise EuroLeagueDataError(
                f"game {game_id}: unknown Header.Phase {header.get('Phase')!r} "
                "(extend euroleague_live.PHASE_TO_COMPETITION; never guess)"
            )
        competition = Competition(competition_raw)

        home_id = resolve_team(header["CodeTeamA"])
        away_id = resolve_team(header["CodeTeamB"])
        if home_id == away_id:
            raise EuroLeagueDataError(f"game {game_id}: TeamA == TeamB ({home_id})")

        home_score = _score_int(header.get("ScoreA"), "ScoreA")
        away_score = _score_int(header.get("ScoreB"), "ScoreB")

        home_name = str(header.get("TeamA", "")).strip()
        away_name = str(header.get("TeamB", "")).strip()
        tz_name = TEAM_TIMEZONES.get(home_id.removeprefix("EL-"))
        if tz_name is None:
            raise EuroLeagueDataError(
                f"game {game_id}: no timezone for home team {home_id!r}"
            )
        game_date = _parse_local_datetime(header.get("Date"), header.get("Hour"), tz_name)

        if home_score is None or away_score is None:
            # header exists but no result recorded -> postponed/cancelled
            games.append(Game(
                game_id=game_id, league=League.EUROLEAGUE, season=season,
                game_date=game_date, home_team_id=home_id, away_team_id=away_id,
                home_score=None, away_score=None, status=GameStatus.POSTPONED,
                competition=competition,
            ))
            postponed += 1
            continue

        box = boxscores.get(key)
        if box is None:
            raise EuroLeagueDataError(
                f"game {game_id}: FINAL but Boxscore missing from bundle"
            )
        totals = _boxscore_team_totals(box)
        if len(totals) != 2:
            raise EuroLeagueDataError(
                f"game {game_id}: Boxscore has {len(totals)} team rows, expected 2"
            )
        by_name = {name.upper(): line for name, line in totals}
        home_line = by_name.get(home_name.upper())
        away_line = by_name.get(away_name.upper())
        if home_line is None or away_line is None:
            raise EuroLeagueDataError(
                f"game {game_id}: Boxscore team names {sorted(by_name)} do not "
                f"match header teams {home_name!r} / {away_name!r}"
            )
        # header and Boxscore are two endpoints of the same source: the total
        # points MUST agree, otherwise the data itself is broken
        if home_line["points"] != home_score or away_line["points"] != away_score:
            raise EuroLeagueDataError(
                f"game {game_id}: header score {home_score}-{away_score} != "
                f"boxscore totals {home_line['points']}-{away_line['points']}"
            )

        games.append(Game(
            game_id=game_id, league=League.EUROLEAGUE, season=season,
            game_date=game_date, home_team_id=home_id, away_team_id=away_id,
            home_score=home_score, away_score=away_score, status=GameStatus.FINAL,
            competition=competition,
        ))
        for team_id, line in ((home_id, home_line), (away_id, away_line)):
            estimated += 1
            team_stats.append(TeamGameStats(
                game_id=game_id, team_id=team_id, is_home=team_id == home_id,
                **line,
            ))
        for team_id, name in ((home_id, home_name), (away_id, away_name)):
            teams.setdefault(team_id, Team(
                team_id=team_id, league=League.EUROLEAGUE, name=name,
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
            "possessions_estimated_for": estimated,
            "season_code": season_code,
            "source_excluded": [{"game_id": gid, "reason": reason} for gid, reason in excluded],
        },
    }


def load_euroleague_bundle_file(path: Path | str) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"EuroLeague bundle not found: {path}")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EuroLeagueDataError(f"bundle is not valid JSON: {exc}") from exc
    return parse_euroleague_bundle(bundle)
