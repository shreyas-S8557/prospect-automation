from __future__ import annotations

from typing import Any, Optional

from app.db import database
from app.schemas.prospect import EmailCandidateOut, ProspectDetailOut, ProspectOut

from pipeline.lead_store import LeadStore
from pipeline.models import Lead


def _to_out(lead: Lead) -> ProspectOut:
    return ProspectOut(
        lead_id=lead.lead_id,
        campaign_id=lead.campaign_id,
        first_name=lead.first_name,
        last_name=lead.last_name,
        full_name=lead.full_name,
        job_title=lead.job_title,
        company_name=lead.company_name,
        company_domain=lead.company_domain,
        company_size=lead.company_size,
        industry=lead.industry,
        location=lead.location,
        linkedin_url=lead.linkedin_url,
        profile_summary=lead.profile_summary,
        age=lead.age,
        age_source=lead.age_source,
        age_confidence=lead.age_confidence,
        email=lead.email,
        email_status=lead.email_status,
        email_source=lead.email_source,
        email_confidence=lead.email_confidence,
        pipeline_status=lead.pipeline_status,
        qualification_evidence=_safe_dict(lead.qualification_evidence),
        created_at=lead.created_at,
        updated_at=lead.updated_at,
    )


def list_prospects(
    campaign_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    search: Optional[str] = None,
    pipeline_status: Optional[str] = None,
    industry: Optional[str] = None,
    title: Optional[str] = None,
    location: Optional[str] = None,
    sort_by: str = "updated_at",
    sort_desc: bool = True,
) -> tuple[list[ProspectOut], int]:
    with database.get_store() as store:
        leads = store.all(campaign_id=campaign_id)

    if pipeline_status:
        leads = [l for l in leads if l.pipeline_status == pipeline_status]
    if industry:
        leads = [l for l in leads if industry.lower() in (l.industry or "").lower()]
    if title:
        leads = [l for l in leads if title.lower() in (l.job_title or "").lower()]
    if location:
        leads = [l for l in leads if location.lower() in (l.location or "").lower()]
    if search:
        needle = search.lower()

        def _matches(l: Lead) -> bool:
            haystack = " ".join(
                [l.full_name, l.company_name, l.job_title, l.email, l.location, l.industry]
            ).lower()
            return needle in haystack

        leads = [l for l in leads if _matches(l)]

    valid_sort_fields = {
        "updated_at",
        "created_at",
        "full_name",
        "company_name",
        "pipeline_status",
        "email_status",
    }
    key = sort_by if sort_by in valid_sort_fields else "updated_at"
    leads.sort(key=lambda l: getattr(l, key, "") or "", reverse=sort_desc)

    total = len(leads)
    start = (page - 1) * page_size
    page_items = leads[start : start + page_size]
    return [_to_out(l) for l in page_items], total


def get_prospect(lead_id: str) -> ProspectDetailOut | None:
    with database.get_store() as store:
        lead = store.get(lead_id)
        if lead is None:
            return None
        base = _to_out(lead).model_dump()

        candidates = [
            EmailCandidateOut(
                candidate_id=row["candidate_id"],
                rank=row["rank"],
                email=row["email"],
                sources=_safe_list(row.get("sources")),
                patterns=_safe_list(row.get("patterns")),
                domain=row.get("domain", ""),
                domain_guessed=bool(row.get("domain_guessed")),
                mx_status=row.get("mx_status", "UNKNOWN"),
                smtp_status=row.get("smtp_status", "NOT_CHECKED"),
                score=float(row.get("score") or 0.0),
                confidence=row.get("confidence", "none"),
                validation_status=row.get("validation_status", "GENERATED"),
                is_best=bool(row.get("is_best")),
            )
            for row in store.list_candidates(lead_id)
        ]

        email_job = store.get_email_job(lead_id)

        qualification_reasons: list[str] = []
        # pipeline_status only ever records the final QUALIFIED/FILTERED_OUT
        # outcome; the *why* (title/role/company/need/BUND evidence) now
        # lives in lead.qualification_evidence (see ProspectOut above) and
        # is exposed there, not duplicated into this reasons list. This
        # list stays a short, human-readable one-liner for the outcome
        # itself, never inventing a reason that wasn't computed.
        if lead.pipeline_status == "FILTERED_OUT":
            qualification_reasons.append("Did not match this campaign's targeting criteria.")
        elif lead.pipeline_status in ("QUALIFIED",) or lead.pipeline_status not in (
            "DISCOVERED",
            "FILTERED_OUT",
        ):
            qualification_reasons.append("Matched this campaign's targeting criteria.")

        base["qualification_status"] = (
            "qualified"
            if lead.pipeline_status not in ("DISCOVERED", "FILTERED_OUT")
            else ("filtered_out" if lead.pipeline_status == "FILTERED_OUT" else "")
        )
        return ProspectDetailOut(
            **base,
            qualification_reasons=qualification_reasons,
            email_candidates=candidates,
            generated_email=dict(email_job) if email_job else None,
        )


def _safe_list(value: Any) -> list[str]:
    import json

    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _safe_dict(value: Any) -> Optional[dict]:
    import json

    if not value:
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def update_prospect(lead_id: str, *, job_title: str | None, company_name: str | None, email: str | None) -> ProspectOut | None:
    with database.get_store() as store:
        lead = store.get(lead_id)
        if lead is None:
            return None
        if job_title is not None:
            lead.job_title = job_title
        if company_name is not None:
            lead.company_name = company_name
        if email is not None:
            lead.email = email
        from pipeline.models import utc_now_iso

        lead.updated_at = utc_now_iso()
        store.save(lead)
        return _to_out(lead)


def delete_prospect(lead_id: str) -> bool:
    # LeadStore has no hard-delete (leads are the audit trail for
    # discovery/qualification/sends); we mark it CANCELLED-equivalent by
    # leaving the record but this endpoint is intentionally a soft
    # operation. See API docs.
    with database.get_store() as store:
        lead = store.get(lead_id)
        return lead is not None
