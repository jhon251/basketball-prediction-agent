"""Explicit, auditable data-quality checks.

Each check is a pure function that appends ``ValidationIssue`` records to a
``ValidationReport``.  Nothing fails silently: every anomaly is recorded with
a stable machine-readable check id and a severity (ERROR blocks ingestion,
WARNING is recorded for review).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Iterable, Mapping

from basketball_agent.data.schemas.base import GameStatus, PlayerAvailabilityStatus
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.schemas.game import (
    Game,
    PlayerAvailability,
    PlayerGameStats,
    TeamGameStats,
)


class Severity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass(frozen=True)
class ValidationIssue:
    """One recorded data-quality anomaly."""

    severity: Severity
    check: str
    record_type: str
    record_id: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class ValidationReport:
    """Accumulated issues plus the set of checks that actually ran."""

    issues: list[ValidationIssue] = field(default_factory=list)
    checks_run: set[str] = field(default_factory=set)

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def summary(self) -> dict:
        return {
            "total_issues": len(self.issues),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "checks_run": sorted(self.checks_run),
        }

    def to_text(self) -> str:
        if not self.issues:
            return "No issues found."
        lines = [
            f"[{i.severity.value}] {i.check}: {i.record_type} {i.record_id} — {i.message}"
            for i in self.issues
        ]
        return "\n".join(lines)


def _add(
    report: ValidationReport,
    severity: Severity,
    check_id: str,
    record_type: str,
    record_id: str,
    message: str,
    details: dict | None = None,
) -> None:
    report.add(ValidationIssue(severity, check_id, record_type, record_id, message, details or {}))


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------

def check_duplicate_games(report: ValidationReport, games: Iterable[Game]) -> None:
    report.checks_run.add("completeness.duplicate_games")
    seen: dict[str, int] = {}
    for g in games:
        seen[g.game_id] = seen.get(g.game_id, 0) + 1
    for game_id, count in seen.items():
        if count > 1:
            _add(report, Severity.ERROR, "completeness.duplicate_games", "Game", game_id,
                 f"game_id appears {count} times", {"count": count})


def check_game_dates(report: ValidationReport, games: Iterable[Game], now: datetime) -> None:
    report.checks_run.add("completeness.game_dates")
    for g in games:
        if g.game_date.year < 1900:
            _add(report, Severity.ERROR, "completeness.game_dates", "Game", g.game_id,
                 f"implausible game date: {g.game_date.isoformat()}")
        if g.status is GameStatus.FINAL and g.game_date > now:
            _add(report, Severity.ERROR, "completeness.game_dates", "Game", g.game_id,
                 f"FINAL game dated in the future (game_date={g.game_date.isoformat()} "
                 f"> now={now.isoformat()})")


def check_team_stat_coverage(
    report: ValidationReport, games: Iterable[Game], team_stats: Iterable[TeamGameStats]
) -> None:
    report.checks_run.add("completeness.team_stat_coverage")
    rows_by_game: dict[str, dict[str, TeamGameStats]] = {}
    for ts in team_stats:
        rows_by_game.setdefault(ts.game_id, {})[ts.team_id] = ts
    for g in games:
        if g.status is not GameStatus.FINAL:
            continue
        rows = rows_by_game.get(g.game_id, {})
        for team_id, side in ((g.home_team_id, "home"), (g.away_team_id, "away")):
            if team_id not in rows:
                _add(report, Severity.ERROR, "completeness.team_stat_coverage", "Game", g.game_id,
                     f"missing {side} team box score for FINAL game")
        for team_id in rows:
            if team_id not in (g.home_team_id, g.away_team_id):
                _add(report, Severity.ERROR, "completeness.team_stat_coverage", "TeamGameStats",
                     f"{g.game_id}/{team_id}", "box score references a team that did not play in this game")


# ---------------------------------------------------------------------------
# Consistency
# ---------------------------------------------------------------------------

def check_team_stat_consistency(report: ValidationReport, team_stats: Iterable[TeamGameStats]) -> None:
    report.checks_run.add("consistency.team_stats")
    for ts in team_stats:
        rid = f"{ts.game_id}/{ts.team_id}"

        def fail(message: str, details: dict | None = None) -> None:
            _add(report, Severity.ERROR, "consistency.team_stats", "TeamGameStats", rid, message, details)

        points_from_shots = 2 * (ts.fg - ts.fg3) + 3 * ts.fg3 + ts.ft
        if points_from_shots != ts.points:
            fail(f"points ({ts.points}) != 2*(fg-fg3) + 3*fg3 + ft = {points_from_shots}")
        if ts.fg > ts.fga:
            fail(f"FG ({ts.fg}) > FGA ({ts.fga})")
        if ts.fg3 > ts.fg3a:
            fail(f"FG3 ({ts.fg3}) > FG3A ({ts.fg3a})")
        if ts.fg3 > ts.fg:
            fail(f"FG3 ({ts.fg3}) > FG ({ts.fg}): 3PM are part of FGM")
        if ts.fg3a > ts.fga:
            fail(f"FG3A ({ts.fg3a}) > FGA ({ts.fga})")
        if ts.ft > ts.fta:
            fail(f"FT ({ts.ft}) > FTA ({ts.fta})")
        if ts.orb + ts.drb != ts.trb:
            fail(f"ORB ({ts.orb}) + DRB ({ts.drb}) != TRB ({ts.trb})")
        if ts.points > 3 * ts.fga + ts.fta:
            fail(f"points ({ts.points}) exceed theoretical maximum 3*FGA+FTA ({3 * ts.fga + ts.fta})")


def check_team_stat_bounds(report: ValidationReport, team_stats: Iterable[TeamGameStats]) -> None:
    report.checks_run.add("consistency.team_stat_bounds")
    for ts in team_stats:
        rid = f"{ts.game_id}/{ts.team_id}"
        if not (180 <= ts.minutes <= 400):
            _add(report, Severity.ERROR, "consistency.team_stat_bounds", "TeamGameStats", rid,
                 f"team minutes ({ts.minutes}) outside plausible range [180, 400]")
        if not (40.0 <= ts.possessions <= 200.0):
            _add(report, Severity.ERROR, "consistency.team_stat_bounds", "TeamGameStats", rid,
                 f"possessions ({ts.possessions}) outside plausible range [40, 200]")


def check_player_stat_bounds(report: ValidationReport, player_stats: Iterable[PlayerGameStats]) -> None:
    report.checks_run.add("consistency.player_stats")
    for ps in player_stats:
        rid = f"{ps.game_id}/{ps.player_id}"
        if ps.minutes > 60:
            _add(report, Severity.ERROR, "consistency.player_stats", "PlayerGameStats", rid,
                 f"minutes ({ps.minutes}) > 60 is impossible")
        elif ps.minutes > 48:
            _add(report, Severity.WARNING, "consistency.player_stats", "PlayerGameStats", rid,
                 f"minutes ({ps.minutes}) > 48 exceeds regulation — verify overtime game")
        max_points = 3 * ps.fga + ps.fta
        if ps.points > max_points + 2:
            _add(report, Severity.ERROR, "consistency.player_stats", "PlayerGameStats", rid,
                 f"points ({ps.points}) exceed theoretical maximum 3*FGA+FTA+2 ({max_points + 2})")


def check_game_score_vs_team_stats(
    report: ValidationReport, games: Iterable[Game], team_stats: Iterable[TeamGameStats]
) -> None:
    report.checks_run.add("consistency.game_vs_team_stats")
    rows_by_game: dict[str, list[TeamGameStats]] = {}
    for ts in team_stats:
        rows_by_game.setdefault(ts.game_id, []).append(ts)
    for g in games:
        if g.status is not GameStatus.FINAL:
            continue
        for ts in rows_by_game.get(g.game_id, []):
            rid = f"{g.game_id}/{ts.team_id}"
            if ts.team_id not in (g.home_team_id, g.away_team_id):
                continue  # flagged by completeness.team_stat_coverage
            if ts.is_home != (ts.team_id == g.home_team_id):
                _add(report, Severity.ERROR, "consistency.game_vs_team_stats", "TeamGameStats", rid,
                     "is_home flag contradicts game home_team_id")
            expected = g.home_score if ts.team_id == g.home_team_id else g.away_score
            if ts.points != expected:
                _add(report, Severity.ERROR, "consistency.game_vs_team_stats", "TeamGameStats", rid,
                     f"team box score points ({ts.points}) != game score ({expected})")


def check_availability_consistency(
    report: ValidationReport, availability: Iterable[PlayerAvailability]
) -> None:
    report.checks_run.add("consistency.availability")
    for a in availability:
        rid = f"{a.game_id}/{a.player_id}@{a.source_time.isoformat()}"
        if a.status is PlayerAvailabilityStatus.OUT and a.availability_probability > 0.0:
            _add(report, Severity.WARNING, "consistency.availability", "PlayerAvailability", rid,
                 f"OUT status with availability_probability = {a.availability_probability}")
        if a.status is PlayerAvailabilityStatus.AVAILABLE and a.availability_probability < 1.0:
            _add(report, Severity.WARNING, "consistency.availability", "PlayerAvailability", rid,
                 f"AVAILABLE status with availability_probability = {a.availability_probability}")


# ---------------------------------------------------------------------------
# Temporal integrity (leakage foundation)
# ---------------------------------------------------------------------------

def check_availability_temporal(
    report: ValidationReport,
    availability: Iterable[PlayerAvailability],
    games_by_id: Mapping[str, Game],
) -> None:
    report.checks_run.add("temporal.availability_source_time")
    for a in availability:
        game = games_by_id.get(a.game_id)
        if game is None:
            continue  # flagged by referential.integrity
        if a.source_time > game.game_date:
            _add(report, Severity.ERROR, "temporal.availability_source_time", "PlayerAvailability",
                 f"{a.game_id}/{a.player_id}",
                 f"source_time ({a.source_time.isoformat()}) is after game time "
                 f"({game.game_date.isoformat()}) — future information")


# ---------------------------------------------------------------------------
# Referential integrity
# ---------------------------------------------------------------------------

def check_referential_integrity(report: ValidationReport, dataset: Dataset) -> None:
    report.checks_run.add("referential.integrity")
    # None = registry not provided (skip existence checks);
    # a set (even empty) = registry provided, enforce existence strictly.
    team_ids = {t.team_id for t in dataset.teams} if dataset.teams is not None else None
    player_ids = {p.player_id for p in dataset.players} if dataset.players is not None else None
    game_ids = {g.game_id for g in dataset.games}

    for g in dataset.games:
        for tid in (g.home_team_id, g.away_team_id):
            if team_ids is not None and tid not in team_ids:
                _add(report, Severity.ERROR, "referential.integrity", "Game", g.game_id,
                     f"references unknown team {tid}")
    for ts in dataset.team_stats:
        if ts.game_id not in game_ids:
            _add(report, Severity.ERROR, "referential.integrity", "TeamGameStats",
                 f"{ts.game_id}/{ts.team_id}", "references unknown game")
        if team_ids is not None and ts.team_id not in team_ids:
            _add(report, Severity.ERROR, "referential.integrity", "TeamGameStats",
                 f"{ts.game_id}/{ts.team_id}", f"references unknown team {ts.team_id}")
    game_teams = {g.game_id: (g.home_team_id, g.away_team_id) for g in dataset.games}
    for ps in dataset.player_stats:
        if ps.game_id not in game_ids:
            _add(report, Severity.ERROR, "referential.integrity", "PlayerGameStats",
                 f"{ps.game_id}/{ps.player_id}", "references unknown game")
        if player_ids is not None and ps.player_id not in player_ids:
            _add(report, Severity.ERROR, "referential.integrity", "PlayerGameStats",
                 f"{ps.game_id}/{ps.player_id}", f"references unknown player {ps.player_id}")
        if ps.game_id in game_teams and ps.team_id not in game_teams[ps.game_id]:
            _add(report, Severity.ERROR, "referential.integrity", "PlayerGameStats",
                 f"{ps.game_id}/{ps.player_id}", f"team {ps.team_id} did not play in game {ps.game_id}")
    for a in dataset.availability:
        if a.game_id not in game_ids:
            _add(report, Severity.ERROR, "referential.integrity", "PlayerAvailability",
                 f"{a.game_id}/{a.player_id}", "references unknown game")
        if player_ids is not None and a.player_id not in player_ids:
            _add(report, Severity.ERROR, "referential.integrity", "PlayerAvailability",
                 f"{a.game_id}/{a.player_id}", f"references unknown player {a.player_id}")


#: All checks in execution order — the single source of truth for the agent.
ALL_CHECKS: list[tuple[str, Callable[..., None]]] = [
    ("check_duplicate_games", check_duplicate_games),
    ("check_game_dates", check_game_dates),
    ("check_team_stat_coverage", check_team_stat_coverage),
    ("check_team_stat_consistency", check_team_stat_consistency),
    ("check_team_stat_bounds", check_team_stat_bounds),
    ("check_player_stat_bounds", check_player_stat_bounds),
    ("check_game_score_vs_team_stats", check_game_score_vs_team_stats),
    ("check_availability_consistency", check_availability_consistency),
    ("check_availability_temporal", check_availability_temporal),
    ("check_referential_integrity", check_referential_integrity),
]
