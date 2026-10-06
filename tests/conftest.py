"""Shared test setup: one app instance (one lifespan) for the whole test session."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("RV_DATA_DIR", tempfile.mkdtemp(prefix="rvdata-"))
os.environ.setdefault("RV_PROBES", "false")   # tests run the probes explicitly
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app import main as rvmain

    with TestClient(rvmain.app) as c:
        yield c
