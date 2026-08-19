from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class EmailCandidateOut(BaseModel):
    candidate_id: str
    rank: int
    email: str
    sources: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    domain: str = ""
    domain_guessed: bool = False
    mx_status: str = "UNKNOWN"
    smtp_status: str = "NOT_CHECKED"
    score: float = 0.0
    confidence: str = "none"
    validation_status: str = "GENERATED"
    is_best: bool = False


class ProspectOut(BaseModel):
    lead_id: str
    campaign_id: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    job_title: str = ""
    company_name: str = ""
    company_domain: str = ""
    company_size: str = ""
    industry: str = ""
    location: str = ""
    linkedin_url: str = ""
    profile_summary: str = ""

    age: str = ""
    age_source: str = ""
    age_confidence: str = ""

    email: str = ""
    email_status: str = "unknown"
    email_source: str = ""
    email_confidence: str = ""

    pipeline_status: str = "DISCOVERED"
    qualification_status: str = ""

    # Evidence-based BUND / champion / role-relevance scoring (Aug 2026
    # requirements) -- parsed from Lead.qualification_evidence. None means
    # not yet scored (e.g. a lead that hasn't reached qualification yet).
    # Every non-"unknown" value inside carries its own evidence string; see
    # pipeline/qualification_scoring.py for the full contract.
    qualification_evidence: Optional[dict] = None

    created_at: str = ""
    updated_at: str = ""


class ProspectDetailOut(ProspectOut):
    qualification_reasons: list[str] = Field(default_factory=list)
    email_candidates: list[EmailCandidateOut] = Field(default_factory=list)
    generated_email: Optional[dict] = None


class ProspectUpdate(BaseModel):
    job_title: Optional[str] = None
    company_name: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None


class ProspectListOut(BaseModel):
    items: list[ProspectOut]
    page: int
    page_size: int
    total: int
