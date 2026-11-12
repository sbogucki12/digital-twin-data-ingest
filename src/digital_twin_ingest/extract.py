"""Local text extraction for non-Google files: .docx -> Markdown, PDF -> text.

Everything happens in memory on downloaded bytes; nothing is written to disk or
to Drive. Output is passed through markdown.clean_markdown() by the caller.
"""

from __future__ import annotations

import io
import logging
import re

# pypdf logs noisy, harmless warnings for decorative fonts (e.g. "fontTools is required").
logging.getLogger("pypdf").setLevel(logging.ERROR)

_HEADING = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)


# ---- .docx -------------------------------------------------------------------

def _run_markdown(paragraph) -> str:
    parts: list[str] = []
    for run in paragraph.runs:
        text = run.text
        if not text:
            continue
        if text.strip() and (run.bold or run.italic):
            core = text.strip()
            lead = text[: len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            mark = "***" if run.bold and run.italic else "**" if run.bold else "*"
            text = f"{lead}{mark}{core}{mark}{trail}"
        parts.append(text)
    # Merge adjacent same-format runs: "**a****b**" -> "**ab**"
    return "".join(parts).replace("****", "")


def _list_prefix(paragraph) -> str | None:
    style = (paragraph.style.name if paragraph.style is not None else "") or ""
    ppr = paragraph._p.pPr
    num = ppr.numPr if ppr is not None else None
    level = 0
    if num is not None and num.ilvl is not None and num.ilvl.val is not None:
        level = int(num.ilvl.val)
    if "list number" in style.lower():
        return "  " * level + "1. "
    if "list" in style.lower() or num is not None:
        return "  " * level + "- "
    return None


def _table_markdown(table) -> str:
    rows = [
        [" ".join(cell.text.split()).replace("|", "\\|") for cell in row.cells]
        for row in table.rows
    ]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def docx_to_markdown(data: bytes) -> str:
    """Convert .docx bytes to Markdown: headings, lists, bold/italic, tables."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    blocks: list[tuple[str, str]] = []  # (kind, text) where kind in {"p", "li", "table"}
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = _run_markdown(para).strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            if style.lower() == "title":
                blocks.append(("p", f"# {text}"))
            elif m := _HEADING.match(style):
                blocks.append(("p", "#" * min(int(m[1]), 6) + " " + text))
            elif (prefix := _list_prefix(para)) is not None:
                blocks.append(("li", prefix + text))
            else:
                blocks.append(("p", text))
        elif tag == "tbl" and (md := _table_markdown(Table(child, document))):
            blocks.append(("table", md))

    out: list[str] = []
    for i, (kind, text) in enumerate(blocks):
        if i:
            out.append("\n" if kind == "li" and blocks[i - 1][0] == "li" else "\n\n")
        out.append(text)
    return "".join(out) + "\n"


# ---- PDF ---------------------------------------------------------------------

_PAGE_NUM = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[•●▪◦‣∙·*–-]\s+")


def normalize_pdf_pages(pages: list[str]) -> str:
    """Turn raw per-page PDF text into readable Markdown-ish text.

    Drops bare page numbers, converts bullet glyphs to "- ", rejoins lines that
    were wrapped mid-sentence (next line starts lowercase) and hyphenated words.
    """
    lines: list[str] = []
    for page in pages:
        for raw in page.splitlines():
            line = " ".join(raw.split())
            if not line:
                if lines and lines[-1]:
                    lines.append("")
                continue
            if _PAGE_NUM.match(line):
                continue
            if _BULLET.match(raw):
                lines.append("- " + _BULLET.sub("", raw).strip())
                continue
            prev = lines[-1] if lines else ""
            if prev and line[0].islower():
                if prev.endswith("-") and not prev.endswith(" -"):
                    lines[-1] = prev[:-1] + line
                else:
                    lines[-1] = f"{prev} {line}"
                continue
            if line.startswith("#"):
                line = "\\" + line  # don't let PDF text become a Markdown heading
            lines.append(line)
        if lines and lines[-1]:
            lines.append("")  # page break -> paragraph break
    return "\n".join(lines).strip() + "\n"


def pdf_to_markdown(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return normalize_pdf_pages([(page.extract_text() or "") for page in reader.pages])


EXTRACTORS = {"docx": docx_to_markdown, "pdf": pdf_to_markdown}
