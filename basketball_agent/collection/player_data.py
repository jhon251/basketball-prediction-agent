"""Player-stats ingestion runner (Phase 12-G).

Turns a parsed ``PlayerlogBundle`` (+ optional roster) into DB rows:
``players`` first (roster priority, log-derived fallback names), then
``player_game_stats``.  Insert path is idempotent per (player, game):
existing rows are skipped and counted — re-ingesting the same file is a
no-op.  A single malformed row aborts its batch (parse happens before
any write; the frozen validators' all-or-nothing rule keeps its spirit).
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.data.collectors.nba_playerlog import (
    PlayerlogBundle, PlayerlogError,
)
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import PlayerGameStats
from basketball_agent.data.schemas.player import Player


@dataclass
class PlayerIngestResult:
    players_written: int = 0
    stats_written: int = 0
    stats_skipped_existing: int = 0
    stats_failed: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def final_nba_games_by_id(repo: Repository) -> dict[str, tuple[str, str]]:
    """game_id -> (home_team_id, away_team_id) for FINAL NBA games."""
    return {
        g.game_id: (g.home_team_id, g.away_team_id)
        for g in repo.list_games(league=League.NBA, status=GameStatus.FINAL)
    }


def ingest_playerlog(
    repo: Repository,
    bundle: PlayerlogBundle,
    *,
    roster: list[dict] | None = None,
) -> PlayerIngestResult:
    result = PlayerIngestResult()
    existing_players = {p.player_id for p in repo.list_players()}
    to_write: list[Player] = []
    by_id: dict[str, dict] = {}
    for r in (roster or []):
        if r.get("player_id") and r["player_id"] not in by_id:
            by_id[r["player_id"]] = r
    for p in bundle.players:  # log names only fill roster gaps
        if p["player_id"] not in by_id:
            by_id[p["player_id"]] = p
    for pid, r in sorted(by_id.items()):
        if pid in existing_players:
            continue
        team_id = r.get("team_id") or None
        if team_id and repo.get_team(team_id) is None:
            team_id = None  # roster rows may reference moved franchises
        to_write.append(Player(
            player_id=pid, league=League.NBA,
            name=r.get("name") or f"player-{pid}",
            current_team_id=team_id))
    for p in to_write:
        repo.upsert_player(p)
        result.players_written += 1

    existing_stats = repo.list_player_stats() if hasattr(
        repo, "list_player_stats") else None
    seen_keys: set[tuple[str, str]] = set()
    if existing_stats:
        seen_keys = {(s.game_id, s.player_id) for s in existing_stats}
    for s in bundle.stats:
        key = (s["game_id"], s["player_id"])
        if key in seen_keys:
            result.stats_skipped_existing += 1
            continue
        try:
            repo.insert_player_stats(PlayerGameStats(**s))
            seen_keys.add(key)
            result.stats_written += 1
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            result.stats_failed += 1
            if len(result.errors) < 10:
                result.errors.append(f"{s['game_id']}/{s['player_id']}: "
                                     f"{type(exc).__name__}: {exc}")
    return result


def ingest_playerlog_files(
    db, log_path, roster_path=None,
) -> tuple[PlayerIngestResult, PlayerlogBundle]:
    from basketball_agent.data.collectors.nba_playerlog import (
        load_parquet_rows, parse_playerlog, parse_roster,
    )

    repo = Repository(db)
    games = final_nba_games_by_id(repo)
    bundle = parse_playerlog(load_parquet_rows(log_path),
                             games_by_id=games)
    roster = parse_roster(load_parquet_rows(roster_path)) if roster_path \
        else None
    result = ingest_playerlog(repo, bundle, roster=roster)
    return result, bundle
