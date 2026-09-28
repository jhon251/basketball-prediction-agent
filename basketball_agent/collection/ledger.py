"""Source-id -> game-id ledger (Phase 10-E.1).

Append-only JSONL per league: maps the source's stable game id (TBF
matchId, CBA scheduleId, NBL uuid, ...) to the internal game_id assigned
at ingestion.  This is what makes Scheduled -> Live -> Final updates and
season top-ups idempotent: a source id that is already in the ledger is
never re-ingested, and its internal game_id never changes.
"""
from __future__ import annotations

import json
from pathlib import Path


class SourceGameLedger:
    def __init__(self, log_dir: Path | str) -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, league: str) -> Path:
        return self._dir / f"{league.upper()}_source_ids.jsonl"

    def entries(self, league: str) -> dict[str, dict]:
        """source_id -> {game_id, season, recorded_at}."""
        path = self._path(league)
        if not path.exists():
            return {}
        out: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            out[e["source_id"]] = e
        return out

    def has(self, league: str, source_id: str) -> bool:
        return source_id in self.entries(league)

    def game_id_for(self, league: str, source_id: str) -> str | None:
        e = self.entries(league).get(source_id)
        return e["game_id"] if e else None

    def record(self, league: str, season: str, source_id: str,
               game_id: str, recorded_at: str | None = None) -> str:
        """Append one mapping.  First record wins (append-only: a repeated
        source id with a different game id is refused — the internal id of
        an ingested game is immutable)."""
        entries = self.entries(league)
        if source_id in entries:
            existing = entries[source_id]["game_id"]
            if existing != game_id:
                raise ValueError(
                    f"{league} source id {source_id!r} already maps to "
                    f"{existing!r}, refusing re-map to {game_id!r}")
            return "duplicate"
        entry = {"league": league.upper(), "season": season,
                 "source_id": source_id, "game_id": game_id,
                 "recorded_at": recorded_at}
        with self._path(league).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False,
                                sort_keys=True) + "\n")
        return "recorded"

    def max_game_seq(self, league: str, prefix: str) -> int:
        """Highest numeric suffix among this league's game_ids with the
        given prefix (e.g. prefix='BSL26' for 'BSL26-0007')."""
        best = 0
        for e in self.entries(league).values():
            gid = e["game_id"]
            if gid.startswith(prefix + "-"):
                try:
                    best = max(best, int(gid.split("-")[1]))
                except (IndexError, ValueError):
                    continue
        return best

    def game_ids(self, league: str) -> set[str]:
        return {e["game_id"] for e in entries(league).values()}
