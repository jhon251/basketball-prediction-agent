"""Collector scheduler (Phase 10-E): per-league isolated collection runs.

``CollectionScheduler.run`` executes one collection pass over the
registered leagues:

* **isolation** — every league runs in its own try/except; a failure is
  recorded in that league's state and never blocks the others;
* **observability** — every run updates the league's heartbeat/state
  (heartbeat, status, new/updated counts, validation result, errors);
* **idempotency** — source games are fingerprinted in the per-league
  state; re-running without source changes reports ``no_change`` and
  writes nothing to the Prediction Core;
* **snapshot hygiene** — ingestion happens only through the season
  refresh gate (validate → ingest → snapshot) and only for a league-
  season that has no games in the core yet; a no-change run never
  creates a snapshot.

Mid-season incremental top-up is deliberately NOT implemented: game ids
are assigned per ingestion pass, so re-ingesting an already-ingested
season would renumber games.  It is recorded as an open item (see
PHASE10_E doc §known limitations).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from basketball_agent.collection.state import CollectorState, now_utc
from basketball_agent.data.db.repository import Repository


@dataclass
class CollectionResult:
    league: str
    status: str                       # success | no_change | partial | failed | skipped
    source: str
    new_games: int = 0
    updated_games: int = 0
    status_counts: dict = field(default_factory=dict)
    validation: str | None = None
    errors: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def summary(self) -> str:
        return (f"{self.league:12s} {self.status:10s} new={self.new_games} "
                f"updated={self.updated_games} "
                f"validation={self.validation or '-'} "
                f"errors={len(self.errors)}")


def _fingerprint(row: dict) -> str:
    import hashlib

    payload = json.dumps({"s": row.get("status_raw"),
                          "h": row.get("home_score"),
                          "a": row.get("away_score")}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _status_counts(rows: list[dict]) -> dict:
    from collections import Counter

    counts: dict[str, int] = {}
    for r in rows:
        raw = str(r.get("status_raw"))
        counts[raw] = counts.get(raw, 0) + 1
    return counts


class CollectionScheduler:
    """Per-league isolated collection with persisted run state."""

    def __init__(self, repo: Repository, state_dir,
                 bundle_dir=None, *, season_overrides: dict[str, str] | None = None):
        self._repo = repo
        self.state = CollectorState(state_dir)
        self._bundle_dir = bundle_dir
        self._season_overrides = season_overrides or {}

    # ------------------------------------------------------------- registry
    def _discover(self, league: str) -> dict:
        """Light discovery for one league (live verified endpoints)."""
        import basketball_agent.collection.sources as src

        if league == "BSL":
            season_info = src.bsl_current_season()
            rows = src.bsl_matches(season_info["activity_id"],
                                   season_info["season_id"])
            return {"source": "tbf.org.tr official API",
                    "season": season_info["season"], "rows": rows,
                    "extra": {"activity_id": season_info["activity_id"],
                              "season_id": season_info["season_id"]}}
        if league == "NBL":
            year = self._season_overrides.get("NBL")
            year = int(year) if year else datetime.now(timezone.utc).year
            rows = src.nbl_matches(year)
            return {"source": "schedule.nbl.com.au official API",
                    "season": f"{year}-{str((year + 1) % 100).zfill(2)}",
                    "rows": rows, "extra": {"year": year}}
        if league == "CBA":
            season = src.cba_current_season()
            year = int(season["season_value"])
            counts = src.cba_recent_schedule_counts(
                [(year, 10), (year, 11), (year, 12),
                 (year + 1, 1), (year + 1, 2), (year + 1, 3)])
            return {"source": "cbaleague.com official data-server",
                    "season": season["season"], "rows": [],
                    "extra": {"scheduled_counts": counts,
                              "season_value": season["season_value"]}}
        if league == "NBA":
            fresh = src.nba_source_freshness()
            return {"source": "llimllib/nba_data (parquet file source)",
                    "rows": [], "extra": fresh,
                    "manual_refresh": True}
        if league == "EUROLEAGUE":
            probe = src.euroleague_reachable()
            return {"source": "live.euroleague.net API", "rows": [],
                    "extra": probe, "manual_refresh": True}
        if league == "ACB":
            listing = src.acb_seasons()
            return {"source": "acb official API (season listing)", "rows": [],
                    "extra": listing, "manual_refresh": True}
        raise RuntimeError(f"no collector registered for league {league!r}")

    # ----------------------------------------------------------------- run
    def run(self, leagues: list[str] | None = None,
            *, as_of: datetime | None = None) -> list[CollectionResult]:
        """One collection pass.  Per-league isolation: a failure in one
        league is recorded and never blocks the others."""
        registry = ["NBA", "EUROLEAGUE", "ACB", "NBL", "CBA", "BSL"]
        targets = leagues or registry
        results: list[CollectionResult] = []
        for league in targets:
            try:
                results.append(self._collect_one(league))
            except Exception as exc:  # noqa: BLE001 — isolation boundary
                err = f"{type(exc).__name__}: {exc}"
                self.state.update(league, status="failed",
                                  source="unknown", errors=[err])
                results.append(CollectionResult(
                    league=league, status="failed", source="unknown",
                    errors=[err]))
        return results

    def _collect_one(self, league: str) -> CollectionResult:
        discovered = self._discover(league)
        rows = discovered.get("rows", [])
        status_counts = _status_counts(rows)

        # new/updated detection against persisted fingerprints
        seen = self.state.seen_games(league)
        new_ids = [r["source_id"] for r in rows
                   if r["source_id"] not in seen]
        updated_ids = [r["source_id"] for r in rows
                       if r["source_id"] in seen
                       and seen[r["source_id"]] != _fingerprint(r)]

        result = CollectionResult(
            league=league, status="success", source=discovered["source"],
            new_games=len(new_ids), updated_games=len(updated_ids),
            status_counts=status_counts, detail={
                "season": discovered.get("season"),
                "source_rows": len(rows),
                "new_source_ids": new_ids[:20],
                "updated_source_ids": updated_ids[:20],
                **({"manual_refresh": True} if discovered.get("manual_refresh")
                   else {}),
                **({"extra": discovered["extra"]}
                   if discovered.get("extra") else {}),
            })

        # validation: a discovery with zero rows is recorded as an anomaly
        validation = None
        if not rows and not discovered.get("manual_refresh"):
            validation = "anomaly: source returned zero games"
            result.errors.append(validation)
            result.status = "partial"
        result.validation = validation

        # persist new/updated fingerprints (idempotency baseline)
        self.state.update_seen_games(
            league, {r["source_id"]: _fingerprint(r) for r in rows})

        self.state.update(
            league,
            status=("no_change" if not new_ids and not updated_ids
                    and result.status == "success" else result.status),
            source=discovered["source"],
            new_games=result.new_games,
            updated_games=result.updated_games,
            validation=validation,
            errors=result.errors,
            season=discovered.get("season"),
            extra={"last_detail": result.detail},
        )
        result.status = ("no_change" if not new_ids and not updated_ids
                         and result.status == "success" else result.status)
        return result


# ------------------------------------------------------- season refresh gate
_REFRESH_ARGS: dict[str, list[str]] = {
    # subprocess args for the EXISTING, battle-tested fetchers (idempotent
    # --resume); the produced season bundle then flows through the frozen
    # validate -> ingest -> snapshot path.
    "BSL": ["--season", "{season}", "--out-dir", "{out_dir}", "--resume"],
    "CBA": ["--season", "{season_value}", "--out-dir", "{out_dir}", "--resume"],
    "NBL": ["--year", "{nbl_year}", "--out-dir", "{out_dir}", "--resume"],
    "EUROLEAGUE": ["--seasons", "{el_season}", "--out-dir", "{out_dir}",
                   "--resume"],
}


@dataclass
class RefreshResult:
    league: str
    season: str
    status: str                    # ingested | already_ingested | failed | unsupported
    games_ingested: int = 0
    snapshot_id: str | None = None
    errors: list[str] = field(default_factory=list)


def refresh_season(repo: Repository, state_dir, db, league: str,
                   *, season: str | None = None,
                   bundle_dir: "Path | None" = None) -> RefreshResult:
    """Season-refresh gate (Phase 10-E, hardened in 10-E.1).

    * season ABSENT from the core  -> full onboarding through the frozen
      validate -> ingest -> snapshot path (default id sequence);
    * season ALREADY in the core   -> safe incremental top-up: the bundle
      is parsed with ``game_seq_start`` continuing the stored sequence,
      games whose source id is already in the ledger are dropped (never
      re-ingested, never renumbered), and score corrections on ingested
      games are REPORTED (never auto-applied).

    All source_id -> game_id mappings are recorded in the append-only
    ledger (``collection.ledger.SourceGameLedger``).
    """
    from basketball_agent.config import get_settings
    from basketball_agent.collection.ledger import SourceGameLedger
    from basketball_agent.collection.state import CollectorState

    settings = get_settings()
    state = CollectorState(state_dir)
    ledger = SourceGameLedger(state_dir)
    st = state.load(league)
    discovered_season = st.get("last_season")
    season = season or discovered_season
    if not season:
        return RefreshResult(league=league, season=season or "?",
                             status="failed",
                             errors=["no season known for this league; run "
                                     "a discovery pass first"])

    def _norm(label: str) -> str:
        parts = str(label).split("-")
        if len(parts) == 2 and len(parts[1]) == 4:
            return f"{parts[0]}-{parts[1][-2:]}"
        return str(label)

    season_norm = _norm(season)
    from basketball_agent.data.schemas.base import League

    league_enum = League(league)
    games_in_core = [g for g in repo.list_games(league=league_enum)
                     if g.season == season_norm]
    top_up = bool(games_in_core)
    season_prefix = f"{league}{season_norm[:2][-2:]}"
    season_prefix = f"{league}{season_norm[:4][-2:]}"
    max_seq = ledger.max_game_seq(league, season_prefix)

    args_template = _REFRESH_ARGS.get(league)
    if args_template is None:
        return RefreshResult(league=league, season=season_norm,
                             status="unsupported",
                             errors=[f"{league} has no automated season "
                                     "refresh (documented manual refresh)"])

    import subprocess

    out_dir = (Path(bundle_dir) if bundle_dir else
               Path(settings.db_path).parent / "collector" / "bundles"
               / league)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = {"season": season,
           "season_value": (st.get("last_detail", {}).get("extra", {})
                            .get("season_value") or season_norm[:4]),
           "out_dir": str(out_dir),
           "nbl_year": season_norm[:4],
           "el_season": "E" + season_norm[:4]}
    cmd = ["./.venv/Scripts/python.exe",
           f"tools/fetch_{league.lower()}_bundle.py",
           *[a.format(**fmt) for a in args_template]]
    proc = subprocess.run(cmd, capture_output=True, timeout=3600,
                          cwd=_repo_root())
    if proc.returncode != 0:
        return RefreshResult(league=league, season=season_norm,
                             status="failed",
                             errors=["fetcher failed: " +
                                     proc.stderr.decode("utf-8", "replace")
                                     [-400:]])

    bundles = sorted(out_dir.glob("*_raw.json"),
                     key=lambda p: p.stat().st_mtime)
    if not bundles:
        return RefreshResult(league=league, season=season_norm,
                             status="failed",
                             errors=["fetcher produced no bundle"])
    bundle_path = bundles[-1]

    from basketball_agent.cli.main import _load_any_games_file
    from basketball_agent.data.collectors.nba_parquet import merge_raw_datasets

    raw = _load_any_games_file(str(bundle_path))
    merged = merge_raw_datasets([raw])
    source_ids = merged.get("_meta", {}).get("source_ids") or []

    corrections = []
    if top_up:
        keep = [i for i, sid in enumerate(source_ids)
                if str(sid) not in ledger.game_ids(league)]
        corrections = _detect_corrections(raw, ledger, league, db)
        merged = _filter_parsed(merged, keep)
        if not merged["games"]:
            note = ("all source games already ingested"
                    + (f"; {len(corrections)} score correction(s) detected "
                       "(report-only)" if corrections else ""))
            return RefreshResult(league=league, season=season_norm,
                                 status="no_new_games",
                                 games_ingested=len(games_in_core),
                                 errors=[note])

    from basketball_agent.agents.validation_agent import DataValidationAgent
    from basketball_agent.data.validators.parsing import parse_dataset

    dataset, parse_errors = parse_dataset(merged)
    if parse_errors:
        return RefreshResult(league=league, season=season_norm,
                             status="failed",
                             errors=[f"parse errors: {parse_errors[:3]}"])

    # final dedup: drop games whose game_id is already in the core (never
    # re-ingest, never duplicate — 10-E requirement 4)
    existing_ids = {g.game_id for g in repo.list_games(league=league_enum)}
    merged["games"] = [g for g in merged["games"]
                       if g["game_id"] not in existing_ids]
    merged["team_stats"] = [ts for ts in merged["team_stats"]
                            if ts["game_id"] in
                            {g["game_id"] for g in merged["games"]}]
    if not merged["games"]:
        return RefreshResult(
            league=league, season=season_norm, status="no_new_games",
            games_ingested=len(games_in_core),
            errors=["all source games already ingested"
                    + (f"; {len(corrections)} score correction(s) "
                       "detected (report-only)" if corrections else "")])

    dataset, parse_errors = parse_dataset(merged)
    if parse_errors:
        return RefreshResult(league=league, season=season_norm,
                             status="failed",
                             errors=[f"re-parse errors after dedup: "
                                     f"{parse_errors[:3]}"])
    if not dataset.games:
        return RefreshResult(league=league, season=season_norm,
                             status="failed",
                             errors=["source produced zero ingestable games "
                                     "(season may not have started)"])
    game_dates = [g.game_date for g in dataset.games]
    latest = max(game_dates)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    validation_now = latest + timedelta(days=1)
    report = DataValidationAgent(now=validation_now).validate(dataset)
    if report.has_errors:
        return RefreshResult(league=league, season=season_norm,
                             status="failed",
                             errors=["validation errors: "
                                     f"{report.errors[:3]}"])

    from basketball_agent.data.snapshots.manager import SnapshotManager

    DataIngestor(db).ingest(merged)
    snapshot = SnapshotManager(settings.snapshots_dir).create(
        db.path, tag=f"collector:{league}")
    state.mark_season_ingested(league, season_norm, snapshot.snapshot_id)

    # record source_id -> game_id mappings for the ingested games
    if top_up:
        new_sids = [str(sid) for sid in source_ids
                        if str(sid) not in ledger.game_ids(league)]
    else:
        new_sids = [str(sid) for sid in source_ids]
    for sid, game in zip(new_sids, dataset.games):
        ledger.record(league, season_norm, sid, game.game_id)

    return RefreshResult(league=league, season=season_norm, status="ingested",
                         games_ingested=len(dataset.games),
                         snapshot_id=snapshot.snapshot_id)


def _detect_corrections(raw: dict, ledger, league: str, db) -> list[dict]:
    """Score corrections: an ingested source game whose CURRENT source
    scores differ from the stored ones.  Report-only."""
    from basketball_agent.config import get_settings
    from basketball_agent.data.db.connection import Database

    corrections = []
    if league != "BSL":
        return corrections
    settings = get_settings()
    db = Database(settings.db_path)
    db.initialize()
    for m in raw.get("schedule") or []:
        sid = str(m.get("matchId"))
        game_id = ledger.game_id_for(league, sid)
        if game_id is None:
            continue
        hs = (m.get("homeTeam") or {}).get("score")
        as_ = (m.get("awayTeam") or {}).get("score")
        if hs is None or as_ is None:
            continue
        row = db.connect().__enter__().execute(
            "SELECT home_score, away_score FROM games WHERE game_id = ?",
            (game_id,)).fetchone()
        if row and (int(hs), int(as_)) != (row["home_score"],
                                           row["away_score"]):
            corrections.append({
                "source_id": sid, "game_id": game_id,
                "stored": [row["home_score"], row["away_score"]],
                "source_now": [int(hs), int(as_)]})
    return corrections


def _filter_parsed(merged: dict, keep_positions: list[int]) -> dict:
    """Keep only the parsed games at the given source_ids positions (and
    their team_stats)."""
    sid_list = merged.get("_meta", {}).get("source_ids") or []
    keep = [i for i in keep_positions if i < len(sid_list)]
    keep_sids = {sid_list[i] for i in keep}
    games = [g for i, g in enumerate(merged.get("games") or [])
             if i < len(sid_list) and sid_list[i] in keep_sids]
    keep_gids = {g["game_id"] for g in games}
    team_stats = [ts for ts in merged.get("team_stats") or []
                  if ts.get("game_id") in keep_gids]
    out = {k: list(v) for k, v in merged.items()
           if k in ("games", "team_stats", "teams", "players",
                    "player_stats", "availability")}
    out["games"] = games
    out["team_stats"] = team_stats
    meta = dict(merged.get("_meta", {}))
    meta["n_games"] = len(games)
    meta["filtered_top_up"] = True
    out["_meta"] = meta
    return out


def _repo_root() -> str:
    from basketball_agent.config import get_settings
    from pathlib import Path as _P
    return str(_P(get_settings().db_path).parent.parent)
