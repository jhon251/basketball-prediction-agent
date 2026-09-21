"""Fetch EuroLeague seasons from the official live endpoint into raw bundles.

RUN THIS ON A MACHINE WITH INTERNET ACCESS — the project sandbox has no
network, so acquisition must happen user-side and enter the system through
the normal ``validate -> ingest`` path (see docs/DATA_SOURCES.md).

Source (verified in Phase 8-B0): ``https://live.euroleague.net/api/``
  ``Header?gameCode=<n>&seasonCode=<Eyyyy>``
  ``Boxscore?gameCode=<n>&seasonCode=<Eyyyy>``

The emitted bundle matches ``basketball_agent/data/collectors/euroleague_live.py``
exactly::

    {"dataset": "euroleague-live-v1",
     "season_code": "E2023",
     "source": "https://live.euroleague.net/api",
     "fetched_at": "...", "n_headers": N, "n_boxscores": M,
     "empty_game_codes": [ ... ],
     "headers": {"1": {...}, ...},      # Header API payload keyed by gameCode
     "boxscores": {"1": {...}, ...}}    # Boxscore API payload keyed by gameCode

Usage (user side)::

    python tools/fetch_euroleague_bundle.py --seasons E2019 E2020 E2021 E2022 E2023 \
        --out-dir C:\\lanqiu\\storage\\raw\\euroleague_live

Then, in the project venv::

    python -m basketball_agent.cli.main validate C:\\...\\euroleague_E2023_raw.json
    python -m basketball_agent.cli.main ingest   C:\\...\\euroleague_E2023_raw.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://live.euroleague.net/api"
DATASET_MARKER = "euroleague-live-v1"
DEFAULT_MAX_GAME_CODE = 400
DEFAULT_TRAILING_EMPTY_STOP = 40
USER_AGENT = "Mozilla/5.0 (compatible; basketball-agent/0.1; research)"


def fetch_json(url: str, *, timeout: float = 30.0, retries: int = 5, backoff: float = 2.0):
    """GET one endpoint with bounded retries.

    HTTP 429 (rate limit) is handled explicitly: the wait honours
    ``Retry-After`` when present and otherwise grows with the attempt number.
    Raises FetchError on failure.
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
            return json.loads(payload) if payload.strip() else None
        except urllib.error.HTTPError as exc:
            last_error = exc
            if attempt < retries:
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    wait = float(retry_after) if (retry_after or "").strip().isdigit() else backoff * 10 * attempt
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


class FetchError(RuntimeError):
    pass


def _is_header_payload(payload) -> bool:
    return isinstance(payload, dict) and bool(payload.get("CodeTeamA") or payload.get("TeamA"))


def build_bundle(
    season_code: str,
    headers: dict[str, dict],
    boxscores: dict[str, dict],
    *,
    empty_game_codes: list[int] | None = None,
    fetched_at: datetime | None = None,
) -> dict:
    """Assemble the bundle dict (pure; unit-testable without network)."""
    fetched_at = fetched_at or datetime.now(timezone.utc)
    return {
        "dataset": DATASET_MARKER,
        "season_code": str(season_code).strip(),
        "source": API_BASE,
        "fetched_at": fetched_at.isoformat(),
        "n_headers": len(headers),
        "n_boxscores": len(boxscores),
        "empty_game_codes": sorted(empty_game_codes or []),
        "headers": {str(k): v for k, v in sorted(headers.items(), key=lambda kv: int(kv[0]))},
        "boxscores": {str(k): v for k, v in sorted(boxscores.items(), key=lambda kv: int(kv[0]))},
    }


def fetch_season(
    season_code: str,
    *,
    max_game_code: int = DEFAULT_MAX_GAME_CODE,
    trailing_empty_stop: int = DEFAULT_TRAILING_EMPTY_STOP,
    existing: dict | None = None,
    request_pause: float = 0.5,
    verbose: bool = True,
    checkpoint=None,
    checkpoint_every: int = 10,
) -> dict:
    """Fetch one season's headers + boxscores (network required).

    ``checkpoint`` is called with the current bundle every
    ``checkpoint_every`` fetched games so a mid-season interruption can be
    resumed with ``--resume`` instead of losing all progress.
    """
    headers: dict[str, dict] = dict((existing or {}).get("headers") or {})
    boxscores: dict[str, dict] = dict((existing or {}).get("boxscores") or {})
    empty_codes: list[int] = list((existing or {}).get("empty_game_codes") or [])
    trailing_empty = 0
    fetched_since_checkpoint = 0

    def current() -> dict:
        return build_bundle(season_code, headers, boxscores, empty_game_codes=empty_codes)

    for code in range(1, max_game_code + 1):
        key = str(code)
        if key in headers:
            continue
        header = fetch_json(f"{API_BASE}/Header?{urllib.parse.urlencode({'gameCode': code, 'seasonCode': season_code})}")
        if not _is_header_payload(header):
            empty_codes.append(code)
            trailing_empty += 1
            if trailing_empty >= trailing_empty_stop:
                if verbose:
                    print(f"  stopping at gameCode={code}: "
                          f"{trailing_empty} consecutive empty codes", file=sys.stderr)
                break
            continue
        trailing_empty = 0
        headers[key] = header
        boxscore = fetch_json(
            f"{API_BASE}/Boxscore?{urllib.parse.urlencode({'gameCode': code, 'seasonCode': season_code})}"
        )
        if isinstance(boxscore, dict):
            boxscores[key] = boxscore
        fetched_since_checkpoint += 1
        if checkpoint is not None and fetched_since_checkpoint >= checkpoint_every:
            checkpoint(current())
            fetched_since_checkpoint = 0
        if verbose and code % 25 == 0:
            print(f"  {season_code}: {len(headers)} games fetched (gameCode={code})",
                  file=sys.stderr)
        if request_pause:
            time.sleep(request_pause)

    return current()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch EuroLeague raw bundles (user-side network)")
    parser.add_argument("--seasons", nargs="+", required=True,
                        help="season codes, e.g. E2019 E2020 E2021 E2022 E2023")
    parser.add_argument("--out-dir", required=True, help="output directory for raw bundles")
    parser.add_argument("--max-game-code", type=int, default=DEFAULT_MAX_GAME_CODE)
    parser.add_argument("--pause", type=float, default=0.5,
                        help="pause between games in seconds (be polite; the endpoint "
                             "rate-limits aggressive clients with HTTP 429)")
    parser.add_argument("--resume", action="store_true",
                        help="continue from an existing bundle file if present")
    parser.add_argument("--checkpoint-every", type=int, default=10,
                        help="write the partial bundle every N fetched games so --resume "
                             "can continue after an interruption")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def write_bundle(target: Path, bundle: dict) -> None:
        """Atomic write: temp file + replace.

        The replace is retried briefly: on Windows a concurrent reader
        (e.g. an ingest of the previous checkpoint) can hold the target file
        for a moment.  Run ONE fetcher per season to avoid collisions.
        """
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                tmp.replace(target)
                return
            except PermissionError as exc:  # file briefly locked by another process
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        raise FetchError(f"could not write {target}: {last_error}")

    exit_code = 0
    for season in args.seasons:
        target = out_dir / f"euroleague_{season}_raw.json"
        existing = None
        if args.resume and target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            print(f"{season}: resuming from {target.name} "
                  f"({existing.get('n_headers', 0)} headers)", file=sys.stderr)
        print(f"{season}: fetching …", file=sys.stderr)
        try:
            bundle = fetch_season(
                season, max_game_code=args.max_game_code,
                existing=existing, request_pause=args.pause,
                checkpoint=lambda b: write_bundle(target, b),
                checkpoint_every=args.checkpoint_every,
            )
        except FetchError as exc:
            print(f"{season}: FETCH FAILED — {exc}", file=sys.stderr)
            print(f"  partial progress was checkpointed to {target.name}; "
                  f"re-run with --resume to continue", file=sys.stderr)
            exit_code = 1
            continue
        write_bundle(target, bundle)
        print(f"{season}: wrote {target} "
              f"(headers={bundle['n_headers']}, boxscores={bundle['n_boxscores']}, "
              f"empty={len(bundle['empty_game_codes'])})", file=sys.stderr)

    print("\nNext steps (project venv):", file=sys.stderr)
    for season in args.seasons:
        target = out_dir / f"euroleague_{season}_raw.json"
        print(f"  python -m basketball_agent.cli.main validate \"{target}\"", file=sys.stderr)
        print(f"  python -m basketball_agent.cli.main ingest   \"{target}\"", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
