# Architecture — Digital Twin Ingestion Engine

Status: **Approved 2026-11-11** with cost/observability/masking constraints. Phases 3–4 implemented; Phase 4 awaiting first real-API run.

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

## 10. Security

- **Secrets:** `.env`, OAuth client secret, token gitignored; templates only in git.
- **Masking:** taxonomy and folder IDs only in gitignored `config.local.yaml` / `.env`; committed files use `Category_N` placeholders; tests use placeholders too.
- **Prompt injection:** doc text is untrusted. It's passed as delimited data, the model has **no tools**, output is schema-constrained, and destination is a fixed enum. Worst case: a misfiled doc.
- **Logging:** doc IDs and outcomes only — never document content or credentials. Google client HTTP logging is suppressed unless `-v`.

## 11. Out of scope (for now)

Scheduling, non-Doc inputs, re-ingesting edited docs, two-way sync.
