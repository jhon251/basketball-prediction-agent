"""Game state for one simulated basketball game (spec §13).

V1 simplifications (documented, per spec §14): fouls, timeouts and referee
decisions are NOT simulated — the corresponding fields exist and stay at
their initial values; player minutes are distributed by the simplified
player-state layer (§17 interface).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GameState:
    home_team_id: str
    away_team_id: str
    quarter: int = 1
    seconds_remaining: float = 720.0
    possession_team: str | None = None
    home_score: int = 0
    away_score: int = 0
    # V1: maintained but not simulated (§14)
    team_fouls: dict[str, int] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)
    # Player layer (§13, §17): minutes are simulated, fouls reserved for V2
    player_minutes: dict[str, float] = field(default_factory=dict)
    player_fouls: dict[str, int] = field(default_factory=dict)
    # Bookkeeping
    team_possessions: dict[str, int] = field(default_factory=dict)
    quarter_scores: list[tuple[int, int]] = field(default_factory=list)
    overtime_quarters: int = 0

    def __post_init__(self) -> None:
        self.team_fouls.setdefault(self.home_team_id, 0)
        self.team_fouls.setdefault(self.away_team_id, 0)
        self.timeouts.setdefault(self.home_team_id, 0)
        self.timeouts.setdefault(self.away_team_id, 0)
        self.team_possessions.setdefault(self.home_team_id, 0)
        self.team_possessions.setdefault(self.away_team_id, 0)
        if self.possession_team is None:
            self.possession_team = self.home_team_id

    @property
    def is_tied(self) -> bool:
        return self.home_score == self.away_score

    def score_of(self, team_id: str) -> int:
        if team_id == self.home_team_id:
            return self.home_score
        if team_id == self.away_team_id:
            return self.away_score
        raise ValueError(f"unknown team {team_id}")

    def add_points(self, team_id: str, points: int) -> None:
        if points <= 0:
            return
        if team_id == self.home_team_id:
            self.home_score += points
        elif team_id == self.away_team_id:
            self.away_score += points
        else:
            raise ValueError(f"unknown team {team_id}")

    def switch_possession(self) -> None:
        if self.possession_team is None:
            return
        self.possession_team = (
            self.away_team_id if self.possession_team == self.home_team_id else self.home_team_id
        )

    def record_quarter(self) -> None:
        self.quarter_scores.append((self.home_score, self.away_score))

    def start_quarter(self, quarter: int, seconds: float, first_possession_team: str) -> None:
        self.quarter = quarter
        self.seconds_remaining = seconds
        self.possession_team = first_possession_team
