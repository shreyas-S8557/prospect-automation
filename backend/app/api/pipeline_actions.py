"""Endpoints that kick off async pipeline stages for a campaign: discovery,
qualification, email finding/validation/generation, and sending. Every one
of these returns a job_id immediately (PHASE 4/5/10 of the brief) -- none
of them block the HTTP request for the underlying work.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.config import ALLOW_LIVE_SEND
from app.schemas.email import BulkActionResult, EmailListOut, EmailOut, EmailRejectRequest, EmailUpdate
from app.schemas.job import JobCreatedOut, JobOut
from app.services import (
    campaign_service,
    discovery_service,
    email_service,
    sending_service,
)
from app.workers.jobs import job_manager

router = APIRouter(prefix="/api/campaigns", tags=["pipeline"])


def _job_created(job) -> JobCreatedOut:
    return JobCreatedOut(job_id=job.id, status=job.status.value)


def _require_campaign(campaign_id: str) -> None:
    try:
        campaign_service.get_campaign(campaign_id)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc


# -- Discovery / qualification ---------------------------------------------


@router.post("/{campaign_id}/discover", response_model=JobCreatedOut)
def discover(campaign_id: str) -> JobCreatedOut:
    _require_campaign(campaign_id)
    return _job_created(discovery_service.start_discovery(campaign_id))


@router.get("/{campaign_id}/discovery-status", response_model=list[JobOut])
def discovery_status(campaign_id: str) -> list[JobOut]:
    jobs = [j for j in job_manager.list(campaign_id=campaign_id) if j.type == "discovery"]
    return [JobOut(**j.to_dict()) for j in jobs]


@router.post("/{campaign_id}/qualify", response_model=JobCreatedOut)
def qualify(campaign_id: str) -> JobCreatedOut:
    _require_campaign(campaign_id)
    return _job_created(discovery_service.start_qualification(campaign_id))


# -- Email discovery / validation / generation ------------------------------


@router.post("/{campaign_id}/find-emails", response_model=JobCreatedOut)
def find_emails(campaign_id: str) -> JobCreatedOut:
    _require_campaign(campaign_id)
    return _job_created(email_service.start_find_emails(campaign_id))


@router.post("/{campaign_id}/validate-emails", response_model=JobCreatedOut)
def validate_emails(campaign_id: str) -> JobCreatedOut:
    _require_campaign(campaign_id)
    return _job_created(email_service.start_validate_emails(campaign_id))


@router.post("/{campaign_id}/generate-emails", response_model=JobCreatedOut)
def generate_emails(campaign_id: str) -> JobCreatedOut:
    _require_campaign(campaign_id)
    try:
        return _job_created(email_service.start_generate_emails(campaign_id))
    except email_service.CampaignTemplateMissing as exc:
        raise HTTPException(status_code=409, detail="Campaign has no outreach template") from exc


@router.get("/{campaign_id}/emails", response_model=EmailListOut)
def list_campaign_emails(
    campaign_id: str,
    review_status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> EmailListOut:
    _require_campaign(campaign_id)
    items, total = email_service.list_emails(
        campaign_id, review_status=review_status, page=page, page_size=page_size
    )
    return EmailListOut(items=items, page=page, page_size=page_size, total=total)


@router.post("/{campaign_id}/emails/approve-all", response_model=BulkActionResult)
def approve_all_emails(campaign_id: str) -> BulkActionResult:
    _require_campaign(campaign_id)
    result = email_service.bulk_approve(campaign_id)
    return BulkActionResult(**result)


@router.post("/{campaign_id}/emails/reject-all", response_model=BulkActionResult)
def reject_all_emails(campaign_id: str, payload: EmailRejectRequest) -> BulkActionResult:
    _require_campaign(campaign_id)
    result = email_service.bulk_reject(campaign_id, reason=payload.reason)
    return BulkActionResult(**result)


# -- Sending ------------------------------------------------------------


@router.post("/{campaign_id}/send", response_model=JobCreatedOut)
def send(campaign_id: str) -> JobCreatedOut:
    campaign = None
    try:
        campaign = campaign_service.get_campaign(campaign_id)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc
    if not campaign.sending_enabled:
        raise HTTPException(
            status_code=409,
            detail="Sending is not enabled for this campaign. Enable it in campaign settings first.",
        )
    # Fail fast, synchronously, for a live-mode campaign on a backend that
    # hasn't explicitly opted into live sending -- don't make the caller
    # poll a job just to discover it was refused (the job layer also
    # enforces this as defense in depth; see sending_service._resolve_sender).
    if campaign.send_mode == "live" and not ALLOW_LIVE_SEND:
        raise HTTPException(
            status_code=403,
            detail=(
                "Live sending is disabled on this backend (PROSPECT_ALLOW_LIVE_SEND is not "
                "set). This is a deliberate safety default."
            ),
        )
    try:
        return _job_created(sending_service.start_sending(campaign_id, send_mode=campaign.send_mode))
    except sending_service.SendModeNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/{campaign_id}/pause")
def pause(campaign_id: str) -> dict:
    _require_campaign(campaign_id)
    return sending_service.pause_campaign(campaign_id)


@router.post("/{campaign_id}/resume")
def resume(campaign_id: str) -> dict:
    _require_campaign(campaign_id)
    return sending_service.resume_campaign(campaign_id)


@router.post("/{campaign_id}/stop")
def stop(campaign_id: str) -> dict:
    _require_campaign(campaign_id)
    return sending_service.stop_campaign(campaign_id)


@router.get("/{campaign_id}/sending-summary")
def sending_summary(campaign_id: str) -> dict:
    _require_campaign(campaign_id)
    return sending_service.sending_summary(campaign_id)
