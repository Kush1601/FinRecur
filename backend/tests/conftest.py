import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def backend_root() -> Path:
    return BACKEND_ROOT


@pytest.fixture
def faults_manifest() -> list[dict]:
    return json.loads((REPO_ROOT / "data" / "faults.json").read_text())["faults"]
