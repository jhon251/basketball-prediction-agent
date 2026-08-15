"""Data collectors.

Real data sources arrive with the data-collection phase; the V1 sandbox
includes a synthetic league generator (explicitly labeled as NOT real data)
so the full pipeline can be exercised end-to-end.
"""

from basketball_agent.data.collectors.synthetic import (
    SyntheticLeagueConfig,
    generate_league,
)

__all__ = ["SyntheticLeagueConfig", "generate_league"]
