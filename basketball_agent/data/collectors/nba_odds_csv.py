"""NBA odds CSV adapter (Phase 11-A, deliverable 2).

Reads a CSV file with historical closing odds and produces validated
odds rows for the repository.  Column names are mapped via a config dict
so the adapter can handle different source formats.

Expected canonical columns (after mapping):
    game_date      — match date (YYYY-MM-DD or similar)
    away_team      — away team name/code (matched against games table)
    home_team      — home team name/code
    close_moneyline_home — American odds for home ML (e.g. -150, +130)
    close_moneyline_away — American odds for away ML
    close_spread   — spread line (optional; only for SPAD/TOTAL)
    close_spread_odds    — decimal odds for the spread
    close_total    — total line
    close_total_odds     — decimal odds for the total
    bookmaker      — bookmaker name
    season         — season label

The adapter matches games by (home_team, away_team, date_window) against
the games table.  Unmatchable rows raise ``UnknownTeamError`` (never
guess).

Usage:
    from basketball_agent.data.collectors.nba_odds_csv import load_nba_odds_csv
    rows, corrections = load_nba_odds_csv("odds.csv", column_map={...})
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from basketball_agent.evaluation.devig import moneyline_pair_to_fair

_DEFAULT_COLUMN_MAP = {
    "game_date": "game_date",
    "away_team": "away_team",
    "home_team": "home_team",
    "close_moneyline_home": "close_moneyline_home",
    "close_moneyline_away": "close_moneyline_away",
    "close_spread": "close_spread",
    "close_spread_odds": "close_spread_odds",
    "close_total": "close_total",
    "close_total_odds": "close_total_odds",
    "bookmaker": "bookmaker",
    "season": "season",
}


class UnknownTeamError(Exception):
    pass


class OddsCSVError(Exception):
    pass


def _parse_date(raw: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    raise OddsCSVError(f"cannot parse date {raw!r}")


def load_nba_odds_csv(
    path: Path | str,
    db,
    *,
    column_map: dict[str, str] | None = None,
    date_tolerance_days: int = 1,
    source: str = "csv_import",
    bookmaker_default: str = "unknown",
) -> tuple[list[dict], list[dict]]:
    """Load an NBA odds CSV, match against the games table, and produce
    validated odds rows.

    Returns ``(odds_rows, skipped)`` where skipped is a list of
    {row_number, reason} dicts for rows that could not be processed.

    ``column_map`` overrides the default column name mapping.  Only the
    keys present in the CSV are used; missing optional columns (spread,
    total) are skipped.
    """
    from basketball_agent.data.db.repository import Repository

    cmap = dict(_DEFAULT_COLUMN_MAP)
    if column_map:
        cmap.update(column_map)

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"odds CSV not found: {path}")

    repo = Repository(db)
    odds_rows: list[dict] = []
    skipped: list[dict] = []

    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise OddsCSVError("CSV has no header row")
        # normalise header names (strip whitespace/BOM)
        reader.fieldnames = [f.strip().lstrip("\\ufeff") for f in reader.fieldnames]

        for row_num, raw_row in enumerate(reader, start=2):  # 1-based, header = 1
            try:
                row = {k.strip(): v.strip() for k, v in raw_row.items()
                       if k and v}
                game_date_raw = row.get(cmap["game_date"], "")
                if not game_date_raw:
                    skipped.append({"row": row_num, "reason": "missing date"})
                    continue
                game_date = _parse_date(game_date_raw)
                home_name = row.get(cmap["home_team"], "")
                away_name = row.get(cmap["away_team"], "")
                if not home_name or not away_name:
                    skipped.append({"row": row_num, "reason": "missing team"})
                    continue

                # match against games table
                matched = _match_game(repo, home_name, away_name, game_date,
                                      date_tolerance_days)
                if matched is None:
                    raise UnknownTeamError(
                        f"row {row_num}: no game matches "
                        f"{away_name} @ {home_name} on {game_date_raw}")

                game_id, actual_home, actual_away = matched
                bookmaker = row.get(cmap.get("bookmaker", "bookmaker"),
                                    bookmaker_default)

                # moneyline odds -> decimal + fair probabilities
                ml_home_raw = row.get(cmap.get("close_moneyline_home", ""), "")
                ml_away_raw = row.get(cmap.get("close_moneyline_away", ""), "")
                if ml_home_raw and ml_away_raw:
                    am_h, am_a = int(float(ml_home_raw)), int(float(ml_away_raw))
                    fair_h, fair_a, margin = moneyline_pair_to_fair(am_h, am_a)
                    odds_rows.append({
                        "game_id": game_id, "league": "NBA",
                        "bookmaker": bookmaker, "market": "MONEYLINE",
                        "kind": "CLOSE", "line": None,
                        "price_home": am_h, "price_away": am_a,
                        "price_decimal": min(
                            1 + abs(am_h) / (100 if am_h > 0 else -100),
                            1 + am_a / (100 if am_a > 0 else -100)
                        ) if am_h > 0 == (am_a > 0) else
                        (1 + am_h / 100 if am_h > 0 else 1 + 100 / abs(am_h)),
                        "fair_prob_home": fair_h,
                        "captured_at": _capture_at(game_date),
                        "source": source,
                    })

                # spread
                spread_line = row.get(cmap.get("close_spread", ""), "")
                spread_odds = row.get(cmap.get("close_spread_odds", ""), "")
                if spread_line and spread_odds:
                    odds_rows.append({
                        "game_id": game_id, "league": "NBA",
                        "bookmaker": bookmaker, "market": "SPREAD",
                        "kind": "CLOSE", "line": float(spread_line),
                        "price_decimal": float(spread_odds),
                        "captured_at": _capture_at(game_date),
                        "source": source,
                    })

                # total
                total_line = row.get(cmap.get("close_total", ""), "")
                total_odds = row.get(cmap.get("close_total_odds", ""), "")
                if total_line and total_odds:
                    odds_rows.append({
                        "game_id": game_id, "league": "NBA",
                        "bookmaker": bookmaker, "market": "TOTAL",
                        "kind": "CLOSE", "line": float(total_line),
                        "price_decimal": float(total_odds),
                        "captured_at": _capture_at(game_date),
                        "source": source,
                    })

            except UnknownTeamError:
                raise
            except OddsCSVError:
                raise
            except (ValueError, KeyError, TypeError) as exc:
                skipped.append({"row": row_num, "reason": str(exc)})

    return odds_rows, skipped


def _capture_at(game_date: datetime) -> str:
    """CLOSE odds are captured ~1 hour before tip-off."""
    return (game_date - timedelta(hours=1)).isoformat()


def _match_game(repo, home_name: str, away_name: str,
                game_date: datetime, tolerance_days: int):
    """Find a FINAL game matching (home, away, date ± tolerance).

    Matching is by exact team_id pair (not name) because the odds CSV
    is expected to use the same team codes as the games table."""
    from basketball_agent.data.db.connection import Database
    from basketball_agent.data.schemas.base import GameStatus

    all_games = repo.list_games(league=None, status=GameStatus.FINAL)
    delta = timedelta(days=tolerance_days)
    for g in all_games:
        if g.game_date - delta <= game_date <= g.game_date + delta:
            if (g.home_team_id.upper() == home_name.upper()
                    and g.away_team_id.upper() == away_name.upper()):
                return g.game_id, g.home_team_id, g.away_team_id
    return None
