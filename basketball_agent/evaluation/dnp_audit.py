"""DNP reconciliation audit for the BasketNews EuroLeague feed (12-J).

Measures what the OUT records got RIGHT: every ``basketnews`` OUT record
is joined to its game's Boxscore (12-man game rosters) and classified —

* ``false_alarm``      — the player actually played minutes>0;
* ``confirmed_out``    — rostered on the game sheet with 0 minutes;
* ``confirmed_absent`` — absent from the 12-man game roster entirely
  (consistent with the OUT claim; counted separately so the readout
  shows how much of the precision rests on absence inference — a
  name-format miss would also land here, which is why the two buckets
  are never merged silently);
* ``pending``          — the game has not finished yet;
* ``unresolved_names`` — reserved (currently always empty; kept for the
  schema so a future roster source can tighten the classification).

Precision = (confirmed_out + confirmed_absent) / decided.  Honest
boundary (docs/PHASE12_A_SPREAD_API.md 12-J): this measures PRECISION
ONLY.  Recall — players who missed a game but were never reported — is
not measurable without full rosters, because a non-reported healthy
player and a non-reported injured player look identical in a boxscore.

Boxscores are cached under ``storage/raw/basketnews/boxscores/``; missing
ones are fetched from the same live endpoint the fixtures bootstrap uses
(cacheable, one request per game).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BOXSCORE_API_BASE = "https://live.euroleague.net/api"
BOXSCORE_CACHE_DIR = Path("storage/raw/basketnews/boxscores")


@dataclass
class DNPAuditResult:
    out_records_total: int = 0
    confirmed_out: int = 0          # rostered, 0 minutes
    confirmed_absent: int = 0       # absent from the 12-man game roster
    false_alarms: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    unresolved_names: list[dict] = field(default_factory=list)
    non_out_records: int = 0
    no_boxscore: int = 0

    @property
    def decided(self) -> int:
        return self.confirmed_out + self.confirmed_absent + len(
            self.false_alarms)

    @property
    def precision(self) -> float | None:
        confirmed = self.confirmed_out + self.confirmed_absent
        return (confirmed / self.decided) if self.decided else None

    def summary(self) -> dict:
        return {
            "out_records_total": self.out_records_total,
            "confirmed_out": self.confirmed_out,
            "confirmed_absent": self.confirmed_absent,
            "false_alarms": len(self.false_alarms),
            "pending": len(self.pending),
            "unresolved_names": len(self.unresolved_names),
            "non_out_records": self.non_out_records,
            "no_boxscore": self.no_boxscore,
            "decided": self.decided,
            "out_precision": self.precision,
        }


def _played_minutes(raw) -> int:
    """``'22:04'`` -> 22; ``'0:00'``/missing -> 0."""
    text = str(raw or "").strip()
    if not text:
        return 0
    head = text.split(":")[0]
    try:
        return int(head)
    except ValueError:
        return 0


def _boxscore_participation(box: dict) -> dict[str, int]:
    """Normalised player name -> minutes played, from one Boxscore."""
    from basketball_agent.data.collectors.basketnews_injuries import (
        normalize_name,
    )

    out: dict[str, int] = {}
    for team in box.get("Stats", []):
        for row in team.get("PlayersStats", []):
            name = str(row.get("Player") or "").strip()
            if not name:
                continue
            if "," in name:
                last, first = name.split(",", 1)
                joined = f"{first.strip()} {last.strip()}"
            else:
                joined = name
            key = normalize_name(joined)
            out[key] = max(out.get(key, 0), _played_minutes(row.get("Minutes")))
    return out


def _boxscore_for_game(
    repo, game_id: str, *, cache_dir: Path, fetch_missing: bool,
    fetch_fn=None,
) -> dict | None:
    path = cache_dir / f"{game_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not fetch_missing:
        return None
    season_code, code_text = game_id.split("-", 1)
    url = (f"{BOXSCORE_API_BASE}/Boxscore?gameCode={int(code_text)}"
           f"&seasonCode={season_code}")
    fetch = fetch_fn or _default_fetch
    try:
        box = fetch(url)
    except Exception:  # noqa: BLE001 — per-row isolation, counted upstream
        return None
    if not isinstance(box, dict) or not box.get("Stats"):
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(box, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return box


def _default_fetch(url: str) -> dict:
    import time

    from basketball_agent.collection.sources import _get_json

    payload = _get_json(url)
    time.sleep(0.4)
    return payload


def audit_basketnews_out(
    db,
    *,
    now: datetime | None = None,
    lookback_days: int = 21,
    cache_dir: Path | str = BOXSCORE_CACHE_DIR,
    fetch_missing: bool = True,
    fetch_fn=None,
) -> DNPAuditResult:
    """Reconcile every stored basketnews OUT record with actual participation."""
    from basketball_agent.data.db.repository import Repository
    from basketball_agent.data.schemas.base import GameStatus

    now = now or datetime.now(timezone.utc)
    repo = Repository(db) if not isinstance(db, Repository) else db
    cache_dir = Path(cache_dir)
    result = DNPAuditResult()

    for a in repo.list_availability():
        if a.source != "basketnews":
            continue
        game = repo.get_game(a.game_id)
        if game is None:
            continue
        player = repo.get_player(a.player_id)
        player_name = player.name if player else a.player_id
        if a.status != "OUT":
            if game.status is GameStatus.FINAL:
                result.non_out_records += 1
            continue
        result.out_records_total += 1
        if game.status is not GameStatus.FINAL:
            result.pending.append({"game_id": a.game_id,
                                   "player": player_name})
            continue
        box = _boxscore_for_game(
            repo, a.game_id, cache_dir=cache_dir,
            fetch_missing=fetch_missing, fetch_fn=fetch_fn)
        if box is None:
            result.no_boxscore += 1
            continue
        participation = _boxscore_participation(box)
        from basketball_agent.data.collectors.basketnews_injuries import (
            normalize_name,
        )

        key = normalize_name(player_name)
        if key in participation:
            if participation[key] > 0:
                result.false_alarms.append({
                    "game_id": a.game_id, "player": player_name,
                    "minutes": participation[key],
                    "reported_at": a.source_time.isoformat()
                    if a.source_time else None})
            else:
                result.confirmed_out += 1  # rostered, did not play
        else:
            # absent from the 12-man game roster — consistent with the
            # OUT claim; counted separately so the precision readout
            # shows how much rests on absence inference
            result.confirmed_absent += 1
    return result


def audit_snapshot_out(
    db,
    snapshot_path: Path | str,
    *,
    as_of: datetime | None = None,
    lookback_days: int = 7,
    season_bundle_path: Path | str | None = None,
    cache_dir: Path | str = BOXSCORE_CACHE_DIR,
    fetch_missing: bool = True,
    fetch_fn=None,
) -> DNPAuditResult:
    """Historical mode: reconcile a dated REPORT SNAPSHOT directly.

    Availability rows only ever attach to FUTURE games (leakage rule),
    so yesterday's report cannot be reconciled through the store.  This
    mode takes an archived snapshot, resolves its OUT records against the
    team's FINAL games inside ``[as_of - lookback_days, as_of]`` whose
    round matches the record's Round targeting (open-ended records match
    any round in the window), and classifies against the boxscore.
    Measurement only — nothing is written to the store.
    """
    from datetime import timedelta

    from basketball_agent.collection.basketnews import _rounds_by_game_id
    from basketball_agent.data.collectors.basketnews_injuries import (
        load_snapshot_file, normalize_name, parse_report,
    )
    from basketball_agent.data.db.repository import Repository
    from basketball_agent.data.schemas.base import GameStatus, League

    now = as_of or datetime.now(timezone.utc)
    repo = Repository(db) if not isinstance(db, Repository) else db
    cache_dir = Path(cache_dir)
    result = DNPAuditResult()

    report = parse_report(load_snapshot_file(snapshot_path), fetched_at=now)
    rounds = _rounds_by_game_id(Path(season_bundle_path)) if (
        season_bundle_path) else {}

    for record in report.records:
        if record.status != "OUT":
            result.non_out_records += 1
            continue
        start = now - timedelta(days=lookback_days)
        candidates = [
            g for g in repo.list_games(league=League.EUROLEAGUE,
                                       status=GameStatus.FINAL)
            if g.home_team_id == record.team_id
            or g.away_team_id == record.team_id]
        candidates = [
            g for g in candidates
            if start <= g.game_date <= now]
        if record.round_targets is not None and rounds:
            candidates = [g for g in candidates
                          if rounds.get(g.game_id)
                          in record.round_targets]
        if not candidates:
            continue
        for g in candidates:
            result.out_records_total += 1
            box = _boxscore_for_game(
                repo, g.game_id, cache_dir=cache_dir,
                fetch_missing=fetch_missing, fetch_fn=fetch_fn)
            if box is None:
                result.no_boxscore += 1
                continue
            participation = _boxscore_participation(box)
            key = normalize_name(record.player_name)
            if key in participation:
                if participation[key] > 0:
                    result.false_alarms.append({
                        "game_id": g.game_id, "player": record.player_name,
                        "minutes": participation[key],
                        "reported_at": record.round_raw})
                else:
                    result.confirmed_out += 1
            else:
                result.confirmed_absent += 1
    return result
