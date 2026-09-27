"""Fetch one CBA season from the official cbaleague.com data API into a raw bundle.

Phase 8-E source decision (documented):

* B0's preferred RealGM route is BLOCKED in this environment (evidence:
  basketball.realgm.com returns HTTP 403 to direct requests; the reader
  service only serves stale mid-2024-25 snapshots and blocks schedule/box
  paths; Basketball-Reference fails at connection level; the SciDB
  "2022-2023 CBA Official Data" dataset is referees' officiating data, not
  team box scores).  Per the B0 fallback chain the official site is next.
* The official SPA (https://www.cbaleague.com/data/) talks to
  ``https://data-server.cbaleague.com`` and AES-encrypts every JSON
  response.  The key ships in the site's own public JS bundle
  (``assets/index.e20827fb.js``); responses are JSON-quoted base64,
  AES-128/ECB/PKCS7, decrypted here with a small pure-Python AES
  implementation (FIPS-197 self-test at startup, no new dependencies).

Endpoints used (all verified against live responses):

* ``GET  /api/com-code-tables/getTeam?type=2&season={S}``  -> 20 team ids/names
* ``GET  /api/schedules/month?year={Y}&month={M}``         -> per-day game counts
* ``GET  /api/schedules/day?day={YYYY-MM-DD}&auto=0``      -> full game rows
* ``POST /api/team-match-datas/match-logs-basic/{teamId}`` -> per-game TEAM box
  scores, body ``{"season":S,"stage":stage,"ownType":1,"order":"points",
  "sort":1}`` with stage 1 = regular season, 2 = playoffs (official
  ``matchTypeId`` 1/2); totals carry fg/fga, 3p/3pa, ft/fta, offensive/
  defensive rebounds, assists, steals, blocks, turnovers, personal fouls.

The bundle keeps the full day-level schedule (including non-regular
matchTypeId rows) so the quality audit can account for every official game;
the adapter decides what to drop.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://data-server.cbaleague.com/api"
# Public front-end key of www.cbaleague.com/data/ (AES-128/ECB/PKCS7)
AES_KEY = b"uVayqL4ONKjFbVzQ"
DATASET_MARKER = "cba-api-v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REFERER = "https://www.cbaleague.com/data/"

# matchTypeId -> meaning (official /api/com-code-tables/getMatchType)
MATCH_TYPE_NAMES = {1: "regular", 2: "playoffs", 3: "preseason",
                    4: "all-star", 9: "relegation"}


# ---------------------------------------------------------------- AES-128-ECB
_SBOX = None
_INV_SBOX = None
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _tables() -> tuple[list[int], list[int]]:
    global _SBOX, _INV_SBOX
    if _SBOX is None:
        p = q = 1
        sbox = [0] * 256
        sbox[0] = 0x63
        while True:
            p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
            q ^= q << 1
            q ^= q << 2
            q ^= q << 4
            q &= 0xFF
            if q & 0x80:
                q ^= 0x09
            xformed = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) \
                ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
            sbox[p] = xformed & 0xFF ^ 0x63
            if p == 1:
                break
        _SBOX, _INV_SBOX = sbox, [0] * 256
        for i, v in enumerate(sbox):
            _INV_SBOX[v] = i
    return _SBOX, _INV_SBOX


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _expand_key(key: bytes) -> list[list[int]]:
    sbox, _ = _tables()
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [sbox[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ t[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(11)]


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    _, inv_sbox = _tables()
    state = list(block)
    state = [state[i] ^ round_keys[10][i] for i in range(16)]
    for rnd in range(9, 0, -1):
        # InvShiftRows
        for r in range(1, 4):
            state[r::4] = state[r::4][-r:] + state[r::4][:-r]
        state = [inv_sbox[b] for b in state]
        state = [state[i] ^ round_keys[rnd][i] for i in range(16)]
        # InvMixColumns on column c = bytes 4c..4c+3 (state is column-major)
        for c in range(4):
            s0, s1, s2, s3 = (state[4 * c + k] for k in range(4))
            state[4 * c + 0] = _mul(s0, 14) ^ _mul(s1, 11) ^ _mul(s2, 13) ^ _mul(s3, 9)
            state[4 * c + 1] = _mul(s0, 9) ^ _mul(s1, 14) ^ _mul(s2, 11) ^ _mul(s3, 13)
            state[4 * c + 2] = _mul(s0, 13) ^ _mul(s1, 9) ^ _mul(s2, 14) ^ _mul(s3, 11)
            state[4 * c + 3] = _mul(s0, 11) ^ _mul(s1, 13) ^ _mul(s2, 9) ^ _mul(s3, 14)
    for r in range(1, 4):
        state[r::4] = state[r::4][-r:] + state[r::4][:-r]
    state = [inv_sbox[b] for b in state]
    return bytes(state[i] ^ round_keys[0][i] for i in range(16))


def _self_test() -> None:
    # FIPS-197 Appendix C.1
    rk = _expand_key(bytes(range(16)))
    out = _decrypt_block(bytes.fromhex(
        "69c4e0d86a7b0430d8cdb78070b4c55a"), rk)
    if out != bytes.fromhex("00112233445566778899aabbccddeeff"):
        raise RuntimeError("AES self-test failed (FIPS-197 C.1)")
    # NIST SP800-38A F.1.2 (ECB-AES128.Decrypt, block 1)
    rk2 = _expand_key(bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c"))
    out2 = _decrypt_block(bytes.fromhex("3ad77bb40d7a3660a89ecaf32466ef97"), rk2)
    if out2 != bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"):
        raise RuntimeError("AES self-test failed (SP800-38A F.1.2)")


def decrypt_payload(raw: bytes) -> bytes:
    """Official response format: a JSON-quoted base64 string, AES-ECB/PKCS7."""
    text = json.loads(raw.decode("utf-8"))
    cipher = base64.b64decode(text)
    if len(cipher) == 0 or len(cipher) % 16 != 0:
        raise ValueError(f"ciphertext length {len(cipher)} not 16-aligned")
    rk = _expand_key(AES_KEY)
    plain = b"".join(
        _decrypt_block(cipher[i:i + 16], rk) for i in range(0, len(cipher), 16)
    )
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad PKCS7 padding (wrong key or corrupted payload)")
    return plain[:-pad]


def fetch_api(path: str, body: dict | None = None, attempts: int = 4,
              pause: float = 0.3):
    """GET (or POST with ``body``) one endpoint; returns decrypted JSON."""
    url = path if path.startswith("http") else f"{BASE}{path}"
    last = None
    for i in range(attempts):
        try:
            headers = {"User-Agent": UA, "Referer": REFERER}
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(decrypt_payload(resp.read()))
        except Exception as exc:
            last = exc
            time.sleep(pause * (2 ** i))
    print(f"FETCH FAILED {url}: {last}", file=sys.stderr)
    return None


def _slim_game(row: dict) -> dict:
    return {k: v for k, v in row.items() if "Logo" not in k}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True,
                    help="CBA season START year (e.g. 2025 -> 2025-26)")
    ap.add_argument("--out-dir", default=str(Path("C:/CBA_DATA")))
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    _self_test()
    print("AES self-test: OK (FIPS-197 C.1, SP800-38A F.1.2)")

    season = args.season
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / f"cba_cba{season % 100:02d}_raw.json"

    teams = fetch_api(f"/com-code-tables/getTeam?type=2&season={season}",
                      pause=args.pause)
    if not teams:
        print("team list fetch failed", file=sys.stderr)
        return 1
    team_ids = [(int(t["value"]), t["key"]) for t in teams]
    print(f"teams: {len(team_ids)} for season {season}")

    bundle: dict = {
        "dataset": DATASET_MARKER,
        "league": "CBA",
        "season": season,
        "source": f"{BASE} (www.cbaleague.com/data/ official API; "
                  "AES-128/ECB/PKCS7, key from the site's public JS bundle)",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "match_type_names": MATCH_TYPE_NAMES,
        "teams": teams,
        "months": [],
        "days": {},
        "team_logs": [],
    }
    if args.resume and bundle_path.exists():
        old = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["months"] = old.get("months", [])
        bundle["days"] = old.get("days", {})
        bundle["team_logs"] = old.get("team_logs", [])
        print(f"resume: {len(bundle['days'])} days, "
              f"{len(bundle['team_logs'])} team logs already present")

    # 1) month calendars (official per-day game counts -> completeness audit)
    have_months = {f"{m['year']}-{m['month']:02d}" for m in bundle["months"]}
    months: list[dict] = bundle["months"]
    for year, month in ([(season, m) for m in (10, 11, 12)] +
                        [(season + 1, m) for m in range(1, 7)]):
        key = f"{year}-{month:02d}"
        if key in have_months:
            continue
        cal = fetch_api(f"/schedules/month?year={year}&month={month}",
                        pause=args.pause)
        if cal is None:
            print(f"month calendar failed for {key}", file=sys.stderr)
            return 1
        months.append({"year": year, "month": month, "days": cal})
        print(f"calendar {key}: {sum(d.get('scheduleNum') or 0 for d in cal)} games")

    # 2) per-day schedule for every day that has games
    days = bundle["days"]
    wanted: list[str] = []
    for m in months:
        for d in m["days"]:
            n = d.get("scheduleNum") or 0
            if n > 0:
                wanted.append(d["scheduleTime"][:10])
    wanted = sorted(set(wanted))
    print(f"days with games: {len(wanted)}")
    for i, day in enumerate(wanted, 1):
        if day in days:
            continue
        rows = fetch_api(f"/schedules/day?day={day}&auto=0", pause=args.pause)
        if rows is None:
            print(f"day fetch failed: {day}", file=sys.stderr)
            return 1
        days[day] = [_slim_game(r) for r in rows]
        if i % 20 == 0:
            print(f"  days: {i}/{len(wanted)}")

    # 3) per-team match logs (stage 1 regular + stage 2 playoffs)
    have_logs = {(l["teamId"], l["stage"]) for l in bundle["team_logs"]}
    for tid, tname in team_ids:
        for stage, sname in ((1, "regular"), (2, "playoffs")):
            if (tid, stage) in have_logs:
                continue
            rows: list[dict] = []
            page = 1
            while True:
                res = fetch_api(
                    f"/team-match-datas/match-logs-basic/{tid}"
                    f"?pageNumber={page}&pageSize=60",
                    body={"season": season, "stage": stage, "ownType": 1,
                          "order": "points", "sort": 1},
                    pause=args.pause)
                if res is None:
                    print(f"match logs failed: {tid} stage {stage}",
                          file=sys.stderr)
                    return 1
                records = res.get("records") or []
                rows.extend(records)
                total = int(res.get("total") or 0)
                if len(rows) >= total or not records:
                    break
                page += 1
            bundle["team_logs"].append({
                "teamId": tid, "teamName": tname, "stage": stage,
                "total": total, "rows": rows,
            })
            print(f"logs {tid} {tname} {sname}: {len(rows)}/{total}")
            bundle["fetched_at"] = datetime.now(timezone.utc).isoformat()
            bundle_path.write_text(json.dumps(bundle, ensure_ascii=False),
                                   encoding="utf-8")

    bundle["fetched_at"] = datetime.now(timezone.utc).isoformat()
    bundle["n_days"] = len(bundle["days"])
    bundle["n_team_logs"] = len(bundle["team_logs"])
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False),
                           encoding="utf-8")
    n_games = sum(len(v) for v in bundle["days"].values())
    print(f"bundle: {bundle_path} days={len(bundle['days'])} "
          f"games={n_games} team_logs={len(bundle['team_logs'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
