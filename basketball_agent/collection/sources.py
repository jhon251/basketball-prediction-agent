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
def nbl_matches(year: int) -> list[dict]:
    data = _get_json(f"{NBL_CAL}?league=nbl&year={year}")
    rows = []
    for m in data.get("matches") or []:
        home = m.get("home") or {}
        away = m.get("away") or {}
        rows.append({
            "source_id": str(m.get("id")),
            "date": ((m.get("start_time_datetime") or "")[:10]),
            "status_raw": m.get("phase"),
            "home": home.get("name"), "away": away.get("name"),
            "home_id": home.get("id"), "away_id": away.get("id"),
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
