"""``basketball predict <game_id>`` command-line interface (spec §34)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
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
    p.add_argument("--params", default=None,
                   help="apply CALIBRATED parameters from a calibrate --save-params "
                        "JSON file (explicit opt-in; without it the frozen defaults "
                        "are used)")
    p.add_argument("--family", default=None,
                   choices=["elo", "strength", "simulation"],
                   help="explicit primary prediction-family override (Phase 8-K; "
                        "default: the league's configured family, which is "
                        "simulation for all current leagues). 'ensemble' is an "
                        "experimental layer available only programmatically with "
                        "explicit weights")

    st = sub.add_parser("settle", help="settle saved predictions for a FINAL game (spec §33)")
    st.add_argument("game_id", help="FINAL game whose saved predictions should be settled")

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
    exp.add_argument("--train-start", default=None,
                     help="train window start (ISO, UTC); default: all available history")
    exp.add_argument("--train-seasons", type=int, default=None, choices=[1, 3, 5],
                     help="shorthand for --train-start: bound fitting to the last N NBA "
                          "seasons before the cutoff (1/3/5); default: all available")
    exp.add_argument("--test-start", required=True, help="test window start (ISO, UTC)")
    exp.add_argument("--test-end", default=None, help="test window end (ISO, UTC)")
    exp.add_argument("--n-simulations", type=int, default=300)
    exp.add_argument("--seed", type=int, default=7)
    exp.add_argument("--warmup", type=int, default=5)

    wb = sub.add_parser("window-backtest",
                        help="multi-season walk-forward backtest over a REGULAR_SEASON window")
    wb.add_argument("--window-start", required=True, help="evaluation window start (ISO, UTC)")
    wb.add_argument("--window-end", default=None, help="evaluation window end (ISO, UTC)")
    wb.add_argument("--train-seasons", type=int, default=1,
                    help="fit window before the evaluation window, in NBA seasons "
                         "(1/3/5; 0 skips fitting)")
    wb.add_argument("--league", default=None,
                    choices=["NBA", "EUROLEAGUE", "ACB", "NBL", "CBA", "BSL"],
                    help="restrict the evaluation window to one league (default: all)")
    wb.add_argument("--n-simulations", type=int, default=100)

    cal = sub.add_parser("calibrate",
                         help="per-league calibration: fit on the train window, "
                              "compare frozen vs fitted on a held-out window")
    cal.add_argument("--league", required=True,
                     choices=["NBA", "EUROLEAGUE", "ACB", "NBL", "CBA", "BSL"])
    cal.add_argument("--cutoff", required=True,
                     help="train window end (ISO, UTC); the fit sees nothing after it")
    cal.add_argument("--test-start", required=True, help="held-out window start (ISO, UTC)")
    cal.add_argument("--test-end", default=None, help="held-out window end (ISO, UTC)")
    cal.add_argument("--train-seasons", type=int, default=None, choices=[1, 3, 5],
                     help="bound the fitting window to the last N seasons (default: all "
                          "available before the cutoff)")
    cal.add_argument("--train-start", default=None,
                     help="explicit train window start (overrides --train-seasons)")
    cal.add_argument("--save-params", default=None,
                     help="write the fitted parameters to this JSON file (explicit "
                          "opt-in; predict --params applies them)")
    cal.add_argument("--n-simulations", type=int, default=100)
    cal.add_argument("--seed", type=int, default=7)
    cal.add_argument("--warmup", type=int, default=5)
    wb.add_argument("--seed", type=int, default=7)
    wb.add_argument("--warmup", type=int, default=5)

    rep = sub.add_parser("report", help="render the Phase 7 Final Report (spec §21)")
    rep.add_argument("--train-cutoff", required=True)
    rep.add_argument("--test-start", required=True)
    rep.add_argument("--test-end", default=None)
    rep.add_argument("--n-simulations", type=int, default=300)
    rep.add_argument("--seed", type=int, default=7)
    rep.add_argument("--warmup", type=int, default=5)

    la = sub.add_parser("league-audit",
                        help="audit ONE league's data (ids/namespace/seasons/competition/"
                             "regulation/timezone/missing/FINAL/leakage) — read-only")
    la.add_argument("--league", default="EUROLEAGUE",
                    choices=["NBA", "EUROLEAGUE", "ACB", "NBL", "CBA", "BSL"])
    la.add_argument("--leakage-sample", type=int, default=3,
                    help="how many as-of feature computations to audit (0 disables)")
    la.add_argument("--expected-regular", type=int, default=None,
                    help="expected regular-season games per season (e.g. 306 for EuroLeague; "
                         "omit when no schedule reference is available)")
    la.add_argument("--expected-regular-map", default=None,
                    help="per-season schedule reference, e.g. "
                         "\"2020-21=342,2021-22=306\" (league whose season shape changes)")

    col = sub.add_parser("collector",
                         help="production data collection (Phase 10-E; per-league "
                              "isolated, heartbeat/state persisted)")
    col_sub = col.add_subparsers(dest="col_command", required=True)
    col_run = col_sub.add_parser("run", help="run one collection pass")
    col_run.add_argument("--leagues", default=None,
                         help="comma-separated subset (default: all six)")
    col_run.add_argument("--ingest", action="store_true",
                         help="EXPLICIT opt-in: also run the season-refresh gate "
                              "(fetch -> validate -> ingest -> snapshot) for "
                              "league-seasons not yet in the core")
    col_status = col_sub.add_parser("status",
                                    help="collector heartbeat/state (read-only)")
    col_status.add_argument("--league", default=None)

    st = sub.add_parser("agent",
                        help="agent status: collectors, snapshots, data, model")
    st_sub = st.add_subparsers(dest="st_command")
    st_sub.add_parser("status", help="full production status report")

    prod = sub.add_parser("production",
                          help="production auto-prediction loop (Phase 10-E.2: "
                               "collect -> predict -> log -> settle)")
    prod_sub = prod.add_subparsers(dest="prod_command", required=True)
    prod_run = prod_sub.add_parser("run", help="run one production pass")
    prod_run.add_argument("--leagues", default=None,
                          help="comma-separated subset (default: all six)")
    prod_run.add_argument("--max-predictions", type=int, default=None,
                          help="cap predictions per pass (batching)")
    prod_run.add_argument("--skip-collect", action="store_true",
                          help="skip the collection discovery pass")
    prod_status = prod_sub.add_parser("status",
                                      help="production loop state (read-only)")

    ar = sub.add_parser("agent-report",
                        help="read-only Agent orchestration report (9-A..9-E chain; "
                             "no prediction is saved)")
    ar.add_argument("game_id", help="game id in the data snapshot")
    ar.add_argument("--as-of", required=True,
                    help="prediction timestamp (ISO-8601, UTC); the caller owns "
                         "the information boundary")
    ar.add_argument("--family", default=None,
                    choices=["elo", "strength", "simulation"],
                    help="explicit primary prediction-family override")
    ar.add_argument("--params", default=None,
                    help="apply CALIBRATED parameters from a calibrate --save-params "
                         "JSON file (explicit opt-in)")
    ar.add_argument("--log", default=None,
                    help="EXPLICIT opt-in: append the produced AgentBundle to this "
                         "append-only Decision Log directory (Phase 10-D; without "
                         "this flag nothing is persisted)")
    ar.add_argument("--log-settle", action="store_true",
                    help="after logging, settle the game through the existing "
                         "settlement chain and append settlement event(s) to the "
                         "same Decision Log")

    al = sub.add_parser("agent-log",
                        help="append-only Decision Log operations (Phase 10-D)")
    al_sub = al.add_subparsers(dest="log_command", required=True)
    al_verify = al_sub.add_parser("verify",
                                  help="verify the hash chain (read-only)")
    al_verify.add_argument("--log-dir", required=True)
    al_list = al_sub.add_parser("list",
                                help="list stored decision records (read-only)")
    al_list.add_argument("--log-dir", required=True)
    al_list.add_argument("--league", default=None)
    al_settle = al_sub.add_parser("settle",
                                  help="settle a FINAL game and append settlement "
                                       "event(s) for its stored decisions")
    al_settle.add_argument("game_id")
    al_settle.add_argument("--log-dir", required=True)

    ev = sub.add_parser("evaluate",
                        help="read-only evaluation of settled predictions "
                             "(Phase 10-A; league/season/window filters)")
    ev.add_argument("--league", default=None)
    ev.add_argument("--season", default=None)
    ev.add_argument("--window-start", default=None, help="ISO-8601 (UTC)")
    ev.add_argument("--window-end", default=None,
                    help="historical cutoff (exclusive, ISO-8601 UTC)")
    ev.add_argument("--per-model", action="store_true",
                    help="also run the frozen walk-forward engine for "
                         "per-model metrics on the window (explicit windows "
                         "required; slow on large windows)")
    ev.add_argument("--source", default="settled",
                    choices=["settled", "decision-log"],
                    help="evidence source: settled predictions (10-A) or "
                         "the append-only Decision Log (10-B real Agent "
                         "history; joined with settlement events at "
                         "evaluation time)")
    ev.add_argument("--log-dir", default=None,
                    help="Decision Log directory (default: storage/agent_log)")
    ev.add_argument("--family", default=None,
                    help="filter by primary family (decision-log source)")

    ea = sub.add_parser("error-analysis",
                        help="read-only error slices over settled predictions "
                             "(Phase 10-A)")
    ea.add_argument("--league", default=None)
    ea.add_argument("--season", default=None)
    ea.add_argument("--window-start", default=None, help="ISO-8601 (UTC)")
    ea.add_argument("--window-end", default=None,
                    help="historical cutoff (exclusive, ISO-8601 UTC)")
    ea.add_argument("--source", default="settled",
                    choices=["settled", "decision-log"],
                    help="evidence source (10-A settled or 10-B decision log)")
    ea.add_argument("--log-dir", default=None,
                    help="Decision Log directory (default: storage/agent_log)")
    ea.add_argument("--family", default=None,
                    help="filter by primary family (decision-log source)")

    return parser


def _open_db():
    settings = get_settings()
    db = Database(settings.db_path)
    db.initialize()
    return db, settings


def _load_any_games_file(path: str, season: str | None = None) -> dict:
    """Format dispatch for ingest/validate.

    ``.json`` bundles are raw source bundles and are dispatched on their
    ``dataset`` marker (``euroleague-live-v1`` / ``acb-api-v1``); anything
    else goes through the NBA loaders.
    """
    from pathlib import Path as _Path

    if _Path(path).suffix.lower() == ".json":
        import re as _re

        from basketball_agent.data.collectors.acb_api import (
            DATASET_MARKER as ACB_MARKER,
        )
        from basketball_agent.data.collectors.acb_api import load_acb_bundle_file
        from basketball_agent.data.collectors.bsl_livestats import (
            DATASET_MARKER as BSL_MARKER,
        )
        from basketball_agent.data.collectors.bsl_livestats import (
            load_bsl_bundle_file,
        )
        from basketball_agent.data.collectors.cba_api import (
            DATASET_MARKER as CBA_MARKER,
        )
        from basketball_agent.data.collectors.cba_api import load_cba_bundle_file
        from basketball_agent.data.collectors.euroleague_live import (
            load_euroleague_bundle_file,
        )
        from basketball_agent.data.collectors.nbl_api import (
            DATASET_MARKER as NBL_MARKER,
        )
        from basketball_agent.data.collectors.nbl_api import load_nbl_bundle_file

        # peek at the marker without loading the whole (potentially huge) bundle
        try:
            with _Path(path).open(encoding="utf-8", errors="replace") as handle:
                head = handle.read(4096)
            match = _re.search(r'"dataset"\s*:\s*"([^"]+)"', head)
            marker = match.group(1) if match else None
        except OSError:
            marker = None
        if marker == ACB_MARKER:
            return load_acb_bundle_file(path)
        if marker == NBL_MARKER:
            return load_nbl_bundle_file(path)
        if marker == CBA_MARKER:
            return load_cba_bundle_file(path)
        if marker == BSL_MARKER:
            return load_bsl_bundle_file(path)
        return load_euroleague_bundle_file(path)
    from basketball_agent.data.collectors.nba_parquet import load_nba_games_file

    return load_nba_games_file(path, season=season)


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
    if args.command == "window-backtest":
        return _cmd_window_backtest(args)
    if args.command == "calibrate":
        return _cmd_calibrate(args)
    if args.command == "league-audit":
        return _cmd_league_audit(args)
    if args.command == "report":
        return _cmd_report(args)

    if args.command == "predict":
        return _cmd_predict(args)
    if args.command == "settle":
        return _cmd_settle(args)
    if args.command == "agent-report":
        return _cmd_agent_report(args)
    if args.command == "agent-log":
        return _cmd_agent_log(args)
    if args.command == "collector":
        return _cmd_collector(args)
    if args.command == "production":
        return _cmd_production(args)
    if args.command == "agent":
        return _cmd_agent_status(args)
    if args.command == "evaluate":
        return _cmd_evaluate(args)
    if args.command == "error-analysis":
        return _cmd_error_analysis(args)

    return 2


def _eval_filter(args):
    from basketball_agent.evaluation.settled_evaluation import EvaluationFilter

    return EvaluationFilter(
        league=args.league, season=args.season,
        window_start=(_parse_as_of(args.window_start)
                      if args.window_start else None),
        window_end=(_parse_as_of(args.window_end)
                    if args.window_end else None))


def _print_slice_metrics(title: str, slices: dict) -> None:
    print(title)
    for name, m in slices.items():
        insuff = "" if m.sufficient else "  [SMALL SAMPLE — descriptive only]"
        print(f"  {name:28s} n={m.n:4d} acc={m.accuracy} ll={m.log_loss} "
              f"brier={m.brier} ece={m.ece} margin_mae={m.margin_mae} "
              f"total_mae={m.total_mae}{insuff}")
    if not slices:
        print("  (no settled records)")


def _cmd_evaluate(args) -> int:
    from pathlib import Path as _Path

    if getattr(args, "source", "settled") == "decision-log":
        from basketball_agent.agents.decision_log import DecisionLog
        from basketball_agent.evaluation.log_evaluation import (
            DecisionLogEvaluator, LogEvaluationFilter,
        )

        log_dir = args.log_dir or _Path("storage/agent_log")
        ev = DecisionLogEvaluator(DecisionLog(log_dir))
        flt = LogEvaluationFilter(
            league=args.league, season=args.season, family=args.family,
            window_start=args.window_start, window_end=args.window_end)
        out = ev.evaluate(flt, by_model=True)
        print("EVALUATION (real Agent history; Decision Log x Settlement "
              "Log join — evidence_kind: real_agent_history)")
        s = out["primary"]
        print(f"  primary: n={s.n} acc={s.accuracy} logloss={s.log_loss} "
              f"brier={s.brier} ece={s.ece} margin_mae={s.margin_mae} "
              f"total_mae={s.total_mae}"
              + ("" if s.sufficient else "  [SMALL SAMPLE — descriptive only]"))
        print("  by model (stored probabilities, no re-run):")
        for model, m in out["by_model"].items():
            print(f"  {model:12s} n={m.n:4d} acc={m.accuracy} ll={m.log_loss} "
                  f"brier={m.brier} ece={m.ece}")
        _print_slice_metrics("slices:", out["slices"])
        print(f"join stats: {out['join_stats']}")
        return 0

    from basketball_agent.evaluation.settled_evaluation import (
        SettlementEvaluator, per_model_evaluation,
    )

    db, _ = _open_db()
    ev = SettlementEvaluator(db)
    flt = _eval_filter(args)
    overall = ev.evaluate(flt)
    print("EVALUATION (settled predictions; read-only)")
    print(f"  overall: n={overall.n} acc={overall.accuracy} "
          f"logloss={overall.log_loss} brier={overall.brier} ece={overall.ece} "
          f"margin_mae={overall.margin_mae} total_mae={overall.total_mae}"
          + ("" if overall.sufficient else
             f"  [SMALL SAMPLE — descriptive only: {overall.note}]"))
    _print_slice_metrics("by league:", ev.evaluate_by_league(flt))
    _print_slice_metrics("by season:", ev.evaluate_by_season(flt))
    disc = ev.model_disagreement_slices(flt)
    print(f"model disagreement over settled records: available="
          f"{disc['available']} ({disc['reason']})")
    if args.per_model:
        if not args.window_start or not args.window_end:
            print("per-model evaluation requires --window-start and "
                  "--window-end", file=sys.stderr)
            return 1
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.schemas.base import League
        repo = Repository(db)
        league = League(args.league) if args.league in League.__members__ else None
        per_model = per_model_evaluation(
            repo, _parse_as_of(args.window_start),
            _parse_as_of(args.window_end), league=league)
        print("per-model (frozen window-backtest engine, frozen defaults):")
        for model, m in per_model.items():
            print(f"  {model:12s} n={m.get('n_win_prob')} acc={m.get('accuracy')} "
                  f"ll={m.get('log_loss')} brier={m.get('brier_score')} "
                  f"ece={m.get('ece')} margin_mae={m.get('margin_mae')} "
                  f"total_mae={m.get('total_mae')}")
    return 0


def _cmd_error_analysis(args) -> int:
    from pathlib import Path as _Path

    if getattr(args, "source", "settled") == "decision-log":
        from basketball_agent.agents.decision_log import DecisionLog
        from basketball_agent.evaluation.log_evaluation import (
            DecisionLogEvaluator, LogEvaluationFilter,
        )

        log_dir = args.log_dir or _Path("storage/agent_log")
        ev = DecisionLogEvaluator(DecisionLog(log_dir))
        flt = LogEvaluationFilter(
            league=args.league, season=args.season, family=args.family,
            window_start=args.window_start, window_end=args.window_end)
        rows = ev.joined(flt)
        slices = ev.slices(rows)
        print("ERROR ANALYSIS (real Agent history; Decision Log x Settlement "
              "Log — every slice carries its sample count)")
        _print_slice_metrics("slices:", slices)
        print(f"join stats: {ev.last_join_stats}")
        return 0

    from basketball_agent.evaluation.settled_evaluation import SettlementEvaluator

    db, _ = _open_db()
    ev = SettlementEvaluator(db)
    flt = _eval_filter(args)
    print("ERROR ANALYSIS (settled predictions; read-only; every slice "
          "carries its sample count)")
    _print_slice_metrics("fixed slices:", ev.error_slices(flt))
    _print_slice_metrics("calibration drift (settlement half-year):",
                         ev.calibration_drift(flt))
    disc = ev.model_disagreement_slices(flt)
    print(f"model disagreement: available={disc['available']} ({disc['reason']})")
    return 0


def _cmd_agent_report(args) -> int:
    """Read-only Phase 9-E orchestration: prints the full Agent chain
    (9-A..9-D) without saving anything."""
    try:
        db, settings = _open_db()
        repo = Repository(db)
        as_of = _parse_as_of(args.as_of)
        win_config = None
        calibration = None
        if getattr(args, "params", None):
            from basketball_agent.evaluation.param_tuning import load_fitted_params

            fitted = load_fitted_params(args.params)
            win_config = fitted.to_win_config()
            from basketball_agent.agents.decision_contract import (
                CalibrationProvenance,
            )
            calibration = CalibrationProvenance(
                params_file=args.params,
                fitted_window=(fitted.training_window[0],
                               fitted.training_window[1]),
                fitted_at=fitted.fitted_at,
                validation_logloss=fitted.validation_logloss,
                boundary_hits=fitted.boundary_hits,
            )
        core = PredictionAgent(
            repo,
            n_simulations=settings.default_n_simulations,
            seed=settings.default_seed,
            decay_lambda=(fitted.decay_lambda if win_config is not None
                          else settings.decay_lambda),
            snapshot_id=_latest_snapshot_id(),
            win_config=win_config,
            family=getattr(args, "family", None),
        )
        from basketball_agent.agents.orchestration import (
            AgentOrchestrator,
            format_agent_bundle,
        )

        bundle = AgentOrchestrator(core, calibration=calibration).orchestrate(
            args.game_id, as_of=as_of)
    except PredictionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_agent_bundle(bundle))

    if getattr(args, "log", None):
        from basketball_agent.agents.decision_log import DecisionLog
        from basketball_agent.agents.decision_record import DecisionRecordAgent
        from basketball_agent.agents.persistence import persist, settle_and_log
        from basketball_agent.agents.risk_analysis import RiskAnalysisAgent

        risk = RiskAnalysisAgent().analyze(bundle.agent_decision)
        record = DecisionRecordAgent().build(bundle.agent_decision, risk)
        persisted = persist(bundle.agent_decision, risk, record,
                            DecisionLog(args.log))
        print(f"Decision Log: record_id={persisted.record_id} "
              f"status={persisted.status} (dir: {args.log})")
        if getattr(args, "log_settle", False):
            for rid, status, _ in settle_and_log(db, DecisionLog(args.log),
                                                 bundle.context.game_id):
                print(f"Settlement event: record_id={rid} status={status}")
    else:
        print("\n(nothing was saved — agent-report is read-only)")
    return 0


def _cmd_collector(args) -> int:
    from basketball_agent.collection.scheduler import CollectionScheduler
    from basketball_agent.config import get_settings
    from basketball_agent.collection.state import CollectorState

    settings = get_settings()
    state_dir = Path(settings.db_path).parent / "collector"
    db, _ = _open_db()
    scheduler = CollectionScheduler(Repository(db), state_dir)
    if args.col_command == "status":
        states = CollectorState(state_dir).all()
        if args.league:
            states = {k: v for k, v in states.items() if k == args.league.upper()}
        print("COLLECTOR STATE (per league; operational telemetry)")
        for league, s in sorted(states.items()):
            print(f"  {league:12s} heartbeat={s.get('heartbeat')} "
                  f"last_status={s.get('last_status')} "
                  f"last_success={s.get('last_success_at')}")
            print(f"               last: new={s.get('last_new_games')} "
                  f"updated={s.get('last_updated_games')} "
                  f"validation={s.get('last_validation')} "
                  f"errors={s.get('last_errors') or []}")
            if s.get("seasons_ingested"):
                print(f"               seasons ingested: "
                      f"{[x.get('season') if isinstance(x, dict) else x for x in s['seasons_ingested']]}")
        if not states:
            print("  (no collector has run yet)")
        return 0
    if args.col_command == "run":
        leagues = ([x.strip().upper() for x in args.leagues.split(",")]
                   if args.leagues else None)
        results = scheduler.run(leagues)
        print("COLLECTION RUN (per-league isolated; all times UTC)")
        for r in results:
            print(f"  {r.summary()}")
            for err in r.errors:
                print(f"    error: {err[:300]}")
        failed = [r for r in results if r.status == "failed"]
        print(f"pass complete: {len(results) - len(failed)}/{len(results)} "
              f"leagues OK, {len(failed)} failed")

        if getattr(args, "ingest", False):
            from basketball_agent.collection.scheduler import refresh_season
            from basketball_agent.data.snapshots.manager import SnapshotManager
            settings = get_settings()
            for league in (leagues or ["NBA", "EUROLEAGUE", "ACB", "NBL",
                                       "CBA", "BSL"]):
                print(f"season-refresh gate: {league} ...")
                try:
                    rr = refresh_season(Repository(db), state_dir, db,
                                        league)
                except Exception as exc:  # noqa: BLE001 — isolation
                    rr = None
                    print(f"  {league}: refresh failed "
                          f"({type(exc).__name__}: {exc})")
                if rr is not None:
                    print(f"  {league}: {rr.status} season={rr.season} "
                          f"games={rr.games_ingested} "
                          f"snapshot={rr.snapshot_id or '-'}")
                    for err in rr.errors:
                        print(f"    note: {err[:300]}")
        return 0
    return 2


def _cmd_agent_status(args) -> int:
    from basketball_agent import __version__
    from basketball_agent.collection.state import CollectorState
    from basketball_agent.config import get_settings
    from basketball_agent.data.db.repository import Repository
    from basketball_agent.data.snapshots.manager import SnapshotManager

    settings = get_settings()
    db, _ = _open_db()
    repo = Repository(db)
    print("PRODUCTION STATUS")
    print("=" * 60)
    print(f"model version : {__version__}")
    print(f"database      : {len(repo.list_games())} games "
          f"(range {repo.list_games()[0].game_date.date()} .. "
          f"{repo.list_games()[-1].game_date.date()})")
    from collections import Counter
    counts = Counter(g.league.value for g in repo.list_games())
    print("leagues       : " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items())))
    latest = _latest_snapshot_id()
    print(f"snapshot      : {latest or 'none'}")
    print()
    print("collector (Phase 10-E):")
    states = CollectorState(Path(settings.db_path).parent
                            / "collector").all()
    if not states:
        print("  (no collector has run yet — run: collector run)")
    for league, s in sorted(states.items()):
        print(f"  {league:12s} heartbeat={s.get('heartbeat')} "
              f"status={s.get('last_status')} "
              f"last_success={s.get('last_success_at')}")
        print(f"               source={s.get('source')}")
        print(f"               last run: new={s.get('last_new_games')} "
              f"updated={s.get('last_updated_games')} "
              f"validation={s.get('last_validation')} "
              f"errors={s.get('last_errors') or []}")
    print()
    print("agent log (Phase 10-B):",
          f"{sum(1 for _ in (Path(settings.db_path).parent / 'agent_log' / 'decisions.jsonl').open())} decisions"
          if (Path(settings.db_path).parent / "agent_log" / "decisions.jsonl").exists()
          else "empty")
    return 0


def _cmd_production(args) -> int:
    from datetime import datetime, timezone

    from basketball_agent.agents.decision_log import DecisionLog
    from basketball_agent.agents.prediction_agent import PredictionAgent
    from basketball_agent.collection.production_loop import ProductionLoop
    from basketball_agent.collection.scheduler import CollectionScheduler
    from basketball_agent.collection.state import CollectorState
    from pathlib import Path as _Path

    settings = get_settings()
    db, _ = _open_db()
    state_dir = _Path(settings.db_path).parent / "collector"
    scheduler = CollectionScheduler(Repository(db), state_dir)
    core = PredictionAgent(
        Repository(db),
        n_simulations=settings.default_n_simulations,
        seed=settings.default_seed,
        decay_lambda=settings.decay_lambda,
        snapshot_id=_latest_snapshot_id())
    log = DecisionLog(_Path(settings.db_path).parent / "agent_log")
    loop = ProductionLoop(db, scheduler, core, log,
                          CollectorState(state_dir), state_dir=state_dir)
    now = datetime.now(timezone.utc)

    if args.prod_command == "run":
        leagues = ([x.strip().upper() for x in args.leagues.split(",")]
                   if args.leagues else None)
        report = loop.run(leagues, now=now,
                          max_predictions=args.max_predictions,
                          skip_collection=getattr(args, "skip_collect",
                                                  False))
        print("PRODUCTION PASS (collect -> predict -> log -> settle)")
        for p in report.predicted:
            print(f"  predicted: {p['game_id']} [{p['league']}] "
                  f"prob={p['prob_home']:.4f} record={p['record_id']} "
                  f"({p['log_status']})")
        for s in report.settled:
            print(f"  settled: {s['game_id']} record={s['record_id']} "
                  f"correct={s['correct']}")
        for e in report.errors:
            print(f"  error: {e}")
        print(f"pass complete: {report.summary()}")
        return 0
    if args.prod_command == "status":
        state = CollectorState(state_dir).load("LOOP")
        print("PRODUCTION LOOP STATE")
        for k in ("heartbeat", "last_status", "last_new_games",
                  "last_updated_games", "last_loop_summary"):
            print(f"  {k}: {state.get(k)}")
        print(f"  last_errors: {state.get('last_errors') or []}")
        return 0
    return 2


def _cmd_agent_log(args) -> int:
    from pathlib import Path as _Path

    from basketball_agent.agents.decision_log import DecisionLog

    log = DecisionLog(_Path(args.log_dir))
    if args.log_command == "verify":
        ok = log.verify_chain()
        print(f"hash chain: {'VALID' if ok else 'TAMPERED/INVALID'} "
              f"({len(log.records())} records)")
        return 0 if ok else 1
    if args.log_command == "list":
        for r in log.filter_records(league=args.league):
            print(f"{r['record_id']}  {r['as_of']}  {r['league']} "
                  f"{r['season'] or ''}  {r['game_id']}  "
                  f"{r['decision_state']}")
        print(f"total: {len(log.records())} records")
        return 0
    if args.log_command == "settle":
        from basketball_agent.agents.persistence import settle_and_log

        db, _ = _open_db()
        outcomes = settle_and_log(db, log, args.game_id)
        if not outcomes:
            print(f"no stored decision records for game {args.game_id}")
            return 1
        for rid, status, settled in outcomes:
            verdict = "CORRECT" if settled.correct else "WRONG"
            print(f"Settlement event: record_id={rid} status={status} "
                  f"score {settled.actual_home_score}-"
                  f"{settled.actual_away_score} -> {verdict} "
                  f"(brier={settled.brier:.4f})")
        return 0
    return 2


def _cmd_predict(args) -> int:
    try:
        db, settings = _open_db()
        repo = Repository(db)
        as_of = _parse_as_of(args.as_of)
        win_config = None
        params_note = ""
        if getattr(args, "params", None):
            from basketball_agent.evaluation.param_tuning import load_fitted_params

            fitted = load_fitted_params(args.params)
            win_config = fitted.to_win_config()
            params_note = (f"Calibrated params: {args.params} "
                           f"(HCA={fitted.home_court_advantage:.2f} "
                           f"scale={fitted.margin_logistic_scale:.2f} "
                           f"k={fitted.shrinkage_k:.1f} "
                           f"lambda={fitted.decay_lambda:.3f}, fitted "
                           f"{fitted.training_window[0][:10]} .. "
                           f"{fitted.training_window[1][:10]})")
        agent = PredictionAgent(
            repo,
            n_simulations=args.n_simulations or settings.default_n_simulations,
            seed=args.seed if args.seed is not None else settings.default_seed,
            decay_lambda=(fitted.decay_lambda if win_config is not None
                          else settings.decay_lambda),
            snapshot_id=_latest_snapshot_id(),
            win_config=win_config,
            family=getattr(args, "family", None),
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

    if params_note:
        print(params_note)
    print(f"Primary family: {prediction.primary_family} "
          f"(source: {prediction.primary_family_source})")
    print(format_report(prediction, team_names))

    if args.save:
        store = SettlementStore(db)
        prediction_id = store.save(prediction)
        print(f"\nPrediction saved (id={prediction_id}); it will be settled once the game is FINAL.")

    return 0


def _cmd_settle(args) -> int:
    try:
        db, _ = _open_db()
        store = SettlementStore(db)
        settled = store.settle(args.game_id)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    verdict = "CORRECT" if settled.correct else "WRONG"
    print(f"Settled prediction {settled.prediction_id} for {settled.game_id}: "
          f"prob_home={settled.prob_home:.4f}, "
          f"final score {settled.actual_home_score}-{settled.actual_away_score}, "
          f"brier={settled.brier:.4f}, {verdict}")
    print(f"Aggregate over settled predictions: {store.aggregate_metrics()}")
    return 0


def _cmd_ingest(args) -> int:
    from basketball_agent.data.collectors.manifest import ProcessedDataStore, RawDataStore
    from basketball_agent.data.collectors.nba_csv import IngestDataError
    from basketball_agent.data.collectors.nba_parquet import (
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
            raws.append(_load_any_games_file(path, season=args.season))
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
        merge_raw_datasets,
    )
    from basketball_agent.data.validators.parsing import parse_dataset

    try:
        raws = [_load_any_games_file(p, season=args.season) for p in args.paths]
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
    from basketball_agent.evaluation.experiments import resolve_train_start, run_phase7_experiments
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
        if args.train_start and args.train_seasons:
            print("FAIL: use either --train-start or --train-seasons, not both",
                  file=sys.stderr)
            return 1
        train_start = (_parse_as_of(args.train_start) if args.train_start
                       else resolve_train_start(train_cutoff, args.train_seasons))
        results = run_phase7_experiments(
            Repository(db), train_cutoff, test_start, test_end,
            n_simulations=args.n_simulations, seed=args.seed, warmup_games=args.warmup,
            train_start=train_start,
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


def _cmd_window_backtest(args) -> int:
    from basketball_agent.data.schemas.base import League
    from basketball_agent.evaluation.experiments import run_window_backtest

    try:
        db, _ = _open_db()
        window_start = _parse_as_of(args.window_start)
        window_end = _parse_as_of(args.window_end) if args.window_end else None
        if window_start is None:
            print("FAIL: --window-start must be a valid ISO timestamp", file=sys.stderr)
            return 1
        result = run_window_backtest(
            Repository(db), window_start, window_end,
            train_seasons=(args.train_seasons or None),
            n_simulations=args.n_simulations, seed=args.seed, warmup_games=args.warmup,
            league=(League(args.league) if args.league else None),
        )
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"WINDOW BACKTEST {result['window_start'][:10]} .. "
          f"{str(result['window_end'])[:10]}  "
          f"(games: {result['n_window_games']}, train_seasons: {result['train_seasons']})")
    if result["fitted_params"] is not None:
        fp = result["fitted_params"]
        print(f"fitted (train-window only, NOT applied to model defaults): "
              f"HCA={fp.home_court_advantage:.2f} scale={fp.margin_logistic_scale:.2f} "
              f"k={fp.shrinkage_k:.1f} lambda={fp.decay_lambda:.3f} "
              f"val_logloss={fp.validation_logloss:.4f}")
    print()
    header = (f"{'model':<12}{'n':>6}{'acc':>8}{'logloss':>9}{'brier':>8}{'ece':>8}"
              f"{'margin_mae':>12}{'total_mae':>11}")
    print(header)
    for name, metrics in result["models"].items():
        print(f"{name:<12}{metrics['n_win_prob']:>6}"
              f"{_fmt(metrics['accuracy']):>8}{_fmt(metrics['log_loss']):>9}"
              f"{_fmt(metrics['brier_score']):>8}{_fmt(metrics['ece']):>8}"
              f"{_fmt(metrics['margin_mae']):>12}{_fmt(metrics['total_mae']):>11}")
    return 0


def _cmd_league_audit(args) -> int:
    from basketball_agent.data.schemas.base import League
    from basketball_agent.evaluation.league_audit import audit_league, format_audit

    try:
        db, _ = _open_db()
        expected_map = None
        if args.expected_regular_map:
            expected_map = {}
            for chunk in args.expected_regular_map.split(","):
                season, _, count = chunk.strip().partition("=")
                if not season or not count.strip().isdigit():
                    print(f"FAIL: --expected-regular-map entry {chunk!r} is not "
                          "'<season>=<count>'", file=sys.stderr)
                    return 1
                expected_map[season.strip()] = int(count)
        audit = audit_league(
            Repository(db),
            League(args.league),
            expected_regular_season_games=args.expected_regular,
            expected_regular_by_season=expected_map,
            leakage_sample=args.leakage_sample,
        )
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(format_audit(audit))
    return 0 if audit.status == "PASS" else 1


def _fmt(value):
    return f"{value:.4f}" if value is not None else "n/a"


def _cmd_calibrate(args) -> int:
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    from basketball_agent.data.schemas.base import League
    from basketball_agent.evaluation.experiments import resolve_train_start
    from basketball_agent.evaluation.league_calibration import (
        calibrate_window, frozen_vs_fitted_table,
    )
    from basketball_agent.evaluation.param_tuning import save_fitted_params

    try:
        db, _ = _open_db()
        cutoff = _parse_as_of(args.cutoff)
        test_start = _parse_as_of(args.test_start)
        test_end = _parse_as_of(args.test_end) if args.test_end else None
        if cutoff is None or test_start is None:
            print("FAIL: --cutoff and --test-start must be valid ISO timestamps",
                  file=sys.stderr)
            return 1
        if args.train_start and args.train_seasons:
            print("FAIL: use either --train-start or --train-seasons, not both",
                  file=sys.stderr)
            return 1
        league = League(args.league)
        if args.train_start:
            train_start = _parse_as_of(args.train_start)
        else:
            # anchor the fit window to the season BEFORE the cutoff (a cutoff
            # on a season boundary would otherwise produce an empty window)
            train_start = (resolve_train_start(cutoff - _td(days=1), args.train_seasons)
                           if args.train_seasons else None)
        result = calibrate_window(
            Repository(db), league,
            cutoff=cutoff, test_start=test_start, test_end=test_end,
            train_start=train_start,
            n_simulations=args.n_simulations, seed=args.seed, warmup_games=args.warmup,
        )
        if args.save_params:
            save_fitted_params(result["fitted"], args.save_params)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(frozen_vs_fitted_table(result))
    if args.save_params:
        print(f"fitted parameters saved: {args.save_params} "
              "(apply explicitly with: basketball predict <game_id> --params "
              f"{args.save_params})")
    if result["fitted"].decay_lambda == 0.0:
        print("lambda=0.0 chosen on the train window: no decay benefit -> "
              "the frozen defaults remain the reference (fitted == frozen "
              "except HCA/scale/k).")
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
