"""League-scoped data audit (Phase 8-B4, acceptance item 8).

Audits ONE league at a time and never mixes leagues:

* ``game_id``           — format + uniqueness (EuroLeague: ``E<yyyy>-<gameCode>``)
* ``team.namespace``    — EuroLeague ids must carry the ``EL-`` prefix; NBA ids must not
* ``season.boundaries`` — per-season date ranges, overlap and length sanity
* ``competition.tags``  — tags present and split per season
* ``regulation.minutes``— 40-min (EuroLeague/FIBA: 200) vs 48-min (NBA: 240) + OT steps
* ``timezone.aware``    — every timestamp timezone-aware
* ``missing.games``     — per-season counts vs an optional schedule reference
* ``final.consistency`` — FINAL ⇔ both scores present
* ``provenance.leakage``— sampled as-of feature computation must PASS temporal integrity
                          and stay inside the league (no cross-league history)

The audit is read-only.  ``status`` is FAIL when any check FAILs (WARN never fails).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

#: regulation team-minutes per team per game (5 players x period length)
REGULATION_TEAM_MINUTES = {
    League.NBA: 240,          # 4 x 12
    League.EUROLEAGUE: 200,   # 4 x 10
    League.ACB: 200,          # 4 x 10 (Phase 8-B6)
    League.NBL: 200,          # 4 x 10 (Phase 8-D)
    League.G_LEAGUE: 240,
    League.CBA: 200,
    League.BSL: 200,          # 4 x 10 (FIBA) — Phase 8-F
    League.EUROLEAGUE.value: 200,
    League.NBA.value: 240,
    League.ACB.value: 200,
    League.NBL.value: 200,
    League.CBA.value: 200,    # 4 x 10 (FIBA rules since 2025-26) — Phase 8-E
    League.BSL.value: 200,
}
OT_STEP_MINUTES = 25  # 5-minute overtime in both regulations modelled here

#: per-season regulation overrides (Phase 8-H): the CBA played 4 x 12
#: (240 team-minutes) through 2024-25 and moved to FIBA 4 x 10 (200) in
#: 2025-26 — verified against the official per-game minutes field.
REGULATION_TEAM_MINUTES_BY_SEASON: dict[tuple[str, str], int] = {
    ("CBA", "2024-25"): 240,
}

EUROLEAGUE_ID_RE = re.compile(r"^E\d{4}-\d{4}$")
ACB_ID_RE = re.compile(r"^ACB\d{4}-\d{6}$")
SEASON_LABEL_RE = re.compile(r"^\d{4}-\d{2}$")


@dataclass(frozen=True)
class AuditCheck:
    name: str
    status: str  # PASS / FAIL / WARN
    detail: str


@dataclass(frozen=True)
class SeasonCoverage:
    season: str
    games: int
    regular_season: int
    other_competitions: int
    first_game: str
    last_game: str
    teams: int


@dataclass(frozen=True)
class LeagueAudit:
    league: str
    n_games: int
    seasons: tuple[SeasonCoverage, ...]
    checks: tuple[AuditCheck, ...]
    leakage_sampled: tuple[str, ...]
    leakage_failures: tuple[str, ...]

    @property
    def status(self) -> str:
        return FAIL if any(c.status == FAIL for c in self.checks) else PASS

    @property
    def failed_checks(self) -> tuple[AuditCheck, ...]:
        return tuple(c for c in self.checks if c.status == FAIL)


def _regulation_minutes(league: League) -> int:
    return REGULATION_TEAM_MINUTES.get(league, REGULATION_TEAM_MINUTES.get(league.value, 240))


def _season_coverage(games) -> tuple[SeasonCoverage, ...]:
    by_season: dict[str, list] = {}
    for g in games:
        by_season.setdefault(g.season, []).append(g)
    out: list[SeasonCoverage] = []
    for season in sorted(by_season):
        rows = sorted(by_season[season], key=lambda g: g.game_date)
        regular = sum(1 for g in rows if g.competition is not None
                      and g.competition.value == "REGULAR_SEASON")
        teams = {g.home_team_id for g in rows} | {g.away_team_id for g in rows}
        out.append(SeasonCoverage(
            season=season,
            games=len(rows),
            regular_season=regular,
            other_competitions=len(rows) - regular,
            first_game=rows[0].game_date.date().isoformat(),
            last_game=rows[-1].game_date.date().isoformat(),
            teams=len(teams),
        ))
    return tuple(out)


def audit_league(
    repo: Repository,
    league: League,
    *,
    expected_regular_season_games: int | None = None,
    expected_regular_by_season: dict[str, int] | None = None,
    leakage_sample: int = 3,
    leakage_reference: datetime | None = None,
) -> LeagueAudit:
    """Run the full audit battery for one league (read-only).

    ``expected_regular_season_games`` is one schedule reference applied to
    every season; ``expected_regular_by_season`` (Phase 8-B7) supplies a
    per-season reference instead, which is what a league whose season shape
    changes over time needs (e.g. ACB 2020-21 had 19 clubs / 38 rounds).
    Passing both is an error.
    """
    if expected_regular_season_games is not None and expected_regular_by_season:
        raise ValueError(
            "pass either expected_regular_season_games or "
            "expected_regular_by_season, not both"
        )
    # Audits measure INGESTED (settled) data only: production SCHEDULED
    # rows are a different lifecycle (Phase 12-H fixtures) with no stats
    # to reconcile -- counting them would inflate every per-season pin.
    games = [g for g in repo.list_games() if g.league is league
             and g.status is GameStatus.FINAL]
    checks: list[AuditCheck] = []

    # 1) game_id format + uniqueness ---------------------------------------
    ids = [g.game_id for g in games]
    if league is League.EUROLEAGUE:
        bad_format = [gid for gid in ids if not EUROLEAGUE_ID_RE.match(gid)]
        checks.append(AuditCheck(
            "game_id.format", PASS if not bad_format else FAIL,
            f"{len(ids)} ids; format E<yyyy>-<gameCode>"
            + (f"; offenders: {bad_format[:5]}" if bad_format else ""),
        ))
    elif league is League.ACB:
        bad_format = [gid for gid in ids if not ACB_ID_RE.match(gid)]
        checks.append(AuditCheck(
            "game_id.format", PASS if not bad_format else FAIL,
            f"{len(ids)} ids; format ACB<seasonStartYear>-<matchId>"
            + (f"; offenders: {bad_format[:5]}" if bad_format else ""),
        ))
    else:
        bad_format = [gid for gid in ids
                      if EUROLEAGUE_ID_RE.match(gid) or ACB_ID_RE.match(gid)]
        checks.append(AuditCheck(
            "game_id.format", PASS if not bad_format else FAIL,
            f"{len(ids)} ids; EuroLeague/ACB-style ids must not appear in {league.value}"
            + (f"; offenders: {bad_format[:5]}" if bad_format else ""),
        ))
    duplicates = len(ids) - len(set(ids))
    checks.append(AuditCheck(
        "game_id.unique", PASS if duplicates == 0 else FAIL,
        f"duplicates: {duplicates}",
    ))

    # 2) team namespace -----------------------------------------------------
    team_ids = {t for g in games for t in (g.home_team_id, g.away_team_id)}
    if league is League.EUROLEAGUE:
        wrong = sorted(t for t in team_ids if not t.startswith("EL-"))
        checks.append(AuditCheck(
            "team.namespace", PASS if not wrong else FAIL,
            f"{len(team_ids)} team ids; all must carry the EL- prefix"
            + (f"; offenders: {wrong[:5]}" if wrong else ""),
        ))
    elif league is League.ACB:
        wrong = sorted(t for t in team_ids if not t.startswith("ACB-"))
        checks.append(AuditCheck(
            "team.namespace", PASS if not wrong else FAIL,
            f"{len(team_ids)} team ids; all must carry the ACB- prefix"
            + (f"; offenders: {wrong[:5]}" if wrong else ""),
        ))
    else:
        foreign = sorted(t for t in team_ids
                         if t.startswith("EL-") or t.startswith("ACB-"))
        checks.append(AuditCheck(
            "team.namespace", PASS if not foreign else FAIL,
            f"{len(team_ids)} team ids; no EL-/ACB- ids allowed in {league.value}"
            + (f"; offenders: {foreign[:5]}" if foreign else ""),
        ))
    unregistered = sorted(t for t in team_ids if repo.get_team(t) is None)
    checks.append(AuditCheck(
        "team.registered", PASS if not unregistered else FAIL,
        "all referenced teams exist in the registry"
        if not unregistered else f"unregistered: {unregistered[:5]}",
    ))

    seasons = _season_coverage(games)

    # 3) season boundaries --------------------------------------------------
    overlaps: list[str] = []
    implausible: list[str] = []
    for s in seasons:
        if not SEASON_LABEL_RE.match(s.season):
            implausible.append(f"{s.season}(label)")
        span = (datetime.fromisoformat(s.last_game) - datetime.fromisoformat(s.first_game)).days
        if span > 400:
            implausible.append(f"{s.season}(span={span}d)")
    ordered = sorted(seasons, key=lambda s: s.first_game)
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.first_game <= prev.last_game:
            overlaps.append(f"{prev.season}~{cur.season}")
    checks.append(AuditCheck(
        "season.boundaries", PASS if not (overlaps or implausible) else FAIL,
        f"{len(seasons)} season(s); overlaps: {overlaps or 'none'}; "
        f"implausible: {implausible or 'none'}",
    ))

    # 4) competition tags ---------------------------------------------------
    untagged = sum(1 for g in games if g.competition is None)
    checks.append(AuditCheck(
        "competition.tags",
        PASS if untagged == 0 else WARN,
        f"untagged games: {untagged} of {len(games)}"
        + ("" if untagged == 0 else " (legacy rows; non-regular segments may leak into windows)"),
    ))

    # 5) regulation minutes -------------------------------------------------
    stats = [ts for ts in repo.list_team_stats()
             if any(g.game_id == ts.game_id for g in games)]
    season_of = {g.game_id: g.season for g in games}
    expected = _regulation_minutes(league)
    hard_violations: list[str] = []
    source_quirks: list[str] = []
    ot_games = 0
    for ts in stats:
        ts_season = season_of.get(ts.game_id)
        expected_for = REGULATION_TEAM_MINUTES_BY_SEASON.get(
            (league.value, ts_season), expected)
        delta = ts.minutes - expected_for
        if delta == 0:
            continue
        if delta > 0 and delta % OT_STEP_MINUTES == 0:
            ot_games += 1
            continue
        entry = f"{ts.game_id}/{ts.team_id}={ts.minutes}"
        # outside any physically possible range -> the ingest gate's territory
        if not (180 <= ts.minutes <= 400):
            hard_violations.append(entry)
        else:
            source_quirks.append(entry)
    if hard_violations:
        status, detail = FAIL, (
            f"expected {expected} min/team; OT rows: {ot_games}; "
            f"impossible values: {len(hard_violations)} e.g. {hard_violations[:3]}"
        )
    elif source_quirks:
        status, detail = WARN, (
            f"expected {expected} min/team; OT rows: {ot_games}; "
            f"source aggregation quirks (retained verbatim, not repaired): "
            f"{len(source_quirks)} e.g. {source_quirks[:4]}"
        )
    else:
        status, detail = PASS, f"expected {expected} min/team; OT rows: {ot_games}; violations: 0"
    checks.append(AuditCheck("regulation.minutes", status, detail))

    # 6) timezone awareness -------------------------------------------------
    naive = [g.game_id for g in games if g.game_date.tzinfo is None]
    checks.append(AuditCheck(
        "timezone.aware", PASS if not naive else FAIL,
        "all timestamps timezone-aware" if not naive else f"naive: {naive[:5]}",
    ))

    # 7) missing games ------------------------------------------------------
    if expected_regular_by_season:
        short = [
            f"{s.season}: {s.regular_season}/{expected_regular_by_season.get(s.season)}"
            for s in seasons
            if expected_regular_by_season.get(s.season) is not None
            and s.regular_season != expected_regular_by_season[s.season]
        ]
        unknown = [s.season for s in seasons
                   if s.season not in expected_regular_by_season]
        checks.append(AuditCheck(
            "missing.games", PASS if not short else FAIL,
            f"per-season reference for {len(expected_regular_by_season)} season(s); "
            + ("all seasons complete" if not short else f"incomplete: {short}")
            + (f"; no reference for {unknown}" if unknown else ""),
        ))
    elif expected_regular_season_games is None:
        checks.append(AuditCheck(
            "missing.games", WARN,
            "no schedule reference supplied; per-season regular-season counts reported only",
        ))
    else:
        short = [
            f"{s.season}: {s.regular_season}/{expected_regular_season_games}"
            for s in seasons
            if s.regular_season != expected_regular_season_games
        ]
        checks.append(AuditCheck(
            "missing.games", PASS if not short else FAIL,
            f"expected {expected_regular_season_games} regular-season games/season; "
            + ("all seasons complete" if not short else f"incomplete: {short}"),
        ))

    # 8) FINAL consistency --------------------------------------------------
    bad_final = [
        g.game_id for g in games
        if (g.status is GameStatus.FINAL) != (g.home_score is not None and g.away_score is not None)
    ]
    non_final = sum(1 for g in games if g.status is not GameStatus.FINAL)
    checks.append(AuditCheck(
        "final.consistency", PASS if not bad_final else FAIL,
        f"FINAL/score mismatches: {len(bad_final)}; non-final games: {non_final}"
        + (f"; offenders: {bad_final[:5]}" if bad_final else ""),
    ))

    # 9) provenance / leakage sampling --------------------------------------
    sampled, failures, insufficient = _leakage_sample(
        repo, league, games, leakage_sample, leakage_reference
    )
    checks.append(AuditCheck(
        "provenance.leakage", PASS if not failures else FAIL,
        f"sampled {len(sampled)} game(s); failures: {len(failures)}; "
        f"insufficient-history skips: {len(insufficient)}"
        + (f"; {failures[:3]}" if failures else ""),
    ))

    return LeagueAudit(
        league=league.value,
        n_games=len(games),
        seasons=seasons,
        checks=tuple(checks),
        leakage_sampled=tuple(sampled),
        leakage_failures=tuple(failures),
    )


def _leakage_sample(
    repo: Repository,
    league: League,
    games: list,
    sample_size: int,
    reference: datetime | None,
) -> tuple[list[str], list[str], list[str]]:
    """Run as-of feature computation + temporal integrity on the LATEST games.

    Takes the most recent FINAL games so every sample has real history (a
    season opener with no prior games is *insufficient data*, not a leak, and
    is reported separately).  Also verifies league isolation: every game used
    as history must belong to the same league (no cross-league sharing).
    """
    from basketball_agent.evaluation.leakage import check_temporal_integrity
    from basketball_agent.features import FeatureComputationError, FeaturePipeline

    finals = [g for g in games if g.status is GameStatus.FINAL
              and g.home_score is not None and g.away_score is not None]
    if not finals or sample_size <= 0:
        return [], [], []
    finals.sort(key=lambda g: (g.game_date, g.game_id))
    sample = finals[-sample_size:]
    pipeline = FeaturePipeline(repo)
    sampled: list[str] = []
    failures: list[str] = []
    insufficient: list[str] = []
    for game in sample:
        as_of = reference or game.game_date
        if as_of > game.game_date:
            continue
        try:
            features = pipeline.compute(game.game_id, as_of=as_of)
        except FeatureComputationError as exc:
            insufficient.append(f"{game.game_id}: {exc}")
            continue
        sampled.append(game.game_id)
        report = check_temporal_integrity(features, repo)
        if report.status == FAIL:
            failures.append(f"{game.game_id}: leakage {report.violations[:2]}")
        wrong_league = [
            gid for gid in features.provenance.games_used
            if (repo.get_game(gid) is not None and repo.get_game(gid).league is not league)
        ]
        if wrong_league:
            failures.append(f"{game.game_id}: cross-league history {wrong_league[:3]}")
    return sampled, failures, insufficient


def format_audit(audit: LeagueAudit) -> str:
    lines = [
        f"LEAGUE AUDIT — {audit.league}",
        "=" * 46,
        f"games: {audit.n_games} | seasons: {len(audit.seasons)} | status: {audit.status}",
        "",
        "season coverage:",
        f"{'season':<12}{'games':>7}{'regular':>9}{'other':>7}{'teams':>7}  range",
    ]
    for s in audit.seasons:
        lines.append(
            f"{s.season:<12}{s.games:>7}{s.regular_season:>9}{s.other_competitions:>7}"
            f"{s.teams:>7}  {s.first_game} .. {s.last_game}"
        )
    lines += ["", "checks:"]
    for c in audit.checks:
        lines.append(f"[{c.status:<4}] {c.name:<22} {c.detail}")
    if audit.leakage_sampled:
        lines += ["", f"leakage sample: {', '.join(audit.leakage_sampled)}"]
    if audit.leakage_failures:
        lines += ["leakage failures:"] + [f"  - {f}" for f in audit.leakage_failures]
    return "\n".join(lines)
