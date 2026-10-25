"""Confidence calibration: empirical hit-rate by predicted-probability
bucket (Phase 12-F).

The risk agent's ``confidence_score`` is a rule-penalty checklist, NOT a
calibrated probability of being right (see docs/PHASE12_A_SPREAD_API.md).
This module supplies the calibrated complement: on a frozen walk-forward
window, bucket every game by the PICKED side's predicted probability
(``p_picked = max(p_home, 1 - p_home)``) and record the empirical rate at
which that side actually won.  The API/UI surface it next to the
checklist score as the honest "把握" number.

Payload JSON shape (``storage/calibration/confidence_calibration.json``)::

    {
      "generated_at": "...", "n_simulations": 300, "seed": 7,
      "windows": {"NBA": ["2022-10-01", "2025-10-01"], ...},
      "leagues": {
        "NBA": {
          "simulation": [
            {"bucket": "0.50-0.55", "lo": 0.5, "hi": 0.55,
             "n": 431, "hits": 244, "rate": 0.566},
            ...   # 0.50-0.55 .. 0.95-1.00, only non-empty buckets kept
          ], ...
        }
      }
    }
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FAMILIES = ("elo", "strength", "simulation")

# 5-point buckets on the picked side's probability.
BUCKET_EDGES: tuple[float, ...] = (0.50, 0.55, 0.60, 0.65, 0.70,
                                   0.75, 0.80, 0.85, 0.90, 0.95, 1.0)


def bucket_label(lo: float, hi: float) -> str:
    return f"{lo:.2f}-{hi:.2f}"


def bucket_of(p_picked: float) -> tuple[float, float] | None:
    """The bucket containing p_picked, or None when outside [0.5, 1.0]."""
    if not 0.5 <= p_picked <= 1.0:
        return None
    for lo, hi in zip(BUCKET_EDGES, BUCKET_EDGES[1:]):
        if lo <= p_picked < hi or (hi == 1.0 and p_picked == 1.0):
            return (lo, hi)
    return None


def build_family_table(entries: list[dict], family: str) -> list[dict]:
    """Bucket hit rates for one family from walk-forward entries."""
    stats: dict[tuple[float, float], list[int]] = {}
    for e in entries:
        p_home = e.get(f"p_{family}")
        if p_home is None:
            continue
        p_picked = max(p_home, 1.0 - p_home)
        picked_home = p_home >= 0.5
        won = e["outcome"] if picked_home else not e["outcome"]
        b = bucket_of(p_picked)
        if b is None:
            continue
        n, hits = stats.get(b, [0, 0])
        stats[b] = [n + 1, hits + int(won)]
    out = []
    for (lo, hi) in sorted(stats):
        n, hits = stats[(lo, hi)]
        if n == 0:
            continue
        out.append({"bucket": bucket_label(lo, hi), "lo": lo, "hi": hi,
                    "n": n, "hits": hits, "rate": hits / n})
    return out


def build_payload(
    league_tables: dict[str, dict[str, list[dict]]],
    *, windows: dict[str, list[str]],
    n_simulations: int, seed: int,
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_simulations": n_simulations, "seed": seed,
        "windows": windows, "leagues": league_tables,
    }


def save_payload(payload: dict, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)
    return path


def load_payload(path: Path | str) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def lookup(
    payload: dict | None,
    league: str,
    family: str,
    p_home: float,
) -> dict | None:
    """The empirical hit-rate bucket for one live prediction."""
    if not payload:
        return None
    tables = payload.get("leagues", {}).get(league)
    if not tables:
        return None
    rows = tables.get(family)
    if not rows:
        return None
    p_picked = max(float(p_home), 1.0 - float(p_home))
    b = bucket_of(p_picked)
    if b is None:
        return None
    label = bucket_label(*b)
    for row in rows:
        if row["bucket"] == label:
            return {"bucket": label, "picked_side_is_home": p_home >= 0.5,
                    "n": row["n"], "empirical_hit_rate": row["rate"]}
    return None


def default_calibration_path(db_path: Path | str) -> Path:
    return Path(db_path).parent / "calibration" / "confidence_calibration.json"
