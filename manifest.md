# Session Manifest

> **Protocol:** every agent (Claude Code, Gemini) and human reads this file **first** at the start of a session and updates it **last** before ending. Keep entries terse and factual.
> **Masking rule:** this file is committed to a public repo. Never record secrets, folder IDs, personal names, or the personal category names/descriptions here. Refer to categories as `Category_1..5` and point to `config.local.yaml`.

## Current State

- **Project status: ✅ OPERATIONAL** (Phases 1–4 complete) · **Multi-agent pipeline live: Agent 1 (Retriever & Router) ✅ + Agent 2 (Critic & Validator) ✅**
- **Last updated:** 2026-11-12 by Claude Code
- **Branch:** `main`, pushed to GitHub (`origin`). Migration feature and Agent 2 committed and pushed 2026-11-12.
- **Tests:** 158 passing (`pytest -q`), `ruff check` clean, 91% line coverage overall (`validator.py` 100%; `pytest --cov=digital_twin_ingest`, `pytest-cov` in dev extras). Drive and Anthropic are faked in tests; no network.
- **Agent ecosystem (both operational):**
  - **Agent 1, Retriever & Router ✅ Operational / Complete.** Implemented by the ingestion pipeline and Markdown/Drive architecture: ingestion of Google Docs, `.docx`, and text-layer PDFs (`run`, `migrate`); automatic YAML frontmatter tagging (`topic`, `tags`, `core_stance`); category-first routing into the 5 core folders (`Category_1`–`Category_5`; real names in `config.local.yaml`); native Google Drive storage that serves as the retrieval index (route to folder → match frontmatter).
  - **Agent 2, Critic & Validator ✅ Operational.** `validator.py` + `validate` CLI: audits the retrieved set and fact-checks drafts against the notes. Offline, zero spend.
  - See ARCHITECTURE "Agent map" and §9b.
- **Verified live (engineer-confirmed):** Google auth; dry runs end-to-end, including Haiku classification, frontmatter rendering, and cost logging.
- **Pre-release security sweep (2026-11-11):** all committable files and full git history scanned for personal names, category names, API keys/OAuth secrets, and `.env` values. Clean. No sensitive filenames ever committed.
- **Local-only, gitignored, NEVER commit:** `explainer_site/` (private; deployed separately), `config.local.yaml`, `.env`, `logs/`, `output/`, OAuth token/client secret (stored outside the repo).

## Phase Tracker

| Phase | Description | Status |
|---|---|---|
| 1 | Scaffolding & security | ✅ Complete |
| 2 | Architecture | ✅ Complete |
| 3 | Google auth, Inbox reading, Markdown export, dry run | ✅ Complete |
| 4 | Haiku classification, cost logging, $1 cap, frontmatter, upload, `_Processed` move | ✅ Complete |
| A1 | Agent 1: Retriever & Router (ingestion, frontmatter tagging, category routing, Drive storage) | ✅ Complete |
| A2 | Agent 2: Critic & Validator | ✅ Complete |
| — | Maintenance & feature expansion | 🔄 Ongoing (see backlog) |

## Standing Constraints (from engineer, 2026-11-11)

1. **Cost:** never Opus for this task. `claude-haiku-5-5`, `effort: low`.
2. **Cost observability:** every Anthropic response → gitignored SQLite ledger (`logs/cost_metrics.sqlite`); `costs` CLI.
3. **Spend cap:** hard stop at **$1.00 per run** (validator rejects higher values).
4. **Masking:** no personal taxonomy or personal details in source or committed docs; committed files use `Category_N`.
5. **Dry-run default:** Drive writes only with explicit `--live`.
6. **Phase gates:** propose → engineer review → implement only approved scope → update this manifest.

## Future Sessions: Maintenance & Feature Expansion

The engine is feature-complete for its original scope. Future sessions focus on maintenance or scoped feature work, each following the phase-gate protocol above.

**Active feature: archive migration** (approved 2026-11-11)
- Step A ✅ read-only inventory → gitignored `archive_manifest.csv`; engineer-reviewed include list (Word + text-layer PDF only; employer documents, admin/financial/PII, third-party documents excluded).
- Step B ✅ `migrate --manifest` built: local `.docx`/PDF extraction, manifest-authoritative dates, per-file length override, originals tagged in place (never moved), 39 new offline tests. Real-file extraction smoke test (no Claude, no writes): all included files extract cleanly.
- Step C ⏳ engineer dry run `--limit 5` → full dry run → `--live`.
- Deferred: spreadsheets, slides, oversized plain-text files, OCR for scanned PDFs.

**Active feature: Agent 2, Critic & Validator** (requested 2026-11-12)
- ✅ `validator.py`: retrieval verification (existence, frontmatter validity, routing/category match, claimed-tag check, intent relevance, missed-note report) + hallucination guardrail (hard facts: figures+units, versions, ISO dates, quotes, code, proper nouns; lexical support per passage; negation check). Strict by default.
- ✅ `validate` CLI (local `preview_dir`, `--kb DIR`, or read-only `--drive`); `DriveClient.list_markdown()`; 72 new offline tests.
- ✅ Smoke-tested read-only against local previews: faithful claims supported; an invented figure, year, and vendor rejected.
- ✅ Engineer-verified 2026-11-12. Next: tune thresholds on real queries against Agent 1's Drive folders.

**Feature backlog (unprioritized, each needs engineer approval):**
1. **Re-ingest command** for already-processed docs (e.g. `reingest --doc-id ID`): clear the `dt_ingested` marker, optionally trash the old `.md`, move the doc back to the Inbox, preserving its original created date. Today the only workaround is copying the text into a new doc, which loses the original date.
2. **Re-ingest on edit:** detect processed docs modified after ingestion and offer to refresh their `.md`.
3. **Scheduling:** run automatically (Windows Task Scheduler or a cloud job) with the same spend cap.
4. **Additional inputs:** PDFs, `.docx`, or Sheets in the Inbox.
5. **Agent 1 enhancement (optional): embedding search** over the Markdown corpus, alongside category routing + frontmatter matching, emitting `RetrievalHit` JSON for Agent 2.
6. **Agent 2 LLM-judge layer (optional):** a Haiku pass for paraphrase/synonym support on claims the lexical check marks `weak`. Must follow the cost constraints (ledger + cap).

**Maintenance checklist:**
- Keep Haiku pricing in `cost.py` defaults / `config.local.yaml` in sync with Anthropic's published rates.
- Watch for Anthropic model deprecations; migrate the model ID when Haiku 5.5 is superseded (re-check structured-output support and pricing).
- Periodically bump dependencies and re-run `pytest` + `ruff`.
- Recommended hardening: `gitleaks` pre-commit hook + GitHub secret scanning / push protection.

## Decision Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-11-11 | Python, `src/` layout, `pyproject.toml` | Standard, testable packaging. |
| 2026-11-11 | Taxonomy + folder IDs only in gitignored `config.local.yaml` / `.env` | Repo assumed public. |
| 2026-11-11 | `claude-haiku-5-5`, effort `low` | Cost constraint; lightweight classification. |
| 2026-11-11 | Structured outputs via `output_config.format` JSON schema + Pydantic re-validation | API-enforced schema; enum restricts destination. |
| 2026-11-11 | SQLite cost ledger; record every response before inspecting it; fail closed on unpriced models | No untracked spend. |
| 2026-11-11 | $1.00/run cap enforced pre-call with worst-case bound (bytes ≥ tokens, max_tokens output, higher rate card) | Cap can't be crossed even by one call. |
| 2026-11-11 | Dry run calls Claude (logged, capped) but never writes Drive | Previews show real classifications. |
| 2026-11-11 | Frontmatter strings emitted as JSON literals | Exact requested format; YAML-injection safe. |
| 2026-11-11 | Uploaded `.md` tagged `dt_source_id`; source tagged `dt_ingested`/`dt_output_id` | Idempotency + crash recovery without a local DB. |
| 2026-11-11 | Drive native Markdown export + conservative cleanup | Engineer decision. |
| 2026-11-11 | Full `drive` OAuth scope | Needed to write into pre-existing folders. |
| 2026-11-11 | `date` = created, fallback modified, never ingestion | Engineer decision. |
| 2026-11-11 | `explainer_site/` is private and gitignored; public repo = generic engine + generic README only | Keeps personal details out of the public repo. |
| 2026-11-11 | README links to the deployed explainer site but never its source | Socialize the project without publishing the site's code. |
| 2026-11-11 | Archive migration via reviewed CSV manifest; originals tagged in place, never moved | Preserve archive folder structure; human approves every file before any API call. |
| 2026-11-11 | `.docx` converted locally (python-docx), not via temporary Drive copies | Keeps dry run write-free. |
| 2026-11-11 | Manifest `doc_date` authoritative (title → file metadata → Drive modified) | Bulk upload stamped every Drive created date with the upload day. |
| 2026-11-12 | Agent 2 validator is deterministic and lexical, with no LLM | Zero spend; reproducible verdicts; an invented figure can never "sound right" to a checker. Paraphrase may read as `weak` (fail-safe). |
| 2026-11-12 | Hard facts must appear verbatim (numerically normalized) in a retrieved note; strict mode fails `weak` claims | "Zero hallucination of figures, specs, personal history" requirement. |
| 2026-11-12 | `relevant` requires a frontmatter (tags/topic/stance) match, not just body overlap | Validates Agent 1's frontmatter-matching contract. |
| 2026-11-12 | Agent 1 (Retriever & Router) = the ingestion pipeline + category-first Drive/Markdown architecture; marked Operational/Complete | Engineer decision: retrieval is served by the routed folder + frontmatter structure the pipeline produces. |
| 2026-11-12 | Draft grounded against all retrieved notes that exist, regardless of relevance | Relevance and factuality are separate verdicts; missing notes ground nothing. |

## Session Log

- **2026-11-11 (Claude Code) #1:** Repo init; scaffolding + architecture draft. Stopped for review.
- **2026-11-11 (Claude Code) #2:** Applied cost/observability/masking constraints; implemented Phase 3. 28 tests.
- **2026-11-11 (Engineer):** Google auth + Phase 3 dry run succeeded; Markdown cleanup approved.
- **2026-11-11 (Claude Code) #3:** Implemented Phase 4 (analyzer, frontmatter, Drive writes, spend cap, crash recovery, `--live`). 47 tests. First commit.
- **2026-11-11 (Claude Code) #4:** Gitignored `explainer_site/`; README links only to the deployed site. Amended the initial commit so the old README never appears in history.
- **2026-11-11 (Engineer):** Phase 4 dry runs flawless; explainer site deployed.
- **2026-11-11 (Claude Code) #5:** Pre-release security sweep (clean). Marked project Operational; added maintenance/feature backlog. Final commit for public release.
- **2026-11-11 (Engineer):** Pushed to GitHub. Live runs verified; sample outputs trashed.
- **2026-11-11 (Claude Code) #6:** Archive migration Steps A–B: inventory + reviewed manifest (24 files, est. ~$0.02), `migrate` command, 86 tests. Awaiting engineer `--limit 5` dry run.
- **2026-11-12 (Claude Code) #7:** Agent 2 (Critic & Validator): `validator.py`, `validate` CLI, `list_markdown`, 72 tests (158 total, 91% coverage). Docs + private operator manual + private explainer site updated. Security sweep of tracked + untracked files clean.
- **2026-11-12 (Engineer):** Verified archive migration and Agent 2.
- **2026-11-12 (Claude Code) #8:** Added `pytest-cov` to dev extras; committed migration + Agent 2; pushed to GitHub.
- **2026-11-12 (Claude Code) #9:** Docs reclassified Agent 1 (Retriever & Router) as Operational/Complete (the pipeline + folder architecture); README, ARCHITECTURE, manifest, private manual and explainer site updated. Security sweep clean; tests 158 passing. Staged, not committed.
