"""In-memory fakes for the Drive v3 surface and the analyzer. No network."""

from __future__ import annotations

import itertools
import re
from typing import Any

from digital_twin_ingest.analyzer import AnalysisError, AnalysisOutcome, DocAnalysis
from digital_twin_ingest.cost import CostLedger, Pricing

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
_ids = itertools.count(1)


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeFiles:
    def __init__(self, svc: FakeDriveService):
        self.svc = svc
        self.queries: list[str] = []
        self.creates: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    def _match(self, f: dict[str, Any], q: str) -> bool:
        parent = re.search(r"'([^']+)' in parents", q)
        if parent and parent.group(1) not in f.get("parents", []):
            return False
        mime = re.search(r"mimeType='([^']+)'", q)
        if mime and f["mimeType"] != mime.group(1):
            return False
        name = re.search(r"name='((?:[^'\\]|\\.)*)'", q)
        if name and f["name"] != name.group(1).replace("\\'", "'"):
            return False
        prop = re.search(r"appProperties has \{ key='(\w+)' and value='([^']*)' \}", q)
        if prop and (f.get("appProperties") or {}).get(prop.group(1)) != prop.group(2):
            return False
        return not f.get("trashed", False)

    def list(self, q: str, pageToken: str | None = None, **_: Any) -> _Req:
        self.queries.append(q)
        matches = [f for f in self.svc.store.values() if self._match(f, q)]
        start = int(pageToken or 0)
        size = self.svc.page_size
        page = matches[start : start + size]
        nxt = str(start + size) if start + size < len(matches) else None
        return _Req(lambda: {"files": page, **({"nextPageToken": nxt} if nxt else {})})

    def get(self, fileId: str, **_: Any) -> _Req:
        return _Req(lambda: self.svc.store[fileId])

    def export(self, fileId: str, mimeType: str) -> _Req:
        assert mimeType == "text/markdown"

        def run():
            if fileId not in self.svc.exports:
                raise RuntimeError("export failed")
            return self.svc.exports[fileId].encode("utf-8")

        return _Req(run)

    def create(self, body: dict[str, Any], media_body: Any = None, **_: Any) -> _Req:
        def run():
            if self.svc.fail_uploads and media_body is not None:
                raise RuntimeError("upload failed")
            fid = f"new{next(_ids)}"
            f = {"id": fid, **body}
            if media_body is not None:
                f["content"] = media_body.getbytes(0, media_body.size()).decode("utf-8")
            self.svc.store[fid] = f
            self.creates.append(f)
            return {"id": fid}

        return _Req(run)

    def update(self, fileId: str, body: dict[str, Any] | None = None, addParents: str | None = None,
               removeParents: str | None = None, **_: Any) -> _Req:
        def run():
            if self.svc.fail_moves:
                raise RuntimeError("move failed")
            f = self.svc.store[fileId]
            if removeParents:
                f["parents"] = [p for p in f.get("parents", []) if p != removeParents]
            if addParents:
                f.setdefault("parents", []).append(addParents)
            if body and "appProperties" in body:
                f.setdefault("appProperties", {}).update(body["appProperties"])
            self.updates.append({"fileId": fileId, "add": addParents, "remove": removeParents})
            return {"id": fileId}

        return _Req(run)


class FakeDriveService:
    def __init__(self, page_size: int = 2):
        self.store: dict[str, dict[str, Any]] = {}
        self.exports: dict[str, str] = {}
        self.page_size = page_size
        self.fail_uploads = False
        self.fail_moves = False
        self._files = FakeFiles(self)

    def files(self) -> FakeFiles:
        return self._files

    def add_folder(self, fid: str, name: str, parent: str | None = None) -> str:
        self.store[fid] = {"id": fid, "name": name, "mimeType": FOLDER, "parents": [parent] if parent else []}
        return fid

    def add_doc(self, fid: str, name: str, parent: str, body: str, created: str | None = "2026-01-02T03:04:05.000Z",
                modified: str = "2026-02-01T00:00:00.000Z", app_props: dict | None = None) -> str:
        f = {"id": fid, "name": name, "mimeType": DOC, "parents": [parent], "modifiedTime": modified}
        if created:
            f["createdTime"] = created
        if app_props:
            f["appProperties"] = app_props
        self.store[fid] = f
        self.exports[fid] = body
        return fid

    def children(self, parent: str) -> list[dict[str, Any]]:
        return [f for f in self.store.values() if parent in f.get("parents", [])]


# Pricing where 1 input token == $1e-6, so recorded cost == input_tokens / 1e6.
UNIT_PRICING = Pricing(input_per_mtok=1.0, output_per_mtok=0.0)


class FakeAnalyzer:
    """Records a fixed cost per call to the real ledger, like the real Analyzer."""

    def __init__(self, ledger: CostLedger, run_id: str, *, dest: str = "Category_1", cost: float = 0.01,
                 worst: float = 0.02, fail_ids: tuple[str, ...] = ()):
        self.ledger, self.run_id = ledger, run_id
        self.dest, self.cost, self.worst, self.fail_ids = dest, cost, worst, fail_ids
        self.calls: list[str] = []

    def worst_case_cost(self, doc, text) -> float:
        return self.worst

    def analyze(self, doc, text) -> AnalysisOutcome:
        self.calls.append(doc.id)
        c = self.ledger.record(run_id=self.run_id, model="fake", doc_id=doc.id, pricing=UNIT_PRICING,
                               usage={"input_tokens": round(self.cost * 1e6), "output_tokens": 0})
        if doc.id in self.fail_ids:
            raise AnalysisError("model declined to classify this document")
        a = DocAnalysis(topic=f"Topic {doc.name}", tags=["Tag One", "tag-two"], core_stance="A stance.",
                        destination_folder=self.dest)
        return AnalysisOutcome(analysis=a, cost_usd=c.cost_usd)
