"""Fetch one ACB (Liga Endesa) season from the official ACB API into a raw bundle.

Source (verified Phase 8-B6 against real responses):

* ``https://api2.acb.com/api/seasondata/Calendar/season-calendar``
  -> available competitions (Liga Endesa=1, Copa del Rey=2, Supercopa=3,
  Minicopa=10) and every edition (``id`` + ``seasonStartYear``); with
  ``?competitionId=&editionId=`` it returns all rounds and matches of one
  edition (``rounds[].matches[]`` with ``id``, ``homeTeamId``, ``awayTeamId``,
  ``homeScore``, ``awayScore``, ``startDateTime`` UTC, ``matchStatus``).
* ``https://api2.acb.com/api/matchdata/MatchHeader/match-header?matchId=``
  -> teams (home/away), published score, ``quarterScores`` (OT detection).
* ``https://api2.acb.com/api/matchdata/Result/boxscores?matchId=``
  -> per-team ``statsByPeriods``; period 0 holds the official game totals
  (``stats.total``) plus the team-only row (``stats.team``) and player rows.

The API requires the ``X-APIKEY`` header.  The default below is the public
client key that acb.com/live.acb.com ship inside their own browser bundles
(``/_next/static/chunks/2c05liq812vx-.js``); it is not a secret and is used
here only for read-only research access with a polite pause.  Override it with
``--api-key`` or ``ACB_API_KEY`` if the site rotates it.

The emitted bundle matches ``basketball_agent/data/collectors/acb_api.py``::

    {"dataset": "acb-api-v1", "competition_id": 1, "competition_name": "Liga Endesa",
     "edition_id": 90, "season": "2025-26", "season_code": "ACB2025",
     "source": "https://api2.acb.com", "fetched_at": "...",
     "n_calendar_matches": 327, "n_headers": N, "n_boxscores": M,
     "calendar_root": {...}, "calendar": {...},
     "headers": {"104459": {...}}, "boxscores": {"104459": {...}}}

Usage::

    python tools/fetch_acb_bundle.py --edition 90 --out-dir C:\\lanqiu\\.tmp\\acb_fetch
    python tools/fetch_acb_bundle.py --list-seasons
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api2.acb.com"
DATASET_MARKER = "acb-api-v1"
DEFAULT_API_KEY = "0dd94928-6f57-4c08-a3bd-b1b2f092976e"
USER_AGENT = "Mozilla/5.0 (compatible; basketball-agent/0.1; research)"
LIGA_ENDESA_COMPETITION_ID = 1


class FetchError(RuntimeError):
    pass


def fetch_json(
    path: str,
    *,
    api_key: str = DEFAULT_API_KEY,
    timeout: float = 60.0,
    retries: int = 5,
    backoff: float = 2.0,
):
    """GET one ACB API path with bounded retries (429-aware)."""
    url = path if path.startswith("http") else BASE + path
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "X-APIKEY": api_key,
            "Origin": "https://www.acb.com",
            "Referer": "https://www.acb.com/",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            text = raw.decode("utf-8", errors="replace")
            return json.loads(text) if text.strip() else None
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (401, 403):
                raise FetchError(
                    f"{url}: HTTP {exc.code} — the ACB API key was rejected; "
                    "pass --api-key (see the module docstring for provenance)"
                ) from exc
            if attempt < retries:
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    wait = (float(retry_after)
                            if (retry_after or "").strip().isdigit()
                            else backoff * 10 * attempt)
                    wait = min(max(wait, 10.0), 120.0)
                    print(f"  rate limited (429); sleeping {wait:.0f}s", file=sys.stderr)
                    time.sleep(wait)
                else:
                    time.sleep(backoff * attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise FetchError(f"failed to fetch {url}: {last_error}")


def season_code(edition: dict) -> str:
    """``{id: 90, seasonStartYear: 2025}`` -> ``ACB2025`` (source-native style)."""
    return f"ACB{int(edition['seasonStartYear'])}"


def season_label(edition: dict) -> str:
    """``2025-26`` (the project's unified season format)."""
    start = int(edition["seasonStartYear"])
    end = int(edition.get("seasonEndYear") or start + 1)
    return f"{start}-{str(end)[-2:]}"


def list_seasons(api_key: str = DEFAULT_API_KEY) -> tuple[dict, list[dict]]:
    """Return (calendar_root, competitions) with their editions."""
    root = fetch_json("/api/seasondata/Calendar/season-calendar", api_key=api_key)
    if not isinstance(root, dict) or "availableFilters" not in root:
        raise FetchError("unexpected season-calendar payload (no availableFilters)")
    return root, root["availableFilters"]["competitions"]


def resolve_edition(root: dict, competition_id: int, edition_id: int) -> dict:
    seasons = root["availableFilters"]["seasons"]
    for season in seasons:
        if int(season["id"]) == int(edition_id):
            return season
    raise FetchError(f"edition {edition_id} not present in season-calendar "
                     f"(available: {[s['id'] for s in seasons]})")


def fetch_season_calendar(competition_id: int, edition_id: int,
                          api_key: str = DEFAULT_API_KEY) -> dict:
    query = urllib.parse.urlencode({
        "competitionId": competition_id, "editionId": edition_id})
    calendar = fetch_json(f"/api/seasondata/Calendar/season-calendar?{query}",
                          api_key=api_key)
    if not isinstance(calendar, dict) or "rounds" not in calendar:
        raise FetchError("unexpected season-calendar payload (no rounds)")
    return calendar


def calendar_matches(calendar: dict) -> list[dict]:
    """Flatten rounds -> matches, keeping round/subphase context per match."""
    out: list[dict] = []
    for round_obj in calendar.get("rounds") or []:
        for match in round_obj.get("matches") or []:
            out.append({
                **match,
                "roundId": round_obj.get("id"),
                "roundNumber": round_obj.get("roundNumber"),
                "subphaseId": round_obj.get("subphaseId"),
                "subphaseNumber": round_obj.get("subphaseNumber"),
            })
    return out


def build_bundle(
    *,
    calendar_root: dict,
    calendar: dict,
    competition_id: int,
    competition_name: str,
    edition: dict,
    headers: dict[str, dict],
    boxscores: dict[str, dict],
    skipped: dict[str, str] | None = None,
    fetched_at: datetime | None = None,
) -> dict:
    """Assemble the bundle dict (pure; unit-testable without network)."""
    matches = calendar_matches(calendar)
    fetched_at = fetched_at or datetime.now(timezone.utc)
    return {
        "dataset": DATASET_MARKER,
        "competition_id": int(competition_id),
        "competition_name": competition_name,
        "edition_id": int(edition["id"]),
        "season": season_label(edition),
        "season_code": season_code(edition),
        "source": BASE,
        "fetched_at": fetched_at.isoformat(),
        "n_calendar_matches": len(matches),
        "n_headers": len(headers),
        "n_boxscores": len(boxscores),
        "calendar_skipped": skipped or {},
        "calendar_root": calendar_root,
        "calendar": calendar,
        "headers": {str(k): v for k, v in sorted(headers.items(), key=lambda kv: int(kv[0]))},
        "boxscores": {str(k): v for k, v in sorted(boxscores.items(), key=lambda kv: int(kv[0]))},
    }


def fetch_edition(
    *,
    competition_id: int = LIGA_ENDESA_COMPETITION_ID,
    edition_id: int,
    api_key: str = DEFAULT_API_KEY,
    existing: dict | None = None,
    request_pause: float = 0.4,
    verbose: bool = True,
    checkpoint=None,
    checkpoint_every: int = 10,
) -> dict:
    """Fetch one edition's calendar + headers + boxscores (network required)."""
    calendar_root, competitions = list_seasons(api_key)
    names = {int(c["id"]): c["name"] for c in competitions}
    if competition_id not in names:
        raise FetchError(f"competition {competition_id} not offered by the API "
                         f"(available: {names})")
    edition = resolve_edition(calendar_root, competition_id, edition_id)
    calendar = fetch_season_calendar(competition_id, edition_id, api_key)
    matches = calendar_matches(calendar)

    headers: dict[str, dict] = dict((existing or {}).get("headers") or {})
    boxscores: dict[str, dict] = dict((existing or {}).get("boxscores") or {})
    skipped: dict[str, str] = dict((existing or {}).get("calendar_skipped") or {})

    def current() -> dict:
        return build_bundle(
            calendar_root=calendar_root, calendar=calendar,
            competition_id=competition_id, competition_name=names[competition_id],
            edition=edition, headers=headers, boxscores=boxscores, skipped=skipped,
        )

    fetched_since_checkpoint = 0
    for index, match in enumerate(matches, start=1):
        key = str(match["id"])
        status = str(match.get("matchStatus") or "").upper()
        need_box = status == "FINALIZED"
        if key in headers and (not need_box or key in boxscores):
            continue

        header = fetch_json(
            f"/api/matchdata/MatchHeader/match-header?matchId={match['id']}",
            api_key=api_key)
        if isinstance(header, dict):
            headers[key] = header
        if request_pause:
            time.sleep(request_pause)

        if need_box:
            box = fetch_json(
                f"/api/matchdata/Result/boxscores?matchId={match['id']}",
                api_key=api_key)
            if isinstance(box, dict):
                boxscores[key] = box
            else:
                skipped[key] = "boxscore endpoint returned no payload"
            if request_pause:
                time.sleep(request_pause)

        fetched_since_checkpoint += 1
        if checkpoint is not None and fetched_since_checkpoint >= checkpoint_every:
            checkpoint(current())
            fetched_since_checkpoint = 0
        if verbose and index % 25 == 0:
            print(f"  edition {edition_id}: {index}/{len(matches)} matches "
                  f"(headers={len(headers)}, boxscores={len(boxscores)})",
                  file=sys.stderr)

    return current()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch ACB (Liga Endesa) raw bundles (read-only API)")
    parser.add_argument("--edition", type=int, default=90,
                        help="ACB edition id (90 = 2025-26, 89 = 2024-25, …)")
    parser.add_argument("--competition", type=int, default=LIGA_ENDESA_COMPETITION_ID,
                        help="competition id: 1 = Liga Endesa (default)")
    parser.add_argument("--out-dir", default=str(Path(".tmp") / "acb_fetch"),
                        help="output directory for the raw bundle")
    parser.add_argument("--api-key", default=os.environ.get("ACB_API_KEY", DEFAULT_API_KEY))
    parser.add_argument("--pause", type=float, default=0.4,
                        help="pause between API calls in seconds (be polite)")
    parser.add_argument("--resume", action="store_true",
                        help="continue from an existing bundle file if present")
    parser.add_argument("--checkpoint-every", type=int, default=10,
                        help="write the partial bundle every N matches")
    parser.add_argument("--list-seasons", action="store_true",
                        help="print the available competitions/editions and exit")
    args = parser.parse_args(argv)

    if args.list_seasons:
        _, competitions = list_seasons(args.api_key)
        for competition in competitions:
            print(f"competition {competition['id']}: {competition['name']}")
        root = fetch_json("/api/seasondata/Calendar/season-calendar", api_key=args.api_key)
        for season in root["availableFilters"]["seasons"][-8:]:
            print(f"  edition {season['id']}: {season_label(season)}")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def write_bundle(target: Path, bundle: dict) -> None:
        """Atomic write (tmp + replace) with a short retry for Windows locks."""
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                tmp.replace(target)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        raise FetchError(f"could not write {target}: {last_error}")

    root, _ = list_seasons(args.api_key)
    edition = resolve_edition(root, args.competition, args.edition)
    name = f"acb_competition{args.competition}_edition{args.edition}_raw.json"
    target = out_dir / name
    existing = None
    if args.resume and target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        print(f"resuming from {target.name} (headers={existing.get('n_headers', 0)}, "
              f"boxscores={existing.get('n_boxscores', 0)})", file=sys.stderr)

    print(f"fetching {season_label(edition)} (edition {args.edition}) …", file=sys.stderr)
    try:
        bundle = fetch_edition(
            competition_id=args.competition, edition_id=args.edition,
            api_key=args.api_key, existing=existing, request_pause=args.pause,
            checkpoint=lambda b: write_bundle(target, b),
            checkpoint_every=args.checkpoint_every,
        )
    except FetchError as exc:
        print(f"FETCH FAILED — {exc}", file=sys.stderr)
        print(f"partial progress was checkpointed to {target.name}; re-run with "
              f"--resume to continue", file=sys.stderr)
        return 1
    write_bundle(target, bundle)
    print(f"wrote {target} (matches={bundle['n_calendar_matches']}, "
          f"headers={bundle['n_headers']}, boxscores={bundle['n_boxscores']})",
          file=sys.stderr)
    print("\nNext steps (project venv):", file=sys.stderr)
    print(f"  python -m basketball_agent.cli.main validate \"{target}\"", file=sys.stderr)
    print(f"  python -m basketball_agent.cli.main ingest   \"{target}\" "
          f"--source acb_api --snapshot-tag ingest:acb_{edition['id']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
