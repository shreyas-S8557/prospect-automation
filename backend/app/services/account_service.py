from __future__ import annotations

from app.db import database
from app.schemas.account import AccountContactOut, AccountFunnelOut, AccountListOut, AccountOut

from pipeline.account_intelligence import account_funnel, group_leads_into_accounts


def list_accounts(campaign_id: str) -> AccountListOut:
    with database.get_store() as store:
        leads = store.all(campaign_id=campaign_id)
    accounts = group_leads_into_accounts(leads)
    items = [
        AccountOut(
            company_key=a.company_key,
            company_name=a.company_name,
            company_domain=a.company_domain,
            industry=a.industry,
            company_size=a.company_size,
            contact_count=a.contact_count,
            senior_contact_count=len(a.senior_contacts),
            practitioner_contact_count=len(a.practitioner_contacts),
            potential_champion_count=len(a.potential_champions),
            decision_maker_candidate_count=len(a.decision_maker_candidates),
            has_relevant_signal=a.has_relevant_signal,
            contacts=[AccountContactOut(**c.to_dict()) for c in a.contacts],
            best_contact_path=[c.lead_id for c in a.best_contact_path()],
        )
        for a in accounts
    ]
    return AccountListOut(campaign_id=campaign_id, items=items, total=len(items))


def get_account_funnel(campaign_id: str) -> AccountFunnelOut:
    with database.get_store() as store:
        leads = store.all(campaign_id=campaign_id)
    accounts = group_leads_into_accounts(leads)
    stages = account_funnel(accounts)
    return AccountFunnelOut(campaign_id=campaign_id, stages=stages)
