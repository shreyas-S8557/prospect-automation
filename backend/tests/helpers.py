from __future__ import annotations

import os


def seed_qualified_leads(campaign_id: str, count: int = 3):
    """Insert `count` QUALIFIED leads directly via LeadStore, bypassing the
    network-dependent discovery stage -- this is what PHASE 24's "Use mocks
    for external APIs" means for discovery specifically: the pipeline's own
    per-lead functions (email_discovery/validation/generation/sending) are
    exercised for real, only the network-hitting discovery search is
    skipped in favor of directly-seeded data.
    """
    from app.db import database
    from pipeline.models import Lead, PipelineStatus

    with database.get_store() as store:
        leads = []
        for i in range(count):
            lead = Lead(
                first_name=f"Jane{i}",
                last_name="Doe",
                job_title="Founder & CEO",
                company_name=f"Acme{i} Inc",
                industry="SaaS",
                location="United States",
                linkedin_url=f"https://linkedin.com/in/jane{i}-{campaign_id}",
                campaign_id=campaign_id,
                pipeline_status=PipelineStatus.DISCOVERED.value,
                email=f"jane{i}@acme{i}-{campaign_id}.com",
                identity_key=f"key-{campaign_id}-{i}",
            )
            store.upsert_lead(lead)
            store.transition(lead.lead_id, PipelineStatus.QUALIFIED)
            leads.append(lead)
        return leads
