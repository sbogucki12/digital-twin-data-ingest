"""Agent 2 — the Critic & Validator.

Audits a retrieval-augmented answer before anyone sees it:

1. ``verify_retrieval`` — do the notes Agent 1 retrieved (category routing + YAML
   frontmatter matching) exist in the knowledge base, carry valid frontmatter, sit in the
   category Agent 1 claimed, carry the tags Agent 1 says it matched, and genuinely match
   the query's intent? Strong notes Agent 1 *missed* are reported too.
2. ``verify_grounding`` — is every factual claim in a drafted response backed by the exact
   source text of the retrieved notes? Hard facts (figures + units, versions, ISO dates,
   quoted text, inline/fenced code, proper nouns) must appear in a source note; the rest of
   each sentence must be lexically supported by one passage of one note.

Deterministic and offline: no model calls, no spend, no network (the optional Drive
loader is read-only). The checks are conservative by design: a faithful paraphrase may be
flagged ``weak`` and sent back for revision, but an invented figure is always rejected.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

MAX_NOTE_BYTES = 2_000_000

# --------------------------------------------------------------------------------------
# Text normalization
# --------------------------------------------------------------------------------------

_STOPWORDS = frozenset(
    [
        "a", "about", "above", "after", "again", "against", "all", "also", "am", "an", "and",
        "any", "are", "as", "at", "be", "because", "been", "before", "being", "below",
        "between", "both", "but", "by", "can", "could", "did", "do", "does", "doing", "down",
        "during", "each", "few", "for", "from", "further", "had", "has", "have", "having", "he",
        "her", "here", "hers", "herself", "him", "himself", "his", "how", "i", "if", "in",
        "into", "is", "it", "its", "itself", "just", "me", "more", "most", "my", "myself", "of",
        "off", "on", "once", "only", "or", "other", "our", "ours", "ourselves", "out", "over",
        "own", "same", "she", "should", "so", "some", "such", "than", "that", "the", "their",
        "theirs", "them", "themselves", "then", "there", "these", "they", "this", "those",
        "through", "to", "too", "under", "until", "up", "very", "was", "we", "were", "what",
        "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you",
        "your", "yours", "yourself", "yourselves", "s", "t", "d", "ll", "m", "re", "ve", "don",
        "isn", "aren", "wasn", "weren", "hasn", "haven", "hadn", "doesn", "didn", "won",
        "wouldn", "shan", "shouldn", "couldn", "mustn", "let", "us", "via", "per", "vs", "etc",
        "eg", "ie", "not", "no", "nor",
    ]
)
# Words that carry the *shape* of a question, not its intent.
_QUERY_FILLER = frozenset(
    [
        "think", "thought", "thoughts", "tell", "know", "notes", "note", "say", "says", "said",
        "opinion", "opinions", "view", "views", "feel", "anything", "something", "write",
        "wrote", "written", "describe", "explain", "summarize", "summary", "please", "give",
        "show", "find", "get", "remind", "remember", "recall", "approach", "take",
    ]
)
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
_NEGATION = re.compile(r"\b(?:not|no|never|none|nothing|neither|nor|without|cannot)\b|n['’]t\b", re.IGNORECASE)


def _stem(word: str) -> str:
    """Tiny deterministic suffix stripper; applied identically to queries and notes."""
    if any(c.isdigit() for c in word):
        return word
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    else:
        if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
            word = word[:-1]
    if len(word) >= 4 and word.endswith("e"):
        word = word[:-1]
    return word


def content_terms(text: str) -> list[str]:
    """Lowercased, stemmed, stopword-free terms (digits kept)."""
    words = _WORD.findall(text.lower())
    return [_stem(w) for w in words if w.isdigit() or (len(w) > 1 and w not in _STOPWORDS)]


def query_terms(query: str) -> tuple[str, ...]:
    filler = {_stem(w) for w in _QUERY_FILLER}
    return tuple(dict.fromkeys(t for t in content_terms(query) if t not in filler))


def _squash(text: str) -> str:
    return " ".join(text.split())


def _norm_quotes(text: str) -> str:
    return text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "−": "-"}))


# --------------------------------------------------------------------------------------
# Notes + knowledge base
# --------------------------------------------------------------------------------------


class NoteFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Note:
    id: str                 # path relative to the KB root, POSIX style: "<Category>/<file>.md"
    category: str | None    # top-level folder, or None for files at the KB root
    topic: str
    tags: tuple[str, ...]
    core_stance: str
    date: date | None
    body: str
    raw: str                # the exact source text (frontmatter + body)


_FRONTMATTER = re.compile(r"---\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)\Z", re.DOTALL)


def _category_of(note_id: str) -> str | None:
    return note_id.split("/", 1)[0] if "/" in note_id else None


def parse_note(note_id: str, text: str, category: str | None = None) -> Note:
    """Parse a digital-twin note; raises NoteFormatError on missing/invalid frontmatter."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    m = _FRONTMATTER.match(text)
    if not m:
        raise NoteFormatError("missing YAML frontmatter")
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise NoteFormatError(f"invalid YAML frontmatter ({exc.__class__.__name__})") from None
    if not isinstance(meta, dict):
        raise NoteFormatError("frontmatter is not a mapping")

    bad: list[str] = []
    topic, stance, tags, raw_date = (meta.get(k) for k in ("topic", "core_stance", "tags", "date"))
    if not isinstance(topic, str) or not topic.strip():
        bad.append("topic")
    if not isinstance(stance, str):
        bad.append("core_stance")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        bad.append("tags")
    note_date: date | None = None
    if isinstance(raw_date, datetime):
        note_date = raw_date.date()
    elif isinstance(raw_date, date):
        note_date = raw_date
    elif isinstance(raw_date, str):
        try:
            note_date = date.fromisoformat(raw_date)
        except ValueError:
            bad.append("date")
    else:
        bad.append("date")
    if bad:
        raise NoteFormatError(f"frontmatter field(s) missing or invalid: {', '.join(bad)}")

    return Note(
        id=note_id,
        category=category if category is not None else _category_of(note_id),
        topic=topic.strip(),
        tags=tuple(tags),
        core_stance=stance.strip(),
        date=note_date,
        body=m.group(2),
        raw=text,
    )


def _norm_ref(ref: str) -> str:
    ref = ref.strip().replace("\\", "/")
    while ref.startswith("./"):
        ref = ref[2:]
    return ref.lstrip("/")


class KnowledgeBase:
    """The digital twin's Markdown notes, keyed by ``<Category>/<filename>.md``."""

    def __init__(self, notes: Iterable[Note] = (), errors: Mapping[str, str] | None = None):
        self._notes: dict[str, Note] = {}
        self._fields: dict[str, dict[str, set[str]]] = {}
        self.errors: dict[str, str] = dict(errors or {})
        for note in notes:
            self.add(note)

    def add(self, note: Note) -> None:
        if note.id in self._notes:
            self.errors[note.id] = "duplicate note id"
            return
        self._notes[note.id] = note
        self._fields[note.id] = {
            "tags": set(content_terms(" ".join(note.tags))),
            "topic": set(content_terms(note.topic)),
            "core_stance": set(content_terms(note.core_stance)),
            "category": set(content_terms(note.category or "")),
            "body": set(content_terms(note.body)),
        }

    @classmethod
    def from_texts(cls, texts: Mapping[str, str]) -> KnowledgeBase:
        kb = cls()
        for note_id, text in texts.items():
            nid = _norm_ref(note_id)
            try:
                kb.add(parse_note(nid, text))
            except NoteFormatError as exc:
                kb.errors[nid] = str(exc)
        return kb

    @classmethod
    def from_directory(cls, root: Path | str) -> KnowledgeBase:
        root = Path(root)
        if not root.is_dir():
            raise FileNotFoundError(f"knowledge base directory not found: {root}")
        kb = cls()
        for path in sorted(root.rglob("*.md")):
            if not path.is_file():
                continue
            nid = path.relative_to(root).as_posix()
            try:
                if path.stat().st_size > MAX_NOTE_BYTES:
                    kb.errors[nid] = "file too large"
                    continue
                text = path.read_text(encoding="utf-8-sig")
                kb.add(parse_note(nid, text))
            except (OSError, UnicodeDecodeError) as exc:
                kb.errors[nid] = f"unreadable ({exc.__class__.__name__})"
            except NoteFormatError as exc:
                kb.errors[nid] = str(exc)
        return kb

    @classmethod
    def from_drive(cls, drive: Any, folders: Mapping[str, str]) -> KnowledgeBase:
        """Read-only load from Drive: ``folders`` maps category key -> folder ID."""
        texts: dict[str, str] = {}
        errors: dict[str, str] = {}
        for category, folder_id in folders.items():
            for f in drive.list_markdown(folder_id):
                nid = f"{category}/{f['name']}"
                if nid in texts:
                    errors[nid] = "duplicate file name in folder"
                    continue
                texts[nid] = drive.download(f["id"]).decode("utf-8", errors="replace")
        kb = cls.from_texts(texts)
        kb.errors.update(errors)
        return kb

    @property
    def notes(self) -> list[Note]:
        return list(self._notes.values())

    def __len__(self) -> int:
        return len(self._notes)

    def __contains__(self, note_id: object) -> bool:
        return note_id in self._notes

    def get(self, note_id: str) -> Note | None:
        return self._notes.get(note_id)

    def fields(self, note_id: str) -> dict[str, set[str]]:
        return self._fields[note_id]

    def resolve(self, ref: str) -> tuple[str | None, Note | None]:
        """Resolve a retrieval reference: exact id, else a unique bare filename.

        Returns ``(id, note)``; ``(id, None)`` for a known-but-unparseable file;
        ``(None, None)`` when nothing (or more than one file) matches.
        """
        ref = _norm_ref(ref)
        if ref in self._notes:
            return ref, self._notes[ref]
        if ref in self.errors:
            return ref, None
        if "/" not in ref:
            known = list(self._notes) + list(self.errors)
            matches = [k for k in known if k.rsplit("/", 1)[-1] == ref]
            if len(matches) == 1:
                return matches[0], self._notes.get(matches[0])
        return None, None


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatorConfig:
    relevant_threshold: float = 0.5     # retrieval score for "relevant"
    weak_threshold: float = 0.2         # retrieval score for "weak" (below: "irrelevant")
    supported_threshold: float = 0.6    # claim support for "supported"
    unsupported_threshold: float = 0.35  # claim support below this: "unsupported"
    window: int = 3                     # consecutive source sentences a claim may draw on
    max_missed: int = 5
    strict: bool = True                 # strict: "weak" claims also fail the draft


DEFAULT_CONFIG = ValidatorConfig()

# How strongly a query term matching each note field signals intent.
_FIELD_WEIGHTS = (("tags", 1.0), ("topic", 1.0), ("core_stance", 0.75), ("category", 0.5), ("body", 0.5))
_META_FIELDS = frozenset({"tags", "topic", "core_stance"})


# --------------------------------------------------------------------------------------
# 1. Retrieval verification
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalHit:
    """One document Agent 1 returned, with the routing/matching it claims to have done."""

    note_id: str
    category: str | None = None             # category Agent 1 routed this note to
    matched_tags: tuple[str, ...] = ()      # frontmatter tags Agent 1 says it matched on

    @classmethod
    def from_obj(cls, obj: str | Mapping[str, Any] | RetrievalHit) -> RetrievalHit:
        if isinstance(obj, RetrievalHit):
            return obj
        if isinstance(obj, str):
            return cls(obj)
        tags = obj.get("matched_tags") or ()
        if isinstance(tags, str):
            tags = (tags,)
        return cls(str(obj["note_id"]), obj.get("category"), tuple(str(t) for t in tags))


@dataclass
class DocVerdict:
    note_id: str
    verdict: str                                  # relevant | weak | irrelevant | missing | invalid
    score: float = 0.0
    matched_terms: tuple[str, ...] = ()
    issues: list[str] = field(default_factory=list)    # fail the retrieval
    warnings: list[str] = field(default_factory=list)  # reported only

    @property
    def ok(self) -> bool:
        return self.verdict == "relevant" and not self.issues


@dataclass
class RetrievalReport:
    query: str
    query_terms: tuple[str, ...]
    results: list[DocVerdict]
    missed: list[tuple[str, float]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if not self.results:
            return not self.missed  # "nothing relevant" is only right if nothing is
        return all(r.ok for r in self.results)


def _relevance(terms: Sequence[str], fields: Mapping[str, set[str]]) -> tuple[float, tuple[str, ...], bool]:
    total, matched, meta = 0.0, [], False
    for t in terms:
        weight = 0.0
        for name, w in _FIELD_WEIGHTS:
            if t in fields[name]:
                weight = max(weight, w)
                meta = meta or name in _META_FIELDS
        if weight:
            total += weight
            matched.append(t)
    return (total / len(terms) if terms else 0.0), tuple(matched), meta


def score_note(query: str, kb: KnowledgeBase, note_id: str) -> float:
    return _relevance(query_terms(query), kb.fields(note_id))[0]


def _verdict(score: float, meta: bool, cfg: ValidatorConfig) -> str:
    if score >= cfg.relevant_threshold and meta:
        return "relevant"
    return "weak" if score >= cfg.weak_threshold else "irrelevant"


def verify_retrieval(
    query: str,
    hits: Sequence[RetrievalHit | str | Mapping[str, Any]],
    kb: KnowledgeBase,
    *,
    routed_category: str | None = None,
    config: ValidatorConfig = DEFAULT_CONFIG,
) -> RetrievalReport:
    terms = query_terms(query)
    if not terms:
        raise ValueError("query has no content terms to verify retrieval against")

    results: list[DocVerdict] = []
    seen: set[str] = set()
    for raw_hit in hits:
        hit = RetrievalHit.from_obj(raw_hit)
        nid, note = kb.resolve(hit.note_id)
        if nid is None:
            results.append(DocVerdict(_norm_ref(hit.note_id), "missing",
                                      issues=["not found in the knowledge base"]))
            continue
        if note is None:
            results.append(DocVerdict(nid, "invalid", issues=[f"unparseable note: {kb.errors[nid]}"]))
            continue

        score, matched, meta = _relevance(terms, kb.fields(nid))
        v = DocVerdict(nid, _verdict(score, meta, config), round(score, 3), matched)
        if v.verdict != "relevant":
            v.issues.append(f"{v.verdict} match for the query intent (score {score:.2f})")
        if hit.category is not None and hit.category != note.category:
            v.issues.append(f"routed to {hit.category!r} but the note is filed under {note.category!r}")
        note_tags = {t.casefold() for t in note.tags}
        phantom = [t for t in hit.matched_tags if t.casefold() not in note_tags]
        if phantom:
            v.issues.append(f"claimed frontmatter tag(s) not on the note: {phantom}")
        if routed_category is not None and note.category != routed_category:
            v.warnings.append(f"outside the routed category {routed_category!r}")
        if nid in seen:
            v.warnings.append("retrieved more than once")
        seen.add(nid)
        results.append(v)

    missed: list[tuple[str, float]] = []
    for note in kb.notes:
        if note.id in seen:
            continue
        score, _, meta = _relevance(terms, kb.fields(note.id))
        if _verdict(score, meta, config) == "relevant":
            missed.append((note.id, round(score, 3)))
    missed.sort(key=lambda x: (-x[1], x[0]))
    return RetrievalReport(query, terms, results, missed[: config.max_missed])


# --------------------------------------------------------------------------------------
# 2. Hallucination guardrail (grounding)
# --------------------------------------------------------------------------------------

_UNITS = {
    "%": "%", "percent": "%", "pct": "%", "$": "$", "usd": "$", "dollar": "$", "dollars": "$",
    "ms": "ms", "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "day": "day", "days": "day", "week": "week", "weeks": "week", "month": "month", "months": "month",
    "year": "year", "years": "year", "yr": "year", "yrs": "year",
    "hz": "hz", "khz": "khz", "mhz": "mhz", "ghz": "ghz", "db": "db", "dbfs": "dbfs", "dbtp": "dbtp",
    "lufs": "lufs", "lu": "lu", "bit": "bit", "bits": "bit", "bpm": "bpm",
    "b": "b", "kb": "kb", "mb": "mb", "gb": "gb", "tb": "tb", "k": "k", "x": "x",
    "px": "px", "rem": "rem", "em": "em", "fps": "fps",
    "mm": "mm", "cm": "cm", "m": "m", "km": "km", "in": "in", "ft": "ft", "mi": "mi",
    "g": "g", "kg": "kg", "oz": "oz", "lb": "lb", "lbs": "lb", "ml": "ml", "l": "l",
    "f": "f", "c": "c", "°f": "f", "°c": "c", "tok": "tok", "tokens": "tok", "mtok": "mtok",
}
_NUM = re.compile(
    r"(?<![\w.$])(?P<cur>\$)?(?P<sign>[-+])?"
    r"(?P<val>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<unit>%|°?[A-Za-z]+))?"
)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_VERSION = re.compile(r"\b(?:v\d+(?:\.\d+)*|\d+\.\d+\.\d+(?:\.\d+)*)\b", re.IGNORECASE)
_QUOTE = re.compile(r"\"([^\"\n]{2,300})\"")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_ENTITY = re.compile(r"(?<![\w'])[A-Z][A-Za-z0-9]*(?:['-][A-Za-z0-9]+)*")
_MD_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9$])")
_ABSTAIN = re.compile(
    r"\b(?:i\s+(?:don't|do not|couldn't|could not|can't|cannot)\s+(?:have|find|see)"
    r"|no\s+(?:notes?|information|records?)\s+(?:on|about|for)"
    r"|not\s+(?:in|covered by)\s+(?:my|the)\s+notes)\b",
    re.IGNORECASE,
)


def _num_key(m: re.Match[str]) -> tuple[str, str | None]:
    raw = m.group("val").replace(",", "")
    try:
        value = format(Decimal(raw).normalize(), "f")
    except InvalidOperation:  # pragma: no cover - regex only admits digits
        value = raw
    if m.group("sign") == "-" and value != "0":
        value = "-" + value
    unit = _UNITS.get((m.group("unit") or "").lower())
    if m.group("cur"):
        unit = "$"
    return value, unit


@dataclass(frozen=True)
class _SourceIndex:
    note_id: str
    raw: str
    lower: str              # quotes normalized, whitespace squashed, lowercased
    squashed: str           # whitespace squashed, case kept (code)
    values: frozenset[str]
    value_units: frozenset[tuple[str, str]]
    sentences: tuple[str, ...]
    windows: tuple[tuple[frozenset[str], int], ...]  # (terms, first sentence index)

    @classmethod
    def build(cls, note: Note, window: int) -> _SourceIndex:
        raw = _norm_quotes(note.raw)
        keys = [_num_key(m) for m in _NUM.finditer(raw)]
        sentences = tuple(_sentences(raw))
        sent_terms = [set(content_terms(s)) for s in sentences]
        windows = tuple((frozenset().union(*sent_terms[i : i + window]), i) for i in range(len(sentences)))
        return cls(
            note_id=note.id,
            raw=raw,
            lower=_squash(raw).lower(),
            squashed=_squash(raw),
            values=frozenset(v for v, _ in keys),
            value_units=frozenset((v, u) for v, u in keys if u),
            sentences=sentences,
            windows=windows,
        )


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        line = _MD_PREFIX.sub("", line).replace("**", "").replace("__", "").strip()
        if line and line != "---":
            out.extend(s.strip() for s in _SENT_SPLIT.split(line) if s.strip())
    return out


@dataclass
class Fact:
    kind: str               # number | date | version | quote | code | entity
    text: str
    grounded: bool = False
    source: str | None = None


@dataclass
class Claim:
    text: str
    status: str             # supported | weak | unsupported
    support: float
    source: str | None      # best-supporting note
    facts: list[Fact] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class GroundingReport:
    claims: list[Claim]
    skipped: list[str] = field(default_factory=list)  # questions, abstentions, filler
    strict: bool = True

    @property
    def ungrounded_facts(self) -> list[Fact]:
        return [f for c in self.claims for f in c.facts if not f.grounded]

    @property
    def passed(self) -> bool:
        bad = {"unsupported", "weak"} if self.strict else {"unsupported"}
        return not any(c.status in bad for c in self.claims)


def _mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def extract_facts(sentence: str) -> list[Fact]:
    """Hard, checkable facts in a sentence. Each must appear in a source note."""
    s = _norm_quotes(sentence)
    facts: list[Fact] = []
    spans: list[tuple[int, int]] = []
    for kind, rx in (("code", _INLINE_CODE), ("quote", _QUOTE)):
        for m in rx.finditer(s):
            if not any(a <= m.start() < b for a, b in spans):
                facts.append(Fact(kind, m.group(1).strip()))
                spans.append(m.span())
    s = _mask(s, spans)
    spans = []
    for kind, rx in (("date", _ISO_DATE), ("version", _VERSION)):
        for m in rx.finditer(s):
            if not any(a <= m.start() < b for a, b in spans):
                facts.append(Fact(kind, m.group(0)))
                spans.append(m.span())
    s = _mask(s, spans)
    spans = []
    for m in _NUM.finditer(s):
        unit_ok = (m.group("unit") or "").lower() in _UNITS
        end = m.end() if unit_ok else m.end("val")
        facts.append(Fact("number", s[m.start() : end].strip()))
        spans.append((m.start(), end))

    first = re.search(r"[A-Za-z0-9]", s)
    first_idx = first.start() if first else -1
    for m in _ENTITY.finditer(s):
        tok = re.sub(r"'s$", "", m.group(0))
        is_acronym = len(tok) >= 2 and tok.isupper() and tok.isalpha()
        if tok in {"I", "I'm", "I've", "I'd", "I'll"} or tok.lower() in _STOPWORDS:
            continue
        before = s[: m.start()].rstrip()
        if not is_acronym and (m.start() == first_idx or before.endswith((":", ".", "!", "?", "—", "("))):
            continue  # capitalized only because a sentence/clause starts here
        if any(a <= m.start() < b for a, b in spans):
            continue  # "48 kHz": the unit is checked with its number
        facts.append(Fact("entity", tok))
    return facts


def _ground(fact: Fact, src: _SourceIndex) -> bool:
    if fact.kind == "code":
        return _squash(fact.text) in src.squashed
    if fact.kind == "quote":
        needle = _squash(fact.text).lower().strip(" .,;:!?")
        return bool(needle) and needle in src.lower
    if fact.kind in ("date", "version"):
        return re.search(rf"(?<![\w.]){re.escape(fact.text)}(?![\w]|\.\d)", src.raw, re.IGNORECASE) is not None
    if fact.kind == "number":
        m = _NUM.fullmatch(fact.text) or _NUM.search(fact.text)
        if not m:  # pragma: no cover - fact text came from _NUM
            return False
        value, unit = _num_key(m)
        return (value, unit) in src.value_units if unit else value in src.values
    # entity: whole-word, case-insensitive; allow a plural/possessive form in the source
    pat = rf"(?<![A-Za-z0-9]){re.escape(fact.text)}(?:'?s)?(?![A-Za-z0-9])"
    return re.search(pat, src.raw, re.IGNORECASE) is not None


def _support(
    terms: set[str], sources: Sequence[_SourceIndex], window: int
) -> tuple[float, _SourceIndex | None, str]:
    """Best fraction of claim terms covered by one window of one note, + best sentence."""
    best, best_src, best_i = 0.0, None, 0
    for src in sources:
        for wterms, i in src.windows:
            frac = len(terms & wterms) / len(terms)
            if frac > best:
                best, best_src, best_i = frac, src, i
    if best_src is None:
        return 0.0, None, ""
    passage = best_src.sentences[best_i : best_i + window]
    sentence = max(passage, key=lambda x: len(terms & set(content_terms(x))))
    return best, best_src, sentence


def _is_claim(sentence: str, facts: list[Fact], terms: list[str]) -> bool:
    if sentence.rstrip().endswith("?") or _ABSTAIN.search(sentence):
        return False
    return bool(facts) or len(set(terms)) >= 3


def verify_grounding(
    draft: str,
    notes: Sequence[Note],
    *,
    config: ValidatorConfig = DEFAULT_CONFIG,
) -> GroundingReport:
    sources = [_SourceIndex.build(n, config.window) for n in notes]
    report = GroundingReport(claims=[], strict=config.strict)
    draft = _norm_quotes(draft.replace("\r\n", "\n"))

    # Fenced code: every non-blank line is a spec that must exist in a source verbatim.
    for block in _FENCE.findall(draft):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        facts = [Fact("code", ln.strip()) for ln in lines]
        _check_facts(facts, sources)
        grounded = all(f.grounded for f in facts)
        claim = Claim("[code block]", "supported" if grounded else "unsupported", 1.0 if grounded else 0.0,
                      next((f.source for f in facts if f.source), None), facts)
        if not grounded:
            claim.issues.extend(f"ungrounded code: {f.text}" for f in facts if not f.grounded)
        report.claims.append(claim)
    # Headings organize the answer; they are not claims.
    prose = "\n".join(ln for ln in _FENCE.sub("\n", draft).split("\n") if not ln.lstrip().startswith("#"))

    for sentence in _sentences(prose):
        facts = extract_facts(sentence)
        # Figures are checked exactly as facts; lexical support covers the words around them.
        terms = [t for t in content_terms(sentence) if not any(c.isdigit() for c in t)]
        if not _is_claim(sentence, facts, terms):
            report.skipped.append(sentence)
            continue
        _check_facts(facts, sources)
        support, src, best_sentence = _support(set(terms), sources, config.window) if terms else (1.0, None, "")
        claim = Claim(sentence, "supported", round(support, 3), src.note_id if src else None, facts)

        ungrounded = [f for f in facts if not f.grounded]
        claim.issues.extend(f"ungrounded {f.kind}: {f.text}" for f in ungrounded)
        if ungrounded:
            claim.status = "unsupported"
        elif support < config.unsupported_threshold:
            claim.status = "unsupported"
            claim.issues.append(f"not supported by any retrieved note (support {support:.2f})")
        else:
            if support < config.supported_threshold:
                claim.status = "weak"
                claim.issues.append(f"only partially supported (support {support:.2f})")
            if best_sentence and bool(_NEGATION.search(sentence)) != bool(_NEGATION.search(best_sentence)):
                claim.status = "weak"
                claim.issues.append("negation differs from the closest source sentence")
        report.claims.append(claim)
    return report


def _check_facts(facts: list[Fact], sources: Sequence[_SourceIndex]) -> None:
    for f in facts:
        for src in sources:
            if _ground(f, src):
                f.grounded, f.source = True, src.note_id
                break


# --------------------------------------------------------------------------------------
# Combined audit
# --------------------------------------------------------------------------------------


@dataclass
class ValidationReport:
    retrieval: RetrievalReport
    grounding: GroundingReport | None

    @property
    def approved(self) -> bool:
        return self.retrieval.passed and (self.grounding is None or self.grounding.passed)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "approved": self.approved,
            "retrieval": {**asdict(self.retrieval), "passed": self.retrieval.passed},
            "grounding": None,
        }
        for r, v in zip(out["retrieval"]["results"], self.retrieval.results, strict=True):
            r["ok"] = v.ok
        if self.grounding is not None:
            out["grounding"] = {**asdict(self.grounding), "passed": self.grounding.passed}
        return out


def validate(
    query: str,
    hits: Sequence[RetrievalHit | str | Mapping[str, Any]],
    kb: KnowledgeBase,
    draft: str | None = None,
    *,
    routed_category: str | None = None,
    config: ValidatorConfig = DEFAULT_CONFIG,
) -> ValidationReport:
    """Audit Agent 1's retrieval and (optionally) a drafted response grounded on it.

    The draft is checked against every retrieved note that exists and parses — relevance
    is judged separately, so an off-topic note can still ground a true statement.
    """
    retrieval = verify_retrieval(query, hits, kb, routed_category=routed_category, config=config)
    grounding = None
    if draft is not None:
        notes = [n for r in retrieval.results if (n := kb.get(r.note_id)) is not None]
        notes = list({n.id: n for n in notes}.values())
        grounding = verify_grounding(draft, notes, config=config)
    return ValidationReport(retrieval, grounding)


def format_report(report: ValidationReport) -> str:
    lines = [f"VERDICT: {'APPROVED' if report.approved else 'REJECTED'}", ""]
    r = report.retrieval
    lines.append(f"Retrieval: {'PASS' if r.passed else 'FAIL'}  (query terms: {', '.join(r.query_terms)})")
    if not r.results:
        lines.append("  (no documents retrieved)")
    for v in r.results:
        lines.append(f"  [{v.verdict:>10}] {v.note_id}  score={v.score:.2f}")
        lines.extend(f"      ! {i}" for i in v.issues)
        lines.extend(f"      ~ {w}" for w in v.warnings)
    if r.missed:
        lines.append("  Relevant notes not retrieved:")
        lines.extend(f"      + {nid}  score={s:.2f}" for nid, s in r.missed)
    g = report.grounding
    if g is not None:
        mode = "strict" if g.strict else "lenient"
        lines += ["", (f"Grounding: {'PASS' if g.passed else 'FAIL'}  "
                      f"({len(g.claims)} claim(s), {len(g.skipped)} skipped, {mode})")]
        for c in g.claims:
            text = c.text if len(c.text) <= 90 else c.text[:87] + "..."
            lines.append(f"  [{c.status:>11}] {text}")
            lines.append(f"      support={c.support:.2f} source={c.source or '-'}")
            lines.extend(f"      ! {i}" for i in c.issues)
    return "\n".join(lines)
