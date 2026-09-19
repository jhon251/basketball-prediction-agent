"""stats.nba.com team-gamelog adapter (llimllib/nba_data ``gamelog_*.parquet``).

The source is one row per TEAM per game (``team_abbreviation`` + ``matchup``
like ``"LAL vs. SAC"`` / ``"BOS @ CHI"``).  This adapter pivots it into the
canonical game-per-row record set and hands it to the SAME shared parsers,
validators and ingest gate as every other source — no core contract changes.

Game selection (fail-loud, never silent):

* excluded: game_id prefix ``001`` (preseason, includes non-NBA exhibition
  opponents such as GUA/MEL/SEM/HAP) and ``003`` (All-Star weekend special
  teams whose rows have no team abbreviation) — these are not NBA league
  competition;
* kept: ``002`` regular season, ``004`` playoffs, ``005`` play-in,
  ``006`` NBA Cup final;
* unknown team abbreviations inside kept games raise (never guessed);
* neutral-site games (both rows ``@``, e.g. Mexico City / Paris games and
  the Cup final): a nominal home is assigned deterministically — team rows
  sorted by abbreviation, the second team is home — and the count is
  reported in ``_meta["neutral_site_games"]``.
"""
from __future__ import annotations

from typing import Any

from basketball_agent.data.collectors.nba_csv import IngestDataError, _load_records
from basketball_agent.data.collectors.nba_teams import canonical_team_id

GAMELOG_IDENTITY_COLUMNS = (
    "season_year", "game_id", "game_date", "team_abbreviation", "matchup", "pts", "poss",
)
STAT_MAP = {
    "fgm": "fg", "fga": "fga", "fg3m": "fg3", "fg3a": "fg3a",
    "ftm": "ft", "fta": "fta", "oreb": "orb", "dreb": "drb", "reb": "trb",
    "ast": "ast", "tov": "tov", "stl": "stl", "blk": "blk", "pf": "pf",
}
EXCLUDED_GAME_PREFIXES = ("001", "003")  # preseason / All-Star exhibitions
CANONICAL_STAT_COLUMNS = (
    "possessions", "fg", "fga", "fg3", "fg3a", "ft", "fta",
    "orb", "drb", "trb", "ast", "tov", "stl", "blk", "pf",
)
# stats.nba.com game_id prefix -> competition segment; anything else -> OTHER
_PREFIX_TO_COMPETITION = {
    "002": "REGULAR_SEASON",
    "004": "PLAYOFF",
    "005": "PLAY_IN",
    "006": "CUP",
}


def looks_like_gamelog(df: Any) -> bool:
    """True when a DataFrame has the team-gamelog layout (vs. boxscores)."""
    cols = {str(c).strip().lower() for c in df.columns}
    return {"matchup", "team_abbreviation"} <= cols


def load_nba_gamelog_frame(df: Any, *, season: str | None = None) -> dict:
    """Pivot a stats.nba.com team-gamelog DataFrame into the raw dataset dict."""
    import pandas as pd

    df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
    missing = [c for c in GAMELOG_IDENTITY_COLUMNS if c not in df.columns]
    missing += [c for c in STAT_MAP if c not in df.columns]
    if missing:
        raise IngestDataError(
            f"gamelog layout missing required columns: {sorted(set(missing))} "
            f"(found: {sorted(df.columns)})"
        )

    prefixes = df["game_id"].astype(str).str[:3]
    frame = df[~prefixes.isin(EXCLUDED_GAME_PREFIXES)].copy()
    unattributed = frame[frame["team_abbreviation"].isna() | frame["matchup"].isna()]
    if len(unattributed):
        raise IngestDataError(
            f"{len(unattributed)} gamelog row(s) have no team/matchup after "
            "exhibition filtering — cannot attribute them to a team"
        )

    unknown: set[str] = set()
    resolved: dict[str, str | None] = {}

    def resolve(raw: Any) -> str | None:
        key = str(raw).strip()
        if key not in resolved:
            resolved[key] = canonical_team_id(key)
            if resolved[key] is None:
                unknown.add(key)
        return resolved[key]

    frame["team_id"] = frame["team_abbreviation"].map(resolve)
    if unknown:
        raise IngestDataError(
            "unknown team abbreviations in gamelog (verify the mapping or extend "
            f"nba_teams.py): {sorted(unknown)}"
        )

    records: list[dict] = []
    neutral_games = 0
    for game_id, group in frame.groupby("game_id", sort=True):
        rows = group.to_dict("records")
        if len(rows) != 2:
            raise IngestDataError(f"game {game_id}: expected 2 team rows, got {len(rows)}")
        home_rows = [r for r in rows if " vs. " in str(r["matchup"])]
        if len(home_rows) == 1:
            home = home_rows[0]
            away = next(r for r in rows if r is not home_rows[0])
        elif len(home_rows) == 0:
            # neutral site: deterministic nominal home (sorted by abbreviation)
            ordered = sorted(rows, key=lambda r: str(r["team_abbreviation"]))
            away, home = ordered[0], ordered[1]
            neutral_games += 1
        else:
            raise IngestDataError(
                f"game {game_id}: both rows claim home ('vs.') — venue is ambiguous"
            )

        record: dict[str, Any] = {
            "game_id": str(game_id),
            "game_date": str(home["game_date"]),
            "season": season or str(home["season_year"]),
            "home_team": home["team_id"],
            "away_team": away["team_id"],
            "home_score": int(home["pts"]),
            "away_score": int(away["pts"]),
            "competition": _PREFIX_TO_COMPETITION.get(str(game_id)[:3], "OTHER"),
        }
        for side, row in (("home", home), ("away", away)):
            for source_col, canonical in STAT_MAP.items():
                value = row[source_col]
                record[f"{side}_{canonical}"] = None if pd.isna(value) else int(value)
            poss = row["poss"]
            record[f"{side}_possessions"] = None if pd.isna(poss) else float(poss)
        records.append(record)

    columns = {name: name for name in (
        ["game_id", "game_date", "season", "home_team", "away_team", "home_score",
         "away_score", "competition"]
        + [f"home_{s}" for s in CANONICAL_STAT_COLUMNS]
        + [f"away_{s}" for s in CANONICAL_STAT_COLUMNS]
    )}
    result = _load_records(records, columns, season)
    result["_meta"]["neutral_site_games"] = neutral_games
    return result
