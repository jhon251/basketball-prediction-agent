"""Live discovery clients for the collector (Phase 10-E).

Light, read-only probes of the VERIFIED production endpoints (each one
exercised in earlier phases against live responses).  Discovery answers
"what does the source have right now" — status/score/schedule facts only;
box-score fetching and ingestion happen through the season-refresh path
(``scheduler.py``) and the frozen adapters.

 NBA                — llimllib/nba_data parquet: source freshness via the
                      GitHub commits API (file-refresh source; ingestion
                      stays the documented manual refresh)
 EuroLeague         — live.euroleague.net API reachability probe
 ACB               — official API season listing (fetcher --list-seasons
                      endpoint)
 NBL                — official calendar API (per-match status/scores)
 CBA                — official data-server (season list + month schedule
                      counts)
 BSL                — TBF official API (current season + per-match
                      status/scores)

No LLM, no scraping of unverified sources; every client degrades to a
recorded error (never a silent pass).
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TBF = "https://www.tbf.org.tr"
TBF_TLS = ["--tlsv1.2", "--tls-max", "1.2"]  # mirror tools/fetch_bsl_bundle.py
CBA_DATA = "https://data-server.cbaleague.com"
CBA_REFERER = "https://www.cbaleague.com/data/"
NBA_COMMITS = ("https://api.github.com/repos/llimllib/nba_data/commits"
               "?per_page=1")
EL_BASE = "https://live.euroleague.net/api"
NBL_CAL = "https://schedule.nbl.com.au/api/calendar/schedule"


def _get_json(url: str, *, headers: dict | None = None,
              attempts: int = 3, pause: float = 0.5):
    import time

    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — collector telemetry
            last = exc
            time.sleep(pause * (2 ** i))
    raise RuntimeError(f"discovery fetch failed {url}: {last}")


def _http_get(url: str, *, headers: dict | None = None) -> tuple[int, bytes | None]:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None


# ----------------------------------------------------------------- BSL / TBF
def _tbf_get_json(path: str, attempts: int = 3) -> dict:
    """TBF requires TLS 1.2 on this machine (8-F finding: TLS-1.3
    negotiation fails with the local OpenSSL) — reuse the proven curl
    subprocess path, with retry/backoff for transient TLS drops."""
    import subprocess
    import time

    last = None
    for i in range(attempts):
        r = subprocess.run(
            ["curl", "-s", "--max-time", "45", "-A", UA, *TBF_TLS,
             "-H", f"Referer: {TBF}/", f"{TBF}{path}"],
            capture_output=True, timeout=60)
        if r.returncode == 0 and r.stdout:
            try:
                return json.loads(r.stdout.decode("utf-8"))
            except json.JSONDecodeError as exc:
                last = exc
        else:
            last = RuntimeError(f"rc={r.returncode}")
        time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"TBF fetch failed {path}: {last}")


def bsl_current_season() -> dict:
    """Current BSL season label + ids from the TBF API."""
    data = _tbf_get_json("/api/League/get-leagues-and-seasons-by-prefix"
                         "?prefix=bsl")
    entry = (data.get("data") or [None])[0]
    if not entry:
        raise RuntimeError("BSL season list empty")
    return {"season": entry["sezon"], "activity_id": int(entry["faaliyet_ID"]),
            "season_id": int(entry["sezon_ID"])}


def bsl_matches(activity_id: int, season_id: int) -> list[dict]:
    """Every match of one BSL season (light rows: id/status/scores)."""
    data = _tbf_get_json(
        "/api/Match/get-all-matches-for-filter"
        f"?ActivityId={activity_id}&TeamId=&Page=1&PageSize=-1&WeekFilter=*")
    rows = []
    for r in data.get("data") or []:
        home, away = r.get("homeTeam") or {}, r.get("awayTeam") or {}
        rows.append({
            "source_id": str(r.get("matchId")),
            "date": (r.get("matchDateOnly") or "")[:10],
            "match_time": (r.get("matchTime") or "00:00"),
            "status_raw": r.get("matchStatusId"),
            "home": home.get("name"), "away": away.get("name"),
            "home_id": home.get("id"), "away_id": away.get("id"),
            "home_score": home.get("score"), "away_score": away.get("score"),
            "has_ot": bool(r.get("hasUzatma")),
        })
    return rows


# ----------------------------------------------------------------- NBL
def _nbl_kickoff_utc(m: dict) -> tuple[str, str]:
    """Kickoff ``(date, HH:MM)`` in UTC for one NBL calendar row.

    The 2026-27 calendar dropped the legacy ``start_time_datetime`` text
    field; the only kickoff fact left is the epoch-millis
    ``starts_at_ms`` (present on every row).  The production loop stamps
    NBL rows UTC (``_league_tz``), so UTC is the contract here.  Returns
    ``("", "")`` when neither field yields a date — such rows are counted
    by the upsert pass as ``skipped_no_date``, never guessed.
    """
    ms = m.get("starts_at_ms")
    if ms:
        ms = float(ms)
        if ms < 1e11:  # seconds-shaped epoch -> normalise to millis
            ms *= 1000.0
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    legacy = str(m.get("start_time_datetime") or "")
    if legacy[:10]:
        return legacy[:10], (legacy[11:16] or "00:00")
    return "", ""


def _nbl_team_ref(team: dict) -> str:
    """Canonical team ref for an upsert row: ``NBL-{team_code}`` — the
    same id shape the core's teams table and the season-refresh fetcher
    use (via ``nbl_api.resolve_team``).  Falls back to the raw source id
    when the code is implausible/missing; a row with an unresolvable ref
    then fails its own FK check (per-row isolation) instead of poisoning
    the whole discovery."""
    team = team or {}
    code = str(team.get("team_code") or "").strip()
    try:
        from basketball_agent.data.collectors.nbl_api import resolve_team
        return resolve_team(code)
    except Exception:  # noqa: BLE001 — row-level fallback, never discovery-wide
        return str(team.get("id"))


def nbl_matches(year: int) -> list[dict]:
    data = _get_json(f"{NBL_CAL}?league=nbl&year={year}")
    rows = []
    for m in data.get("matches") or []:
        home = m.get("home") or {}
        away = m.get("away") or {}
        date, match_time = _nbl_kickoff_utc(m)
        rows.append({
            "source_id": str(m.get("id")),
            "date": date,
            "match_time": match_time,
            "status_raw": m.get("phase"),
            "home": home.get("name"), "away": away.get("name"),
            "home_id": _nbl_team_ref(home), "away_id": _nbl_team_ref(away),
            "home_score": m.get("home_score"), "away_score": m.get("away_score"),
        })
    return rows


# ----------------------------------------------------------------- CBA
def cba_current_season() -> dict:
    """Current CBA season from the official data-server.

    The response is the same AES/ECB/PKCS7 encrypted JSON the 8-E fetcher
    decrypts (key from the site's public front-end bundle).
    """
    raw = _cba_curl("/api/com-code-tables/getSeason")
    seasons = json.loads(decrypt_cba(raw))
    # getSeason returns a LIST of season entries
    current = next(x for x in seasons if str(x.get("isDefault")) == "1")
    return {"season": current["key"], "season_value": current["value"]}


def decrypt_cba(raw: bytes) -> bytes:
    """Decrypt one CBA data-server payload (JSON-quoted base64,
    AES-128/ECB/PKCS7, key from the public front-end bundle)."""
    import base64

    key = bytes.fromhex("75566179714c344f4e4b6a4662567a51")
    text = json.loads(raw.decode("utf-8"))
    cipher = base64.b64decode(text)
    # decrypt via openssl (available everywhere Git Bash is; the 8-E
    # fetcher's pure-python AES stays in tools/ for the full fetch)
    import subprocess
    r = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-ecb", "-K",
         key.hex(), "-nosalt"],
        input=cipher, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"CBA decrypt failed: {r.stderr[:120]}")
    return r.stdout


def _cba_curl(path: str, attempts: int = 3) -> bytes:
    import time

    last = None
    for i in range(attempts):
        r = subprocess.run(
            ["curl", "-s", "--max-time", "30", "-A",
             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0",
             "--tlsv1.2", "--tls-max", "1.2", "-H",
             f"Referer: {CBA_REFERER}",
             f"{CBA_DATA}{path}"],
            capture_output=True, timeout=60)
        body = r.stdout or b""
        # valid bodies are plain JSON objects/arrays OR the JSON-quoted
        # AES ciphertext string (starts with a double quote)
        if r.returncode == 0 and body.strip().startswith(
                (b"{", b"[", b'"')):
            return body
        last = RuntimeError(f"rc={r.returncode} body={body[:80]!r}")
        time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"CBA discovery fetch failed {path}: {last}")


def cba_recent_schedule_counts(months: list[tuple[int, int]]) -> dict:
    """Per-month official game counts (light discovery; no per-game fetch)."""
    counts = {}
    for year, month in months:
        raw = _cba_curl(f"/api/schedules/month?year={year}&month={month:02d}")
        data = json.loads(decrypt_cba(raw))
        counts[f"{year}-{month:02d}"] = sum(
            d.get("scheduleNum") or 0 for d in data)
    return counts


# ------------------------------------------------- NBA / EuroLeague / ACB
def nba_source_freshness() -> dict:
    """llimllib/nba_data latest commit (source freshness for the parquet)."""
    data = _get_json(NBA_COMMITS)
    commit = data[0]
    return {"latest_commit_at": commit["commit"]["committer"]["date"],
            "latest_commit_sha": commit["sha"][:12],
            "refresh": "documented manual refresh (download parquet -> "
                       "convert -> ingest)"}


def euroleague_reachable() -> dict:
    status, _ = _http_get(EL_BASE)
    if status != 200:
        raise RuntimeError(f"euroleague API base returned {status}")
    return {"api_base": EL_BASE, "reachable": True,
            "refresh": "tools/fetch_euroleague_bundle.py --seasons E2026"}


def acb_seasons() -> dict:
    """Official ACB season listing via the fetcher's verified probe."""
    import subprocess

    r = subprocess.run(
        ["./.venv/Scripts/python.exe", "tools/fetch_acb_bundle.py",
         "--list-seasons"],
        capture_output=True, timeout=120, cwd=_repo_root())
    if r.returncode != 0:
        raise RuntimeError(f"ACB season listing failed: "
                           f"{r.stderr[:200] if r.stderr else r.returncode}")
    return {"listing": r.stdout.decode("utf-8", errors="replace")[:2000]}


def _repo_root() -> str:
    from basketball_agent.config import get_settings
    return str(Path(get_settings().db_path).parent.parent)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cba_schedule_rows(season_value: str, months: list[tuple[int, int]]) -> list[dict]:
    """Per-game schedule rows for one CBA season (via the encrypted
    schedules/day endpoint).  Returns light rows compatible with the
    BSL/NBL upsert pipeline."""
    rows = []
    seen_ids: set[str] = set()
    for year, month in months:
        raw = _cba_curl(f"/api/schedules/month?year={year}&month={month:02d}")
        data = json.loads(decrypt_cba(raw))
        for day_entry in data:
            day = (day_entry.get("scheduleTime") or "")[:10]
            if not day:
                continue
            day_raw = _cba_curl(
                f"/api/schedules/day?day={day}&auto=0")
            day_data = json.loads(decrypt_cba(day_raw))
            for g in day_data:
                sid = str(g.get("scheduleId") or "")
                if not sid or sid in seen_ids:
                    continue
                seen_ids.add(sid)
                home = g.get("homeTeamId") or g.get("homeTeamName")
                away = g.get("visitingTeamId") or g.get("visitingTeamName")
                # CBA status semantics (cba_api._status_for is the
                # authority): 4 = FINAL, 1 = scheduled.  The upsert path
                # uses the NBL convention ("1" = played), so translate.
                played = str(g.get("status")) == "4"
                # The day API has no tip-off time for upcoming games
                # (always 00:00): bound the kickoff at end-of-day UTC —
                # never earlier than the real slot, so same-day pre-game
                # predictions stay possible.  Not a guess of the hour.
                match_time = str(g.get("matchTime") or "00:00")
                if not played and match_time == "00:00":
                    match_time = "23:59"
                rows.append({
                    "source_id": sid,
                    "date": day,
                    "match_time": match_time,
                    "status_raw": "1" if played else "0",
                    "home": g.get("homeTeamName") or str(home),
                    "away": g.get("visitingTeamName") or str(away),
                    "home_id": str(home), "away_id": str(away),
                    "home_score": g.get("homeTeamScore") if played else "",
                    "away_score": g.get("visitingTeamScore") if played else "",
                })
    return rows


# ------------------------------------------------------------------ ACB rows
ACB_BASE = "https://api2.acb.com"
ACB_API_KEY = "0dd94928-6f57-4c08-a3bd-b1b2f092976e"  # public site key
LIGA_ENDESA_COMPETITION_ID = 1


def _acb_get_json(path: str) -> dict:
    """One ACB API call with the public-site key (verified 8-B6 header set)."""
    return _get_json(ACB_BASE + path, headers={
        "Accept": "application/json, text/plain, */*",
        "X-APIKEY": ACB_API_KEY,
        "Origin": "https://www.acb.com",
        "Referer": "https://www.acb.com/",
    })


def acb_schedule_rows(
    competition_id: int = LIGA_ENDESA_COMPETITION_ID,
) -> tuple[list[dict], str | None, dict]:
    """Season calendar flattened to upsert rows (Phase 12-H).

    Two API calls: the season listing (latest edition picked by
    ``seasonStartYear``) and that edition's calendar.  Row shape matches
    the fixtures lifecycle: ``ACB{yyyy}-{source_id}`` ids are assigned by
    the caller (``apply_fixtures_rows(id_scheme="source")``) so a later
    season refresh reuses the SAME rows.
    """
    from datetime import datetime, timezone

    root = _acb_get_json("/api/seasondata/Calendar/season-calendar")
    seasons = (root.get("availableFilters") or {}).get("seasons") or []
    if not seasons:
        raise RuntimeError("ACB season-calendar payload has no seasons")
    latest = max(seasons, key=lambda s: int(s.get("seasonStartYear") or 0))
    start_year = int(latest["seasonStartYear"])
    end_year = int(latest.get("seasonEndYear") or start_year + 1)
    season_label = f"{start_year}-{str(end_year)[-2:]}"
    query = (f"?competitionId={int(competition_id)}"
             f"&editionId={int(latest['id'])}")
    calendar = _acb_get_json(
        "/api/seasondata/Calendar/season-calendar" + query)
    rows: list[dict] = []
    for round_obj in calendar.get("rounds") or []:
        for m in round_obj.get("matches") or []:
            mid = str(m.get("id") or "")
            start_raw = str(m.get("startDateTime") or "")
            if not mid or not start_raw:
                continue
            # matchStatus drives the lifecycle (scores are initialised
            # to 0 for future games — never infer "played" from them);
            # STARTED (live) and NOT_SCHEDULED (placeholder slot) drop.
            status = str(m.get("matchStatus") or "")
            if status == "FINALIZED":
                status_raw, scored = "1", True
            elif status == "NOT_STARTED":
                status_raw, scored = "0", False
            else:
                continue
            dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            dt = dt.astimezone(timezone.utc)
            hs, as_ = m.get("homeScore"), m.get("awayScore")
            rows.append({
                "source_id": mid,
                "date": dt.strftime("%Y-%m-%d"),
                "match_time": dt.strftime("%H:%M"),
                "status_raw": status_raw,
                "home_id": str(m.get("homeTeamId")),
                "away_id": str(m.get("awayTeamId")),
                "home_score": str(hs) if scored else "",
                "away_score": str(as_) if scored else "",
                "season": season_label,
                "round": round_obj.get("roundNumber"),
            })
    extra = {"edition_id": latest.get("id"),
             "competition_id": competition_id,
             "n_matches": len(rows)}
    return rows, season_label, extra


def acb_edition_team_map(
    competition_id: int = LIGA_ENDESA_COMPETITION_ID,
    *,
    cache_dir=None,
    max_header_fetches: int = 40,
) -> dict[str, dict]:
    """Edition-scoped team id -> {club, name, abbr} for the latest ACB
    edition (Phase 12-H).

    ACB replaced its small stable ids with edition-scoped ones in
    2026-27 (edition 91): the calendar's ``homeTeamId`` is now the
    edition id, while the stable ``clubId`` survives inside MatchHeader
    payloads (``teams.home.clubId``).  This fetches headers for already
    FINALIZED matches and builds the bridge, cached to
    ``acb_team_map_<edition>.json`` (24h TTL) so loop passes do not
    re-scrape.  Clubs with no played match yet are absent — they resolve
    on a later pass once they play (rows error loudly meanwhile).
    """
    import time
    from pathlib import Path as _P

    rows, season, extra = acb_schedule_rows(competition_id)
    edition = int(extra.get("edition_id") or 0)
    cache = _P(cache_dir) / f"acb_team_map_{edition}.json" if cache_dir \
        else None
    if cache is not None and cache.is_file():
        age = time.time() - cache.stat().st_mtime
        if age < 24 * 3600:
            import json as _json
            cached = _json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("edition") == edition:
                return cached["map"]
    mapping: dict[str, dict] = {}
    finals = [r for r in rows if r["status_raw"] == "1"]
    for r in finals[:max_header_fetches]:
        try:
            h = _acb_get_json(
                "/api/matchdata/MatchHeader/match-header?matchId="
                f"{r['source_id']}")
        except Exception:  # noqa: BLE001 — best-effort bridge
            continue
        for side in ("home", "away"):
            t = (h.get("teams") or {}).get(side) or {}
            tid = t.get("id")
            if tid is None:
                continue
            mapping[str(tid)] = {
                "club": t.get("clubId"),
                "name": t.get("fullName") or t.get("shortName"),
                "abbr": t.get("abbreviatedName"),
            }
        time.sleep(0.3)
    if cache is not None and mapping:
        cache.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        cache.write_text(_json.dumps(
            {"edition": edition, "map": mapping}, ensure_ascii=False,
            indent=1), encoding="utf-8")
    return mapping
