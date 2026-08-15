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


if __name__ == "__main__":
    sys.exit(main())
