"""Feature pipeline shared types and errors."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class FeatureComputationError(RuntimeError):
    """Raised when features cannot be computed (missing data, bad timestamp)."""


@dataclass(frozen=True)
class TeamFeatureSet:
    """All team-level features as of one timestamp.

    ``None`` means "not computable from the available history"; the pipeline
    raises instead of silently producing team features with no history at all.
    """

    team_id: str
    games_played: int
    last_game_date: datetime | None
    # Team strength
    elo: float | None = None
    ortg: float | None = None
    drtg: float | None = None
    nrtg: float | None = None
    # Pace
    pace: float | None = None
    # Shooting
    efg_pct: float | None = None
    ts_pct: float | None = None
    fg3_pct: float | None = None
    ft_pct: float | None = None
    # Rebounding
    orb_pct: float | None = None
    drb_pct: float | None = None
    trb_pct: float | None = None
    # Turnovers
    tov_pct: float | None = None
    opp_tov_pct: float | None = None
    # Per-possession outcome rates (feed the score model / simulation)
    fga_rate: float | None = None
    fta_rate: float | None = None
    p3a_share: float | None = None
    p2_pct: float | None = None
    # Recent form
    form_last5_win_pct: float | None = None
    form_last5_avg_margin: float | None = None
    form_last10_win_pct: float | None = None
    form_last10_avg_margin: float | None = None
    season_win_pct: float | None = None
    season_avg_margin: float | None = None


@dataclass(frozen=True)
class LeagueFeatureSet:
    """League-average context computed from the same eligible history.

    Every model's matchup adjustment is expressed relative to these
    averages, so the whole Phase-3 layer stays pure over ``GameFeatures``.
    """

    ortg: float | None = None
    drtg: float | None = None
    nrtg: float | None = None
    pace: float | None = None
    efg_pct: float | None = None
    ts_pct: float | None = None
    fg3_pct: float | None = None
    ft_pct: float | None = None
    orb_pct: float | None = None
    drb_pct: float | None = None
    trb_pct: float | None = None
    tov_pct: float | None = None
    opp_tov_pct: float | None = None
    fga_rate: float | None = None
    fta_rate: float | None = None
    p3a_share: float | None = None
    p2_pct: float | None = None
    n_team_games: int = 0


@dataclass(frozen=True)
class FeatureProvenance:
    """Audit trail of everything that went into one feature computation.

    Every element here is checkable by the leakage-detection system: no game
    in ``games_used`` may be dated at/after ``as_of``, and every availability
    record must have ``source_time <= as_of``.
    """

    games_used: tuple[str, ...]
    team_stats_used: int
    availability_used: tuple[tuple[str, str], ...]  # (player_id, source_time_iso)
    decay_lambda: float
    elo_k: float
    elo_home_advantage: float
    elo_initial: float


@dataclass(frozen=True)
class GameFeatures:
    """All features for one upcoming game, computed strictly as-of."""

    game_id: str
    as_of: datetime
    home_team_id: str
    away_team_id: str
    home: TeamFeatureSet
    away: TeamFeatureSet
    league: LeagueFeatureSet
    # Pace
    home_pace: float | None
    away_pace: float | None
    expected_pace: float | None
    # Strength deltas
    elo_win_prob_home: float | None
    nrtg_diff: float | None
    # Schedule
    home_rest_days: float | None
    away_rest_days: float | None
    home_back_to_back: bool | None
    away_back_to_back: bool | None
    # League context
    league_home_win_pct: float | None
    # Availability
    home_available_share: float
    away_available_share: float
    # Audit
    provenance: FeatureProvenance
    # League identity of the target game (Phase 8-B3): drives per-league
    # parameters (e.g. 40-minute regulation) — history is already league-scoped
    game_league: str | None = None
    # Season of the target game (Phase 8-H): drives per-season regulation
    # overrides (CBA 2024-25 was 4 x 12; 2025-26+ is FIBA 4 x 10)
    game_season: str | None = None
