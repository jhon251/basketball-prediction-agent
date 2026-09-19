"""Local Parquet -> Phase 7 ingest adapter.

Reads a local NBA games Parquet file (nba_data / llimllib style, e.g.
``team_boxscores.parquet``) and normalizes it through the SAME shared
parsers, validators and gates as the CSV path:

* same column-alias normalization (``*_home`` / ``*_visitor`` side suffixes,
  ``fgm``/``ftm``/``fg3m`` made-shot names, stable franchise IDs);
* same reality / leakage FAIL gates downstream (unchanged);
* same Walk-Forward train/test isolation (unchanged);
* provenance is recorded the same way (the raw file is registered verbatim
  with source / retrieved_at / SHA-256 by the ingest CLI).

Reading needs duckdb (preferred) or pandas+pyarrow.  When neither is
importable the loader raises a clear error instead of failing silently.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from basketball_agent.data.collectors.nba_csv import (
    IngestDataError,
    _load_records,
    _normalize_headers,
)


def read_parquet_frame(path: Path | str):
    """Read a local Parquet file into a pandas DataFrame.

    Prefers duckdb (no extra engine needed); falls back to
    pandas.read_parquet(engine="pyarrow").
    """
    try:
        import duckdb  # type: ignore[import-not-found]
    except ImportError:
        duckdb = None
    if duckdb is not None:
        try:
            return duckdb.read_parquet(str(path)).df()
        except Exception as exc:  # noqa: BLE001 — unreadable file must be a clean FAIL
            raise IngestDataError(f"could not read Parquet file {path}: {exc}") from exc

    try:
        import pandas as pd  # type: ignore[import-not-found]

        return pd.read_parquet(path, engine="pyarrow")
    except ImportError as exc:
        raise IngestDataError(
            "reading Parquet requires duckdb or pandas+pyarrow, neither of which is "
            "importable in this Python environment. Options: (1) run the command with the "
            "environment that has them installed (e.g. the uv env with nbadb), or "
            "(2) convert offline first: `basketball convert <file.parquet> --out games.csv` "
            "and then use the standard CSV ingest path."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — unreadable file must be a clean FAIL
        raise IngestDataError(f"could not read Parquet file {path}: {exc}") from exc


def _clean_value(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _records_from_frame(df) -> list[dict]:
    records = []
    for row in df.to_dict("records"):
        records.append({str(k).strip().lower(): _clean_value(v) for k, v in row.items()})
    return records


def normalize_dataframe(df, *, season: str | None = None) -> dict:
    """Normalize a pandas DataFrame (any source) into the standard raw dict.

    The heavy lifting (layout detection, aliases, franchise mapping, stat
    consistency) reuses the CSV parsers unchanged.
    """
    df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
    df = df.loc[:, ~df.columns.duplicated()]
    columns = _normalize_headers(list(df.columns))
    records = _records_from_frame(df)
    if not records:
        raise IngestDataError("parquet contains no data rows")
    return _load_records(records, columns, season)


def load_nba_games_parquet(path: Path | str, *, season: str | None = None) -> dict:
    """Read + normalize a local NBA games Parquet file.

    Two layouts are auto-detected:

    * team-gamelog (one row per team per game, ``matchup`` column —
      llimllib/nba_data ``gamelog_*.parquet``) -> the gamelog adapter;
    * game-per-row / row-per-team boxscore layouts -> the standard
      ``normalize_dataframe`` path.
    """
    from basketball_agent.data.collectors.nba_gamelog import (
        load_nba_gamelog_frame, looks_like_gamelog,
    )

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"parquet file not found: {path}")
    frame = read_parquet_frame(path)
    if looks_like_gamelog(frame):
        return load_nba_gamelog_frame(frame, season=season)
    return normalize_dataframe(frame, season=season)


def load_nba_games_file(path: Path | str, *, season: str | None = None) -> dict:
    """Format dispatch used by the ingest/validate CLIs: .parquet/.pq -> the
    Parquet adapter, everything else -> the standard CSV loader."""
    from basketball_agent.data.collectors.nba_csv import load_nba_games_csv

    path = Path(path)
    if path.suffix.lower() in (".parquet", ".pq"):
        return load_nba_games_parquet(path, season=season)
    return load_nba_games_csv(path, season=season)


def merge_raw_datasets(raws: list[dict]) -> dict:
    """Merge normalized raw dicts from multiple files (multi-file sources).

    Records are concatenated; team registries are deduplicated by team_id;
    cross-file duplicates are NOT silently dropped — the validation gate
    rejects them like any other duplicate.
    """
    merged: dict[str, list] = {
        "games": [], "team_stats": [], "teams": [], "players": [],
        "player_stats": [], "availability": [],
    }
    estimated = 0
    for raw in raws:
        for key in merged:
            merged[key].extend(raw.get(key, []))
        estimated += raw.get("_meta", {}).get("possessions_estimated_for", 0)
    seen_teams: set[str] = set()
    deduped: list[dict] = []
    for team in merged["teams"]:
        if team["team_id"] not in seen_teams:
            seen_teams.add(team["team_id"])
            deduped.append(team)
    merged["teams"] = deduped
    merged["_meta"] = {"n_games": len(merged["games"]), "possessions_estimated_for": estimated}
    return merged
