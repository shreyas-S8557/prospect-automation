"""Database access for the backend.

Two things live in the SAME SQLite file (app.config.DB_PATH):

1. The existing pipeline tables (leads, campaigns, email_candidates,
   email_jobs, email_sends, campaign_controls, suppressed_contacts) --
   owned entirely by scripts/pipeline/lead_store.py, untouched here.

2. One small, additive, backend-owned table (`app_campaign_configs`) that
   stores the *targeting* half of a campaign (titles/industries/keywords/
   locations/company size/age/target counts/feature toggles) as JSON --
   i.e. the TargetConfig a campaign was built from, plus which pipeline
   stages are enabled for it. The existing `campaigns` table already
   covers the *template* half (subject/body/sender) via campaign.py; this
   table is the missing piece needed so the campaign builder UI has
   somewhere to persist targeting criteria without the user hand-editing
   JSON files on disk, per PHASE 3/PHASE 14 of the brief. It is purely
   additive -- lead_store.py's schema, and every existing CLI script, are
   completely unaware of it and unaffected by it.

We do not use an ORM: the existing project intentionally uses raw
sqlite3 (see lead_store.py's docstring) and this stays consistent with
that choice rather than introducing SQLAlchemy for one extra table.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import DB_PATH
from pipeline.lead_store import LeadStore

_APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_campaign_configs (
    campaign_id TEXT PRIMARY KEY,
    target_config_json TEXT NOT NULL DEFAULT '{}',
    discovery_limit INTEGER NOT NULL DEFAULT 200,
    qualification_threshold TEXT NOT NULL DEFAULT '',
    email_validation_enabled INTEGER NOT NULL DEFAULT 1,
    email_generation_enabled INTEGER NOT NULL DEFAULT 1,
    sending_enabled INTEGER NOT NULL DEFAULT 0,
    send_mode TEXT NOT NULL DEFAULT 'dry_run',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

# A single process-wide write lock. SQLite (in WAL mode, which LeadStore
# already enables) supports concurrent readers fine, but serializing writes
# from Python avoids "database is locked" retries entirely for a
# single-process local backend -- simple and sufficient at this scale.
_WRITE_LOCK = threading.Lock()


def init_app_tables() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.executescript(_APP_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# Also run at import time (not just FastAPI startup) so this module works
# correctly under test clients / scripts that never fire the ASGI startup
# event, and so LeadStore's own schema (created lazily on first
# LeadStore(...) construction) exists too.
init_app_tables()


@contextmanager
def get_store() -> Iterator[LeadStore]:
    """One pipeline LeadStore per call, closed on exit -- same pattern the
    CLI scripts already use (`with LeadStore(...) as store:`)."""
    store = LeadStore(DB_PATH)
    try:
        yield store
    finally:
        store.close()


@contextmanager
def _app_cursor() -> Iterator[sqlite3.Cursor]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def get_campaign_config(campaign_id: str) -> dict[str, Any] | None:
    with _app_cursor() as cur:
        cur.execute(
            "SELECT * FROM app_campaign_configs WHERE campaign_id = ?",
            (campaign_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        data = dict(row)
        data["target_config"] = json.loads(data.pop("target_config_json") or "{}")
        return data


def save_campaign_config(campaign_id: str, row: dict[str, Any]) -> None:
    payload = dict(row)
    payload["campaign_id"] = campaign_id
    payload["target_config_json"] = json.dumps(payload.pop("target_config", {}))
    columns = [
        "campaign_id",
        "target_config_json",
        "discovery_limit",
        "qualification_threshold",
        "email_validation_enabled",
        "email_generation_enabled",
        "sending_enabled",
        "send_mode",
        "created_at",
        "updated_at",
    ]
    values = [payload.get(c) for c in columns]
    placeholders = ", ".join("?" for _ in columns)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "campaign_id")
    with _WRITE_LOCK, _app_cursor() as cur:
        cur.execute(
            f"INSERT INTO app_campaign_configs ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(campaign_id) DO UPDATE SET {update_clause}",
            values,
        )


def delete_campaign_config(campaign_id: str) -> None:
    with _WRITE_LOCK, _app_cursor() as cur:
        cur.execute("DELETE FROM app_campaign_configs WHERE campaign_id = ?", (campaign_id,))
