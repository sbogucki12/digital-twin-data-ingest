import csv
from datetime import date

import pytest

from digital_twin_ingest.manifest import (
    ManifestError,
    ManifestRow,
    load_manifest,
    results_path,
    write_results,
)
from digital_twin_ingest.models import IngestResult

COLS = ["decision", "reason", "kind", "path", "name", "id", "doc_date", "md5", "max_chars_override"]


def _write(tmp_path, rows, cols=COLS):
    p = tmp_path / "m.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    return p


def _row(**kw):
    base = {"decision": "include", "kind": "pdf", "name": "a.pdf", "id": "f1", "doc_date": "2019-01-04"}
    return base | kw


def test_loads_only_included_rows(tmp_path):
    p = _write(tmp_path, [
        _row(id="f1", md5="abc", max_chars_override="350000", path="archive/x"),
        _row(id="f2", decision="exclude"),
        _row(id="f3", decision=" Include ", kind="DOCX"),
    ])
    rows = load_manifest(p)
    assert [r.id for r in rows] == ["f1", "f3"]
    assert rows[0] == ManifestRow(id="f1", name="a.pdf", kind="pdf", path="archive/x",
                                  doc_date=date(2019, 1, 4), md5="abc", max_chars_override=350000)
    assert rows[1].kind == "docx" and rows[1].max_chars_override is None


@pytest.mark.parametrize("bad,match", [
    (_row(kind="xlsx"), "unsupported kind"),
    (_row(doc_date="2019-13-01"), "invalid doc_date"),
    (_row(max_chars_override="lots"), "invalid max_chars_override"),
    (_row(max_chars_override="0"), "must be positive"),
    (_row(id=""), "empty id"),
])
def test_rejects_bad_rows(tmp_path, bad, match):
    with pytest.raises(ManifestError, match=match):
        load_manifest(_write(tmp_path, [bad]))


def test_rejects_duplicate_ids(tmp_path):
    with pytest.raises(ManifestError, match="duplicate"):
        load_manifest(_write(tmp_path, [_row(), _row()]))


def test_rejects_missing_columns(tmp_path):
    p = _write(tmp_path, [{"decision": "include"}], cols=["decision", "name"])
    with pytest.raises(ManifestError, match="missing column"):
        load_manifest(p)


def test_missing_file(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "nope.csv")


def test_results_roundtrip(tmp_path):
    p = tmp_path / "archive_manifest.csv"
    assert results_path(p).name == "archive_manifest_results.csv"
    rows = [ManifestRow("f1", "a.pdf", "pdf", "", date(2020, 1, 1), "", None)]
    write_results(results_path(p), rows, [IngestResult("f1", "dry_run", destination="Category_1",
                                                        cost_usd=0.0012)])
    (out,) = list(csv.DictReader(results_path(p).open(encoding="utf-8")))
    assert out["name"] == "a.pdf" and out["status"] == "dry_run" and out["cost_usd"] == "0.001200"
