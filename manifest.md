# Session Manifest

> **Protocol:** every agent (Claude Code, Gemini) and human reads this file **first** at the start of a session and updates it **last** before ending. Keep entries terse and factual.
> **Masking rule:** this file is committed. Never record secrets, folder IDs, or the personal category names/descriptions here — refer to them as `Category_1..5` and point to `config.local.yaml`.

## Current State

- **Phase:** 4 code complete (classification, cost logging, $1 spend cap, frontmatter, upload, move to `_Processed`) · **awaiting first real-API dry run**
- **Last updated:** 2026-11-11 by Claude Code
- **Branch:** `main` · single initial commit (engine + docs; amended once to scrub the explainer-site README section from history). Not yet pushed.
- **Local-only, gitignored, NEVER commit:** `explainer_site/` (private), `config.local.yaml`, `.env`, `logs/`, `output/`.
- **Tests:** 47 passing (`pytest -q`), `ruff check` clean. Drive and Anthropic are faked in tests.
- **Verified live:** Google auth + Phase 3 dry run (engineer confirmed Markdown quality). **Not yet verified live:** Claude calls, uploads, moves.

## Phase Tracker

| Phase | Description | Status |
|---|---|---|
| 1 | Scaffolding & security | ✅ Done |
| 2 | Architecture | ✅ Approved |
| 3 | Google auth, Inbox reading, Markdown export, dry run | ✅ Approved after live dry run |
| 4 | Haiku classification, cost logging, $1 cap, frontmatter, upload, `_Processed` move | ✅ Code + tests · ⏳ live verification |
| 5 | Hardening: gitleaks pre-commit, GitHub secret scanning, optional scheduling | ⏳ |

## Standing Constraints (from engineer, 2026-11-11)

1. **Cost:** never Opus for this task. `claude-haiku-5-5`, `effort: low`.
2. **Cost observability:** every Anthropic response → gitignored SQLite ledger (`logs/cost_metrics.sqlite`); `costs` CLI.
3. **Spend cap:** hard stop at **$1.00 per run** (validator rejects higher values).
4. **Masking:** no personal taxonomy in source or committed docs; committed files use `Category_N`.
5. **Dry-run default:** Drive writes only with explicit `--live`.

## Pending Blockers (need engineer action)

1. **`ANTHROPIC_API_KEY`** is empty in `.env`. Engineer is setting it, then running the first dry run.

## Next Steps

1. `run --limit 3` (dry run, now calls Haiku) → review `output/<Category>/*.md` for classification quality and frontmatter; check `costs`.
2. If good: `run --limit 1 --live` → confirm the file lands in Drive and the source moves to `Inbox/_Processed`; then full `run --live`.
3. Before first push: gitleaks pre-commit + GitHub secret scanning.

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
| 2026-11-11 | `explainer_site/` is private and gitignored; public repo = generic engine + generic README only | Engineer decision; keeps personal details out of the public repo. |
| 2026-11-11 | README links to the deployed explainer site (Netlify) but never its source | Socialize the project without publishing the site's code. |

## Session Log

- **2026-11-11 (Claude Code) #1:** Repo init; scaffolding + architecture draft. Stopped for review.
- **2026-11-11 (Claude Code) #2:** Applied cost/observability/masking constraints; implemented Phase 3. 28 tests.
- **2026-11-11 (Engineer):** Google auth + Phase 3 dry run succeeded; Markdown cleanup approved.
- **2026-11-11 (Claude Code) #3:** Implemented Phase 4 (analyzer, frontmatter, Drive writes, spend cap, crash recovery, `--live`). 47 tests. First commit.
- **2026-11-11 (Claude Code) #4:** Gitignored `explainer_site/` (private, local-only); README now links only to the deployed site. Added a local operator manual. Amended the initial commit so the old README never appears in history. Pending: engineer's first real-API dry run.
