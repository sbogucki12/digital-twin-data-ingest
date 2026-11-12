"""Migration manifest: a reviewed CSV that lists exactly which Drive files to ingest.

Only rows with decision=include are processed. The manifest is the source of
truth for each file's date and optional per-file length override. Manifests
contain personal file names, so they must stay gitignored.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import ConfigError
from .models import IngestResult

SUPPORTED_KINDS = {"gdoc", "docx", "pdf"}
REQUIRED_COLUMNS = {"decision", "id", "name", "kind", "doc_date"}


class ManifestError(ConfigError):
    pass


@dataclass(frozen=True)
class ManifestRow:
    id: str
    name: str
    kind: str
    path: str
    doc_date: date
    md5: str
    max_chars_override: int | None


def load_manifest(path: Path) -> list[ManifestRow]:
    if not path.exists():
        raise ManifestError(f"Manifest {path} not found")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ManifestError(f"Manifest is missing column(s): {sorted(missing)}")
        rows: list[ManifestRow] = []
        seen: set[str] = set()
        for line_no, r in enumerate(reader, start=2):
            if (r.get("decision") or "").strip().lower() != "include":
                continue
            fid = (r.get("id") or "").strip()
            kind = (r.get("kind") or "").strip().lower()
            if not fid:
                raise ManifestError(f"line {line_no}: empty id")
            if fid in seen:
                raise ManifestError(f"line {line_no}: duplicate id")
            if kind not in SUPPORTED_KINDS:
                raise ManifestError(f"line {line_no}: unsupported kind {kind!r}")
            try:
                doc_date = date.fromisoformat((r.get("doc_date") or "").strip())
            except ValueError as exc:
                raise ManifestError(f"line {line_no}: invalid doc_date") from exc
            override = (r.get("max_chars_override") or "").strip()
            try:
                max_chars = int(override) if override else None
            except ValueError as exc:
                raise ManifestError(f"line {line_no}: invalid max_chars_override") from exc
            if max_chars is not None and max_chars <= 0:
                raise ManifestError(f"line {line_no}: max_chars_override must be positive")
            seen.add(fid)
            rows.append(ManifestRow(
                id=fid, name=(r.get("name") or "").strip(), kind=kind,
                path=(r.get("path") or "").strip(), doc_date=doc_date,
                md5=(r.get("md5") or "").strip(), max_chars_override=max_chars,
            ))
    return rows


RESULT_COLUMNS = ["id", "name", "status", "reason", "destination", "output_file_id",
                  "cost_usd", "preview_path"]


def results_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(f"{manifest_path.stem}_results.csv")


def write_results(path: Path, rows: list[ManifestRow], results: list[IngestResult]) -> None:
    names = {r.id: r.name for r in rows}
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=RESULT_COLUMNS)
        w.writeheader()
        for res in results:
            w.writerow({
                "id": res.doc_id, "name": names.get(res.doc_id, ""), "status": res.status,
                "reason": res.reason or "", "destination": res.destination or "",
                "output_file_id": res.output_file_id or "", "cost_usd": f"{res.cost_usd:.6f}",
                "preview_path": str(res.preview_path or ""),
            })
