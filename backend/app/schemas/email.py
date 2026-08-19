from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class EmailOut(BaseModel):
    job_id: str
    lead_id: str
    campaign_id: str
    prospect_name: str = ""
    prospect_company: str = ""
    to_email: str = ""
    subject: str = ""
    body: str = ""
    review_status: str = "PENDING"
    edited: bool = False
    rejection_reason: str = ""
    generated_at: str = ""
    reviewed_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    send_status: Optional[str] = None
    # Aug 2026: debugging/detail-view metadata (hook_type, evidence_used,
    # evidence_sources, personalization_confidence, cta_type, stage,
    # email_quality_score). Deliberately NOT rendered in the main email
    # card by default -- the frontend shows it behind a "Details" toggle.
    metadata: dict = {}


class EmailUpdate(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None


class EmailRejectRequest(BaseModel):
    reason: str = ""


class EmailListOut(BaseModel):
    items: list[EmailOut]
    page: int
    page_size: int
    total: int


class BulkActionResult(BaseModel):
    succeeded: list[str]
    failed: list[str]
