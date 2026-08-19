from __future__ import annotations

import shutil

from fastapi import APIRouter

from app.config import DB_PATH
from app.utils.security import provider_status

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
def health() -> dict:
    db_ok = True
    db_error = None
    try:
        from app.db import database

        with database.get_store():
            pass
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = str(exc)

    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"status": "ok" if db_ok else "error", "path": str(DB_PATH), "error": db_error},
        "node_available": shutil.which("node") is not None,
    }


@router.get("/providers")
def providers() -> dict:
    return {"providers": provider_status()}
