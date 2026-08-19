from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.email import EmailOut, EmailRejectRequest, EmailUpdate
from app.services import email_service

from pipeline.email_generation import InvalidStateTransition, NoGeneratedEmail

router = APIRouter(prefix="/api/emails", tags=["emails"])


@router.get("/{lead_id}", response_model=EmailOut)
def get_email(lead_id: str) -> EmailOut:
    result = email_service.get_email(lead_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No generated email for lead {lead_id!r}")
    return result


@router.patch("/{lead_id}", response_model=EmailOut)
def update_email(lead_id: str, payload: EmailUpdate) -> EmailOut:
    try:
        return email_service.update_email(lead_id, subject=payload.subject, body=payload.body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No generated email for lead {lead_id!r}") from exc


@router.post("/{lead_id}/approve", response_model=EmailOut)
def approve_email(lead_id: str) -> EmailOut:
    try:
        return email_service.approve_email(lead_id)
    except NoGeneratedEmail as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidStateTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{lead_id}/reject", response_model=EmailOut)
def reject_email(lead_id: str, payload: EmailRejectRequest) -> EmailOut:
    try:
        return email_service.reject_email(lead_id, reason=payload.reason)
    except NoGeneratedEmail as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidStateTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
