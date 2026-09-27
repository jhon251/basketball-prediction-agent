"""Fetch one NBL season from the official NBL APIs into a raw bundle.

Sources (verified Phase 8-D against real responses):

* ``https://schedule.nbl.com.au/api/calendar/schedule?league=nbl&year={Y}``
  -> the season calendar: every match with id (uuid), round_label,
  season_type, home/away teams (official ``team_code``), scores,
  ``status_raw``/``phase``;
* ``https://schedule.nbl.com.au/api/calendar/match?match={uuid}`` ->
  per-match detail incl. ``team_match_statistics`` (per-period rows; the
  ``period=="0"`` rows are the official game totals that reconcile with the
  final score).

Phase 8-D source decision (documented): B0 preferred the FIBA LiveStats
``data.json`` path, but LiveStats game ids have NO public mapping from the
NBL schedule (the id is only discoverable per-game via search engines), while
the NBL's own API exposes the complete official box scores for every game.
The official API is therefore the primary source (ACB precedent); the
LiveStats route is recorded as unavailable-for-enumeration.

The bundle is SLIMMED at fetch time: ``play_by_play``,
``player_match_statistics`` and other oversized blocks are dropped (not part
of the team-level contract); full per-match payloads are additionally
preserved verbatim under ``<out-dir>/full/`` outside the pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://schedule.nbl.com.au/api/calendar"
DATASET_MARKER = "nbl-api-v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

KEEP_MATCH_KEYS = (
    "id", "match_status", "start_time_datetime", "match_type", "attendance",
    "home_score", "away_score", "round", "season", "venue", "match_slug",
    "match_title", "home_team", "away_team", "season_type",
)
KEEP_TEAM_STATS_KEYS = (
    "period", "period_type", "minutes", "points", "field_goals_made",
    "field_goals_attempted", "three_points_made", "three_points_attempted",
    "free_throws_made", "free_throws_attempted", "offensive_rebounds",
    "defensive_rebounds", "assists", "turnovers", "steals", "blocks", "fouls",
)


def fetch_json(url: str, attempts: int = 4, pause: float = 0.4):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(pause * (2 ** i))
    print(f"FETCH FAILED {url}: {last}", file=sys.stderr)
    return None


def slim_detail(detail: dict) -> dict:
    m = detail.get("match") or {}
    slim = {k: m.get(k) for k in KEEP_MATCH_KEYS}
    slim["team_match_statistics"] = [
        {k: row.get(k) for k in KEEP_TEAM_STATS_KEYS}
        for row in (m.get("team_match_statistics") or [])
    ]
    slim["boxscore_source"] = "nbl official match api (team-level rows only; " \
        "play_by_play and player rows dropped at fetch time, full payloads " \
        "archived beside the bundle)"
    return slim


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True,
                    help="NBL season START year (e.g. 2025 -> NBL26, 2025-26)")
    ap.add_argument("--out-dir", default=str(Path("C:/NBL_DATA")))
    ap.add_argument("--pause", type=float, default=0.35)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    full_dir = out_dir / "full"
    full_dir.mkdir(exist_ok=True)
    bundle_path = out_dir / f"nbl_nbl{args.year % 100:02d}_raw.json"

    cal = fetch_json(f"{BASE}/schedule?league=nbl&offset=0&limit=500&year={args.year}")
    if cal is None:
        print("calendar fetch failed", file=sys.stderr)
        return 1
    matches = cal.get("matches") or []
    finished = [m for m in matches if m.get("phase") == "complete"]
    # exhibition games (preseason / Blitz / NBAxNBL) are dropped by the
    # adapter anyway — don't spend detail requests on them
    exhibition_labels = ("PRESEASON", "BLITZ", "NBAXNBL", "PG")
    exhibitions = [m for m in finished
                   if str(m.get("season_type")) == "preseason"
                   or any(lbl in str(m.get("round_label", "")).upper()
                          for lbl in exhibition_labels)]
    finished = [m for m in finished if m not in exhibitions]
    print(f"calendar: {len(matches)} matches, {len(finished)} complete "
          f"({len(exhibitions)} exhibitions skipped)")

    bundle = {
        "dataset": DATASET_MARKER,
        "league": "NBL",
        "year": args.year,
        "source": f"{BASE}/schedule + {BASE}/match",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "n_calendar_matches": len(matches),
        "n_complete": len(finished),
        "seasons_meta": cal.get("seasons_meta"),
        "calendar": cal,
        "matches": {},
    }
    if args.resume and bundle_path.exists():
        old = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["matches"] = old.get("matches", {})
        print(f"resume: {len(bundle['matches'])} details already present")

    misses = []
    for i, m in enumerate(finished, 1):
        mid = m["id"]
        if mid in bundle["matches"]:
            continue
        detail = fetch_json(f"{BASE}/match?match={mid}", pause=args.pause)
        time.sleep(args.pause)
        if detail is None or not (detail.get("match") or {}).get("team_match_statistics"):
            misses.append(mid)
            continue
        (full_dir / f"nbl_match_{mid}.json").write_text(
            json.dumps(detail, ensure_ascii=False), encoding="utf-8")
        bundle["matches"][mid] = slim_detail(detail)
        if i % 25 == 0:
            bundle["fetched_at"] = datetime.now(timezone.utc).isoformat()
            bundle["n_details"] = len(bundle["matches"])
            bundle_path.write_text(json.dumps(bundle, ensure_ascii=False),
                                   encoding="utf-8")
            print(f"  progress: {len(bundle['matches'])}/{len(finished)}")

    bundle["fetched_at"] = datetime.now(timezone.utc).isoformat()
    bundle["n_details"] = len(bundle["matches"])
    bundle["detail_misses"] = misses
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    print(f"bundle: {bundle_path} details={len(bundle['matches'])} misses={len(misses)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
