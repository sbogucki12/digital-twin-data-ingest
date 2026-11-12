# digital-twin-data-ingest

A two-agent pipeline for a personal **digital twin**. **Agent 1 (Retriever & Router)** ingests Google Docs, Word files, and PDFs into structured, category-routed Markdown in Google Drive. **Agent 2 (Critic & Validator)** audits retrieval and fact-checks answers drafted from those notes.

## Objective

Notes, drafts, and position pieces get written in Google Docs. This project automates moving them into the digital twin knowledge base:

1. **Read** new Google Docs from a designated Drive `Inbox` folder.
2. **Convert** each doc to clean Markdown (Drive's native Markdown export + a light cleanup pass).
3. **Analyze** it with the Anthropic API (Claude Haiku), which returns structured JSON:
   `topic`, `tags`, `core_stance`, and `destination_folder`.
4. **Prepend** YAML frontmatter (`topic`, `tags`, `core_stance`, `date`).
5. **Upload** the `.md` file into one of the configured top-level category folders of the `Digital_Twin` Drive, then move the source doc to `Inbox/_Processed`.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design and [`manifest.md`](manifest.md) for current status.

## How this project is built: Gemini ⇄ Claude Code

Development is an intentional, reviewed loop between two AI agents and a human engineer — not autonomous iteration.

| Role | Responsibility |
|---|---|
| **Engineer (human)** | Owns requirements, approves every phase gate, holds all credentials. |
| **Gemini** | Strategy and specification: drafts phase prompts, reviews architecture and output, challenges assumptions. |
| **Claude Code** | Implementation inside this repo: scaffolding, code, tests, and keeping `manifest.md` current. |

The cycle:

1. **Specify** — the engineer (often with Gemini) writes a scoped phase prompt.
2. **Propose** — Claude Code presents an approach and scaffolding *before* writing core logic.
3. **Review** — the engineer (and/or Gemini) reviews, answers open questions, approves or redirects.
4. **Implement** — Claude Code builds only the approved scope, with tests.
5. **Record** — `manifest.md` is updated with state, blockers, and next steps so any agent or human can resume cold.

`manifest.md` is the single source of truth for handoffs. Any agent starting a session reads it first and updates it last.

## Security posture & architecture masking

This repository is written on the assumption that it **is or will be public**.

- **No credentials** are ever committed: `.env`, OAuth client secrets, and tokens are gitignored (tokens default to a path outside the repo).
- **No personal taxonomy** is in the source code or committed docs. The destination folder structure is supplied entirely by a gitignored `config.local.yaml`. The committed `config.example.yaml` uses generic placeholders (`Category_1` … `Category_5`).
- **No internal metadata** (Drive folder IDs) is committed; IDs live in `.env`.
- **No spend data** is committed; the cost ledger lives in gitignored `logs/`.
- Document content is treated as **untrusted input** to the LLM (see ARCHITECTURE §Security).

## Cost control & observability

- Model: **`claude-haiku-5-5`** — the most cost-efficient current Claude model with structured-output support, run at `effort: low`.
- Every Anthropic API response's token usage is recorded to a local SQLite ledger (`logs/cost_metrics.sqlite`, gitignored) with an estimated USD cost per call and per run.
- View spend: `python -m digital_twin_ingest costs`
- **Hard spend cap: $1.00 per run.** Before each call, a worst-case cost is computed; if it could push the run past the cap, the run stops and remaining docs stay in the Inbox. The cap can be lowered in config but not raised above $1.00 without a code change.
- Pricing is configurable in `config.local.yaml`; the app refuses to call a model it has no pricing for.
- Oversized docs are skipped (never silently truncated) to cap per-doc cost.
- Base64 images embedded by Google's export are stripped before analysis (they'd be billed as tokens).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env                       # fill in folder IDs + API key
Copy-Item config.example.yaml config.local.yaml   # replace Category_N with your folders
```

`config.local.yaml` shape (placeholders shown; each `key` must exactly match a folder name under the Digital_Twin root):

```yaml
destinations:
  - key: "Category_1"
    description: "What belongs in category 1 (used in the classification prompt)."
  - key: "Category_2"
    description: "..."
  # ... one entry per top-level category folder
```

Google: create a Cloud project, enable the **Google Drive API**, configure the OAuth consent screen (add yourself as a test user), create an OAuth **Desktop app** client, and save its JSON to the path in `GOOGLE_CLIENT_SECRET_FILE` (outside the repo by default).

## Usage

```powershell
python -m digital_twin_ingest auth                 # one-time browser consent; verifies folder IDs
python -m digital_twin_ingest run --limit 3        # DRY RUN (default): classify + preview, no Drive writes
python -m digital_twin_ingest run --doc-id <ID>    # dry run a single doc
python -m digital_twin_ingest run --live           # upload .md files + move sources to Inbox/_Processed
python -m digital_twin_ingest costs                # estimated API spend per run
```

### Migrating existing files (manifest-driven)

For bulk-migrating an existing Drive folder (Google Docs, `.docx`, and PDFs with a text layer), build a reviewed CSV manifest and run:

```powershell
python -m digital_twin_ingest migrate --manifest archive_manifest.csv --limit 5   # dry run
python -m digital_twin_ingest migrate --manifest archive_manifest.csv --live      # upload + tag
```

- Only rows with `decision=include` are processed; the manifest's `doc_date` is used as the note date, and an optional `max_chars_override` raises the length limit for that row only.
- `.docx` is converted to Markdown locally (headings, lists, bold/italic, tables); PDF text is extracted locally. Nothing is downloaded to disk.
- **Originals are never moved or edited.** Live mode only adds a hidden `dt_ingested` tag, so re-runs skip them. Files changed since the manifest was built are skipped.
- Results are written to `<manifest>_results.csv`. Manifests list personal file names: keep them gitignored.

**Dry run is the default.** It runs the full pipeline — including the (cost-logged, capped) Claude call — and writes previews to `output/<Category>/`, but makes **no** Drive changes. Only `--live` writes to Drive. Re-running is safe: processed docs are skipped, and an interrupted live run resumes without re-calling Claude or duplicating uploads.

## Multi-agent pipeline: Agent 1 (Retriever & Router) + Agent 2 (Critic & Validator)

Two agents make up the active pipeline:

| Agent | What it does | Implemented by | Status |
|---|---|---|---|
| **Agent 1: Retriever & Router** | Ingests Google Docs, Word files, and text-layer PDFs; tags each note with YAML frontmatter (`topic`, `tags`, `core_stance`, `date`); routes it category-first into one of the 5 configured category folders (`Category_1` … `Category_5`); stores it natively in Google Drive. Retrieval runs over that structure: route to the category folder, then match on frontmatter. | `run`, `migrate`, `analyzer.py`, `frontmatter.py`, `drive.py` (the ingestion engine described above) | ✅ Operational |
| **Agent 2: Critic & Validator** | Audits the notes retrieved for a query (right folder, real tags, real intent match) and fact-checks a drafted answer against the notes' exact text | `validator.py`, `validate` | ✅ Operational |

Flow: Agent 1 builds and organizes the knowledge base. A question is routed to its category folder and matched against note frontmatter. The retrieved notes plus a drafted answer go to Agent 2, which approves or rejects them with per-note and per-claim reasons.

### Agent 2 in detail

Agent 2 is **deterministic and offline**: no model calls, no spend, no network (the optional Drive loader is read-only). It runs two checks:

1. **Retrieval verification.** Every retrieved note must exist in the knowledge base, parse cleanly, be filed in the category Agent 1 claims, carry the frontmatter tags Agent 1 says it matched, and match the query's intent (a weighted overlap of query terms against tags, topic, `core_stance`, category, and body). It also lists strong notes Agent 1 *missed*.
2. **Hallucination guardrail.** Each sentence of the draft is a claim. Its **hard facts** must appear in a retrieved note: figures with units (`-14 LUFS` ≠ `14 LUFS`, `30 Hz` ≠ `30 kHz`), versions, ISO dates, quoted text, inline and fenced code, and proper nouns. The remaining words must be supported by a single passage of a single note. A negation mismatch with the closest source sentence is flagged too. Questions and "I don't have notes on that" abstentions are skipped.

A claim is `supported`, `weak` (partially supported), or `unsupported`. In the default **strict** mode, any `weak` or `unsupported` claim rejects the draft; `--lenient` only rejects `unsupported` ones. The checks are lexical by design: a faithful paraphrase may be sent back for revision, but an invented number is always caught.

```powershell
python -m digital_twin_ingest validate --query "What loudness target do I master to?" `
    --doc Category_3/2026-02-10_mastering-chain.md --draft draft.md          # local preview_dir
python -m digital_twin_ingest validate --query "..." --retrieval agent1.json --draft - --json
python -m digital_twin_ingest validate --drive --query "..." --doc <file>.md   # read KB from Drive
```

`--retrieval` takes the retrieved set as a JSON list of `{"note_id", "category"?, "matched_tags"?}`. Exit code `0` = approved, `1` = rejected, `2` = bad input.

## Project explainer site

A companion single-page site explains the project for a general audience: the problem (a Drive full of unsorted notes), the five-step pipeline, and the engineering choices behind it (schema-locked output, the pre-flight spend cap, dry-run by default, crash-safe idempotency). It's deployed separately; its source is intentionally not part of this repository.

[View the Project Explainer Site](https://bogoodski-digital-twin.netlify.app)

## Development

```powershell
pytest -q
ruff check src tests
```

Tests use in-memory fakes for Drive and the Anthropic client and never touch the network. Coverage: `pytest --cov=digital_twin_ingest` (`pytest-cov` is in the `dev` extras).
