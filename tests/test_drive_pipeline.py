from datetime import date

import pytest
import yaml

from digital_twin_ingest.drive import DriveClient, DriveError, _q
from digital_twin_ingest.pipeline import SPEND_CAP_REASON, run
from tests.conftest import RUN_ID
from tests.fakes import FakeAnalyzer


def _run(settings, svc, analyzer, ledger, **kw):
    return run(settings, DriveClient(svc), analyzer, ledger, run_id=RUN_ID, **kw)


# ---- Drive read side ---------------------------------------------------------

def test_list_inbox_paginates_filters_and_sorts(svc):
    svc.add_doc("b", "B", "inbox", "x", created="2026-03-01T00:00:00Z")
    svc.add_doc("a", "A", "inbox", "x", created="2026-01-01T00:00:00Z")
    svc.add_doc("c", "C", "inbox", "x", created="2026-02-01T00:00:00Z")
    svc.add_doc("done", "D", "inbox", "x", app_props={"dt_ingested": "true"})
    svc.add_doc("elsewhere", "E", "root", "x")
    assert [d.id for d in DriveClient(svc).list_inbox("inbox")] == ["a", "c", "b"]


def test_doc_date_falls_back_to_modified(svc):
    svc.add_doc("n", "N", "inbox", "x", created=None, modified="2026-05-06T07:08:09Z")
    (doc,) = DriveClient(svc).list_inbox("inbox")
    assert doc.doc_date == date(2026, 5, 6)


def test_resolve_destinations_by_name(svc, settings):
    resolved = DriveClient(svc).resolve_destinations("root", settings.destinations)
    assert resolved == {f"Category_{i + 1}": f"dest{i}" for i in range(5)}


def test_resolve_destinations_reports_missing(svc, settings):
    del svc.store["dest4"]
    with pytest.raises(DriveError, match="Category_5"):
        DriveClient(svc).resolve_destinations("root", settings.destinations)


def test_query_escaping():
    assert _q("it's") == "it\\'s"


def test_unique_name(svc):
    svc.add_doc("x1", "2026-01-02_topic.md", "dest0", "")
    svc.add_doc("x2", "2026-01-02_topic-2.md", "dest0", "")
    assert DriveClient(svc).unique_name("dest0", "2026-01-02_topic.md") == "2026-01-02_topic-3.md"
    assert DriveClient(svc).unique_name("dest1", "2026-01-02_topic.md") == "2026-01-02_topic.md"


# ---- Dry run -------------------------------------------------------------------

def test_dry_run_previews_with_frontmatter_and_no_drive_writes(svc, settings, analyzer, ledger):
    svc.add_doc("d1", "First Note", "inbox", "# First Note\n\nHello\\-world.\n")
    report = _run(settings, svc, analyzer, ledger)

    (r,) = report.results
    assert r.status == "dry_run" and r.destination == "Category_1"
    text = r.preview_path.read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---\n")[1])
    assert fm == {"topic": "Topic First Note", "tags": ["tag-one", "tag-two"],
                  "core_stance": "A stance.", "date": date(2026, 1, 2)}
    assert text.endswith("---\n\nHello-world.\n")
    assert r.preview_path.parent.name == "Category_1"
    assert svc.files().creates == [] and svc.files().updates == []
    assert report.spent_usd == pytest.approx(0.01)


def test_per_doc_failures_are_isolated(svc, settings, ledger):
    an = FakeAnalyzer(ledger, RUN_ID, fail_ids=("refused",))
    svc.add_doc("ok", "Ok", "inbox", "body")
    svc.add_doc("broken", "Broken", "inbox", "x")
    del svc.exports["broken"]
    svc.add_doc("empty", "Empty", "inbox", "\n\n")
    svc.add_doc("refused", "Refused", "inbox", "body")
    by_id = {r.doc_id: r for r in _run(settings, svc, an, ledger).results}
    assert by_id["ok"].status == "dry_run"
    assert by_id["broken"].status == "failed"
    assert by_id["empty"].status == "skipped"
    assert by_id["refused"].status == "failed" and "declined" in by_id["refused"].reason
    assert ledger.run_total(RUN_ID) == pytest.approx(0.02)  # refused call still logged


def test_oversized_doc_skipped_without_api_call(svc, settings, analyzer, ledger):
    settings.file.ingest.max_doc_chars = 10
    svc.add_doc("big", "Big", "inbox", "x" * 50)
    (r,) = _run(settings, svc, analyzer, ledger).results
    assert r.status == "skipped" and r.reason == "exceeds max_doc_chars"
    assert analyzer.calls == []


# ---- Spend cap -----------------------------------------------------------------

def test_spend_cap_stops_run_before_crossing(svc, settings, ledger):
    settings.file.ingest.max_cost_per_run_usd = 0.05
    an = FakeAnalyzer(ledger, RUN_ID, cost=0.02, worst=0.02)
    for i in range(5):
        svc.add_doc(f"d{i}", f"N{i}", "inbox", "body", created=f"2026-01-0{i + 1}T00:00:00Z")
    report = _run(settings, svc, an, ledger, dry_run=False)

    assert an.calls == ["d0", "d1"]  # third call could reach 0.06 > 0.05
    assert report.cap_reached
    assert [r.status for r in report.results] == ["ingested", "ingested", "skipped", "skipped", "skipped"]
    assert all(r.reason == SPEND_CAP_REASON for r in report.results[2:])
    assert report.spent_usd <= 0.05
    assert {f["id"] for f in svc.children("inbox") if f["mimeType"] != "application/vnd.google-apps.folder"} \
        == {"d2", "d3", "d4"}


def test_cap_is_hard_limited_to_one_dollar():
    from pydantic import ValidationError

    from digital_twin_ingest.config import IngestSettings

    with pytest.raises(ValidationError):
        IngestSettings(max_cost_per_run_usd=1.01)
    assert IngestSettings().max_cost_per_run_usd == 1.00


# ---- Live mode -----------------------------------------------------------------

def test_live_uploads_and_moves(svc, settings, ledger):
    an = FakeAnalyzer(ledger, RUN_ID, dest="Category_3")
    svc.add_doc("d1", "Note", "inbox", "Body text\n")
    report = _run(settings, svc, an, ledger, dry_run=False)

    (r,) = report.results
    assert r.status == "ingested" and r.destination == "Category_3"
    uploaded = svc.store[r.output_file_id]
    assert uploaded["parents"] == ["dest2"]
    assert uploaded["name"] == "2026-01-02_topic-note.md"
    assert uploaded["mimeType"] == "text/markdown"
    assert uploaded["appProperties"] == {"dt_source_id": "d1"}
    assert uploaded["content"].startswith('---\ntopic: "Topic Note"\ntags: ["tag-one", "tag-two"]\n')

    processed = next(f for f in svc.store.values() if f["name"] == "_Processed")
    assert processed["parents"] == ["inbox"]
    src = svc.store["d1"]
    assert src["parents"] == [processed["id"]]
    assert src["appProperties"] == {"dt_ingested": "true", "dt_output_id": r.output_file_id}
    assert DriveClient(svc).list_inbox("inbox") == []


def test_live_upload_failure_leaves_doc_in_inbox(svc, settings, analyzer, ledger):
    svc.fail_uploads = True
    svc.add_doc("d1", "Note", "inbox", "Body")
    (r,) = _run(settings, svc, analyzer, ledger, dry_run=False).results
    assert r.status == "failed"
    assert svc.store["d1"]["parents"] == ["inbox"]


def test_crash_recovery_reuses_prior_upload_without_new_api_call(svc, settings, analyzer, ledger):
    svc.add_doc("d1", "Note", "inbox", "Body")
    svc.fail_moves = True
    (first,) = _run(settings, svc, analyzer, ledger, dry_run=False).results
    assert first.status == "failed" and len(analyzer.calls) == 1

    svc.fail_moves = False
    (second,) = _run(settings, svc, analyzer, ledger, dry_run=False).results
    assert second.status == "ingested" and second.reason == "recovered previous upload"
    assert len(analyzer.calls) == 1  # no second Claude call, no duplicate upload
    assert len([f for f in svc.store.values() if f.get("mimeType") == "text/markdown"]) == 1


def test_limit_and_single_doc(svc, settings, analyzer, ledger):
    for i in range(3):
        svc.add_doc(f"d{i}", f"N{i}", "inbox", "body")
    assert len(_run(settings, svc, analyzer, ledger, limit=2).results) == 2
    (r,) = _run(settings, svc, analyzer, ledger, doc_id="d1").results
    assert r.doc_id == "d1"


def test_bad_inbox_id_fails_preflight(svc, settings, analyzer, ledger):
    svc.add_doc("notafolder", "X", "root", "x")
    settings.inbox_folder_id = "notafolder"
    with pytest.raises(DriveError, match="Inbox"):
        _run(settings, svc, analyzer, ledger)
