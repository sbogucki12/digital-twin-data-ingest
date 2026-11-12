"""Agent 2 (Critic & Validator): retrieval verification + hallucination guardrail.

Offline, deterministic. Generic placeholder content only (public repo).
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from digital_twin_ingest import validator as v
from digital_twin_ingest.__main__ import main
from digital_twin_ingest.drive import DriveClient
from digital_twin_ingest.validator import (
    KnowledgeBase,
    NoteFormatError,
    RetrievalHit,
    ValidatorConfig,
    extract_facts,
    format_report,
    parse_note,
    query_terms,
    validate,
    verify_grounding,
    verify_retrieval,
)
from tests.fakes import FakeDriveService


def note_text(topic: str, tags: list[str], stance: str, body: str, d: str = "2026-01-05") -> str:
    return (
        "---\n"
        f"topic: {json.dumps(topic)}\n"
        f"tags: {json.dumps(tags)}\n"
        f"core_stance: {json.dumps(stance)}\n"
        f"date: {d}\n"
        "---\n\n" + body
    )


HYDRATION = "Category_1/2026-01-05_astro-component-hydration-rules.md"
MASTERING = "Category_3/2026-02-10_low-end-mixing-and-mastering-chain.md"
PACING = "Category_2/2026-03-01_narrative-pacing-notes.md"
GRIDDLE = "Category_5/2026-04-01_griddle-technique.md"
BROKEN = "Category_4/broken.md"

TEXTS = {
    HYDRATION: note_text(
        "Astro Component Hydration Rules",
        ["astro", "react", "hydration", "performance"],
        "Ship zero JavaScript by default and hydrate only the interactive islands.",
        "# Rules\n\n"
        "- Use `client:visible` for below-the-fold widgets.\n"
        "- Use `client:load` only for above-the-fold controls.\n"
        "- The JavaScript budget is 50 KB per island.\n"
        "- Never hydrate static content.\n\n"
        "Tested with Astro 4.2.1 on Node 20. Benchmarks improved by 35% after the change.\n\n"
        "```js\nexport default defineConfig({ integrations: [react()] });\n```\n",
    ),
    MASTERING: note_text(
        "Low-End Mixing and Mastering Chain",
        ["mixing", "mastering", "low-end", "eq", "loudness"],
        "Carve the low end early and keep the master dynamic.",
        "High-pass everything except kick and bass at 30 Hz. "
        "Target -14 LUFS integrated loudness for streaming. "
        "Keep true peak below -1 dBTP. "
        "Sidechain the bass to the kick with 3 dB of gain reduction. "
        "The limiter goes last in the chain. "
        'My rule: "mix at low volume, master with fresh ears."',
        d="2026-02-10",
    ),
    PACING: note_text(
        "Narrative Pacing Notes",
        ["writing", "pacing", "subtext", "dialogue"],
        "Let silence and subtext carry tension instead of exposition.",
        "Act two drags in the current draft. Trim the second chase scene and let the quiet harbor "
        "chapter breathe before the reveal. Rewrite the harbor dialogue to rely on subtext.",
        d="2026-03-01",
    ),
    GRIDDLE: note_text(
        "Griddle Technique",
        ["griddle", "cooking", "maillard"],
        "Screaming-hot steel and one firm press beat any gadget.",
        "Preheat the griddle for 10 minutes on medium-high. Season with a thin layer of oil. "
        "Press once, firmly, within the first 30 seconds.",
        d="2026-04-01",
    ),
    BROKEN: "No frontmatter here, just text.",
}


@pytest.fixture
def kb() -> KnowledgeBase:
    return KnowledgeBase.from_texts(TEXTS)


# ---------------------------------------------------------------------------------------
# Note parsing
# ---------------------------------------------------------------------------------------


class TestParseNote:
    def test_valid_note(self):
        n = parse_note(HYDRATION, TEXTS[HYDRATION])
        assert n.category == "Category_1"
        assert n.topic == "Astro Component Hydration Rules"
        assert n.tags == ("astro", "react", "hydration", "performance")
        assert n.date == date(2026, 1, 5)
        assert n.body.startswith("\n# Rules")
        assert n.raw.startswith("---\n")

    def test_bom_crlf_and_string_date(self):
        text = '﻿---\r\ntopic: "T"\r\ntags: []\r\ncore_stance: ""\r\ndate: "2025-12-31"\r\n---\r\nBody'
        n = parse_note("root.md", text)
        assert n.category is None and n.date == date(2025, 12, 31) and n.body == "Body"

    def test_datetime_date_is_truncated(self):
        n = parse_note("x.md", '---\ntopic: "T"\ntags: []\ncore_stance: s\ndate: 2026-01-01 10:00:00\n---\n')
        assert n.date == date(2026, 1, 1)

    def test_explicit_category_overrides_path(self):
        assert parse_note("a/b.md", TEXTS[PACING], category="Other").category == "Other"

    @pytest.mark.parametrize(
        "text, msg",
        [
            ("no frontmatter", "missing YAML frontmatter"),
            ("---\ntopic: [unclosed\n---\n", "invalid YAML"),
            ("---\n- a list\n---\n", "not a mapping"),
            ("---\ntopic: T\ntags: x\ncore_stance: s\ndate: 2026-01-01\n---\n", "tags"),
            ("---\ntopic: ''\ntags: []\ncore_stance: s\ndate: 2026-01-01\n---\n", "topic"),
            ("---\ntopic: T\ntags: []\ndate: 2026-01-01\n---\n", "core_stance"),
            ("---\ntopic: T\ntags: []\ncore_stance: s\ndate: soon\n---\n", "date"),
            ("---\ntopic: T\ntags: []\ncore_stance: s\n---\n", "date"),
        ],
    )
    def test_invalid(self, text, msg):
        with pytest.raises(NoteFormatError, match=msg):
            parse_note("x.md", text)


# ---------------------------------------------------------------------------------------
# Knowledge base loading
# ---------------------------------------------------------------------------------------


class TestKnowledgeBase:
    def test_from_texts_records_errors(self, kb):
        assert len(kb) == 4
        assert HYDRATION in kb and BROKEN not in kb
        assert kb.errors == {BROKEN: "missing YAML frontmatter"}

    def test_duplicate_note_id(self):
        n = parse_note(PACING, TEXTS[PACING])
        kb = KnowledgeBase([n, n])
        assert len(kb) == 1 and kb.errors[PACING] == "duplicate note id"

    def test_from_directory(self, tmp_path):
        (tmp_path / "Category_1").mkdir()
        (tmp_path / "Category_1" / "a.md").write_text(TEXTS[HYDRATION], encoding="utf-8")
        (tmp_path / "root.md").write_text(TEXTS[PACING], encoding="utf-8")
        (tmp_path / "Category_1" / "bad.md").write_text("nope", encoding="utf-8")
        (tmp_path / "Category_1" / "binary.md").write_bytes(b"\xff\xfe\x00bad")
        (tmp_path / "Category_1" / "ignored.txt").write_text("x", encoding="utf-8")
        (tmp_path / "Category_1" / "dir.md").mkdir()
        kb = KnowledgeBase.from_directory(tmp_path)
        assert sorted(n.id for n in kb.notes) == ["Category_1/a.md", "root.md"]
        assert kb.get("root.md").category is None
        assert kb.errors["Category_1/bad.md"] == "missing YAML frontmatter"
        assert kb.errors["Category_1/binary.md"].startswith("unreadable")

    def test_from_directory_skips_oversized(self, tmp_path, monkeypatch):
        monkeypatch.setattr(v, "MAX_NOTE_BYTES", 10)
        (tmp_path / "big.md").write_text(TEXTS[PACING], encoding="utf-8")
        assert KnowledgeBase.from_directory(tmp_path).errors == {"big.md": "file too large"}

    def test_from_directory_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            KnowledgeBase.from_directory(tmp_path / "nope")

    def test_from_drive_is_read_only(self):
        svc = FakeDriveService(page_size=1)
        svc.add_folder("root", "Digital_Twin")
        svc.add_folder("f1", "Category_1", parent="root")
        svc.add_folder("f3", "Category_3", parent="root")
        svc.add_file("m1", "a.md", "f1", "text/markdown", TEXTS[HYDRATION].encode())
        svc.add_file("m2", "b.md", "f1", "text/markdown", TEXTS[GRIDDLE].encode())
        svc.add_file("m3", "c.md", "f3", "text/markdown", b"broken")
        svc.add_file("pdf", "d.pdf", "f3", "application/pdf", b"%PDF")
        kb = KnowledgeBase.from_drive(DriveClient(svc), {"Category_1": "f1", "Category_3": "f3"})
        assert sorted(n.id for n in kb.notes) == ["Category_1/a.md", "Category_1/b.md"]
        assert kb.get("Category_1/a.md").category == "Category_1"
        assert "Category_3/c.md" in kb.errors
        assert not svc.files().creates and not svc.files().updates

    def test_from_drive_duplicate_names(self):
        class Drive:
            def list_markdown(self, folder_id):
                return [{"id": "1", "name": "a.md"}, {"id": "2", "name": "a.md"}]

            def download(self, file_id):
                return TEXTS[PACING].encode()

        kb = KnowledgeBase.from_drive(Drive(), {"Category_2": "f"})
        assert len(kb) == 1 and kb.errors["Category_2/a.md"] == "duplicate file name in folder"

    def test_resolve(self, kb):
        assert kb.resolve(HYDRATION)[0] == HYDRATION
        assert kb.resolve(".\\" + HYDRATION.replace("/", "\\"))[0] == HYDRATION
        assert kb.resolve("2026-02-10_low-end-mixing-and-mastering-chain.md")[0] == MASTERING
        assert kb.resolve(BROKEN) == (BROKEN, None)
        assert kb.resolve("broken.md") == (BROKEN, None)
        assert kb.resolve("nope.md") == (None, None)
        assert kb.resolve("Category_9/" + HYDRATION.split("/")[1]) == (None, None)

    def test_resolve_ambiguous_filename(self):
        kb = KnowledgeBase.from_texts({"Category_1/x.md": TEXTS[PACING], "Category_2/x.md": TEXTS[PACING]})
        assert kb.resolve("x.md") == (None, None)


# ---------------------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------------------


def test_query_terms_drop_question_shape_and_stem():
    assert query_terms("What do I think about mixing the low-end?") == ("mix", "low", "end")
    assert query_terms("Tell me my rules") == ("rul",)
    assert v.content_terms("Notes noted noting 3.11 a 7") == ["not", "not", "not", "3.11", "7"]
    assert v._stem("strategies") == "strategy" and v._stem("class") == "class"


# ---------------------------------------------------------------------------------------
# 1. Retrieval verification
# ---------------------------------------------------------------------------------------


class TestRetrieval:
    def test_relevant_hit_passes(self, kb):
        r = verify_retrieval("What are my Astro hydration rules?", [HYDRATION], kb)
        assert r.passed
        (d,) = r.results
        assert d.verdict == "relevant" and d.ok and d.score == 1.0
        assert set(d.matched_terms) == {"astro", "hydration", "rul"}
        assert r.missed == []

    def test_irrelevant_hit_fails(self, kb):
        r = verify_retrieval("How loud should my mastering target be?", [GRIDDLE], kb)
        assert not r.passed
        assert r.results[0].verdict == "irrelevant"
        assert "irrelevant match" in r.results[0].issues[0]
        assert [m[0] for m in r.missed] == [MASTERING]

    def test_weak_hit_body_only(self, kb):
        # "harbor" and "chapter" only appear in the body
        r = verify_retrieval("harbor chapter rewrite", [PACING], kb)
        assert r.results[0].verdict == "weak" and not r.passed

    def test_body_only_match_is_never_relevant(self, kb):
        cfg = ValidatorConfig(relevant_threshold=0.1)
        r = verify_retrieval("harbor", [PACING], kb, config=cfg)
        assert r.results[0].verdict == "weak"  # score high enough, but no frontmatter match

    def test_missing_and_invalid_notes(self, kb):
        r = verify_retrieval("astro hydration", ["Category_1/invented.md", BROKEN], kb)
        assert [d.verdict for d in r.results] == ["missing", "invalid"]
        assert "not found" in r.results[0].issues[0]
        assert "unparseable note: missing YAML frontmatter" in r.results[1].issues[0]
        assert not r.passed

    def test_category_routing_mismatch(self, kb):
        hit = RetrievalHit(HYDRATION, category="Category_2")
        r = verify_retrieval("astro hydration", [hit], kb)
        assert not r.passed
        assert "routed to 'Category_2' but the note is filed under 'Category_1'" in r.results[0].issues

    def test_phantom_frontmatter_tags(self, kb):
        hit = {"note_id": HYDRATION, "category": "Category_1", "matched_tags": ["Astro", "vue"]}
        r = verify_retrieval("astro hydration", [hit], kb)
        assert r.results[0].issues == ["claimed frontmatter tag(s) not on the note: ['vue']"]

    def test_matched_tags_as_string_and_valid(self, kb):
        r = verify_retrieval("astro hydration", [{"note_id": HYDRATION, "matched_tags": "astro"}], kb)
        assert r.passed

    def test_routed_category_and_duplicate_warnings(self, kb):
        r = verify_retrieval("astro hydration", [HYDRATION, HYDRATION], kb, routed_category="Category_3")
        assert r.passed  # warnings don't fail
        assert r.results[0].warnings == ["outside the routed category 'Category_3'"]
        assert "retrieved more than once" in r.results[1].warnings

    def test_empty_retrieval(self, kb):
        assert not verify_retrieval("astro hydration", [], kb).passed  # relevant notes exist
        assert verify_retrieval("quantum chromodynamics", [], kb).passed  # nothing relevant exists

    def test_empty_query_raises(self, kb):
        with pytest.raises(ValueError, match="no content terms"):
            verify_retrieval("what do I think about it?", [HYDRATION], kb)

    def test_missed_is_capped_and_sorted(self, kb):
        r = verify_retrieval("mixing mastering", [], kb, config=ValidatorConfig(max_missed=0))
        assert r.missed == [] and r.passed
        assert v.score_note("mixing mastering", kb, MASTERING) == 1.0

    def test_hit_from_obj_passthrough(self):
        h = RetrievalHit("a.md")
        assert RetrievalHit.from_obj(h) is h


# ---------------------------------------------------------------------------------------
# 2. Hallucination guardrail
# ---------------------------------------------------------------------------------------


def notes(kb, *ids):
    return [kb.get(i) for i in ids]


class TestFactExtraction:
    def test_kinds(self):
        facts = extract_facts(
            'Use `client:visible` with Astro 4.2.1, released 2026-01-05, budget 50 KB, "zero JavaScript", '
            "costs $20 and 35% faster, see Node 20."
        )
        kinds = {(f.kind, f.text) for f in facts}
        assert ("code", "client:visible") in kinds
        assert ("version", "4.2.1") in kinds
        assert ("date", "2026-01-05") in kinds
        assert ("number", "50 KB") in kinds
        assert ("quote", "zero JavaScript") in kinds
        assert ("number", "$20") in kinds and ("number", "35%") in kinds
        assert ("entity", "Astro") in kinds and ("entity", "Node") in kinds
        assert ("number", "20") in kinds

    def test_sentence_initial_and_pronouns_are_not_entities(self):
        facts = extract_facts("Carve the low end: Always keep it simple. I think I'm right.")
        assert [f for f in facts if f.kind == "entity"] == []

    def test_acronyms_and_units(self):
        facts = extract_facts("LUFS target is -14 LUFS at 48 kHz, see the EQ notes.")
        ents = {f.text for f in facts if f.kind == "entity"}
        assert ents == {"LUFS", "EQ"}  # sentence-initial acronym kept; unit-after-number skipped
        assert {f.text for f in facts if f.kind == "number"} == {"-14 LUFS", "48 kHz"}

    def test_non_unit_word_after_number(self):
        assert [f.text for f in extract_facts("Press 1 time, then 2 more.") if f.kind == "number"] == ["1", "2"]


class TestGrounding:
    def test_faithful_draft_passes(self, kb):
        draft = (
            "## Your mastering chain\n\n"
            "- High-pass everything except the kick and bass at 30 Hz.\n"
            "- Target -14 LUFS integrated loudness for streaming.\n"
            "- Keep the true peak below -1 dBTP.\n"
            'Your rule is "mix at low volume, master with fresh ears".'
        )
        g = verify_grounding(draft, notes(kb, MASTERING))
        assert g.passed, format_report(v.ValidationReport(v.RetrievalReport("q", ("q",), []), g))
        assert [c.status for c in g.claims] == ["supported"] * 4
        assert all(c.source == MASTERING for c in g.claims)
        assert g.ungrounded_facts == []

    @pytest.mark.parametrize(
        "claim, issue",
        [
            ("Target -10 LUFS integrated loudness for streaming.", "ungrounded number: -10 LUFS"),
            ("Target 14 LUFS integrated loudness for streaming.", "ungrounded number: 14 LUFS"),
            ("High-pass everything except kick and bass at 30 kHz.", "ungrounded number: 30 kHz"),
            ("Sidechain the bass to the kick with 6 dB of gain reduction.", "ungrounded number: 6 dB"),
            ('Your rule: "mix loud, master quiet".', "ungrounded quote: mix loud, master quiet"),
            ("The limiter goes last in the chain on the SSL bus.", "ungrounded entity: SSL"),
        ],
    )
    def test_invented_figures_are_rejected(self, kb, claim, issue):
        g = verify_grounding(claim, notes(kb, MASTERING))
        assert not g.passed
        assert g.claims[0].status == "unsupported"
        assert issue in g.claims[0].issues

    def test_specs_versions_dates_code(self, kb):
        src = notes(kb, HYDRATION)
        ok = verify_grounding(
            "Tested with Astro 4.2.1 on Node 20. Use `client:load` only for above-the-fold controls.", src
        )
        assert ok.passed
        bad = verify_grounding("Tested with Astro 4.3.0 on Node 22.", src)
        assert {f.text for f in bad.ungrounded_facts} == {"4.3.0", "22"}
        assert not verify_grounding("Use `client:idle` for below-the-fold widgets.", src).passed
        assert verify_grounding("This note was written on 2026-01-05 about Astro hydration rules.", src)\
            .claims[0].facts[0].grounded
        assert not verify_grounding("You decided this on 2025-06-01 for Astro hydration.", src).passed

    def test_number_normalization(self):
        kb = KnowledgeBase.from_texts({
            "Category_1/n.md": note_text("Budget", ["budget"], "s",
                                         "The cap is 1,000 requests at 48 kHz and 2.50 dollars per run.")
        })
        src = kb.notes
        assert verify_grounding("The cap is 1000 requests at 48kHz and $2.5 per run.", src).passed
        assert not verify_grounding("The cap is 10000 requests per run at 48kHz.", src).passed

    def test_fabricated_personal_history(self, kb):
        g = verify_grounding("You spent three years touring with a jazz quartet before moving abroad.",
                             notes(kb, MASTERING, PACING))
        assert g.claims[0].status == "unsupported"
        assert "not supported by any retrieved note" in g.claims[0].issues[0]

    def test_partial_support_is_weak(self, kb):
        g = verify_grounding("Trim the second chase scene and add a flashback montage sequence.",
                             notes(kb, PACING))
        assert g.claims[0].status == "weak" and not g.passed
        assert verify_grounding("Trim the second chase scene and add a flashback montage sequence.",
                                notes(kb, PACING), config=ValidatorConfig(strict=False)).passed

    def test_negation_flip_is_flagged(self, kb):
        g = verify_grounding("Always hydrate static content.", notes(kb, HYDRATION))
        assert g.claims[0].status == "weak"
        assert "negation differs from the closest source sentence" in g.claims[0].issues

    def test_no_sources_means_nothing_is_grounded(self):
        g = verify_grounding("The limiter goes last in the mastering chain.", [])
        assert g.claims[0].status == "unsupported" and g.claims[0].source is None

    def test_non_claims_are_skipped(self, kb):
        draft = ("Want me to dig deeper into the chain? "
                 "I don't have notes on reverb settings. Sure thing! "
                 "There are no notes about vocal tuning in the archive.")
        g = verify_grounding(draft, notes(kb, MASTERING))
        assert g.claims == [] and g.passed and len(g.skipped) == 4

    def test_fenced_code_must_be_verbatim(self, kb):
        good = "Your config:\n```js\nexport default defineConfig({ integrations: [react()] });\n```\n"
        g = verify_grounding(good, notes(kb, HYDRATION))
        assert g.passed and g.claims[0].text == "[code block]" and g.claims[0].source == HYDRATION
        bad = "```js\nexport default defineConfig({ integrations: [vue()] });\n\n```"
        g = verify_grounding(bad, notes(kb, HYDRATION))
        assert not g.passed and g.claims[0].issues[0].startswith("ungrounded code:")
        assert verify_grounding("```\n\n```", notes(kb, HYDRATION)).claims == []

    def test_fact_found_in_second_source(self, kb):
        g = verify_grounding("Preheat the griddle for 10 minutes on medium-high.", notes(kb, MASTERING, GRIDDLE))
        assert g.passed and g.claims[0].facts[0].source == GRIDDLE

    def test_fact_only_claim(self, kb):
        g = verify_grounding("35%", notes(kb, HYDRATION))
        assert g.passed and g.claims[0].support == 1.0


# ---------------------------------------------------------------------------------------
# Integration: retrieval -> grounding -> verdict
# ---------------------------------------------------------------------------------------


class TestValidate:
    QUERY = "What loudness target do I use when mastering?"

    def test_approved_end_to_end(self, kb):
        r = validate(self.QUERY, [RetrievalHit(MASTERING, "Category_3", ("mastering",))], kb,
                     "Target -14 LUFS integrated loudness for streaming, and keep true peak below -1 dBTP.",
                     routed_category="Category_3")
        assert r.approved
        d = r.to_dict()
        assert d["approved"] and d["retrieval"]["passed"] and d["grounding"]["passed"]
        assert d["retrieval"]["results"][0]["ok"] is True
        json.dumps(d)  # serializable
        assert format_report(r).startswith("VERDICT: APPROVED")

    def test_hallucination_rejects_even_with_good_retrieval(self, kb):
        r = validate(self.QUERY, [MASTERING], kb, "Target -9 LUFS integrated loudness for streaming.")
        assert r.retrieval.passed and not r.grounding.passed and not r.approved
        text = format_report(r)
        assert "VERDICT: REJECTED" in text and "ungrounded number: -9 LUFS" in text

    def test_bad_retrieval_rejects_and_missing_notes_ground_nothing(self, kb):
        r = validate(self.QUERY, ["Category_3/invented.md", GRIDDLE], kb,
                     "Target -14 LUFS integrated loudness for streaming.")
        assert not r.approved and not r.retrieval.passed
        # grounding only sees the griddle note (the invented one doesn't exist) -> unsupported
        assert r.grounding.claims[0].status == "unsupported"
        text = format_report(r)
        assert "Relevant notes not retrieved" in text and MASTERING in text

    def test_retrieval_only(self, kb):
        r = validate(self.QUERY, [MASTERING], kb)
        assert r.grounding is None and r.approved and r.to_dict()["grounding"] is None

    def test_duplicate_hits_ground_once(self, kb):
        r = validate(self.QUERY, [MASTERING, MASTERING], kb, "The limiter goes last in the chain.")
        assert r.approved

    def test_report_formatting_edge_cases(self, kb):
        long = "The limiter goes last in the chain " + "and stays last " * 10 + "."
        r = validate("quantum chromodynamics", [], kb, long, config=ValidatorConfig(strict=False))
        text = format_report(r)
        assert "(no documents retrieved)" in text and "..." in text and "lenient" in text


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


@pytest.fixture
def kb_dir(tmp_path):
    root = tmp_path / "kb"
    for nid, text in TEXTS.items():
        p = root / nid
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


class TestCli:
    def test_approved_exit_0(self, kb_dir, tmp_path, capsys):
        draft = tmp_path / "draft.md"
        draft.write_text("Target -14 LUFS integrated loudness for streaming.", encoding="utf-8")
        code = main(["validate", "--kb", str(kb_dir), "--query", "mastering loudness target",
                     "--doc", MASTERING, "--draft", str(draft), "--category", "Category_3"])
        assert code == 0
        assert "VERDICT: APPROVED" in capsys.readouterr().out

    def test_rejected_exit_1_json_and_retrieval_file(self, kb_dir, tmp_path, capsys):
        ret = tmp_path / "retrieval.json"
        ret.write_text(json.dumps([{"note_id": MASTERING, "category": "Category_3"}]), encoding="utf-8")
        draft = tmp_path / "draft.md"
        draft.write_text("Target -6 LUFS integrated loudness for streaming.", encoding="utf-8")
        code = main(["validate", "--kb", str(kb_dir), "--query", "mastering loudness",
                     "--retrieval", str(ret), "--draft", str(draft), "--json"])
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["approved"] is False
        assert out["grounding"]["claims"][0]["status"] == "unsupported"

    def test_draft_from_stdin_lenient(self, kb_dir, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("Trim the second chase scene and add a flashback montage."))
        code = main(["validate", "--kb", str(kb_dir), "--query", "narrative pacing",
                     "--doc", PACING, "--draft", "-", "--lenient"])
        assert code == 0

    def test_default_kb_from_config(self, kb_dir, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(
            "destinations:\n  - key: Category_1\n    description: d\n"
            f"ingest:\n  preview_dir: {json.dumps(str(kb_dir))}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("INGEST_CONFIG", str(cfg))
        assert main(["validate", "--query", "astro hydration", "--doc", HYDRATION]) == 0

    def test_default_kb_without_config(self, kb_dir, monkeypatch):
        monkeypatch.chdir(kb_dir.parent)
        kb_dir.rename(kb_dir.parent / "output")
        monkeypatch.setenv("INGEST_CONFIG", "missing.yaml")
        assert main(["validate", "--query", "astro hydration", "--doc", HYDRATION]) == 0

    @pytest.mark.parametrize(
        "content, msg",
        [("not json", "cannot read retrieval file"), ('{"note_id": "x"}', "must be a JSON list"),
         ('[{"category": "x"}]', "note_id")],
    )
    def test_bad_retrieval_file_exit_2(self, kb_dir, tmp_path, capsys, content, msg):
        ret = tmp_path / "r.json"
        ret.write_text(content, encoding="utf-8")
        assert main(["validate", "--kb", str(kb_dir), "--query", "astro", "--retrieval", str(ret)]) == 2
        assert msg in capsys.readouterr().err

    def test_empty_query_and_missing_kb_exit_2(self, kb_dir, tmp_path, capsys):
        assert main(["validate", "--kb", str(kb_dir), "--query", "what about it?"]) == 2
        assert main(["validate", "--kb", str(tmp_path / "nope"), "--query", "astro"]) == 2
        err = capsys.readouterr().err
        assert "no content terms" in err and "not found" in err

    def test_drive_source(self, monkeypatch, settings, svc, capsys):
        import digital_twin_ingest.__main__ as cli
        import digital_twin_ingest.drive as drive_mod
        import digital_twin_ingest.google_auth as auth_mod

        svc.add_file("m1", "a.md", "dest0", "text/markdown", TEXTS[HYDRATION].encode())
        monkeypatch.setattr(cli, "load_settings", lambda: settings)
        monkeypatch.setattr(auth_mod, "get_credentials", lambda *a, **k: object())
        monkeypatch.setattr(drive_mod, "build_service", lambda creds: svc)
        assert main(["validate", "--drive", "--query", "astro hydration", "--doc", "Category_1/a.md"]) == 0
        assert not svc.files().creates and not svc.files().updates
