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

    ing = sub.add_parser("ingest", help="ingest a real NBA games CSV (raw preserved, snapshot created)")
    ing.add_argument("csv_path", help="path to the NBA games CSV")
    ing.add_argument("--source", default="nba_games", help="data source name for the raw manifest")
    ing.add_argument("--season", default=None, help="season override (e.g. 2024-25)")
    ing.add_argument("--snapshot-tag", default=None, help="tag for the created snapshot")
    ing.add_argument("--stats-notes", default=None,
                     help="stat-definition notes recorded in the processed manifest (spec §6)")

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
    if args.command == "coverage":
        return _cmd_coverage(args)
    if args.command == "data-status":
        return _cmd_data_status(args)
    if args.command == "experiments":
        return _cmd_experiments(args)

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
    from basketball_agent.data.collectors.nba_csv import IngestDataError, load_nba_games_csv
    from basketball_agent.data.db.loader import DataIngestor
    from basketball_agent.data.snapshots.manager import SnapshotManager
    from basketball_agent.evaluation.coverage import build_coverage_report, format_coverage_report

    try:
        db, settings = _open_db()
        store = RawDataStore(settings.raw_dir)
        manifest = store.register(args.source, args.csv_path)
        raw = load_nba_games_csv(args.csv_path, source=args.source, season=args.season)
        DataIngestor(db).ingest(raw)
        processed = ProcessedDataStore(settings.processed_dir).save(
            args.source, raw,
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
    print(f"Ingested {coverage['games']} games from source '{manifest.source}' "
          f"(sha256 {manifest.file_hash[:12]}...).")
    print(f"Processed dataset: {processed.file_name} (sha256 {processed.file_hash[:12]}...)")
    print(f"Data Snapshot: {snapshot.snapshot_id} (tag: {snapshot.tag})")
    print()
    print(format_coverage_report(coverage))
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


if __name__ == "__main__":
    sys.exit(main())
