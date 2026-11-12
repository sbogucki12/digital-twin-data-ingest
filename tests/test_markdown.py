from digital_twin_ingest.markdown import IMAGE_PLACEHOLDER, clean_markdown, slugify


def test_normalizes_whitespace_and_line_endings():
    out = clean_markdown("a  \r\n\r\n\r\n\r\nb\u00a0c\u200b\n\n").text
    assert out == "a\n\nb c\n"


def test_strips_reference_style_base64_images():
    raw = "Intro\n\n![][image1]\n\nMore\n\n[image1]: <data:image/png;base64,iVBORw0KGgo=>\n"
    res = clean_markdown(raw)
    assert "base64" not in res.text
    assert IMAGE_PLACEHOLDER in res.text
    assert res.images_removed == 1


def test_strips_inline_base64_images():
    res = clean_markdown("x ![alt](data:image/jpeg;base64,/9j/4AAQ) y")
    assert res.text == f"x {IMAGE_PLACEHOLDER} y\n"
    assert res.images_removed == 1


def test_keeps_normal_images_and_links():
    raw = "![pic](https://example.com/a.png) [link](https://example.com)"
    assert clean_markdown(raw).text == raw + "\n"


def test_unescapes_midword_punctuation_only():
    res = clean_markdown("e\\.g\\. well\\-known\n\n1\\. not a list\n\n\\- not a bullet").text
    assert "e.g. well-known" in res
    assert "1\\. not a list" in res
    assert "\\- not a bullet" in res


def test_drops_h1_that_repeats_title():
    assert clean_markdown("# My Doc\n\nBody", title="My doc").text == "Body\n"
    assert clean_markdown("# Other\n\nBody", title="My doc").text == "# Other\n\nBody\n"


def test_empty_input():
    assert clean_markdown("\n\n  \n").text == ""


def test_slugify():
    assert slugify("Héllo, World: A/B Test!") == "hello-world-ab-test"
    assert slugify("!!!") == "untitled"
    assert len(slugify("x" * 200)) == 60
