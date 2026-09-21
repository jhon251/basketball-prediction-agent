r"""Phase 8-B5 fitting-efficiency benchmark + old/new equivalence harness.

Read-only against the real database.  Subcommands:

    baseline  per-pass walk-forward cost on one league's train window
              (the "before" measurement, tracemalloc on)
    legacy    timed OLD (``git show <ref>:.../param_tuning.py``, verbatim) vs
              NEW ``fit_v1_params`` on the same real window, asserting that the
              fitted parameters are identical
    windows   the three frozen EuroLeague windows with frozen defaults
              (must reproduce the Phase 8-B4 numbers exactly)
    fit       one EuroLeague ``train-seasons=1`` window end to end, timed

Examples (from the repository root):

    .\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py baseline
    .\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py legacy --window season
    .\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py windows
    .\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py fit

Nothing here is imported by the package or the test suite; it is evidence
tooling only and never writes to ``storage/``.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import importlib.util
import json
import os
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import basketball_agent.agents  # noqa: E402,F401  (import order: avoids an agents/eval cycle)
from basketball_agent.data.db.connection import Database  # noqa: E402
from basketball_agent.data.db.repository import Repository  # noqa: E402
from basketball_agent.data.schemas.base import GameStatus, League  # noqa: E402
from basketball_agent.evaluation.backtest import (  # noqa: E402
    BacktestConfig,
    BacktestEngine,
    SimulationPredictor,
    StrengthPredictor,
)
from basketball_agent.evaluation import param_tuning as new_mod  # noqa: E402
from basketball_agent.evaluation.experiments import run_window_backtest  # noqa: E402
from basketball_agent.features import FeaturePipeline  # noqa: E402

UTC = timezone.utc
DEFAULT_DB = Path(os.environ.get("BASKETBALL_DB_PATH", ROOT / "storage" / "basketball.db"))
TMP = ROOT / ".tmp"

#: (window_start, window_end) of the three frozen Phase 8-B4 EuroLeague windows
EL_WINDOWS = {
    "el-1season": (datetime(2024, 10, 1, tzinfo=UTC), datetime(2025, 7, 1, tzinfo=UTC)),
    "el-3season": (datetime(2022, 10, 1, tzinfo=UTC), datetime(2025, 7, 1, tzinfo=UTC)),
    "el-all": (datetime(2019, 10, 1, tzinfo=UTC), datetime(2025, 7, 1, tzinfo=UTC)),
}

#: the training window a EuroLeague ``train-seasons=1`` backtest actually fits
EL_FIT_WINDOWS = {
    "small": (datetime(2023, 11, 15, tzinfo=UTC), datetime(2023, 10, 1, tzinfo=UTC)),
    "season": (datetime(2024, 10, 1, tzinfo=UTC), datetime(2023, 10, 1, tzinfo=UTC)),
}


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def peak_working_set_mb() -> float:
    """Peak working set of this process in MB (Windows); -1.0 if unavailable."""
    try:
        pmc = _ProcessMemoryCounters()
        pmc.cb = ctypes.sizeof(_ProcessMemoryCounters)
        k32 = ctypes.windll.kernel32
        fn = getattr(k32, "K32GetProcessMemoryInfo", None) or \
            ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wt.DWORD]
        fn.restype = wt.BOOL
        if not fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return -1.0
        return round(pmc.PeakWorkingSetSize / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001 - evidence only, never fatal
        return -1.0


class FeatureCounter:
    """Counts every walk-forward feature derivation in the process."""

    def __init__(self) -> None:
        self.n = 0
        self._original = FeaturePipeline.compute
        counter = self

        def wrapper(pipeline_self, game_id, as_of):
            counter.n += 1
            return counter._original(pipeline_self, game_id, as_of)

        FeaturePipeline.compute = wrapper

    def stop(self) -> None:
        FeaturePipeline.compute = self._original


def _repo(db_path: Path) -> Repository:
    if not Path(db_path).exists():
        raise SystemExit(f"FAIL: no database at {db_path}")
    return Repository(Database(Path(db_path)))


def _el_train_games(repo: Repository, cutoff: datetime, train_start: datetime):
    games = [
        g for g in repo.list_games(status=GameStatus.FINAL, league=League.EUROLEAGUE)
        if train_start <= g.game_date < cutoff
    ]
    games.sort(key=lambda g: (g.game_date, g.game_id))
    return games


# ---------------------------------------------------------------------------
# baseline: per-pass cost of the walk-forward protocol
# ---------------------------------------------------------------------------


def cmd_baseline(args) -> int:
    repo = _repo(args.db)
    train = _el_train_games(
        repo, datetime(2024, 7, 1, tzinfo=UTC), datetime(2023, 7, 1, tzinfo=UTC)
    )
    ids = [g.game_id for g in train]
    print(f"train window: {len(ids)} EuroLeague games (E2023)", flush=True)

    counter = FeatureCounter()
    config = BacktestConfig(warmup_games=5)

    def timed(label, predictor):
        counter.n = 0
        tracemalloc.start()
        t0 = time.perf_counter()
        result = BacktestEngine(repo, predictor, config).run(game_ids=ids)
        dt = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        preds = sum(1 for r in result.records if r.prediction is not None)
        print(f"{label:<28} time={dt:7.2f}s  peak={peak / 1e6:6.1f}MB  "
              f"feature_calls={counter.n:<5} predictions={preds}", flush=True)
        return dt

    t_strength = timed("1x strength pass", StrengthPredictor())
    t_sim = timed("1x simulation pass (100 sims)",
                  SimulationPredictor(n_simulations=100, seed=7))
    counter.stop()

    passes = 21 + 5 + 3  # logistic grid + shrinkage grid + lambda sweep
    estimate = 26 * t_strength + 3 * t_sim
    print(f"\nOLD fit (extrapolated from the tracemalloc passes): "
          f"{estimate:.0f}s ({estimate / 60:.1f} min), {passes} feature passes, "
          f"{passes * len(ids)} per-game feature computations", flush=True)
    print("note: tracemalloc inflates each pass; use `legacy` for the measured "
          "old-vs-new numbers.", flush=True)
    return 0


# ---------------------------------------------------------------------------
# legacy: verbatim pre-B5 implementation vs the optimised one
# ---------------------------------------------------------------------------


def _load_old_module(git_ref: str):
    TMP.mkdir(exist_ok=True)
    proc = subprocess.run(
        ["git", "show", f"{git_ref}:basketball_agent/evaluation/param_tuning.py"],
        cwd=ROOT, capture_output=True, check=True,
    )
    source = proc.stdout.decode("utf-8")  # byte mode: the locale codec is not UTF-8 here
    path = TMP / f"old_param_tuning_{git_ref.replace('/', '_')}.py"
    path.write_text(source, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("old_param_tuning", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["old_param_tuning"] = module  # dataclasses resolve via sys.modules
    spec.loader.exec_module(module)
    # The ONLY deviation: league scoping (a Phase 8-B5 requirement, not part of
    # the pre-B5 numeric path).  Everything else is the code at ``git_ref``.
    module._train_games = lambda repo, cutoff, train_start=None: new_mod._train_games(
        repo, cutoff, train_start, League.EUROLEAGUE
    )
    return module


def _fields(fp) -> dict:
    return {
        "home_court_advantage": fp.home_court_advantage,
        "margin_logistic_scale": fp.margin_logistic_scale,
        "shrinkage_k": fp.shrinkage_k,
        "decay_lambda": fp.decay_lambda,
        "training_window": list(fp.training_window),
        "validation_logloss": fp.validation_logloss,
    }


def cmd_legacy(args) -> int:
    repo = _repo(args.db)
    old_mod = _load_old_module(args.git_ref)
    cutoff, train_start = EL_FIT_WINDOWS[args.window]
    train = new_mod._train_games(repo, cutoff, train_start, League.EUROLEAGUE)
    print(f"real EL train window {train_start.date()} .. {cutoff.date()}: "
          f"{len(train)} games", flush=True)

    counter = FeatureCounter()
    t0 = time.perf_counter()
    new_fp = new_mod.fit_v1_params(
        repo, cutoff, train_start=train_start, n_simulations=args.n_simulations,
        seed=args.seed, warmup_games=args.warmup, league=League.EUROLEAGUE,
    )
    t_new, new_features = time.perf_counter() - t0, counter.n

    counter.n = 0
    t0 = time.perf_counter()
    old_fp = old_mod.fit_v1_params(
        repo, cutoff, train_start=train_start, n_simulations=args.n_simulations,
        seed=args.seed, warmup_games=args.warmup,
    )
    t_old, old_features = time.perf_counter() - t0, counter.n
    counter.stop()

    same = _fields(new_fp) == _fields(old_fp)
    print(f"NEW fit: {t_new:.2f}s  feature_computations={new_features}", flush=True)
    print(f"OLD fit: {t_old:.2f}s  feature_computations={old_features}", flush=True)
    print(("=== EQUIVALENT === " if same else "=== MISMATCH === ") +
          f"speedup={t_old / t_new:.2f}x", flush=True)
    print(json.dumps({
        "window": args.window, "n_train_games": len(train), "same": same,
        "old_s": round(t_old, 2), "new_s": round(t_new, 2),
        "old_features": old_features, "new_features": new_features,
        "speedup": round(t_old / t_new, 2) if t_new else None,
        "new": _fields(new_fp), "old": _fields(old_fp),
    }, indent=2), flush=True)
    return 0 if same else 1


# ---------------------------------------------------------------------------
# windows / fit
# ---------------------------------------------------------------------------


def _summarise(result: dict) -> dict:
    return {
        "n_window_games": result["n_window_games"],
        "models": {
            k: {f: v.get(f) for f in
                ("n_win_prob", "accuracy", "log_loss", "brier_score", "ece",
                 "margin_mae", "total_mae")}
            for k, v in result["models"].items()
        },
    }


def cmd_windows(args) -> int:
    repo = _repo(args.db)
    out = {}
    for label, (start, end) in EL_WINDOWS.items():
        t0 = time.perf_counter()
        result = run_window_backtest(
            repo, start, end, train_seasons=None,
            n_simulations=args.n_simulations, seed=args.seed,
            warmup_games=args.warmup, league=League.EUROLEAGUE,
        )
        entry = _summarise(result)
        entry["elapsed_s"] = round(time.perf_counter() - t0, 2)
        out[label] = entry
        print(f"{label}: {json.dumps(entry)}", flush=True)
    print(json.dumps(out, indent=2), flush=True)
    return 0


def cmd_fit(args) -> int:
    repo = _repo(args.db)
    counter = FeatureCounter()
    t0 = time.perf_counter()
    result = run_window_backtest(
        repo, datetime(2024, 10, 1, tzinfo=UTC), datetime(2025, 7, 1, tzinfo=UTC),
        train_seasons=1, n_simulations=args.n_simulations, seed=args.seed,
        warmup_games=args.warmup, league=League.EUROLEAGUE,
    )
    elapsed = time.perf_counter() - t0
    features = counter.n
    counter.stop()

    fitted = result["fitted_params"]
    out = {
        "elapsed_s": round(elapsed, 2),
        "peak_working_set_mb": peak_working_set_mb(),
        "feature_computations": features,
        **_summarise(result),
        "fitted_params": _fields(fitted) if fitted else None,
    }
    print(json.dumps(out, indent=2), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("baseline", help="per-pass walk-forward cost (before)")

    legacy = sub.add_parser("legacy", help="verbatim old vs new fit on real data")
    legacy.add_argument("--window", choices=sorted(EL_FIT_WINDOWS), default="small")
    legacy.add_argument("--git-ref", default="HEAD")
    legacy.add_argument("--n-simulations", type=int, default=100)
    legacy.add_argument("--seed", type=int, default=7)
    legacy.add_argument("--warmup", type=int, default=5)

    for name, help_text in (("windows", "three frozen EL windows"),
                            ("fit", "one EL train-seasons=1 window")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--n-simulations", type=int, default=100)
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--warmup", type=int, default=5)

    args = parser.parse_args(argv)
    return {"baseline": cmd_baseline, "legacy": cmd_legacy,
            "windows": cmd_windows, "fit": cmd_fit}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
