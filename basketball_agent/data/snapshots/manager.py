"""Point-in-time database snapshots.

A snapshot is a byte-exact copy of the SQLite file plus a JSON manifest
(snapshot id, timestamps, date range, game count, SHA-256 checksum).
Predictions must always cite the snapshot id they were computed from — this
is what makes "same snapshot + same model version + same seed" reproducible.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    created_at: str
    schema_version: int
    game_count: int
    min_game_date: str | None
    max_game_date: str | None
    file_name: str
    sha256: str
    tag: str | None = None

    @classmethod
    def from_manifest(cls, data: dict) -> "SnapshotInfo":
        return cls(
            snapshot_id=data["snapshot_id"],
            created_at=data["created_at"],
            schema_version=data["schema_version"],
            game_count=data["game_count"],
            min_game_date=data.get("min_game_date"),
            max_game_date=data.get("max_game_date"),
            file_name=data["file_name"],
            sha256=data["sha256"],
            tag=data.get("tag"),
        )


class SnapshotManager:
    """Creates and lists snapshots of a source database."""

    def __init__(self, snapshots_dir: Path | str, schema_version: int = 1) -> None:
        self.dir = Path(snapshots_dir)
        self.schema_version = schema_version

    def create(
        self, source_db_path: Path | str, tag: str | None = None, now: datetime | None = None
    ) -> SnapshotInfo:
        source = Path(source_db_path)
        if not source.exists():
            raise FileNotFoundError(f"source database not found: {source}")
        now = now or datetime.now(timezone.utc)
        self.dir.mkdir(parents=True, exist_ok=True)
        snapshot_id = uuid.uuid4().hex[:12]
        file_name = f"snapshot_{now.strftime('%Y%m%dT%H%M%S')}_{snapshot_id}.db"
        target = self.dir / file_name

        src = sqlite3.connect(str(source))
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        game_count, min_date, max_date = self._game_meta(target)
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        info = SnapshotInfo(
            snapshot_id=snapshot_id,
            created_at=now.isoformat(),
            schema_version=self.schema_version,
            game_count=game_count,
            min_game_date=min_date,
            max_game_date=max_date,
            file_name=file_name,
            sha256=sha,
            tag=tag,
        )
        manifest_path = self.dir / f"{file_name}.json"
        manifest_path.write_text(json.dumps(asdict(info), indent=2), encoding="utf-8")
        return info

    @staticmethod
    def _game_meta(db_path: Path) -> tuple[int, str | None, str | None]:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT COUNT(*), MIN(game_date), MAX(game_date) FROM games").fetchone()
        finally:
            conn.close()
        return row[0], row[1], row[2]

    def list_snapshots(self) -> list[SnapshotInfo]:
        infos: list[SnapshotInfo] = []
        for manifest in sorted(self.dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                infos.append(SnapshotInfo.from_manifest(data))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return infos
