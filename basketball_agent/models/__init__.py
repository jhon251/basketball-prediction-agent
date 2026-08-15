"""Statistical models: Elo, offense/defense, pace, score distribution.

Phase 2 includes the Elo model (Model A) because the feature pipeline uses
it as a team-strength feature.  The remaining models land in Phase 3.
"""

from basketball_agent.models.elo import (
    EloConfig,
    EloResult,
    compute_ratings,
    expected_score,
    margin_multiplier,
    update_ratings,
)

__all__ = [
    "EloConfig",
    "EloResult",
    "compute_ratings",
    "expected_score",
    "margin_multiplier",
    "update_ratings",
]
