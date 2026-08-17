"""``basketball predict <game_id>`` command-line interface (spec §34)."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from basketball_agent import __version__
from basketball_agent.agents.prediction_agent import GamePrediction, PredictionAgent, PredictionError
from basketball_agent.config.settings import get_settings
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.snapshots.manager import SnapshotManager
from basketball_agent.evaluation.settlement import SettlementStore
from basketball_agent.simulation.monte_carlo import DistributionStats

BANNER = "================================================"


def _fmt_stats(label: str, stats: DistributionStats) -> str:
    return (
        f"{label}\n"
        f"Mean: {stats.mean:.1f}\n"
        f"Median: {stats.median:.1f}\n"
        f"P5: {stats.p5:.0f}\n"
        f"P95: {stats.p95:.0f}"
    )


def format_report(prediction: GamePrediction, team_names: dict[str, str]) -> str:
    """Render the §34 report for one prediction."""
    home = prediction.features.home_team_id
    away = prediction.features.away_team_id
    home_name = team_names.get(home, home)
    away_name = team_names.get(away, away)
    mc = prediction.monte_carlo
    lines = [
        BANNER,
        "Basketball Simulation & Prediction Agent V1",
        BANNER,
        "",
        f"Game: {home_name} vs {away_name}",
        "",
        "Win Probability:",
        f"HOME: {prediction.win_probability_home * 100:.1f}%",
        f"AWAY: {prediction.win_probability_away * 100:.1f}%",
        "",
        "Expected Score:",
        f"HOME: {prediction.expected_home_score:.1f}",
        f"AWAY: {prediction.expected_away_score:.1f}",
        "",
        f"Expected Margin: {prediction.expected_margin:+.1f}",
        "",
        f"Expected Total: {prediction.expected_total:.1f}",
        "",
        f"Simulation: {mc.n_simulations:,} games",
        "",
        f"HOME wins: {mc.home_win_probability * 100:.1f}%",
        f"AWAY wins: {mc.away_win_probability * 100:.1f}%",
        "",
        "Score Distribution:",
        _fmt_stats("Home Score", mc.home_score),
        _fmt_stats("Away Score", mc.away_score),
        "",
        "Margin Distribution:",
        _fmt_stats("Margin", mc.margin),
        "",
        "Total Distribution:",
        _fmt_stats("Total", mc.total),
        "",
        f"Confidence: {prediction.risk.confidence_score:.0f}/100",
        "",
        "Risk Flags:",
    ]
    if prediction.risk.risk_flags:
        for flag in prediction.risk.risk_flags:
            lines.append(f"- [{flag.severity}] {flag.code}: {flag.message}")
    else:
        lines.append("- none")
    lines += [
        "",
        f"Model Version: {prediction.model_version}",
        f"Data Snapshot: {prediction.snapshot_id or 'n/a'}",
        "",
        BANNER,
    ]
    return "\n".join(lines)


def _latest_snapshot_id() -> str | None:
    try:
        settings = get_settings()
        snapshots = SnapshotManager(settings.snapshots_dir).list_snapshots()
        return snapshots[0].snapshot_id if snapshots else None
    except Exception:
        return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="basketball",
                                     description="Basketball Simulation & Prediction Agent V1")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("predict", help="generate a pre-game prediction for one game")
    p.add_argument("game_id", help="game id in the data snapshot")
    p.add_argument("--as-of", default=None,
                   help="prediction timestamp (ISO-8601, UTC); defaults to now")
    p.add_argument("--n-simulations", type=int, default=None,
                   help="Monte Carlo runs (default from settings: 10000)")
    p.add_argument("--seed", type=int, default=None,
                   help="random seed (deterministic mode; default from settings: 42)")
    p.add_argument("--save", action="store_true",
                   help="persist the prediction for later settlement")

    v = sub.add_parser("version", help="print the model version")
    v.add_argument("--quiet", action="store_true")

    ing = sub.add_parser("ingest", help="ingest real NBA data files (CSV/Parquet; raw preserved, snapshot created)")
    ing.add_argument("paths", nargs="+", help="one or more CSV/Parquet files")
    ing.add_argument("--source", default="nba_games", help="data source name for the raw manifest")
    ing.add_argument("--season", default=None, help="season override (e.g. 2024-25)")
    ing.add_argument("--snapshot-tag", default=None, help="tag for the created snapshot")
    ing.add_argument("--stats-notes", default=None,
                     help="stat-definition notes recorded in the processed manifest (spec §6)")

    val = sub.add_parser("validate", help="parse + validate data files WITHOUT writing to the DB")
    val.add_argument("paths", nargs="+", help="one or more CSV/Parquet files")
    val.add_argument("--season", default=None, help="season override (e.g. 2024-25)")

    conv = sub.add_parser("convert", help="convert a local Parquet to the canonical CSV (offline)")
    conv.add_argument("parquet_path", help="path to the Parquet file")
    conv.add_argument("--out", required=True, help="output CSV path")
    conv.add_argument("--season", default=None, help="season override")

    cov = sub.add_parser("coverage", help="print the NBA data coverage report")

    status = sub.add_parser("data-status", help="print SOURCE_STATUS and the snapshot chain")

    exp = sub.add_parser("experiments", help="run EXP-NBA-001..004 (strict train/test split)")
    exp.add_argument("--train-cutoff", required=True,
                     help="train window end (ISO, UTC) — parameters fit only before this")
    exp.add_argument("--test-start", required=True, help="test window start (ISO, UTC)")
    exp.add_argument("--test-end", default=None, help="test window end (ISO, UTC)")
    exp.add_argument("--n-simulations", type=int, default=300)
    exp.add_argument("--seed", type=int, default=7)
    exp.add_argument("--warmup", type=int, default=5)

    rep = sub.add_parser("report", help="render the Phase 7 Final Report (spec §21)")
    rep.add_argument("--train-cutoff", required=True)
    rep.add_argument("--test-start", required=True)
    rep.add_argument("--test-end", default=None)
    rep.add_argument("--n-simulations", type=int, default=300)
    rep.add_argument("--seed", type=int, default=7)
    rep.add_argument("--warmup", type=int, default=5)

    return parser


def _open_db():
    settings = get_settings()
    db = Database(settings.db_path)
    db.initialize()
    return db, settings


def _parse_as_of(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "version":
        if not args.quiet:
            print(f"Basketball Simulation & Prediction Agent V1 — model version {__version__}")
        return 0

    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "convert":
        return _cmd_convert(args)
    if args.command == "coverage":
        return _cmd_coverage(args)
    if args.command == "data-status":
        return _cmd_data_status(args)
    if args.command == "experiments":
        return _cmd_experiments(args)
    if args.command == "report":
        return _cmd_report(args)

    if args.command != "predict":
        return 2

    try:
        db, settings = _open_db()
        repo = Repository(db)
        as_of = _parse_as_of(args.as_of)
        agent = PredictionAgent(
            repo,
            n_simulations=args.n_simulations or settings.default_n_simulations,
            seed=args.seed if args.seed is not None else settings.default_seed,
            decay_lambda=settings.decay_lambda,
            snapshot_id=_latest_snapshot_id(),
        )
        prediction = agent.predict(args.game_id, as_of=as_of)
    except PredictionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    team_names = {}
    for team_id in (prediction.features.home_team_id, prediction.features.away_team_id):
        team = repo.get_team(team_id)
        if team is not None:
            team_names[team_id] = team.name

    print(format_report(prediction, team_names))

    if args.save:
        store = SettlementStore(db)
        prediction_id = store.save(prediction)
        print(f"\nPrediction saved (id={prediction_id}); it will be settled once the game is FINAL.")

    return 0


def _cmd_ingest(args) -> int:
    from basketball_agent.data.collectors.manifest import ProcessedDataStore, RawDataStore
    from basketball_agent.data.collectors.nba_csv import IngestDataError
    from basketball_agent.data.collectors.nba_parquet import (
        load_nba_games_file,
        merge_raw_datasets,
    )
    from basketball_agent.data.db.loader import DataIngestor
    from basketball_agent.data.snapshots.manager import SnapshotManager
    from basketball_agent.evaluation.coverage import build_coverage_report, format_coverage_report

    try:
        db, settings = _open_db()
        store = RawDataStore(settings.raw_dir)
        raws = []
        for path in args.paths:
            store.register(args.source, path)  # verbatim copy + SHA-256 manifest per file
            raws.append(load_nba_games_file(path, season=args.season))
        merged = merge_raw_datasets(raws)
        DataIngestor(db).ingest(merged)
        processed = ProcessedDataStore(settings.processed_dir).save(
            args.source, merged,
            description=args.stats_notes or "normalized, validated dataset",
        )
        snapshot = SnapshotManager(settings.snapshots_dir).create(
            db.path, tag=args.snapshot_tag or f"ingest:{args.source}"
        )
    except (IngestDataError, FileNotFoundError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    coverage = build_coverage_report(Repository(db))
    print(f"Ingested {coverage['games']} games from {len(args.paths)} file(s) "
          f"of source '{args.source}'.")
    print(f"Processed dataset: {processed.file_name} (sha256 {processed.file_hash[:12]}...)")
    print(f"Data Snapshot: {snapshot.snapshot_id} (tag: {snapshot.tag})")
    print()
    print(format_coverage_report(coverage))
    return 0


def _cmd_validate(args) -> int:
    from basketball_agent.agents.validation_agent import DataValidationAgent
    from basketball_agent.data.collectors.nba_csv import IngestDataError
    from basketball_agent.data.collectors.nba_parquet import (
        load_nba_games_file,
        merge_raw_datasets,
    )
    from basketball_agent.data.validators.parsing import parse_dataset

    try:
        raws = [load_nba_games_file(p, season=args.season) for p in args.paths]
        merged = merge_raw_datasets(raws)
        dataset, parse_errors = parse_dataset(merged)
        report = DataValidationAgent().validate(dataset)
    except (IngestDataError, FileNotFoundError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if parse_errors:
        print(f"FAIL: {len(parse_errors)} parse error(s):")
        for error in parse_errors[:10]:
            print(f"  - [{error.record_type} #{error.index}] {error.message.splitlines()[0]}")
        return 1

    print(f"files: {len(args.paths)} | games: {len(dataset.games)} | "
          f"team_stats: {len(dataset.team_stats)}")
    print(report.to_text())
    if report.has_errors:
        print(f"RESULT: FAIL ({len(report.errors)} errors, {len(report.warnings)} warnings)")
        return 1
    print(f"RESULT: PASS (0 errors, {len(report.warnings)} warnings)")
    return 0


def _cmd_convert(args) -> int:
    from basketball_agent.data.collectors.nba_csv import IngestDataError, write_canonical_csv
    from basketball_agent.data.collectors.nba_parquet import load_nba_games_parquet

    try:
        raw = load_nba_games_parquet(args.parquet_path, season=args.season)
        write_canonical_csv(raw, args.out)
    except (IngestDataError, FileNotFoundError, ImportError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"Converted {raw['_meta']['n_games']} games -> {args.out}")
    print("Next: `basketball validate <csv>` then `basketball ingest <csv>`.")
    return 0


def _cmd_data_status(args) -> int:
    from basketball_agent.data.collectors.manifest import RawDataStore
    from basketball_agent.data.snapshots.manager import SnapshotManager
    from basketball_agent.evaluation.coverage import build_coverage_report

    try:
        db, settings = _open_db()
        manifests = RawDataStore(settings.raw_dir).list_sources()
        snapshots = SnapshotManager(settings.snapshots_dir).list_snapshots()
        coverage = build_coverage_report(Repository(db))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("SOURCE_STATUS:")
    if manifests:
        print(f"AVAILABLE ({len(manifests)} registered source file(s))")
        for m in manifests:
            print(f"- {m.source} / {m.file_name} — {m.status}, "
                  f"retrieved {m.retrieved_at[:19]}, sha256 {m.file_hash[:12]}...")
    else:
        print("UNAVAILABLE — no raw data sources registered.")
        print("  Provide a real NBA games CSV and run: "
              "`basketball ingest <path.csv>` (schema: see DATA_REQUIREMENTS.md).")
    print(f"Snapshots: {len(snapshots)}")
    if snapshots:
        print(f"Latest snapshot: {snapshots[0].snapshot_id} "
              f"(games {snapshots[0].game_count}, created {snapshots[0].created_at[:19]})")
    print(f"Database: {coverage['games']} games, {coverage['teams']} teams, "
          f"range {coverage['date_range']}")
    return 0


def _cmd_coverage(args) -> int:
    from basketball_agent.evaluation.coverage import build_coverage_report, format_coverage_report

    try:
        db, _ = _open_db()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(format_coverage_report(build_coverage_report(Repository(db))))
    return 0


def _cmd_experiments(args) -> int:
    from basketball_agent.evaluation.experiments import run_phase7_experiments
    from basketball_agent.evaluation.param_tuning import format_param_report

    try:
        db, settings = _open_db()
        train_cutoff = _parse_as_of(args.train_cutoff)
        test_start = _parse_as_of(args.test_start)
        test_end = _parse_as_of(args.test_end) if args.test_end else None
        if train_cutoff is None or test_start is None:
            print("FAIL: --train-cutoff and --test-start must be valid ISO timestamps",
                  file=sys.stderr)
            return 1
        results = run_phase7_experiments(
            Repository(db), train_cutoff, test_start, test_end,
            n_simulations=args.n_simulations, seed=args.seed, warmup_games=args.warmup,
        )
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_param_report(results["fitted_params"]))
    for exp_id in ("EXP-NBA-001", "EXP-NBA-002", "EXP-NBA-003", "EXP-NBA-004"):
        exp = results[exp_id]
        print()
        print(f"{exp_id}: {exp['description']}")
        if exp_id == "EXP-NBA-002":
            print(f"  chosen lambda: {exp['chosen_lambda']} "
                  f"(validation logloss {exp['chosen_validation_logloss']:.4f})")
        elif exp_id == "EXP-NBA-003":
            print(f"  weights: {exp['weights']}")
            print(f"  test logloss: {exp['test_logloss']}")
        elif exp_id == "EXP-NBA-004":
            print(f"  comparison: {exp['simulation_vs_baselines']}")
        else:
            for name, metrics in exp["models"].items():
                print(f"  {name:10s} acc={metrics['accuracy']:.3f} "
                      f"logloss={metrics['log_loss']:.4f} brier={metrics['brier_score']:.4f}")
    return 0


def _cmd_report(args) -> int:
    from basketball_agent.data.collectors.manifest import RawDataStore
    from basketball_agent.data.schemas.base import GameStatus
    from basketball_agent.evaluation.backtest import (
        BacktestConfig, BacktestEngine, SimulationPredictor,
    )
    from basketball_agent.evaluation.calibration import build_reliability_table
    from basketball_agent.evaluation.coverage import build_coverage_report
    from basketball_agent.evaluation.error_analysis import error_analysis
    from basketball_agent.evaluation.experiments import run_phase7_experiments
    from basketball_agent.evaluation.metrics import mae
    from basketball_agent.evaluation.phase7_report import build_phase7_report

    try:
        db, settings = _open_db()
        repo = Repository(db)
        train_cutoff = _parse_as_of(args.train_cutoff)
        test_start = _parse_as_of(args.test_start)
        test_end = _parse_as_of(args.test_end) if args.test_end else None
        if train_cutoff is None or test_start is None:
            print("FAIL: invalid timestamps", file=sys.stderr)
            return 1

        results = run_phase7_experiments(
            repo, train_cutoff, test_start, test_end,
            n_simulations=args.n_simulations, seed=args.seed, warmup_games=args.warmup,
        )

        test_ids = [
            g.game_id for g in repo.list_games(status=GameStatus.FINAL)
            if g.game_date >= test_start and (test_end is None or g.game_date < test_end)
        ]
        sim_result = BacktestEngine(
            repo,
            SimulationPredictor(n_simulations=args.n_simulations, seed=args.seed),
            BacktestConfig(n_simulations=args.n_simulations, seed=args.seed,
                           warmup_games=args.warmup),
        ).run(game_ids=test_ids)

        recs = [r for r in sim_result.records
                if r.prediction is not None and r.actual_home_win is not None]
        probs = [r.prediction.prob_home for r in recs]
        outcomes = [r.actual_home_win for r in recs]
        table = build_reliability_table(probs, outcomes) if recs else []

        score_recs = [r for r in recs
                      if r.prediction.home_score_mean is not None
                      and r.prediction.away_score_mean is not None
                      and r.prediction.expected_margin is not None]
        score_metrics = {"home_mae": None, "away_mae": None, "total_mae": None, "margin_mae": None}
        if score_recs:
            score_metrics = {
                "home_mae": mae([float(r.actual_home_score) for r in score_recs],
                                [r.prediction.home_score_mean for r in score_recs]),
                "away_mae": mae([float(r.actual_away_score) for r in score_recs],
                                [r.prediction.away_score_mean for r in score_recs]),
                "total_mae": mae([float(r.actual_total) for r in score_recs],
                                 [r.prediction.home_score_mean + r.prediction.away_score_mean
                                  for r in score_recs]),
                "margin_mae": mae([float(r.actual_margin) for r in score_recs],
                                  [float(r.prediction.expected_margin) for r in score_recs]),
            }

        # temporal integrity summary over the test predictions
        passed = failed = 0
        for r in recs:
            if r.prediction.provenance is None:
                continue
            for gid in r.prediction.provenance.games_used:
                game = repo.get_game(gid)
                if game is not None and game.game_date < r.as_of:
                    passed += 1
                else:
                    failed += 1
        leakage = {"total": passed + failed, "passed": passed, "failed": failed}

        manifests = RawDataStore(settings.raw_dir).list_sources()
        coverage = build_coverage_report(repo)
        report_text = build_phase7_report(
            source_status={"source": (manifests[0].source if manifests else "n/a"),
                           "status": "AVAILABLE" if manifests else "UNAVAILABLE"},
            manifests=manifests,
            coverage_report=coverage,
            leakage=leakage,
            windows={
                "train": f".. {train_cutoff.date().isoformat()}",
                "validation": "embedded in train-window walk-forward",
                "test": f"{test_start.date().isoformat()} .. "
                        f"{test_end.date().isoformat() if test_end else 'end of data'}",
            },
            params=results["fitted_params"],
            experiments=results,
            reliability_table=table,
            score_metrics=score_metrics,
            error_groups=error_analysis(sim_result, repo),
            known_limitations=[
                "data is the registered source only (see SOURCE_STATUS); "
                "no market baseline available (not fabricated)",
                "missing-games accounting requires an official schedule reference",
            ],
        )
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(report_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
