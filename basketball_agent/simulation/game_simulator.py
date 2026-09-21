"""One full possession-level game simulation.

Flow (spec §12): Game Start -> possessions loop (offensive decision ->
event -> outcome -> state update -> next possession) -> end of regulation ->
overtime if tied.

Time model (V1): each possession consumes ``2880 / expected_pace`` seconds;
continuation opportunities (offensive rebounds) are free.  The simulated
possession count therefore tracks the expected pace by construction.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from basketball_agent.models.game_params import GamePredictionParams, TeamGameParams
from basketball_agent.models.score_model import ScoreModelConfig, rescale_to_ppp
from basketball_agent.simulation.game_state import GameState
from basketball_agent.simulation.player_state import PlayerProfile, team_efficiency_multiplier
from basketball_agent.simulation.possession import resolve_opportunity

REGULATION_SECONDS = 2880.0  # 4 x 12 minutes


@dataclass(frozen=True)
class SimulationConfig:
    quarter_seconds: float = 720.0
    overtime_seconds: float = 300.0
    max_overtimes: int = 5


@dataclass(frozen=True)
class SimulatedGame:
    home_team_id: str
    away_team_id: str
    home_score: int
    away_score: int
    margin: int
    total: int
    winner: str  # "home" | "away" | "tie"
    quarters: tuple[tuple[int, int], ...]
    overtime_quarters: int
    home_possessions: int
    away_possessions: int
    player_minutes: dict[str, float]


class GameSimulator:
    """Simulates one game from ``GamePredictionParams``.

    Pure with respect to the provided ``rng``: the same seed yields the same
    game.  Optional rosters apply the player-layer efficiency/availability
    adjustments before the game starts.
    """

    def __init__(self, config: SimulationConfig | None = None) -> None:
        self._config = config or SimulationConfig()

    def simulate(
        self,
        params: GamePredictionParams,
        rng: random.Random,
        home_roster: list[PlayerProfile] | None = None,
        away_roster: list[PlayerProfile] | None = None,
    ) -> SimulatedGame:
        home_tp = self._adjusted(params.home, home_roster)
        away_tp = self._adjusted(params.away, away_roster)
        params_by_team = {home_tp.team_id: home_tp, away_tp.team_id: away_tp}
        rosters = {
            home_tp.team_id: home_roster or [],
            away_tp.team_id: away_roster or [],
        }
        # League-aware game length (Phase 8-B3): NBA keeps 2880 s / 720 s
        # quarters; a 40-minute league carries regulation_seconds=2400.0 in
        # its params and gets 600 s quarters.  When the params disagree with
        # the configured quarter length, an effective config is derived for
        # this game only — explicit custom configs still win for NBA games.
        regulation = getattr(params, "regulation_seconds", REGULATION_SECONDS)
        cfg = self._config
        if abs(regulation - REGULATION_SECONDS) > 1e-9:
            cfg = SimulationConfig(
                quarter_seconds=regulation / 4.0,
                overtime_seconds=cfg.overtime_seconds,
                max_overtimes=cfg.max_overtimes,
            )
        seconds_per_possession = regulation / (2.0 * params.expected_pace)

        state = GameState(
            home_team_id=home_tp.team_id,
            away_team_id=away_tp.team_id,
            seconds_remaining=cfg.quarter_seconds,
        )

        while True:
            if state.seconds_remaining <= 0:
                state.record_quarter()
                if self._advance_quarter(state, home_tp.team_id, away_tp.team_id, cfg):
                    break
                continue
            self._run_possession(state, params_by_team, seconds_per_possession, rng, rosters)
            state.switch_possession()

        winner = (
            "home" if state.home_score > state.away_score
            else "away" if state.away_score > state.home_score
            else "tie"
        )
        return SimulatedGame(
            home_team_id=state.home_team_id,
            away_team_id=state.away_team_id,
            home_score=state.home_score,
            away_score=state.away_score,
            margin=state.home_score - state.away_score,
            total=state.home_score + state.away_score,
            winner=winner,
            quarters=tuple(state.quarter_scores),
            overtime_quarters=state.overtime_quarters,
            home_possessions=state.team_possessions[state.home_team_id],
            away_possessions=state.team_possessions[state.away_team_id],
            player_minutes=dict(state.player_minutes),
        )

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _adjusted(team: TeamGameParams, roster: list[PlayerProfile] | None) -> TeamGameParams:
        if not roster:
            return team
        multiplier = team_efficiency_multiplier(roster)
        ppp = team.expected_ppp * multiplier
        return TeamGameParams(
            team_id=team.team_id,
            expected_ppp=ppp,
            expected_possessions=team.expected_possessions,
            possession=rescale_to_ppp(team.possession, ppp, ScoreModelConfig()),
        )

    def _advance_quarter(
        self, state: GameState, home_id: str, away_id: str, cfg: SimulationConfig
    ) -> bool:
        """Advance the clock after a finished quarter; True when the game is over."""
        if state.quarter < 4:
            state.start_quarter(
                state.quarter + 1,
                cfg.quarter_seconds,
                self._first_possession_team(state.quarter + 1, home_id, away_id),
            )
            return False
        if state.quarter == 4:
            if state.is_tied and state.overtime_quarters < cfg.max_overtimes:
                state.overtime_quarters += 1
                state.start_quarter(
                    5, cfg.overtime_seconds, self._first_possession_team(5, home_id, away_id)
                )
                return False
            return True
        # overtime
        if state.is_tied and state.overtime_quarters < cfg.max_overtimes:
            state.overtime_quarters += 1
            state.start_quarter(
                state.quarter + 1,
                cfg.overtime_seconds,
                self._first_possession_team(state.quarter + 1, home_id, away_id),
            )
            return False
        return True

    @staticmethod
    def _first_possession_team(quarter: int, home_id: str, away_id: str) -> str:
        # deterministic alternation: odd quarters start with the home team
        return home_id if quarter % 2 == 1 else away_id

    def _run_possession(
        self,
        state: GameState,
        params_by_team: dict[str, TeamGameParams],
        seconds_per_possession: float,
        rng: random.Random,
        rosters: dict[str, list[PlayerProfile]],
    ) -> None:
        team = state.possession_team
        tp = params_by_team[team]
        state.seconds_remaining = max(0.0, state.seconds_remaining - seconds_per_possession)
        # snap float dust (e.g. 25 x 28.8 slightly under 720) to an exact end
        # of quarter so an extra phantom possession is never simulated.
        if state.seconds_remaining < 1e-9:
            state.seconds_remaining = 0.0
        state.team_possessions[team] += 1
        # both teams are on the floor for the whole game clock
        for team_id, roster in rosters.items():
            self._track_minutes(state, team_id, seconds_per_possession, roster)
        while True:
            result = resolve_opportunity(rng, tp.possession)
            if result.points:
                state.add_points(team, result.points)
            if result.possession_over:
                return
            # OFFENSIVE_REBOUND: same possession continues with a new opportunity

    @staticmethod
    def _track_minutes(
        state: GameState,
        team: str,
        seconds: float,
        roster: list[PlayerProfile],
    ) -> None:
        active = [p for p in roster if p.available and p.team_id == team]
        total_share = sum(p.minutes_share for p in active)
        if total_share <= 0:
            return
        minutes = seconds / 60.0
        for p in active:
            state.player_minutes[p.player_id] = (
                state.player_minutes.get(p.player_id, 0.0) + minutes * p.minutes_share / total_share
            )
