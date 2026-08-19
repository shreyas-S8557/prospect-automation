from __future__ import annotations

import os
from pathlib import Path

from app import pipeline_bridge  # noqa: F401  (sets sys.path + loads .env)

from pipeline.lead_store import DEFAULT_DB_PATH  # noqa: E402

REPO_ROOT = pipeline_bridge.REPO_ROOT

# The database is the SAME SQLite file the CLI pipeline already uses, so
# campaigns/leads/emails created via the CLI and via the API are one and
# the same data. Override with PROSPECT_DB_PATH for tests / isolated runs.
DB_PATH = Path(os.environ.get("PROSPECT_DB_PATH", str(DEFAULT_DB_PATH)))

# CORS: local Vite dev server + configurable extra origins.
# CORS: the frontend is a static file (see frontend/index.html — no build
# step), so it can be served from any static file server / port during
# local dev. Allow the common ones plus configurable extras.
_default_origins = (
    "http://localhost:5173,http://127.0.0.1:5173,"
    "http://localhost:8080,http://127.0.0.1:8080,"
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:5500,http://127.0.0.1:5500,"  # VS Code Live Server / `python -m http.server 5500` default
    "null"  # file:// origins send "null" -- opening index.html directly still works
)
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("PROSPECT_CORS_ORIGINS", _default_origins).split(",")
    if o.strip()
]

# Global safety switch: even if a campaign is configured for "live" sending,
# real SMTP sends are refused unless this is explicitly enabled. This is
# what keeps automated tests (and any accidental button click during
# development) from ever sending real email.
ALLOW_LIVE_SEND = os.environ.get("PROSPECT_ALLOW_LIVE_SEND", "false").lower() in (
    "1",
    "true",
    "yes",
)

LOG_LEVEL = os.environ.get("PROSPECT_LOG_LEVEL", "INFO")
