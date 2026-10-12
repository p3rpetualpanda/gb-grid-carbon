import json
from pathlib import Path

import duckdb
import pytest
from tenacity import wait_none

from gridcarbon.http import ApiClient
from gridcarbon.load import prepare

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_json():
    """Load a recorded API response from tests/fixtures (see scripts/record_fixtures.py)."""

    def _load(name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def client():
    """Fast client for tests: no backoff waits, no politeness pause."""
    return ApiClient(wait=wait_none(), max_attempts=3, timeout_s=5)


@pytest.fixture
def con():
    """Fresh in-memory warehouse with all raw/meta tables."""
    c = duckdb.connect()
    prepare(c)
    yield c
    c.close()
