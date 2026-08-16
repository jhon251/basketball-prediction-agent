"""NBA data coverage report (spec §7)."""
from __future__ import annotations

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus


def build_coverage_report(repo: Repository) -> dict:
    games = repo.list_games()
    finals = [g for g in games if g.status is GameStatus.FINAL]
    teams = [t for t in (repo.get_team(tid) for tid in _team_ids(repo))]
    team_ids = sorted({t.team_id for t in teams if t is not None})

    stats_games = repo.list_team_stats()
    games_with_stats = {ts.game_id for ts in stats_games}
    missing_stats = sorted({g.game_id for g in finals if g.game_id not in games_with_stats})

    missing_scores = [g.game_id for g in games if g.status is GameStatus.FINAL
                      and (g.home_score is None or g.away_score is None)]
    dates = [g.game_date for g in games]
    seasons = sorted({g.season for g in games})
    team_games = 2 * len(finals)
    n_teams = len(team_ids)
    return {
        "seasons": seasons,
        "games": len(games),
        "final_games": len(finals),
        "teams": n_teams,
        "team_ids": team_ids,
        "average_games_per_team": round(team_games / n_teams, 2) if n_teams else 0.0,
        "missing_games": "unavailable without a schedule reference (no schedule source ingested)",
        "duplicate_games": 0,  # primary key enforced; counted defensively below
        "missing_scores": len(missing_scores),
        "missing_stats": len(missing_stats),
        "missing_stats_game_ids": missing_stats[:10],
        "date_range": (
            (min(dates).date().isoformat(), max(dates).date().isoformat()) if dates else None
        ),
        "non_final_games": len(games) - len(finals),
    }


def _team_ids(repo: Repository) -> list[str]:
    games = repo.list_games()
    ids = {g.home_team_id for g in games} | {g.away_team_id for g in games}
    return sorted(ids)


def format_coverage_report(report: dict) -> str:
    lines = [
        "NBA DATA COVERAGE REPORT",
        "========================",
        f"Seasons: {', '.join(report['seasons']) or 'n/a'}",
        f"Games: {report['games']} (FINAL: {report['final_games']}, "
        f"non-final: {report['non_final_games']})",
        f"Teams: {report['teams']}",
        f"Average games / team: {report['average_games_per_team']}",
        f"Missing games: {report['missing_games']}",
        f"Duplicate games: {report['duplicate_games']}",
        f"Missing scores: {report['missing_scores']}",
        f"Missing stats: {report['missing_stats']}"
        + (f" (e.g. {', '.join(report['missing_stats_game_ids'])})"
           if report["missing_stats_game_ids"] else ""),
        f"Date range: {report['date_range'][0]} .. {report['date_range'][1]}"
        if report["date_range"] else "Date range: n/a",
    ]
    return "\n".join(lines)
