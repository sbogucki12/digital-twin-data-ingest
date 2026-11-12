"""Date resolution for migrated files, where Drive's own dates are unreliable
(bulk uploads stamp every file with the upload day).

Priority used when building a migration manifest:
  1. a date in the title, e.g. "Saturday, August 1" (weekday must match; year from Drive)
  2. the file's embedded metadata (docx core "created", PDF /CreationDate), if plausible
  3. Drive modifiedTime (fallback)
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_TITLE = re.compile(
    r"^\s*(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_PDF_DATE = re.compile(r"^D?:?(\d{4})(\d{2})(\d{2})")
MIN_YEAR = 1990


def title_date(title: str, year: int) -> date | None:
    """Date from a title like "Saturday, August 1" in the given year, only if the
    weekday matches (guards against picking the wrong year)."""
    m = _TITLE.match(title)
    if not m or m[2][:3].lower() not in _MONTHS:
        return None
    try:
        d = date(year, _MONTHS[m[2][:3].lower()], int(m[3]))
    except ValueError:
        return None
    return d if d.strftime("%a").lower() == m[1][:3].lower() else None


def parse_pdf_date(raw: object) -> date | None:
    m = _PDF_DATE.match(str(raw or ""))
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def plausible(d: date | None, today: date | None = None) -> bool:
    return d is not None and MIN_YEAR <= d.year and d <= (today or date.today())


def resolve_date(
    title: str, metadata_date: date | None, drive_modified: date, today: date | None = None
) -> tuple[date, str]:
    if td := title_date(title, drive_modified.year):
        return td, "title (year from Drive)"
    if plausible(metadata_date, today):
        return metadata_date, "file metadata"
    return drive_modified, "drive modified (fallback)"
