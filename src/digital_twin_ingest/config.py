"""Settings: secrets/IDs from .env, taxonomy + tuning from a gitignored YAML file.

No personal taxonomy lives in source code — destination folders come exclusively
from the local config file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

from .cost import DEFAULT_PRICING, Pricing


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


class Destination(BaseModel):
    key: str = Field(min_length=1)
    description: str = ""
    drive_folder_id: str | None = None


class AnthropicSettings(BaseModel):
    model: str = "claude-haiku-5-5"
    effort: str = "low"
    max_tokens: int = Field(default=2048, gt=0)


class IngestSettings(BaseModel):
    processed_folder_name: str = "_Processed"
    max_doc_chars: int = Field(default=200_000, gt=0)
    preview_dir: Path = Path("output")
    cost_db: Path = Path("logs/cost_metrics.sqlite")
    # Hard per-run spend cap (USD). Can be lowered in config; raising it above $1.00
    # deliberately requires a code change.
    max_cost_per_run_usd: float = Field(default=1.00, gt=0, le=1.00)


class FileConfig(BaseModel):
    destinations: list[Destination] = Field(min_length=1)
    anthropic: AnthropicSettings = AnthropicSettings()
    pricing: dict[str, Pricing] = Field(default_factory=dict)
    ingest: IngestSettings = IngestSettings()

    @field_validator("destinations")
    @classmethod
    def _unique_keys(cls, v: list[Destination]) -> list[Destination]:
        keys = [d.key for d in v]
        if len(keys) != len(set(keys)):
            raise ValueError("destination keys must be unique")
        return v


class Settings(BaseModel):
    file: FileConfig
    client_secret_file: Path
    token_file: Path
    inbox_folder_id: str
    root_folder_id: str

    @property
    def destinations(self) -> list[Destination]:
        return self.file.destinations

    @property
    def ingest(self) -> IngestSettings:
        return self.file.ingest

    @property
    def anthropic(self) -> AnthropicSettings:
        return self.file.anthropic

    def pricing_for(self, model: str) -> Pricing:
        """Fail closed: never run against a model we can't price."""
        table = {**DEFAULT_PRICING, **self.file.pricing}
        if model not in table:
            raise ConfigError(f"No pricing configured for model {model!r}; add it under `pricing:`")
        return table[model]


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name} (see .env.example)")
    return value


def _path_env(name: str, default: str) -> Path:
    return Path(os.path.expanduser(os.environ.get(name, "").strip() or default))


def load_file_config(path: Path) -> FileConfig:
    if not path.exists():
        raise ConfigError(
            f"Config file {path} not found. Copy config.example.yaml to config.local.yaml."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return FileConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"Invalid config file {path}: {exc}") from exc


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    if env_file is not None and env_file.exists():
        load_dotenv(env_file, override=False)
    file_cfg = load_file_config(_path_env("INGEST_CONFIG", "config.local.yaml"))
    return Settings(
        file=file_cfg,
        client_secret_file=_path_env(
            "GOOGLE_CLIENT_SECRET_FILE", "~/.config/digital-twin-ingest/client_secret.json"
        ),
        token_file=_path_env("GOOGLE_TOKEN_FILE", "~/.config/digital-twin-ingest/token.json"),
        inbox_folder_id=_require_env("INBOX_FOLDER_ID"),
        root_folder_id=_require_env("DIGITAL_TWIN_ROOT_FOLDER_ID"),
    )
