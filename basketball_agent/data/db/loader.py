"""Guarded ingestion pipeline: raw records -> parse -> validate -> database.

The pipeline refuses to write ANY record when parsing or validation fails —
an ERROR is a FAIL, never a warning (§2.2 of the spec).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from basketball_agent.agents.validation_agent import DataValidationAgent
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.data.validators.checks import ValidationReport
from basketball_agent.data.validators.parsing import ParseError, parse_dataset


class IngestBlockedError(RuntimeError):
    """Ingestion refused: parsing or validation failed."""

    def __init__(
        self,
        message: str,
        parse_errors: tuple[ParseError, ...] = (),
        report: ValidationReport | None = None,
    ) -> None:
        super().__init__(message)
        self.parse_errors = parse_errors
        self.report = report


@dataclass(frozen=True)
class IngestResult:
    parsed: Dataset
    parse_errors: tuple[ParseError, ...]
    report: ValidationReport
    inserted: int

    @property
    def success(self) -> bool:
        return not self.parse_errors and not self.report.has_errors


class DataIngestor:
    """Parse -> validate -> load, with the validation gate enforced."""

    def __init__(self, db: Database, now: datetime | None = None) -> None:
        self._db = db
        self._now = now or datetime.now(timezone.utc)
        self._validator = DataValidationAgent(now=self._now)
        self._repo = Repository(db)

    def ingest(self, raw: dict) -> IngestResult:
        dataset, parse_errors = parse_dataset(raw)
        report = self._validator.validate(dataset)
        if parse_errors:
            raise IngestBlockedError(
                f"parsing failed for {len(parse_errors)} record(s) — ingestion blocked",
                parse_errors=tuple(parse_errors),
                report=report,
            )
        if report.has_errors:
            raise IngestBlockedError(
                f"validation FAILED with {len(report.errors)} error(s) — ingestion blocked",
                parse_errors=(),
                report=report,
            )
        inserted = self._repo.load_dataset(dataset, inserted_at=self._now)
        return IngestResult(
            parsed=dataset, parse_errors=(), report=report, inserted=inserted
        )
