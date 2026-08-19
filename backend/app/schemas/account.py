from __future__ import annotations

from pydantic import BaseModel


class AccountContactOut(BaseModel):
    lead_id: str
    full_name: str = ""
    job_title: str = ""
    pipeline_status: str = "DISCOVERED"
    seniority_band: str = "unknown"
    champion_likelihood: float = 0.0
    champion_level: str = "unknown"
    decision_authority_signal: str = "unknown"
    role_relevance_signal: str = "unknown"
    is_decision_maker_candidate: bool = False
    is_potential_champion: bool = False


class AccountOut(BaseModel):
    company_key: str
    company_name: str
    company_domain: str = ""
    industry: str = ""
    company_size: str = ""
    contact_count: int = 0
    senior_contact_count: int = 0
    practitioner_contact_count: int = 0
    potential_champion_count: int = 0
    decision_maker_candidate_count: int = 0
    has_relevant_signal: bool = False
    contacts: list[AccountContactOut] = []
    best_contact_path: list[str] = []


class AccountListOut(BaseModel):
    campaign_id: str
    items: list[AccountOut]
    total: int


class AccountFunnelOut(BaseModel):
    campaign_id: str
    stages: dict[str, int]
