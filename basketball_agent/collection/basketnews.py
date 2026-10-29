"""BasketNews EuroLeague injury ingestion (Phase 12-J).

Bridge between the parsed report (collectors.basketnews_injuries) and the
core store:

* player identity is the NAME STRING until a roster source exists —
  first sight of a name upserts a ``players`` row
  (``ELP-<normalised-name>``); the id is deterministic, so re-runs and
  the audit converge on the same identity;
* each non-AVAILABLE record attaches to the team's UPCOMING SCHEDULED
  EuroLeague games inside the refresh window as a PlayerAvailability
  row (``source_time`` = fetch time < kickoff by construction — the
  leakage rule is structurally satisfied);
* the report's ``Round`` column is real targeting information: a record
  with parsed round targets only attaches to games of those rounds
  (rounds come from the season's raw Header bundle); open-ended records
  ("Indefinitely", "Long-term", "Out for season") attach to every game
  in the window.  AVAILABLE records are skipped (no information).

Raw snapshots are archived under ``storage/raw/basketnews/`` with a
sha256 manifest — the snapshot is the provenance of every availability
row (the site 403s plain clients, so snapshots come from a rendered
fetch; see collectors.basketnews_injuries).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from basketball_agent.data.collectors.basketnews_injuries import (
    ARTICLE_URL, BasketNewsReport, normalize_name,
)
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import PlayerAvailability
from basketball_agent.data.schemas.player import Player

SNAPSHOT_DIR = Path("storage/raw/basketnews")
SEASON_BUNDLE_DIR = Path("storage/raw/euroleague_live")


@dataclass
class BasketNewsAttachDelta:
    records_parsed: int = 0
    availability_written: int = 0
    players_registered: int = 0
    skipped_available: int = 0
    skipped_existing: int = 0
    skipped_round_mismatch: int = 0
    skipped_no_round_map: int = 0
    records_without_game: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _player_id_for(name: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in normalize_name(name))
    slug = "-".join(part for part in slug.split("-") if part)
    return f"ELP-{slug}"


def _rounds_by_game_id(bundle_path: Path) -> dict[str, int]:
    """``E2026-0068`` -> round int, from the season raw Header bundle."""
    if not bundle_path.exists():
        return {}
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    season_code = str(bundle.get("season_code") or "")
    for key, header in (bundle.get("headers") or {}).items():
        try:
            out[f"{season_code}-{int(key):04d}"] = int(
                str(header.get("Round")).strip())
        except (TypeError, ValueError):
            continue
    return out


def ensure_el_player(repo, name: str, team_id: str) -> tuple[str, bool]:
    """Upsert a name-keyed EL player row; returns (player_id, created)."""
    player_id = _player_id_for(name)
    existing = repo.get_player(player_id)
    created = existing is None
    repo.upsert_player(Player(
        player_id=player_id, league=League.EUROLEAGUE, name=name,
        current_team_id=team_id,
    ))
    return player_id, created


def attach_report(
    db,
    report: BasketNewsReport,
    *,
    now: datetime | None = None,
    window_days: int = 8,
    season_bundle_path: Path | None = None,
) -> BasketNewsAttachDelta:
    """Parsed report -> players + PlayerAvailability rows."""
    from basketball_agent.data.db.repository import Repository

    now = now or datetime.now(timezone.utc)
    repo = Repository(db) if not isinstance(db, Repository) else db
    delta = BasketNewsAttachDelta(records_parsed=len(report.records))

    rounds = _rounds_by_game_id(
        season_bundle_path
        or SEASON_BUNDLE_DIR / "euroleague_E2026_raw.json")

    end = now + timedelta(days=window_days)
    upcoming: dict[str, list] = {}
    for g in repo.list_games(status=GameStatus.SCHEDULED,
                             league=League.EUROLEAGUE):
        if now <= g.game_date <= end:
            for team_id in (g.home_team_id, g.away_team_id):
                upcoming.setdefault(team_id, []).append(g)

    seen: set[tuple[str, str]] = {
        (a.player_id, a.game_id) for a in repo.list_availability()}
    for record in report.records:
        try:
            if record.status == "AVAILABLE":
                delta.skipped_available += 1
                continue
            games = upcoming.get(record.team_id, [])
            if not games:
                delta.records_without_game += 1
                continue
            player_id, created = ensure_el_player(
                repo, record.player_name, record.team_id)
            if created:
                delta.players_registered += 1
            matched = False
            for g in games:
                game_round = rounds.get(g.game_id)
                if (record.round_targets is not None
                        and game_round is None):
                    delta.skipped_no_round_map += 1
                    continue
                if (record.round_targets is not None
                        and game_round not in record.round_targets):
                    delta.skipped_round_mismatch += 1
                    continue
                matched = True
                key = (player_id, g.game_id)
                if key in seen:
                    delta.skipped_existing += 1
                    continue
                repo.insert_availability(PlayerAvailability(
                    player_id=player_id, game_id=g.game_id,
                    status=record.status,
                    availability_probability=record.probability,
                    source_time=now, source="basketnews",
                ))
                seen.add(key)
                delta.availability_written += 1
            if not matched:
                delta.records_without_game += 1
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            delta.errors.append(
                f"{record.player_name}/{record.team_id}: "
                f"{type(exc).__name__}: {exc}")
    return delta


def save_snapshot(
    text: str,
    *,
    out_dir: Path | str = SNAPSHOT_DIR,
    fetched_at: datetime | None = None,
    url: str = ARTICLE_URL,
    n_records: int | None = None,
) -> Path:
    """Archive the rendered snapshot + manifest; returns the file path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = fetched_at or datetime.now(timezone.utc)
    stamp = fetched_at.strftime("%Y%m%d-%H%M")
    path = out_dir / f"injury_report_snapshot_{stamp}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    manifest = {
        "file": path.name,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "url": url,
        "fetched_at": fetched_at.isoformat(),
        "n_records": n_records,
        "acquisition": "rendered snapshot (site 403s plain HTTP clients)",
    }
    (out_dir / f"injury_report_snapshot_{stamp}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def latest_snapshot(out_dir: Path | str = SNAPSHOT_DIR) -> Path:
    """Newest snapshot file (by name timestamp), fail-loud if none."""
    out_dir = Path(out_dir)
    snapshots = sorted(out_dir.glob("injury_report_snapshot_*.md"))
    if not snapshots:
        raise FileNotFoundError(
            f"no BasketNews snapshots under {out_dir} — save the rendered "
            f"article ({ARTICLE_URL}) first")
    return snapshots[-1]
