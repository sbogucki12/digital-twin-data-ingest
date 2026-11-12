import io

import docx
import pytest

from digital_twin_ingest.extract import docx_to_markdown, normalize_pdf_pages, pdf_to_markdown


def _docx_bytes(build) -> bytes:
    d = docx.Document()
    build(d)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_headings_lists_formatting_and_tables():
    def build(d):
        d.add_heading("Project Notes", level=1)
        p = d.add_paragraph("Plain then ")
        p.add_run("bold").bold = True
        p.add_run(" and ")
        p.add_run("italic").italic = True
        p.add_run(".")
        d.add_heading("Details", level=2)
        d.add_paragraph("first item", style="List Bullet")
        d.add_paragraph("second item", style="List Bullet")
        d.add_paragraph("step one", style="List Number")
        d.add_paragraph("")  # empty paragraphs are dropped
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "Name", "Value"
        t.cell(1, 0).text, t.cell(1, 1).text = "a|b", "2"

    md = docx_to_markdown(_docx_bytes(build))
    assert "# Project Notes" in md
    assert "Plain then **bold** and *italic*." in md
    assert "## Details" in md
    assert "- first item\n- second item" in md  # tight list
    assert "1. step one" in md
    assert "| Name | Value |\n| --- | --- |\n| a\\|b | 2 |" in md
    assert "\n\n\n" not in md


def test_docx_bold_with_surrounding_spaces_stays_valid():
    def build(d):
        p = d.add_paragraph("x")
        p.add_run(" spaced ").bold = True
        p.add_run("y")

    assert "x **spaced** y" in docx_to_markdown(_docx_bytes(build))


def test_docx_title_style_becomes_h1():
    md = docx_to_markdown(_docx_bytes(lambda d: d.add_paragraph("My Title", style="Title")))
    assert md.startswith("# My Title")


def test_pdf_normalization():
    pages = [
        "Summary of the plan that wraps\nacross two lines.\n• First bullet\n• Second\nbullet continues\n1\n",
        "Page 2 of 3\nNew Section\nhyphen-\nated word here\n#hashtag line\n",
    ]
    out = normalize_pdf_pages(pages)
    assert "Summary of the plan that wraps across two lines." in out
    assert "- First bullet\n- Second bullet continues" in out
    assert "\n1\n" not in out and "Page 2 of 3" not in out
    assert "hyphenated word here" in out
    assert "\\#hashtag line" in out
    assert "New Section" in out


def test_pdf_empty_pages():
    assert normalize_pdf_pages(["", "  \n "]) == "\n"


def test_pdf_to_markdown_on_textless_pdf():
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    assert pdf_to_markdown(buf.getvalue()).strip() == ""


def test_pdf_to_markdown_rejects_garbage():
    with pytest.raises(Exception):  # noqa: B017 — any pypdf parse error is fine
        pdf_to_markdown(b"not a pdf")
