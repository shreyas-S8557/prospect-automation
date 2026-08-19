from __future__ import annotations


def _create_campaign(client):
    return client.post(
        "/api/campaigns",
        json={"campaign_name": "Account Grouping Test", "target_leads": 10},
    ).json()


def _seed_lead(store, *, campaign_id, name, title, company, keyword_evidence="", industry_evidence=""):
    from pipeline.models import Lead, PipelineStatus
    from pipeline.qualification_scoring import build_qualification_evidence
    from pipeline.target_config import TargetConfig
    import json

    row = {
        "profile_title": title,
        "summary": f"{title} at {company}. Responsible for the platform.",
        "keyword_evidence": keyword_evidence,
        "industry_evidence": industry_evidence,
        "company_name": company,
    }
    target = TargetConfig(name="t", titles=["CEO", "CTO", "Engineer"], industries=["SaaS"], keywords=["AI"])
    evidence = build_qualification_evidence(row, target)

    lead = Lead(
        first_name=name.split()[0],
        last_name=" ".join(name.split()[1:]),
        job_title=title,
        company_name=company,
        industry="SaaS",
        location="United States",
        linkedin_url=f"https://linkedin.com/in/{name.lower().replace(' ', '-')}-{campaign_id}",
        campaign_id=campaign_id,
        pipeline_status=PipelineStatus.DISCOVERED.value,
        identity_key=f"key-{campaign_id}-{name}",
        keyword_evidence=keyword_evidence,
        industry_evidence=industry_evidence,
        qualification_evidence=json.dumps(evidence),
    )
    store.upsert_lead(lead)
    store.transition(lead.lead_id, PipelineStatus.QUALIFIED)
    return lead


def test_accounts_group_multiple_contacts_by_company(client):
    from app.db import database

    campaign = _create_campaign(client)
    cid = campaign["campaign_id"]

    with database.get_store() as store:
        # Two contacts at the same company (Acme AI): a CEO with no
        # ownership/problem evidence, and a practitioner engineer WITH
        # strong problem-proximity evidence -- this is the exact
        # "watermelon effect" scenario from the requirements: the
        # practitioner should be favored as champion/best-contact-path,
        # not the CEO by default.
        _seed_lead(
            store, campaign_id=cid, name="Pat CEO", title="CEO",
            company="Acme AI", keyword_evidence="", industry_evidence="",
        )
        _seed_lead(
            store, campaign_id=cid, name="Sam Engineer", title="ML Engineer",
            company="Acme AI",
            keyword_evidence="AI automation platform engineer",
            industry_evidence="SaaS",
        )
        # A third, unrelated company with a single contact.
        _seed_lead(
            store, campaign_id=cid, name="Lone Founder", title="Founder",
            company="Solo Co", keyword_evidence="", industry_evidence="",
        )

    r = client.get(f"/api/campaigns/{cid}/accounts")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2  # Acme AI + Solo Co

    acme = next(a for a in body["items"] if a["company_name"] == "Acme AI")
    assert acme["contact_count"] == 2
    assert acme["has_relevant_signal"] is True

    # The practitioner (Sam) should rank ahead of the CEO in the
    # recommended contact path, since Sam carries the actual problem
    # evidence and the CEO carries none.
    contacts_by_id = {c["lead_id"]: c for c in acme["contacts"]}
    sam = next(c for c in acme["contacts"] if c["job_title"] == "ML Engineer")
    pat = next(c for c in acme["contacts"] if c["job_title"] == "CEO")
    assert sam["role_relevance_signal"] in ("weak_signal", "strong_signal")
    assert pat["role_relevance_signal"] == "unknown"

    best_path = acme["best_contact_path"]
    assert best_path[0] == sam["lead_id"], "practitioner with real evidence should outrank a title-only CEO"

    solo = next(a for a in body["items"] if a["company_name"] == "Solo Co")
    assert solo["contact_count"] == 1


def test_account_funnel_counts(client):
    from app.db import database

    campaign = _create_campaign(client)
    cid = campaign["campaign_id"]

    with database.get_store() as store:
        _seed_lead(
            store, campaign_id=cid, name="A One", title="CEO",
            company="MultiCo", keyword_evidence="", industry_evidence="",
        )
        _seed_lead(
            store, campaign_id=cid, name="B Two", title="Engineering Manager",
            company="MultiCo",
            keyword_evidence="AI automation team", industry_evidence="SaaS",
        )
        _seed_lead(
            store, campaign_id=cid, name="C Solo", title="Founder",
            company="SingleCo", keyword_evidence="", industry_evidence="",
        )

    r = client.get(f"/api/campaigns/{cid}/accounts/funnel")
    assert r.status_code == 200
    stages = r.json()["stages"]
    assert stages["accounts_discovered"] == 2
    assert stages["accounts_with_relevant_signal"] == 1  # MultiCo only
    assert stages["accounts_with_multiple_contacts"] == 1  # MultiCo only
    assert stages["qualified_accounts"] <= stages["accounts_with_relevant_signal"]


def test_accounts_404_for_unknown_campaign(client):
    r = client.get("/api/campaigns/does-not-exist/accounts")
    assert r.status_code == 404
