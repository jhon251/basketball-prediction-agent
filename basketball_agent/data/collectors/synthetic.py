"""Synthetic league generator (research-grade test data).

Generates a fully self-consistent season: every box score satisfies the
validation identities BY CONSTRUCTION (points == 2*(fg-fg3)+3*fg3+ft,
ORB+DRB==TRB, attempt/percentage bounds, player lines below their theoretical
maxima).  The whole pipeline, backtest and calibration stack can therefore be
exercised end-to-end without real-world data access.

This is NOT real basketball data and must never be presented as such — it is
a controlled test bed whose true team strengths are known by construction.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game, PlayerGameStats, TeamGameStats
from basketball_agent.data.schemas.player import Player
from basketball_agent.data.schemas.team import Team

UTC = timezone.utc


@dataclass(frozen=True)
class SyntheticLeagueConfig:
    n_teams: int = 6
    games_per_pair: int = 2
    seed: int = 2025
    league_ortg: float = 110.0
    team_strength_sigma: float = 4.0
    pace_mean: float = 99.0
    pace_sigma: float = 3.0
    base_p3_share: float = 0.38
    base_p2_pct: float = 0.53
    base_p3_pct: float = 0.36
    base_ft_pct: float = 0.78
    ft_rate: float = 0.22
    fga_rate: float = 0.84
    tov_rate: float = 0.13
    orb_pct: float = 0.24
    season: str = "2024-25"
    start_date: datetime = datetime(2024, 11, 1, tzinfo=UTC)
    days_between_games: float = 1.0
    players_per_team: int = 5

    def __post_init__(self) -> None:
        if self.n_teams < 2:
            raise ValueError("n_teams must be >= 2")
        if self.games_per_pair < 1:
            raise ValueError("games_per_pair must be >= 1")


def _binomial(rng: random.Random, n: int, p: float) -> int:
    return sum(1 for _ in range(n) if rng.random() < p)


def _split(total: int, weights: list[float]) -> list[int]:
    """Split an integer total across players proportionally to weights.

    Largest-remainder method; the sum of the parts equals the total exactly.
    """
    n = len(weights)
    exact = [total * w for w in weights]
    floors = [int(x) for x in exact]
    remainder = total - sum(floors)
    order = sorted(range(n), key=lambda i: exact[i] - floors[i], reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def generate_league(
    config: SyntheticLeagueConfig | None = None, seed: int | None = None
) -> tuple[list[Team], list[Player], list[Game], list[TeamGameStats], list[PlayerGameStats]]:
    cfg = config or SyntheticLeagueConfig()
    rng = random.Random(cfg.seed if seed is None else seed)

    # ------------------------------------------------------------------ teams
    team_ids = [f"T{i + 1:02d}" for i in range(cfg.n_teams)]
    true_ortg = {
        t: cfg.league_ortg + rng.gauss(0.0, cfg.team_strength_sigma) for t in team_ids
    }
    true_drtg = {
        t: cfg.league_ortg + rng.gauss(0.0, cfg.team_strength_sigma) for t in team_ids
    }
    pace_of = {
        t: min(115.0, max(85.0, cfg.pace_mean + rng.gauss(0.0, cfg.pace_sigma)))
        for t in team_ids
    }
    teams = [
        Team(team_id=t, league=League.NBA, name=f"Synthetic {t}", abbreviation=t)
        for t in team_ids
    ]

    # ---------------------------------------------------------------- players
    players: list[Player] = []
    player_ids: dict[str, list[str]] = {}
    weights = [1.0 / cfg.players_per_team] * cfg.players_per_team
    for t in team_ids:
        ids = [f"{t}-P{i + 1}" for i in range(cfg.players_per_team)]
        player_ids[t] = ids
        players.extend(
            Player(player_id=pid, league=League.NBA, name=f"Player {pid}", current_team_id=t)
            for pid in ids
        )

    # ------------------------------------------------------------------ games
    games: list[Game] = []
    team_stats: list[TeamGameStats] = []
    player_stats: list[PlayerGameStats] = []
    game_index = 0
    for i in range(cfg.n_teams):
        for j in range(i + 1, cfg.n_teams):
            for k in range(cfg.games_per_pair):
                home, away = (team_ids[i], team_ids[j]) if k % 2 == 0 else (team_ids[j], team_ids[i])
                date = cfg.start_date + timedelta(days=cfg.days_between_games * game_index)
                game_id = f"{date:%Y%m%d}_{home}_{away}_{k}"
                game_index += 1

                pace = min(120.0, max(
                    80.0, (pace_of[home] + pace_of[away]) / 2.0 + rng.gauss(0.0, 1.0)
                ))
                possessions = round(pace)

                def side(team_id: str, opp_id: str) -> TeamGameStats:
                    strength_factor = (
                        (true_ortg[team_id] / cfg.league_ortg)
                        * (true_drtg[opp_id] / cfg.league_ortg)
                    )
                    fga = round(possessions * cfg.fga_rate * rng.uniform(0.97, 1.03))
                    fg3a = round(fga * cfg.base_p3_share * rng.uniform(0.95, 1.05))
                    fga2 = fga - fg3a
                    fta = round(possessions * cfg.ft_rate * rng.uniform(0.95, 1.05))
                    p2 = min(0.70, max(0.30, cfg.base_p2_pct * strength_factor * rng.uniform(0.9, 1.1)))
                    p3 = min(0.55, max(0.15, cfg.base_p3_pct * strength_factor * rng.uniform(0.9, 1.1)))
                    ftp = min(0.95, max(0.50, cfg.base_ft_pct * rng.uniform(0.95, 1.05)))
                    fg2 = _binomial(rng, fga2, p2)
                    fg3 = _binomial(rng, fg3a, p3)
                    fg = fg2 + fg3
                    ft = _binomial(rng, fta, ftp)
                    points = 2 * fg2 + 3 * fg3 + ft  # identity by construction
                    tov = round(possessions * cfg.tov_rate * rng.uniform(0.85, 1.15))
                    return TeamGameStats(
                        game_id=game_id, team_id=team_id,
                        is_home=(team_id == home), minutes=240, points=points,
                        possessions=float(possessions), fg=fg, fga=fga,
                        fg3=fg3, fg3a=fg3a, ft=ft, fta=fta,
                        orb=0, drb=0, trb=0,  # rebound balance below
                        ast=round(fg * 0.55 * rng.uniform(0.9, 1.1)),
                        tov=tov,
                        stl=round(tov * 0.45 * rng.uniform(0.8, 1.2)),
                        blk=0,
                        pf=round(rng.uniform(15, 24)),
                    )

                home_stats = side(home, away)
                away_stats = side(away, home)

                # rebound balance: each team's ORB comes out of the opponent's
                # misses, so DRB = opp_misses - opp_ORB (>= 0 by construction)
                home_misses = home_stats.fga - home_stats.fg
                away_misses = away_stats.fga - away_stats.fg
                home_orb = round(home_misses * cfg.orb_pct * rng.uniform(0.8, 1.2))
                away_orb = round(away_misses * cfg.orb_pct * rng.uniform(0.8, 1.2))
                home_drb = max(0, away_misses - away_orb)
                away_drb = max(0, home_misses - home_orb)
                home_orb = min(home_misses, home_orb)
                away_orb = min(away_misses, away_orb)
                home_drb = max(0, away_misses - away_orb)
                away_drb = max(0, home_misses - home_orb)
                home_blk = round(away_misses * 0.08)
                away_blk = round(home_misses * 0.08)

                home_stats = home_stats.model_copy(
                    update={"orb": home_orb, "drb": home_drb, "trb": home_orb + home_drb,
                            "blk": home_blk}
                )
                away_stats = away_stats.model_copy(
                    update={"orb": away_orb, "drb": away_drb, "trb": away_orb + away_drb,
                            "blk": away_blk}
                )

                home_score = home_stats.points
                away_score = away_stats.points
                games.append(Game(
                    game_id=game_id, league=League.NBA, season=cfg.season, game_date=date,
                    home_team_id=home, away_team_id=away,
                    home_score=home_score, away_score=away_score, status=GameStatus.FINAL,
                ))
                team_stats.extend([home_stats, away_stats])

                for team_id, ts in ((home, home_stats), (away, away_stats)):
                    # player lines: makes and misses split with the same
                    # weights so points <= 3*FGA + FTA + 2 holds per player
                    fg2s = _split(ts.fg - ts.fg3, weights)
                    fg3s = _split(ts.fg3, weights)
                    fts = _split(ts.ft, weights)
                    fgas = _split(ts.fga, weights)
                    fg3as = _split(ts.fg3a, weights)
                    ftas = _split(ts.fta, weights)
                    rebs = _split(ts.trb, weights)
                    asts = _split(ts.ast, weights)
                    tovs = _split(ts.tov, weights)
                    stls = _split(ts.stl, weights)
                    blks = _split(ts.blk, weights)
                    fouls = _split(ts.pf, weights)
                    for idx, pid in enumerate(player_ids[team_id]):
                        player_stats.append(PlayerGameStats(
                            game_id=game_id, player_id=pid, team_id=team_id,
                            minutes=48.0,  # synthetic: full-availability bench, 5 x 48
                            points=2 * fg2s[idx] + 3 * fg3s[idx] + fts[idx],
                            fga=fgas[idx], fg3a=fg3as[idx], fta=ftas[idx],
                            rebounds=rebs[idx], assists=asts[idx],
                            turnovers=tovs[idx], steals=stls[idx],
                            blocks=blks[idx], fouls=fouls[idx],
                        ))

    return teams, players, games, team_stats, player_stats
