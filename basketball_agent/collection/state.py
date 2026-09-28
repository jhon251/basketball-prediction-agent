"""Collector run-state store (Phase 10-E).

Per-league collector state as atomic JSON files under
``storage/collector/``.  This is OPERATIONAL TELEMETRY (heartbeat, last
success, counts, errors) — wall-clock timestamps are the point here, in
contrast to the frozen prediction layers where ``as_of`` is caller-owned.

State is written atomically (temp file + os.replace) so a crashed run can
never corrupt a state file, and each league has its own file so one
league's failure can never touch another's state (isolation).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

#: how many recent runs are retained per league
RUN_HISTORY_LIMIT = 10


def now_utc() -> str:
    """Operational wall-clock stamp (collector telemetry only)."""
    return datetime.now(timezone.utc).isoformat()


class CollectorState:
    def __init__(self, state_dir: Path | str) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, league: str) -> Path:
        return self._dir / f"{league.upper()}.json"

    def load(self, league: str) -> dict:
        path = self._path(league)
        if not path.exists():
            return {"league": league.upper(), "runs": [], "seasons_ingested": [],
                    "games_seen": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def update(self, league: str, *, status: str, source: str,
               new_games: int = 0, updated_games: int = 0,
               validation: str | None = None,
               errors: list[str] | None = None,
               season: str | None = None,
               extra: dict | None = None) -> None:
        """Merge one run's outcome into the league state (atomic write).

        ``heartbeat``/``last_fetch_at`` are stamped here; ``last_success_at``
        only advances on status == "success" / "no_change".
        """
        state = self.load(league)
        now = now_utc()
        state["league"] = league.upper()
        state["source"] = source
        state["heartbeat"] = now
        state["last_status"] = status
        state["last_fetch_at"] = now
        state["last_new_games"] = new_games
        state["last_updated_games"] = updated_games
        state["last_validation"] = validation
        state["last_errors"] = errors or []
        if season:
            state["last_season"] = season
        if status in ("success", "no_change", "partial"):
            state["last_success_at"] = now
        if errors:
            state["last_error"] = errors[-1]
        run = {"at": now, "status": status, "new_games": new_games,
               "updated_games": updated_games, "validation": validation,
               "errors": errors or []}
        state["runs"] = ([run] + state.get("runs", []))[:RUN_HISTORY_LIMIT]
        if extra:
            state.update(extra)
        self._atomic_write(self._path(league), state)

    def mark_season_ingested(self, league: str, season: str,
                             snapshot_id: str | None = None) -> None:
        state = self.load(league)
        seasons = state.get("seasons_ingested", [])
        entry = {"season": season,
                 "ingested_at": now_utc(),
                 "snapshot_id": snapshot_id}
        if season not in [s["season"] if isinstance(s, dict) else s
                          for s in seasons]:
            seasons.append(entry)
        state["seasons_ingested"] = seasons
        if snapshot_id:
            state["last_snapshot_id"] = snapshot_id
        self._atomic_write(self._path(league), state)

    def seen_games(self, league: str) -> dict:
        """Source-id fingerprints for new/updated detection."""
        return self.load(league).get("games_seen", {})

    def update_seen_games(self, league: str, fingerprints: dict[str, str]) -> None:
        state = self.load(league)
        merged = dict(state.get("games_seen", {}))
        merged.update(fingerprints)
        state["games_seen"] = merged
        self._atomic_write(self._path(league), state)

    def all(self) -> dict[str, dict]:
        out = {}
        for path in sorted(self._dir.glob("*.json")):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                out[state.get("league", path.stem.upper())] = state
            except (json.JSONDecodeError, OSError):
                continue  # a corrupt state file never crashes `agent status`
        return out

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
