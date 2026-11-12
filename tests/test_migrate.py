import io
from datetime import date

import docx
import pytest
import yaml

from digital_twin_ingest.drive import DriveClient
from digital_twin_ingest.manifest import ManifestRow
from digital_twin_ingest.migrate import KIND_MIME, migrate
from digital_twin_ingest.pipeline import SPEND_CAP_REASON
from tests.conftest import RUN_ID
from tests.fakes import FakeAnalyzer

DOCX = KIND_MIME["docx"]


def _docx(text: str) -> bytes:
    d = docx.Document()
    d.add_heading("Heading Here", level=1)
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def archive(svc):
    svc.add_folder("archive", "archive")
    svc.add_folder("sub", "sub", parent="archive")
    return svc


def _row(svc, fid, *, kind="docx", d=date(2019, 1, 4), override=None, md5=None, name=None):
    meta = svc.store[fid]
    return ManifestRow(id=fid, name=name or meta["name"], kind=kind, path="archive/sub",
                       doc_date=d, md5=meta.get("md5Checksum", "") if md5 is None else md5,
                       max_chars_override=override)


def _migrate(settings, svc, analyzer, ledger, rows, **kw):
    return migrate(settings, DriveClient(svc), analyzer, ledger, rows, run_id=RUN_ID, **kw)


def test_dry_run_previews_docx_and_gdoc_without_drive_writes(archive, settings, analyzer, ledger):
    archive.add_file("w1", "Notes.docx", "sub", DOCX, _docx("Body of the note."))
    archive.add_doc("g1", "Manuscript", "archive", "# Manuscript\n\nChapter one.\n")
    rows = [_row(archive, "w1"), _row(archive, "g1", kind="gdoc", d=date(2026, 11, 8))]
    report = _migrate(settings, archive, analyzer, ledger, rows)

    assert [r.status for r in report.results] == ["dry_run", "dry_run"]
    w1 = report.results[0].preview_path.read_text(encoding="utf-8")
    fm = yaml.safe_load(w1.split("---\n")[1])
    assert fm["date"] == date(2019, 1, 4)  # manifest date wins over Drive dates
    assert "# Heading Here" in w1 and "Body of the note." in w1
    assert analyzer.calls == ["w1", "g1"]
    assert archive.files().creates == [] and archive.files().updates == []


def test_live_uploads_and_tags_original_in_place(archive, settings, ledger):
    an = FakeAnalyzer(ledger, RUN_ID, dest="Category_2")
    archive.add_file("w1", "Notes.docx", "sub", DOCX, _docx("Body."))
    (r,) = _migrate(settings, archive, an, ledger, [_row(archive, "w1")], dry_run=False).results

    assert r.status == "ingested" and r.destination == "Category_2"
    out = archive.store[r.output_file_id]
    assert out["parents"] == ["dest1"] and out["appProperties"] == {"dt_source_id": "w1"}
    assert out["name"] == "2019-01-04_topic-notes.md"  # extension stripped from title
    src = archive.store["w1"]
    assert src["parents"] == ["sub"]  # NOT moved
    assert src["name"] == "Notes.docx"
    assert src["appProperties"] == {"dt_ingested": "true", "dt_output_id": r.output_file_id}
    (upd,) = archive.files().updates
    assert upd["add"] is None and upd["remove"] is None
    assert not any(f["name"] == "_Processed" for f in archive.store.values())


def test_already_ingested_is_skipped_and_does_not_use_limit(archive, settings, analyzer, ledger):
    archive.add_file("done", "Old.docx", "sub", DOCX, _docx("x" * 300), app_props={"dt_ingested": "true"})
    archive.add_file("w1", "A.docx", "sub", DOCX, _docx("a" * 300))
    archive.add_file("w2", "B.docx", "sub", DOCX, _docx("b" * 300))
    rows = [_row(archive, f) for f in ("done", "w1", "w2")]
    report = _migrate(settings, archive, analyzer, ledger, rows, limit=1)
    assert [(r.doc_id, r.status) for r in report.results] == [("done", "skipped"), ("w1", "dry_run")]
    assert report.results[0].reason == "already ingested"


def test_changed_trashed_and_retyped_files_are_skipped(archive, settings, analyzer, ledger):
    archive.add_file("w1", "A.docx", "sub", DOCX, _docx("a"))
    archive.add_file("w2", "B.docx", "sub", DOCX, _docx("b"))
    archive.store["w2"]["trashed"] = True
    archive.add_file("w3", "C.pdf", "sub", "application/pdf", b"%PDF")
    rows = [_row(archive, "w1", md5="stale"), _row(archive, "w2"), _row(archive, "w3", kind="docx")]
    reasons = [r.reason for r in _migrate(settings, archive, analyzer, ledger, rows).results]
    assert reasons == ["file content changed since manifest was built", "file is in trash",
                       "file type changed since manifest was built"]
    assert analyzer.calls == []


def test_missing_file_fails_without_stopping(archive, settings, analyzer, ledger):
    archive.add_file("w1", "A.docx", "sub", DOCX, _docx("a" * 50))
    ghost = ManifestRow("gone", "Gone.docx", "docx", "", date(2020, 1, 1), "", None)
    report = _migrate(settings, archive, analyzer, ledger, [ghost, _row(archive, "w1")])
    assert [r.status for r in report.results] == ["failed", "dry_run"]


def test_per_file_override_allows_long_doc(archive, settings, analyzer, ledger):
    settings.file.ingest.max_doc_chars = 100
    long = "word " * 100  # ~500 chars
    archive.add_doc("g1", "Long", "archive", long)
    archive.add_doc("g2", "Long2", "archive", long)
    rows = [_row(archive, "g1", kind="gdoc", override=1000), _row(archive, "g2", kind="gdoc")]
    statuses = [(r.status, r.reason) for r in _migrate(settings, archive, analyzer, ledger, rows).results]
    assert statuses == [("dry_run", None), ("skipped", "exceeds max_doc_chars")]


def test_pdf_extraction_path(archive, settings, analyzer, ledger, monkeypatch):
    import digital_twin_ingest.migrate as m

    monkeypatch.setitem(m.EXTRACTORS, "pdf", lambda data: f"Extracted from {len(data)} bytes\n")
    archive.add_file("p1", "Resume.pdf", "sub", "application/pdf", b"%PDF-fake")
    (r,) = _migrate(settings, archive, analyzer, ledger, [_row(archive, "p1", kind="pdf")]).results
    assert r.status == "dry_run"
    assert r.preview_path.read_text(encoding="utf-8").endswith("Extracted from 9 bytes\n")


def test_unreadable_file_fails_that_file_only(archive, settings, analyzer, ledger):
    archive.add_file("bad", "Bad.docx", "sub", DOCX, b"not a docx")
    archive.add_file("ok", "Ok.docx", "sub", DOCX, _docx("fine"))
    report = _migrate(settings, archive, analyzer, ledger, [_row(archive, "bad"), _row(archive, "ok")])
    assert [r.status for r in report.results] == ["failed", "dry_run"]
    assert analyzer.calls == ["ok"]


def test_spend_cap_stops_migration(archive, settings, ledger):
    settings.file.ingest.max_cost_per_run_usd = 0.05
    an = FakeAnalyzer(ledger, RUN_ID, cost=0.02, worst=0.02)
    for i in range(4):
        archive.add_file(f"w{i}", f"N{i}.docx", "sub", DOCX, _docx(f"note {i}"))
    rows = [_row(archive, f"w{i}") for i in range(4)]
    report = _migrate(settings, archive, an, ledger, rows, dry_run=False)
    assert an.calls == ["w0", "w1"]
    assert report.cap_reached
    assert [r.reason for r in report.results[2:]] == [SPEND_CAP_REASON, SPEND_CAP_REASON]
    assert "appProperties" not in archive.store["w2"]  # untouched, picked up next run


def test_crash_recovery_tags_without_new_call(archive, settings, analyzer, ledger):
    archive.add_file("w1", "A.docx", "sub", DOCX, _docx("a"))
    archive.fail_moves = True  # makes the tagging update fail after upload
    (first,) = _migrate(settings, archive, analyzer, ledger, [_row(archive, "w1")], dry_run=False).results
    assert first.status == "failed"
    archive.fail_moves = False
    (second,) = _migrate(settings, archive, analyzer, ledger, [_row(archive, "w1")], dry_run=False).results
    assert second.status == "ingested" and second.reason == "recovered previous upload"
    assert len(analyzer.calls) == 1
    assert archive.store["w1"]["appProperties"]["dt_ingested"] == "true"
