from __future__ import annotations

from typing import Any

from app.db import database
from app.schemas.email import EmailOut
from app.workers.jobs import Job, JobControl, job_manager

from pipeline import campaign as campaign_pipeline
from pipeline import email_generation
from pipeline import email_validation
from pipeline.email_discovery import process_lead_email
from pipeline.lead_store import LeadStore
from pipeline.models import PipelineStatus


class CampaignTemplateMissing(LookupError):
    pass


def _load_campaign(store: LeadStore, campaign_id: str) -> campaign_pipeline.Campaign:
    template = campaign_pipeline.load_campaign(store, campaign_id)
    if template is None:
        raise CampaignTemplateMissing(campaign_id)
    return template


# --------------------------------------------------------------------------
# find-emails / validate-emails / generate-emails: each is a job that loops
# per-lead over the existing per-lead pipeline functions, for real progress.
# --------------------------------------------------------------------------


def _run_find_emails(job: Job, ctl: JobControl, campaign_id: str) -> dict[str, Any]:
    ctl.set_phase("email_discovery")
    with database.get_store() as store:
        pending = store.list_by_status(PipelineStatus.QUALIFIED, campaign_id=campaign_id)
        ctl.set_total(len(pending), phase="email_discovery")
        found = not_found = 0
        for lead in pending:
            updated = process_lead_email(store, lead)
            ok = updated.status == PipelineStatus.EMAIL_CANDIDATES_FOUND
            found += int(ok)
            not_found += int(not ok)
            ctl.advance(success=ok, message=updated.full_name or updated.lead_id)
    return {"email_candidates_found": found, "email_not_found": not_found}


def start_find_emails(campaign_id: str) -> Job:
    return job_manager.create(
        "email_discovery", campaign_id, lambda job, ctl: _run_find_emails(job, ctl, campaign_id)
    )


def _run_validate_emails(job: Job, ctl: JobControl, campaign_id: str) -> dict[str, Any]:
    ctl.set_phase("email_validation")
    with database.get_store() as store:
        pending = store.list_by_status(
            PipelineStatus.EMAIL_CANDIDATES_FOUND, campaign_id=campaign_id
        )
        ctl.set_total(len(pending), phase="email_validation")
        validated = not_found = 0
        for lead in pending:
            updated = email_validation.validate_and_select_email(store, lead)
            ok = updated.status == PipelineStatus.EMAIL_VALIDATED
            validated += int(ok)
            not_found += int(not ok)
            ctl.advance(success=ok, message=updated.full_name or updated.lead_id)
    return {"email_validated": validated, "email_not_found": not_found}


def start_validate_emails(campaign_id: str) -> Job:
    return job_manager.create(
        "email_validation",
        campaign_id,
        lambda job, ctl: _run_validate_emails(job, ctl, campaign_id),
    )


def _run_generate_emails(job: Job, ctl: JobControl, campaign_id: str) -> dict[str, Any]:
    ctl.set_phase("email_generation")
    with database.get_store() as store:
        template = _load_campaign(store, campaign_id)
        pending = store.list_by_status(PipelineStatus.EMAIL_VALIDATED, campaign_id=campaign_id)
        ctl.set_total(len(pending), phase="email_generation")
        generated = failed = 0
        failures: list[dict[str, str]] = []
        for lead in pending:
            _, reason, _outreach = email_generation._render_and_check(template, lead)
            updated = email_generation.generate_email_for_lead(store, lead, template)
            ok = updated.status == PipelineStatus.EMAIL_GENERATED
            generated += int(ok)
            if not ok:
                failed += 1
                failures.append({"lead_id": lead.lead_id, "reason": reason})
            ctl.advance(success=ok, message=updated.full_name or updated.lead_id)
    return {"generated": generated, "failed": failed, "failures": failures}


def start_generate_emails(campaign_id: str) -> Job:
    return job_manager.create(
        "email_generation",
        campaign_id,
        lambda job, ctl: _run_generate_emails(job, ctl, campaign_id),
    )


# --------------------------------------------------------------------------
# Email CRUD / review (synchronous -- these are fast, single-row operations)
# --------------------------------------------------------------------------


def _to_email_out(store: LeadStore, row: dict[str, Any]) -> EmailOut:
    lead = store.get(row["lead_id"])
    send_row = store.get_email_send(row["lead_id"])
    import json

    try:
        metadata = json.loads(row.get("metadata_json") or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
    except (TypeError, ValueError):
        metadata = {}
    return EmailOut(
        job_id=row["job_id"],
        lead_id=row["lead_id"],
        campaign_id=row["campaign_id"],
        prospect_name=lead.full_name if lead else "",
        prospect_company=lead.company_name if lead else "",
        to_email=lead.email if lead else "",
        subject=row["subject"],
        body=row["body"],
        review_status=row["review_status"],
        edited=bool(row["edited"]),
        rejection_reason=row["rejection_reason"],
        generated_at=row["generated_at"],
        reviewed_at=row["reviewed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        send_status=send_row["send_status"] if send_row else None,
        metadata=metadata,
    )


def list_emails(
    campaign_id: str, *, review_status: str | None = None, page: int = 1, page_size: int = 50
) -> tuple[list[EmailOut], int]:
    with database.get_store() as store:
        rows = store.list_email_jobs(campaign_id=campaign_id, review_status=review_status)
        total = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start : start + page_size]
        return [_to_email_out(store, r) for r in page_rows], total


def get_email(lead_id: str) -> EmailOut | None:
    with database.get_store() as store:
        row = store.get_email_job(lead_id)
        if row is None:
            return None
        return _to_email_out(store, row)


def update_email(lead_id: str, *, subject: str | None, body: str | None) -> EmailOut:
    with database.get_store() as store:
        email_generation.edit_email_job(store, lead_id, subject=subject, body=body)
        row = store.get_email_job(lead_id)
        return _to_email_out(store, row)


def approve_email(lead_id: str) -> EmailOut:
    with database.get_store() as store:
        email_generation.approve_email(store, lead_id)
        row = store.get_email_job(lead_id)
        return _to_email_out(store, row)


def reject_email(lead_id: str, *, reason: str = "") -> EmailOut:
    with database.get_store() as store:
        email_generation.reject_email(store, lead_id, reason=reason)
        row = store.get_email_job(lead_id)
        return _to_email_out(store, row)


def bulk_approve(campaign_id: str) -> dict[str, list[str]]:
    with database.get_store() as store:
        pending_ids = [
            r["lead_id"]
            for r in store.list_email_jobs(
                campaign_id=campaign_id, review_status=email_generation.REVIEW_PENDING
            )
        ]
        result = email_generation.bulk_approve(store, pending_ids)
        return {
            "succeeded": result.get("approved", []),
            "failed": [item["lead_id"] for item in result.get("failed", [])],
        }


def bulk_reject(campaign_id: str, *, reason: str = "") -> dict[str, list[str]]:
    with database.get_store() as store:
        pending_ids = [
            r["lead_id"]
            for r in store.list_email_jobs(
                campaign_id=campaign_id, review_status=email_generation.REVIEW_PENDING
            )
        ]
        result = email_generation.bulk_reject(store, pending_ids, reason=reason)
        return {
            "succeeded": result.get("rejected", []),
            "failed": [item["lead_id"] for item in result.get("failed", [])],
        }
