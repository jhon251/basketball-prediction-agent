"""Possession-level simulation engine + Monte Carlo (Phase 4)."""

from basketball_agent.simulation.game_simulator import (
    GameSimulator,
    SimulatedGame,
    SimulationConfig,
)
from basketball_agent.simulation.monte_carlo import (
    DistributionStats,
    MonteCarloResult,
    MonteCarloRunner,
    ScoreBucket,
    SimulationRun,
    compute_stats,
)
from basketball_agent.simulation.player_state import (
    PlayerProfile,
    team_efficiency_multiplier,
)
from basketball_agent.simulation.possession import (
    PossessionEvent,
    PossessionResult,
    resolve_opportunity,
)

__all__ = [
    "DistributionStats",
    "GameSimulator",
    "MonteCarloResult",
    "MonteCarloRunner",
    "PlayerProfile",
    "PossessionEvent",
    "PossessionResult",
    "ScoreBucket",
    "SimulatedGame",
    "SimulationConfig",
    "SimulationRun",
    "compute_stats",
    "resolve_opportunity",
    "team_efficiency_multiplier",
]
