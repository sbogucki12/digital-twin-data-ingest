from __future__ import annotations

from pathlib import Path

import pytest

from digital_twin_ingest.config import Destination, FileConfig, IngestSettings, Settings
from digital_twin_ingest.cost import CostLedger
from tests.fakes import FakeAnalyzer, FakeDriveService

# Generic placeholders only — tests are committed to a public repo.
CATEGORIES = [f"Category_{i}" for i in range(1, 6)]
RUN_ID = "test-run"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        file=FileConfig(
            destinations=[Destination(key=k, description=f"desc {k}") for k in CATEGORIES],
            ingest=IngestSettings(preview_dir=tmp_path / "output", cost_db=tmp_path / "logs" / "c.sqlite"),
        ),
        client_secret_file=tmp_path / "client_secret.json",
        token_file=tmp_path / "token.json",
        inbox_folder_id="inbox",
        root_folder_id="root",
    )


@pytest.fixture
def svc() -> FakeDriveService:
    s = FakeDriveService(page_size=2)
    s.add_folder("inbox", "Inbox")
    s.add_folder("root", "Digital_Twin")
    for i, k in enumerate(CATEGORIES):
        s.add_folder(f"dest{i}", k, parent="root")
    return s


@pytest.fixture
def ledger(settings):
    with CostLedger(settings.ingest.cost_db) as led:
        yield led


@pytest.fixture
def analyzer(ledger) -> FakeAnalyzer:
    return FakeAnalyzer(ledger, RUN_ID)
