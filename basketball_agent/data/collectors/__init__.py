"""Data collectors.

Real NBA data ingestion lands here (CSV collector + raw manifests); the
synthetic league generator remains available as a clearly-labeled test bed.
"""

from basketball_agent.data.collectors.manifest import (
    DataSourceManifest,
    ProcessedDataStore,
    RawDataStore,
    create_manifest,
    sha256_file,
)
from basketball_agent.data.collectors.nba_csv import IngestDataError, load_nba_games_csv
from basketball_agent.data.collectors.nba_teams import (
    NBA_TEAM_IDS,
    TEAM_NAMES,
    canonical_team_id,
)
from basketball_agent.data.collectors.synthetic import (
    SyntheticLeagueConfig,
    generate_league,
)

__all__ = [
    "DataSourceManifest",
    "IngestDataError",
    "NBA_TEAM_IDS",
    "ProcessedDataStore",
    "RawDataStore",
    "SyntheticLeagueConfig",
    "TEAM_NAMES",
    "canonical_team_id",
    "create_manifest",
    "generate_league",
    "load_nba_games_csv",
    "sha256_file",
]
