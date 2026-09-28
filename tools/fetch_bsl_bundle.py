"""Fetch one BSL (Turkish Basketbol Süper Ligi) season into a raw bundle.

Phase 8-F source decision (documented, all verified against live responses):

* B0's primary path is FIBA LiveStats.  Per-game LiveStats data.json works
  (Genius Sports host ``fibalivestats.dcd.shared.geniussports.com/data/
  {gameId}/data.json`` — the legacy ``fibalivestats.com/u/TBF/{id}`` URL
  redirects there and the data files are public), but LiveStats exposes NO
  schedule -> gameId mapping (same conclusion as NBL Phase 8-D:
  unavailable-for-enumeration).
* The enumeration source is therefore the TBF (Turkish Basketball
  Federation) official API behind ``www.tbf.org.tr`` (Nuxt SPA):
  ``/api/Match/get-all-matches-for-filter?ActivityId={id}&TeamId=&Page=1&
  PageSize=-1&WeekFilter=*`` returns EVERY match of one league season
  including ``genuisId`` — the LiveStats gameId — plus official scores,
  so schedule and box scores are two independent sources (cross-checked
  by the adapter).  Season ids from
  ``/api/League/get-leagues-and-seasons-by-prefix?prefix=bsl``.
  NOTE: www.tbf.org.tr fails TLS-1.3 negotiation with this machine's
  OpenSSL (exit 35) but works with ``--tlsv1.2 --tls-max 1.2``; API paths
  are Cloudflare-challenged for plain HTML clients only via the page
  route, the JSON endpoints answer directly with the right TLS.

The bundle is SLIMMED at fetch time: LiveStats play-by-play, shot data,
scoring list, player rows and lead data are dropped (not part of the
team-level contract); team-level official totals (``tot_s*``) and period
scores are kept.  BSL plays FIBA rules (4 x 10 min, 5-minute OT).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TBF = "https://www.tbf.org.tr"
LS = "https://fibalivestats.dcd.shared.geniussports.com"
DATASET_MARKER = "bsl-livestats-v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# www.tbf.org.tr fails TLS-1.3 with this machine's OpenSSL; TLS 1.2 works
CURL_TLS = ["--tlsv1.2", "--tls-max", "1.2"]

TBF_MATCH_KEYS = (
    "matchId", "activityId", "activityName", "seasonId", "season",
    "matchDate", "matchDateOnly", "matchTime", "week", "halfValue",
    "matchStatusId", "hasUzatma", "genuisId", "salonAdi",
)

LS_TEAM_KEYS = (
    "code", "codeInternational", "name", "shortName", "score", "full_score",
    "tot_sMinutes", "tot_sFieldGoalsMade", "tot_sFieldGoalsAttempted",
    "tot_sTwoPointersMade", "tot_sTwoPointersAttempted",
    "tot_sThreePointersMade", "tot_sThreePointersAttempted",
    "tot_sFreeThrowsMade", "tot_sFreeThrowsAttempted",
    "tot_sFreeThrowsPercentage",
    "tot_sReboundsOffensive", "tot_sReboundsDefensive", "tot_sReboundsTotal",
    "tot_sReboundsTeam", "tot_sReboundsTeamOffensive",
    "tot_sReboundsTeamDefensive",
    "tot_sAssists", "tot_sTurnovers", "tot_sTurnoversTeam", "tot_sSteals",
    "tot_sBlocks", "tot_sFoulsPersonal", "tot_sFoulsTeam", "tot_sFoulsTotal",
    "tot_sPoints", "tot_sBenchPoints", "tot_sPointsInThePaint",
    "tot_sPointsSecondChance", "tot_sPointsFastBreak",
    "tot_sPointsFromTurnovers",
    "fouls", "timeouts",
)
LS_PERIOD_KEYS = tuple(f"p{i}_score" for i in range(1, 9))
LS_META_KEYS = (
    "inOT", "period", "periodType", "periodLength", "periodLengthOVERTIME",
    "periodsMax", "attendance", "disableMatch", "clock",
)


def _curl(url: str, attempts: int = 4, pause: float = 0.4) -> bytes | None:
    last = None
    for i in range(attempts):
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "45", "-A", UA, *CURL_TLS,
                 "-H", f"Referer: {TBF}/", url],
                capture_output=True, timeout=60)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            last = RuntimeError(f"curl rc={r.returncode}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(pause * (2 ** i))
    print(f"TBF FETCH FAILED {url}: {last}", file=sys.stderr)
    return None


def _tbf_json(path: str, pause: float = 0.4):
    raw = _curl(f"{TBF}{path}", pause=pause)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


def _ls_json(game_id: int, attempts: int = 4, pause: float = 0.4):
    url = f"{LS}/data/{game_id}/data.json"
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(pause * (2 ** i))
    print(f"LIVESTATS FETCH FAILED {url}: {last}", file=sys.stderr)
    return None


def slim_livestats(d: dict) -> dict:
    out = {k: d.get(k) for k in LS_META_KEYS}
    teams = {}
    for k in ("1", "2"):
        t = d.get("tm", {}).get(k) or {}
        slim = {kk: t.get(kk) for kk in LS_TEAM_KEYS + LS_PERIOD_KEYS}
        # presence flags for the dropped per-player/per-event assets (record
        # only — Phase 8-F does not extend the schema)
        slim["_has_player_rows"] = bool(t.get("pl"))
        teams[k] = slim
    out["teams"] = teams
    out["_asset_flags"] = {
        "pbp_dropped": bool(d.get("pbp")),
        "shot_chart_dropped": bool((d.get("tm", {}).get("1") or {}).get("shot")),
        "scoring_dropped": bool((d.get("tm", {}).get("1") or {}).get("scoring")),
        "lead_data_dropped": bool(d.get("leaddata")),
        "timeline_dropped": bool(d.get("timeline")),
    }
    out["boxscore_source"] = ("FIBA LiveStats (Genius Sports) team totals; "
                              "pbp/shots/player rows dropped at fetch time")
    return out


def slim_match(m: dict) -> dict:
    slim = {k: m.get(k) for k in TBF_MATCH_KEYS}
    for side in ("homeTeam", "awayTeam"):
        t = m.get(side) or {}
        slim[side] = {k: t.get(k) for k in ("id", "name", "score")}
    return slim


# --- TBF-native box scores (seasons where the schedule carries no
# genuisId — e.g. BSL 2024-25).  ``api/Match/mac-istatistik`` publishes the
# official team totals; normalised here to the same field names the
# LiveStats rows use so one adapter path validates both.

_TBF_BOX_MAP = {
    "points": "score",
    "minutesPlayed": "tot_sMinutes",
    "successfulShots": "tot_sFieldGoalsMade",
    "shotAttempts": "tot_sFieldGoalsAttempted",
    "twoPointShots": "tot_sTwoPointersMade",
    "twoPointShotAttempts": "tot_sTwoPointersAttempted",
    "threePointShots": "tot_sThreePointersMade",
    "threePointShotAttempts": "tot_sThreePointersAttempted",
    "freeThrows": "tot_sFreeThrowsMade",
    "freeThrowAttempts": "tot_sFreeThrowsAttempted",
    "offensiveRebounds": "tot_sReboundsOffensive",
    "defensiveRebounds": "tot_sReboundsDefensive",
    "totalRebounds": "tot_sReboundsTotal",
    "assists": "tot_sAssists",
    "turnovers": "tot_sTurnovers",
    "steals": "tot_sSteals",
    "blocks": "tot_sBlocks",
    "fouls": "tot_sFoulsPersonal",
}


def fetch_tbf_box(match_id: int, pause: float) -> dict | None:
    raw = _curl(f"{TBF}/api/Match/mac-istatistik?matchId={match_id}",
                pause=pause)
    if raw is None:
        return None
    d = json.loads(raw.decode("utf-8")).get("data") or {}
    home = d.get("homeTeamTotal") or {}
    away = d.get("awayTeamTotal") or {}
    if not home or not away:
        return None

    def norm(t: dict) -> dict:
        row = {ls_key: t.get(tbf_key) for tbf_key, ls_key in _TBF_BOX_MAP.items()}
        row["code"] = ""
        row["name"] = t.get("teamName") or ""
        row["full_score"] = t.get("points")
        return row

    return {
        "box_source": "tbf-native",
        "periodLength": 10,
        "periodLengthOVERTIME": 5,
        "boxscore_source": ("TBF native match statistics (mac-istatistik "
                            "team totals; no LiveStats feed for this season)"),
        "teams": {"1": norm(home), "2": norm(away)},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2025-2026",
                    help="BSL season label, e.g. 2025-2026")
    ap.add_argument("--out-dir", default=str(Path("C:/BSL_DATA")))
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    y0, y1 = args.season.split("-")
    bundle_path = out_dir / f"bsl_{y0[2:]}{y1[2:]}_raw.json"

    seasons = _tbf_json("/api/League/get-leagues-and-seasons-by-prefix?prefix=bsl",
                        pause=args.pause)
    if not seasons:
        print("season list fetch failed", file=sys.stderr)
        return 1
    entry = next((s for s in seasons.get("data") or []
                  if s.get("sezon") == args.season), None)
    if entry is None:
        print(f"season {args.season} not found in the TBF list", file=sys.stderr)
        return 1
    activity_id = int(entry["faaliyet_ID"])
    season_id = int(entry["sezon_ID"])
    print(f"season {args.season}: activityId={activity_id} seasonId={season_id}")

    matches = _tbf_json(
        "/api/Match/get-all-matches-for-filter"
        f"?ActivityId={activity_id}&TeamId=&Page=1&PageSize=-1&WeekFilter=*",
        pause=args.pause)
    if not matches:
        print("match list fetch failed", file=sys.stderr)
        return 1
    rows = matches.get("data") or []
    print(f"TBF schedule: {len(rows)} matches (count={matches.get('count')})")

    bundle = {
        "dataset": DATASET_MARKER,
        "league": "BSL",
        "season": args.season,
        "activity_id": activity_id,
        "season_id": season_id,
        "source": (
            f"{TBF}/api/Match/get-all-matches-for-filter (TBF official API; "
            "schedule + genuisId) + " + LS + "/data/{id}/data.json "
            "(FIBA LiveStats via Genius Sports; team-level box scores)"
        ),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "schedule": [slim_match(m) for m in rows],
        "livestats": {},
        "tbf_boxes": {},
    }
    if args.resume and bundle_path.exists():
        old = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["livestats"] = old.get("livestats", {})
        bundle["tbf_boxes"] = old.get("tbf_boxes", {})
        print(f"resume: {len(bundle['livestats'])} livestats, "
              f"{len(bundle['tbf_boxes'])} tbf boxes already present")

    misses = []
    # matches without a final score are not played yet — their box data
    # does not exist and fetching it would burn failing requests
    def _played(m):
        return m.get("homeTeam", {}).get("score") not in (None, "") and             m.get("awayTeam", {}).get("score") not in (None, "")
    no_feed = [m for m in rows
               if not m.get("genuisId") and _played(m)]
    skipped_unplayed = sum(1 for m in rows
                           if not m.get("genuisId") and not _played(m))
    if skipped_unplayed:
        print(f"{skipped_unplayed} unplayed matches skipped (no box fetch)")
    if no_feed:
        print(f"{len(no_feed)} played matches without genuisId -> "
              "TBF-native boxes")
    todo = [m for m in rows if m.get("genuisId")]
    for i, m in enumerate(todo, 1):
        gid = int(m["genuisId"])
        if str(gid) in bundle["livestats"]:
            continue
        d = _ls_json(gid, pause=args.pause)
        if d is None or not (d.get("tm") or {}).get("1"):
            misses.append(gid)
            continue
        bundle["livestats"][str(gid)] = slim_livestats(d)
        if i % 25 == 0:
            bundle["fetched_at"] = datetime.now(timezone.utc).isoformat()
            bundle_path.write_text(json.dumps(bundle, ensure_ascii=False),
                                   encoding="utf-8")
            print(f"  progress: {len(bundle['livestats'])}/{len(todo)}")
        time.sleep(args.pause)

    box_misses = []
    for i, m in enumerate(no_feed, 1):
        mid = int(m["matchId"])
        if str(mid) in bundle["tbf_boxes"]:
            continue
        box = fetch_tbf_box(mid, pause=args.pause)
        if box is None:
            box_misses.append(mid)
            continue
        bundle["tbf_boxes"][str(mid)] = box
        if i % 25 == 0:
            bundle["fetched_at"] = datetime.now(timezone.utc).isoformat()
            bundle_path.write_text(json.dumps(bundle, ensure_ascii=False),
                                   encoding="utf-8")
            print(f"  progress: {len(bundle['tbf_boxes'])}/{len(no_feed)}")
        time.sleep(args.pause)

    bundle["fetched_at"] = datetime.now(timezone.utc).isoformat()
    bundle["n_matches"] = len(rows)
    bundle["n_livestats"] = len(bundle["livestats"])
    bundle["n_tbf_boxes"] = len(bundle["tbf_boxes"])
    bundle["livestats_misses"] = misses
    bundle["tbf_box_misses"] = box_misses
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False),
                           encoding="utf-8")
    print(f"bundle: {bundle_path} matches={len(rows)} "
          f"livestats={len(bundle['livestats'])} "
          f"tbf_boxes={len(bundle['tbf_boxes'])} "
          f"misses={len(misses)}/{len(box_misses)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
