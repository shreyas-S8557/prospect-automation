from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class CampaignCreate(BaseModel):
    campaign_name: str = Field(..., min_length=1, max_length=200)
    description: str = ""

    # Targeting (-> pipeline.target_config.TargetConfig)
    target_titles: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=lambda: ["United States"])
    exclude_keywords: list[str] = Field(default_factory=list)

    company_size_min: Optional[int] = None
    company_size_max: Optional[int] = None
    age_min: Optional[int] = None
    age_max: Optional[int] = None

    target_leads: int = Field(50, gt=0, description="TargetConfig.target_count")
    target_count_mode: str = Field("raw", pattern="^(raw|qualified)$")
    discovery_limit: int = Field(200, gt=0)
    qualification_threshold: str = ""

    # Outreach template (-> pipeline.campaign.Campaign)
    email_subject_template: Optional[str] = None
    email_body_template: Optional[str] = None
    email_sender_name: str = ""

    # Aug 2026: optional, VERIFIED case studies/results this campaign can
    # draw on in generated outreach. Each item: {"industries": [...],
    # "keywords": [...], "text": "..."}. Leave empty if you have none --
    # the generator will use honest, clearly-hedged hypothesis language
    # instead of fabricating one. See email_generation.select_case_study().
    case_studies: list[dict] = Field(default_factory=list)

    # Aug 2026: optional, industry-tailored problem hypotheses ("make it
    # tailored to any industry" -- beauty/wellness, legal, manufacturing,
    # anything). Each item: {"industries": [...], "keywords": [...],
    # "roles": [...], "label": "...", "phrase": "..."}. Leave empty to use
    # the generator's industry-neutral generic fallback instead.
    # See email_generation.select_pain_angle().
    pain_points: list[dict] = Field(default_factory=list)

    # Feature toggles
    email_validation_enabled: bool = True
    email_generation_enabled: bool = True
    sending_enabled: bool = False
    send_mode: str = Field("dry_run", pattern="^(dry_run|test|live)$")

    @field_validator("target_titles", "industries", "keywords", "locations", "exclude_keywords")
    @classmethod
    def _strip_empty(cls, v: list[str]) -> list[str]:
        return [item.strip() for item in v if item and item.strip()]

    @field_validator("age_min", "age_max")
    @classmethod
    def _age_at_least_18(cls, v: Optional[int]) -> Optional[int]:
        # Person-age filter, never company age (see age_min/age_max field
        # placement next to titles/industries, not company_size_min/max).
        # Blank/None means "no constraint" and is left untouched here.
        if v is not None and v < 18:
            raise ValueError("age must be >= 18")
        return v


class CampaignUpdate(BaseModel):
    campaign_name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(active|archived)$")

    target_titles: Optional[list[str]] = None
    industries: Optional[list[str]] = None
    keywords: Optional[list[str]] = None
    locations: Optional[list[str]] = None
    exclude_keywords: Optional[list[str]] = None

    company_size_min: Optional[int] = None
    company_size_max: Optional[int] = None
    age_min: Optional[int] = None
    age_max: Optional[int] = None

    target_leads: Optional[int] = None
    target_count_mode: Optional[str] = Field(None, pattern="^(raw|qualified)$")
    discovery_limit: Optional[int] = None
    qualification_threshold: Optional[str] = None

    email_subject_template: Optional[str] = None
    email_body_template: Optional[str] = None
    email_sender_name: Optional[str] = None

    case_studies: Optional[list[dict]] = None
    pain_points: Optional[list[dict]] = None

    email_validation_enabled: Optional[bool] = None
    email_generation_enabled: Optional[bool] = None
    sending_enabled: Optional[bool] = None
    send_mode: Optional[str] = Field(None, pattern="^(dry_run|test|live)$")

    @field_validator("age_min", "age_max")
    @classmethod
    def _age_at_least_18(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 18:
            raise ValueError("age must be >= 18")
        return v


class CampaignOut(BaseModel):
    campaign_id: str
    campaign_name: str
    description: str = ""
    status: str = "active"

    target_titles: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)

    company_size_min: Optional[int] = None
    company_size_max: Optional[int] = None
    age_min: Optional[int] = None
    age_max: Optional[int] = None

    target_leads: int = 0
    target_count_mode: str = "raw"
    discovery_limit: int = 200
    qualification_threshold: str = ""

    email_subject_template: str = ""
    email_body_template: str = ""
    email_sender_name: str = ""

    case_studies: list[dict] = Field(default_factory=list)
    pain_points: list[dict] = Field(default_factory=list)

    email_validation_enabled: bool = True
    email_generation_enabled: bool = True
    sending_enabled: bool = False
    send_mode: str = "dry_run"

    run_state: str = "RUNNING"
    created_at: str = ""
    updated_at: str = ""


class CampaignListOut(BaseModel):
    items: list[CampaignOut]
    page: int
    page_size: int
    total: int
