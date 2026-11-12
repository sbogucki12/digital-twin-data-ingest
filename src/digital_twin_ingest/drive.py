"""Google Drive API v3 access: Inbox discovery, Markdown export, folder resolution,
and (live mode only) upload + move-to-processed.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from .models import SourceDoc

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
MARKDOWN_MIME = "text/markdown"
INGESTED_KEY = "dt_ingested"   # on the source doc once processed
OUTPUT_KEY = "dt_output_id"    # on the source doc: ID of the generated .md
SOURCE_KEY = "dt_source_id"    # on the generated .md: ID of its source doc
_DOC_FIELDS = "id, name, mimeType, createdTime, modifiedTime, appProperties"


class DriveError(RuntimeError):
    pass


def build_service(credentials: Any) -> Any:
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _q(value: str) -> str:
    """Escape a value for a Drive query string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _to_doc(f: dict[str, Any]) -> SourceDoc:
    return SourceDoc(
        id=f["id"],
        name=f.get("name", ""),
        created_time=_ts(f.get("createdTime")),
        modified_time=_ts(f.get("modifiedTime")),
    )


class DriveClient:
    def __init__(self, service: Any):
        self._svc = service

    def _list(self, q: str, fields: str) -> Iterator[dict[str, Any]]:
        token = None
        while True:
            resp = (
                self._svc.files()
                .list(
                    q=q,
                    fields=f"nextPageToken, files({fields})",
                    pageSize=100,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            yield from resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                return

    def get_file(self, file_id: str, fields: str = _DOC_FIELDS) -> dict[str, Any]:
        return self._svc.files().get(fileId=file_id, fields=fields, supportsAllDrives=True).execute()

    def assert_folder(self, folder_id: str, label: str) -> None:
        meta = self.get_file(folder_id, fields="id, mimeType, trashed")
        if meta.get("mimeType") != FOLDER_MIME or meta.get("trashed"):
            raise DriveError(f"{label} ({folder_id}) is not an active Drive folder")

    def list_inbox(self, inbox_id: str) -> list[SourceDoc]:
        """Google Docs directly in the Inbox that have not been ingested, oldest first."""
        q = f"'{_q(inbox_id)}' in parents and mimeType='{DOC_MIME}' and trashed=false"
        docs = [
            _to_doc(f)
            for f in self._list(q, _DOC_FIELDS)
            if (f.get("appProperties") or {}).get(INGESTED_KEY) != "true"
        ]
        return sorted(docs, key=lambda d: d.date_basis or datetime.min)

    def get_doc(self, doc_id: str) -> SourceDoc:
        meta = self.get_file(doc_id)
        if meta.get("mimeType") != DOC_MIME:
            raise DriveError(f"{doc_id} is not a Google Doc")
        return _to_doc(meta)

    def export_markdown(self, doc_id: str) -> str:
        """Google's native Docs -> Markdown export (10 MB export limit applies)."""
        data = self._svc.files().export(fileId=doc_id, mimeType=MARKDOWN_MIME).execute()
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)

    def find_child_folder(self, parent_id: str, name: str) -> str | None:
        q = (
            f"'{_q(parent_id)}' in parents and name='{_q(name)}'"
            f" and mimeType='{FOLDER_MIME}' and trashed=false"
        )
        matches = list(self._list(q, "id, name"))
        if len(matches) > 1:
            raise DriveError(f"Multiple folders named {name!r} in parent {parent_id}")
        return matches[0]["id"] if matches else None

    def resolve_destinations(self, root_id: str, destinations: list[Any]) -> dict[str, str]:
        """Map each destination key to a folder ID (configured ID, else lookup by name)."""
        resolved: dict[str, str] = {}
        missing: list[str] = []
        for dest in destinations:
            if dest.drive_folder_id:
                self.assert_folder(dest.drive_folder_id, f"destination {dest.key!r}")
                resolved[dest.key] = dest.drive_folder_id
            elif folder_id := self.find_child_folder(root_id, dest.key):
                resolved[dest.key] = folder_id
            else:
                missing.append(dest.key)
        if missing:
            raise DriveError(
                f"Destination folder(s) not found under the Digital_Twin root: {missing}. "
                "Config keys must match folder names exactly."
            )
        return resolved

    # ---- Write operations (live mode only) ---------------------------------

    def find_output_for_source(self, doc_id: str) -> str | None:
        """ID of a previously uploaded .md for this source doc, if any (crash recovery)."""
        q = f"appProperties has {{ key='{SOURCE_KEY}' and value='{_q(doc_id)}' }} and trashed=false"
        matches = list(self._list(q, "id"))
        return matches[0]["id"] if matches else None

    def unique_name(self, folder_id: str, filename: str) -> str:
        stem, dot, ext = filename.rpartition(".")
        if not dot:
            stem, ext = filename, ""
        q = f"'{_q(folder_id)}' in parents and trashed=false"
        taken = {f["name"] for f in self._list(q, "name")}
        candidate, n = filename, 2
        while candidate in taken:
            candidate = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
            n += 1
        return candidate

    def upload_markdown(self, folder_id: str, filename: str, content: str, source_doc_id: str) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype=MARKDOWN_MIME)
        body = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": MARKDOWN_MIME,  # stored as a plain .md file, not converted to a Doc
            "appProperties": {SOURCE_KEY: source_doc_id},
        }
        created = (
            self._svc.files()
            .create(body=body, media_body=media, fields="id", supportsAllDrives=True)
            .execute()
        )
        return created["id"]

    def ensure_folder(self, parent_id: str, name: str) -> str:
        if existing := self.find_child_folder(parent_id, name):
            return existing
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        return self._svc.files().create(body=body, fields="id", supportsAllDrives=True).execute()["id"]

    def mark_processed(self, doc_id: str, inbox_id: str, processed_id: str, output_id: str) -> None:
        """Move the source doc Inbox -> processed folder and tag it as ingested."""
        self._svc.files().update(
            fileId=doc_id,
            addParents=processed_id,
            removeParents=inbox_id,
            body={"appProperties": {INGESTED_KEY: "true", OUTPUT_KEY: output_id}},
            fields="id",
            supportsAllDrives=True,
        ).execute()
