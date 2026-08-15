"""End-to-end ingestion pipeline: raw records -> parse -> validate -> load.

Also covers the snapshot manager and cross-run determinism.
"""
from __future__ import annotations

import hashlib

import pytest

from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor, IngestBlockedError
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.snapshots.manager import SnapshotManager

pytestmark = pytest.mark.integration


def raw_from_dataset(ds: Dataset) -> dict:
    return {
        "teams": [t.model_dump(mode="json") for t in ds.teams],
        "players": [p.model_dump(mode="json") for p in ds.players],
        "games": [g.model_dump(mode="json") for g in ds.games],
        "team_stats": [ts.model_dump(mode="json") for ts in ds.team_stats],
        "player_stats": [ps.model_dump(mode="json") for ps in ds.player_stats],
        "availability": [a.model_dump(mode="json") for a in ds.availability],
    }


class TestIngestPipeline:
    def test_valid_dataset_ingests(self, db, sample_dataset, fixed_now):
        result = DataIngestor(db, now=fixed_now).ingest(raw_from_dataset(sample_dataset))
        assert result.success
        repo = Repository(db)
        assert repo.game_count() == 3
        assert repo.get_game("20241106_LAL_BOS") is not None
        assert repo.min_max_game_date()[0] is not None

    def test_parse_error_blocks_ingest(self, db, sample_dataset, fixed_now):
        raw = raw_from_dataset(sample_dataset)
        raw["games"][0]["status"] = "NOT_A_STATUS"
        with pytest.raises(IngestBlockedError) as exc:
            DataIngestor(db, now=fixed_now).ingest(raw)
        assert exc.value.parse_errors
        assert Repository(db).game_count() == 0

    def test_validation_error_blocks_ingest(self, db, sample_dataset, fixed_now):
        raw = raw_from_dataset(sample_dataset)
        raw["team_stats"][0]["points"] += 1  # breaks box score identity
        with pytest.raises(IngestBlockedError) as exc:
            DataIngestor(db, now=fixed_now).ingest(raw)
        assert exc.value.report is not None and exc.value.report.has_errors
        assert Repository(db).game_count() == 0

    def test_warning_only_still_ingests(self, db, sample_dataset, fixed_now):
        # Player minutes just above regulation is a WARNING, not an ERROR.
        raw = raw_from_dataset(sample_dataset)
        raw["player_stats"][0]["minutes"] = 50.0
        result = DataIngestor(db, now=fixed_now).ingest(raw)
        assert result.success
        assert result.report.warnings

    def test_repeated_ingest_is_identical(self, db, sample_dataset, fixed_now, tmp_path):
        db2 = Database(tmp_path / "db2.db")
        db2.initialize()
        DataIngestor(db, now=fixed_now).ingest(raw_from_dataset(sample_dataset))
        DataIngestor(db2, now=fixed_now).ingest(raw_from_dataset(sample_dataset))
        assert Repository(db).list_games() == Repository(db2).list_games()
        assert Repository(db).list_team_stats() == Repository(db2).list_team_stats()


class TestSnapshotManager:
    def test_snapshot_roundtrip(self, db, sample_dataset, fixed_now, tmp_path):
        DataIngestor(db, now=fixed_now).ingest(raw_from_dataset(sample_dataset))
        mgr = SnapshotManager(tmp_path / "snapshots")
        info = mgr.create(db.path, tag="phase1")

        assert info.game_count == 3
        assert info.tag == "phase1"

        # Byte-exact checksum recorded in the manifest matches the file.
        actual_sha = hashlib.sha256(
            (tmp_path / "snapshots" / info.file_name).read_bytes()
        ).hexdigest()
        assert actual_sha == info.sha256

        # The snapshot is a standalone database with identical content.
        snap_db = Database(tmp_path / "snapshots" / info.file_name)
        assert Repository(snap_db).list_games() == Repository(db).list_games()

        # Manifest is listed and re-readable.
        listed = mgr.list_snapshots()
        assert len(listed) == 1
        assert listed[0].snapshot_id == info.snapshot_id

    def test_create_missing_source_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SnapshotManager(tmp_path / "snapshots").create(tmp_path / "missing.db")
