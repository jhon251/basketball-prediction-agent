"""Central configuration.

All runtime paths and default simulation parameters are resolved here so that
predictions stay reproducible: same snapshot + same model version + same seed
=> identical output.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parse_optional_int(raw: str) -> int | None:
    return None if raw.strip() == "" else int(raw)


@dataclass(frozen=True)
class Settings:
    """Runtime settings, overridable via environment variables."""

    storage_dir: Path
    db_path: Path
    snapshots_dir: Path
    default_n_simulations: int
    default_seed: int | None
    log_level: str
    decay_lambda: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "Settings":
        storage = Path(environ.get("BASKETBALL_STORAGE_DIR", str(PROJECT_ROOT / "storage")))
        return cls(
            storage_dir=storage,
            db_path=Path(environ.get("BASKETBALL_DB_PATH", str(storage / "basketball.db"))),
            snapshots_dir=Path(environ.get("BASKETBALL_SNAPSHOTS_DIR", str(storage / "snapshots"))),
            default_n_simulations=int(environ.get("BASKETBALL_N_SIMULATIONS", "10000")),
            default_seed=_parse_optional_int(environ.get("BASKETBALL_SEED", "42")),
            log_level=environ.get("BASKETBALL_LOG_LEVEL", "INFO"),
            decay_lambda=float(environ.get("BASKETBALL_DECAY_LAMBDA", "0.0")),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide settings (override env vars before the first call)."""
    return Settings.from_env()
