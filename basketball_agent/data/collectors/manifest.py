"""Raw data provenance: source manifests (spec §3).

Raw files are never modified: ingestion copies the original file verbatim
into ``raw/<source>/`` and records a manifest with the source name, retrieval
time, optional source version and the file's SHA-256 — every processed
dataset and every prediction can be traced back to it.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

STATUS_AVAILABLE = "AVAILABLE"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class DataSourceManifest:
    source: str
    status: str
    file_name: str
    file_hash: str
    retrieved_at: str
    source_version: str | None = None
    description: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "DataSourceManifest":
        return cls(
            source=data["source"],
            status=data.get("status", STATUS_AVAILABLE),
            file_name=data["file_name"],
            file_hash=data["file_hash"],
            retrieved_at=data["retrieved_at"],
            source_version=data.get("source_version"),
            description=data.get("description"),
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_manifest(
    source: str,
    file_path: Path,
    *,
    status: str = STATUS_AVAILABLE,
    source_version: str | None = None,
    description: str | None = None,
    retrieved_at: datetime | None = None,
) -> DataSourceManifest:
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    return DataSourceManifest(
        source=source,
        status=status,
        file_name=file_path.name,
        file_hash=sha256_file(file_path),
        retrieved_at=retrieved_at.isoformat(),
        source_version=source_version,
        description=description,
    )


class RawDataStore:
    """Stores raw data files verbatim under ``raw_dir/<source>/``."""

    def __init__(self, raw_dir: Path | str) -> None:
        self.dir = Path(raw_dir)

    def register(
        self,
        source: str,
        file_path: Path | str,
        *,
        status: str = STATUS_AVAILABLE,
        source_version: str | None = None,
        description: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> DataSourceManifest:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"raw file not found: {file_path}")
        target_dir = self.dir / source
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / file_path.name
        shutil.copyfile(file_path, target)
        manifest = create_manifest(
            source, target, status=status, source_version=source_version,
            description=description, retrieved_at=retrieved_at,
        )
        (target_dir / f"{target.name}.manifest.json").write_text(
            json.dumps(asdict(manifest), indent=2), encoding="utf-8"
        )
        return manifest

    def list_sources(self) -> list[DataSourceManifest]:
        manifests: list[DataSourceManifest] = []
        if not self.dir.exists():
            return manifests
        for path in sorted(self.dir.glob("*/*.manifest.json")):
            try:
                manifests.append(DataSourceManifest.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                ))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return manifests
