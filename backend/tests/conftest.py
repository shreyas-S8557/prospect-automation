from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient wired to a fresh, isolated SQLite file per test -- never
    the real data/pipeline_state.db, and never touching the network or
    sending real email (sending tests always use dry_run/test mode)."""
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    monkeypatch.setenv("PROSPECT_DB_PATH", str(db_path))
    monkeypatch.setenv("PROSPECT_ALLOW_LIVE_SEND", "false")

    # Reload app.config + app.db.database so they pick up the monkeypatched
    # env var rather than a value cached from a previous test's import.
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
