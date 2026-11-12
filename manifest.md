# Session Manifest

> **Protocol:** every agent (Claude Code, Gemini) and human reads this file **first** at the start of a session and updates it **last** before ending. Keep entries terse and factual.
> **Masking rule:** this file is committed to a public repo. Never record secrets, folder IDs, personal names, or the personal category names/descriptions here. Refer to categories as `Category_1..5` and point to `config.local.yaml`.

## Current State

- **Project status: ✅ OPERATIONAL** (Phases 1–4 complete; v0.1.0)
- **Last updated:** 2026-11-11 by Claude Code
- **Branch:** `main` · ready for first push to GitHub (engineer linking remote).
- **Tests:** 47 passing (`pytest -q`), `ruff check` clean. Drive and Anthropic are faked in tests; no network.
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

**Feature backlog (unprioritized, each needs engineer approval):**
1. **Re-ingest command** for already-processed docs (e.g. `reingest --doc-id ID`): clear the `dt_ingested` marker, optionally trash the old `.md`, move the doc back to the Inbox, preserving its original created date. Today the only workaround is copying the text into a new doc, which loses the original date.
2. **Re-ingest on edit:** detect processed docs modified after ingestion and offer to refresh their `.md`.
3. **Scheduling:** run automatically (Windows Task Scheduler or a cloud job) with the same spend cap.
4. **Additional inputs:** PDFs, `.docx`, or Sheets in the Inbox.
5. **RAG layer:** embeddings/search over the digital twin's Markdown corpus.

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

## Session Log

- **2026-11-11 (Claude Code) #1:** Repo init; scaffolding + architecture draft. Stopped for review.
- **2026-11-11 (Claude Code) #2:** Applied cost/observability/masking constraints; implemented Phase 3. 28 tests.
- **2026-11-11 (Engineer):** Google auth + Phase 3 dry run succeeded; Markdown cleanup approved.
- **2026-11-11 (Claude Code) #3:** Implemented Phase 4 (analyzer, frontmatter, Drive writes, spend cap, crash recovery, `--live`). 47 tests. First commit.
- **2026-11-11 (Claude Code) #4:** Gitignored `explainer_site/`; README links only to the deployed site. Amended the initial commit so the old README never appears in history.
- **2026-11-11 (Engineer):** Phase 4 dry runs flawless; explainer site deployed.
- **2026-11-11 (Claude Code) #5:** Pre-release security sweep (clean). Marked project Operational; added maintenance/feature backlog. Final commit for public release.
