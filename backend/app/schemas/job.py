from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class JobOut(BaseModel):
    id: str
    campaign_id: Optional[str] = None
    type: str
    status: str
    phase: str = ""
    progress: float = 0.0
    total: int = 0
    processed: int = 0
    successful: int = 0
    failed: int = 0
    message: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    # Incremental counters (queries_done/queries_total/raw_discovered/
    # qualified/filtered_out/target) reported live during a discovery run --
    # see workers/jobs.py Job.stats and JobControl.update_stats().
    stats: dict[str, Any] = {}


class JobCreatedOut(BaseModel):
    job_id: str
    status: str
