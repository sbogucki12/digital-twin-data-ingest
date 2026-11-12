import json
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
import yaml

from digital_twin_ingest.analyzer import (
    AnalysisError,
    Analyzer,
    DocAnalysis,
    FatalAnalysisError,
)
from digital_twin_ingest.frontmatter import output_filename, render_file, render_frontmatter
from digital_twin_ingest.models import SourceDoc
from tests.conftest import CATEGORIES, RUN_ID

DOC = SourceDoc(id="doc1", name="My <Doc>", created_time=None, modified_time=None)
GOOD = {"topic": "Widget Basics", "tags": ["Testing", "testing", "Edge Case!"],
        "core_stance": "Test first.", "destination_folder": "Category_2"}


class FakeMessages:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.kwargs = response, exc, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


def _response(payload=GOOD, stop_reason="end_turn", text=True):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text:
        content.append(SimpleNamespace(type="text", text=json.dumps(payload)))
    usage = SimpleNamespace(input_tokens=2000, output_tokens=300, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, cache_creation=None)
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


def _analyzer(settings, ledger, **kw):
    msgs = FakeMessages(**kw)
    return Analyzer(settings, ledger, RUN_ID, client=SimpleNamespace(messages=msgs)), msgs


def test_request_shape(settings, ledger):
    an, msgs = _analyzer(settings, ledger, response=_response())
    an.analyze(DOC, "body text")
    kw = msgs.kwargs
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["output_config"]["effort"] == "low"
    schema = kw["output_config"]["format"]["schema"]
    assert schema["properties"]["destination_folder"]["enum"] == CATEGORIES
    assert schema["additionalProperties"] is False
    assert "temperature" not in kw and "tool_choice" not in kw
    assert "<document>\nbody text\n</document>" in kw["messages"][0]["content"]
    assert "My (Doc)" in kw["messages"][0]["content"]  # title can't fake a closing tag
    for k in CATEGORIES:
        assert k in kw["system"]


def test_parses_skips_thinking_normalizes_and_logs_cost(settings, ledger):
    an, _ = _analyzer(settings, ledger, response=_response())
    out = an.analyze(DOC, "body")
    assert out.analysis.tags == ["testing", "edge-case"]
    assert out.analysis.destination_folder == "Category_2"
    expected = (2000 * 0.10 + 300 * 0.50) / 1e6
    assert out.cost_usd == pytest.approx(expected)
    assert ledger.run_total(RUN_ID) == pytest.approx(expected)


@pytest.mark.parametrize("kw,match", [
    ({"stop_reason": "refusal"}, "declined"),
    ({"stop_reason": "max_tokens"}, "max_tokens"),
    ({"text": False}, "no text"),
    ({"payload": {**GOOD, "topic": "  "}}, "invalid"),
    ({"payload": {**GOOD, "destination_folder": "Elsewhere"}}, "not in configured"),
])
def test_unusable_responses_raise_but_are_still_billed(settings, ledger, kw, match):
    an, _ = _analyzer(settings, ledger, response=_response(**kw))
    with pytest.raises(AnalysisError, match=match):
        an.analyze(DOC, "body")
    assert ledger.run_total(RUN_ID) > 0


def test_auth_error_is_fatal(settings, ledger):
    resp = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    exc = anthropic.AuthenticationError("bad key", response=resp, body=None)
    an, _ = _analyzer(settings, ledger, exc=exc)
    with pytest.raises(FatalAnalysisError):
        an.analyze(DOC, "body")


def test_worst_case_cost_bounds_max_size_doc(settings, ledger):
    an, _ = _analyzer(settings, ledger, response=_response())
    worst = an.worst_case_cost(DOC, "x" * settings.ingest.max_doc_chars)
    assert 0.10 < worst < 0.12  # ~9 max-size docs fit under the $1 cap, guaranteed


def test_unknown_model_pricing_fails_closed(settings, ledger):
    settings.file.anthropic.model = "unpriced-model"
    from digital_twin_ingest.config import ConfigError

    with pytest.raises(ConfigError):
        _analyzer(settings, ledger, response=_response())


# ---- Frontmatter -----------------------------------------------------------------

def test_frontmatter_exact_format():
    a = DocAnalysis(topic="Topic Name", tags=["tag1", "tag2"], core_stance="A standard.",
                    destination_folder="Category_1")
    assert render_frontmatter(a, date(2026, 3, 4)) == (
        '---\ntopic: "Topic Name"\ntags: ["tag1", "tag2"]\n'
        'core_stance: "A standard."\ndate: 2026-03-04\n---\n'
    )


def test_frontmatter_is_injection_safe_yaml():
    nasty = 'He said "hi": yes\\no\n---\nevil: true'
    a = DocAnalysis(topic=nasty, tags=["a"], core_stance="ÃœnÃ¯code âœ“", destination_folder="Category_1")
    text = render_file(a, date(2026, 1, 1), "body\n")
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    assert fm["topic"] == " ".join(nasty.split())  # whitespace collapsed by validator
    assert fm["core_stance"] == "ÃœnÃ¯code âœ“"
    assert "evil" not in fm


def test_output_filename():
    a = DocAnalysis(topic="Widgets: The Basics!", tags=[], core_stance="x", destination_folder="c")
    assert output_filename(a, date(2026, 1, 2)) == "2026-01-02_widgets-the-basics.md"
