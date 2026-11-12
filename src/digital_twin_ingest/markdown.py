"""Light cleanup of Google Drive's native Markdown export.

Deliberately conservative: only transformations that cannot change meaning.
Embedded base64 images are removed because they bloat files and would be
billed as input tokens when the text is sent to Claude.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Reference-style image definitions with inline data URIs: [image1]: <data:image/png;base64,...>
_DATA_REF_DEF = re.compile(r"^\[([^\]]+)\]:\s*<?data:image/[^\s>]+>?[ \t]*$", re.MULTILINE)
# Inline data-URI images: ![alt](data:image/png;base64,...)
_DATA_INLINE = re.compile(r"!\[([^\]]*)\]\(<?data:image/[^\s)>]+>?\)")
# Over-escaped punctuation mid-word, e.g. "e\.g\." or "well\-known". Escapes at the
# start of a token (e.g. "1\." or "\- item") are kept since they prevent list syntax.
_MIDWORD_ESCAPE = re.compile(r"(?<=[A-Za-z])\\([.!\-()])")
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\ufeff]")
_H1 = re.compile(r"\A#\s+(.+?)\s*#*\s*(?:\n|\Z)")
IMAGE_PLACEHOLDER = "*[image omitted]*"


@dataclass(frozen=True)
class CleanResult:
    text: str
    images_removed: int


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s).strip().casefold()


def clean_markdown(raw: str, *, title: str | None = None) -> CleanResult:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH.sub("", text).replace("\u00a0", " ")

    images = 0
    labels = set(_DATA_REF_DEF.findall(text))
    if labels:
        text = _DATA_REF_DEF.sub("", text)
        for label in labels:
            pattern = re.compile(r"!\[[^\]]*\]\[" + re.escape(label) + r"\]")
            text, n = pattern.subn(IMAGE_PLACEHOLDER, text)
            images += n
    text, n = _DATA_INLINE.subn(IMAGE_PLACEHOLDER, text)
    images += n

    text = _MIDWORD_ESCAPE.sub(r"\1", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")

    # Drop a leading H1 that merely repeats the doc title (topic goes in frontmatter).
    if title and (m := _H1.match(text)) and _norm(m.group(1)) == _norm(title):
        text = text[m.end():].lstrip("\n")

    return CleanResult(text=text + "\n" if text else "", images_removed=images)


def slugify(value: str, max_len: int = 60) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[\s_-]+", "-", value).strip("-")
    return value[:max_len].rstrip("-") or "untitled"
