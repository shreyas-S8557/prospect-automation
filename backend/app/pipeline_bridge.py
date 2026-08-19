"""Makes the existing `pipeline` package (scripts/pipeline) importable from
the backend without moving or duplicating a single line of it.

CRITICAL RULE: nothing in scripts/pipeline is rewritten. This module's only
job is path plumbing so `import pipeline.xxx` works the same way it already
does for scripts/*.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Load .env / ~/.hermes/.env exactly like every CLI entry point already does,
# once, at import time.
from pipeline.config import load_env  # noqa: E402

load_env()
