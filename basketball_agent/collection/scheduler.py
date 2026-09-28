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
                   n_simulations: int | None = None) -> RefreshResult:
    """Season-refresh gate: onboard ONE league-season into the Prediction
    Core through the existing validate -> ingest -> snapshot mechanisms.

    Gate: the league-season must have ZERO games in the core (first
    onboarding).  A partially/fully ingested season is refused (game ids
    are assigned per ingestion pass — incremental top-up is a documented
    open item).  The fetcher subprocess is the existing, battle-tested
    tool; its bundle flows through the frozen adapters and validation.
    """
    from basketball_agent.config import get_settings
    from basketball_agent.collection.state import CollectorState

    settings = get_settings()
    state = CollectorState(state_dir)
    st = state.load(league)
    discovered_season = st.get("last_season")
    season = season or discovered_season
    if not season:
        return RefreshResult(league=league, season=season or "?",
                             status="failed",
                             errors=["no season known for this league; run "
                                     "a discovery pass first"])

    # gate: the season must be absent from the core.  The source label
    # (e.g. TBF's "2026-2027") is normalised to the adapter's season label
    # ("2026-27") so the gate compares like with like.
    from basketball_agent.data.schemas.base import League

    def _norm_season(label: str) -> str:
        parts = str(label).split("-")
        if len(parts) == 2 and len(parts[1]) == 4:
            return f"{parts[0]}-{parts[1][-2:]}"
        return str(label)

    league_enum = League(league)
    season_norm = _norm_season(season)
    games = [g for g in repo.list_games(league=league_enum)
             if g.season == season_norm]
    if games:
        season = season_norm
        return RefreshResult(league=league, season=season,
                             status="already_ingested",
                             games_ingested=len(games),
                             errors=["season already present in the core "
                                     f"({len(games)} games); incremental "
                                     "top-up is not supported yet"])

    args_template = _REFRESH_ARGS.get(league)
    if args_template is None:
        return RefreshResult(league=league, season=season, status="unsupported",
                             errors=[f"{league} has no automated season "
                                     "refresh (documented manual refresh)"])

    import subprocess

    def _repo_root() -> str:
        return str(Path(settings.db_path).parent.parent)

    out_dir = Path(settings.db_path).parent / "collector" / "bundles" / league
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = {"season": season,
           "season_value": st.get("last_detail", {}).get(
               "extra", {}).get("season_value") or season.split("-")[0],
           "out_dir": str(out_dir),
           "nbl_year": season.split("-")[0],
           "el_season": "E" + season.split("-")[0]}
    cmd = ["./.venv/Scripts/python.exe", f"tools/fetch_{league.lower()}_bundle.py",
           *[a.format(**fmt) for a in args_template]]
    proc = subprocess.run(cmd, capture_output=True, timeout=3600,
                          cwd=_repo_root())
    if proc.returncode != 0:
        return RefreshResult(league=league, season=season, status="failed",
                             errors=[f"fetcher failed: "
                                     f"{proc.stderr.decode('utf-8', 'replace')[-400:]}"])

    # locate the produced bundle (newest *_raw.json in the out-dir)
    bundles = sorted(out_dir.glob("*_raw.json"), key=lambda p: p.stat().st_mtime)
    if not bundles:
        return RefreshResult(league=league, season=season, status="failed",
                             errors=["fetcher produced no bundle"])
    bundle_path = bundles[-1]

    # frozen validate -> ingest -> snapshot path (mirrors cli ingest)
    from basketball_agent.cli.main import _load_any_games_file
    from basketball_agent.data.collectors.nba_parquet import merge_raw_datasets
    from basketball_agent.data.db.loader import DataIngestor
    from basketball_agent.data.validators.parsing import parse_dataset
    from basketball_agent.agents.validation_agent import DataValidationAgent
    from basketball_agent.data.snapshots.manager import SnapshotManager

    raw = _load_any_games_file(str(bundle_path))
    merged = merge_raw_datasets([raw])
    dataset, parse_errors = parse_dataset(merged)
    if parse_errors:
        return RefreshResult(league=league, season=season, status="failed",
                             errors=[f"parse errors: {parse_errors[:3]}"])
    game_dates = [g.game_date for g in dataset.games]
    latest = max(game_dates)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    validation_now = latest + timedelta(days=1)
    report = DataValidationAgent(now=validation_now).validate(dataset)
    if report.has_errors:
        return RefreshResult(league=league, season=season, status="failed",
                             errors=[f"validation errors: {report.errors[:3]}"])

    DataIngestor(db).ingest(merged)
    snapshot = SnapshotManager(settings.snapshots_dir).create(
        db.path, tag=f"collector:{league}")
    state.mark_season_ingested(league, season, snapshot.snapshot_id)
    return RefreshResult(league=league, season=season, status="ingested",
                         games_ingested=len(dataset.games),
                         snapshot_id=snapshot.snapshot_id)
