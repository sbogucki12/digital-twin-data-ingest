"""Render the exact frontmatter format:

---
topic: "[Topic Name]"
tags: ["tag1", "tag2"]
core_stance: "[Brief description]"
date: YYYY-MM-DD
---

Strings are emitted as JSON string literals, which are valid YAML double-quoted
scalars, so quotes, colons, backslashes, and newlines can never break the YAML.
"""

from __future__ import annotations

import json
from datetime import date

from .analyzer import DocAnalysis
from .markdown import slugify


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_frontmatter(analysis: DocAnalysis, doc_date: date) -> str:
    tags = "[" + ", ".join(_q(t) for t in analysis.tags) + "]"
    return (
        "---\n"
        f"topic: {_q(analysis.topic)}\n"
        f"tags: {tags}\n"
        f"core_stance: {_q(analysis.core_stance)}\n"
        f"date: {doc_date.isoformat()}\n"
        "---\n"
    )


def render_file(analysis: DocAnalysis, doc_date: date, body: str) -> str:
    return render_frontmatter(analysis, doc_date) + "\n" + body


def output_filename(analysis: DocAnalysis, doc_date: date) -> str:
    return f"{doc_date.isoformat()}_{slugify(analysis.topic)}.md"
