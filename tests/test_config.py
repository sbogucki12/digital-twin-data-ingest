import pytest

from digital_twin_ingest.config import ConfigError, load_file_config, load_settings


def _write(tmp_path, text):
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_example_config_is_valid_and_generic():
    cfg = load_file_config(__import__("pathlib").Path("config.example.yaml"))
    assert [d.key for d in cfg.destinations] == [f"Category_{i}" for i in range(1, 6)]
    assert cfg.anthropic.model == "claude-haiku-5-5"


def test_duplicate_keys_rejected(tmp_path):
    p = _write(tmp_path, "destinations:\n  - key: A\n  - key: A\n")
    with pytest.raises(ConfigError):
        load_file_config(p)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_file_config(tmp_path / "nope.yaml")


def test_load_settings_requires_folder_ids(tmp_path, monkeypatch):
    p = _write(tmp_path, "destinations:\n  - key: A\n")
    monkeypatch.setenv("INGEST_CONFIG", str(p))
    monkeypatch.delenv("INBOX_FOLDER_ID", raising=False)
    monkeypatch.setenv("DIGITAL_TWIN_ROOT_FOLDER_ID", "root")
    with pytest.raises(ConfigError, match="INBOX_FOLDER_ID"):
        load_settings(env_file=None)


def test_pricing_fails_closed_for_unknown_model(settings):
    assert settings.pricing_for("claude-haiku-5-5").input_per_mtok == 0.10
    with pytest.raises(ConfigError):
        settings.pricing_for("some-unpriced-model")
