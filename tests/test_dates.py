from datetime import date

import pytest

from digital_twin_ingest.dates import parse_pdf_date, plausible, resolve_date, title_date


@pytest.mark.parametrize("title,year,expected", [
    ("Saturday, August 1", 2026, date(2026, 8, 1)),
    ("Saturday, August 1.docx", 2026, date(2026, 8, 1)),
    ("Thursday, Aug 20", 2026, date(2026, 8, 20)),
    ("Monday August 10th notes", 2026, date(2026, 8, 10)),
    ("tue, aug. 18", 2026, date(2026, 8, 18)),
])
def test_title_date_parses(title, year, expected):
    assert title_date(title, year) == expected


@pytest.mark.parametrize("title,year", [
    ("Saturday, August 1", 2025),     # Aug 1 2025 is a Friday: weekday must match
    ("Friday, February 30", 2026),    # impossible date
    ("Meeting notes August 1", 2026),  # no weekday prefix
    ("Saturday, Smarch 1", 2026),     # not a month
])
def test_title_date_rejects(title, year):
    assert title_date(title, year) is None


def test_parse_pdf_date():
    assert parse_pdf_date("D:20100227153000-05'00'") == date(2010, 2, 27)
    assert parse_pdf_date("20190104") == date(2019, 1, 4)
    assert parse_pdf_date("D:20101399") is None
    assert parse_pdf_date(None) is None


def test_plausible():
    today = date(2026, 11, 11)
    assert plausible(date(2010, 1, 1), today)
    assert not plausible(date(1980, 1, 1), today)
    assert not plausible(date(2030, 1, 1), today)
    assert not plausible(None, today)


def test_resolve_date_priority():
    today = date(2026, 11, 11)
    drive = date(2026, 8, 11)
    assert resolve_date("Monday, August 10", date(2001, 1, 1), drive, today) == (
        date(2026, 8, 10), "title (year from Drive)")
    assert resolve_date("Resume", date(2016, 11, 8), drive, today) == (date(2016, 11, 8), "file metadata")
    assert resolve_date("Resume", date(1970, 1, 1), drive, today) == (drive, "drive modified (fallback)")
    assert resolve_date("Resume", None, drive, today) == (drive, "drive modified (fallback)")
