"""A clean, in-process background-job abstraction.

Deliberately NOT Redis/Celery/RQ (see PHASE 5 of the brief: "Do not
introduce Redis/Celery merely for complexity if the current project does
not need it"). This is a single-process ThreadPoolExecutor-backed job
manager, good enough for local/single-user use, and structured so it could
be swapped for Celery/RQ later without changing the service layer's call
sites (submit/get/pause/resume/cancel is the whole contract).

Job history lives in memory only (it is bookkeeping about *runs*, not
data -- the actual leads/emails a job touched are persisted in SQLite via
LeadStore exactly as before). Restarting the backend process loses job
history, not campaign data. This is documented as a known limitation.
"""
from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCancelled(Exception):
    """Raised internally by JobControl.checkpoint() to unwind a cancelled job."""


@dataclass
class Job:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    campaign_id: Optional[str] = None
    type: str = ""
    status: JobStatus = JobStatus.QUEUED
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
    # Free-form, incrementally-updated counters that don't fit the generic
    # total/processed pair above -- specifically so the frontend can show
    # separate numbers for "search queries attempted" (e.g. 6/3774),
    # "raw candidates discovered", "qualified prospects" and "target",
    # instead of the old behaviour of conflating query attempts with
    # prospect counts. See discovery_service._run_discovery_job.
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "type": self.type,
            "status": self.status.value,
            "phase": self.phase,
            "progress": round(self.progress, 4),
            "total": self.total,
            "processed": self.processed,
            "successful": self.successful,
            "failed": self.failed,
            "message": self.message,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result": self.result,
            "stats": self.stats,
        }


class JobControl:
    """Handed to worker functions so they can report real progress and
    cooperatively honor pause/resume/cancel between units of work."""

    def __init__(self, job: Job, manager: "JobManager") -> None:
        self._job = job
        self._manager = manager
        self._pause_event = threading.Event()
        self._pause_event.set()  # set == not paused
        self._cancel_event = threading.Event()

    def set_total(self, total: int, *, phase: str | None = None) -> None:
        with self._manager._lock:
            self._job.total = total
            if phase is not None:
                self._job.phase = phase

    def set_phase(self, phase: str) -> None:
        with self._manager._lock:
            self._job.phase = phase

    def update_stats(self, **kwargs: Any) -> None:
        """Merge incremental, named counters into job.stats (e.g.
        queries_done=6, queries_total=3774, raw_discovered=3,
        qualified=1, target=10). Safe to call from inside a synchronous
        callback fired deep inside the discovery pipeline."""
        with self._manager._lock:
            self._job.stats.update(kwargs)

    def advance(self, *, success: bool, message: str = "") -> None:
        """Call once per processed item -- this is what drives real
        (never faked) progress."""
        self.checkpoint()
        with self._manager._lock:
            self._job.processed += 1
            if success:
                self._job.successful += 1
            else:
                self._job.failed += 1
            if self._job.total > 0:
                self._job.progress = min(1.0, self._job.processed / self._job.total)
            if message:
                self._job.message = message

    def checkpoint(self) -> None:
        """Call between units of work: blocks while paused, raises
        JobCancelled if cancellation was requested."""
        if self._cancel_event.is_set():
            raise JobCancelled()
        if not self._pause_event.is_set():
            with self._manager._lock:
                self._job.status = JobStatus.PAUSED
            self._pause_event.wait()
            if self._cancel_event.is_set():
                raise JobCancelled()
            with self._manager._lock:
                if self._job.status == JobStatus.PAUSED:
                    self._job.status = JobStatus.RUNNING

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def cancel(self) -> None:
        self._cancel_event.set()
        self._pause_event.set()  # unblock a paused job so it can unwind


WorkerFn = Callable[[Job, JobControl], dict[str, Any]]


class JobManager:
    def __init__(self, max_workers: int = 4) -> None:
        self._jobs: dict[str, Job] = {}
        self._controls: dict[str, JobControl] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")

    def create(self, job_type: str, campaign_id: str | None, fn: WorkerFn) -> Job:
        job = Job(type=job_type, campaign_id=campaign_id, status=JobStatus.QUEUED)
        control = JobControl(job, self)
        with self._lock:
            self._jobs[job.id] = job
            self._controls[job.id] = control
        self._executor.submit(self._run, job, control, fn)
        return job

    def _run(self, job: Job, control: JobControl, fn: WorkerFn) -> None:
        with self._lock:
            job.status = JobStatus.RUNNING
            job.started_at = utc_now_iso()
        try:
            result = fn(job, control)
            with self._lock:
                job.result = result
                job.status = JobStatus.COMPLETED
                job.progress = 1.0
                job.completed_at = utc_now_iso()
        except JobCancelled:
            with self._lock:
                job.status = JobStatus.CANCELLED
                job.completed_at = utc_now_iso()
        except Exception as exc:  # noqa: BLE001 -- job errors must never crash the worker thread
            with self._lock:
                job.status = JobStatus.FAILED
                job.error = f"{exc}"
                job.message = traceback.format_exc(limit=5)
                job.completed_at = utc_now_iso()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, *, campaign_id: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if campaign_id:
            jobs = [j for j in jobs if j.campaign_id == campaign_id]
        return sorted(jobs, key=lambda j: j.started_at or "", reverse=True)

    def pause(self, job_id: str) -> Job | None:
        control = self._controls.get(job_id)
        job = self._jobs.get(job_id)
        if control is None or job is None:
            return None
        if job.status == JobStatus.RUNNING:
            control.pause()
        return job

    def resume(self, job_id: str) -> Job | None:
        control = self._controls.get(job_id)
        job = self._jobs.get(job_id)
        if control is None or job is None:
            return None
        if job.status == JobStatus.PAUSED:
            control.resume()
        return job

    def cancel(self, job_id: str) -> Job | None:
        control = self._controls.get(job_id)
        job = self._jobs.get(job_id)
        if control is None or job is None:
            return None
        if job.status in (JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.QUEUED):
            control.cancel()
        return job


# Process-wide singleton -- one job manager per backend process, matching
# the single-SQLite-file, single-process deployment model documented in
# DEVELOPMENT.md.
job_manager = JobManager()
