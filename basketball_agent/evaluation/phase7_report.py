"""Phase 7 final report renderer (spec §21).

Produces the markdown report with data provenance, temporal integrity,
train/validation/test splits, results, calibration, experiments, an
evidence-based conclusion and known limitations.
"""
from __future__ import annotations

from basketball_agent.data.collectors.manifest import DataSourceManifest
from basketball_agent.evaluation.calibration import ReliabilityBin
from basketball_agent.evaluation.coverage import format_coverage_report
from basketball_agent.evaluation.param_tuning import FittedParams


def _fmt_metric(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _calibration_lines(table: list[ReliabilityBin]) -> list[str]:
    lines = ["| bucket | predicted | actual | n |", "|---|---|---|---|"]
    for b in table:
        if b.count == 0:
            continue
        lines.append(
            f"| {b.bin_low:.2f}-{b.bin_high:.2f} | "
            f"{b.mean_predicted:.3f} | {b.actual_rate:.3f} | {b.count} |"
        )
    return lines


def build_phase7_report(
    *,
    source_status: dict,
    manifests: list[DataSourceManifest],
    coverage_report: dict,
    leakage: dict,
    windows: dict,
    params: FittedParams | None,
    experiments: dict,
    reliability_table: list[ReliabilityBin],
    score_metrics: dict,
    error_groups: dict,
    known_limitations: list[str],
) -> str:
    lines = [
        "# Phase 7 Final Report",
        "",
        "## Data",
        "",
        f"Source: {source_status.get('source', 'n/a')}",
        f"Status: {source_status.get('status', 'n/a')}",
        f"Date Range: {coverage_report.get('date_range')}",
        f"Seasons: {', '.join(coverage_report.get('seasons', [])) or 'n/a'}",
        f"Games: {coverage_report.get('games', 'n/a')}",
        f"Teams: {coverage_report.get('teams', 'n/a')}",
        f"Missing: {coverage_report.get('missing_games', 'n/a')} "
        f"(missing scores: {coverage_report.get('missing_scores', 0)}, "
        f"missing stats: {coverage_report.get('missing_stats', 0)})",
        f"Duplicates: {coverage_report.get('duplicate_games', 0)}",
        "",
        "### Raw source manifests",
        "",
    ]
    for m in manifests:
        lines.append(
            f"- `{m.source}` / {m.file_name} — {m.status}, retrieved {m.retrieved_at[:19]}, "
            f"sha256 {m.file_hash[:16]}…, version: {m.source_version or 'n/a'}"
        )
    if not manifests:
        lines.append("- (none registered)")

    lines += [
        "",
        "## Temporal Integrity",
        "",
        f"Leakage Tests: {leakage.get('total', 0)}",
        f"Passed: {leakage.get('passed', 0)}",
        f"Failed: {leakage.get('failed', 0)}",
        "",
        "## Backtest",
        "",
        f"Train: {windows.get('train', 'n/a')}",
        f"Validation: {windows.get('validation', 'n/a')}",
        f"Test: {windows.get('test', 'n/a')}",
        "",
        "## Results",
        "",
        "| Model | Accuracy | LogLoss | Brier | ECE |",
        "|---|---|---|---|---|",
    ]
    for name, metrics in experiments.get("EXP-NBA-004", {}).get("models", {}).items():
        lines.append(
            f"| {name} | {_fmt_metric(metrics.get('accuracy'))} | "
            f"{_fmt_metric(metrics.get('log_loss'))} | "
            f"{_fmt_metric(metrics.get('brier_score'))} | "
            f"{_fmt_metric(metrics.get('ece'))} |"
        )
    if not experiments.get("EXP-NBA-004", {}).get("models"):
        lines.append("| (no results — test window unavailable) | - | - | - | - |")

    lines += [
        "",
        "## Score Prediction",
        "",
        f"Home MAE: {_fmt_metric(score_metrics.get('home_mae'))}",
        f"Away MAE: {_fmt_metric(score_metrics.get('away_mae'))}",
        f"Total MAE: {_fmt_metric(score_metrics.get('total_mae'))}",
        f"Margin MAE: {_fmt_metric(score_metrics.get('margin_mae'))}",
        "",
        "## Calibration",
        "",
        *_calibration_lines(reliability_table),
        "",
        "## Experiments",
        "",
    ]
    for exp_id in ("EXP-NBA-001", "EXP-NBA-002", "EXP-NBA-003", "EXP-NBA-004"):
        exp = experiments.get(exp_id, {})
        lines.append(f"### {exp_id}")
        lines.append(f"Description: {exp.get('description', 'not run')}")
        lines.append(f"Window: {exp.get('test_window') or exp.get('train_cutoff') or 'n/a'}")
        lines.append("")
        if exp_id == "EXP-NBA-002":
            lines.append(f"Chosen lambda: {exp.get('chosen_lambda')} "
                         f"(validation logloss {_fmt_metric(exp.get('chosen_validation_logloss'))})")
        if exp_id == "EXP-NBA-003":
            lines.append(f"Weights: {exp.get('weights')}")
            lines.append(f"Test logloss: {exp.get('test_logloss')}")
        if exp_id == "EXP-NBA-004":
            lines.append(f"Comparison: {exp.get('simulation_vs_baselines')}")
        lines.append("")

    if params is not None:
        lines += [
            "## Frozen Parameters",
            "",
            f"| parameter | value |",
            "|---|---|",
            f"| HCA | {params.home_court_advantage:.2f} |",
            f"| logistic scale | {params.margin_logistic_scale:.2f} |",
            f"| shrinkage k | {params.shrinkage_k:.1f} |",
            f"| decay lambda | {params.decay_lambda:.3f} |",
            f"| training window | {params.training_window[0][:10]} .. {params.training_window[1][:10]} |",
            f"| validation logloss | {params.validation_logloss:.4f} |",
            "",
        ]

    lines += ["## Error Analysis", ""]
    for group_name, metrics in error_groups.items():
        lines.append(
            f"- {group_name}: n={metrics.get('n', 0)}, "
            f"acc={_fmt_metric(metrics.get('accuracy'))}, "
            f"logloss={_fmt_metric(metrics.get('log_loss'))}, "
            f"brier={_fmt_metric(metrics.get('brier'))}"
        )
    if not error_groups:
        lines.append("- (no test predictions to analyze)")

    lines += ["", "## Conclusion", ""]
    lines.extend(_conclusion(experiments))

    lines += ["", "## Known Limitations", ""]
    for limitation in known_limitations:
        lines.append(f"- {limitation}")
    return "\n".join(lines) + "\n"


def _conclusion(experiments: dict) -> list[str]:
    answers: list[str] = []
    exp001 = experiments.get("EXP-NBA-001", {}).get("models", {})
    exp003 = experiments.get("EXP-NBA-003", {})
    exp004 = experiments.get("EXP-NBA-004", {})

    if not exp001:
        return ["(insufficient data to conclude — see Known Limitations)"]

    sim = exp001.get("simulation", {})
    elo = exp001.get("elo", {})
    random_ll = exp001.get("random", {}).get("log_loss")
    sim_ll = sim.get("log_loss")
    elo_ll = elo.get("log_loss")

    if sim_ll is None or random_ll is None:
        answers.append("1. V1 on real NBA data: not evaluable (no test predictions).")
    elif sim_ll < random_ll - 0.01:
        answers.append(
            f"1. V1 on real NBA data: YES — simulation logloss {sim_ll:.4f} beats the "
            f"random baseline {random_ll:.4f}."
        )
    else:
        answers.append(
            f"1. V1 on real NBA data: NOT PROVEN — simulation logloss {sim_ll:.4f} does not "
            f"beat the random baseline {random_ll:.4f}."
        )

    cmp = exp004.get("simulation_vs_baselines", {})
    better = [k for k, v in cmp.items() if "BETTER" in v]
    worse = [k for k, v in cmp.items() if "WORSE" in v]
    if better and not worse:
        answers.append(f"2. Simulation vs baselines: better than {', '.join(better)}.")
    elif worse:
        answers.append(f"2. Simulation vs baselines: worse than/equal to {', '.join(worse)}.")
    else:
        answers.append("2. Simulation vs baselines: no clear advantage (≈ baseline).")

    test_ll = exp003.get("test_logloss", {})
    if test_ll:
        ens = test_ll.get("ensemble")
        best_single = min(
            (v for k, v in test_ll.items() if k != "ensemble" and v is not None), default=None
        )
        if ens is None or best_single is None:
            answers.append("3. Ensemble: not evaluable on the test window.")
        elif ens < best_single - 0.005:
            answers.append(
                f"3. Ensemble vs best single model: YES — ensemble {ens:.4f} beats "
                f"best single {best_single:.4f}."
            )
        else:
            answers.append(
                f"3. Ensemble vs best single model: NO — ensemble {ens:.4f} does not beat "
                f"best single {best_single:.4f}."
            )
    else:
        answers.append("3. Ensemble: not evaluable on the test window.")

    answers.append("4. Parameters needing adjustment: see Frozen Parameters / EXP-NBA-002 sweep.")
    answers.append("5. Model weaknesses: see Error Analysis groups with the worst logloss.")
    answers.append("6. Next step: fix the weakest error-analysis group before adding features.")
    return answers
