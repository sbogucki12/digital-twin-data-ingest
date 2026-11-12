"""Orchestration: discover -> export + clean -> [spend cap] -> analyze -> render -> upload -> move.

Dry run (default): everything except Drive writes. Claude IS called (so previews show
real classifications); those calls are cost-logged and count toward the spend cap.
Live (--live): uploads the .md and moves the source doc to the processed folder.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .analyzer import AnalysisError, AnalysisOutcome, FatalAnalysisError
from .config import Settings
from .cost import CostLedger
from .drive import DriveClient, DriveError
from .frontmatter import output_filename, render_file
from .markdown import clean_markdown
from .models import IngestResult, RunReport, SourceDoc

log = logging.getLogger(__name__)

SPEND_CAP_REASON = "per-run spend cap reached"


class AnalyzerLike(Protocol):
    def worst_case_cost(self, doc: SourceDoc, text: str) -> float: ...
    def analyze(self, doc: SourceDoc, text: str) -> AnalysisOutcome: ...


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


@dataclass
class _Ctx:
    settings: Settings
    drive: DriveClient
    analyzer: AnalyzerLike
    ledger: CostLedger
    run_id: str
    dry_run: bool
    destinations: dict[str, str]
    processed_id: str | None


def _ingest_one(doc: SourceDoc, ctx: _Ctx) -> IngestResult:
    s = ctx.settings
    raw = ctx.drive.export_markdown(doc.id)
    cleaned = clean_markdown(raw, title=doc.name)
    text, chars = cleaned.text, len(cleaned.text)
    if not text.strip():
        return IngestResult(doc.id, "skipped", reason="empty document")
    if chars > s.ingest.max_doc_chars:
        return IngestResult(doc.id, "skipped", chars=chars, reason="exceeds max_doc_chars")
    if doc.doc_date is None:
        return IngestResult(doc.id, "failed", chars=chars, reason="no created/modified date")

    # Crash recovery: a previous live run uploaded the .md but didn't finish the move.
    if not ctx.dry_run and (existing := ctx.drive.find_output_for_source(doc.id)):
        ctx.drive.mark_processed(doc.id, s.inbox_folder_id, ctx.processed_id, existing)
        return IngestResult(doc.id, "ingested", chars=chars, output_file_id=existing,
                            reason="recovered previous upload")

    # Hard spend cap: refuse any call whose worst-case cost could cross the cap.
    spent = ctx.ledger.run_total(ctx.run_id)
    worst = ctx.analyzer.worst_case_cost(doc, text)
    if spent + worst > s.ingest.max_cost_per_run_usd:
        return IngestResult(doc.id, "skipped", chars=chars, reason=SPEND_CAP_REASON)

    outcome = ctx.analyzer.analyze(doc, text)
    a = outcome.analysis
    content = render_file(a, doc.doc_date, text)
    filename = output_filename(a, doc.doc_date)

    if ctx.dry_run:
        preview_dir = s.ingest.preview_dir / a.destination_folder
        preview_dir.mkdir(parents=True, exist_ok=True)
        path = preview_dir / filename
        path.write_text(content, encoding="utf-8")
        return IngestResult(doc.id, "dry_run", chars=chars, images_removed=cleaned.images_removed,
                            preview_path=path, destination=a.destination_folder,
                            cost_usd=outcome.cost_usd)

    folder_id = ctx.destinations[a.destination_folder]
    name = ctx.drive.unique_name(folder_id, filename)
    out_id = ctx.drive.upload_markdown(folder_id, name, content, source_doc_id=doc.id)
    ctx.drive.mark_processed(doc.id, s.inbox_folder_id, ctx.processed_id, out_id)
    return IngestResult(doc.id, "ingested", chars=chars, images_removed=cleaned.images_removed,
                        destination=a.destination_folder, output_file_id=out_id,
                        cost_usd=outcome.cost_usd)


def ingest_one(doc: SourceDoc, ctx: _Ctx) -> IngestResult:
    """Never raises for per-doc problems; only FatalAnalysisError stops the run."""
    try:
        return _ingest_one(doc, ctx)
    except FatalAnalysisError:
        raise
    except AnalysisError as exc:
        return IngestResult(doc.id, "failed", reason=str(exc))
    except Exception as exc:  # noqa: BLE001 — isolate per-doc failures; never log content
        log.warning("doc %s failed: %s", doc.id, type(exc).__name__)
        return IngestResult(doc.id, "failed", reason=f"{type(exc).__name__}: {exc}"[:300])


def run(
    settings: Settings,
    drive: DriveClient,
    analyzer: AnalyzerLike,
    ledger: CostLedger,
    *,
    run_id: str,
    dry_run: bool = True,
    limit: int | None = None,
    doc_id: str | None = None,
) -> RunReport:
    report = RunReport(run_id=run_id, dry_run=dry_run)

    # Preflight (read-only): fail fast on bad IDs or config/folder mismatches.
    drive.assert_folder(settings.inbox_folder_id, "Inbox")
    drive.assert_folder(settings.root_folder_id, "Digital_Twin root")
    destinations = drive.resolve_destinations(settings.root_folder_id, settings.destinations)
    processed_name = settings.ingest.processed_folder_name
    if dry_run:
        processed_id = drive.find_child_folder(settings.inbox_folder_id, processed_name)
    else:
        processed_id = drive.ensure_folder(settings.inbox_folder_id, processed_name)

    docs = [drive.get_doc(doc_id)] if doc_id else drive.list_inbox(settings.inbox_folder_id)
    if limit is not None:
        docs = docs[:limit]
    log.info("run %s: %d doc(s), %s", run_id, len(docs), "DRY RUN" if dry_run else "LIVE")

    ctx = _Ctx(settings, drive, analyzer, ledger, run_id, dry_run, destinations, processed_id)
    for i, doc in enumerate(docs):
        result = ingest_one(doc, ctx)
        report.results.append(result)
        log.info("doc %s -> %s%s", doc.id, result.status, f" ({result.reason})" if result.reason else "")
        if result.reason == SPEND_CAP_REASON:
            report.cap_reached = True
            report.results.extend(
                IngestResult(d.id, "skipped", reason=SPEND_CAP_REASON) for d in docs[i + 1 :]
            )
            break

    report.spent_usd = ledger.run_total(run_id)
    return report


__all__ = ["DriveError", "FatalAnalysisError", "ingest_one", "new_run_id", "run"]
