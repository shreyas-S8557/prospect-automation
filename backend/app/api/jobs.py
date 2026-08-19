from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas.job import JobOut
from app.workers.jobs import job_manager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
def list_jobs(campaign_id: str | None = Query(None)) -> list[JobOut]:
    return [JobOut(**j.to_dict()) for j in job_manager.list(campaign_id=campaign_id)]


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str) -> JobOut:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return JobOut(**job.to_dict())


@router.post("/{job_id}/pause", response_model=JobOut)
def pause_job(job_id: str) -> JobOut:
    job = job_manager.pause(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return JobOut(**job.to_dict())


@router.post("/{job_id}/resume", response_model=JobOut)
def resume_job(job_id: str) -> JobOut:
    job = job_manager.resume(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return JobOut(**job.to_dict())


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str) -> JobOut:
    job = job_manager.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return JobOut(**job.to_dict())
