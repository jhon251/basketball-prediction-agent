"""Unit tests for raw data manifests (spec §3)."""
from __future__ import annotations

import hashlib

import pytest

from basketball_agent.data.collectors.manifest import (
    DataSourceManifest,
    RawDataStore,
    sha256_file,
)

pytestmark = pytest.mark.unit


class TestManifest:
    def test_sha256_matches_stdlib(self, tmp_path):
        f = tmp_path / "raw.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        expected = hashlib.sha256(f.read_bytes()).hexdigest()
        assert sha256_file(f) == expected

    def test_register_copies_verbatim_and_writes_manifest(self, tmp_path):
        source_file = tmp_path / "nba_games.csv"
        source_file.write_text("game_date,home_team\n2024-11-01,LAL\n", encoding="utf-8")
        store = RawDataStore(tmp_path / "raw")
        manifest = store.register("nba_games", source_file, source_version="v1", description="real data")

        copied = tmp_path / "raw" / "nba_games" / "nba_games.csv"
        assert copied.read_text(encoding="utf-8") == source_file.read_text(encoding="utf-8")
        assert manifest.status == "AVAILABLE"
        assert manifest.source_version == "v1"
        assert manifest.file_hash == sha256_file(copied)

        listed = store.list_sources()
        assert len(listed) == 1
        assert listed[0] == manifest
        assert isinstance(listed[0], DataSourceManifest)

    def test_register_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            RawDataStore(tmp_path / "raw").register("x", tmp_path / "missing.csv")
