# Architecture — Digital Twin Multi-Agent Pipeline

Status: **Operational.** Phases 1–4 complete (2026-11-11). The active multi-agent pipeline is **Agent 1 (Retriever & Router)**, implemented by the ingestion engine and Drive folder architecture in §§1–9a, and **Agent 2 (Critic & Validator)**, added 2026-11-12 (§9b). Future work is maintenance and scoped feature expansion; see `manifest.md`.

### Agent map

| Agent | Responsibility | Implementation |
|---|---|---|
| **Agent 1: Retriever & Router** | Ingest (Google Docs, `.docx`, text-layer PDF) → YAML frontmatter tagging (`topic`, `tags`, `core_stance`, `date`) → category-first routing into the 5 configured folders → native Google Drive storage. That folder + frontmatter structure is the retrieval index: a query is routed to a category folder and matched on frontmatter. | `pipeline.py`, `migrate.py`, `extract.py`, `analyzer.py`, `frontmatter.py`, `drive.py` (§§1–9a) |
| **Agent 2: Critic & Validator** | Verifies the retrieved set (exists, parses, correct folder, real tags, intent match, missed notes) and fact-checks the draft against the notes' exact text | `validator.py` (§9b) |

## 1. Pipeline overview

```
 Drive: Inbox/ ──► [1] discover ──► [2] export as Markdown + clean ──► [3] analyze (Claude Haiku)
                                                                          │  JSON: topic, tags,
                                                                          │  core_stance, destination_folder
                                                                          │  usage ──► local cost ledger
                                                                          ▼
 Drive: Inbox/_Processed/ ◄── [6] mark done ◄── [5] upload .md ◄── [4] render frontmatter + body
                                                   │
                                                   ▼
                                Drive: Digital_Twin/<Category_N>/<date>_<slug>.md
```

Each document is processed **independently**: a failure on one doc is logged and skipped; the batch continues. A doc is only marked processed after its upload succeeds, so a crash mid-run is safely retried next run.

## 2. Module layout

| Module | Responsibility | Phase |
|---|---|---|
| `__main__.py` | CLI: `auth`, `run [--live] [--limit N] [--doc-id ID]`, `costs` | 3 ✅ |
| `config.py` | `.env` (secrets, folder IDs) + gitignored YAML (taxonomy, tuning, pricing) → typed `Settings` | 3 ✅ |
| `google_auth.py` | OAuth installed-app flow; token cached outside repo, refreshed automatically | 3 ✅ |
| `drive.py` | Inbox listing, Markdown export, folder validation/resolution (read-only so far) | 3 ✅ |
| `markdown.py` | Conservative cleanup of Drive's Markdown export; `slugify` | 3 ✅ |
| `cost.py` | Token → USD estimation; SQLite cost ledger; per-run totals | 3–4 ✅ |
| `pipeline.py` | Orchestration, per-doc isolation, spend cap, crash recovery, dry-run default | 4 ✅ |
| `analyzer.py` | Claude Haiku call → validated `DocAnalysis`; logs every response's cost | 4 ✅ |
| `frontmatter.py` | Exact-format YAML frontmatter + filename | 4 ✅ |
| `drive.py` (writes) | `upload_markdown()`, `unique_name()`, `ensure_folder()`, `mark_processed()`, `find_output_for_source()` | 4 ✅ |
| `validator.py` | **Agent 2 (Critic & Validator):** retrieval verification + hallucination guardrail; `validate` CLI (§9b) | ✅ |

**No personal taxonomy lives in source.** Destination keys/descriptions are read from config at runtime; the `destination_folder` enum is built from them dynamically.

## 3. Discovery (Drive API v3)

- `files.list` with `q = "'<INBOX_ID>' in parents and mimeType='application/vnd.google-apps.document' and trashed=false"`, paginated.
- Docs carrying `appProperties.dt_ingested = "true"` are excluded (belt-and-braces alongside the move to `_Processed`).
- Processed oldest-first by date basis (created, else modified).
- Read-only preflight on every run: Inbox and Digital_Twin root must be folders; every configured destination must resolve (by explicit ID, or by exact folder name under the root).

## 4. Google Doc → Markdown (decided: Drive native export)

`files.export(mimeType="text/markdown")`, then `markdown.clean_markdown()`:

- Normalize line endings; strip zero-width chars; non-breaking spaces → spaces.
- **Remove embedded base64 images** (Google inlines them as data URIs) → `*[image omitted]*`. They'd bloat the `.md` and be billed as input tokens.
- Unescape over-escaped punctuation only mid-word (`e\.g\.` → `e.g.`); escapes that prevent list syntax (`1\.`, `\-`) are kept.
- Trim trailing whitespace; collapse 3+ blank lines.
- Drop a leading H1 that just repeats the doc title.
- Docs over `max_doc_chars` are **skipped, never truncated**.

## 5. Analysis (Anthropic API)

- **Model: `claude-haiku-5-5`** ($0.10 / $0.50 per MTok input/output for prompts ≤100K tokens; $0.50 / $2.50 above). The cheapest current model with structured outputs. (Claude 3 Haiku was retired 2026-04-20.)
- `client.messages.create(..., output_config={"effort": "low", "format": {"type": "json_schema", "schema": ...}})` — the API enforces the JSON schema; the text block is then validated into Pydantic (`DocAnalysis`) as defense in depth. Thinking blocks are skipped by type, never by position.
- Every response is written to the cost ledger **before** its content is inspected, so refusals and truncated responses are still accounted for.
- Auth/permission errors abort the run (every doc would fail); all other failures are per-doc.
- `effort: low` keeps adaptive thinking (billed as output) minimal; `max_tokens: 2048` leaves room for brief thinking plus the JSON, avoiding a truncated `max_tokens` stop.
- `destination_folder` is a `Literal[...]` of the configured keys — the model cannot invent a destination.
- System prompt (instructions + category descriptions) is stable → cacheable prefix if volume warrants it.
- `stop_reason` checked before reading output; on `refusal` or `max_tokens`, the doc stays in the Inbox and is reported. (Haiku 5.5 has no server-side refusal fallback; none is configured.)

```python
class DocAnalysis(BaseModel):
    topic: str
    tags: list[str]          # normalized in code: lowercase, deduped
    core_stance: str
    destination_folder: Literal[<config keys>]
```

## 6. Cost observability

Every Anthropic response → `CostLedger.record(run_id, doc_id, model, usage, pricing, stop_reason)`:

- Stored in `logs/cost_metrics.sqlite` (gitignored). On POSIX the dir/file are `0700`/`0600`; on Windows they inherit the user-profile ACL (owner-only).
- Captures input, output, cache-write (5m/1h), and cache-read tokens; picks the long-context rate card when the full prompt exceeds the threshold; computes estimated USD.
- `python -m digital_twin_ingest costs` prints per-run calls, tokens, and spend, plus the all-time total.
- **Fail closed:** a model with no configured pricing is refused, so spend is never untracked.
- **Hard spend cap: $1.00 per run** (`max_cost_per_run_usd`; config may lower it, the validator rejects anything above $1.00). Before every call the pipeline computes a *worst-case* cost — prompt UTF-8 bytes (an upper bound on tokens) + overhead at the higher rate card, plus `max_tokens` of output — and refuses the call if `spent_this_run + worst_case > cap`. The run then stops; remaining docs stay in the Inbox. A max-size (200K-char) doc bounds at ~$0.11, so the cap cannot be crossed.
- Estimates use configured list prices; the Anthropic Console remains the billing source of truth.

## 7. Frontmatter + output file

Exact format. Strings are emitted as JSON string literals — valid YAML double-quoted scalars — so quotes, colons, and newlines in model output can't break or inject YAML:

```markdown
---
topic: "[Topic Name]"
tags: ["tag1", "tag2"]
core_stance: "[Brief description of philosophy, framework, or standard]"
date: YYYY-MM-DD
---
```

- `date` = doc's original **created** date; last-modified only if created is unavailable. Never ingestion date.
- Filename: `YYYY-MM-DD_<slugified-topic>.md`; on collision append `-2`, `-3`.

## 8. Auth & scopes

- Google OAuth 2.0 installed-app flow, scope `https://www.googleapis.com/auth/drive` (full Drive — required to write into pre-existing folders and move processed docs; `drive.file` can't see them). Token refreshes automatically; scope changes force re-consent.
- Anthropic: `ANTHROPIC_API_KEY` from `.env`, or an `ant auth login` profile.

## 9. Idempotency & dry run

- **Live (`--live`)**: upload `.md` (tagged `appProperties.dt_source_id`) → move source to `Inbox/_Processed` (created on first live run) and tag it `{dt_ingested, dt_output_id}`.
- **Crash recovery:** if a prior run uploaded but failed to move, the next live run finds the `.md` by `dt_source_id`, finishes the move, and makes **no new Claude call or duplicate upload**.
- **Dry run (default)**: full pipeline including the Claude call (cost-logged, capped), but zero Drive writes; previews land in `output/<Category>/<filename>.md` (gitignored).

## 9a. Manifest-driven migration (`migrate`)

For bulk-migrating existing Drive files, a reviewed CSV manifest (gitignored) lists exactly which file IDs to ingest.

| Module | Responsibility |
|---|---|
| `manifest.py` | Load/validate manifest (include rows only, supported kinds `gdoc`/`docx`/`pdf`, ISO `doc_date`, optional `max_chars_override`); write `<manifest>_results.csv` |
| `extract.py` | In-memory extraction: `.docx` → Markdown via python-docx (headings, lists, bold/italic, tables); PDF → text via pypdf (page numbers dropped, wrapped/hyphenated lines rejoined, bullets normalized) |
| `dates.py` | Date resolution for manifest building: title date (weekday-validated, year from Drive) → embedded file metadata (if plausible) → Drive modified |
| `migrate.py` | Per row: precheck (exists, not trashed, not already ingested, mimeType and md5 unchanged) → extract → `clean_markdown` → shared `process_text` |

- **Shared core:** `pipeline.process_text` (checks, spend cap, crash recovery, analyze, render, preview/upload) is used by both `run` and `migrate`; only the finalize step differs. `run` moves the source to `_Processed`; `migrate` calls `tag_ingested` (metadata only, **no move**).
- **Dates:** the manifest's `doc_date` is authoritative (bulk uploads make Drive dates unreliable).
- **Dry run makes zero Drive writes**: exports and downloads are reads; local `.docx` conversion avoids temporary Drive copies.
- `--limit` counts only files actually processed; already-ingested/changed files don't consume it.

## 9b. Agent 2 — Critic & Validator (`validator.py`)

```
 Agent 1: Retriever & Router                                        Agent 2: Critic & Validator
 ingest ─► frontmatter ─► category-first ─► Drive    query ─► notes   │ 1. verify_retrieval
 (Docs/.docx/PDF)  tags      routing        folders  routed + matched │ 2. verify_grounding
                                                     + draft ────────►▼
                                                  APPROVED / REJECTED + per-note, per-claim reasons
```

Agent 2 is deterministic and offline: **no LLM calls, no spend, no network** (the Drive loader is read-only: `files.list` + `get_media`). Its input is the retrieved set from Agent 1's folder + frontmatter structure, as a list of `RetrievalHit(note_id, category?, matched_tags?)`, plus a draft. It reads the same knowledge base Agent 1 writes: local previews (`preview_dir`) or the Drive category folders (`--drive`).

| Component | Responsibility |
|---|---|
| `parse_note` / `Note` | Parse a note's YAML frontmatter (`yaml.safe_load`), validate `topic`/`tags`/`core_stance`/`date`; `raw` keeps the exact source text |
| `KnowledgeBase` | Notes keyed `<Category>/<file>.md`; `from_directory` (local `preview_dir`), `from_drive` (destination folders), `from_texts`. Unparseable/oversized/undecodable files are recorded in `errors`, never fatal. `resolve()` accepts exact IDs, Windows paths, or a unique bare filename |
| `verify_retrieval` | Per hit: `missing` / `invalid` / `relevant` / `weak` / `irrelevant`, plus issues (routing mismatch, phantom tags) and warnings (outside routed category, duplicates). Reports up to 5 relevant notes not retrieved |
| `verify_grounding` | Splits the draft into claims; checks hard facts and lexical support; returns per-claim status, best source note, and issues |
| `validate` / `ValidationReport` | Both checks; `approved` = retrieval passed and grounding passed. `to_dict()` (JSON) and `format_report()` (text) |

**Relevance score.** Query terms (lowercased, stopword- and question-filler-free, lightly stemmed) are matched against each note field. Each term scores its best field weight: tags 1.0, topic 1.0, `core_stance` 0.75, category 0.5, body 0.5. Score = mean over query terms. `relevant` needs score ≥ 0.5 **and** at least one frontmatter match, so body-only matches can't pass; `weak` ≥ 0.2; below that `irrelevant`. A query with no content terms is rejected as unverifiable.

**Grounding.** Fenced code lines and headings are separated out, then prose is split into sentences (markdown bullets/emphasis stripped). Questions, abstentions ("I don't have notes on…"), and fact-free fragments of fewer than 3 content terms are skipped.

- **Hard facts** must appear in some retrieved note: inline/fenced code (whitespace-normalized, case-sensitive), quotes (case-insensitive substring), ISO dates and versions (bounded literal), numbers (normalized: `1,000` = `1000`, `2.50` = `2.5`, sign kept; with a recognized unit, the `(value, unit)` pair must match: `48kHz` = `48 kHz`, `$2.5` = `2.50 dollars`), and proper nouns/acronyms (not sentence- or clause-initial; whole-word, case-insensitive).
- **Lexical support** = the best fraction of the claim's non-numeric content terms found in one window of 3 consecutive sentences of one note. Numbers are excluded here because they're checked exactly above.
- Status: any ungrounded fact, or support < 0.35 → `unsupported`; support < 0.6 or a negation mismatch with the closest source sentence → `weak`; otherwise `supported`. Strict mode (default) fails on `weak`.
- The draft is grounded against **every retrieved note that exists and parses**, whether or not it was relevant. Relevance is judged separately. Missing notes ground nothing.

**Known limits (lexical by design):** synonyms and heavy paraphrase read as `weak`; number words ("three") and sentence-initial proper nouns aren't fact-checked; a true fact from a note that wasn't retrieved still counts as unsupported. Thresholds live in `ValidatorConfig`. An LLM-judge pass for paraphrase is a possible later layer, subject to the cost constraints.

## 10. Security

- **Secrets:** `.env`, OAuth client secret, token gitignored; templates only in git.
- **Masking:** taxonomy and folder IDs only in gitignored `config.local.yaml` / `.env`; committed files use `Category_N` placeholders; tests use placeholders too.
- **Prompt injection:** doc text is untrusted. It's passed as delimited data, the model has **no tools**, output is schema-constrained, and destination is a fixed enum. Worst case: a misfiled doc.
- **Logging:** doc IDs and outcomes only — never document content or credentials. Google client HTTP logging is suppressed unless `-v`.
- **Validator:** treats notes and drafts as untrusted text. It only reads them (YAML via `safe_load`, no code execution, no model calls) and has no write path to Drive or the filesystem. Its report echoes claim text to stdout only.

## 11. Out of scope (for now)

Scheduling, non-Doc inputs, re-ingest command for processed docs, re-ingesting edited docs, two-way sync. Tracked as a backlog in `manifest.md`.
