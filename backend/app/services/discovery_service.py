from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from app.db import database
from app.services.campaign_service import get_target_config
from app.workers.jobs import Job, JobControl, job_manager

from pipeline import campaign as campaign_pipeline
from pipeline import lead_pipeline
from pipeline import orchestrator
from pipeline.lead_store import LeadStore
from pipeline.models import InvestorRow, PipelineStatus
from pipeline.qualification_scoring import qualification_evidence_json

logger = logging.getLogger(__name__)


def _run_discovery_job(job: Job, ctl: JobControl, campaign_id: str) -> dict[str, Any]:
    """
    ROOT CAUSE (see fix notes in DEVELOPMENT.md / PR description):
    `orchestrator.run_discovery()` used to be called once, fully blocking,
    and this function only wrote anything to SQLite (and only advanced job
    progress) AFTER it returned. Since target_count is small (e.g. 10) but
    the query list is huge (3774) and several search backends are
    slow/rate-limited, that "after" could be minutes away -- so every API
    the frontend polls (`/stats`, `/prospects`, `/funnel`, `/jobs/{id}`)
    legitimately had nothing to report the entire time, even though DDGS
    and the local LLM were both working. Fixed by wiring two callbacks
    (`on_candidate`, `on_query_progress`) into the ddgs phase so every
    accepted candidate is written to the DB and every counter advances the
    moment it happens, instead of only at the very end.
    """
    ctl.set_phase("discovery")
    target = get_target_config(campaign_id)
    app_cfg = database.get_campaign_config(campaign_id) or {}

    # "processed" tracks raw candidates discovered against the campaign's
    # target_count (never conflated with "queries attempted", which is
    # reported separately below via job.stats so the frontend can render
    # "6/3774 queries" without ever mislabeling it "6/10 prospects").
    ctl.set_total(target.target_count, phase="discovery")
    ctl.update_stats(
        target=target.target_count,
        queries_done=0,
        queries_total=0,
        raw_discovered=0,
        qualified=0,
        filtered_out=0,
    )
    ctl.checkpoint()

    discovered_count = 0

    with database.get_store() as store:
        campaign_pipeline.ensure_campaign(
            store,
            campaign_id,
            name=target.campaign_name or target.name,
            description=target.campaign_description,
            subject_template=target.email_subject_template,
            body_template=target.email_body_template,
            sender_name=target.email_sender_name,
        )

        def on_candidate(row: InvestorRow) -> None:
            # Persist immediately, status=DISCOVERED. Idempotent: keyed by
            # identity_key, so the later CSV-based ingest pass below simply
            # upserts the *same* leads again (picking up any fields only
            # finalized post-ddgs, e.g. industries/company_name/age) rather
            # than creating duplicates.
            nonlocal discovered_count
            ctl.checkpoint()
            lead = lead_pipeline.normalize_investor_row(row, campaign_id=campaign_id)
            store.upsert_lead(lead)
            discovered_count += 1
            ctl.advance(success=True, message=lead.full_name or lead.company_name or "")
            ctl.update_stats(raw_discovered=discovered_count)

        def on_query_progress(done: int, total_queries: int) -> None:
            ctl.checkpoint()
            ctl.update_stats(queries_done=done, queries_total=total_queries)

        csv_path: Path = orchestrator.run_discovery(
            target,
            phases="ddgs,classify,company_name,age,qualify",
            on_candidate=on_candidate,
            on_query_progress=on_query_progress,
        )
        ctl.checkpoint()
        ctl.set_phase("ingest")

        with csv_path.open(encoding="utf-8", newline="") as f:
            rows: list[InvestorRow] = list(csv.DictReader(f))  # type: ignore[assignment]

        created = updated = qualified = filtered_out = 0

        pre_qualified = [r for r in rows if (r.get("qualification_status") or "").strip()]
        to_qualify_fresh = [r for r in rows if not (r.get("qualification_status") or "").strip()]

        # Re-upsert every row from the finished CSV: this is what carries
        # over enrichment fields (industries/company_name/age) computed in
        # the classify/company_name/age phases, which run *after* ddgs and
        # so weren't known yet when on_candidate() first wrote the bare
        # discovery-time row. Cheap and safe -- upsert_lead is a no-op
        # write for anything unchanged.
        for row in rows:
            lead = lead_pipeline.normalize_investor_row(row, campaign_id=campaign_id)
            _, was_created = store.upsert_lead(lead)
            created += int(was_created)
            updated += int(not was_created)

        by_key = {
            lead.identity_key: lead
            for lead in store.list_by_status(PipelineStatus.DISCOVERED, campaign_id=campaign_id)
        }
        for row in pre_qualified:
            probe = lead_pipeline.normalize_investor_row(row, campaign_id=campaign_id)
            lead = by_key.get(probe.identity_key)
            if lead is None:
                continue
            # Evidence-based BUND/champion scoring, computed here (not via
            # qualify_lead()) because this branch is for rows the orchestrator
            # already qualified inline (qualification_status pre-set on the
            # CSV row) -- qualify_lead()'s own matches_target_criteria() call
            # is intentionally skipped for these, so its scoring step is too.
            # Never affects pass/fail; purely additive evidence.
            lead.keyword_evidence = row.get("keyword_evidence", "") or lead.keyword_evidence
            lead.industry_evidence = row.get("industry_evidence", "") or lead.industry_evidence
            lead.qualification_evidence = qualification_evidence_json(row, target)
            store.save(lead)
            status = (row.get("qualification_status") or "").strip().lower()
            if status == "qualified":
                store.transition(lead.lead_id, PipelineStatus.QUALIFIED)
                qualified += 1
            else:
                store.transition(lead.lead_id, PipelineStatus.FILTERED_OUT)
                filtered_out += 1
            ctl.update_stats(qualified=qualified, filtered_out=filtered_out)

        if to_qualify_fresh:
            qstats = lead_pipeline.qualify_pending_leads(
                store, campaign_id=campaign_id, target=target
            )
            qualified += qstats["qualified"]
            filtered_out += qstats["filtered_out"]
            ctl.update_stats(qualified=qualified, filtered_out=filtered_out)

    return {
        "discovered": max(len(rows), discovered_count),
        "created": created,
        "updated": updated,
        "qualified": qualified,
        "filtered_out": filtered_out,
        "csv_path": str(csv_path),
    }


def start_discovery(campaign_id: str) -> Job:
    return job_manager.create(
        "discovery", campaign_id, lambda job, ctl: _run_discovery_job(job, ctl, campaign_id)
    )


def _run_qualification_job(job: Job, ctl: JobControl, campaign_id: str) -> dict[str, Any]:
    ctl.set_phase("qualification")
    target = get_target_config(campaign_id)
    with database.get_store() as store:
        pending = store.list_by_status(PipelineStatus.DISCOVERED, campaign_id=campaign_id)
        ctl.set_total(len(pending), phase="qualification")
        qualified = filtered_out = 0
        for lead in pending:
            result = lead_pipeline.qualify_lead(store, lead, target)
            ok = result.status == PipelineStatus.QUALIFIED
            qualified += int(ok)
            filtered_out += int(not ok)
            ctl.advance(success=ok, message=f"{lead.full_name or lead.lead_id}")
    return {"qualified": qualified, "filtered_out": filtered_out}


def start_qualification(campaign_id: str) -> Job:
    return job_manager.create(
        "qualification", campaign_id, lambda job, ctl: _run_qualification_job(job, ctl, campaign_id)
    )
