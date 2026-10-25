"""NBA fixtures lifecycle core (Phase 12-B) — shared by the production
loop and the API layer.

ESPN scoreboard rows in, SCHEDULED/FINAL game rows out:
* upcoming rows enter the core with ledger-assigned sequential ids
  (``NBA{yy}-{seq:04d}``, same convention as the BSL/NBL/CBA upsert);
* final rows flip ledger-known games to FINAL with scores
  (``Repository.update_game_result``), which is what prediction
  settlement requires; rows the ledger never saw belong to the deep
  parquet history and are skipped;
* the ledger (``SourceGameLedger``) is the dedup truth — re-applying
  the same rows is a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from basketball_agent.collection.ledger import SourceGameLedger
from basketball_agent.collection.state import CollectorState
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import (
    Competition, GameStatus, League,
)


def _norm_season(label: str) -> str:
    """Same normalisation as the production loop's row sources."""
    parts = str(label).split("-")
    if len(parts) == 2 and len(parts[1]) == 4:
        return f"{parts[0]}-{parts[1][-2:]}"
    return str(label)


@dataclass
class FixturesDelta:
    upserted: int = 0
    finalized: int = 0
    skipped_no_date: int = 0
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _fingerprint(row: dict) -> str:
    import hashlib
    import json

    payload = json.dumps(
        {k: row.get(k) for k in sorted(row) if k != "espn_state"},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def apply_fixtures_rows(
    repo: Repository,
    ledger: SourceGameLedger,
    state: CollectorState,
    rows: list[dict],
    *,
    league: str,
    id_scheme: str = "sequential",
) -> FixturesDelta:
    """Generic fixtures lifecycle (Phase 12-H generalisation of the NBA
    pass).

    ``id_scheme``:
    * ``"sequential"`` — ``{LG}{yy}-{seq:04d}`` continuation (NBA/CBA
      convention, matches the row-source upsert ids);
    * ``"source"`` — ``{LG}{yyyy}-{source_id}`` (ACB convention, matches
      the season-refresh ids so a later refresh reuses the SAME row).
    """
    from basketball_agent.data.schemas.base import League

    league_enum = League(league)
    delta = FixturesDelta()
    seq_by_prefix: dict[str, int] = {}
    for r in rows:
        source_id = str(r["source_id"])
        known = ledger.has(league, source_id)
        try:
            if str(r.get("status_raw")) == "1":
                if not known:
                    continue  # deep history: season refresh owns it
                game_id = ledger.game_id_for(league, source_id)
                game = repo.get_game(game_id) if game_id else None
                if game is None or game.status is GameStatus.FINAL:
                    continue  # already settled / missing row
                repo.update_game_result(
                    game_id,
                    home_score=int(r["home_score"]),
                    away_score=int(r["away_score"]))
                delta.finalized += 1
                continue
            if known:
                continue
            if not str(r.get("date") or "").strip():
                delta.skipped_no_date += 1
                continue
            season = _norm_season(str(r.get("season") or ""))
            if not season:
                delta.errors.append({
                    "stage": f"{league.lower()}_fixtures", "league": league,
                    "source_id": source_id,
                    "error": "row without season label"})
                continue
            if id_scheme == "source":
                game_id = f"{league}{season[:4]}-{source_id}"
            else:
                prefix = f"{league}{season[:4][-2:]}"
                if prefix not in seq_by_prefix:
                    seq_by_prefix[prefix] = (
                        ledger.max_game_seq(league, prefix) + 1)
                game_id = f"{prefix}-{seq_by_prefix[prefix]:04d}"
                seq_by_prefix[prefix] += 1
            repo.insert_game(type("G", (), {
                "game_id": game_id,
                "league": league_enum,
                "season": season,
                "game_date": datetime.strptime(
                    r["date"] + " " + r.get("match_time", "00:00"),
                    "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc),
                "home_team_id": r["home_id"], "away_team_id": r["away_id"],
                "home_score": None, "away_score": None,
                "status": GameStatus.SCHEDULED,
                "competition": Competition(
                    str(r.get("competition") or "REGULAR_SEASON")),
            }))
            state.update_seen_games(league, {source_id: _fingerprint(r)})
            ledger.record(league, season, source_id, game_id)
            delta.upserted += 1
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            delta.errors.append({
                "stage": f"{league.lower()}_fixtures", "league": league,
                "source_id": source_id, "error": str(exc)})
    return delta


def apply_nba_rows(
    repo: Repository,
    ledger: SourceGameLedger,
    state: CollectorState,
    rows: list[dict],
) -> FixturesDelta:
    """Apply parsed ESPN rows (see nba_scoreboard.parse_scoreboard_events)."""
    return apply_fixtures_rows(repo, ledger, state, rows, league="NBA")


def refresh_nba_upcoming(
    db, state_dir, *, n_days: int = 8,
) -> FixturesDelta:
    """Fetch ESPN + apply: the API-layer entry point (one live refresh)."""
    from basketball_agent.data.collectors.nba_scoreboard import fetch_upcoming_rows

    repo = Repository(db)
    state_dir = Path(state_dir)
    delta = apply_nba_rows(
        repo, SourceGameLedger(state_dir), CollectorState(state_dir),
        fetch_upcoming_rows(n_days=n_days),
    )
    return delta
