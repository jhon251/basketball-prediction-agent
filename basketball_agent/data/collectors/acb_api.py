"""ACB (Liga Endesa) official-API adapter (Phase 8-B6).

Source: ``https://api2.acb.com`` — the API that acb.com and live.acb.com use
themselves (verified against real 2025-26 responses).  A raw bundle is one
JSON file::

    {"dataset": "acb-api-v1", "competition_id": 1, "competition_name": "Liga Endesa",
     "edition_id": 90, "season": "2025-26", "season_code": "ACB2025",
     "source": "https://api2.acb.com", "fetched_at": "...",
     "n_calendar_matches": 327, "n_headers": N, "n_boxscores": M,
     "calendar_root": {...}, "calendar": {...},
     "headers": {"104459": {...}}, "boxscores": {"104459": {...}}}

Interpretation rules (fail loud, never guess):

* the official **game id** is the source ``matchId``; stored ids are
  ``ACB<seasonStartYear>-<matchId>`` (e.g. ``ACB2025-104459``), mirroring the
  EuroLeague ``E<year>-<gameCode>`` convention;
* **teams** are keyed on the stable ``clubId`` (sponsor names change, club ids
  do not) and stored as ``ACB-<official code>`` (``ACB-UNI``, ``ACB-BAR``, …).
  Because ACB clubs rename with their sponsor (Baskonia: KBA → TDS → BKN;
  Tenerife: IBT → LNT → LLT; Joventut: CJB → JOV), every historical code
  observed in the real payloads is recorded in ``TEAM_CODE_ALIASES`` and maps
  onto the club's canonical code, so one club keeps one identity across
  seasons (multi-season Elo/strength continuity).  A clubId whose code is
  neither canonical nor a recorded alias is a hard error — the table is
  evidence-based, never guessed — and each alias actually used is reported in
  ``_meta.team_code_aliases_used``;
* **home/away** comes from the calendar (``homeTeamId``) and is cross-checked
  against ``MatchHeader.teams.home.id`` and the boxscore team ids: three
  independent statements must agree (the contract forbids guessing the venue
  orientation);
* ``MatchHeader.start`` / calendar ``startDateTime`` are **UTC** (``…Z``); the
  value is stored verbatim and a non-UTC timestamp is rejected.  Per-club venue
  timezones are recorded for reference/audit only (the Canary Island clubs are
  ``Atlantic/Canary``, the rest ``Europe/Madrid``);
* **competition**: rounds without ``subphaseId`` are the 34-round regular
  season; rounds with a subphase (291 final / 292 semifinal / 293 quarterfinal)
  are playoffs;
* **round labels are not a time axis**: the ACB calendar schedules rounds with
  different dates (and plays rescheduled games weeks later), so 92 of the 327
  matches carry a lower round number than a match played before them.  The
  adapter records that count in ``_meta.matches_out_of_round_order`` and the
  pipeline always sorts by actual tip-off ``(game_date, game_id)`` — round
  order is never used as a temporal key;
* **team totals** come from the boxscore's period-0 ``stats.total`` (the
  official game totals, which include the team-only row ``stats.team``).
  Every total is cross-checked against ``players + team`` row sums, the
  scoring identity ``points == 2*2PM + 3*3PM + FTM`` and ``trb == orb + drb``;
  a mismatch is a data error, not a warning;
* **minutes** are not published at team level: they are the exact sum of the
  players' ``playTime`` (``MM:SS``), which for this source lands on
  ``200 + 25*OT`` every time — that identity is enforced;
* **overtime** is detected from the number of played periods (period 0 is the
  game total, periods 1..N are the played quarters) and cross-checked against
  ``MatchHeader.quarterScores``; the quarter scores must sum to the final score;
* **possessions** are not published: the standard estimate
  ``fga + 0.44*fta - orb + tov`` is applied and counted in ``_meta``;
* a FINALIZED match without a boxscore in the bundle, or with a boxscore that
  does not contain exactly the two expected teams, is a hard error (nothing is
  silently dropped).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.game import Game, TeamGameStats
from basketball_agent.data.schemas.team import Team

DATASET_MARKER = "acb-api-v1"

#: Source-data defects that make a game unusable WITHOUT repairing it
#: (Phase 8-B4 precedent, mirrored from euroleague_live).  Excluded games are
#: skipped and reported in ``_meta["source_excluded"]`` — the source value is
#: never edited and the validator is never loosened.
#:
#: ``ACB2021-102902`` (edition 86, 2021-22): ACB-BUR's 11 published player
#: playTime rows sum to 13181s (219.7min) in a game the header's quarter
#: scores confirm was 4 periods (200min); the opponent's rows sum exactly.
#: One player row over-counts at the source — a repair would be a guess.
KNOWN_SOURCE_DEFECTS: dict[str, str] = {
    "ACB2021-102902": "ACB-BUR player playTime sums to 13181s for a 4-period "
                      "game (12000s expected; source row over-counts); "
                      "game excluded, value not repaired",
}

#: ``clubId`` -> canonical ACB club code.  Taken from the official payloads
#: (``abbreviatedName``), keyed on the stable club id: the display name carries
#: the sponsor (``Asisa Joventut``) and changes, the club does not.
CLUB_ID_TO_TEAM: dict[int, str] = {
    2: "BAR",    # FC Barcelona
    3: "BKN",    # Saski Baskonia
    4: "SBB",    # Surne Bilbao Basket
    5: "DGC",    # Dreamland Gran Canaria
    6: "MOV",    # Movistar Estudiantes (2019-20, 2020-21)
    7: "GBC",    # Acunsa GBC (2020-21)
    8: "JOV",    # Joventut Badalona
    9: "RMB",    # Real Madrid
    10: "MAN",   # Baxi Manresa
    12: "UCM",   # UCAM Murcia
    13: "VBC",   # Valencia Basket
    14: "UNI",   # Unicaja
    15: "BET",   # Real Betis Baloncesto
    16: "CAZ",   # Casademont Zaragoza
    17: "FUE",   # Baloncesto Fuenlabrada
    22: "MBA",   # MoraBanc Andorra
    25: "BRE",   # Río Breogán
    28: "LLT",   # La Laguna Tenerife
    57: "MOB",   # Monbus Obradoiro
    549: "BUR",  # San Pablo Burgos
    591: "GIR",  # Girona
    592: "COV",  # Covirán Granada
    624: "ZPA",  # Zunder Palencia (2023-24)
    657: "COR",  # Leyma Coruña (2024-25)
    658: "ILE",  # Hiopos Lleida
}

#: Historical (sponsor-driven) club codes observed in the real payloads for
#: ACB 2019-20 .. 2025-26, per club id.  They resolve to the canonical code
#: above so a club keeps ONE identity across seasons (Elo / strength features
#: depend on that continuity).  A code that is neither the canonical one nor a
#: recorded alias for THAT club is still a hard error.
TEAM_CODE_ALIASES: dict[int, tuple[str, ...]] = {
    3: ("KBA", "TDS"),        # Kirolbet / TD Systems Baskonia
    4: ("RBB",),              # RETAbet Bilbao Basket
    5: ("HGC", "GCA"),        # Herbalife / Gran Canaria
    8: ("CJB",),              # Club Joventut Badalona
    10: ("BAX",),             # BAXI Manresa
    12: ("UCB",),             # UCAM Murcia CB
    17: ("MKF", "URF"),       # Montakit / Urbas Fuenlabrada
    28: ("IBT", "LNT"),       # Iberostar / Lenovo Tenerife
    549: ("SPB",),            # San Pablo Burgos
    658: ("HIO",),            # Hiopos Lleida (2024-25)
}

#: canonical code -> sponsor-free club name (registry display name)
TEAM_NAMES: dict[str, str] = {
    "BAR": "FC Barcelona", "BKN": "Saski Baskonia", "SBB": "Surne Bilbao Basket",
    "DGC": "Dreamland Gran Canaria", "MOV": "Movistar Estudiantes",
    "GBC": "Acunsa GBC", "JOV": "Joventut Badalona",
    "RMB": "Real Madrid", "MAN": "Baxi Manresa", "UCM": "UCAM Murcia",
    "VBC": "Valencia Basket", "UNI": "Unicaja", "BET": "Real Betis Baloncesto",
    "CAZ": "Casademont Zaragoza", "FUE": "Baloncesto Fuenlabrada",
    "MBA": "MoraBanc Andorra", "BRE": "Río Breogán",
    "LLT": "La Laguna Tenerife", "MOB": "Monbus Obradoiro",
    "BUR": "San Pablo Burgos", "GIR": "Girona",
    "COV": "Covirán Granada", "ZPA": "Zunder Palencia",
    "COR": "Leyma Coruña", "ILE": "Hiopos Lleida",
}

#: venue IANA timezone per club.  The ACB API publishes UTC, so this table is
#: documentation/audit only — it is NOT used to convert timestamps.
TEAM_TIMEZONES: dict[str, str] = {
    code: ("Atlantic/Canary" if code in ("DGC", "LLT") else "Europe/Madrid")
    for code in TEAM_NAMES
}

#: playoff subphase ids observed in the real 2025-26 calendar (291 final,
#: 292 semifinal, 293 quarterfinal).  Any other subphase is a hard error until
#: it is evidenced.
PLAYOFF_SUBPHASES: dict[int, str] = {
    291: "FINAL",
    292: "SEMIFINAL",
    293: "QUARTERFINAL",
}

#: source status -> unified status.  Only FINALIZED is exercised by the
#: 2025-26 season (327/327 matches); the other branches are covered by unit
#: tests with synthetic payloads.
STATUS_MAP: dict[str, GameStatus] = {
    "FINALIZED": GameStatus.FINAL,
    "NOT_STARTED": GameStatus.SCHEDULED,
    "NOT_SCHEDULED": GameStatus.SCHEDULED,
    "STARTED": GameStatus.LIVE,
    "PAUSED": GameStatus.LIVE,
    "POSTPONED": GameStatus.POSTPONED,
    "SUSPENDED": GameStatus.POSTPONED,
    "CANCELED": GameStatus.CANCELLED,
    "CANCELLED": GameStatus.CANCELLED,
}

#: team-stat fields copied straight from the official period-0 ``stats.total``
_FIELD_MAP = {
    "points": "points",
    "ft": "freeThrowsMade",
    "fta": "freeThrowsAttempted",
    "fg3": "threePointersMade",
    "fg3a": "threePointersAttempted",
    "orb": "offRebounds",
    "drb": "defRebounds",
    "trb": "totalRebounds",
    "ast": "assists",
    "tov": "turnovers",
    "stl": "steals",
    "blk": "blocks",
    "pf": "personalFouls",
}

#: the identity checks run against every team total (period-0 ``total`` vs the
#: sum of the player rows and the team-only row)
_SUMMED_FIELDS = (
    "points", "freeThrowsMade", "freeThrowsAttempted", "twoPointersMade",
    "twoPointersAttempted", "threePointersMade", "threePointersAttempted",
    "assists", "offRebounds", "defRebounds", "totalRebounds", "steals",
    "turnovers", "blocks", "personalFouls",
)


class ACBDataError(RuntimeError):
    """Raised when an ACB bundle cannot be mapped into validated records."""


def resolve_team(club_id: Any) -> str:
    """Map an ACB ``clubId`` to its canonical STORED id (``ACB-<CODE>``)."""
    try:
        key = int(club_id)
    except (TypeError, ValueError) as exc:
        raise ACBDataError(f"unparseable ACB clubId: {club_id!r}") from exc
    code = CLUB_ID_TO_TEAM.get(key)
    if code is None:
        raise ACBDataError(
            f"unknown ACB clubId {key} (extend acb_api.CLUB_ID_TO_TEAM from a "
            "real payload; never guess)"
        )
    return f"ACB-{code}"


def _season_label(start_year: int) -> str:
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def _parse_utc(raw: Any, field: str) -> datetime:
    """Source timestamps are UTC (``…Z``); anything else is rejected."""
    text = str(raw or "").strip()
    if not text:
        raise ACBDataError(f"missing {field}")
    if not (text.endswith("Z") or text.endswith("+00:00")):
        raise ACBDataError(
            f"{field}={raw!r} is not published as UTC; refusing to guess a "
            "venue timezone"
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ACBDataError(f"unparseable {field}: {raw!r}") from exc
    return parsed.astimezone(timezone.utc)


def _playtime_seconds(raw: Any) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError as exc:
        raise ACBDataError(f"unparseable playTime: {raw!r}") from exc
    raise ACBDataError(f"unparseable playTime: {raw!r}")


def _team_totals(box: dict, team_id: int, game_id: str) -> tuple[dict, int]:
    """Official totals + derived minutes for one team of one boxscore.

    Returns ``(totals, played_periods)``.  Every published total is verified
    against the player rows and the team-only row, so a broken source row can
    never slip into the database as "our aggregation".
    """
    entry = None
    for candidate in box.get("teamBoxscores") or []:
        team = candidate.get("team") or {}
        if int(team.get("id") or -1) == int(team_id):
            entry = candidate
            break
    if entry is None:
        raise ACBDataError(f"game {game_id}: boxscore has no row for team {team_id}")

    periods = entry.get("statsByPeriods") or []
    first_quarter = periods[0].get("quarter") if periods else None
    if first_quarter is None or int(first_quarter) != 0:
        raise ACBDataError(
            f"game {game_id}: boxscore period 0 (game totals) missing for team {team_id}"
        )

    # Trailing EMPTY overtime slots are a source artifact (Phase 8-B7, real
    # find in edition 87: a 1-OT game ships an extra all-zero quarter=6 row
    # with 0 points and 00:00 playTime for every player).  A genuinely played
    # period always has points or on-court seconds for this team, so a
    # trailing all-zero row can never be a real period — drop it.
    while len(periods) > 1:
        last_stats = periods[-1].get("stats") or {}
        last_total = last_stats.get("total") or {}
        last_secs = sum(
            _playtime_seconds(p.get("playTime")) for p in (last_stats.get("players") or [])
        )
        if int(last_total.get("points") or 0) == 0 and last_secs == 0:
            periods = periods[:-1]
        else:
            break
    stats = periods[0].get("stats") or {}
    total = stats.get("total") or {}
    team_row = stats.get("team") or {}
    players = stats.get("players") or []
    if not total:
        raise ACBDataError(f"game {game_id}: empty team totals for team {team_id}")

    for field in _SUMMED_FIELDS:
        player_sum = sum(int(p.get(field) or 0) for p in players)
        team_sum = int(team_row.get(field) or 0)
        published = int(total.get(field) or 0)
        if player_sum + team_sum != published:
            raise ACBDataError(
                f"game {game_id}/team {team_id}: {field} players({player_sum}) + "
                f"team({team_sum}) != published total({published})"
            )

    seconds = sum(_playtime_seconds(p.get("playTime")) for p in players)
    if seconds == 0:
        raise ACBDataError(f"game {game_id}/team {team_id}: no player minutes")
    # The 2025-26 season sums to the exact expected playtime, but older
    # seasons carry noisy per-player playTime: 2020-21 observed team sums up
    # to +135s off the period-derived expectation (with exact-to-the-second
    # "MM:SS" rows, so this is source noise, not rounding).  Bounded
    # tolerance (±300s = 5 min); ``line["minutes"]`` carries the nearest
    # whole minute and every drifted team is counted in
    # ``_meta.team_minutes_drifted`` — never silently normalized.
    minutes = (seconds + 30) // 60
    if not 180 <= minutes <= 280:
        raise ACBDataError(
            f"game {game_id}/team {team_id}: player minutes sum to {seconds}s "
            f"({minutes}min) — outside plausible game length"
        )

    fg2m = int(total.get("twoPointersMade") or 0)
    fg2a = int(total.get("twoPointersAttempted") or 0)
    fg3m = int(total.get("threePointersMade") or 0)
    fg3a = int(total.get("threePointersAttempted") or 0)
    ftm = int(total.get("freeThrowsMade") or 0)
    if int(total.get("points") or 0) != 2 * fg2m + 3 * fg3m + ftm:
        raise ACBDataError(
            f"game {game_id}/team {team_id}: points != 2*2PM + 3*3PM + FTM"
        )
    if int(total.get("totalRebounds") or 0) != (
        int(total.get("offRebounds") or 0) + int(total.get("defRebounds") or 0)
    ):
        raise ACBDataError(f"game {game_id}/team {team_id}: trb != orb + drb")
    if fg2a < fg2m or fg3a < fg3m or int(total.get("freeThrowsAttempted") or 0) < ftm:
        raise ACBDataError(f"game {game_id}/team {team_id}: made > attempted")

    line = {key: int(total.get(src) or 0) for key, src in _FIELD_MAP.items()}
    line["fg"] = fg2m + fg3m
    line["fga"] = fg2a + fg3a
    line["minutes"] = minutes
    line["possessions"] = round(
        line["fga"] + 0.44 * line["fta"] - line["orb"] + line["tov"], 1
    )
    return line, len(periods) - 1, seconds


def _match_competition(round_obj: dict, game_id: str) -> Competition:
    subphase = round_obj.get("subphaseId")
    if subphase is None:
        return Competition.REGULAR_SEASON
    if int(subphase) not in PLAYOFF_SUBPHASES:
        raise ACBDataError(
            f"game {game_id}: unknown playoff subphaseId {subphase!r} "
            "(extend acb_api.PLAYOFF_SUBPHASES from a real payload; never guess)"
        )
    return Competition.PLAYOFF


def _flatten_calendar(calendar: dict) -> list[tuple[dict, dict]]:
    """``[(round, match), …]`` in (start, matchId) order."""
    out: list[tuple[dict, dict]] = []
    for round_obj in calendar.get("rounds") or []:
        for match in round_obj.get("matches") or []:
            out.append((round_obj, match))
    out.sort(key=lambda pair: (str(pair[1].get("startDateTime") or ""),
                               int(pair[1].get("id") or 0)))
    return out


def parse_acb_bundle(bundle: dict) -> dict:
    """Bundle -> the raw dataset dict consumed by DataIngestor."""
    if bundle.get("dataset") != DATASET_MARKER:
        raise ACBDataError(f"not an ACB bundle (expected dataset={DATASET_MARKER!r})")
    edition = bundle.get("edition_id")
    season = str(bundle.get("season") or "").strip()
    starter = str(bundle.get("season_code") or "").strip()
    if not season or not starter:
        raise ACBDataError("bundle is missing season / season_code")
    calendar = bundle.get("calendar") or {}
    headers: dict = bundle.get("headers") or {}
    boxscores: dict = bundle.get("boxscores") or {}
    if not calendar.get("rounds"):
        raise ACBDataError("bundle contains no calendar rounds")

    club_by_team_id: dict[int, int] = {}
    alias_used: dict[str, str] = {}
    for team in calendar.get("teams") or []:
        team_id = int(team["id"])
        club_id = int(team["clubId"])
        expected = CLUB_ID_TO_TEAM.get(club_id)
        if expected is None:
            raise ACBDataError(
                f"unknown ACB clubId {club_id} in the calendar (extend "
                "acb_api.CLUB_ID_TO_TEAM from a real payload; never guess)"
            )
        code = str(team.get("abbreviatedName") or "").strip().upper()
        aliases = TEAM_CODE_ALIASES.get(club_id, ())
        if code != expected and code not in aliases:
            raise ACBDataError(
                f"clubId {club_id} now abbreviates as "
                f"{team.get('abbreviatedName')!r} but the table says {expected!r} "
                f"(recorded aliases: {list(aliases)}); update the table from the "
                "real payload (never guess)"
            )
        if code != expected:
            alias_used[code] = expected
        club_by_team_id[team_id] = club_id

    games: list[Game] = []
    team_stats: list[TeamGameStats] = []
    teams: dict[str, Team] = {}
    postponed = cancelled = 0
    estimated = 0
    excluded: list[tuple[str, str]] = []
    overtime_games = 0
    minutes_drifted = 0
    out_of_order: list[str] = []
    max_round_seen = 0

    for round_obj, match in _flatten_calendar(calendar):
        match_id = int(match["id"])
        game_id = f"{starter}-{match_id:06d}"
        raw_status = str(match.get("matchStatus") or "").strip().upper()
        status = STATUS_MAP.get(raw_status)
        if status is None:
            raise ACBDataError(
                f"game {game_id}: unknown matchStatus {raw_status!r} "
                "(extend acb_api.STATUS_MAP from a real payload; never guess)"
            )
        competition = _match_competition(round_obj, game_id)

        home_team_id = int(match["homeTeamId"])
        away_team_id = int(match["awayTeamId"])
        if home_team_id == away_team_id:
            raise ACBDataError(f"game {game_id}: home team == away team")
        for team_id in (home_team_id, away_team_id):
            if team_id not in club_by_team_id:
                raise ACBDataError(
                    f"game {game_id}: team id {team_id} is not in the calendar registry"
                )
        home_id = resolve_team(club_by_team_id[home_team_id])
        away_id = resolve_team(club_by_team_id[away_team_id])
        if home_id == away_id:
            raise ACBDataError(f"game {game_id}: both sides resolve to {home_id}")

        game_date = _parse_utc(match.get("startDateTime"), "startDateTime")
        round_number = round_obj.get("roundNumber")
        if isinstance(round_number, int):
            # real-data fact: the calendar's round order is NOT chronological —
            # rescheduled matches are played weeks after later rounds, so a
            # chronological walk-forward visits them when they were actually
            # played (which is exactly what the backtest must do)
            if round_number < max_round_seen:
                out_of_order.append(
                    f"{game_id}(round {round_number}, {game_date.date().isoformat()})"
                )
            max_round_seen = max(max_round_seen, round_number)

        home_score = match.get("homeScore")
        away_score = match.get("awayScore")
        if status is not GameStatus.FINAL:
            if status is GameStatus.POSTPONED:
                postponed += 1
            elif status is GameStatus.CANCELLED:
                cancelled += 1
            games.append(Game(
                game_id=game_id, league=League.ACB, season=season,
                game_date=game_date, home_team_id=home_id, away_team_id=away_id,
                home_score=None, away_score=None, status=status,
                competition=competition,
            ))
            continue
        if home_score is None or away_score is None:
            raise ACBDataError(f"game {game_id}: FINALIZED without scores")

        key = str(match_id)
        defect_reason = KNOWN_SOURCE_DEFECTS.get(game_id)
        if defect_reason is not None:
            excluded.append((game_id, defect_reason))
            continue
        header = headers.get(key)
        box = boxscores.get(key)
        if header is None:
            raise ACBDataError(f"game {game_id}: FINALIZED but MatchHeader missing")
        if box is None:
            raise ACBDataError(f"game {game_id}: FINALIZED but boxscore missing")

        # three independent statements about the venue orientation must agree
        header_home = int(((header.get("teams") or {}).get("home") or {}).get("id") or -1)
        header_away = int(((header.get("teams") or {}).get("away") or {}).get("id") or -1)
        if header_home != home_team_id or header_away != away_team_id:
            raise ACBDataError(
                f"game {game_id}: calendar home/away ({home_team_id}/{away_team_id}) "
                f"!= header home/away ({header_home}/{header_away})"
            )
        box_team_ids: list[int] = []
        for candidate in box.get("teamBoxscores") or []:
            box_team_ids.append(int((candidate.get("team") or {}).get("id") or -1))
        if sorted(box_team_ids) != sorted([home_team_id, away_team_id]):
            raise ACBDataError(
                f"game {game_id}: boxscore teams {sorted(box_team_ids)} != "
                f"expected {sorted([home_team_id, away_team_id])}"
            )
        if int(header.get("matchId") or -1) != match_id:
            raise ACBDataError(f"game {game_id}: header matchId mismatch")
        if int(match.get("homeScore")) != int(header.get("currentHomeScore") or -1) or \
                int(match.get("awayScore")) != int(header.get("currentAwayScore") or -1):
            raise ACBDataError(
                f"game {game_id}: calendar score {home_score}-{away_score} != header "
                f"{header.get('currentHomeScore')}-{header.get('currentAwayScore')}"
            )

        home_line, home_periods, home_seconds = _team_totals(box, home_team_id, game_id)
        away_line, away_periods, away_seconds = _team_totals(box, away_team_id, game_id)
        if home_periods != away_periods:
            raise ACBDataError(
                f"game {game_id}: teams report {home_periods} vs {away_periods} periods"
            )
        if home_line["points"] != int(home_score) or away_line["points"] != int(away_score):
            raise ACBDataError(
                f"game {game_id}: boxscore totals {home_line['points']}-"
                f"{away_line['points']} != score {home_score}-{away_score}"
            )

        periods = home_periods
        overtime = periods > 4
        if overtime:
            overtime_games += 1
        expected_seconds = (200 + 25 * (periods - 4)) * 60
        for team_id, line, seconds in ((home_id, home_line, home_seconds),
                                       (away_id, away_line, away_seconds)):
            if abs(seconds - expected_seconds) > 300:
                raise ACBDataError(
                    f"game {game_id}/{team_id}: {seconds}s team playtime for a "
                    f"{periods}-period game (expected ~{expected_seconds}s)"
                )
            if seconds != expected_seconds:
                minutes_drifted += 1

        quarter_scores = header.get("quarterScores") or []
        if len(quarter_scores) != periods:
            raise ACBDataError(
                f"game {game_id}: header quarterScores={len(quarter_scores)} but the "
                f"boxscore reports {periods} played periods"
            )
        if sum(int(q.get("homeScore") or 0) for q in quarter_scores) != int(home_score) or \
                sum(int(q.get("awayScore") or 0) for q in quarter_scores) != int(away_score):
            raise ACBDataError(
                f"game {game_id}: quarter scores do not sum to the final score"
            )

        games.append(Game(
            game_id=game_id, league=League.ACB, season=season,
            game_date=game_date, home_team_id=home_id, away_team_id=away_id,
            home_score=int(home_score), away_score=int(away_score),
            status=GameStatus.FINAL, competition=competition,
        ))
        for team_id, line, is_home in ((home_id, home_line, True),
                                       (away_id, away_line, False)):
            estimated += 1
            team_stats.append(TeamGameStats(
                game_id=game_id, team_id=team_id, is_home=is_home, **line,
            ))
        # registry names come from the source display names for THIS edition
        for team_id, canonical in ((home_team_id, home_id), (away_team_id, away_id)):
            code = canonical.removeprefix("ACB-")
            teams.setdefault(canonical, Team(
                team_id=canonical, league=League.ACB,
                name=TEAM_NAMES.get(code, code), abbreviation=canonical,
            ))

    skipped = bundle.get("calendar_skipped") or {}
    if skipped:
        raise ACBDataError(
            f"bundle recorded {len(skipped)} finalised match(es) without a fetched "
            f"boxscore: {sorted(skipped)[:5]} — re-fetch with --resume"
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
            "n_postponed": postponed,
            "n_cancelled": cancelled,
            "n_overtime": overtime_games,
            "team_minutes_drifted": minutes_drifted,
            "possessions_estimated_for": estimated,
            "edition_id": edition,
            "season": season,
            "season_code": starter,
            "competition_id": bundle.get("competition_id"),
            "competition_name": bundle.get("competition_name"),
            "source": bundle.get("source"),
            "matches_out_of_round_order": out_of_order,
            "team_code_aliases_used": alias_used,
            "source_excluded": [{"game_id": gid, "reason": reason}
                                for gid, reason in excluded],
        },
    }


def load_acb_bundle_file(path: Path | str) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"ACB bundle not found: {path}")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ACBDataError(f"bundle is not valid JSON: {exc}") from exc
    return parse_acb_bundle(bundle)
