"""Manifest-driven migration of existing Drive files (Google Docs, .docx, PDF).

Originals are never moved, renamed, or edited: in live mode each one only gets
the hidden `dt_ingested=true` / `dt_output_id` appProperties tag. Dry run makes
no Drive writes at all (downloads and exports are reads).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time

from .config import Settings
from .cost import CostLedger
from .drive import DOC_MIME, INGESTED_KEY, DriveClient
from .extract import EXTRACTORS
from .manifest import ManifestRow
from .markdown import clean_markdown
from .models import IngestResult, RunReport, SourceDoc
from .pipeline import SPEND_CAP_REASON, AnalyzerLike, Ctx, guarded, preflight, process_text

log = logging.getLogger(__name__)

KIND_MIME = {
    "gdoc": DOC_MIME,
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}
_META_FIELDS = "id, name, mimeType, md5Checksum, trashed, appProperties"


def _title(row: ManifestRow) -> str:
    if row.kind != "gdoc" and "." in row.name:
        return row.name.rsplit(".", 1)[0]
    return row.name


def _precheck(row: ManifestRow, drive: DriveClient) -> IngestResult | None:
    """Return a skip/fail result if the file shouldn't be processed, else None."""
    try:
        meta = drive.get_file(row.id, fields=_META_FIELDS)
    except Exception as exc:  # noqa: BLE001
        return IngestResult(row.id, "failed", reason=f"file not reachable ({type(exc).__name__})")
    if meta.get("trashed"):
        return IngestResult(row.id, "skipped", reason="file is in trash")
    if (meta.get("appProperties") or {}).get(INGESTED_KEY) == "true":
        return IngestResult(row.id, "skipped", reason="already ingested")
    if meta.get("mimeType") != KIND_MIME[row.kind]:
        return IngestResult(row.id, "skipped", reason="file type changed since manifest was built")
    if row.md5 and meta.get("md5Checksum") != row.md5:
        return IngestResult(row.id, "skipped", reason="file content changed since manifest was built")
    return None


def _migrate_one(row: ManifestRow, ctx: Ctx) -> IngestResult:
    drive = ctx.drive
    if row.kind == "gdoc":
        raw = drive.export_markdown(row.id)
    else:
        raw = EXTRACTORS[row.kind](drive.download(row.id))
    title = _title(row)
    cleaned = clean_markdown(raw, title=title)
    when = datetime.combine(row.doc_date, time(), tzinfo=UTC)  # manifest date is authoritative
    doc = SourceDoc(id=row.id, name=title, created_time=when, modified_time=None)
    return process_text(
        doc, cleaned.text, ctx,
        max_chars=row.max_chars_override or ctx.settings.ingest.max_doc_chars,
        images_removed=cleaned.images_removed,
        finalize=lambda out_id: drive.tag_ingested(row.id, out_id),
    )


def migrate(
    settings: Settings,
    drive: DriveClient,
    analyzer: AnalyzerLike,
    ledger: CostLedger,
    rows: list[ManifestRow],
    *,
    run_id: str,
    dry_run: bool = True,
    limit: int | None = None,
) -> RunReport:
    """Process manifest rows in order. `limit` counts only files actually processed
    (already-ingested / changed files are skipped without using up the limit)."""
    report = RunReport(run_id=run_id, dry_run=dry_run)
    destinations = preflight(settings, drive)
    ctx = Ctx(settings, drive, analyzer, ledger, run_id, dry_run, destinations)
    log.info("migrate %s: %d manifest row(s), %s", run_id, len(rows), "DRY RUN" if dry_run else "LIVE")

    attempted = 0
    for i, row in enumerate(rows):
        if limit is not None and attempted >= limit:
            break
        result = _precheck(row, drive)
        if result is None:
            attempted += 1
            result = guarded(row.id, lambda row=row: _migrate_one(row, ctx))
        report.results.append(result)
        log.info("file %s -> %s%s", row.id, result.status, f" ({result.reason})" if result.reason else "")
        if result.reason == SPEND_CAP_REASON:
            report.cap_reached = True
            report.results.extend(
                IngestResult(r.id, "skipped", reason=SPEND_CAP_REASON) for r in rows[i + 1 :]
            )
            break

    report.spent_usd = ledger.run_total(run_id)
    return report
