"""Data contracts shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class SourceDoc:
    id: str
    name: str
    created_time: datetime | None
    modified_time: datetime | None

    @property
    def date_basis(self) -> datetime | None:
        """Original created date; last-modified only if created is unavailable."""
        return self.created_time or self.modified_time

    @property
    def doc_date(self) -> date | None:
        basis = self.date_basis
        return basis.date() if basis else None


# DocAnalysis lives in analyzer.py; destination_folder is constrained to the configured
# keys by the JSON-schema enum sent to the API and re-checked after parsing.


@dataclass(frozen=True)
class IngestResult:
    doc_id: str
    status: str  # "dry_run" | "ingested" | "skipped" | "failed"
    chars: int = 0
    images_removed: int = 0
    preview_path: Path | None = None
    destination: str | None = None
    output_file_id: str | None = None
    reason: str | None = None
    cost_usd: float = 0.0


@dataclass
class RunReport:
    run_id: str
    dry_run: bool
    results: list[IngestResult] = field(default_factory=list)
    spent_usd: float = 0.0
    cap_reached: bool = False

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out
